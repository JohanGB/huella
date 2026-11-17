"""Mide lo que Quietwatch le cuesta a TU equipo. Funciona igual en Windows y Linux.

    python tools/bench.py                 # todo, con la cadencia real
    python tools/bench.py --seconds 120   # ventanas de medición más largas
    python tools/bench.py --skip-qt       # sin la bandeja

Lanza a Quietwatch como proceso hijo y lo observa desde afuera con psutil, de modo
que la medición no contamina al medido.
"""
from __future__ import annotations

import argparse
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def child_headless(data_dir: str) -> None:
    from config import Config
    from daemon import TelemetryDaemon
    d = TelemetryDaemon(Config(), Path(data_dir))
    d.start()
    print("ready", flush=True)
    for line in sys.stdin:             # bloqueado en el kernel: 0 despertares
        cmd = line.strip()
        if cmd == "gaming on":
            d.set_gaming(True)
        elif cmd == "gaming off":
            d.set_gaming(False)
        elif cmd == "quit":
            break
        print("ok", flush=True)
    d.stop()


def child_qt(data_dir: str) -> None:
    from PyQt6.QtCore import QObject, pyqtSignal
    from PyQt6.QtWidgets import QApplication
    from config import Config
    from tray import Tray

    class Remote(QObject):
        command = pyqtSignal(str)

    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    tray = Tray(Config(), Path(data_dir))
    remote = Remote()

    def run(cmd: str) -> None:
        if cmd == "gaming on":
            tray.gaming_action.setChecked(True)
        elif cmd == "gaming off":
            tray.gaming_action.setChecked(False)
        elif cmd == "panel":
            tray.show_panel()
        elif cmd == "panel close" and tray.panel:
            tray.panel.close()
        elif cmd == "quit":
            app.quit()
        print("ok", flush=True)

    remote.command.connect(run)

    def reader() -> None:
        for line in sys.stdin:
            remote.command.emit(line.strip())

    threading.Thread(target=reader, daemon=True).start()
    tray.start()
    print("ready", flush=True)
    app.aboutToQuit.connect(tray.stop)
    app.exec()


class Probe:
    def __init__(self, mode: str, data_dir: str) -> None:
        self.child = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--child", mode, "--data-dir", data_dir],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        assert self.child.stdout.readline().strip() == "ready"
        self.proc = psutil.Process(self.child.pid)

    def send(self, cmd: str) -> None:
        self.child.stdin.write(cmd + "\n")
        self.child.stdin.flush()
        self.child.stdout.readline()

    def switches(self) -> dict[int, int]:
        """Despertares por hilo. En Linux, num_ctx_switches() solo lee
        /proc/PID/status, que corresponde al hilo principal: hay que sumar las
        tareas. Se compara por hilo para que uno que termina no reste."""
        tasks = Path(f"/proc/{self.proc.pid}/task")
        if not tasks.exists():
            c = self.proc.num_ctx_switches()
            return {0: c.voluntary + c.involuntary}
        total: dict[int, int] = {}
        for status in tasks.glob("*/status"):
            try:
                lines = status.read_text().splitlines()
            except OSError:
                continue
            total[int(status.parent.name)] = sum(
                int(line.split()[1]) for line in lines if "ctxt_switches" in line)
        return total

    def counters(self) -> tuple:
        t = self.proc.cpu_times()
        try:
            io = self.proc.io_counters()
            written = io.write_bytes
        except (psutil.AccessDenied, AttributeError):
            written = 0
        return t.user + t.system, self.switches(), written, 0

    def window(self, seconds: float) -> dict:
        a = self.counters()
        t0 = time.monotonic()
        time.sleep(seconds)
        b = self.counters()
        dt = time.monotonic() - t0
        wakes = sum(n - a[1].get(tid, 0) for tid, n in b[1].items())
        return dict(cpu_s=b[0] - a[0], cpu_pct=(b[0] - a[0]) / dt * 100,
                    wakeups=wakes, per_min=wakes / dt * 60,
                    written=b[2] - a[2], seconds=dt)

    def rss_mb(self) -> float:
        return self.proc.memory_info().rss / 1024 ** 2

    def close(self) -> None:
        self.send("quit")
        self.child.wait(15)


def fmt(w: dict) -> str:
    return (f"CPU {w['cpu_s'] * 1000:8.1f} ms ({w['cpu_pct']:.4f} %)  "
            f"cambios de contexto {w['wakeups']:6d} ({w['per_min']:.1f}/min)  "
            f"escrito {w['written'] / 1024:.0f} KB")


def bench_tick(ticks: int = 200) -> None:
    from metrics import Sampler
    s = Sampler()
    s.sample(15.0)
    costs, cpu = [], []
    for _ in range(ticks):
        c0 = time.process_time()
        snap = s.sample(15.0)
        cpu.append((time.process_time() - c0) * 1000)
        costs.append(snap.tick_ms)
        time.sleep(0.01)
    costs.sort()
    cpu.sort()
    print(f"  procesos: {len(snap.procs)}")
    print(f"  tick (pared): mediana {statistics.median(costs):.1f} ms, p95 {costs[int(ticks * .95)]:.1f} ms")
    print(f"  tick (CPU):   mediana {statistics.median(cpu):.1f} ms, p95 {cpu[int(ticks * .95)]:.1f} ms")
    med = statistics.median(cpu)
    print(f"  proyección a 15 s: {med / 15000 * 100:.4f} % de un núcleo")


def bench_mode(mode: str, seconds: float) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        p = Probe(mode, tmp)
        time.sleep(2)
        print(f"  RSS en marcha: {p.rss_mb():.1f} MB, hilos: {p.proc.num_threads()}")
        print(f"  ACTIVO  {seconds:.0f} s: {fmt(p.window(seconds))}")
        if mode == "qt":
            p.send("panel")
            time.sleep(3)
            print(f"  RSS con panel abierto: {p.rss_mb():.1f} MB")
            p.send("panel close")
            time.sleep(2)
            print(f"  RSS tras cerrar panel: {p.rss_mb():.1f} MB")
            time.sleep(40)   # el QThreadPool de Qt retira sus hilos a los 30 s
        p.send("gaming on")
        time.sleep(2)     # deja terminar el último lote
        gaming = p.window(seconds)
        print(f"  GAMING  {seconds:.0f} s: {fmt(gaming)}")
        p.send("gaming off")
        time.sleep(1)
        print(f"  VUELTA  {seconds:.0f} s: {fmt(p.window(seconds))}")
        p.close()


def main() -> None:
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", choices=["headless", "qt"])
    parser.add_argument("--data-dir")
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--skip-qt", action="store_true")
    args = parser.parse_args()

    if args.child == "headless":
        return child_headless(args.data_dir)
    if args.child == "qt":
        return child_qt(args.data_dir)

    print(f"Python {sys.version.split()[0]}, psutil {psutil.__version__}, {sys.platform}, "
          f"{psutil.cpu_count()} CPU")
    print("\n[1] Costo del tick")
    bench_tick()
    print("\n[2] Demonio sin interfaz")
    bench_mode("headless", args.seconds)
    if not args.skip_qt:
        print("\n[3] Bandeja con PyQt6")
        bench_mode("qt", args.seconds)


if __name__ == "__main__":
    main()
