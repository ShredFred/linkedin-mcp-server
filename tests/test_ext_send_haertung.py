"""fork send hardening 2026-10-01: pre-click exceptions, batch
remaining, content checks for bare links, Calendly host, hidden format
characters and DM length. No browser."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastmcp import Client, FastMCP

from linkedin_mcp_server import ext_outreach as outreach


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(outreach.LEDGER_ENV, str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv(outreach.NOTES_ENV, str(tmp_path / "notes.json"))
    import linkedin_mcp_server.tools.ext as m

    class _NoWait:
        @staticmethod
        def uniform(a, b):
            return 0.0

    # Read-back wait and send gap both draw from random.uniform.
    monkeypatch.setattr(m, "random", _NoWait)


def _call(name, args, extractor, monkeypatch):
    import linkedin_mcp_server.tools.ext as m

    async def fake_run(ctx, tool, body):
        return await body(extractor)

    monkeypatch.setattr(m, "_run", fake_run)
    mcp = FastMCP("t")
    m.register_ext_tools(mcp)

    async def go():
        async with Client(mcp) as c:
            return (
                await c.call_tool(name, args, raise_on_error=False)
            ).structured_content

    return asyncio.run(go())


def _rows():
    path = outreach.Ledger.default().path
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


class _Page:
    url = "https://www.linkedin.com/feed/"


class _Session:
    page = _Page()


class _ExRaise:
    """send_message raises before the click (profile page did not load)."""

    ext_session = _Session()

    def __init__(self, exc):
        self.exc = exc
        self.calls = []

    async def send_message(self, username, message, *, confirm_send):
        self.calls.append(username)
        raise self.exc


def test_pre_click_exception_books_not_sent_and_allows_retry(monkeypatch):
    import linkedin_mcp_server.tools.ext as m

    ledger = outreach.Ledger.default()
    ex = _ExRaise(RuntimeError("profile did not load"))
    with pytest.raises(RuntimeError):
        asyncio.run(m._send_and_verify(ex, ledger, "dieter", "Hallo", campaign=None))
    assert _rows()[-1]["status"] == "not_sent"
    assert not ledger.already_contacted("message", "dieter", outreach.text_sha("Hallo"))


def test_cancellation_during_send_stays_unknown_and_blocks():
    import linkedin_mcp_server.tools.ext as m

    ledger = outreach.Ledger.default()
    ex = _ExRaise(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(m._send_and_verify(ex, ledger, "dieter", "Hallo", campaign=None))
    assert _rows()[-1]["status"] == "unknown"
    assert ledger.already_contacted("message", "dieter", outreach.text_sha("Hallo"))


class _ExBatch:
    """Canary verified beforehand; second target raises before the click."""

    ext_session = _Session()

    def __init__(self, failing):
        self.failing = failing
        self.sent = []

    async def send_message(self, username, message, *, confirm_send):
        if username == self.failing:
            raise RuntimeError("page timeout")
        self.sent.append(username)
        return {"sent": True, "url": "/messaging/thread/T1/"}

    async def get_conversation(self, **lookup):
        return {"sections": {"c": "Kampagnentext"}}


def _verify_canary(sha):
    outreach.Ledger.default().append(
        {
            "attempt": "c1",
            "kind": "message",
            "recipient": outreach.recipient_key(outreach.DEFAULT_CANARY),
            "text_sha": sha,
            "status": "verified",
            "started_at": "2026-10-01T08:00:00+02:00",
        }
    )


def test_batch_keeps_pre_click_failure_in_remaining(monkeypatch):
    import linkedin_mcp_server.tools.ext as m

    monkeypatch.setattr(m, "_pace", lambda *a, **k: None)
    monkeypatch.setattr(
        outreach, "delivered_in_conversation", lambda message, conv: True
    )
    _verify_canary(outreach.text_sha("Kampagnentext"))
    out = _call(
        "send_campaign_batch",
        {
            "message": "Kampagnentext",
            "recipients": ["anna", "bert", "carl"],
            "campaign": "k1",
            "confirm_send": True,
            "batch_size": 3,
        },
        _ExBatch("bert"),
        monkeypatch,
    )
    assert out["status"] == "stopped_on_failure"
    assert [r["status"] for r in out["results"]] == ["verified", "not_sent"]
    # bert got nothing and stays open; the verified send before is reported.
    assert out["remaining"] == ["bert", "carl"]


def test_batch_keeps_pace_refused_recipient_in_remaining(monkeypatch):
    import linkedin_mcp_server.tools.ext as m

    monkeypatch.setattr(m, "_pace", lambda *a, **k: None)
    _verify_canary(outreach.text_sha("Kampagnentext"))
    calls = {"n": 0}

    def fake_book(ledger, kind, row, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            return {"status": "pace_lock_busy", "detail": "busy"}
        ledger.append(row)
        return None

    monkeypatch.setattr(m, "_book_attempt", fake_book)
    monkeypatch.setattr(
        outreach, "delivered_in_conversation", lambda message, conv: True
    )
    out = _call(
        "send_campaign_batch",
        {
            "message": "Kampagnentext",
            "recipients": ["anna", "bert"],
            "campaign": "k1",
            "confirm_send": True,
            "batch_size": 2,
        },
        _ExBatch(None),
        monkeypatch,
    )
    assert out["status"] == "stopped_on_failure"
    assert out["remaining"] == ["bert"]


@pytest.mark.parametrize(
    "text",
    [
        "Termin: calendly.com/acme/30min",
        "Kurz: bit.ly/abc123",
        "Termin: https://calendly.com.evil.example/ext_jane-doe",
        "Termin: https://calendly.com/acme_jane-doe-x/30",
        "Termin: https://evil.example/?r=calendly.com/acme_jane-doe",
    ],
)
def test_content_check_catches_bare_and_disguised_links(text):
    from linkedin_mcp_server.tools.ext import check_message_content

    refused = check_message_content(text)
    assert refused and refused["status"] == "content_check_failed", text


@pytest.mark.parametrize(
    "text",
    [
        "Termin: https://calendly.com/acme_jane-doe/30min",
        "Termin: calendly.com/acme_jane-doe/30min",
        "Mehr unter www.example.com, z.B. die Fallstudien.",
        "Familie \U0001f468‍\U0001f469‍\U0001f467 und Flagge \U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f",
    ],
)
def test_content_check_lets_ordinary_text_pass(text):
    from linkedin_mcp_server.tools.ext import check_message_content

    assert check_message_content(text) is None


@pytest.mark.parametrize("hidden", ["؜", "⁠", "­", "⁤"])
def test_hidden_format_characters_refused_everywhere(hidden):
    from linkedin_mcp_server.tools.ext import check_invite_note, check_message_content

    assert check_message_content(f"Hallo{hidden}Welt")["status"] == "invalid_message"
    assert check_invite_note(f"Hallo{hidden}Welt")["status"] == "invalid_note"


def test_dm_length_counted_in_utf16():
    from linkedin_mcp_server.tools.ext import MESSAGE_MAX_UTF16, check_message_content

    assert check_message_content("a" * MESSAGE_MAX_UTF16) is None
    over = "\U0001f600" * (MESSAGE_MAX_UTF16 // 2 + 1)
    assert check_message_content(over)["status"] == "message_too_long"


def test_dry_run_writes_nothing(monkeypatch):
    out = _call(
        "send_message",
        {"linkedin_username": "dieter", "message": "Hallo", "confirm_send": False},
        None,
        monkeypatch,
    )
    assert out["status"] == "dry_run"
    path = outreach.Ledger.default().path
    assert not path.exists() or not path.read_text(encoding="utf-8").strip()
