"""Hardening round 3 (2026-10-01): validation before the pacer booking,
click marker for follow_only/unavailable, one source for the content rules.
No browser."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastmcp import Client, FastMCP

from linkedin_mcp_server import ext_message_checks as checks
from linkedin_mcp_server import ext_outreach as outreach


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(outreach.LEDGER_ENV, str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv(outreach.NOTES_ENV, str(tmp_path / "notes.json"))


@pytest.fixture
def booked(monkeypatch):
    import linkedin_mcp_server.tools.ext as m

    taken: list = []
    monkeypatch.setattr(
        m, "_pace", lambda action, count=1, *, tool: taken.append((action, count))
    )
    monkeypatch.setattr(m, "_run", _no_browser)
    return taken


async def _no_browser(ctx, tool, body):
    return {"status": "ran"}


def _call(name, args, extractor=None, monkeypatch=None):
    import linkedin_mcp_server.tools.ext as m

    if extractor is not None:

        async def run(ctx, tool, body):
            return await body(extractor)

        monkeypatch.setattr(m, "_run", run)
    mcp = FastMCP("t")
    m.register_ext_tools(mcp)

    async def go():
        async with Client(mcp) as c:
            res = await c.call_tool(name, args, raise_on_error=False)
            return res.structured_content

    return asyncio.run(go())


# (1) a call the input check refuses must not spend budget
@pytest.mark.parametrize(
    "name,args",
    [
        ("get_event_attendees", {"event_id": "abc"}),
        ("get_event_attendees", {"event_id": "linkedin.com/events/12/"}),
        ("get_event_attendee_count", {"event_id": "not-a-number"}),
        ("get_event_status", {"event_id": ""}),
        ("list_connections", {"since": "gestern"}),
        ("outreach_selftest", {"connect_probe_username": "https://evil.example/x"}),
    ],
)
def test_invalid_input_books_nothing(booked, name, args):
    out = _call(name, args)
    assert booked == []
    assert out is None or out.get("status") != "ran"


def test_valid_event_url_still_books(booked):
    out = _call(
        "get_event_attendees",
        {"event_id": "https://www.linkedin.com/events/7449450258214014977/"},
    )
    assert booked == [("search", 10)]
    assert out["status"] == "ran"


@pytest.mark.parametrize("bad", [0, 101, True])
def test_start_page_checked_before_booking(booked, monkeypatch, bad):
    import linkedin_mcp_server.tools.ext as m

    # Direct call path without pydantic: the tool's own check must hold.
    assert m._int_in(bad, 1, 100) is False


# (2) connect_with_person: the click marker decides for every pre-click status
class _Ex:
    def __init__(self, status, clicked):
        self.status = status
        self.invite_send_clicked = clicked

    async def connect_with_person(self, username, note=None):
        return {"status": self.status}


def _last_status():
    rows = outreach.Ledger.default().path.read_text(encoding="utf-8").splitlines()
    return json.loads(rows[-1])["status"]


@pytest.mark.parametrize(
    "raw",
    ["follow_only", "unavailable", "connect_unavailable", "custom_note_limit_reached"],
)
@pytest.mark.parametrize("clicked,expected", [(False, "not_sent"), (True, "unknown")])
def test_click_marker_decides(monkeypatch, raw, clicked, expected):
    import linkedin_mcp_server.tools.ext as m

    monkeypatch.setattr(m, "_pace", lambda *a, **k: None)
    _call(
        "connect_with_person",
        {"linkedin_username": "dieter", "confirm_send": True},
        extractor=_Ex(raw, clicked),
        monkeypatch=monkeypatch,
    )
    assert _last_status() == expected
    blocked = outreach.Ledger.default().already_contacted("invite", "dieter", None)
    assert bool(blocked) is clicked


# (3) one source, strictest rule kept
def test_aliases_point_at_the_single_source():
    import linkedin_mcp_server.tools.ext as m
    from linkedin_mcp_server.tools import ext_stage2

    assert m._hidden_format_char is checks.hidden_format_char
    assert ext_stage2._hidden_format_char is checks.hidden_format_char
    assert m._utf16_len is checks.utf16_len
    assert m.MESSAGE_MAX_UTF16 == checks.MESSAGE_MAX_UTF16 == 8000


@pytest.mark.parametrize("ch", [" ", "‮", "⁦", "\u0085", "­"])
def test_hidden_chars_refused_by_both_routes(ch):
    from linkedin_mcp_server.tools.ext import check_message_content

    assert checks.hidden_format_char(ch)
    assert check_message_content(f"Hallo{ch}x")["status"] == "invalid_message"
    assert checks.check_outgoing(f"Hallo{ch}x", max_utf16=100)


@pytest.mark.parametrize(
    "text",
    [
        "Termin: https://calendly.com./ext_jane-doe",
        "Termin: xhttps://calendly.com.evil.example/ext_jane-doe",
        "Termin: calendly.com/acme",
        "kurz: bit.ly/abc",
        "Hallo {vorname}",
    ],
)
def test_outgoing_and_message_content_agree_and_refuse(text):
    from linkedin_mcp_server.tools.ext import check_message_content

    assert check_message_content(text)["status"] == "content_check_failed"
    assert checks.check_outgoing(text, max_utf16=8000)["status"] == (
        "content_check_failed"
    )


def test_booking_link_still_passes():
    from linkedin_mcp_server.tools.ext import check_message_content

    ok = "Termin: https://calendly.com/acme_jane-doe/30min und www.example.com"
    assert check_message_content(ok) is None


def test_finished_campaign_books_no_message_budget(booked, monkeypatch):
    monkeypatch.setattr(outreach.Ledger, "canary_verified", lambda *a, **k: True)
    out = _call(
        "send_campaign_batch",
        {
            "message": "Guten Tag, kurze Frage zu Ihrer Metallografie.",
            "recipients": [],
            "campaign": "c1",
            "confirm_send": True,
        },
    )
    assert booked == []
    assert out["status"] == "done"
