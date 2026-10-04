"""R6 Mutationsprobe: Tests fuer Schutzstellen, deren Entfernen vorher keinen
Test rot machte. Jeder Test hier wurde gegen die mutierte Kopie rot gefahren."""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client, FastMCP

from linkedin_mcp_server import ext_outreach as outreach


@pytest.fixture(autouse=True)
def _ledger(tmp_path, monkeypatch):
    monkeypatch.setenv(outreach.LEDGER_ENV, str(tmp_path / "ledger.jsonl"))


def _call(register, name, args):
    mcp = FastMCP("t")
    register(mcp)

    async def go():
        async with Client(mcp) as c:
            return (await c.call_tool(name, args)).structured_content

    return asyncio.run(go())


def _msg_tools(mcp):
    from linkedin_mcp_server.tools.ext import register_ext_tools

    register_ext_tools(mcp)


# Mutation dup_vor_browser_msg: the ledger duplicate check in
# send_message_verified before the dry-run/browser step removed.
@pytest.mark.parametrize("confirm", [False, True])
def test_message_duplicate_refused_before_browser(confirm):
    text = "Guten Tag, kurze Frage zu Ihrem Labor."
    outreach.Ledger.default().append(
        {
            "attempt": "x",
            "kind": "message",
            "recipient": outreach.recipient_key("dieter"),
            "text_sha": outreach.text_sha(text),
            "status": "verified",
            "started_at": "2026-09-30T10:00:00+02:00",
        }
    )
    out = _call(
        _msg_tools,
        "send_message_verified",
        {"linkedin_username": "dieter", "message": text, "confirm_send": confirm},
    )
    assert out["status"] == "duplicate"


# Mutation clicked_post: the composer never sets clicked before the publish
# click, so an exception after it would be booked not_posted and the text
# released for a second post.
@pytest.mark.asyncio
async def test_post_composer_marks_clicked_before_publish_exception():
    import test_ext_post as tp

    class Boom(tp.FakePage):
        async def wait_for_selector(self, *a, **k):
            if '[data-ext-target="post"]' in self.clicked:
                raise RuntimeError("page died after the click")

    page = Boom(urn=None, body="Erste Zeile")
    c = tp.composer(page)
    try:
        await c.create_post("Erste Zeile", image_path=None, confirm_post=True)
    except RuntimeError:
        pass
    assert '[data-ext-target="post"]' in page.clicked
    assert c.clicked is True


@pytest.mark.asyncio
async def test_post_composer_dry_run_not_clicked():
    import test_ext_post as tp

    page = tp.FakePage(urn=None, body="Erste Zeile")
    c = tp.composer(page)
    out = await c.create_post("Erste Zeile", image_path=None, confirm_post=False)
    assert out["status"] == "dry_run"
    assert c.clicked is False
    assert '[data-ext-target="post"]' not in page.clicked


class _Field:
    """Generic fake locator/field for the InMail composer."""

    def __init__(self, on_click=None):
        self.value = ""
        self.on_click = on_click
        self.clicks = 0

    first = property(lambda self: self)
    last = property(lambda self: self)

    def locator(self, *_):
        return self

    def filter(self, **_):
        return self

    async def count(self):
        return 1

    async def click(self):
        self.clicks += 1
        if self.on_click:
            self.on_click()

    async def wait_for(self, **_):
        return None

    async def inner_text(self):
        return "Unterhaltung"

    async def fill(self, v):
        self.value = v

    async def input_value(self):
        return self.value

    async def is_disabled(self):
        return False


def _inmail_reader(monkeypatch, send_raises):
    from linkedin_mcp_server.linkedin import ext_inmail as mi

    def boom():
        if send_raises:
            raise RuntimeError("page died after the send click")

    send = _Field(on_click=boom)
    subject, body, other = _Field(), _Field(), _Field()

    async def first_match(scope, chain, *, last=False):
        return send if chain is mi._SN_SEND else subject

    monkeypatch.setattr(mi, "first_match", first_match)
    monkeypatch.setattr(mi, "credit_refusal", lambda c: None)
    monkeypatch.setattr(mi, "parse_degree", lambda t: 2)

    class Page(_Field):
        url = "https://www.linkedin.com/sales/inbox/1"

        def locator(self, css):
            return body if "textarea" in css else other

        async def evaluate(self, *_a, **_k):
            return "2nd"

    obj = object.__new__(mi.ExtInmail)
    page = Page()
    obj._session = type("S", (), {"page": page})()

    async def nothing(*_a, **_k):
        return None

    obj._goto = nothing
    obj._wait = nothing
    obj._close_sn_dialog = nothing
    return obj, send


# Mutation clicked_inmail: clicked never set before the Sales Navigator send
# click; the tool then books an exception after the click as not_sent and a
# second InMail (and credit) becomes possible.
def test_inmail_marks_clicked_before_send_click(monkeypatch):
    obj, send = _inmail_reader(monkeypatch, send_raises=True)
    with pytest.raises(RuntimeError):
        asyncio.run(obj.inmail({"sales_url": "x"}, "Betreff", "Text", confirm=True))
    assert send.clicks == 1
    assert obj.clicked is True


def test_inmail_dry_run_never_clicks_send(monkeypatch):
    obj, send = _inmail_reader(monkeypatch, send_raises=True)
    out = asyncio.run(obj.inmail({"sales_url": "x"}, "Betreff", "Text", confirm=False))
    assert out["status"] == "dry_run"
    assert send.clicks == 0
    assert obj.clicked is False


# Mutation trocken_kommentar: the dry-run return in ExtActions._comment_typed
# removed -- comment_on_post without confirm would click the submit button.
def test_comment_dry_run_never_clicks_submit():
    import test_ext_write_guards as wg

    submits: list[str] = []

    class Page(wg._Page):
        def locator(self, selector):
            loc = wg._Locator(self)
            if "data-ext-submit" in selector:

                async def click():
                    submits.append(selector)

                loc.click = click
            return loc

    page = Page(before=[], after=[])
    actions = wg._comment_actions(page)
    out = asyncio.run(actions.comment("1", "Starker Beitrag", confirm=False))
    assert out["status"] == "dry_run"
    assert out["posted"] is False
    assert submits == []
    assert actions.comment_submitted is False
