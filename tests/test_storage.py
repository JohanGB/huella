import sqlite3

import pytest

from anomalies import Alert
from storage import Batch, Reader, Writer


def row(day="2026-11-03", app="a", cpu=10.0, opened=100.0, ram_int=5000.0, peak=60.0, r=1, w=2):
    return (day, app, cpu, opened, ram_int, peak, r, w)


@pytest.fixture
def db(tmp_path):
    w = Writer(tmp_path / "t.db")
    yield w, tmp_path / "t.db"
    w.close()


def test_batches_accumulate_on_the_same_day(db):
    w, path = db
    w.write(Batch([row()]))
    w.write(Batch([row(peak=90.0)]))
    r = Reader(path)
    assert r.monthly("2026-11") == [("a", 20.0, 200.0, 50.0, 90.0, 2, 4)]
    r.close()


def test_month_groups_days_and_excludes_others(db):
    w, path = db
    w.write(Batch([row(day="2026-10-31"), row(day="2026-11-01"), row(day="2026-11-30"),
                   row(day="2026-12-01")]))
    r = Reader(path)
    assert r.monthly("2026-11")[0][1] == 20.0
    assert r.months() == ["2026-12", "2026-11", "2026-10"]
    r.close()


def test_monthly_orders_by_cpu(db):
    w, path = db
    w.write(Batch([row(app="poco", cpu=1.0), row(app="mucho", cpu=9.0)]))
    r = Reader(path)
    assert [x[0] for x in r.monthly("2026-11")] == ["mucho", "poco"]
    r.close()


def test_reader_cannot_write(db):
    _, path = db
    r = Reader(path)
    with pytest.raises(sqlite3.OperationalError):
        r.conn.execute("DELETE FROM app_daily")
    r.close()


def test_alert_is_closed_by_a_later_batch(db):
    w, path = db
    a = Alert("cpu_sostenida", "x", 1, 100.0, "d")
    w.write(Batch(alerts=[a]))
    a.ended = 200.0
    w.write(Batch(alerts=[a]))
    r = Reader(path)
    assert [(x[0], x[1]) for x in r.alerts()] == [(100.0, 200.0)]
    r.close()


def test_failed_batch_rolls_back_entirely(db):
    w, path = db
    with pytest.raises(Exception):
        w.write(Batch([row(), ("solo", "dos")]))
    r = Reader(path)
    assert r.monthly("2026-11") == []
    r.close()


def test_replace_files_drops_previous_scan(db):
    w, path = db
    w.replace_files([("/a", "a", ".x", 10, None, 1.0)])
    w.replace_files([("/b", "b", ".y", 20, 5.0, 2.0), ("/c", "c", ".y", 30, None, 2.0)])
    r = Reader(path)
    assert [f[1] for f in r.files()] == ["/c", "/b"]
    r.close()


def test_retention_prunes_on_start(tmp_path):
    p = tmp_path / "t.db"
    w = Writer(p)
    w.write(Batch([row(day="2001-01-01"), row(day="2999-01-01")]))
    w.close()
    w = Writer(p, retention_days=30)
    w.close()
    r = Reader(p)
    assert r.months() == ["2999-01"]
    r.close()


def test_reader_path_with_spaces_and_accents(tmp_path):
    folder = tmp_path / "Datos de Johán #1"
    w = Writer(folder / "t.db")
    w.write(Batch([row()]))
    r = Reader(folder / "t.db")
    assert r.months() == ["2026-11"]
    r.close()
    w.close()
