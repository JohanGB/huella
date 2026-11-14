import sys
import threading
import time

import pytest

import daemon as daemon_mod
from anomalies import Alert
from config import Config
from daemon import Gate, State, TelemetryDaemon, next_interval
from storage import Reader

FAST = Config(poll_busy_s=0.05, poll_idle_s=0.05, max_duty=1.0, flush_every_s=0.2,
              min_app_cpu_s=0.0, min_app_ram_mb=0.0, scan_duty=1.0)


def wait_for(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def d(tmp_path):
    daemon = TelemetryDaemon(FAST, tmp_path)
    daemon.start()
    yield daemon
    daemon.stop()


def test_interval_depends_on_load():
    cfg = Config()
    assert next_interval(cfg, 90.0, 5.0) == 10.0
    assert next_interval(cfg, 5.0, 5.0) == 15.0


def test_interval_enforces_duty_cap():
    assert next_interval(Config(), 90.0, 1000.0) == pytest.approx(50.0)


def test_gate_returns_after_timeout():
    t0 = time.monotonic()
    assert Gate().wait_turn(0.05, lambda: None) is State.RUNNING
    assert time.monotonic() - t0 >= 0.04


def test_gate_poke_returns_early():
    g = Gate()
    threading.Timer(0.02, g.poke).start()
    t0 = time.monotonic()
    g.wait_turn(5.0, lambda: None)
    assert time.monotonic() - t0 < 1.0


def test_gate_parks_without_timeout(monkeypatch):
    """La prueba central del modo Gaming: la espera en pausa no lleva timeout."""
    timeouts = []
    g = Gate()
    real = g._cv.wait
    def spy(timeout=None):
        timeouts.append(timeout)
        return real(timeout)
    g._cv.wait = spy
    g.set(State.PAUSED)
    parked = []
    t = threading.Thread(target=lambda: parked.append(g.wait_turn(0.01, lambda: None)))
    t.start()
    assert wait_for(lambda: timeouts)
    time.sleep(0.05)
    assert timeouts == [None]
    g.set(State.RUNNING)
    t.join(1)
    assert parked == [State.RUNNING]


def test_stop_releases_a_parked_gate():
    g = Gate()
    g.set(State.PAUSED)
    result = []
    t = threading.Thread(target=lambda: result.append(g.wait_turn(1, lambda: None)))
    t.start()
    time.sleep(0.02)
    g.set(State.STOPPED)
    t.join(1)
    assert result == [State.STOPPED]


def test_stopped_is_final():
    g = Gate()
    g.set(State.STOPPED)
    g.set(State.RUNNING)
    assert g.state is State.STOPPED


def test_only_collector_uses_timeouts():
    """Regla de arquitectura: analyzer y writer esperan sin timeout."""
    import ast, inspect
    for fn in (TelemetryDaemon._analyzer_loop, TelemetryDaemon._writer_loop):
        tree = ast.parse(inspect.getsource(fn).replace("    def ", "def ", 1))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "get":
                assert not node.args and not node.keywords


def test_data_reaches_disk_in_batches(d):
    assert wait_for(lambda: d.flushes >= 1)
    d.stop()
    r = Reader(d.db_path)
    months = r.months()
    assert months and r.monthly(months[0])
    r.close()


def test_stop_flushes_what_is_pending(tmp_path):
    cfg = Config(poll_busy_s=0.02, poll_idle_s=0.02, max_duty=1.0, flush_every_s=9999,
                 min_app_cpu_s=0, min_app_ram_mb=0)
    d = TelemetryDaemon(cfg, tmp_path)
    d.start()
    assert wait_for(lambda: d.ticks >= 3)
    d.stop()
    r = Reader(d.db_path)
    assert r.months()
    r.close()


def test_gaming_parks_everything_and_flushes_first(tmp_path):
    cfg = Config(poll_busy_s=0.02, poll_idle_s=0.02, max_duty=1.0, flush_every_s=9999,
                 min_app_cpu_s=0, min_app_ram_mb=0)
    d = TelemetryDaemon(cfg, tmp_path)
    d.start()
    assert wait_for(lambda: d.ticks >= 3)
    d.set_gaming(True)
    assert wait_for(lambda: d.flushes >= 1)
    time.sleep(0.1)
    ticks, flushes = d.ticks, d.flushes
    time.sleep(0.3)
    assert (d.ticks, d.flushes) == (ticks, flushes)

    d.set_gaming(False)
    assert wait_for(lambda: d.ticks > ticks + 2)
    d.stop()


def test_resume_starts_with_a_new_baseline(d):
    assert wait_for(lambda: d.ticks >= 2)
    d.set_gaming(True)
    time.sleep(0.15)
    ticks = d.ticks
    d.set_gaming(False)
    assert wait_for(lambda: d.ticks > ticks)
    assert d.latest.baseline or d.ticks > ticks + 1


def test_stop_while_gaming_does_not_hang(tmp_path):
    d = TelemetryDaemon(FAST, tmp_path)
    d.start()
    d.set_gaming(True)
    time.sleep(0.05)
    t0 = time.monotonic()
    d.stop()
    assert time.monotonic() - t0 < 2
    assert not any(t.is_alive() for t in d._threads)


def test_gaming_cancels_a_running_scan(tmp_path, monkeypatch):
    import scanner
    started = threading.Event()
    def slow_scan(root, cfg, cancel, progress):
        started.set()
        cancel.wait(5)
        return [], cancel.is_set()
    monkeypatch.setattr(scanner, "scan", slow_scan)
    d = TelemetryDaemon(FAST, tmp_path)
    d.start()
    done = []
    assert d.start_scan(".", lambda *a: None, lambda rows, c: done.append(c))
    assert started.wait(2)
    d.set_gaming(True)
    assert wait_for(lambda: done == [True])
    assert not d.start_scan(".", lambda *a: None, lambda *a: None)
    d.stop()


def test_a_single_scan_at_a_time(tmp_path, monkeypatch):
    import scanner
    release = threading.Event()
    monkeypatch.setattr(scanner, "scan", lambda r, c, x, p: (release.wait(5), ([], False))[1])
    d = TelemetryDaemon(FAST, tmp_path)
    d.start()
    assert d.start_scan(".", lambda *a: None, lambda *a: None)
    assert not d.start_scan(".", lambda *a: None, lambda *a: None)
    release.set()
    d.stop()


def test_scan_rows_are_persisted(tmp_path):
    (tmp_path / "pesado.bin").open("wb").truncate(3 * 1024 ** 2)
    cfg = Config(poll_busy_s=0.05, poll_idle_s=0.05, max_duty=1.0, scan_min_mb=1, scan_duty=1.0)
    d = TelemetryDaemon(cfg, tmp_path / "data")
    d.start()
    done = threading.Event()
    assert d.start_scan(str(tmp_path), lambda *a: None, lambda *a: done.set())
    assert done.wait(5)
    d.stop()
    r = Reader(d.db_path)
    assert [f[2] for f in r.files()] == [".bin"]
    r.close()


def test_tick_failure_does_not_kill_the_loop(tmp_path, monkeypatch):
    calls = {"n": 0}
    real = daemon_mod.Sampler.sample
    def flaky(self, expected):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("psutil falló")
        return real(self, expected)
    monkeypatch.setattr(daemon_mod.Sampler, "sample", flaky)
    d = TelemetryDaemon(FAST, tmp_path)
    d.start()
    assert wait_for(lambda: calls["n"] >= 4)
    d.stop()


def test_failed_write_is_retried(tmp_path, monkeypatch):
    import storage
    real = storage.Writer.write
    calls = {"n": 0}
    def flaky(self, batch):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disco lleno")
        return real(self, batch)
    monkeypatch.setattr(storage.Writer, "write", flaky)
    d = TelemetryDaemon(FAST, tmp_path)
    d.start()
    assert wait_for(lambda: d.flushes >= 2)
    d.stop()


def test_collector_never_blocks_on_a_full_queue(tmp_path):
    d = TelemetryDaemon(FAST, tmp_path)
    for _ in range(50):
        d._put(d._to_analyzer, "tick")
    assert d.dropped == 42


def test_callback_receives_alerts(tmp_path):
    seen = []
    cfg = Config(poll_busy_s=0.02, poll_idle_s=0.02, max_duty=1.0, flush_every_s=9999,
                 hot_minutes=0.0005, hot_core_pct=0.0)
    d = TelemetryDaemon(cfg, tmp_path, on_alerts=seen.append)
    d.start()
    assert wait_for(lambda: seen and d.active_alerts())
    d.set_gaming(True)
    assert wait_for(lambda: d.active_alerts() == [])
    d.stop()


def test_config_file_overrides_and_rejects_bad_values(tmp_path):
    (tmp_path / "huella.json").write_text(
        '{"hot_minutes": 8, "poll_idle_s": 0, "flush_every_s": "x", "otra": 1}', encoding="utf-8")
    cfg = Config.load(tmp_path)
    assert cfg.hot_minutes == 8
    assert cfg.poll_idle_s == Config().poll_idle_s
    assert cfg.flush_every_s == Config().flush_every_s


def test_config_survives_corrupt_file(tmp_path):
    (tmp_path / "huella.json").write_text("{no es json", encoding="utf-8")
    assert Config.load(tmp_path) == Config()
