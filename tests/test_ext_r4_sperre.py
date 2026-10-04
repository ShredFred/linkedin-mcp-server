"""R4: a lock is taken over only when its holder is gone, never from a live,
slow holder; and invite_to_event shares the network layer's event-id rule."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from linkedin_mcp_server import ext_outreach as outreach


def _age(path, seconds):
    old = time.time() - seconds
    os.utime(path, (old, old))


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_own_token_names_a_live_process(tmp_path):
    lock = tmp_path / "x.lock"
    with outreach._file_lock(lock):
        assert outreach._lock_holder_alive(lock) is True


def test_live_slow_holder_is_never_taken_over(tmp_path):
    """A child process holds the lock, its file aged far beyond stale_after."""
    lock = tmp_path / "x.append.lock"
    code = (
        "import sys,time;from pathlib import Path;"
        "from linkedin_mcp_server import ext_outreach as o\n"
        f"with o._file_lock(Path(r'{lock}')):\n"
        "    print('held', flush=True); time.sleep(4)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout.readline().strip() == "held"
        _age(lock, 600)
        with pytest.raises(TimeoutError):
            with outreach._file_lock(lock, timeout=1.0, stale_after=0.1):
                pass
        assert lock.exists()
    finally:
        child.wait(timeout=30)
    # Once the holder finished, the lock is free again.
    with outreach._file_lock(lock, timeout=2.0):
        pass


def test_crashed_holder_is_taken_over_promptly(tmp_path):
    lock = tmp_path / "x.append.lock"
    pid = _dead_pid()
    lock.write_bytes(f"{pid}:123:1-1".encode())
    _age(lock, 11)
    assert outreach._lock_holder_alive(lock) is False
    start = time.monotonic()
    with outreach._file_lock(lock, timeout=5.0, stale_after=10.0):
        assert time.monotonic() - start < 2.0


def test_reused_pid_counts_as_dead(tmp_path):
    lock = tmp_path / "x.lock"
    created = outreach._process_created(os.getpid())
    if created is None:
        pytest.skip("creation time not readable on this platform")
    lock.write_bytes(f"{os.getpid()}:{created + 1}:1-1".encode())
    assert outreach._lock_holder_alive(lock) is False


def test_foreign_token_falls_back_to_age(tmp_path):
    lock = tmp_path / "x.lock"
    lock.write_bytes(b"crashed-owner")
    assert outreach._lock_holder_alive(lock) is None
    _age(lock, 120)
    with outreach._file_lock(lock, timeout=2.0):
        pass


def test_live_thread_holder_blocks_waiter(tmp_path):
    """Pacer lock, same process: an aged file of a live holder stays."""
    lock = tmp_path / "x.lock"
    held, release = threading.Event(), threading.Event()

    def hold():
        with outreach._file_lock(lock):
            held.set()
            release.wait(10)

    t = threading.Thread(target=hold)
    t.start()
    try:
        assert held.wait(5)
        _age(lock, 3600)
        with pytest.raises(TimeoutError):
            with outreach._file_lock(lock, timeout=0.5, stale_after=60):
                pass
    finally:
        release.set()
        t.join(10)


def test_invite_event_id_rule_is_the_network_rule():
    from linkedin_mcp_server.linkedin import ext_network
    from linkedin_mcp_server.tools import ext_stage2

    assert ext_stage2._NETWORK_EVENT_ID_RE is ext_network._EVENT_ID_RE
