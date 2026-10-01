"""Gegenpruefung R4: Sperrhalter-Erkennung gegen echte Windows-Prozesse."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from linkedin_mcp_server import mivia_outreach as mo

win = pytest.mark.skipif(os.name != "nt", reason="Windows-API")


@win
def test_kernel32_prototypes_are_declared():
    k = mo._win_kernel32()
    for name in ("OpenProcess", "GetExitCodeProcess", "GetProcessTimes", "CloseHandle"):
        assert getattr(k, name).argtypes, name
        assert getattr(k, name).restype is not None, name


@win
def test_live_then_dead_child():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        state, created = mo._win_process_state(child.pid)
        assert state == "alive" and created
        assert mo._process_created(child.pid) == created  # stable on re-read
        assert mo._pid_running(child.pid)
    finally:
        child.kill()
        child.wait()
    time.sleep(0.2)
    assert mo._win_process_state(child.pid)[0] == "dead"
    assert not mo._pid_running(child.pid)


@win
def test_protected_process_counts_as_running():
    # PID 4 (System) or csrss: OpenProcess may refuse -- never "dead".
    assert mo._pid_running(4)


@win
def test_nonexistent_and_out_of_range_pid_are_dead():
    assert mo._win_process_state(0xFFFFFFFC)[0] == "dead"
    assert mo._win_process_state(2**40)[0] == "dead"


def test_denied_holder_keeps_its_lock(tmp_path, monkeypatch):
    lock = tmp_path / "x.lock"
    lock.write_bytes(b"4242:123:abc")
    monkeypatch.setattr(mo, "_pid_running", lambda pid: True)
    monkeypatch.setattr(mo, "_process_created", lambda pid: None)
    assert mo._lock_holder_alive(lock) is True


def test_old_format_lock_falls_back_to_age(tmp_path):
    lock = tmp_path / "x.lock"
    lock.write_bytes(b"legacy-token-without-colons")
    assert mo._lock_holder_alive(lock) is None
    old = time.time() - 600
    os.utime(lock, (old, old))
    with mo._file_lock(lock, timeout=2, stale_after=60):
        assert lock.exists()


def test_live_holder_yields_timeout_not_takeover(tmp_path):
    lock = tmp_path / "x.lock"
    me = os.getpid()
    lock.write_bytes(f"{me}:{mo._process_created(me) or 0}:t".encode())
    old = time.time() - 600
    os.utime(lock, (old, old))
    with pytest.raises(TimeoutError):
        with mo._file_lock(lock, timeout=0.3, stale_after=60):
            pass
    assert lock.read_bytes().startswith(str(me).encode())
