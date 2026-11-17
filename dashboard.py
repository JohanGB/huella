"""Panel bajo demanda: se crea con el doble clic y se destruye al cerrarlo.

Sin hojas de estilo ni animaciones. Su único timer vive mientras la ventana esté
visible, y solo copia a la tabla el último Snapshot que ya está en memoria.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import Callable, NamedTuple

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer
from PyQt6.QtGui import QFontDatabase
from PyQt6.QtWidgets import (QComboBox, QFileDialog, QHBoxLayout, QHeaderView,
                             QLabel, QLineEdit, QPushButton, QTableView, QTabWidget,
                             QVBoxLayout, QWidget)

from metrics import MB, format_bytes
from storage import Reader

REFRESH_MS = 5000
TOP_PROCESSES = 30


def fmt_date(ts: float | None) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "—"


def fmt_hours(seconds: float | None) -> str:
    return f"{(seconds or 0) / 3600:,.1f} h"


def fmt_mb(value: float | None) -> str:
    return format_bytes((value or 0) * MB)


def fmt_bytes(value: float | None) -> str:
    return format_bytes(value or 0)


class Column(NamedTuple):
    title: str
    format: Callable = str
    numeric: bool = False


class TableModel(QAbstractTableModel):
    """Guarda valores crudos y formatea al pintar: ordena 2 GB por encima de 900 MB."""

    def __init__(self, columns: list[Column]) -> None:
        super().__init__()
        self.columns = columns
        self.rows: list[tuple] = []
        self._sort: tuple[int, Qt.SortOrder] | None = None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.columns[section].title
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        column = self.columns[index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            return column.format(self.rows[index.row()][index.column()])
        if role == Qt.ItemDataRole.TextAlignmentRole and column.numeric:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def set_rows(self, rows: list[tuple]) -> None:
        self.beginResetModel()
        self.rows = list(rows)
        self._apply_sort()
        self.endResetModel()

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        self._sort = (column, order)
        self.layoutAboutToBeChanged.emit()
        self._apply_sort()
        self.layoutChanged.emit()

    def _apply_sort(self) -> None:
        if self._sort is None:
            return
        column, order = self._sort
        # Los vacíos van al final en orden descendente.
        self.rows.sort(key=lambda r: (r[column] is not None, r[column] or 0)
                       if self.columns[column].numeric
                       else str(r[column]).lower(),
                       reverse=order == Qt.SortOrder.DescendingOrder)


def make_table(columns: list[Column], stretch: int = 0,
               sort_by: int = 0) -> tuple[QTableView, TableModel]:
    """La tabla abre ordenada de mayor a menor por `sort_by`."""
    model = TableModel(columns)
    view = QTableView()
    view.setModel(model)
    view.horizontalHeader().setSortIndicator(sort_by, Qt.SortOrder.DescendingOrder)
    view.setSortingEnabled(True)
    view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
    view.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
    view.setAlternatingRowColors(True)
    view.verticalHeader().hide()
    view.verticalHeader().setDefaultSectionSize(22)
    view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
    header = view.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(stretch, QHeaderView.ResizeMode.Stretch)
    return view, model


class Dashboard(QWidget):
    def __init__(self, daemon, bridge) -> None:
        super().__init__()
        self.daemon = daemon
        self.bridge = bridge
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("Quietwatch")
        self.resize(980, 620)

        tabs = QTabWidget()
        tabs.addTab(self._build_now(), "Ahora")
        tabs.addTab(self._build_monthly(), "Mensual")
        tabs.addTab(self._build_files(), "Archivos")
        tabs.addTab(self._build_alerts(), "Alertas")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(tabs)

        self.timer = QTimer(self)
        self.timer.setInterval(REFRESH_MS)
        self.timer.timeout.connect(self.refresh_now)
        bridge.alerts_changed.connect(self.refresh_alerts)
        bridge.scan_progress.connect(self._scan_progress)
        bridge.scan_done.connect(self._scan_done)
        self.load_history()

    # --- Pestañas -------------------------------------------------------------

    def _build_now(self) -> QWidget:
        self.status = QLabel("Esperando el primer tick…")
        self.status.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.active = QLabel("")
        self.active.setWordWrap(True)
        view, self.now_model = make_table([
            Column("Proceso"), Column("PID", str, True),
            Column("CPU (100 % = 1 núcleo)", lambda v: f"{v:,.1f} %", True),
            Column("Memoria privada", fmt_mb, True),
            Column("Lectura último tick", fmt_bytes, True),
            Column("Escritura último tick", fmt_bytes, True)], sort_by=2)
        page = QWidget()
        box = QVBoxLayout(page)
        box.addWidget(self.status)
        box.addWidget(self.active)
        box.addWidget(view)
        return page

    def _build_monthly(self) -> QWidget:
        self.month = QComboBox()
        self.month.currentTextChanged.connect(self.load_month)
        view, self.month_model = make_table([
            Column("Aplicación"), Column("CPU", fmt_hours, True),
            Column("Abierta", fmt_hours, True), Column("RAM promedio", fmt_mb, True),
            Column("RAM pico", fmt_mb, True), Column("Leído", fmt_bytes, True),
            Column("Escrito", fmt_bytes, True)], sort_by=1)
        note = QLabel("El lote más reciente llega a disco cada 5 minutos.")
        page = QWidget()
        box = QVBoxLayout(page)
        box.addWidget(self.month)
        box.addWidget(view)
        box.addWidget(note)
        return page

    def _build_files(self) -> QWidget:
        self.root = QLineEdit(str(Path.home()))
        pick = QPushButton("Elegir…")
        pick.clicked.connect(self._pick_root)
        self.scan_button = QPushButton("Escanear")
        self.scan_button.clicked.connect(self._toggle_scan)
        row = QHBoxLayout()
        row.addWidget(self.root)
        row.addWidget(pick)
        row.addWidget(self.scan_button)
        view, self.files_model = make_table([
            Column("Nombre"), Column("Ruta absoluta"), Column("Tipo"),
            Column("Tamaño", fmt_bytes, True), Column("Creado", fmt_date, True)],
            stretch=1, sort_by=3)
        self.scan_status = QLabel(f"Archivos desde {self.daemon.cfg.scan_min_mb:.0f} MB.")
        page = QWidget()
        box = QVBoxLayout(page)
        box.addLayout(row)
        box.addWidget(view)
        box.addWidget(self.scan_status)
        return page

    def _build_alerts(self) -> QWidget:
        view, self.alerts_model = make_table([
            Column("Inicio", fmt_date), Column("Fin", fmt_date), Column("Tipo"),
            Column("Aplicación"), Column("PID", str, True), Column("Detalle")],
            stretch=5, sort_by=0)
        page = QWidget()
        QVBoxLayout(page).addWidget(view)
        return page

    # --- Datos ----------------------------------------------------------------

    def refresh_now(self) -> None:
        snap = self.daemon.latest
        if snap is None:
            return
        self.status.setText(
            f"CPU {snap.cpu_pct:.0f} %  |  RAM {snap.ram_pct:.0f} % "
            f"({fmt_mb(snap.ram_used_mb)} de {fmt_mb(snap.ram_total_mb)})  |  "
            f"Swap {snap.swap_pct:.0f} %  |  Quietwatch: tick {snap.tick_ms:.0f} ms, "
            f"{self.daemon.self_cpu_pct:.2f} % CPU")
        visible = (p for p in snap.procs if p.core_pct or p.private_mb)
        top = sorted(visible, key=lambda p: (p.core_pct, p.private_mb),
                     reverse=True)[:TOP_PROCESSES]
        self.now_model.set_rows([(p.name, p.pid, p.core_pct, p.private_mb,
                                  p.read_delta, p.write_delta) for p in top])
        self.refresh_active()

    def refresh_active(self) -> None:
        lines = [f"{a.app} (PID {a.pid}): {a.detail}" for a in self.daemon.active_alerts()]
        self.active.setText("\n".join(lines) or "Sin anomalías activas.")

    def refresh_alerts(self) -> None:
        self.refresh_active()
        self._query(lambda db: self.alerts_model.set_rows(
            [(s, e, k.replace("_", " "), a, p, d) for s, e, k, a, p, d in db.alerts()]))

    def load_history(self) -> None:
        def load(db: Reader) -> None:
            self.month.clear()
            self.month.addItems(db.months())
            self.files_model.set_rows(db.files())
        self._query(load)
        self.refresh_alerts()

    def load_month(self, month: str) -> None:
        if month:
            self._query(lambda db: self.month_model.set_rows(db.monthly(month)))

    def _query(self, work: Callable[[Reader], None]) -> None:
        """Abre, lee y cierra: el panel no retiene una conexión entre consultas."""
        try:
            db = Reader(self.daemon.db_path)
        except sqlite3.Error:
            return     # primera ejecución: aún no hay base
        try:
            work(db)
        except sqlite3.Error:
            pass
        finally:
            db.close()

    # --- Escaneo --------------------------------------------------------------

    def _pick_root(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Carpeta a escanear", self.root.text())
        if folder:
            self.root.setText(folder)

    def _toggle_scan(self) -> None:
        if self.scan_button.text() == "Cancelar":
            self.daemon.cancel_scan()
            return
        started = self.daemon.start_scan(self.root.text(),
                                         self.bridge.scan_progress.emit,
                                         self.bridge.scan_done.emit)
        if started:
            self.scan_button.setText("Cancelar")
            self.scan_status.setText("Escaneando…")

    def _scan_progress(self, seen: int, kept: int) -> None:
        self.scan_status.setText(f"{seen:,} entradas revisadas, {kept:,} pesadas.")

    def _scan_done(self, rows: list, cancelled: bool) -> None:
        self.scan_button.setText("Escanear")
        if cancelled:
            self.scan_status.setText("Escaneo cancelado; se conserva el resultado anterior.")
            return
        total = sum(r[3] for r in rows)
        self.scan_status.setText(f"{len(rows):,} archivos, {format_bytes(total)} en total.")
        self.files_model.set_rows([(r[1], r[0], r[2], r[3], r[4]) for r in rows])

    # --- Ciclo de vida del timer ----------------------------------------------

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh_now()
        self.timer.start()

    def hideEvent(self, event) -> None:
        self.timer.stop()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:
        self.timer.stop()
        self.daemon.cancel_scan()
        super().closeEvent(event)
