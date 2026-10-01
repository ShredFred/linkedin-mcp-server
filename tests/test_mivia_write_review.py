"""MiViA fork review 2026-10-01: invite note and post text, connect_guarded
ledger mapping, partial batch report. No browser."""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client, FastMCP

from linkedin_mcp_server import mivia_outreach as outreach


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(outreach.LEDGER_ENV, str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv(outreach.NOTES_ENV, str(tmp_path / "notes.json"))
    monkeypatch.delenv("MIVIA_INVITE_NOTE_MAX", raising=False)


def _call(name, args, *, extractor=None, monkeypatch=None):
    import linkedin_mcp_server.tools.mivia as m

    if monkeypatch is not None:

        async def fake_run(ctx, tool, body):
            return await body(extractor)

        monkeypatch.setattr(m, "_run", fake_run)
    mcp = FastMCP("t")
    m.register_mivia_tools(mcp)

    async def go():
        async with Client(mcp) as c:
            return (await c.call_tool(name, args)).structured_content

    return asyncio.run(go())


@pytest.mark.parametrize("bad", ["​", " ", "\u0085", "﻿", "‎"])
def test_invite_note_refuses_invisible_controls(bad):
    from linkedin_mcp_server.tools.mivia import check_invite_note

    assert check_invite_note(f"Hallo{bad}Welt")["status"] == "invalid_note"


def test_invite_note_counts_utf16_units():
    from linkedin_mcp_server.tools.mivia import check_invite_note

    # 150 emojis are 150 code points but 300 UTF-16 units: over the 200 limit.
    assert check_invite_note("\U0001f600" * 150)["status"] == "note_too_long"
    assert check_invite_note("\U0001f600" * 100) is None


def test_post_refuses_invisible_control_and_utf16_overlength():
    out = _call("create_post", {"text": "a b", "confirm_post": False})
    assert out["status"] == "invalid_text"
    out = _call("create_post", {"text": "\U0001f600" * 1501, "confirm_post": False})
    assert out["status"] == "invalid_text"


class _Ex:
    def __init__(self, status):
        self.status = status

    async def connect_with_person(self, username, note=None):
        return {"status": self.status}


@pytest.mark.parametrize("raw", ["unavailable", "custom_note_limit_reached"])
def test_connect_refusal_before_send_does_not_block_retry(monkeypatch, raw):
    import linkedin_mcp_server.tools.mivia as m

    monkeypatch.setattr(m, "_pace", lambda *a, **k: None)
    args = {"linkedin_username": "dieter", "confirm_send": True}
    ex = _ExClick(raw, False)
    _call("connect_guarded", args, extractor=ex, monkeypatch=monkeypatch)
    ledger = outreach.Ledger.default()
    assert not ledger.already_contacted("invite", "dieter", None)


def test_note_upsell_after_a_send_click_still_blocks(monkeypatch):
    # The upsell can follow the Enter fallback or a slowly closing dialog:
    # an invitation may have left, so the person stays blocked.
    import linkedin_mcp_server.tools.mivia as m

    monkeypatch.setattr(m, "_pace", lambda *a, **k: None)
    args = {"linkedin_username": "dieter", "confirm_send": True}
    ex = _ExClick("custom_note_limit_reached", True)
    _call("connect_guarded", args, extractor=ex, monkeypatch=monkeypatch)
    assert outreach.Ledger.default().already_contacted("invite", "dieter", None)


def test_connect_send_failed_still_blocks(monkeypatch):
    import linkedin_mcp_server.tools.mivia as m

    monkeypatch.setattr(m, "_pace", lambda *a, **k: None)
    args = {"linkedin_username": "dieter", "confirm_send": True}
    _call(
        "connect_guarded", args, extractor=_Ex("send_failed"), monkeypatch=monkeypatch
    )
    assert outreach.Ledger.default().already_contacted("invite", "dieter", None)


class _ExClick(_Ex):
    def __init__(self, status, clicked, raises=False):
        super().__init__(status)
        self.invite_send_clicked = clicked
        self.raises = raises

    async def connect_with_person(self, username, note=None):
        if self.raises:
            raise RuntimeError("browser died")
        return {"status": self.status}


def _last_status():
    rows = outreach.Ledger.default().path.read_text(encoding="utf-8").splitlines()
    import json

    return json.loads(rows[-1])["status"]


@pytest.mark.parametrize(
    ("clicked", "blocked", "status"),
    [(False, False, "not_sent"), (True, True, "unknown")],
)
def test_connect_unavailable_booked_by_click_marker(
    monkeypatch, clicked, blocked, status
):
    import linkedin_mcp_server.tools.mivia as m

    monkeypatch.setattr(m, "_pace", lambda *a, **k: None)
    args = {"linkedin_username": "dieter", "confirm_send": True}
    ex = _ExClick("connect_unavailable", clicked)
    _call("connect_guarded", args, extractor=ex, monkeypatch=monkeypatch)
    assert (
        bool(outreach.Ledger.default().already_contacted("invite", "dieter", None))
        is blocked
    )
    assert _last_status() == status


def test_connect_unavailable_without_marker_fails_closed(monkeypatch):
    import linkedin_mcp_server.tools.mivia as m

    monkeypatch.setattr(m, "_pace", lambda *a, **k: None)
    args = {"linkedin_username": "dieter", "confirm_send": True}
    _call(
        "connect_guarded",
        args,
        extractor=_Ex("connect_unavailable"),
        monkeypatch=monkeypatch,
    )
    assert outreach.Ledger.default().already_contacted("invite", "dieter", None)


@pytest.mark.parametrize(("clicked", "blocked"), [(False, False), (True, True)])
def test_connect_exception_booked_by_click_marker(monkeypatch, clicked, blocked):
    import linkedin_mcp_server.tools.mivia as m

    monkeypatch.setattr(m, "_pace", lambda *a, **k: None)
    args = {"linkedin_username": "dieter", "confirm_send": True}
    ex = _ExClick("connected", clicked, raises=True)
    with pytest.raises(Exception):
        _call("connect_guarded", args, extractor=ex, monkeypatch=monkeypatch)
    assert (
        bool(outreach.Ledger.default().already_contacted("invite", "dieter", None))
        is blocked
    )


def test_batch_exception_keeps_earlier_verified_sends(monkeypatch):
    import linkedin_mcp_server.tools.mivia as m

    monkeypatch.setattr(m, "_pace", lambda *a, **k: None)
    monkeypatch.setattr(m, "SEND_GAP", (0.0, 0.0))
    monkeypatch.setattr(
        outreach.Ledger, "canary_verified", lambda self, sha, canary: True
    )
    calls = []

    async def fake_send(
        ex, ledger, username, message, *, campaign, allow_repeat=False, **kw
    ):
        calls.append(username)
        if len(calls) == 2:
            raise RuntimeError("rate limited")
        return {"recipient": username, "status": "verified", "verified": True}

    monkeypatch.setattr(m, "_send_and_verify", fake_send)
    out = _call(
        "send_campaign_batch",
        {
            "message": "Text",
            "recipients": ["anna", "bert", "carl"],
            "campaign": "c",
            "confirm_send": True,
            "batch_size": 3,
        },
        extractor=object(),
        monkeypatch=monkeypatch,
    )
    assert out["status"] == "stopped_on_failure"
    assert [r["status"] for r in out["results"]] == ["verified", "unknown"]
    assert out["remaining"] == ["carl"]
    assert calls == ["anna", "bert"]


def test_message_content_allows_bare_www_but_not_http():
    import linkedin_mcp_server.tools.mivia as m

    assert m.check_message_content("Mehr unter www.mivia.ai, gerne melden.") is None
    refused = m.check_message_content("Mehr unter http://mivia.ai")
    assert refused and refused["status"] == "content_check_failed"
    assert m.check_message_content("Hallo {{vorname}}, kurz zu MiViA.")


@pytest.mark.parametrize(
    "text",
    [
        "siehe www.bit.ly/abc",
        "www.lnkd.in/x",
        "Termin: www.calendly.com/mivia/30min",
    ],
)
def test_bare_www_still_gets_shortener_and_calendly_rules(text):
    import linkedin_mcp_server.tools.mivia as m

    refused = m.check_message_content(text)
    assert refused and refused["status"] == "content_check_failed"
