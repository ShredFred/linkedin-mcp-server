"""Background Python children never get a console window on Windows (live 2026-10-13).

A venv's ``python.exe`` is a launcher: started without a console, the real
interpreter it starts allocates a new one and Windows Terminal shows a window --
twice per daemon start. ``pythonw.exe`` never gets a console.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from linkedin_mcp_server import no_console

PKG = Path(no_console.__file__).resolve().parent
SPAWNING = (
    "bootstrap.py",
    "daemon_election.py",
    "installer_supervisor.py",
    "process_tree.py",
)


def test_python_exe_becomes_its_pythonw_sibling(tmp_path, monkeypatch):
    exe = tmp_path / "python.exe"
    exe.write_bytes(b"")
    (tmp_path / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "platform", "win32")
    assert no_console.background_interpreter() == str(tmp_path / "pythonw.exe")


def test_without_pythonw_or_off_windows_nothing_changes(tmp_path, monkeypatch):
    exe = tmp_path / "python.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "platform", "win32")
    assert no_console.background_interpreter() == str(exe), (
        "no sibling: keep the interpreter"
    )
    (tmp_path / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "platform", "linux")
    assert no_console.background_interpreter() == str(exe)


def test_already_pythonw_stays(tmp_path, monkeypatch):
    exe = tmp_path / "pythonw.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "platform", "win32")
    assert no_console.background_interpreter() == str(exe)


@pytest.mark.parametrize("name", SPAWNING)
def test_spawning_modules_do_not_start_sys_executable(name):
    """A new ``sys.executable`` in these modules would bring the windows back."""
    text = (PKG / name).read_text(encoding="utf-8")
    assert not re.search(r"\bsys\.executable\b", text), name
