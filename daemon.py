"""Demonio de Huella: hilos, ciclo de vida y modo Gaming.

Arquitectura: un solo reloj y una tubería.

    Qt (hilo principal)      nunca llama a psutil ni a SQLite para escribir
         │ set_gaming / poke / scan
         ▼
    collector ──queue──▶ analyzer ──queue──▶ writer
    ÚNICO con timeout    bloqueado en get()  bloqueado en get()

Solo `collector` tiene temporizador. `analyzer` y `writer` no saben de tiempo:
duermen en un `get()` sin timeout hasta que les llega trabajo. Por eso el modo
Gaming no tiene que apagar varios timers: basta con parquear al recolector y el
resto del sistema ya está dormido. Un hilo parqueado espera en el kernel y no
despierta: no hay `while pausado: sleep(1)`.
"""
from __future__ import annotations

import argparse
import logging
import queue
import signal
import sys
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Callable

import metrics
from anomalies import Alert, Analyzer, AppTotals, active_summary
from config import DB_NAME, Config, default_data_dir
from metrics import Sampler, Snapshot
from storage import Batch, Writer

log = logging.getLogger("huella")


class State(Enum):
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"


class Control(Enum):
    """Mensajes por la tubería. Recorrerla en orden garantiza que el último
    lote llegue a disco antes de que todo quede en reposo."""
    QUIESCE = "quiesce"   # hubo una pausa: vaciar, escribir y olvidar series
    STOP = "stop"


class Gate:
    """El único reloj del sistema. Lo consulta solo el hilo recolector."""

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._state = State.RUNNING
        self._poke = False

    @property
    def state(self) -> State:
        return self._state

    def set(self, state: State) -> None:
        with self._cv:
            if self._state is not State.STOPPED:
                self._state = state
            self._cv.notify_all()

    def poke(self) -> None:
        """Adelanta el siguiente tick, por ejemplo al abrir el panel."""
        with self._cv:
            self._poke = True
            self._cv.notify_all()

    def wait_turn(self, seconds: float, on_park: Callable[[], None]) -> State:
        """Espera hasta el próximo tick. Devuelve RUNNING o STOPPED.

        Si se pidió pausa, avisa con `on_park` (fuera del candado) y se bloquea
        SIN timeout hasta que alguien cambie el estado.
        """
        deadline = time.monotonic() + seconds
        while True:
            with self._cv:
                if self._state is State.STOPPED:
                    return State.STOPPED
                if self._state is State.RUNNING:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or self._poke:
                        self._poke = False
                        return State.RUNNING
                    self._cv.wait(remaining)
                    continue
            on_park()
            with self._cv:
                while self._state is State.PAUSED:
                    self._cv.wait()          # 0 despertares mientras dure
                if self._state is State.STOPPED:
                    return State.STOPPED
                self._poke = False
                return State.RUNNING


def next_interval(cfg: Config, cpu_pct: float, tick_ms: float) -> float:
    """Polling inteligente: 10 s con el equipo ocupado, 15 s en calma, y nunca
    más del 2 % de ciclo útil aunque un tick salga caro."""
    base = cfg.poll_busy_s if cpu_pct >= cfg.busy_cpu_pct else cfg.poll_idle_s
    return max(base, (tick_ms / 1000.0) / cfg.max_duty)


