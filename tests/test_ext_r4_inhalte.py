"""fork R4 (2026-10-01): strong deletion evidence and no leftover overlays.

Standalone (tests is no package): the own-content fake page is loaded from its
test module by path.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from linkedin_mcp_server.linkedin import ext_actions as ma
from linkedin_mcp_server.linkedin import ext_inmail as mi
from linkedin_mcp_server.linkedin import ext_own_content as oc
from linkedin_mcp_server.tools import ext_own_content as tool

_spec = importlib.util.spec_from_file_location(
    "_r4_own_content_fakes", Path(__file__).with_name("test_ext_own_content.py")
)
fx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fx)

ACT, CID = fx.ACT, fx.CID
ACT_URL = f"https://www.linkedin.com/feed/update/urn:li:activity:{ACT}/"


def _run(coro):
    return asyncio.run(coro)


def _session(page):
    return type("S", (), {"page": page})()


# -- (1) deletion evidence -------------------------------------------------------


class TestPostGoneEvidence:
    def test_feed_redirect(self):
        assert oc.post_gone_evidence(ACT, "https://www.linkedin.com/feed/", {})

    def test_short_error_page_on_activity_url(self):
        state = {"text": "Dieser Beitrag ist nicht mehr verfügbar", "cards": 0}
        assert oc.post_gone_evidence(ACT, ACT_URL, state)

    def test_gone_text_in_a_feed_with_cards_is_no_evidence(self):
        state = {"text": "Inhalt nicht verfügbar", "cards": 3}
        assert not oc.post_gone_evidence(ACT, ACT_URL, state)

    def test_gone_text_on_a_long_page_is_no_evidence(self):
        state = {"text": "nicht verfügbar " + "x" * 3000, "cards": 0}
        assert not oc.post_gone_evidence(ACT, ACT_URL, state)

    def test_other_page_is_no_evidence(self):
        state = {"text": "nicht verfügbar", "cards": 0}
        assert not oc.post_gone_evidence(
            ACT, "https://www.linkedin.com/checkpoint/challenge", state
        )
        assert not oc.post_gone_evidence(ACT, "https://www.linkedin.com/login", state)

    def test_delete_on_feed_with_gone_text_stays_unverified(self):
        page = fx._Page(posts=[fx._own_post(), None], feed_cards=2)
        out = _run(fx._content(page).delete(ACT, None, confirm=True))
        assert out["status"] == "unverified" and out["done"] is True


class TestCommentPermalink:
    def test_permalink_shape(self):
        url = oc.comment_permalink(ACT, CID)
        assert url.startswith(ACT_URL + "?commentUrn=urn%3Ali%3Acomment%3A")
        assert CID in url

    def _page(self):
        return fx._Page(
            posts=[fx._own_post()],
            comments=[fx._own_comment(), {"count": 0, "total": 3}],
            menu={"items": ["Löschen"], "count": 1},
        )

    def test_readback_goes_through_the_permalink(self):
        page = self._page()
        c = fx._content(page)
        goto = c._goto
        visits = []

        async def rec(url):
            visits.append(url)
            await goto(url)

        c._goto = rec
        out = _run(c.delete(ACT, CID, confirm=True))
        assert out["status"] == "verified"
        assert visits[-1] == oc.comment_permalink(ACT, CID)

    def test_permalink_redirect_is_unverified(self):
        page = self._page()
        c = fx._content(page)
        goto = c._goto

        async def rec(url):
            await goto(url)
            if "commentUrn" in url:
                page.url = "https://www.linkedin.com/feed/"

        c._goto = rec
        assert _run(c.delete(ACT, CID, confirm=True))["status"] == "unverified"


def test_unverified_blocks_a_repeat():
    # "unverified" means the click happened: it must never read as "not done".
    assert "unverified" in tool._REPEAT_BLOCKING


# -- (2) no leftover overlays ------------------------------------------------------


class TestOwnContentCleanup:
    def test_delete_exception_before_confirm_cancels(self):
        page = fx._Page(posts=[fx._own_post()], raise_on="entry")
        c = fx._content(page)
        with pytest.raises(RuntimeError):
            _run(c.delete(ACT, None, confirm=True))
        assert c.clicked is False
        assert "cancel" in page.clicks

    def test_delete_exception_on_final_click_leaves_page_alone(self):
        page = fx._Page(posts=[fx._own_post()], raise_on="confirm")
        c = fx._content(page)
        with pytest.raises(RuntimeError):
            _run(c.delete(ACT, None, confirm=True))
        assert c.clicked is True
        assert "cancel" not in page.clicks

    def test_edit_exception_in_editor_aborts(self):
        page = fx._Page(
            posts=[fx._own_post()],
            menu={"items": ["Beitrag bearbeiten"], "count": 1},
            editor={"count": 1, "text": "Alter Beitrag"},
            raise_on="entry",
        )
        c = fx._content(page)
        with pytest.raises(RuntimeError):
            _run(c.edit(ACT, None, "Neuer Beitrag", confirm=True))
        assert c.clicked is False
        assert "cancel" in page.clicks


class _NoLoc:
    @property
    def last(self):
        return self

    @property
    def first(self):
        return self

    async def count(self):
        return 0


class TestInmailCleanup:
    def _reader(self, fail_after_click: bool):
        obj = object.__new__(mi.ExtInmail)
        calls: list[str] = []
        page = type("P", (), {"locator": lambda s, c: _NoLoc()})()
        obj._session = _session(page)

        async def boom(*a, **k):
            obj.clicked = fail_after_click
            raise RuntimeError("boom")

        async def close(dialog):
            calls.append("close")

        async def leave():
            calls.append("leave")

        obj._inmail = boom
        obj._edit = boom
        obj._close_sn_dialog = close
        obj._leave_edit_form = leave
        return obj, calls

    def test_inmail_exception_before_send_closes_composer(self):
        obj, calls = self._reader(False)
        with pytest.raises(RuntimeError):
            _run(obj.inmail({"sales_url": "x"}, "s", "b", confirm=True))
        assert calls == ["close"]

    def test_inmail_exception_after_send_keeps_dialog(self):
        obj, calls = self._reader(True)
        with pytest.raises(RuntimeError):
            _run(obj.inmail({"sales_url": "x"}, "s", "b", confirm=True))
        assert calls == []

    def test_edit_exception_before_save_leaves_form(self):
        obj, calls = self._reader(False)
        with pytest.raises(RuntimeError):
            _run(obj.edit("u", {"index": 0}, "neu", confirm=True))
        assert calls == ["leave"]

    def test_leave_edit_form_falls_back_to_escape(self):
        obj = object.__new__(mi.ExtInmail)
        pressed: list[str] = []

        class _Kbd:
            async def press(self, key):
                pressed.append(key)

        page = type("P", (), {"locator": lambda s, c: _NoLoc(), "keyboard": _Kbd()})()
        obj._session = _session(page)
        _run(obj._leave_edit_form())
        assert pressed == ["Escape"]


class TestCommentCleanup:
    def _actions(self, submitted: bool):
        obj = object.__new__(ma.ExtActions)
        cleared: list[bool] = []

        class _Editor:
            @property
            def first(self):
                return self

            async def count(self):
                return 1

        page = type("P", (), {"locator": lambda s, c: _Editor()})()
        obj._session = _session(page)

        async def goto(url):
            return None

        async def typed(ed, text, confirm):
            obj.comment_submitted = submitted
            raise RuntimeError("boom")

        async def clear(ed):
            cleared.append(True)

        obj._goto = goto
        obj._comment_typed = typed
        obj._clear = clear
        return obj, cleared

    def test_exception_before_submit_clears_editor(self):
        obj, cleared = self._actions(False)
        with pytest.raises(RuntimeError):
            _run(obj.comment(ACT, "Text", confirm=True))
        assert cleared == [True]

    def test_exception_after_submit_keeps_editor(self):
        obj, cleared = self._actions(True)
        with pytest.raises(RuntimeError):
            _run(obj.comment(ACT, "Text", confirm=True))
        assert cleared == []
