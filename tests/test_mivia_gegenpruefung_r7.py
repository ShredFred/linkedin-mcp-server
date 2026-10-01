"""MiViA fork: counter-check of 1bb1fc5 (R7) -- no browser, no network.

(a) a cancellation after the read-back wrote "verified" does not downgrade it;
(c) the raw attendee read carries list_end (the daily scan reads it raw), and
    an empty page before the counted last page does not close the event at once;
(e) a shortener after a single "/" is only skipped when that "/" belongs to
    another host's path.
"""

from __future__ import annotations

import asyncio

import pytest
from test_mivia_r7_abbruch import (  # noqa: F401  (autouse fixture)
    TEXT,
    _cancelled,
    _final,
    _isolated,
    _msg_ex,
    _with_extractor,
)

from linkedin_mcp_server import mivia_daily
from linkedin_mcp_server import mivia_outreach as outreach
from linkedin_mcp_server.mivia_message_checks import shortener_findings

# -- (a) ----------------------------------------------------------------------


def test_cancel_after_verified_row_keeps_verified(monkeypatch):
    import linkedin_mcp_server.tools.mivia as m

    async def no_sleep(_s):
        return None

    async def read_back_then_cancel(extractor, ledger, attempt, *a, **k):
        ledger.append({"attempt": attempt, "status": "verified"})
        raise asyncio.CancelledError()

    monkeypatch.setattr(m.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(m, "_read_back", read_back_then_cancel)
    _with_extractor(monkeypatch, _msg_ex("c"))
    _cancelled(
        "send_message_verified",
        linkedin_username="anna",
        message=TEXT,
        confirm_send=True,
    )
    assert _final("message") == "verified"
    assert outreach.Ledger.default().already_contacted(
        "message", "anna", outreach.text_sha(TEXT)
    )


# -- (c) ----------------------------------------------------------------------


def _collector(tmp_path, monkeypatch, pages):
    monkeypatch.setenv("MIVIA_LINKEDIN_ENGAGERS_SEEN", str(tmp_path / "seen.json"))

    class Ex:
        mivia_session = object()
        mivia_navigator = object()

    c = mivia_daily.Collector(Ex(), {"radar": {"enabled": False}}, tmp_path)

    async def count(*_a):
        return 30  # three pages

    async def nothing(*_a, **_k):
        return None

    class Session:
        page = None

        async def delay(self, *_a):
            return None

    class Actions:
        async def get_event_attendees(self, eid, page, n):
            return pages(page)

    monkeypatch.setattr(mivia_daily, "read_event_count", count)
    monkeypatch.setattr(c, "_goto", nothing)
    c.session = Session()
    c.actions = Actions()
    return c


EID = "7457346711301214208"


def test_empty_mid_page_does_not_close_event_on_first_read(tmp_path, monkeypatch):
    glitch = {"on": True}

    def pages(page):
        if page == 1:
            return {"attendees": [{"slug": "a"}], "complete": False, "next_page": 2}
        if page == 2 and glitch["on"]:
            # what the scraper returns for a page with no cards
            return {
                "attendees": [],
                "complete": True,
                "next_page": None,
                "list_end": True,
            }
        if page == 2:
            return {"attendees": [{"slug": "b"}], "complete": False, "next_page": 3}
        return {
            "attendees": [{"slug": "c"}],
            "complete": True,
            "next_page": None,
            "list_end": True,
        }

    c = _collector(tmp_path, monkeypatch, pages)
    out = asyncio.run(c.event(EID, {"search_reserve": -1000}))
    assert out["scan_finished"] is False
    ev = c._state()["events"][EID]
    assert ev["resume_page"] == 2
    glitch["on"] = False
    out = asyncio.run(c.event(EID, {"search_reserve": -1000}))
    assert out["scan_finished"] is True
    assert set(c._state()["events"][EID]["attendees"]) == {"a", "b", "c"}


def test_empty_page_twice_in_a_row_closes_event(tmp_path, monkeypatch):
    def pages(page):
        if page == 1:
            return {"attendees": [{"slug": "a"}], "complete": False, "next_page": 2}
        return {"attendees": [], "complete": True, "next_page": None, "list_end": True}

    c = _collector(tmp_path, monkeypatch, pages)
    assert (
        asyncio.run(c.event(EID, {"search_reserve": -1000}))["scan_finished"] is False
    )
    assert asyncio.run(c.event(EID, {"search_reserve": -1000}))["scan_finished"] is True
    assert c._state()["events"][EID]["resume_page"] is None


def test_raw_attendee_read_carries_list_end():
    import inspect

    from linkedin_mcp_server.linkedin import mivia_network

    src = (
        inspect.getsource(mivia_network.MiviaActions.get_event_attendees)
        if hasattr(mivia_network, "MiviaActions")
        else inspect.getsource(mivia_network)
    )
    assert '"list_end": exhausted' in src


# -- (e) ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,flagged",
    [
        ("https://example.com/bit.ly/a", False),
        ("example.com/t.co/abc", False),
        ("https://x.com/redirect?u=https://bit.ly/a", True),
        ("example.com/https://bit.ly/a", True),
        ("Termin/bit.ly/abc", True),
        ("/bit.ly/x", True),
        ("Link: /lnkd.in/abc", True),
        ("robot.co/x", False),
    ],
)
def test_shortener_after_slash(text, flagged):
    assert bool(shortener_findings(text)) is flagged