class TelemetryDaemon:
    def __init__(self, cfg: Config | None = None, data_dir: Path | None = None,
                 on_alerts: Callable[[list[Alert]], None] | None = None) -> None:
        self.cfg = cfg or Config()
        self.data_dir = data_dir or default_data_dir()
        self.db_path = self.data_dir / DB_NAME
        self._on_alerts = on_alerts or (lambda alerts: None)

        self._gate = Gate()
        self._to_analyzer: queue.Queue = queue.Queue(maxsize=8)
        self._to_writer: queue.Queue = queue.Queue(maxsize=8)
        self._lock = threading.Lock()         # protege _active y _scan
        self._active: list[Alert] = []
        self._scan: threading.Thread | None = None
        self._scan_cancel = threading.Event()

        # Lecturas atómicas para el panel: se reemplaza la referencia entera.
        self.latest: Snapshot | None = None
        self.ticks = 0
        self.flushes = 0
        self.dropped = 0
        self.self_cpu_pct = 0.0

        self._threads = [
            threading.Thread(target=self._collector_loop, name="huella-collector", daemon=True),
            threading.Thread(target=self._analyzer_loop, name="huella-analyzer", daemon=True),
            threading.Thread(target=self._writer_loop, name="huella-writer", daemon=True),
        ]
        self._writer_ready = threading.Event()

    # --- API pública: segura desde cualquier hilo, no bloquea -----------------

    def start(self) -> None:
        for thread in reversed(self._threads):   # consumidores antes que productor
            thread.start()
        self._writer_ready.wait(10.0)

    def stop(self, timeout: float = 10.0) -> None:
        self._scan_cancel.set()
        self._gate.set(State.STOPPED)
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))

    def set_gaming(self, enabled: bool) -> None:
        if enabled:
            self._scan_cancel.set()
        self._gate.set(State.PAUSED if enabled else State.RUNNING)

    @property
    def gaming(self) -> bool:
        return self._gate.state is State.PAUSED

    def poke(self) -> None:
        self._gate.poke()

    def active_alerts(self) -> list[Alert]:
        with self._lock:
            return list(self._active)

    def start_scan(self, root: str,
                   on_progress: Callable[[int, int], None],
                   on_done: Callable[[list, bool], None]) -> bool:
        """Escaneo bajo demanda en un hilo propio que muere al terminar."""
        from scanner import scan   # no se carga si nunca se escanea
        with self._lock:
            if self.gaming or (self._scan and self._scan.is_alive()):
                return False
            self._scan_cancel = cancel = threading.Event()

            def work() -> None:
                rows, cancelled = scan(root, self.cfg, cancel, on_progress)
                if not cancelled:
                    self._put(self._to_writer, ("files", rows))
                on_done(rows, cancelled)

            self._scan = threading.Thread(target=work, name="huella-scan", daemon=True)
            self._scan.start()
        return True

    def cancel_scan(self) -> None:
        self._scan_cancel.set()

    # --- Hilo 1: recolector. El único con temporizador ------------------------

    def _collector_loop(self) -> None:
        sampler = Sampler()
        interval = self.cfg.poll_idle_s
        pending_reset = False

        def on_park() -> None:
            nonlocal pending_reset
            pending_reset = True
            self._put(self._to_analyzer, Control.QUIESCE)

        cpu_mark, wall_mark = metrics.self_cpu_seconds(), time.monotonic()
        while True:
            try:
                snap = sampler.sample(interval)
                self.latest = snap
                self.ticks += 1
                self._put(self._to_analyzer, snap)
                interval = next_interval(self.cfg, snap.cpu_pct, snap.tick_ms)

                now = time.monotonic()
                if now - wall_mark >= 60.0:
                    cpu = metrics.self_cpu_seconds()
                    self.self_cpu_pct = (cpu - cpu_mark) / (now - wall_mark) * 100.0
                    cpu_mark, wall_mark = cpu, now
            except Exception:
                log.exception("Tick fallido; se sigue en el siguiente")

            if self._gate.wait_turn(interval, on_park) is State.STOPPED:
                break
            if pending_reset:
                pending_reset = False
                sampler.reset()
                cpu_mark, wall_mark = metrics.self_cpu_seconds(), time.monotonic()
        self._to_analyzer.put(Control.STOP)

    # --- Hilo 2: analizador. Bloqueado en get() sin timeout -------------------

    def _analyzer_loop(self) -> None:
        analyzer = Analyzer(self.cfg, metrics.hottest_threads, metrics.read_temperature)
        totals = AppTotals(self.cfg)
        alerts: list[Alert] = []
        last_flush = time.monotonic()

        def flush() -> None:
            nonlocal alerts, last_flush
            batch = Batch(totals.drain(), alerts)
            alerts = []
            last_flush = time.monotonic()
            if batch:
                self._put(self._to_writer, batch)

        def clear_all(now: float) -> None:
            ended = analyzer.reset()
            for alert in ended:
                alert.ended = now
            alerts.extend(ended)
            self._publish(analyzer, ended)

        while True:
            msg = self._to_analyzer.get()
            try:
                if msg is Control.STOP:
                    clear_all(time.time())
                    flush()
                    break
                if msg is Control.QUIESCE:
                    # El último tick pudo llegar antes: todo queda en disco y
                    # los hilos se duermen con los búferes vacíos.
                    clear_all(time.time())
                    flush()
                    continue
                self._consume(msg, analyzer, totals, alerts, clear_all)
                if time.monotonic() - last_flush >= self.cfg.flush_every_s:
                    flush()
            except Exception:
                log.exception("Fallo al analizar un tick")
        self._to_writer.put(Control.STOP)

    def _consume(self, snap: Snapshot, analyzer: Analyzer, totals: AppTotals,
                 alerts: list[Alert], clear_all: Callable[[float], None]) -> None:
        if snap.gap:
            clear_all(snap.wall)
        if snap.baseline or snap.gap:
            return
        totals.add(time.strftime("%Y-%m-%d", time.localtime(snap.wall)), snap)
        raised, cleared = analyzer.feed(snap)
        changed = raised + cleared
        if changed:
            alerts.extend(changed)
            self._publish(analyzer, changed)

    def _publish(self, analyzer: Analyzer, changed: list[Alert]) -> None:
        with self._lock:
            self._active = list(analyzer.active.values())
        if changed:
            log.info("Alertas activas: %s", active_summary(self._active) or "ninguna")
            self._on_alerts(changed)

    # --- Hilo 3: escritor. Dueño exclusivo de la conexión de escritura --------

    def _writer_loop(self) -> None:
        writer = Writer(self.db_path, self.cfg.retention_days)
        self._writer_ready.set()
        retry: list[Batch] = []
        while True:
            msg = self._to_writer.get()
            if msg is Control.STOP:
                break
            if isinstance(msg, tuple):     # resultado del escáner
                try:
                    writer.replace_files(msg[1])
                except Exception:
                    log.exception("No se pudo guardar el escaneo")
                continue
            retry = self._write_all(writer, retry + [msg])
        self._write_all(writer, retry)
        writer.close()

    def _write_all(self, writer: Writer, batches: list[Batch]) -> list[Batch]:
        """Devuelve lo que no se pudo escribir, con tope para no crecer sin fin."""
        for index, batch in enumerate(batches):
            try:
                writer.write(batch)
                self.flushes += 1
            except Exception:
                log.exception("Lote no escrito; se reintenta con el siguiente")
                return batches[index:][-12:]   # una hora de lotes
        return []

    def _put(self, q: queue.Queue, item: object) -> None:
        """El recolector nunca se bloquea por un consumidor lento."""
        try:
            q.put_nowait(item)
        except queue.Full:
            if isinstance(item, Control):
                q.put(item)                    # los mensajes de control no se pierden
            else:
                self.dropped += 1


def main() -> int:
    """Modo sin interfaz, útil en Linux sin escritorio. En escritorio usa tray.py."""
    parser = argparse.ArgumentParser(description="Huella sin interfaz")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")

    data_dir = args.data_dir or default_data_dir()
    daemon = TelemetryDaemon(Config.load(data_dir), data_dir)
    done = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: done.set())
    signal.signal(signal.SIGTERM, lambda *_: done.set())
    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, lambda *_: daemon.set_gaming(not daemon.gaming))

    daemon.start()
    if sys.platform == "win32":
        # En Windows una espera sin timeout no atiende Ctrl+C. Solo afecta a este
        # modo de consola: la bandeja se apoya en el bucle de eventos de Qt.
        while not done.wait(1.0):
            pass
    else:
        done.wait()
    daemon.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
