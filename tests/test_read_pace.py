"""Every read tool books its action kind with the pacer.

Live on 2026-11-24, a day of ~31 profile reads, 22 people searches and two
post searches left ``pace_status`` at zero for ``profile_view`` and
``search``: the upstream read tools never asked the pacer. These tests call
each read tool through the MCP surface against a mocked extractor and read the
ledger back.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import FastMCP

from linkedin_mcp_server import ext_outreach as outreach
from linkedin_mcp_server.linkedin.contracts import ExtractedSection
from linkedin_mcp_server.tools.company import register_company_tools
from linkedin_mcp_server.tools.feed import register_feed_tools
from linkedin_mcp_server.tools.job import register_job_tools
from linkedin_mcp_server.tools.messaging import register_messaging_tools
from linkedin_mcp_server.tools.person import register_person_tools
from linkedin_mcp_server.tools.post import register_post_tools

_MODULES = ("person", "company", "job", "messaging", "feed", "post")
_RESULT = {"url": "https://www.linkedin.com/", "sections": {"main": "text"}}


def _extractor() -> MagicMock:
    mock = MagicMock()
    for name in (
        "read_person",
        "search_people",
        "get_sidebar_profiles",
        "get_my_profile",
        "read_company",
        "search_companies",
        "get_company_employees",
        "read_job",
        "get_job_apply_url",
        "search_jobs",
        "get_saved_jobs",
        "search_posts",
        "get_inbox",
        "get_conversation",
        "search_conversations",
    ):
        setattr(mock, name, AsyncMock(return_value=dict(_RESULT)))
    mock.extract_page = AsyncMock(
        return_value=ExtractedSection(text="posts", references=[])
    )
    mock.extract_feed = AsyncMock(
        return_value=ExtractedSection(text="feed", references=[])
    )
    return mock


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> tuple[FastMCP, MagicMock]:
    extractor = _extractor()
    ready = AsyncMock(return_value=extractor)
    for module in _MODULES:
        monkeypatch.setattr(
            f"linkedin_mcp_server.tools.{module}.get_ready_extractor", ready
        )
    mcp = FastMCP("test")
    for register in (
        register_person_tools,
        register_company_tools,
        register_job_tools,
        register_messaging_tools,
        register_feed_tools,
        register_post_tools,
    ):
        register(mcp)
    return mcp, extractor


def _today(action: str) -> int:
    return outreach.Pacer(outreach.Ledger.default()).state(action)["today"]


def _counts() -> dict[str, int]:
    return {a: _today(a) for a in ("profile_view", "search", "page_read")}


async def _call(mcp: FastMCP, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    result = await mcp.call_tool(tool, args)
    return result.structured_content or {}


READS = [
    ("get_person_profile", {"linkedin_username": "test-user"}, "profile_view", 1),
    ("get_sidebar_profiles", {"linkedin_username": "test-user"}, "profile_view", 1),
    ("search_people", {"keywords": "metallographer"}, "search", 1),
    ("search_companies", {"keywords": "steel"}, "search", 1),
    ("search_jobs", {"keywords": "lab", "max_pages": 2}, "search", 2),
    ("search_posts", {"keywords": "microscopy", "max_pages": 3}, "search", 3),
    ("get_my_profile", {}, "page_read", 1),
    ("get_company_profile", {"company_name": "example-co"}, "page_read", 1),
    ("get_company_posts", {"company_name": "example-co"}, "page_read", 1),
    ("get_company_employees", {"company_name": "example-co"}, "page_read", 1),
    ("get_job_details", {"job_id": "4012345678"}, "page_read", 1),
    ("get_job_apply_url", {"job_id": "4012345678"}, "page_read", 1),
    ("get_saved_jobs", {"max_pages": 2}, "page_read", 2),
    ("get_inbox", {}, "page_read", 1),
    ("get_conversation", {"linkedin_username": "test-user"}, "page_read", 1),
    ("search_conversations", {"keywords": "hello"}, "page_read", 1),
    ("get_feed", {}, "page_read", 1),
]


@pytest.mark.parametrize(("tool", "args", "action", "units"), READS)
async def test_every_read_tool_books_its_action_kind(server, tool, args, action, units):
    mcp, _ = server
    before = _counts()

    await _call(mcp, tool, args)

    after = _counts()
    expected = dict(before)
    expected[action] += units
    # Exactly the one kind: a read booked under the wrong kind is the bug too.
    assert after == expected


async def test_a_spent_budget_refuses_before_the_browser(server, monkeypatch):
    mcp, extractor = server

    def spent(self, action, count=1, **_kwargs):
        raise outreach.PaceExceeded({"action": action, "left": 0})

    monkeypatch.setattr(outreach.Pacer, "take", spent)

    result = await _call(mcp, "search_people", {"keywords": "metallographer"})

    assert result["status"] == "pace_budget_spent"
    assert result["pace"]["action"] == "search"
    extractor.search_people.assert_not_awaited()


async def test_a_failed_read_gives_its_units_back(server):
    mcp, extractor = server
    extractor.read_person.side_effect = RuntimeError("browser died")

    with pytest.raises(Exception):
        await _call(mcp, "get_person_profile", {"linkedin_username": "test-user"})

    assert _today("profile_view") == 0


async def test_every_feed_scroll_books_one_page_read(server):
    mcp, extractor = server

    async def scrolling(num_posts, scroll_gate):
        for _ in range(4):
            assert scroll_gate() is True
        return ExtractedSection(text="feed", references=[])

    extractor.extract_feed.side_effect = scrolling

    result = await _call(mcp, "get_feed", {"num_posts": 50})

    # One for the page load, one per scroll.
    assert _today("page_read") == 5
    assert "pace_stopped" not in result


async def test_a_spent_budget_mid_scroll_returns_what_loaded(server, monkeypatch):
    mcp, extractor = server
    gate_answers: list[bool] = []

    async def scrolling(num_posts, scroll_gate):
        gate_answers.append(scroll_gate())
        return ExtractedSection(text="feed so far", references=[])

    extractor.extract_feed.side_effect = scrolling
    monkeypatch.setattr(
        "linkedin_mcp_server.tools.feed.take_read",
        lambda action, count=1, *, tool: {"status": "pace_budget_spent"},
    )

    result = await _call(mcp, "get_feed", {"num_posts": 50})

    assert gate_answers == [False]
    assert result["sections"]["feed"] == "feed so far"
    assert result["pace_stopped"] is True
