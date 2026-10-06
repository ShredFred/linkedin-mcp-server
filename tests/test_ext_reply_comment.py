"""Reply to a comment by its URN (2026-10-05).

Two layers: the tool (ledger, pacer, dedup, refusals) with a stub actions
object, and the page walk of ``ExtActions.reply`` against a real chromium DOM
modelled on the measured comment cards (componentkey
``replaceableComment_urn:li:comment:(activity:A,C)``, replies nested inside
their parent card). The reply controls themselves are not measured; the DOM
here is the assumption the code documents, and every miss must end without a
post-level comment.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server import ext_outreach as outreach
from linkedin_mcp_server.core.exceptions import OffSiteNavigationError
from linkedin_mcp_server.linkedin import ext_actions as ea
from test_ext_stage2_haertung import (  # noqa: F401  (autouse fixture)
    A,
    B,
    POST,
    _call,
    _isolated,
)

CID = "7123456789012340001"
REPLY_CID = "7123456789012340002"
URN = f"urn:li:comment:(activity:{A},{CID})"


# -- tool layer -------------------------------------------------------------------


class _Stub:
    def __init__(self, result: dict[str, Any] | None = None, boom: Any = None):
        self.result = result or {"status": "posted", "posted": True}
        self.boom = boom
        self.replies: list[tuple] = []
        self.comments: list[tuple] = []
        self.comment_submitted = False

    async def reply(self, activity_id, comment_id, text, confirm):
        self.replies.append((activity_id, comment_id, text, confirm))
        if self.boom:
            raise self.boom
        return self.result

    async def comment(self, activity_id, text, confirm):
        self.comments.append((activity_id, text, confirm))
        return {"status": "posted", "posted": True}


def _reply(monkeypatch, actions, reply_to=URN, text="Danke dafuer", confirm=True):
    return _call(
        "comment_on_post",
        {"post_url": POST, "text": text, "confirm": confirm, "reply_to": reply_to},
        monkeypatch,
        actions=actions,
    )


def _rows():
    return outreach.Ledger.default().rows()


def test_dry_run_books_nothing_and_never_comments(monkeypatch):
    stub = _Stub({"status": "dry_run", "posted": False})
    out = _reply(monkeypatch, stub, confirm=False)
    assert out["status"] == "dry_run"
    assert stub.replies == [(A, CID, "Danke dafuer", False)]
    assert stub.comments == []
    assert not [r for r in _rows() if r.get("kind") == "comment"]


def test_confirmed_reply_books_row_with_target(monkeypatch):
    stub = _Stub()
    out = _reply(monkeypatch, stub, reply_to=CID)
    assert out["status"] == "posted" and stub.comments == []
    attempt = [r for r in _rows() if r.get("kind") == "comment"][0]
    assert attempt["reply_to"] == CID and attempt["activity"] == A
    assert (
        outreach.Ledger.default().latest_by_attempt()[attempt["attempt"]]["status"]
        == "posted"
    )


@pytest.mark.parametrize(
    "ref", ["kein-kommentar", f"urn:li:comment:(activity:{B},{CID})"]
)
def test_bad_or_foreign_target_refused(monkeypatch, ref):
    stub = _Stub()
    out = _reply(monkeypatch, stub, reply_to=ref)
    assert out["status"] == "invalid_reply_target" and out["posted"] is False
    assert stub.replies == stub.comments == []


def test_retry_same_target_is_refused(monkeypatch):
    stub = _Stub()
    assert _reply(monkeypatch, stub)["status"] == "posted"
    again = _reply(monkeypatch, stub, text="Anderer Text")
    assert again["status"] == "already_replied"
    same = _reply(monkeypatch, stub, reply_to=REPLY_CID)
    assert same["status"] == "duplicate_text"
    assert len(stub.replies) == 1


def test_reply_and_top_level_comment_do_not_block_each_other(monkeypatch):
    stub = _Stub()
    assert _reply(monkeypatch, stub)["status"] == "posted"
    out = _call(
        "comment_on_post",
        {"post_url": POST, "text": "Eigener Kommentar", "confirm": True},
        monkeypatch,
        actions=stub,
    )
    assert out["status"] == "posted" and len(stub.comments) == 1
    assert (
        _reply(monkeypatch, stub, reply_to=REPLY_CID, text="Zweite")["status"]
        == "posted"
    )


def test_unverified_keeps_blocking_target(monkeypatch):
    stub = _Stub({"status": "unverified", "posted": True})
    assert _reply(monkeypatch, stub)["status"] == "unverified"
    assert _reply(monkeypatch, stub, text="Nochmal")["status"] == "already_replied"


def test_target_miss_releases_for_retry(monkeypatch):
    stub = _Stub({"status": "reply_target_not_found", "posted": False})
    assert _reply(monkeypatch, stub)["status"] == "reply_target_not_found"
    stub.result = {"status": "posted", "posted": True}
    assert _reply(monkeypatch, stub)["status"] == "posted"


def test_pace_spent_before_browser(monkeypatch):
    monkeypatch.setitem(outreach.PACE_BUDGETS, "comment", {"day": 0, "week": 0})
    stub = _Stub()
    assert _reply(monkeypatch, stub)["status"] == "pace_budget_spent"
    assert stub.replies == []


def test_offsite_navigation_before_click_is_not_posted(monkeypatch):
    stub = _Stub(
        boom=OffSiteNavigationError(
            "https://www.linkedin.com/", "https://evil.example/"
        )
    )
    with pytest.raises(Exception):
        _reply(monkeypatch, stub)
    latest = list(outreach.Ledger.default().latest_by_attempt().values())
    assert [r["status"] for r in latest if r.get("kind") == "comment"] == ["not_posted"]
    assert stub.comments == []


def test_offsite_navigation_goes_through_navigator():
    class Nav:
        def __init__(self):
            self.urls = []

        async def _navigate_to_page(self, url):
            self.urls.append(url)
            raise OffSiteNavigationError(url, "https://evil.example/")

    class Page:
        def locator(self, css):
            raise AssertionError("no element touched after an off-site redirect")

    actions = object.__new__(ea.ExtActions)
    nav = Nav()
    actions._navigator = nav
    actions._session = type("S", (), {"page": Page()})()
    with pytest.raises(OffSiteNavigationError):
        asyncio.run(actions.reply(A, CID, "Text", True))
    assert nav.urls == [ea.reply_permalink(A, CID)]
    assert actions.comment_submitted is False


# -- page layer (real DOM) --------------------------------------------------------


def _card(cid: str, text: str, inner: str = "", reply: bool = True) -> str:
    button = '<button onclick="openReply(this)">Antworten</button>' if reply else ""
    return (
        f'<article componentkey="replaceableComment_urn:li:comment:(activity:{A},{cid})">'
        f"<a href='/in/person-{cid[-1]}/'>Person</a><p>{text}</p>"
        f"<button>Gefällt mir</button>{button}"
        f'<div class="replies">{inner}</div></article>'
    )


SCRIPT = (
    """
