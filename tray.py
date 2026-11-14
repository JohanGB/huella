"""Punto de entrada de escritorio: bandeja del sistema y modo Gaming.

Este archivo no mide nada. El hilo principal de Qt solo atiende eventos y delega
en el demonio, así que un tick lento no puede congelar la bandeja. Tampoco crea
ningún QTimer: mientras el panel esté cerrado, duerme hasta recibir un evento.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from pathlib import Path

from PyQt6.QtCore import QLockFile, QObject, QSocketNotifier, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QIcon, QPixmap
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from anomalies import active_summary
from config import APP_NAME, Config, default_data_dir
from daemon import TelemetryDaemon

COLOR_OK = "#2e7d32"
COLOR_ALERT = "#e65100"
COLOR_GAMING = "#616161"


def square_icon(color: str) -> QIcon:
    pixmap = QPixmap(32, 32)
    pixmap.fill(QColor(color))
    return QIcon(pixmap)


class Bridge(QObject):
    """Señales entre hilos: los hilos del demonio nunca tocan un widget."""
    alerts_changed = pyqtSignal()
    scan_progress = pyqtSignal(int, int)
    scan_done = pyqtSignal(object, bool)


class Tray(QObject):
    def __init__(self, cfg: Config, data_dir: Path) -> None:
        super().__init__()
        self.bridge = Bridge()
        self.daemon = TelemetryDaemon(
            cfg, data_dir, on_alerts=lambda changed: self.bridge.alerts_changed.emit())
        self.panel = None

        self.icon = QSystemTrayIcon(square_icon(COLOR_OK), self)
        self.menu = QMenu()
        self.open_action = QAction("Abrir panel", self.menu)
        self.gaming_action = QAction("Modo Trabajo Pesado / Gaming", self.menu)
        self.gaming_action.setCheckable(True)
        quit_action = QAction("Salir", self.menu)
        self.menu.addAction(self.open_action)
        self.menu.addAction(self.gaming_action)
        self.menu.addSeparator()
        self.menu.addAction(quit_action)
        self.icon.setContextMenu(self.menu)

        self.open_action.triggered.connect(self.show_panel)
        self.gaming_action.toggled.connect(self.set_gaming)
        quit_action.triggered.connect(QApplication.quit)
        self.icon.activated.connect(self._activated)
        self.bridge.alerts_changed.connect(self.refresh)

    def start(self) -> None:
        self.daemon.start()
        self.refresh()
        self.icon.show()

    def stop(self) -> None:
        if self.panel is not None:
            self.panel.close()
        self.daemon.stop()
        self.icon.hide()

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason is QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_panel()

    def show_panel(self) -> None:
        if self.daemon.gaming:
            return
        if self.panel is None:
            from dashboard import Dashboard   # el panel no se carga hasta que se pide
            self.panel = Dashboard(self.daemon, self.bridge)
            self.panel.destroyed.connect(self._panel_closed)
        self.daemon.poke()                    # datos frescos, no los de hace 15 s
        self.panel.showNormal()
        self.panel.raise_()
        self.panel.activateWindow()

    def _panel_closed(self) -> None:
        self.panel = None

    def set_gaming(self, enabled: bool) -> None:
        if enabled and self.panel is not None:
            self.panel.close()                # el panel tiene un timer: se destruye
        self.daemon.set_gaming(enabled)
        self.open_action.setEnabled(not enabled)
        self.refresh()

    def refresh(self) -> None:
        if self.daemon.gaming:
            self.icon.setIcon(square_icon(COLOR_GAMING))
            self.icon.setToolTip(f"{APP_NAME}: en pausa total (modo Gaming)")
            return
        active = self.daemon.active_alerts()
        self.icon.setIcon(square_icon(COLOR_ALERT if active else COLOR_OK))
        text = active_summary(active) or "sin anomalías"
        self.icon.setToolTip(f"{APP_NAME}: {text}"[:120])


def watch_termination(app: QApplication) -> None:
    """Atiende SIGTERM/SIGINT en Linux sin un timer: el aviso llega por un pipe."""
    if sys.platform == "win32":
        return
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    signal.set_wakeup_fd(write_fd)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: None)
    notifier = QSocketNotifier(read_fd, QSocketNotifier.Type.Read, app)
    notifier.activated.connect(lambda *_: (os.read(read_fd, 64), app.quit()))


def main() -> int:
    parser = argparse.ArgumentParser(description="Huella: profiler pasivo en la bandeja")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--show", action="store_true", help="abre el panel al iniciar")
    args = parser.parse_args()

    data_dir = args.data_dir or default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=data_dir / "huella.log", level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)

    lock = QLockFile(str(data_dir / "huella.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        logging.warning("Ya hay una instancia de Huella en ejecución")
        return 1

    tray = Tray(Config.load(data_dir), data_dir)
    app.aboutToQuit.connect(tray.stop)
    app.commitDataRequest.connect(lambda manager: tray.daemon.stop())  # cierre de sesión
    watch_termination(app)
    tray.start()

    if args.show or not QSystemTrayIcon.isSystemTrayAvailable():
        tray.show_panel()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
