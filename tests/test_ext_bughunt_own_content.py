"""Bughunt 2026-10-11: own comment read-back, lock release, edit re-read, card text."""

from __future__ import annotations

import asyncio

import pytest

from linkedin_mcp_server import ext_outreach as outreach
from linkedin_mcp_server.linkedin import ext_actions as A
from linkedin_mcp_server.linkedin import ext_own_content as oc
from linkedin_mcp_server.tools import ext_own_content as t
ACT = "7123456789012345678"
CID = "7123456789012340001"
ME = "jane-doe-acme"
OWN_LINES = [
    "Jane Doe Profil",
    "Jane Doe",
    "Sales bei ACME",
    "1 Std.",
    "Alter Kommentar",
    "Gefällt mir",
    "Antworten",
]

LONG = (
    "Danke für den Beitrag, das deckt sich mit dem, was wir in der Praxis "
    "bei der Auswertung von Gefügebildern sehen, gerade bei feinen Körnern "
    "und schwierigen Kontrasten zwischen den Phasen."
)


# -- 1: truncated read-back --------------------------------------------------------


class TestTruncatedReadBack:
    probe = A._canon(LONG)[:150]

    @pytest.mark.parametrize("marker", ["…mehr", "… mehr", "…more", "… see more"])
    def test_cut_comment_counts(self, marker):
        body = LONG[:70] + marker
        comments = [{"key": "k1", "text": f"Jane Doe\n1 Std.\n{body}"}]
        assert A._matching_comment_keys(comments, self.probe) == {"k1"}

    def test_cut_too_short_does_not_count(self):
        comments = [{"key": "k1", "text": LONG[:30] + "…mehr"}]
        assert A._matching_comment_keys(comments, self.probe) == set()

    def test_cut_other_text_does_not_count(self):
        other = LONG[:45] + " ganz anders weiter …mehr"
        assert A._matching_comment_keys([{"key": "k", "text": other}], self.probe) == set()

    def test_no_marker_needs_full_probe(self):
        comments = [{"key": "k1", "text": LONG[:70]}]
        assert A._matching_comment_keys(comments, self.probe) == set()

    def test_still_needs_a_new_key(self):
        cut = [{"key": "old", "text": LONG[:70] + "…mehr"}]
        before = A._matching_comment_keys(cut, self.probe)
        after = A._matching_comment_keys(cut, self.probe)
        assert before == {"old"} and not (after - before)


# -- 2: lock release on a read text that does not hash --------------------------------


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv(outreach.LEDGER_ENV, str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv(outreach.NOTES_ENV, str(tmp_path / "notes.json"))
    return outreach.Ledger.default()


def _row(attempt, text, **extra):
    return {
        "attempt": attempt,
        "kind": "comment",
        "activity": ACT,
        "text_sha": outreach.text_sha(text),
        "status": "posted",
        "started_at": "2026-10-01T10:00:00+02:00",
        **extra,
    }


class _ReadPage:
    def __init__(self, land=True, post=True, card=None):
        self.url = "https://www.linkedin.com/feed/"
        self.land, self.post, self.card = land, post, card

    async def evaluate(self, js, arg=None, **_):
        if js is oc._POST_CARD_JS:
            return {"text": "Beitrag"} if self.post else None
        if js is oc._COMMENT_CARD_JS:
            return self.card
        raise AssertionError("unexpected script")


def _reader(page):
    obj = object.__new__(oc.ExtOwnContent)
    obj._session = type("S", (), {"page": page})()
    obj.visits = []

    async def goto(url):
        obj.visits.append(url)
        page.url = url if page.land else "https://www.linkedin.com/feed/"

    obj._goto = goto
    return obj


class TestRereadPermalink:
    def test_reads_via_comment_permalink(self):
        card = {"count": 1, "actor_href": f"/in/{ME}/", "lines": OWN_LINES, "menu_count": 1}
        r = _reader(_ReadPage(card=card))
        text = asyncio.run(r._reread_text(ACT, CID))
        assert r.visits == [oc.comment_permalink(ACT, CID)]
        assert text == "Alter Kommentar"

    def test_permalink_not_landed_is_none(self):
        r = _reader(_ReadPage(land=False, card={"count": 1, "lines": OWN_LINES}))
        assert asyncio.run(r._reread_text(ACT, CID)) is None

    def test_post_missing_is_none(self):
        r = _reader(_ReadPage(post=False, card={"count": 1, "lines": OWN_LINES}))
        assert asyncio.run(r._reread_text(ACT, CID)) is None


# -- 4: card text with an identical reply (real DOM) ----------------------------------

_CARD_HTML = f"""<html><body><main>
<div componentkey="replaceableComment_urn:li:comment:(activity:{ACT},{CID})">
  <a href="/in/{ME}/">Jane Doe</a>
  <p>Gleicher Text</p>
  <div componentkey="replaceableComment_urn:li:comment:(activity:{ACT},7123456789012340002)">
    <a href="/in/john-roe-acme/">John Roe</a>
    <p>Gleicher Text</p>
  </div>
</div>
</main></body></html>"""


@pytest.fixture
async def dom_page():
    from patchright.async_api import async_playwright

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(channel="chromium", headless=True)
        except Exception as exc:  # pragma: no cover - local only
            pytest.skip(f"chromium unavailable: {exc}")
        page = await browser.new_page()
        await page.set_content(_CARD_HTML)
        try:
            yield page
        finally:
            await browser.close()


@pytest.mark.browser_dom
async def test_identical_reply_does_not_erase_own_text(dom_page):
    card = await dom_page.evaluate(oc._COMMENT_CARD_JS, CID)
    assert card["count"] == 1
    assert "Gleicher Text" in card["lines"]
    assert "John Roe" not in card["lines"]
    # The live card is untouched by the read.
    assert (
        await dom_page.evaluate("() => document.querySelectorAll('[componentkey]').length")
        == 2
    )
