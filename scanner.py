"""Explorador plano de archivos pesados. Solo lee metadatos, nunca abre un archivo.

Corre bajo demanda en un hilo que muere al terminar. No hay escaneo automático:
recorrer el disco en segundo plano sería justo el tipo de carga que Quietwatch vigila.
"""
from __future__ import annotations

import ctypes
import heapq
import itertools
import os
import re
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from config import Config
from metrics import clean_text

LINUX = sys.platform.startswith("linux")
WINDOWS = sys.platform == "win32"
MB = 1024 * 1024

# Pseudo sistemas de archivos: sus "tamaños" no ocupan disco.
LINUX_SKIP = {"/proc", "/sys", "/dev", "/run"}

FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
CLOUD_ONLY = (FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN
              | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)

IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003   # uniones de carpeta (junctions)
IO_REPARSE_TAG_SYMLINK = 0xA000000C

AT_FDCWD = -100
AT_SYMLINK_NOFOLLOW = 0x100
AT_STATX_DONT_SYNC = 0x4000
STATX_BTIME = 0x800
_STATX_BTIME_OFFSET = 80
_libc = None


def _statx_birthtime(path: str) -> float | None:
    """Fecha de creación real en Linux.

    `st_ctime` en Linux es el último cambio de metadatos, no la creación, y
    `os.stat` no expone el btime. Hay que llamar a statx(2) directamente.
    """
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
    statx = getattr(_libc, "statx", None)
    if statx is None:
        return None
    buffer = ctypes.create_string_buffer(256)
    result = statx(AT_FDCWD, os.fsencode(path), AT_SYMLINK_NOFOLLOW | AT_STATX_DONT_SYNC,
                   STATX_BTIME, buffer)
    if result != 0:
        return None
    mask = struct.unpack_from("=I", buffer.raw, 0)[0]
    if not mask & STATX_BTIME:
        return None   # el sistema de archivos no la guarda
    return float(struct.unpack_from("=q", buffer.raw, _STATX_BTIME_OFFSET)[0])


def creation_time(path: str) -> float | None:
    try:
        if LINUX:
            return _statx_birthtime(path)
        st = os.stat(path, follow_symlinks=False)
        # st_birthtime existe en Windows desde 3.12; antes, st_ctime es la creación.
        return getattr(st, "st_birthtime", None) or (st.st_ctime if WINDOWS else None)
    except OSError:
        return None


def readable_name(filename: str) -> str:
    """'backup_final-2024.v2.zip' -> 'backup final 2024 v2'"""
    stem = filename[1:] if filename.startswith(".") else Path(filename).stem
    text = re.sub(r"\s+", " ", re.sub(r"[._\-]+", " ", stem)).strip()
    return text or filename


def file_type(filename: str) -> str:
    return Path(filename).suffix.lower() or "(sin extensión)"


def is_cloud_only(st: os.stat_result) -> bool:
    """Marcador de OneDrive y similares: aparenta un tamaño que no está en disco."""
    return bool(getattr(st, "st_file_attributes", 0) & CLOUD_ONLY)


def is_link_dir(st: os.stat_result) -> bool:
    """Unión o enlace simbólico. Las carpetas de OneDrive también son puntos de
    reanálisis, pero con otra etiqueta: esas sí se recorren."""
    return getattr(st, "st_reparse_tag", 0) in (IO_REPARSE_TAG_MOUNT_POINT,
                                                IO_REPARSE_TAG_SYMLINK)


def scan(root: str, cfg: Config, cancel: threading.Event,
         on_progress: Callable[[int, int], None] | None = None) -> tuple[list[tuple], bool]:
    """Top N por tamaño. Devuelve (filas, cancelado).

    Un montículo de tamaño fijo mantiene la memoria constante sin importar
    cuántos archivos haya. Fila: (ruta, nombre, tipo, bytes, creado, escaneado).
    """
    min_size = int(cfg.scan_min_mb * MB)
    heap: list[tuple[int, int, str]] = []
    ticket = itertools.count()
    seen = 0
    rest = (1.0 / cfg.scan_duty - 1.0) if 0 < cfg.scan_duty < 1 else 0.0
    root = os.path.abspath(root)
    try:
        root_dev = os.stat(root).st_dev
    except OSError:
        return [], False

    stack = [root]
    mark = time.perf_counter()
    while stack:
        folder = stack.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    seen += 1
                    if seen % 1024 == 0:
                        # Ciclo útil: trabaja un tramo y descansa otro proporcional.
                        worked = time.perf_counter() - mark
                        if cancel.wait(worked * rest) or cancel.is_set():
                            return [], True
                        mark = time.perf_counter()
                        if on_progress:
                            on_progress(seen, len(heap))
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            if is_link_dir(st) or (LINUX and entry.path in LINUX_SKIP):
                                continue
                            # Sin cruzar a otros discos ni a montajes de red.
                            if not WINDOWS and st.st_dev != root_dev:
                                continue
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            if st.st_size < min_size or is_cloud_only(st):
                                continue
                            item = (st.st_size, next(ticket), entry.path)
                            if len(heap) < cfg.scan_top_n:
                                heapq.heappush(heap, item)
                            elif item > heap[0]:
                                heapq.heapreplace(heap, item)
                    except OSError:
                        continue
        except OSError:
            continue     # sin permiso o desapareció: se sigue con lo demás

    now = time.time()
    rows = []
    for size, _, path in sorted(heap, reverse=True):
        name = os.path.basename(path)
        rows.append((clean_text(path), clean_text(readable_name(name)),
                     clean_text(file_type(name)), size, creation_time(path), now))
    if on_progress:
        on_progress(seen, len(rows))
    return rows, False
