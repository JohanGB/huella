"""SQLite por lotes. Solo el hilo escritor abre `Writer`; el panel abre `Reader`.

La base guarda agregados, no muestras: `app_daily` crece con apps × días. El
mes se calcula con GROUP BY sobre los días, sin una segunda tabla que mantener.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from anomalies import Alert

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_daily (
    day       TEXT NOT NULL,            -- AAAA-MM-DD, hora local
    app       TEXT NOT NULL,
    cpu_s     REAL NOT NULL DEFAULT 0,  -- CPU-segundos
    open_s    REAL NOT NULL DEFAULT 0,  -- segundos con algún proceso vivo
    ram_mb_s  REAL NOT NULL DEFAULT 0,  -- integral de memoria privada
    ram_peak  REAL NOT NULL DEFAULT 0,  -- MB
    read_b    INTEGER NOT NULL DEFAULT 0,
    write_b   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, app)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS alerts (
    id       TEXT PRIMARY KEY,
    kind     TEXT NOT NULL,
    app      TEXT NOT NULL,
    pid      INTEGER NOT NULL,
    started  REAL NOT NULL,
    ended    REAL,
    detail   TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS heavy_files (
    path     TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    ext      TEXT NOT NULL,
    size     INTEGER NOT NULL,
    created  REAL,                      -- NULL si el sistema de archivos no la guarda
    scanned  REAL NOT NULL
) WITHOUT ROWID;
"""

UPSERT_APP = """
INSERT INTO app_daily (day, app, cpu_s, open_s, ram_mb_s, ram_peak, read_b, write_b)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (day, app) DO UPDATE SET
    cpu_s    = cpu_s    + excluded.cpu_s,
    open_s   = open_s   + excluded.open_s,
    ram_mb_s = ram_mb_s + excluded.ram_mb_s,
    ram_peak = MAX(ram_peak, excluded.ram_peak),
    read_b   = read_b   + excluded.read_b,
    write_b  = write_b  + excluded.write_b
"""

UPSERT_ALERT = """
INSERT INTO alerts (id, kind, app, pid, started, ended, detail)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (id) DO UPDATE SET ended = excluded.ended, detail = excluded.detail
"""


@dataclass
class Batch:
    """Lo que viaja del hilo analizador al hilo escritor."""
    apps: Sequence[tuple] = ()
    alerts: Sequence[Alert] = ()

    def __bool__(self) -> bool:
        return bool(self.apps or self.alerts)


class Writer:
    def __init__(self, path: Path, retention_days: int = 400) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None)
        # WAL + NORMAL: no hay fsync por commit, solo en el checkpoint. Una caída
        # de energía puede perder el último lote, pero nunca corrompe la base.
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.execute("PRAGMA temp_store = MEMORY")
        self.conn.execute("PRAGMA cache_size = -512")  # 512 KB
        self.conn.executescript(SCHEMA)
        cutoff = time.time() - retention_days * 86400
        day = time.strftime("%Y-%m-%d", time.localtime(cutoff))
        self._tx(lambda: (
            self.conn.execute("DELETE FROM app_daily WHERE day < ?", (day,)),
            self.conn.execute("DELETE FROM alerts WHERE started < ?", (cutoff,)),
        ))

    def _tx(self, work) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            work()
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def write(self, batch: Batch) -> None:
        def work() -> None:
            if batch.apps:
                self.conn.executemany(UPSERT_APP, batch.apps)
            if batch.alerts:
                self.conn.executemany(UPSERT_ALERT, [
                    (a.id, a.kind, a.app, a.pid, a.started, a.ended, a.detail)
                    for a in batch.alerts])
        self._tx(work)

    def replace_files(self, rows: Iterable[tuple]) -> None:
        """Sustituye el resultado del escaneo anterior en una sola transacción."""
        def work() -> None:
            self.conn.execute("DELETE FROM heavy_files")
            self.conn.executemany(
                "INSERT OR REPLACE INTO heavy_files (path, name, ext, size, created, scanned)"
                " VALUES (?, ?, ?, ?, ?, ?)", rows)
        self._tx(work)

    def close(self) -> None:
        self.conn.close()


class Reader:
    """Conexión de solo lectura: SQLite rechaza cualquier escritura desde aquí."""

    def __init__(self, path: Path) -> None:
        self.conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        self.conn.execute("PRAGMA query_only = ON")

    def months(self) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT substr(day, 1, 7) FROM app_daily ORDER BY 1 DESC")]

    def monthly(self, month: str, limit: int = 200) -> list[tuple]:
        """(app, cpu_s, abierta_s, ram_prom_mb, ram_pico_mb, leído_b, escrito_b)"""
        return self.conn.execute(
            """
            SELECT app, SUM(cpu_s), SUM(open_s),
                   SUM(ram_mb_s) / NULLIF(SUM(open_s), 0),
                   MAX(ram_peak), SUM(read_b), SUM(write_b)
            FROM app_daily
            WHERE day >= ? AND day < ?
            GROUP BY app
            ORDER BY SUM(cpu_s) DESC
            LIMIT ?
            """, (f"{month}-00", f"{month}-99", limit)).fetchall()

    def alerts(self, limit: int = 100) -> list[tuple]:
        """(inicio, fin, tipo, app, pid, detalle), lo más reciente primero."""
        return self.conn.execute(
            "SELECT started, ended, kind, app, pid, detail FROM alerts "
            "ORDER BY started DESC LIMIT ?", (limit,)).fetchall()

    def files(self) -> list[tuple]:
        """(nombre, ruta, ext, tamaño, creado)"""
        return self.conn.execute(
            "SELECT name, path, ext, size, created FROM heavy_files "
            "ORDER BY size DESC").fetchall()

    def close(self) -> None:
        self.conn.close()
