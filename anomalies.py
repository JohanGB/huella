"""Detectores de anomalías. Lógica pura: no importan psutil, Qt ni SQLite.

Reciben `Snapshot` y devuelven alertas. Eso permite probarlos con series
sintéticas y reproducir un caso de 30 minutos en milisegundos.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from statistics import median
from typing import Callable, Iterable, NamedTuple, Sequence

from config import Config
from metrics import ProcSample, Snapshot

Key = tuple[int, float]

KIND_LEAK = "fuga_memoria"
KIND_HOT = "cpu_sostenida"


@dataclass
class Alert:
    kind: str
    app: str
    pid: int
    started: float
    detail: str
    id: str = ""
    ended: float | None = None

    def __post_init__(self) -> None:
        if not self.id:
            self.id = f"{self.kind}:{self.pid}:{self.started:.0f}"


class TrendStats(NamedTuple):
    slope: float   # Theil-Sen, MB por punto
    tau: float     # Kendall, -1 a 1
    tail: float    # Theil-Sen del último tercio


def trend(series: Sequence[float]) -> TrendStats:
    """Pendiente y monotonía robustas. O(n²) sobre pocos puntos, solo candidatos.

    Theil-Sen y Kendall comparten el bucle de pares. Ninguno se deja arrastrar
    por un pico aislado, a diferencia de una regresión lineal.
    """
    series = list(series)   # puede llegar un deque, que no admite cortes
    n = len(series)
    slopes: list[float] = []
    score = 0
    for i in range(n - 1):
        yi = series[i]
        for j in range(i + 1, n):
            diff = series[j] - yi
            slopes.append(diff / (j - i))
            score += (diff > 0) - (diff < 0)
    pairs = n * (n - 1) / 2
    tail_start = n - max(3, n // 3)
    tail = series[tail_start:]
    tail_slopes = [(tail[j] - tail[i]) / (j - i)
                   for i in range(len(tail) - 1) for j in range(i + 1, len(tail))]
    return TrendStats(median(slopes), score / pairs, median(tail_slopes))


def is_leaking(series: Sequence[float], cfg: Config) -> TrendStats | None:
    """Devuelve las estadísticas si la serie (MB) crece sin estabilizarse."""
    if len(series) < cfg.leak_points:
        return None
    # Filtro barato: la mayoría de los procesos se descarta sin ordenar nada.
    growth = series[-1] - series[0]
    if growth < cfg.leak_min_growth_mb or series[0] <= 0:
        return None
    if growth / series[0] * 100.0 < cfg.leak_min_growth_pct:
        return None

    stats = trend(series)
    if stats.slope <= 0 or stats.tau < cfg.leak_min_tau:
        return None
    # Una caché que se llena también sube: la diferencia es que termina por
    # aplanarse. Si el último tercio ya casi no crece, no es una fuga.
    if stats.tail < stats.slope * cfg.leak_tail_ratio:
        return None
    return stats


@dataclass
class _LeakTrack:
    series: deque
    bucket_min: float = float("inf")
    bucket_elapsed: float = 0.0
    misses: int = 0
    alert: Alert | None = None


@dataclass
class _HotTrack:
    started: float
    hot_s: float = 0.0
    cold_s: float = 0.0
    threads: dict | None = None
    alert: Alert | None = None


class LeakDetector:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._tracks: dict[Key, _LeakTrack] = {}

    def reset(self) -> None:
        self._tracks.clear()

    def feed(self, snap: Snapshot) -> tuple[list[Alert], list[Alert]]:
        raised: list[Alert] = []
        cleared: list[Alert] = []
        seen: set[Key] = set()

        for p in snap.procs:
            seen.add(p.key)
            track = self._tracks.get(p.key)
            if track is None:
                if p.private_mb < self.cfg.leak_track_min_mb:
                    continue
                track = _LeakTrack(deque(maxlen=self.cfg.leak_points))
                self._tracks[p.key] = track

            # Un punto por minuto con el MÍNIMO del minuto: lo que importa en
            # una fuga es que suba el piso, no los picos de trabajo transitorio.
            track.bucket_min = min(track.bucket_min, p.private_mb)
            track.bucket_elapsed += snap.dt
            if track.bucket_elapsed < self.cfg.leak_sample_s:
                continue
            track.series.append(track.bucket_min)
            track.bucket_min, track.bucket_elapsed = float("inf"), 0.0
            self._evaluate(p, track, snap.wall, raised, cleared)

        for key in [k for k in self._tracks if k not in seen]:
            track = self._tracks.pop(key)
            if track.alert:
                track.alert.ended = snap.wall
                cleared.append(track.alert)
        return raised, cleared

    def _evaluate(self, p: ProcSample, track: _LeakTrack, now: float,
                  raised: list[Alert], cleared: list[Alert]) -> None:
        stats = is_leaking(track.series, self.cfg)
        track.misses = 0 if stats else track.misses + 1
        if stats and track.alert is None:
            per_hour = stats.slope * 3600.0 / self.cfg.leak_sample_s
            growth = track.series[-1] - track.series[0]
            track.alert = Alert(
                KIND_LEAK, p.name, p.pid, now,
                f"+{growth:,.0f} MB en la ventana, {per_hour:,.0f} MB/h, "
                f"tau {stats.tau:.2f}, ahora {p.private_mb:,.0f} MB")
            raised.append(track.alert)
        elif track.alert is not None and track.misses >= self.cfg.leak_clear_points:
            # Histéresis: una fuga con escalones no se apaga en cada meseta corta.
            track.alert.ended = now
            cleared.append(track.alert)
            track.alert = None


class HotDetector:
    """CPU sostenida con histéresis: un bajón breve no reinicia la racha."""

    def __init__(self, cfg: Config,
                 thread_probe: Callable[[int], dict[int, float]] | None = None,
                 temperature: Callable[[], float | None] | None = None) -> None:
        self.cfg = cfg
        self._probe = thread_probe or (lambda pid: {})
        self._temperature = temperature or (lambda: None)
        self._tracks: dict[Key, _HotTrack] = {}

    def reset(self) -> None:
        self._tracks.clear()

    def feed(self, snap: Snapshot) -> tuple[list[Alert], list[Alert]]:
        raised: list[Alert] = []
        cleared: list[Alert] = []
        limit_s = self.cfg.hot_minutes * 60.0
        seen: set[Key] = set()

        for p in snap.procs:
            seen.add(p.key)
            hot = p.core_pct >= self.cfg.hot_core_pct
            track = self._tracks.get(p.key)
            if track is None:
                if hot:
                    self._tracks[p.key] = _HotTrack(started=snap.wall - snap.dt, hot_s=snap.dt)
                continue

            if hot:
                track.hot_s += snap.dt
            else:
                track.cold_s += snap.dt
                if track.cold_s > max(snap.dt, track.hot_s * self.cfg.hot_tolerance):
                    self._tracks.pop(p.key)
                    if track.alert:
                        track.alert.ended = snap.wall
                        cleared.append(track.alert)
                    continue

            span = track.hot_s + track.cold_s
            # Foto de hilos a mitad de la racha, para poder restar al alertar.
            if track.threads is None and span >= limit_s / 2:
                track.threads = self._probe(p.pid)
            if track.alert is None and span >= limit_s:
                track.alert = self._raise(p, track, span)
                raised.append(track.alert)

        for key in [k for k in self._tracks if k not in seen]:
            track = self._tracks.pop(key)
            if track.alert:
                track.alert.ended = snap.wall
                cleared.append(track.alert)
        return raised, cleared

    def _raise(self, p: ProcSample, track: _HotTrack, span: float) -> Alert:
        detail = f"{p.core_pct:,.0f} % de un núcleo durante {span / 60:.1f} min"
        tid = dominant_thread(track.threads or {}, self._probe(p.pid))
        if tid is not None:
            detail += f", hilo dominante {tid}"
        temp = self._temperature()
        if temp is not None:
            detail += f", {temp:.0f} °C"
        return Alert(KIND_HOT, p.name, p.pid, track.started, detail)


def dominant_thread(before: dict[int, float], after: dict[int, float]) -> int | None:
    deltas = {tid: t - before.get(tid, 0.0) for tid, t in after.items()}
    if not deltas:
        return None
    tid = max(deltas, key=deltas.__getitem__)
    return tid if deltas[tid] > 0 else None


class Analyzer:
    """Une los detectores y lleva el conjunto de alertas activas."""

    def __init__(self, cfg: Config,
                 thread_probe: Callable[[int], dict[int, float]] | None = None,
                 temperature: Callable[[], float | None] | None = None) -> None:
        self.leaks = LeakDetector(cfg)
        self.hot = HotDetector(cfg, thread_probe, temperature)
        self.active: dict[str, Alert] = {}

    def reset(self) -> list[Alert]:
        """Tras un hueco, las series no son continuas: se empieza de cero."""
        self.leaks.reset()
        self.hot.reset()
        ended = list(self.active.values())
        self.active.clear()
        return ended

    def feed(self, snap: Snapshot) -> tuple[list[Alert], list[Alert]]:
        raised: list[Alert] = []
        cleared: list[Alert] = []
        for detector in (self.leaks, self.hot):
            r, c = detector.feed(snap)
            raised += r
            cleared += c
        for alert in raised:
            self.active[alert.id] = alert
        for alert in cleared:
            self.active.pop(alert.id, None)
        return raised, cleared


class AppTotals:
    """Acumulado diario por aplicación en RAM. Es el lote de 5 minutos.

    Guardar muestras crudas serían cientos de filas por tick. Al acumular, un
    lote tiene como máximo una fila por aplicación y día, sin importar cuántos
    ticks pasaron, y el tamaño de la base depende de apps × días.
    """

    __slots__ = ("cfg", "_rows")

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._rows: dict[tuple[str, str], list[float]] = {}

    def add(self, day: str, snap: Snapshot) -> None:
        per_app: dict[str, list[float]] = {}
        for p in snap.procs:
            app = per_app.setdefault(p.name, [0.0, 0.0, 0.0, 0.0])
            app[0] += p.cpu_delta_s
            app[1] += p.private_mb
            app[2] += p.read_delta
            app[3] += p.write_delta

        for name, (cpu, ram, read_b, write_b) in per_app.items():
            row = self._rows.setdefault((day, name), [0.0] * 6)
            row[0] += cpu
            row[1] += snap.dt                # segundos abierta
            row[2] += ram * snap.dt          # integral MB·s, para el promedio
            row[3] = max(row[3], ram)        # pico de la app, sumando sus procesos
            row[4] += read_b
            row[5] += write_b

    def drain(self) -> list[tuple]:
        """Entrega el lote y vacía el acumulado."""
        rows = self._rows
        self._rows = {}
        out: list[tuple] = []
        others: dict[str, list[float]] = {}
        for (day, app), r in rows.items():
            if r[0] < self.cfg.min_app_cpu_s and r[3] < self.cfg.min_app_ram_mb:
                o = others.setdefault(day, [0.0] * 6)
                o[0] += r[0]
                o[1] = max(o[1], r[1])
                o[2] += r[2]
                o[3] += r[3]
                o[4] += r[4]
                o[5] += r[5]
            else:
                out.append((day, app, *r))
        out.extend((day, "(otros)", *o) for day, o in others.items())
        return out

    def __len__(self) -> int:
        return len(self._rows)


def active_summary(alerts: Iterable[Alert]) -> str:
    return "; ".join(f"{a.app} ({a.kind.replace('_', ' ')})" for a in alerts)
