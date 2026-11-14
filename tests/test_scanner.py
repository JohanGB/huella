import os
import stat
import sys
import threading
import time
from types import SimpleNamespace as NS

import pytest

import scanner
from config import Config
from scanner import (creation_time, file_type, is_cloud_only, is_link_dir, readable_name, scan)

CFG = Config(scan_min_mb=1, scan_top_n=3, scan_duty=1.0)
MB = 1024 ** 2


def sparse(path, mb):
    with open(path, "wb") as f:
        f.truncate(int(mb * MB))


def run(root, cfg=CFG):
    return scan(str(root), cfg, threading.Event())


def test_keeps_only_top_n_sorted(tmp_path):
    for i, mb in enumerate([2, 9, 5, 7, 3]):
        sparse(tmp_path / f"f{i}.bin", mb)
    rows, cancelled = run(tmp_path)
    assert not cancelled
    assert [r[3] // MB for r in rows] == [9, 7, 5]


def test_row_shape_is_absolute_and_readable(tmp_path):
    sparse(tmp_path / "copia_final-2024.V2.ZIP", 2)
    path, name, ext, size, created, scanned = run(tmp_path)[0][0]
    assert os.path.isabs(path) and path.endswith("copia_final-2024.V2.ZIP")
    assert name == "copia final 2024 V2"
    assert ext == ".zip"
    assert size == 2 * MB


def test_small_files_are_ignored(tmp_path):
    sparse(tmp_path / "small.bin", 0.5)
    assert run(tmp_path)[0] == []


def test_symlink_loop_does_not_hang(tmp_path):
    (tmp_path / "sub").mkdir()
    sparse(tmp_path / "sub" / "a.bin", 2)
    try:
        os.symlink(tmp_path, tmp_path / "sub" / "bucle")
        os.symlink(tmp_path / "sub" / "a.bin", tmp_path / "alias.bin")
    except OSError:
        pytest.skip("crear enlaces requiere permisos en este sistema")
    assert len(run(tmp_path)[0]) == 1


def test_unreadable_folder_is_skipped(tmp_path):
    (tmp_path / "ok").mkdir()
    sparse(tmp_path / "ok" / "a.bin", 2)
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        assert len(run(tmp_path)[0]) == 1
    finally:
        locked.chmod(stat.S_IRWXU)


def test_missing_root_returns_empty(tmp_path):
    assert run(tmp_path / "nope") == ([], False)


def test_cancel_stops_early(tmp_path):
    for i in range(3000):
        (tmp_path / f"{i}").touch()
    cancel = threading.Event()
    cancel.set()
    assert scan(str(tmp_path), CFG, cancel) == ([], True)


def test_invalid_utf8_filename_can_be_stored(tmp_path):
    if sys.platform != "linux":
        pytest.skip("solo Linux admite bytes arbitrarios")
    raw = os.fsencode(tmp_path) + b"/mal\xff.bin"
    with open(raw, "wb") as f:
        f.truncate(2 * MB)
    rows, _ = run(tmp_path)
    rows[0][0].encode("utf-8")
    rows[0][1].encode("utf-8")


def test_never_opens_files(tmp_path, monkeypatch):
    sparse(tmp_path / "a.bin", 2)
    import builtins
    def forbidden(*a, **k):
        raise AssertionError("el escáner abrió un archivo")
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(os, "open", forbidden)
    assert len(run(tmp_path)[0]) == 1


def test_birthtime_is_real_creation_not_metadata_change(tmp_path):
    if sys.platform != "linux":
        pytest.skip("statx es de Linux")
    f = tmp_path / "a.bin"
    f.touch()
    born = creation_time(str(f))
    if born is None:
        pytest.skip("este sistema de archivos no guarda btime")
    time.sleep(1.1)
    f.chmod(0o600)                       # mueve st_ctime
    os.utime(f, (1, 1))                  # mueve st_mtime
    assert f.stat().st_ctime > born + 0.5
    assert creation_time(str(f)) == pytest.approx(born, abs=1)


def test_creation_time_of_missing_file():
    assert creation_time("/no/existe") is None


def test_windows_creation_uses_birthtime(monkeypatch):
    monkeypatch.setattr(scanner, "LINUX", False)
    monkeypatch.setattr(scanner, "WINDOWS", True)
    monkeypatch.setattr(os, "stat", lambda p, follow_symlinks: NS(st_birthtime=7.0, st_ctime=9.0))
    assert creation_time("x") == 7.0


def test_windows_before_312_uses_ctime(monkeypatch):
    monkeypatch.setattr(scanner, "LINUX", False)
    monkeypatch.setattr(scanner, "WINDOWS", True)
    monkeypatch.setattr(os, "stat", lambda p, follow_symlinks: NS(st_ctime=9.0))
    assert creation_time("x") == 9.0


def test_onedrive_placeholder_is_cloud_only():
    assert is_cloud_only(NS(st_file_attributes=scanner.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS))
    assert not is_cloud_only(NS(st_file_attributes=0x20))


def test_junctions_are_skipped_but_onedrive_folders_are_not():
    assert is_link_dir(NS(st_reparse_tag=scanner.IO_REPARSE_TAG_MOUNT_POINT))
    assert not is_link_dir(NS(st_reparse_tag=0x9000701A))   # IO_REPARSE_TAG_CLOUD_*
    assert not is_link_dir(NS())


def test_names_without_extension():
    assert file_type("Makefile") == "(sin extensión)"
    assert readable_name(".bashrc") == "bashrc"
    assert readable_name("___.bin") == "___.bin"
