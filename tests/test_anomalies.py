import math
import random

import pytest

from anomalies import (Analyzer, AppTotals, HotDetector, KIND_HOT, KIND_LEAK, LeakDetector,
                       dominant_thread, is_leaking, trend)
from config import Config
from helpers import proc, snap

CFG = Config()
N = CFG.leak_points


def linear(rate=4.0, start=300.0, noise=2.0, rng=random):
    return [start + rate * i + rng.gauss(0, noise) for i in range(N)]


def logistic(ceiling=800.0, start=200.0, midpoint=8, width=2.5):
    """Caché que se llena y se aplana."""
    return [start + (ceiling - start) / (1 + math.exp(-(i - midpoint) / width)) for i in range(N)]


def test_trend_on_perfect_line():
    stats = trend([10.0 + 2.0 * i for i in range(20)])
    assert stats.slope == pytest.approx(2.0)
    assert stats.tau == pytest.approx(1.0)
    assert stats.tail == pytest.approx(2.0)


def test_trend_ignores_a_single_spike():
    series = [10.0 + i for i in range(20)]
    series[10] = 10_000.0
    assert trend(series).slope == pytest.approx(1.0, rel=0.1)


def test_detects_linear_leak():
    random.seed(1)
    assert is_leaking(linear(), CFG)


def test_cache_that_plateaus_is_not_a_leak():
    assert is_leaking(logistic(), CFG) is None


def test_flat_noise_is_not_a_leak():
    random.seed(2)
    assert is_leaking([500 + random.gauss(0, 40) for _ in range(N)], CFG) is None


def test_small_growth_is_ignored():
    assert is_leaking([300 + 0.5 * i for i in range(N)], CFG) is None


def test_big_process_needs_relative_growth():
    assert is_leaking([20_000 + 4.0 * i for i in range(N)], CFG) is None


def test_needs_a_full_window():
    assert is_leaking([300 + 10.0 * i for i in range(N - 1)], CFG) is None


def test_sawtooth_with_flat_floor_is_not_a_leak():
    series = [400 + (i % 6) * 40.0 for i in range(N)]
    assert is_leaking(series, CFG) is None


def run_leak(detector, series, ticks_per_point=4, pid=100):
    raised, cleared, wall = [], [], 1000.0
    for value in series:
        for _ in range(ticks_per_point):
            wall += 15.0
            r, c = detector.feed(snap([proc(pid=pid, mb=value)], wall=wall))
            raised += r
            cleared += c
    return raised, cleared


def test_detector_uses_the_minute_floor_not_spikes():
    d = LeakDetector(CFG)
    raised, wall = [], 1000.0
    for minute in range(N + 2):
        for tick in range(4):
            wall += 15.0
            mb = 300.0 + (900.0 * minute if tick == 0 else 0.0)  # picos que suben
            raised += d.feed(snap([proc(mb=mb)], wall=wall))[0]
    assert raised == []


def test_detector_raises_once_and_clears_on_plateau():
    random.seed(3)
    d = LeakDetector(CFG)
    plateau = [300 + 4.0 * N] * (N * 2)
    raised, cleared = run_leak(d, linear(noise=1.0) + plateau)
    assert [a.kind for a in raised] == [KIND_LEAK]
    assert [a.id for a in cleared] == [raised[0].id]
    assert cleared[0].ended is not None


def test_leak_alert_closes_when_process_exits():
    random.seed(4)
    d = LeakDetector(CFG)
    raised, _ = run_leak(d, linear(noise=1.0))
    assert raised
    _, cleared = d.feed(snap([], wall=99999.0))
    assert cleared and d._tracks == {}


def test_small_processes_are_not_tracked():
    d = LeakDetector(CFG)
    d.feed(snap([proc(mb=5.0)]))
    assert d._tracks == {}


def test_pid_reuse_starts_a_new_series():
    d = LeakDetector(CFG)
    d.feed(snap([proc(mb=100, created=1.0)]))
    d.feed(snap([proc(mb=100, created=2.0)]))
    assert list(d._tracks) == [(100, 2.0)]


