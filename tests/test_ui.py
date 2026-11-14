import time

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon

from anomalies import Alert
from config import Config
from dashboard import Column, TableModel, fmt_bytes
from storage import Batch, Writer
from tray import COLOR_ALERT, Tray


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def pump(app, seconds=0.05):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        app.sendPostedEvents(None, 0)  # incluye DeferredDelete
        time.sleep(0.005)


@pytest.fixture
def tray(app, tmp_path):
    w = Writer(tmp_path / "huella.db")
    w.write(Batch([("2026-11-03", "chrome", 3600.0, 7200.0, 7200.0 * 900, 1500.0, 10, 20)]))
    w.replace_files([("/x/a.iso", "a", ".iso", 3 * 1024 ** 3, 1700000000.0, 1.0)])
    w.close()
    cfg = Config(poll_busy_s=0.05, poll_idle_s=0.05, max_duty=1.0, flush_every_s=9999)
    t = Tray(cfg, tmp_path)
    t.start()
    yield t
    t.stop()


def test_sort_uses_raw_values_not_text():
    m = TableModel([Column("t", fmt_bytes, True)])
    m.set_rows([(900 * 1024 ** 2,), (2 * 1024 ** 3,), (None,)])
    m.sort(0, Qt.SortOrder.DescendingOrder)
    assert [r[0] for r in m.rows] == [2 * 1024 ** 3, 900 * 1024 ** 2, None]


def test_sort_survives_a_refresh():
    m = TableModel([Column("t", str, True)])
    m.sort(0, Qt.SortOrder.DescendingOrder)
    m.set_rows([(1,), (3,), (2,)])
    assert [r[0] for r in m.rows] == [3, 2, 1]


def test_panel_is_not_loaded_until_asked(tray):
    assert tray.panel is None


def test_panel_shows_history_and_files(app, tray):
    tray.show_panel()
    pump(app)
    p = tray.panel
    assert p.month_model.rows[0][0] == "chrome"
    assert p.files_model.rows[0][2] == ".iso"
    assert p.timer.isActive()
    p.close()
    pump(app, 0.1)


def test_tables_open_sorted_biggest_first(app, tray):
    tray.show_panel()
    pump(app)
    p = tray.panel
    p.files_model.set_rows([("a", "/a", ".x", 5, None), ("b", "/b", ".x", 9, None)])
    assert [r[3] for r in p.files_model.rows] == [9, 5]
    p.month_model.set_rows([("a", 1.0, 0, 0, 0, 0, 0), ("b", 8.0, 0, 0, 0, 0, 0)])
    assert p.month_model.rows[0][0] == "b"
    p.close()
    pump(app, 0.1)


def test_closing_panel_destroys_it(app, tray):
    tray.show_panel()
    pump(app)
    tray.panel.close()
    pump(app, 0.1)
    assert tray.panel is None


def test_gaming_destroys_panel_and_leaves_no_active_timer(app, tray):
    tray.show_panel()
    pump(app)
    timer = tray.panel.timer
    tray.gaming_action.setChecked(True)
    pump(app, 0.1)
    assert tray.panel is None
    assert tray.daemon.gaming
    assert not tray.open_action.isEnabled()
    assert not any(t.isActive() for t in QApplication.instance().findChildren(QTimer))
    assert "pausa" in tray.icon.toolTip()


def test_double_click_is_ignored_in_gaming(app, tray):
    tray.gaming_action.setChecked(True)
    tray.icon.activated.emit(QSystemTrayIcon.ActivationReason.DoubleClick)
    pump(app)
    assert tray.panel is None


def test_double_click_opens_panel(app, tray):
    tray.icon.activated.emit(QSystemTrayIcon.ActivationReason.DoubleClick)
    pump(app)
    assert tray.panel is not None
    tray.panel.close()
    pump(app, 0.1)


def test_hidden_panel_stops_its_timer(app, tray):
    tray.show_panel()
    pump(app)
    tray.panel.hide()
    pump(app)
    assert not tray.panel.timer.isActive()
    tray.panel.close()
    pump(app, 0.1)


def test_alert_from_background_thread_reaches_the_tray(app, tray, monkeypatch):
    import threading
    alert = Alert("cpu_sostenida", "spin.exe", 9, 1.0, "d")
    monkeypatch.setattr(tray.daemon, "active_alerts", lambda: [alert])
    threading.Thread(target=tray.bridge.alerts_changed.emit).start()
    pump(app, 0.1)
    assert "spin.exe" in tray.icon.toolTip()