<script>
let next = 900;
const mode = () => document.body.dataset.mode || 'ok';
function rootOf(el) {
  let root = el.closest('article');
  for (let p = root.parentElement; p; p = p.parentElement)
    if (p.tagName === 'ARTICLE') root = p;
  return root;
}
function makeBox() {
  const box = document.createElement('form');
  box.className = 'comments-comment-box--reply reply-box';
  const prefill = mode() === 'mention' ? '<a class="mention">Person Eins</a>&nbsp;' : '';
  box.innerHTML = '<div class="ql-editor" contenteditable="true" role="textbox">' + prefill + '</div>' +
                  '<button type="button" onclick="submitReply(this)">Antworten</button>';
  return box;
}
function openReply(btn) {
  const root = rootOf(btn);
  const m = mode();
  if (m === 'no_editor' || document.querySelector('.reply-box[data-for="' + root.getAttribute('componentkey') + '"]')) return;
  const place = () => {
    const box = makeBox();
    box.dataset.for = root.getAttribute('componentkey');
    if (m === 'sibling' || m === 'late_sibling') root.after(box);
    else if (m === 'far') document.getElementById('far').appendChild(box);
    else root.querySelector('.replies').appendChild(box);
    if (m === 'two') { const b2 = makeBox(); b2.dataset.for = box.dataset.for; root.appendChild(b2); }
  };
  if (m === 'late' || m === 'late_sibling') setTimeout(place, 1200); else place();
}
function submitReply(btn) {
  const box = btn.closest('.reply-box');
  const root = document.querySelector('[componentkey="' + box.dataset.for + '"]');
  const editor = box.querySelector('[contenteditable]');
  document.body.dataset.replied = editor.innerText;
  if (mode() === 'silent') return;
  const text = mode() === 'mismatch' ? 'Etwas ganz anderes' : editor.innerText;
  const a = document.createElement('article');
  a.setAttribute('componentkey',
    'replaceableComment_urn:li:comment:(activity:"""
    + A
    + """,' + (next++) + ')');
  a.innerHTML = '<p></p>';
  a.querySelector('p').innerText = text;
  root.querySelector('.replies').appendChild(a);
}
function loadMore(btn) {
  const clicks = Number(document.body.dataset.clicks || 0) + 1;
  document.body.dataset.clicks = clicks;
  const late = document.getElementById('late');
  if (late && clicks >= Number(late.dataset.need)) {
    document.getElementById('list').insertAdjacentHTML('beforeend', late.innerHTML);
  }
}
</script>
"""
)


def _html(comments: str, extra: str = "") -> str:
    return (
        "<!DOCTYPE html><html><body><main>"
        '<div componentkey="update-card-focus-1">'
        '<div role="textbox" contenteditable="true" '
        'aria-label="Texteditor zum Erstellen von Kommentaren" id="post-editor"></div>'
        "<button onclick=\"document.body.dataset.post='1'\">Kommentieren</button>"
        f'<div id="list">{comments}</div>{extra}</div></main>{SCRIPT}</body></html>'
    )


THREAD = _card(CID, "Erster Kommentar", _card(REPLY_CID, "Eine Antwort"))


@pytest.fixture
def dom():
    async def make():
        p = await async_playwright().start()
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            page = await browser.new_page()
        except Exception as exc:  # browser binary missing
            await p.stop()
            pytest.skip(f"chromium unavailable: {exc}")
        return p, browser, page

    loop = asyncio.new_event_loop()
    p, browser, page = loop.run_until_complete(make())
    yield loop, page
    loop.run_until_complete(browser.close())
    loop.run_until_complete(p.stop())
    loop.close()


async def _no_delay(*_a, **_k):
    return None


def _run(dom, html, comment_id=CID, confirm=True, mode="ok"):
    loop, page = dom
    loop.run_until_complete(page.set_content(html))
    loop.run_until_complete(
        page.evaluate("(m) => { document.body.dataset.mode = m; }", mode)
    )
    actions = object.__new__(ea.ExtActions)
    actions._session = type("S", (), {"page": page, "delay": staticmethod(_no_delay)})()
    urls: list[str] = []

    async def goto(url):
        urls.append(url)

    actions._goto = goto
    out = loop.run_until_complete(
        actions.reply(A, comment_id, "Danke, sehr hilfreich", confirm)
    )
    state = loop.run_until_complete(
        page.evaluate(
            "() => ({post: document.body.dataset.post || null,"
            " replied: document.body.dataset.replied || null,"
            " postEditor: document.getElementById('post-editor').innerText})"
        )
    )
    assert urls == [ea.reply_permalink(A, comment_id)]
    # Never a post-level comment when a reply target is given.
    assert state["post"] is None and state["postEditor"] == ""
    return out, state, actions


pytestmark_dom = pytest.mark.browser_dom


@pytestmark_dom
def test_dom_reply_read_back(dom):
    out, state, actions = _run(dom, _html(THREAD))
    assert out["status"] == "posted" and out["verified"] is True
    assert out["reply_to_reply"] is False and out["thread_root_id"] == CID
    assert state["replied"] == "Danke, sehr hilfreich"
    assert actions.comment_submitted is True


@pytestmark_dom
def test_dom_reply_to_reply_targets_parent_thread(dom):
    out, state, _ = _run(dom, _html(THREAD), comment_id=REPLY_CID)
    assert out["status"] == "posted"
    assert out["reply_to_reply"] is True and out["thread_root_id"] == CID


@pytestmark_dom
def test_dom_dry_run_clears_and_clicks_nothing(dom):
    out, state, actions = _run(dom, _html(THREAD), confirm=False)
    assert out["status"] == "dry_run" and out["posted"] is False
    assert state["replied"] is None and actions.comment_submitted is False


@pytestmark_dom
def test_dom_read_back_mismatch_is_unverified(dom):
    out, state, _ = _run(dom, _html(THREAD), mode="mismatch")
    assert out["status"] == "unverified" and out["posted"] is True


@pytestmark_dom
def test_dom_silent_submit_is_unverified(dom):
    out, _, _ = _run(dom, _html(THREAD), mode="silent")
    assert out["status"] == "unverified" and out["verified"] is False


@pytestmark_dom
def test_dom_older_reply_with_same_text_does_not_count(dom):
    old = _card(CID, "Erster", _card(REPLY_CID, "Danke, sehr hilfreich"))
    out, _, _ = _run(dom, _html(old), mode="silent")
    assert out["status"] == "unverified"


@pytestmark_dom
def test_dom_target_not_found(dom):
    out, state, _ = _run(dom, _html(THREAD), comment_id="7123456789012349999")
    assert out["status"] == "reply_target_not_found" and out["posted"] is False
    assert state["replied"] is None


@pytestmark_dom
def test_dom_target_of_other_post_not_found(dom):
    foreign = THREAD.replace(f"activity:{A}", f"activity:{B}")
    out, _, _ = _run(dom, _html(foreign))
    assert out["status"] == "reply_target_not_found"


@pytestmark_dom
def test_dom_ambiguous_target(dom):
    out, state, _ = _run(dom, _html(THREAD + _card(CID, "Doppelt")))
    assert out["status"] == "reply_target_ambiguous" and out["count"] == 2
    assert state["replied"] is None


@pytestmark_dom
def test_dom_reply_button_missing(dom):
    # The nested reply keeps its button; the target's own is gone and the
    # nested one must not stand in for it.
    html = _html(_card(CID, "Ohne Knopf", _card(REPLY_CID, "Antwort"), reply=False))
    out, state, _ = _run(dom, html)
    assert out["status"] == "reply_button_missing"
    assert state["replied"] is None


@pytestmark_dom
def test_dom_reply_editor_missing(dom):
    out, state, _ = _run(dom, _html(THREAD), mode="no_editor")
    assert out["status"] == "reply_editor_missing" and out["posted"] is False
    assert state["replied"] is None


@pytestmark_dom
def test_dom_prefilled_mention_is_kept_and_text_appended(dom):
    out, state, _ = _run(dom, _html(THREAD), mode="mention")
    assert out["status"] == "posted"
    assert " ".join(state["replied"].split()) == "Person Eins Danke, sehr hilfreich"


@pytestmark_dom
def test_dom_dry_run_with_mention_types_nothing(dom):
    loop, page = dom
    out, state, _ = _run(dom, _html(THREAD), mode="mention", confirm=False)
    assert out["status"] == "dry_run" and out["prefill"] == "Person Eins"
    assert out["expected_text"] == "Person Eins Danke, sehr hilfreich"
    text = loop.run_until_complete(
        page.evaluate(
            "() => document.querySelector('.reply-box [contenteditable]').innerText"
        )
    )
    assert "Danke" not in text and state["replied"] is None


@pytestmark_dom
def test_dom_editor_as_sibling_after_comment(dom):
    out, state, _ = _run(dom, _html(THREAD), mode="sibling")
    assert out["status"] == "posted" and out["editor_placement"] == "sibling"
    assert state["replied"] == "Danke, sehr hilfreich"


@pytestmark_dom
def test_dom_editor_renders_late_is_polled(dom):
    out, _, _ = _run(dom, _html(THREAD), mode="late")
    assert out["status"] == "posted"


@pytestmark_dom
def test_dom_late_sibling_dry_run(dom):
    out, state, _ = _run(dom, _html(THREAD), mode="late_sibling", confirm=False)
    assert out["status"] == "dry_run" and state["replied"] is None


@pytestmark_dom
def test_dom_two_editors_ambiguous(dom):
    out, state, _ = _run(dom, _html(THREAD), mode="two")
    assert out["status"] == "reply_editor_ambiguous" and out["count"] == 2
    assert state["replied"] is None


@pytestmark_dom
def test_dom_editor_beyond_next_thread_not_used(dom):
    # The box lands after another thread: it is not ours -> missing, and
    # never the post-level box either.
    other = _card("7123456789012340042", "Anderer Thread")
    html = _html(THREAD + other, '<div id="far"></div>')
    out, state, _ = _run(dom, html, mode="far")
    assert out["status"] == "reply_editor_missing"
    assert state["replied"] is None


@pytestmark_dom
def test_dom_preexisting_box_of_other_thread_and_bottom_post_box_ignored(dom):
    other = _card("7123456789012340042", "Anderer Thread")
    stale = (
        '<form class="reply-box"><div contenteditable="true" role="textbox"></div>'
        "<button>Antworten</button></form>"
    )
    bottom = '<div contenteditable="true" role="textbox" id="bottom-box"></div>'
    out, state, _ = _run(dom, _html(THREAD + other + stale, bottom), mode="sibling")
    assert out["status"] == "posted" and out["editor_placement"] == "sibling"


@pytestmark_dom
def test_dom_no_sibling_and_only_bottom_post_box_is_missing(dom):
    bottom = '<div contenteditable="true" role="textbox" id="bottom-box"></div>'
    out, state, _ = _run(dom, _html(THREAD, bottom), mode="no_editor")
    assert out["status"] == "reply_editor_missing"
    assert state["replied"] is None


@pytestmark_dom
def test_dom_english_ui_labels(dom):
    html = _html(THREAD).replace(">Antworten<", ">Reply<")
    html = html.replace(
        '\'<button type="button" onclick="submitReply(this)">Antworten</button>\'',
        '\'<button type="button" onclick="submitReply(this)">Reply</button>\'',
    )
    out, state, _ = _run(dom, html)
    assert out["status"] == "posted"


@pytestmark_dom
def test_dom_reply_button_scrolled_into_view(dom):
    spacer = '<div style="height:5000px"></div>'
    out, _, _ = _run(dom, _html(spacer + THREAD))
    assert out["status"] == "posted"


@pytestmark_dom
def test_dom_collapsed_thread_expanded(dom):
    more = "<button onclick='loadMore(this)'>Weitere Kommentare laden</button>"
    late = f'<template id="late" data-need="2">{_card(CID, "Spaet geladen")}</template>'
    out, _, _ = _run(dom, _html("", more + late))
    assert out["status"] == "posted"


@pytestmark_dom
def test_dom_collapsed_thread_cap(dom):
    loop, page = dom
    more = "<button onclick='loadMore(this)'>Vorherige Antworten anzeigen</button>"
    out, state, _ = _run(dom, _html(THREAD.replace(CID, "7123456789012340009"), more))
    assert out["status"] == "reply_thread_collapsed"
    assert out["expansions"] == ea.REPLY_MAX_EXPANSIONS
    assert loop.run_until_complete(
        page.evaluate("() => document.body.dataset.clicks")
    ) == (str(ea.REPLY_MAX_EXPANSIONS))
    assert state["replied"] is None
