"""agent/ 与 common/ 用到的第三方包必须都写在 agent/requirements.txt，否则 PyInstaller 打出的 exe 启动即报 ModuleNotFoundError。"""
import ast
import re
import sys
from pathlib import Path

MODULE_TO_PKG = {"yaml": "pyyaml", "win32service": "pywin32", "win32event": "pywin32", "servicemanager": "pywin32",
                 "win32serviceutil": "pywin32", "win32api": "pywin32"}
LOCAL = {"agent", "common"}


def imported_top_level(files):
    mods = set()
    for f in files:
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                mods |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
    return mods


def test_all_third_party_imports_are_in_agent_requirements():
    files = [*Path("agent").glob("*.py"), *Path("common").glob("*.py"), Path("agent_entry.py")]
    third = {m for m in imported_top_level(files) if m not in sys.stdlib_module_names and m not in LOCAL and m != "__future__"}
    reqs = {re.split(r"[<>=;\s]", ln.strip(), maxsplit=1)[0].lower() for ln in Path("agent/requirements.txt").read_text().splitlines()
            if ln.strip() and not ln.startswith("#")}
    missing = sorted(m for m in third if MODULE_TO_PKG.get(m, m).lower() not in reqs)
    assert not missing, f"agent/requirements.txt 缺少: {missing}"
