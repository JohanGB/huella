from metrics import ProcSample, Snapshot


def proc(pid=100, name="app.exe", core=0.0, mb=100.0, dt=15.0, created=1.0, read=0, write=0):
    return ProcSample(pid, created, name, core / 100 * dt, core, mb, read, write)


def snap(procs, wall=1000.0, dt=15.0, gap=False, baseline=False, cpu=10.0):
    return Snapshot(wall=wall, dt=dt, gap=gap, baseline=baseline, cpu_pct=cpu, ram_pct=50.0,
                    ram_used_mb=1000.0, ram_total_mb=2000.0, swap_pct=0.0, tick_ms=5.0,
                    procs=tuple(procs))
