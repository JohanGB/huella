"""El requisito de solo lectura se comprueba sobre el código, no se promete."""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted(ROOT.glob("*.py"))

# Métodos de psutil.Process que alteran un proceso, y llamadas del SO que alteran
# procesos o archivos. `nice`/`ionice`/`cpu_affinity`/`rlimit` son lectura sin
# argumentos, pero aquí ni siquiera se usan.
FORBIDDEN_ATTRS = {
    "kill", "terminate", "suspend", "resume", "send_signal", "nice", "ionice",
    "cpu_affinity", "rlimit", "killpg", "setpriority", "system", "popen", "spawn",
    "startfile", "unlink", "rmdir", "removedirs", "rename", "replace_file", "truncate",
    "chmod", "chown", "utime", "rmtree",
}
FORBIDDEN_IMPORTS = {"subprocess", "socket", "urllib", "http", "requests", "shutil",
                     "multiprocessing", "winreg"}


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_state_changing_calls(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            attr = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if attr in FORBIDDEN_ATTRS:
                bad.append(f"{attr} en línea {node.lineno}")
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        bad += [f"import {n}" for n in names if n.split(".")[0] in FORBIDDEN_IMPORTS]
    assert not bad


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_supports_python_310_grammar(path):
    ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 10))


def test_only_writer_opens_a_writable_database():
    text = (ROOT / "dashboard.py").read_text(encoding="utf-8")
    assert "Writer" not in text


def test_tray_has_no_timers():
    tree = ast.parse((ROOT / "tray.py").read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert "QTimer" not in names | imported
