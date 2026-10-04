"""The load-more round script only acts on the conversation list.

A message bubble in the open thread pane may carry its own "See more" /
"Mehr anzeigen" button; clicking it would expand a message instead of loading
conversations. Runs the script against synthetic Chromium DOMs; no LinkedIn
page is loaded.
"""

from __future__ import annotations

import os

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.linkedin.conversations import ConversationReader

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]


@pytest.fixture
async def dom_page():
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                channel="chromium", headless=True
            )
            page = await browser.new_page()
        except Exception as exc:
            if os.environ.get("CI"):
                raise
            pytest.skip(f"chromium unavailable: {exc}")
        await page.route(
            "https://www.linkedin.com/**",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body='<!DOCTYPE html><html><head><meta charset="utf-8"></head></html>',
            ),
        )
        try:
            yield page
        finally:
            await browser.close()


def _page(list_button: str, thread_button: str) -> str:
    rows = "".join(
        f'<li><label aria-label="Conversation {i}">Person {i}</label></li>'
        for i in range(3)
    )
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
      <main>
        <div id="list" style="height:60px;overflow-y:auto">
          <ul>{rows}</ul>
          {list_button}
        </div>
        <div id="thread"><ul><li><p>Placeholder message</p>{thread_button}</li></ul></div>
      </main>
    </body></html>"""


async def _run(page, html: str):
    await page.goto("https://www.linkedin.com/messaging/")
    await page.set_content(html)
    await page.evaluate(
        """() => { window.clicked = [];
            document.addEventListener('click', e => {
                if (e.target.dataset && e.target.dataset.id) window.clicked.push(e.target.dataset.id);
            }, true); }"""
    )
    count = await page.evaluate(
        ConversationReader._LOAD_MORE_ROUND_JS, {"target": 50, "act": True}
    )
    return count, await page.evaluate("window.clicked")


@pytest.mark.parametrize("word", ["See more", "Mehr anzeigen", "Show more"])
async def test_message_bubble_button_is_never_clicked(dom_page, word):
    count, clicked = await _run(
        dom_page, _page("", f'<button data-id="bubble">{word}</button>')
    )
    assert count == 3
    assert clicked == []


async def test_list_button_is_clicked_not_the_bubble(dom_page):
    count, clicked = await _run(
        dom_page,
        _page(
            '<button data-id="list">Load more conversations</button>',
            '<button data-id="bubble">See more</button>',
        ),
    )
    assert count == 3
    assert clicked == ["list"]


async def test_bubble_button_in_thread_without_list_scroller(dom_page):
    html = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
      <main>
        <ul><li><label aria-label="Conversation 0">Person 0</label></li></ul>
        <div><button data-id="bubble">Mehr anzeigen</button></div>
      </main></body></html>"""
    count, clicked = await _run(dom_page, html)
    assert count == 1
    assert clicked == []
