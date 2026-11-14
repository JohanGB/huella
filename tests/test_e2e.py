"""Procesos reales contra el demonio real, con el tiempo comprimido."""
import subprocess
import sys
import time

import pytest

from anomalies import KIND_HOT, KIND_LEAK
from config import Config
from daemon import TelemetryDaemon
from storage import Reader

LEAK = """
import time
hoard = []
for _ in range(600):
    hoard.append(bytearray(6 * 1024 * 1024))
    for block in hoard[-1:]:
        block[::4096] = b"x" * len(block[::4096])
    time.sleep(0.1)
"""
CACHE = """
import time
hoard = []
for _ in range(30):
    hoard.append(bytearray(6 * 1024 * 1024))
    hoard[-1][::4096] = b"x" * len(hoard[-1][::4096])
    time.sleep(0.05)
time.sleep(600)
"""
SPIN = "while True: pass"
QUIET = "import time; time.sleep(600)"


def test_real_leak_and_spinner_are_found_and_controls_are_not(tmp_path):
    cfg = Config(poll_busy_s=0.25, poll_idle_s=0.25, max_duty=1.0, flush_every_s=1.0,
                 leak_window_min=0.25, leak_sample_s=0.75, leak_track_min_mb=20,
                 leak_min_growth_mb=40, hot_minutes=0.1, min_app_cpu_s=0, min_app_ram_mb=0)
    procs = {name: subprocess.Popen([sys.executable, "-c", code])
             for name, code in dict(leak=LEAK, cache=CACHE, spin=SPIN, quiet=QUIET).items()}
    daemon = TelemetryDaemon(cfg, tmp_path)
    daemon.start()
    try:
        end = time.monotonic() + 40
        while time.monotonic() < end:
            active = {(a.pid, a.kind) for a in daemon.active_alerts()}
            if {(procs["leak"].pid, KIND_LEAK), (procs["spin"].pid, KIND_HOT)} <= active:
                break
            time.sleep(0.2)
        active = daemon.active_alerts()
        pids = {a.pid: a.kind for a in active}
        assert pids.get(procs["leak"].pid) == KIND_LEAK
        assert procs["spin"].pid in pids
        assert procs["cache"].pid not in pids
        assert procs["quiet"].pid not in pids

        spin = next(a for a in active if a.pid == procs["spin"].pid)
        assert "hilo dominante" in spin.detail

        procs["spin"].terminate()
        procs["spin"].wait()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(
                a.pid == procs["spin"].pid for a in daemon.active_alerts()):
            time.sleep(0.05)
        assert not any(a.pid == procs["spin"].pid for a in daemon.active_alerts())
    finally:
        for p in procs.values():
            p.terminate()
            p.wait()
        daemon.stop()

    db = Reader(daemon.db_path)
    rows = db.alerts()
    closed = [r for r in rows if r[3] and r[4] == procs["spin"].pid]
    assert closed and closed[0][1] is not None
    assert db.monthly(db.months()[0])
    db.close()
