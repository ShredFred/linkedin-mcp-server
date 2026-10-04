"""Runde 5 (2026-10-01): comment_on_post gibt einen Post nur frei, wenn jedes
offene Kommentar-Attempt durch ein verifiziertes Loeschen genau dieses
Kommentars erledigt ist; Zeitvergleich mit echten Datumswerten."""

from __future__ import annotations

from linkedin_mcp_server import ext_outreach as outreach
from test_ext_stage2_haertung import (  # noqa: F401  (autouse fixture)
    A,
    _comment,
    _Commenter,
    _isolated,
)


def _ledger() -> outreach.Ledger:
    return outreach.Ledger.default()


def _comment_row(attempt: str, text: str, status: str, started_at: str) -> None:
    _ledger().append(
        {
            "attempt": attempt,
            "kind": "comment",
            "activity": A,
            "text_sha": outreach.text_sha(text),
            "status": status,
            "started_at": started_at,
        }
    )


def _delete(attempt: str, status: str, started_at: str, bound_to: str | None) -> None:
    _ledger().append(
        {
            "attempt": attempt,
            "kind": "comment_delete",
            "activity": A,
            "comment": "1",
            "status": status,
            "started_at": started_at,
        }
    )
    if bound_to:
        _ledger().append(
            {"attempt": bound_to, "deleted_by": attempt, "deleted_at": started_at}
        )


def _posted(monkeypatch, text="Neuer Gedanke"):
    return _comment(monkeypatch, _Commenter({"status": "posted", "posted": True}), text)


def test_bound_verified_delete_reopens_the_post(monkeypatch):
    _comment_row("c1", "Alt", "posted", "2026-10-01T10:00:00+02:00")
    _delete("d1", "verified", "2026-10-01T10:05:00+02:00", bound_to="c1")
    assert _posted(monkeypatch)["status"] == "posted"
    assert _posted(monkeypatch, "Dritter")["status"] == "already_commented"


def test_unbound_verified_delete_does_not_release(monkeypatch):
    # Before: any verified delete on the post released every earlier comment.
    _comment_row("c1", "Alt", "posted", "2026-10-01T10:00:00+02:00")
    _delete("d1", "verified", "2026-10-01T10:05:00+02:00", bound_to=None)
    assert _posted(monkeypatch)["status"] == "already_commented"


def test_unknown_attempt_keeps_blocking_after_other_delete(monkeypatch):
    _comment_row("c1", "Alt", "posted", "2026-10-01T10:00:00+02:00")
    _comment_row("c2", "Unklar", "unknown", "2026-10-01T10:01:00+02:00")
    _delete("d1", "verified", "2026-10-01T10:05:00+02:00", bound_to="c1")
    out = _posted(monkeypatch)
    assert out["status"] == "already_commented"
    assert out["previous"]["attempt"] == "c2"


def test_unverified_bound_delete_keeps_blocking(monkeypatch):
    _comment_row("c1", "Alt", "posted", "2026-10-01T10:00:00+02:00")
    _delete("d1", "unverified", "2026-10-01T10:05:00+02:00", bound_to="c1")
    assert _posted(monkeypatch)["status"] == "already_commented"


def test_time_compared_as_datetime_not_text(monkeypatch):
    # 09:30Z is 11:30+02:00 -- after the comment, although "09" < "10" as text.
    _comment_row("c1", "Alt", "posted", "2026-10-01T10:00:00+02:00")
    _delete("d1", "verified", "2026-10-01T09:30:00+00:00", bound_to="c1")
    assert _posted(monkeypatch)["status"] == "posted"


def test_delete_before_comment_does_not_release(monkeypatch):
    # 10:30+02:00 is 08:30Z -- before a comment at 09:00Z, although later as text.
    _comment_row("c1", "Alt", "posted", "2026-10-01T09:00:00+00:00")
    _delete("d1", "verified", "2026-10-01T10:30:00+02:00", bound_to="c1")
    assert _posted(monkeypatch)["status"] == "already_commented"


def test_delete_on_other_post_does_not_release(monkeypatch):
    _comment_row("c1", "Alt", "posted", "2026-10-01T10:00:00+02:00")
    _ledger().append(
        {
            "attempt": "d1",
            "kind": "comment_delete",
            "activity": "999",
            "status": "verified",
            "started_at": "2026-10-01T10:05:00+02:00",
        }
    )
    _ledger().append({"attempt": "c1", "deleted_by": "d1"})
    assert _posted(monkeypatch)["status"] == "already_commented"


def test_undated_rows_keep_blocking(monkeypatch):
    _comment_row("c1", "Alt", "posted", "")
    _delete("d1", "verified", "2026-10-01T10:05:00+02:00", bound_to="c1")
    assert _posted(monkeypatch)["status"] == "already_commented"
