"""Own-content menu pick against a real DOM (headless Chromium): a leftover
open dropdown's "Löschen" must never be picked for the freshly opened menu."""

from __future__ import annotations

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.linkedin import ext_own_content as oc

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

LEFTOVER = '<div role="menu"><div role="menuitem">Löschen</div></div>'


@pytest.fixture
async def page():
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            pg = await browser.new_page()
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            yield pg
        finally:
            await browser.close()


async def _pick(page, new_menu):
    await page.set_content(f"<main>{LEFTOVER}<div id='slot'></div></main>")
    await page.evaluate(oc._MENU_PRE_JS)
    await page.evaluate(
        "(h) => { document.getElementById('slot').innerHTML = h; }", new_menu
    )
    return await page.evaluate(
        oc._MENU_PICK_JS, {"words": ["beitrag löschen", "löschen"], "tag": "entry"}
    )


async def test_leftover_entry_is_not_picked(page):
    picked = await _pick(
        page, '<div role="menu"><div role="menuitem">Bearbeiten</div></div>'
    )
    assert picked["count"] == 0


async def test_fresh_entry_is_picked(page):
    picked = await _pick(
        page, '<div role="menu"><div role="menuitem">Beitrag löschen</div></div>'
    )
    assert picked["count"] == 1
    tagged = await page.evaluate(
        "() => document.querySelector('[data-ext-own=\"entry\"]').innerText"
    )
    assert tagged.strip() == "Beitrag löschen"
