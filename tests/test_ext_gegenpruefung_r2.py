"""Gegenpruefung Haertungsrunde 2 (2026-10-01): Befunde aus 9f46463/6868bb0/
ee213e5/eb5a0a9, die die Runde selbst nicht abdeckte."""

from __future__ import annotations

import time

from linkedin_mcp_server import ext_outreach as outreach
from test_ext_stage2_haertung import (  # noqa: F401  (autouse fixture)
    A,
    _call,
    _comment,
    _Commenter,
    _isolated,
)


def _deleted(status: str) -> None:
    from datetime import datetime

    outreach.Ledger.default().append(
        {
            "attempt": f"del-{status}",
            "kind": "comment_delete",
            "activity": A,
            "comment": "1",
            "status": status,
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
    )


def test_verified_delete_reopens_the_post(monkeypatch):
    # Before: one posted comment locked the post forever, even after it was
    # removed by a verified delete_own_comment. Since round 5 the release is
    # bound to the deleted comment: delete_own_comment notes deleted_by on
    # that comment row; an unbound delete no longer releases (see
    # test_ext_r5_kommentar.py).
    actions = _Commenter({"status": "posted", "posted": True})
    assert _comment(monkeypatch, actions)["status"] == "posted"
    time.sleep(1.1)  # second-resolution timestamps
    _deleted("verified")
    ledger = outreach.Ledger.default()
    original = next(
        r for r in ledger.latest_by_attempt().values() if r.get("kind") == "comment"
    )
    ledger.append({"attempt": original["attempt"], "deleted_by": "del-verified"})
    assert _comment(monkeypatch, actions, text="Neuer Gedanke")["status"] == "posted"
    # The new comment blocks the post again.
    assert _comment(monkeypatch, actions, text="Dritter")["status"] == (
        "already_commented"
    )


def test_unverified_delete_keeps_the_post_locked(monkeypatch):
    actions = _Commenter({"status": "posted", "posted": True})
    _comment(monkeypatch, actions)
    _deleted("unverified")
    assert _comment(monkeypatch, actions, text="Neu")["status"] == ("already_commented")


def test_deleted_comment_text_still_refused(monkeypatch):
    actions = _Commenter({"status": "posted", "posted": True})
    _comment(monkeypatch, actions)
    _deleted("verified")
    assert _comment(monkeypatch, actions)["status"] == "duplicate_text"
