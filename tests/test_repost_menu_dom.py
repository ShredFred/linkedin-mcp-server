"""repost_post page scripts against a real DOM (headless Chromium).

The card mimics the measured layout (2026-10-02): a "Reposten" button whose
click inserts two div[role=button] entries. A lazily loaded comment with its
own "Entfernen" button appears in the same moment and must never be read as
a menu entry.
"""

from __future__ import annotations

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.linkedin import mivia_repost as rp

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

CARD = """
<main>
  <div componentkey="update-card-focus-1">
    <p>Beitragstext</p>
    <button aria-label="Gefällt mir">Gefällt mir</button>
    <button id="repost">Reposten</button>
    <div id="slot"></div>
  </div>
  <div id="comments"></div>
</main>
<script>
  document.getElementById('repost').onclick = () => {
    const menu = document.createElement('div');
    menu.innerHTML = MENU_HTML;
    document.getElementById('slot').appendChild(menu);
    // a comment loads in the same moment, with its own remove button
    document.getElementById('comments').innerHTML =
      '<div><span>Kommentar</span><div role="button">Entfernen</div>' +
      '<button>Teilen rückgängig machen</button></div>';
  };
</script>
"""

SHARE_MENU = (
    '<div role="button"><span>Mit Kommentar teilen</span></div>'
    '<div role="button"><span>Sofort teilen</span></div>'
)
UNDO_MENU = (
    '<div role="button"><span>Mit Kommentar teilen</span></div>'
    '<div role="button"><span>Repost rückgängig machen</span></div>'
)


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


async def _open(page, menu_html):
    await page.set_content(CARD.replace("MENU_HTML", repr(menu_html)))
    mark = await page.evaluate(rp._MARK_REPOST_BUTTON_JS)
    assert mark["count"] == 1
    await page.evaluate(rp._MARK_PRESENT_JS)
    await page.locator('[data-mivia-repost="1"]').first.click()
    return await page.evaluate(rp._MENU_ITEMS_JS)


async def test_menu_entries_only_from_the_menu(page):
    items = await _open(page, SHARE_MENU)
    assert [i["text"] for i in items] == ["Mit Kommentar teilen", "Sofort teilen"]
    entry, has_undo = rp.pick_entry(items, "instant")
    assert entry["text"] == "Sofort teilen" and not has_undo
    marked = await page.evaluate(
        '(i) => document.querySelector(`[data-mivia-menu="${i}"]`).innerText',
        entry["index"],
    )
    assert marked.strip() == "Sofort teilen"


async def test_stray_undo_in_comment_is_ignored(page):
    items = await _open(page, SHARE_MENU)
    assert not rp.pick_entry(items, "undo")[1]


async def test_undo_menu_recognised(page):
    items = await _open(page, UNDO_MENU)
    entry, has_undo = rp.pick_entry(items, "undo")
    assert has_undo and entry["text"] == "Repost rückgängig machen"


async def test_no_share_entry_no_menu(page):
    items = await _open(page, '<div role="button">Link kopieren</div>')
    assert items == []


async def test_comment_repost_button_not_counted(page):
    await page.set_content(
        "<main><div componentkey='update-card-focus-1'><button>Reposten</button>"
        "<div componentkey='replaceableComment_urn:li:comment:1'>"
        "<button aria-label='Repost comment'>Reposten</button></div></div></main>"
    )
    mark = await page.evaluate(rp._MARK_REPOST_BUTTON_JS)
    assert mark["count"] == 1
