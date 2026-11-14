"""Compara escrituras a disco: lote por agregados frente a guardar cada muestra."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from storage import Batch, Writer

APPS = 60
DAY_FLUSHES = 288                       # cada 5 minutos
TICKS_PER_FLUSH = 20                    # cada 15 s


def written() -> int:
    """Bytes pedidos al sistema. `write_bytes` no sirve en un bucle apretado: una
    página aún sucia que se reescribe no vuelve a contarse, y un lote real sí llega
    al disco porque entre lotes pasan 5 minutos."""
    io = psutil.Process().io_counters()
    return getattr(io, "write_chars", io.write_bytes)


def batched(folder: Path, days: int) -> tuple[int, int]:
    w = Writer(folder / "lote.db")
    before = written()
    for d in range(days):
        day = f"2026-{1 + d // 28:02d}-{1 + d % 28:02d}"
        for flush in range(DAY_FLUSHES):
            w.write(Batch([(day, f"app{a}.exe", 3.0, 300.0, 9000.0, 300.0, 1000, 2000)
                           for a in range(APPS)]))
    w.close()
    return written() - before, (folder / "lote.db").stat().st_size


def naive(folder: Path) -> tuple[int, int]:
    """Lo ingenuo: una fila por proceso y tick, confirmada en cada tick."""
    c = sqlite3.connect(folder / "crudo.db", isolation_level=None)
    c.execute("CREATE TABLE s (ts REAL, pid INT, name TEXT, cpu REAL, ram REAL, r INT, w INT)")
    before = written()
    for tick in range(DAY_FLUSHES * TICKS_PER_FLUSH):
        c.execute("BEGIN")
        c.executemany("INSERT INTO s VALUES (?,?,?,?,?,?,?)",
                      [(float(tick), p, f"app{p % APPS}.exe", 1.0, 100.0, 5, 5) for p in range(200)])
        c.execute("COMMIT")
    c.close()
    return written() - before, (folder / "crudo.db").stat().st_size


def main() -> None:
    with tempfile.TemporaryDirectory(dir=Path.home()) as tmp:
        folder = Path(tmp)
        t0 = time.perf_counter()
        w, size = batched(folder, 1)
        one_flush_ms = (time.perf_counter() - t0) / DAY_FLUSHES * 1000
        print(f"Lote, {APPS} apps, 1 día: {w / 1024 ** 2:.2f} MB al disco, base {size / 1024:.0f} KB, "
              f"{one_flush_ms:.2f} ms por lote")
        (folder / "lote.db").unlink()
        w30, size30 = batched(folder, 30)
        print(f"Lote, 30 días: {w30 / 1024 ** 2:.1f} MB al disco, base {size30 / 1024 ** 2:.2f} MB")
        n, nsize = naive(folder)
        print(f"Crudo, 200 procesos, 1 día: {n / 1024 ** 2:.1f} MB al disco, base {nsize / 1024 ** 2:.1f} MB")
        print(f"Reducción de escrituras diarias: {n / max(w, 1):.0f} veces")


if __name__ == "__main__":
    main()
