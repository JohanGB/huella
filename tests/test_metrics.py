from types import SimpleNamespace as NS

import psutil
import pytest

import metrics
from metrics import Sampler, clean_text, format_bytes, is_pseudo_process, private_bytes


def test_private_bytes_windows_uses_private_not_working_set():
    assert private_bytes(NS(rss=100, private=900), windows=True) == 900


def test_private_bytes_windows_falls_back():
    assert private_bytes(NS(rss=100, private=0), windows=True) == 100


def test_private_bytes_linux_subtracts_shared():
    assert private_bytes(NS(rss=100, shared=30), windows=False) == 70


def test_format_bytes():
    assert format_bytes(150 * 1024 ** 2) == "150.0 MB"
    assert format_bytes(3 * 1024 ** 3) == "3.00 GB"


def test_clean_text_removes_surrogates():
    assert clean_text("a\udcffb").encode("utf-8") == b"a?b"


def test_windows_pseudo_processes(monkeypatch):
    monkeypatch.setattr(metrics, "WINDOWS", True)
    assert is_pseudo_process(0, "System Idle Process")
    assert is_pseudo_process(4, "System")
    assert not is_pseudo_process(5000, "chrome.exe")


class FakeProc:
    def __init__(self, pid, created, cpu, rss=1024 ** 2, name="x", read=0, write=0):
        self.pid = pid
        self.info = dict(name=name, create_time=created, cpu_times=NS(user=cpu, system=0.0),
                         memory_info=NS(rss=rss, shared=0, private=rss),
                         io_counters=NS(read_bytes=read, write_bytes=write))

    def as_dict(self, attrs, ad_value):
        return self.info


@pytest.fixture
def fake(monkeypatch):
    state = {"procs": []}
    monkeypatch.setattr(metrics.psutil, "process_iter", lambda: iter(state["procs"]))
    return state


def ticks(sampler, n=1):
    return [sampler.sample(15.0) for _ in range(n)][-1]


def test_first_tick_has_no_deltas(fake):
    fake["procs"] = [FakeProc(10, 1.0, 500.0, read=9)]
    s = Sampler()
    snap = ticks(s)
    assert snap.baseline and snap.procs[0].cpu_delta_s == 0 and snap.procs[0].read_delta == 0


def test_cpu_delta_and_core_pct(fake, monkeypatch):
    clock = iter([100.0, 110.0])
    monkeypatch.setattr(metrics.time, "monotonic", lambda: next(clock))
    fake["procs"] = [FakeProc(10, 1.0, 50.0)]
    s = Sampler()
    s.sample(15.0)
    fake["procs"] = [FakeProc(10, 1.0, 55.0)]
    p = s.sample(15.0).procs[0]
    assert p.cpu_delta_s == 5.0
    assert p.core_pct == pytest.approx(50.0)


def test_pid_reuse_does_not_produce_negative_or_mixed_delta(fake):
    fake["procs"] = [FakeProc(10, 1.0, 500.0)]
    s = Sampler()
    s.sample(15.0)
    fake["procs"] = [FakeProc(10, 2.0, 3.0)]   # mismo PID, otro proceso, ya viejo
    assert s.sample(15.0).procs[0].cpu_delta_s == 0.0


def test_process_born_between_ticks_counts_fully(fake):
    import time
    s = Sampler()
    s.sample(15.0)
    fake["procs"] = [FakeProc(11, time.time() + 1, 4.0, read=100)]
    p = s.sample(15.0).procs[0]
    assert p.cpu_delta_s == 4.0 and p.read_delta == 100


def test_counter_going_backwards_is_clamped(fake):
    fake["procs"] = [FakeProc(10, 1.0, 50.0, read=500)]
    s = Sampler()
    s.sample(15.0)
    fake["procs"] = [FakeProc(10, 1.0, 40.0, read=100)]
    p = s.sample(15.0).procs[0]
    assert p.cpu_delta_s == 0.0 and p.read_delta == 0


def test_gap_is_detected_on_wall_clock(fake, monkeypatch):
    wall = iter([1000.0, 1000.0 + 3600])
    monkeypatch.setattr(metrics.time, "time", lambda: next(wall))
    s = Sampler()
    s.sample(15.0)
    assert s.sample(15.0).gap


def test_access_denied_fields_do_not_lose_the_process(fake):
    p = FakeProc(10, 1.0, 1.0)
    p.info["memory_info"] = None
    p.info["io_counters"] = None
    fake["procs"] = [p]
    assert Sampler().sample(15.0).procs[0].private_mb == 0.0


def test_vanished_process_is_skipped(fake):
    class Gone:
        pid = 5
        def as_dict(self, attrs, ad_value):
            raise psutil.NoSuchProcess(5)
    fake["procs"] = [Gone(), FakeProc(10, 1.0, 1.0)]
    assert [p.pid for p in Sampler().sample(15.0).procs] == [10]


def test_reset_rebaselines(fake):
    fake["procs"] = [FakeProc(10, 1.0, 1.0)]
    s = Sampler()
    s.sample(15.0)
    s.reset()
    fake["procs"] = [FakeProc(10, 1.0, 900.0)]
    snap = s.sample(15.0)
    assert snap.baseline and snap.procs[0].cpu_delta_s == 0


def test_real_system_sample_finds_this_process():
    s = Sampler()
    s.sample(15.0)
    snap = s.sample(15.0)
    assert any(p.pid == __import__("os").getpid() and p.private_mb > 0 for p in snap.procs)
