"""Fork extension: counter-check of 500656a -- list end with slug-less entries,
anonymous profile viewers never remembered (no browser)."""

from __future__ import annotations

import asyncio
import json

from linkedin_mcp_server import ext_daily
from linkedin_mcp_server.linkedin import ext_network


def _collector(tmp_path, monkeypatch):
    monkeypatch.setenv("LINKEDIN_MCP_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("LINKEDIN_MCP_ENGAGERS_SEEN", str(tmp_path / "seen.json"))

    class Ex:
        ext_session = object()
        ext_navigator = object()

    return ext_daily.Collector(Ex(), {"radar": {"enabled": False}}, tmp_path)


def test_list_end_survives_skipped_slugless_entries():
    out = ext_network.project_attendees(
        {
            "start_page": 3,
            "pages_read": 1,
            "next_page": None,
            "complete": True,
            "attendees": [{"slug": "a", "name": "A"}, {"slug": None, "name": None}, 7],
        }
    )
    assert out["complete"] is False and out["list_end"] is True
    mid = ext_network.project_attendees(
        {
            "start_page": 1,
            "pages_read": 1,
            "next_page": 2,
            "complete": False,
            "attendees": [{"slug": "a"}],
        }
    )
    assert mid["list_end"] is False


def test_event_with_slugless_entry_on_last_page_finishes(tmp_path, monkeypatch):
    c = _collector(tmp_path, monkeypatch)

    async def count(*_a):
        return 50  # overstated: the list really ends on page 2

    async def nothing(*_a, **_k):
        return None

    class Session:
        page = None

        async def delay(self, *_a):
            return None

    class Actions:
        async def get_event_attendees(self, eid, page, pages):
            if page == 1:
                return {
                    "attendees": [{"slug": "a"}],
                    "complete": False,
                    "next_page": 2,
                    "list_end": False,
                }
            return {
                "attendees": [{"slug": "b"}],
                "complete": False,
                "next_page": None,
                "list_end": True,
            }

    monkeypatch.setattr(ext_daily, "read_event_count", count)
    monkeypatch.setattr(c, "_goto", nothing)
    c.session = Session()
    c.actions = Actions()
    out = asyncio.run(c.event("7457346711301214208", {"search_reserve": -1000}))
    assert out["scan_finished"] is True
    state = c._state()["events"]["7457346711301214208"]
    assert state["resume_page"] is None


def test_anonymous_viewers_are_counted_not_remembered(tmp_path, monkeypatch):
    c = _collector(tmp_path, monkeypatch)

    class Actions:
        async def profile_viewers(self, limit):
            return {
                "total_viewers": 3,
                "viewers": [
                    {"slug": "x", "name": "X"},
                    {"slug": None, "name": ""},
                    {"slug": None, "name": None},
                    None,
                ],
            }

    c.actions = Actions()
    for _ in range(3):
        out = asyncio.run(c.viewers())
    assert out["unidentified_viewers"] == 2
    assert out["new_viewers"] == []
    data = json.loads((tmp_path / "seen.json").read_text(encoding="utf-8"))
    keys = data["profile_viewers"]["keys"]
    assert keys == ["viewer:x:"]
