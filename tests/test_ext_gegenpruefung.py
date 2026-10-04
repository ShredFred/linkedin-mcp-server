"""Gegenpruefung Haertungsrunde 1 (2026-10-01)."""

import os
import time

from linkedin_mcp_server import ext_outreach as outreach


def test_append_after_crashed_append_lock_does_not_time_out(tmp_path):
    # A crashed instance left its append lock behind 20 s ago. With the
    # pacer defaults the append waited 15 s and raised although the lock
    # was dead; a lost "sent" row allows a second send.
    ledger = outreach.Ledger(tmp_path / "x.jsonl")
    lock = tmp_path / "x.append.lock"
    lock.write_bytes(b"crashed-owner")
    old = time.time() - 20
    os.utime(lock, (old, old))
    started = time.monotonic()
    ledger.append({"status": "sent"})
    assert time.monotonic() - started < 14
    assert not lock.exists()
    assert ledger.path.read_text(encoding="utf-8").count("\n") == 1