def hot_ticks(detector, seconds, core=100.0, wall=1000.0, dt=15.0, pid=7):
    raised, cleared = [], []
    for _ in range(int(seconds / dt)):
        wall += dt
        r, c = detector.feed(snap([proc(pid=pid, core=core, dt=dt)], wall=wall, dt=dt))
        raised += r
        cleared += c
    return raised, cleared, wall


def test_hot_fires_after_n_minutes_not_before():
    d = HotDetector(Config(hot_minutes=5))
    assert hot_ticks(d, 285)[0] == []
    d = HotDetector(Config(hot_minutes=5))
    raised, _, _ = hot_ticks(d, 300)
    assert [a.kind for a in raised] == [KIND_HOT]


def test_hot_threshold_is_per_core_so_one_spinning_thread_counts():
    """En 16 núcleos, un hilo girando es 6 % global: por eso se mide en núcleos."""
    d = HotDetector(Config(hot_minutes=1))
    assert hot_ticks(d, 60, core=100.0)[0]


def test_brief_dip_does_not_reset_the_streak():
    d = HotDetector(Config(hot_minutes=5))
    _, _, wall = hot_ticks(d, 150)
    wall += 15
    d.feed(snap([proc(pid=7, core=10.0)], wall=wall))
    raised, _, _ = hot_ticks(d, 150, wall=wall)
    assert raised


def test_sustained_cooldown_resets_and_clears():
    d = HotDetector(Config(hot_minutes=1))
    raised, _, wall = hot_ticks(d, 75)
    assert raised
    _, cleared, _ = hot_ticks(d, 60, core=5.0, wall=wall)
    assert cleared and cleared[0].id == raised[0].id
    assert d._tracks == {}


def test_hot_alert_reports_dominant_thread_and_temperature():
    calls = iter([{1: 10.0, 2: 10.0}, {1: 11.0, 2: 200.0}])
    d = HotDetector(Config(hot_minutes=1), lambda pid: next(calls), lambda: 88.0)
    raised, _, _ = hot_ticks(d, 60)
    assert "hilo dominante 2" in raised[0].detail
    assert "88 °C" in raised[0].detail


def test_dominant_thread_empty():
    assert dominant_thread({}, {}) is None


def test_analyzer_tracks_active_and_reset_ends_them():
    a = Analyzer(Config(hot_minutes=1))
    wall = 1000.0
    for _ in range(5):
        wall += 15
        a.feed(snap([proc(pid=7, core=100.0)], wall=wall))
    assert len(a.active) == 1
    assert len(a.reset()) == 1
    assert a.active == {}


def test_totals_sum_processes_of_the_same_app():
    t = AppTotals(CFG)
    t.add("2026-11-03", snap([proc(pid=1, name="chrome", core=100, mb=1000, read=5),
                              proc(pid=2, name="chrome", core=100, mb=500, write=7)]))
    t.add("2026-11-03", snap([proc(pid=1, name="chrome", core=100, mb=800)]))
    (row,) = t.drain()
    day, app, cpu, opened, ram_int, peak, read_b, write_b = row
    assert (day, app) == ("2026-11-03", "chrome")
    assert cpu == pytest.approx(45.0)
    assert opened == 30.0
    assert ram_int == pytest.approx(1500 * 15 + 800 * 15)
    assert peak == 1500
    assert (read_b, write_b) == (5, 7)
    assert t.drain() == []


def test_totals_fold_noise_into_others_without_losing_cpu():
    t = AppTotals(CFG)
    t.add("d", snap([proc(pid=i, name=f"n{i}", core=2, mb=1) for i in range(50)]
                    + [proc(pid=99, name="big", core=100, mb=500)]))
    rows = {r[1]: r for r in t.drain()}
    assert set(rows) == {"big", "(otros)"}
    assert rows["(otros)"][2] == pytest.approx(50 * 0.3)


def test_batch_size_does_not_grow_with_ticks():
    t = AppTotals(CFG)
    procs = [proc(pid=i, name=f"a{i}", core=50, mb=100) for i in range(20)]
    for _ in range(1000):
        t.add("d", snap(procs))
    assert len(t.drain()) == 20
