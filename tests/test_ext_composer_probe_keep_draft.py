"""The probe's cleanup keeps a saved draft instead of discarding it.

2026-11-10: a composer reopened with a saved page draft; the probe's cleanup
answered the close prompt with "Verwerfen" and the draft was gone. When the
prompt offers to keep the draft, the cleanup must keep it.
"""

from __future__ import annotations

import pytest

from patchright.async_api import async_playwright

from linkedin_mcp_server.linkedin import ext_composer_probe as cp

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]


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


_PROMPT = """<html><body>
<div role="alertdialog">
  <p>Möchten Sie diesen Beitrag als Entwurf speichern?</p>
  <button id="discard" onclick="document.body.dataset.picked='discard'">Verwerfen</button>
  <button id="keep" onclick="document.body.dataset.picked='keep'">Als Entwurf speichern</button>
</div></body></html>"""

_DISCARD_ONLY = """<html><body>
<div role="alertdialog">
  <button onclick="document.body.dataset.picked='discard'">Verwerfen</button>
</div></body></html>"""


async def test_cleanup_keeps_a_saved_draft(page) -> None:
    await page.set_content(_PROMPT)
    done = await cp._discard_everything(page)
    assert done == ["kept_draft"], done
    assert await page.evaluate("document.body.dataset.picked") == "keep"


async def test_cleanup_still_discards_without_a_keep_choice(page) -> None:
    await page.set_content(_DISCARD_ONLY)
    done = await cp._discard_everything(page)
    assert done[0] == "clicked", done
    assert await page.evaluate("document.body.dataset.picked") == "discard"
