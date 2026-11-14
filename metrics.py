"""Recolección de métricas con psutil. Solo lectura.

Este módulo mide y no decide. Cada `sample()` devuelve un `Snapshot` inmutable
que el resto del sistema consume sin volver a tocar psutil.

Por qué deltas propios en vez de `Process.cpu_percent()`:
  * `cpu_times()` es un contador acumulado: el trabajo hecho entre dos ticks
    queda contado aunque el tick llegue tarde o el intervalo cambie.
  * El histórico mensual necesita CPU-segundos, no porcentajes promediados.
  * La clave (pid, create_time) evita sumar contadores de dos procesos
    distintos cuando el sistema reutiliza un PID.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Iterable, NamedTuple

import psutil

WINDOWS = sys.platform == "win32"
MB = 1024 * 1024

ATTRS = ["name", "create_time", "cpu_times", "memory_info", "io_counters"]

# Seudoprocesos: su "uso" es tiempo inactivo o el kernel, no una aplicación.
_IGNORED_NAMES = {"system idle process", "idle", "system", "registry",
                  "memory compression", "secure system"}


class ProcSample(NamedTuple):
    pid: int
    create_time: float
    name: str
    cpu_delta_s: float     # CPU-segundos consumidos desde el tick anterior
    core_pct: float        # 100 = un núcleo lógico entero
    private_mb: float      # memoria privada (ver private_bytes)
    read_delta: int        # bytes leídos desde el tick anterior
    write_delta: int       # bytes escritos desde el tick anterior

    @property
    def key(self) -> tuple[int, float]:
        return self.pid, self.create_time


class Snapshot(NamedTuple):
    wall: float            # time.time() del tick
    dt: float              # segundos desde el tick anterior
    gap: bool              # hubo un hueco: suspensión, pausa o tick muy tardío
    baseline: bool         # primer tick tras (re)arrancar: deltas sin significado
    cpu_pct: float         # CPU global, 0-100
    ram_pct: float
    ram_used_mb: float
    ram_total_mb: float
    swap_pct: float
    tick_ms: float         # costo de pared de este tick
    procs: tuple[ProcSample, ...]


class _Prev(NamedTuple):
    cpu_s: float
    read_b: int
    write_b: int


def private_bytes(mem: object, windows: bool = WINDOWS) -> int:
    """Memoria que el proceso no comparte: la métrica correcta para una fuga.

    Windows: `private` (Private Bytes). A diferencia del Working Set, no baja
    cuando el sistema recorta la RAM del proceso, así que una fuga sigue
    subiendo aunque sus páginas se vayan al archivo de paginación.

    Linux: RSS menos compartida. Es una aproximación barata; USS es más exacta,
    pero `memory_full_info()` recorre smaps y costó unas 27 veces más.
    """
    if windows:
        private = getattr(mem, "private", None)
        if private:
            return int(private)
    rss = int(getattr(mem, "rss", 0))
    return max(rss - int(getattr(mem, "shared", 0)), 0)


def clean_text(text: str) -> str:
    """Nombres con bytes inválidos llegan con sustitutos que SQLite no acepta."""
    return text if text.isascii() else text.encode("utf-8", "replace").decode("utf-8")


def is_pseudo_process(pid: int, name: str) -> bool:
    if pid == 0:
        return True
    return WINDOWS and name.lower() in _IGNORED_NAMES


class Sampler:
    """Lee el sistema. Lo usa un único hilo; no es seguro entre hilos."""

    def __init__(self, min_gap_s: float = 60.0, gap_factor: float = 4.0) -> None:
        self._prev: dict[tuple[int, float], _Prev] = {}
        self._mono = 0.0
        self._wall = 0.0
        self._min_gap_s = min_gap_s
        self._gap_factor = gap_factor
        self.reset()

    def reset(self) -> None:
        """Olvida el estado: el siguiente tick solo fija la línea base."""
        self._prev.clear()
        self._mono = 0.0
        self._wall = 0.0
        psutil.cpu_percent(interval=None)  # no bloquea; fija la referencia global

    def sample(self, expected_s: float) -> Snapshot:
        t0 = time.perf_counter()
        mono, wall = time.monotonic(), time.time()

        baseline = self._mono == 0.0
        dt = 0.0 if baseline else mono - self._mono
        # Se revisan los dos relojes: según la plataforma, el monótono puede
        # no avanzar durante una suspensión, y el de pared sí.
        elapsed = 0.0 if baseline else max(dt, wall - self._wall)
        gap = not baseline and elapsed > max(self._min_gap_s, expected_s * self._gap_factor)
        prev_wall = self._wall

        procs = tuple(self._read_processes(dt, prev_wall, baseline))

        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()
        cpu = psutil.cpu_percent(interval=None)

        self._mono, self._wall = mono, wall
        return Snapshot(
            wall=wall, dt=dt, gap=gap, baseline=baseline, cpu_pct=cpu,
            ram_pct=vm.percent, ram_used_mb=(vm.total - vm.available) / MB,
            ram_total_mb=vm.total / MB, swap_pct=swap.percent,
            tick_ms=(time.perf_counter() - t0) * 1000.0, procs=procs,
        )

    def _read_processes(self, dt: float, prev_wall: float,
                        baseline: bool) -> Iterable[ProcSample]:
        current: dict[tuple[int, float], _Prev] = {}
        out: list[ProcSample] = []

        # process_iter reutiliza objetos Process y as_dict agrupa las lecturas
        # con oneshot(): en Linux, una lectura de /proc por proceso en vez de varias.
        for proc in psutil.process_iter():
            try:
                info = proc.as_dict(attrs=ATTRS, ad_value=None)
            except psutil.Error:
                continue  # terminó entre la enumeración y la lectura

            create_time = info["create_time"]
            times = info["cpu_times"]
            mem = info["memory_info"]
            name = clean_text(info["name"] or f"pid-{proc.pid}")
            if create_time is None or times is None or is_pseudo_process(proc.pid, name):
                continue

            cpu_s = times.user + times.system
            io = info["io_counters"]
            read_b = io.read_bytes if io else 0
            write_b = io.write_bytes if io else 0
            key = (proc.pid, create_time)
            current[key] = _Prev(cpu_s, read_b, write_b)

            old = self._prev.get(key)
            if old is None:
                # Nació después del tick anterior: todo su consumo es de este
                # intervalo. Si no, es un proceso ya visto en la línea base.
                old = (_Prev(0.0, 0, 0) if not baseline and create_time >= prev_wall
                       else _Prev(cpu_s, read_b, write_b))

            cpu_delta = max(cpu_s - old.cpu_s, 0.0)
            out.append(ProcSample(
                pid=proc.pid, create_time=create_time, name=name,
                cpu_delta_s=cpu_delta,
                core_pct=(cpu_delta / dt * 100.0) if dt > 0 else 0.0,
                private_mb=(private_bytes(mem) / MB) if mem else 0.0,
                read_delta=max(read_b - old.read_b, 0),
                write_delta=max(write_b - old.write_b, 0),
            ))

        self._prev = current
        return out


def hottest_threads(pid: int) -> dict[int, float]:
    """CPU acumulada por hilo. Se llama solo sobre sospechosos, nunca en cada tick."""
    try:
        return {t.id: t.user_time + t.system_time for t in psutil.Process(pid).threads()}
    except psutil.Error:
        return {}


def read_temperature() -> float | None:
    """Temperatura máxima si el SO la expone a psutil (Linux). En Windows: None."""
    reader = getattr(psutil, "sensors_temperatures", None)
    if reader is None:
        return None
    try:
        readings = [t.current for group in reader().values() for t in group if t.current]
    except (OSError, RuntimeError, ValueError):
        return None
    return max(readings) if readings else None


def format_bytes(size: float) -> str:
    """Tamaño legible, en MB o GB como pide la tabla de archivos."""
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:,.2f} GB"
    return f"{size / MB:,.1f} MB"


def self_cpu_seconds() -> float:
    return time.process_time()


def logical_cpus() -> int:
    return os.cpu_count() or 1
