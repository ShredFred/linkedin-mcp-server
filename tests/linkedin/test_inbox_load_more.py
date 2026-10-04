"""#1150: get_inbox drives "load more" until the requested limit is reached.

The scripted page answers the round script with a row count, so the loop's
stop rules are checked without a browser: reached target, stagnation, hard
cap, and an answer that is not a count.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from linkedin_mcp_server.linkedin.content import PageContentReader
from linkedin_mcp_server.linkedin.conversations import ConversationReader
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.profile_page import ProfilePageReader
from linkedin_mcp_server.linkedin.session import PageSession


async def _no_target() -> None:
    raise AssertionError("not used")


def _reader(page: Any) -> ConversationReader:
    session = PageSession(page)
    return ConversationReader(
        session,
        PageNavigator(session),
        PageContentReader(session),
        ProfilePageReader(session, _no_target),
    )


@pytest.fixture(autouse=True)
def no_delay():
    with patch.object(PageSession, "delay", new_callable=AsyncMock) as delay:
        yield delay


def _scripted(mock_page: Any, counts: list[Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    answers = iter(counts)

    async def evaluate(script: str, arg: Any = None) -> Any:
        assert script == ConversationReader._LOAD_MORE_ROUND_JS
        calls.append(arg)
        return next(answers)

    mock_page.evaluate = evaluate
    return calls


class TestLoadConversationRows:
    async def test_loads_in_rounds_until_the_limit(self, mock_page, no_delay):
        calls = _scripted(mock_page, [20, 27, 27, 40, 50])
        count = await _reader(mock_page)._load_conversation_rows(50)
        assert count == 50
        assert len(calls) == 5
        assert all(call == {"target": 50, "act": True} for call in calls)
        assert no_delay.await_count == 4

    async def test_target_already_present_runs_one_round(self, mock_page, no_delay):
        calls = _scripted(mock_page, [23])
        assert await _reader(mock_page)._load_conversation_rows(20) == 23
        assert len(calls) == 1
        no_delay.assert_not_awaited()

    async def test_stops_after_three_stagnant_rounds(self, mock_page):
        calls = _scripted(mock_page, [20, 26, 26, 26, 26, 99])
        assert await _reader(mock_page)._load_conversation_rows(50) == 26
        assert len(calls) == 5

    async def test_a_shrinking_list_counts_as_stagnant(self, mock_page):
        calls = _scripted(mock_page, [20, 18, 19, 20, 99])
        assert await _reader(mock_page)._load_conversation_rows(50) == 20
        assert len(calls) == 4

    async def test_growth_resets_the_stagnation_counter(self, mock_page):
        calls = _scripted(mock_page, [20, 20, 20, 21, 21, 21, 30])
        assert await _reader(mock_page)._load_conversation_rows(30) == 30
        assert len(calls) == 7

    async def test_hard_cap_bounds_an_endless_trickle(self, mock_page):
        cap = ConversationReader._LOAD_MORE_MAX_ROUNDS
        calls = _scripted(mock_page, list(range(1, cap + 10)))
        assert await _reader(mock_page)._load_conversation_rows(10_000) == cap
        assert len(calls) == cap

    @pytest.mark.parametrize("answer", [None, "20", True, {"rows": 20}, 2.5])
    async def test_non_count_answer_stops(self, mock_page, answer):
        calls = _scripted(mock_page, [20, answer, 40])
        assert await _reader(mock_page)._load_conversation_rows(50) == 20
        assert len(calls) == 2

    async def test_scan_loads_after_scrolling_and_before_reading(self, mock_page):
        reader = _reader(mock_page)
        order: list[str] = []
        mock_page.wait_for_selector = AsyncMock()

        async def scroll(**_kwargs: Any) -> None:
            order.append("scroll")

        async def load(target: int) -> int:
            order.append(f"load:{target}")
            return target

        async def evaluate(_script: str, _arg: Any = None) -> Any:
            order.append("rows")
            return {
                "rows": [],
                "stoppedAt": None,
                "firstIndexGap": None,
                "startThreadId": None,
            }

        mock_page.evaluate = evaluate
        with (
            patch.object(reader, "_scroll_main_scrollable_region", scroll),
            patch.object(reader, "_load_conversation_rows", load),
        ):
            await reader._extract_conversation_thread_refs(
                limit=40, context="inbox", scroll_attempts=1, load_until=40
            )
        assert order == ["scroll", "load:40", "rows"]


class TestLoadMoreRoundScript:
    """The round script itself, run in Chromium against a synthetic list."""

    PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body><main>
      <div id="list" style="height:200px;overflow-y:auto"><ul id="rows"></ul></div>
      <button id="more">Load more conversations</button>
    </main><script>
      let added = 0;
      const add = n => {
        for (let i = 0; i < n; i++) {
          const li = document.createElement('li');
          li.style.height = '40px';
          li.innerHTML = `<label aria-label="Select conversation with Ada ${added}">x</label>`;
          document.getElementById('rows').appendChild(li);
          added++;
        }
      };
      add(20);
      document.getElementById('more').addEventListener('click', () => {
        if (added < 45) add(10);
      });
    </script></body></html>"""

    @pytest.fixture
    async def dom_page(self):
        from patchright.async_api import async_playwright

        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(
                    channel="chromium", headless=True
                )
            except Exception as exc:  # pragma: no cover - local only
                pytest.skip(f"chromium unavailable: {exc}")
            page = await browser.new_page()
            await page.set_content(self.PAGE)
            try:
                yield page
            finally:
                await browser.close()

    @pytest.mark.browser_dom
    async def test_button_is_pressed_until_rows_stop_growing(self, dom_page):
        script = ConversationReader._LOAD_MORE_ROUND_JS
        seen = []
        for _ in range(6):
            seen.append(await dom_page.evaluate(script, {"target": 100, "act": True}))
        assert seen == [20, 30, 40, 50, 50, 50]

    @pytest.mark.browser_dom
    async def test_reached_target_does_not_press(self, dom_page):
        script = ConversationReader._LOAD_MORE_ROUND_JS
        assert await dom_page.evaluate(script, {"target": 20, "act": True}) == 20
        assert await dom_page.evaluate(script, {"target": 20, "act": False}) == 20
