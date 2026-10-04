"""create_post cleanup navigations go through the shared navigation checks.

The feed return after a dry run, a discarded composer or a restored draft used
to call ``page.goto`` directly and so skipped the rate-limit gate, 429
detection and the LinkedIn host assertion.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from linkedin_mcp_server import ext_outreach as outreach
from linkedin_mcp_server.core.exceptions import (
    OffSiteNavigationError,
    RateLimitError,
)
from linkedin_mcp_server.linkedin import ext_post
from linkedin_mcp_server.linkedin import navigation as nav
from linkedin_mcp_server.linkedin.ext_post import ExtPostComposer
from linkedin_mcp_server.linkedin.navigation import PageNavigator, set_rate_limit_hooks


def _page(responses: list[tuple[str, int]]):
    """goto answers in order with (final_url, status)."""
    page = MagicMock()
    page.url = "about:blank"
    answers = iter(responses)

    async def goto(url, **_):
        final, status = next(answers)
        page.url = final
        return SimpleNamespace(status=status, headers={})

    page.goto = AsyncMock(side_effect=goto)
    page.wait_for_selector = AsyncMock(return_value=None)

    async def evaluate(script, arg=None):
        if script is ext_post._MEDIA_PRESENT_JS:
            return 1  # restored draft with media -> cleanup goto to the feed
        return None

    page.evaluate = AsyncMock(side_effect=evaluate)
    return page


def _composer(page):
    session = SimpleNamespace(page=page, check_rate_limit=AsyncMock())
    return ExtPostComposer(session, PageNavigator(session))  # type: ignore[arg-type]


@pytest.fixture
def ledger(tmp_path):
    led = outreach.Ledger(tmp_path / "ledger.jsonl")
    set_rate_limit_hooks(
        lambda: outreach.rate_limit_gate(led),
        lambda exc: outreach.rate_limit_recorder(exc, led),
    )
    yield led
    set_rate_limit_hooks(None, None)


@pytest.fixture(autouse=True)
def quiet_nav():
    with (
        patch.object(nav, "record_page_trace", AsyncMock()),
        patch.object(nav, "stabilize_navigation", AsyncMock()),
        patch.object(nav, "detect_auth_barrier_quick", AsyncMock(return_value=None)),
    ):
        yield


async def test_cleanup_goto_uses_the_navigator():
    page = _page([(ext_post.SHARE_URL, 200), (ext_post.FEED_URL, 200)])
    result = await _composer(page).create_post(
        "Text", image_path=None, confirm_post=False
    )
    assert result["status"] == "editor_has_media"
    assert [c.args[0] for c in page.goto.await_args_list] == [
        ext_post.SHARE_URL,
        ext_post.FEED_URL,
    ]


async def test_active_cooldown_blocks_every_goto(ledger):
    first = _page([(ext_post.SHARE_URL, 429)])
    with pytest.raises(RateLimitError):
        await _composer(first).create_post("Text", image_path=None, confirm_post=False)
    page = _page([(ext_post.SHARE_URL, 200), (ext_post.FEED_URL, 200)])
    with pytest.raises(RateLimitError):
        await _composer(page).create_post("Text", image_path=None, confirm_post=False)
    page.goto.assert_not_awaited()


async def test_429_on_cleanup_books_a_cooldown(ledger):
    page = _page([(ext_post.SHARE_URL, 200), (ext_post.FEED_URL, 429)])
    with pytest.raises(RateLimitError):
        await _composer(page).create_post("Text", image_path=None, confirm_post=False)
    assert [r["kind"] for r in ledger.rows()] == ["rate_limited"]


async def test_offsite_cleanup_redirect_raises():
    page = _page(
        [(ext_post.SHARE_URL, 200), ("https://linkedin.com.evil.example/feed/", 200)]
    )
    with pytest.raises(OffSiteNavigationError):
        await _composer(page).create_post("Text", image_path=None, confirm_post=False)
