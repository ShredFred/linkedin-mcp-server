"""MiViA fork: delete/edit an own post or comment (2026-10-01).

No browser: the page is a fake that answers each page script by identity and
records every click, so "no click before the author check" is an assertion,
not a hope.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client, FastMCP

from linkedin_mcp_server import mivia_outreach as outreach
from linkedin_mcp_server.linkedin import mivia_own_content as oc

ACT = "7123456789012345678"
CID = "7123456789012340001"
POST = f"https://www.linkedin.com/feed/update/urn:li:activity:{ACT}/"
ME = "jessica-schneider-mivia"
OWN_LINES = [
    "Jessica Schneider Profil",
    "Jessica Schneider",
    "Sales bei MiViA",
    "1 Std.",
    "Alter Kommentar",
    "Gefällt mir",
    "Antworten",
]


# -- fake page -------------------------------------------------------------------


class _Tagged:
    def __init__(self, page, tag):
        self.page, self.tag = page, tag

    @property
    def first(self):
        return self

    async def count(self):
        return 0 if self.tag in self.page.missing else 1

    async def click(self):
        self.page.clicks.append(self.tag)
        if self.tag == self.page.raise_on:
            raise RuntimeError("boom")

    async def scroll_into_view_if_needed(self):
        return None

    async def evaluate(self, js, arg=None):
        self.page.typed = arg if self.page.type_ok else "verstümmelt"
        return True

    async def inner_text(self):
        return self.page.typed


class _Kbd:
    def __init__(self, page):
        self.page = page

    async def press(self, key):
        self.page.pressed.append(key)


class _Page:
    def __init__(self, **kw):
        self.url = "https://www.linkedin.com/feed/"
        self.me = kw.pop("me", ME)
        self.posts = list(kw.pop("posts", []))
        self.comments = list(kw.pop("comments", []))
        self.menu = kw.pop("menu", {"items": ["Beitrag löschen"], "count": 1})
        self.buttons = kw.pop("buttons", {})
        self.editor = kw.pop("editor", {"count": 1, "text": "Alter Kommentar"})
        self.page_text = kw.pop("page_text", "Dieser Beitrag ist nicht mehr verfügbar")
        self.missing = set(kw.pop("missing", ()))
        self.raise_on = kw.pop("raise_on", None)
        self.type_ok = kw.pop("type_ok", True)
        assert not kw
        self.clicks: list[str] = []
        self.pressed: list[str] = []
        self.typed = ""
        self.keyboard = _Kbd(self)

    def locator(self, css):
        return _Tagged(self, css.split('="')[1].rstrip('"]'))

    @staticmethod
    def _next(seq):
        return seq.pop(0) if len(seq) > 1 else (seq[0] if seq else None)

    async def evaluate(self, js, arg=None):
        if js is oc._POST_CARD_JS:
            return self._next(self.posts)
        if js is oc._COMMENT_CARD_JS:
            return self._next(self.comments)
        if js is oc._MENU_PICK_JS:
            return self.menu
        if js is oc._BUTTON_PICK_JS:
            return self.buttons.get(arg["tag"], {"count": 1, "disabled": False})
        if js is oc._EDITOR_PICK_JS:
            return self.editor
        if js is oc._PAGE_TEXT_JS:
            return self.page_text
        raise AssertionError("unexpected script")


def _content(page):
    obj = object.__new__(oc.MiviaOwnContent)
    obj._session = type("S", (), {"page": page})()

    async def goto(url):
        page.url = (
            f"https://www.linkedin.com/in/{page.me}/" if url == oc.ME_URL else url
        )

    async def no_wait(low, high):
        return None

    obj._goto = goto
    obj._wait = no_wait
    return obj


def _own_post(text="Alter Beitrag"):
    return {"actor_href": f"/in/{ME}/", "text": text, "menu_count": 1}


def _own_comment(lines=OWN_LINES):
    return {"count": 1, "actor_href": f"/in/{ME}/", "lines": lines, "menu_count": 1}


def _run(coro):
    return asyncio.run(coro)


# -- pure helpers ----------------------------------------------------------------


class TestHelpers:
    def test_parse_comment_ref(self):
        assert oc.parse_comment_ref(CID) == (None, CID)
        urn = f"urn:li:comment:(activity:{ACT},{CID})"
        assert oc.parse_comment_ref(urn) == (ACT, CID)
        assert oc.parse_comment_ref(urn.replace("(", "%28").replace(")", "%29")) == (
            ACT,
            CID,
        )
        assert oc.parse_comment_ref(f"urn:li:comment:(urn:li:ugcPost:{ACT},{CID})") == (
            ACT,
            CID,
        )
        with pytest.raises(ValueError):
            oc.parse_comment_ref("abc")

    def test_same_member_is_exact_and_decodes(self):
        assert oc.same_member("https://www.linkedin.com/in/J%C3%B6rg-x/", "jörg-x")
        assert not oc.same_member("/in/jessica-schneider-mivia-2/", ME)
        assert not oc.same_member("/company/mivia/", "mivia")
        assert not oc.same_member("/in/ACoAAA123/", ME)
        assert not oc.same_member(None, ME)
        assert not oc.same_member(f"/in/{ME}/", None)

    def test_text_matches_strips_only_the_marker(self):
        assert oc.text_matches("Neu  Text (bearbeitet)", "Neu Text")
        assert not oc.text_matches("Neu Text mehr", "Neu Text")
        assert not oc.text_matches(None, "x")

    def test_prefill_truncated_card(self):
        assert oc.prefill_matches("Langer Text bis zum Ende", "Langer Text … mehr")
        assert not oc.prefill_matches("Anderer Text", "Langer Text … mehr")
        assert oc.prefill_matches("Genau", "Genau")
        assert not oc.prefill_matches("Genau mehr", "Genau")
        assert not oc.prefill_matches("x", None)


# -- browser layer: author check and click discipline ------------------------------


class TestPostDelete:
    def test_foreign_post_refused_without_click(self):
        page = _Page(posts=[{"actor_href": "/in/someone-else/", "menu_count": 1}])
        out = _run(_content(page).delete(ACT, None, confirm=True))
        assert out["status"] == "not_own_post" and out["author"] == "someone-else"
        assert page.clicks == []

    def test_company_author_is_not_own(self):
        page = _Page(posts=[{"actor_href": "/company/mivia/", "menu_count": 1}])
        out = _run(_content(page).delete(ACT, None, confirm=True))
        assert out["status"] == "not_own_post"
        assert page.clicks == []

    def test_missing_author_refused(self):
        page = _Page(posts=[{"actor_href": None, "menu_count": 1}])
        out = _run(_content(page).delete(ACT, None, confirm=True))
        assert out["status"] == "author_unknown" and page.clicks == []

    def test_unresolved_identity_refused(self):
        page = _Page(me="me", posts=[_own_post()])
        out = _run(_content(page).delete(ACT, None, confirm=True))
        assert out["status"] == "own_identity_unknown" and page.clicks == []

    def test_post_not_found(self):
        page = _Page(posts=[None])
        assert _run(_content(page).delete(ACT, None, confirm=True))["status"] == (
            "post_not_found"
        )

    def test_no_or_many_menu_triggers(self):
        for n in (0, 2):
            page = _Page(posts=[{**_own_post(), "menu_count": n}])
            out = _run(_content(page).delete(ACT, None, confirm=True))
            assert out["status"] == "menu_unavailable" and page.clicks == []

    def test_dry_run_opens_menu_only(self):
        page = _Page(posts=[_own_post()])
        c = _content(page)
        out = _run(c.delete(ACT, None, confirm=False))
        assert out["status"] == "dry_run" and out["done"] is False
        assert page.clicks == ["post-menu"]
        assert page.pressed == ["Escape"]
        assert c.clicked is False

    @pytest.mark.parametrize(
        "menu,status",
        [
            ({"items": ["Beitrag speichern"], "count": 0}, "menu_item_missing"),
            ({"items": ["Löschen", "Löschen"], "count": 2}, "menu_item_ambiguous"),
        ],
    )
    def test_menu_entry_missing_is_a_status(self, menu, status):
        page = _Page(posts=[_own_post()], menu=menu)
        out = _run(_content(page).delete(ACT, None, confirm=True))
        assert out["status"] == status
        assert page.clicks == ["post-menu"]
        assert "Escape" in page.pressed

    def test_confirm_dialog_missing_no_final_click(self):
        page = _Page(posts=[_own_post()], buttons={"confirm": {"count": 0}})
        c = _content(page)
        out = _run(c.delete(ACT, None, confirm=True))
        assert out["status"] == "confirm_dialog_missing" and out["done"] is False
        assert "confirm" not in page.clicks
        assert c.clicked is False

    def test_disabled_confirm_not_clicked(self):
        page = _Page(
            posts=[_own_post()], buttons={"confirm": {"count": 1, "disabled": True}}
        )
        out = _run(_content(page).delete(ACT, None, confirm=True))
        assert out["status"] == "confirm_dialog_missing"
        assert "confirm" not in page.clicks

    def test_delete_verified_when_gone(self):
        page = _Page(posts=[_own_post(), None])
        c = _content(page)
        out = _run(c.delete(ACT, None, confirm=True))
        assert out["status"] == "verified" and out["done"] is True
        assert page.clicks == ["post-menu", "entry", "confirm"]
        assert c.clicked is True

    def test_delete_unverified_when_still_there(self):
        page = _Page(posts=[_own_post(), _own_post()])
        out = _run(_content(page).delete(ACT, None, confirm=True))
        assert out["status"] == "unverified" and out["done"] is True

    def test_empty_page_without_gone_signal_is_unverified(self):
        # A page that failed to load also shows no card: not proof of deletion.
        page = _Page(posts=[_own_post(), None], page_text="")
        out = _run(_content(page).delete(ACT, None, confirm=True))
        assert out["status"] == "unverified"


class TestPostEdit:
    def _page(self, **kw):
        kw.setdefault("posts", [_own_post(), _own_post("Neuer Beitrag (bearbeitet)")])
        kw.setdefault("menu", {"items": ["Beitrag bearbeiten"], "count": 1})
        kw.setdefault("editor", {"count": 1, "text": "Alter Beitrag"})
        return _Page(**kw)

    def test_edit_verified_on_exact_read_back(self):
        page = self._page()
        out = _run(_content(page).edit(ACT, None, "Neuer Beitrag", confirm=True))
        assert out["status"] == "verified"
        assert page.clicks == ["post-menu", "entry", "save"]

    def test_unchanged_text_clicks_nothing(self):
        page = self._page()
        out = _run(_content(page).edit(ACT, None, "Alter  Beitrag", confirm=True))
        assert out["status"] == "unchanged" and page.clicks == []

    def test_dry_run(self):
        page = self._page()
        out = _run(_content(page).edit(ACT, None, "Neu", confirm=False))
        assert out["status"] == "dry_run" and page.clicks == ["post-menu"]

    def test_prefill_mismatch_aborts_without_save(self):
        page = self._page(editor={"count": 1, "text": "Ganz anderer Text"})
        c = _content(page)
        out = _run(c.edit(ACT, None, "Neu", confirm=True))
        assert out["status"] == "editor_prefill_mismatch"
        assert "save" not in page.clicks and "cancel" in page.clicks
        assert c.clicked is False

    def test_no_editor(self):
        page = self._page(editor={"count": 0})
        out = _run(_content(page).edit(ACT, None, "Neu", confirm=True))
        assert out["status"] == "editor_missing" and "save" not in page.clicks

    def test_typed_mismatch_aborts(self):
        page = self._page(type_ok=False)
        out = _run(_content(page).edit(ACT, None, "Neu", confirm=True))
        assert out["status"] == "editor_mismatch" and "save" not in page.clicks

    def test_no_save_button(self):
        page = self._page(buttons={"save": {"count": 2}})
        out = _run(_content(page).edit(ACT, None, "Neu", confirm=True))
        assert out["status"] == "save_button_unavailable"
        assert "save" not in page.clicks

    def test_read_back_differs_is_unverified(self):
        page = self._page(posts=[_own_post(), _own_post("Neuer Beitrag und mehr")])
        out = _run(_content(page).edit(ACT, None, "Neuer Beitrag", confirm=True))
        assert out["status"] == "unverified" and out["done"] is True


class TestComment:
    def test_foreign_comment_refused_without_click(self):
        page = _Page(
            posts=[_own_post()],
            comments=[{**_own_comment(), "actor_href": "/in/fremd/"}],
        )
        out = _run(_content(page).delete(ACT, CID, confirm=True))
        assert out["status"] == "not_own_comment" and page.clicks == []

    def test_comment_on_foreign_post_is_allowed(self):
        page = _Page(
            posts=[{"actor_href": "/in/fremd/", "menu_count": 1}],
            comments=[_own_comment()],
            menu={"items": ["Löschen"], "count": 1},
        )
        out = _run(_content(page).delete(ACT, CID, confirm=False))
        assert out["status"] == "dry_run" and page.clicks == ["comment-menu"]

    @pytest.mark.parametrize(
        "count,status", [(0, "comment_not_found"), (2, "comment_ambiguous")]
    )
    def test_comment_lookup(self, count, status):
        page = _Page(posts=[_own_post()], comments=[{"count": count}])
        assert _run(_content(page).delete(ACT, CID, confirm=True))["status"] == status

    def test_delete_verified_only_when_post_loaded(self):
        page = _Page(
            posts=[_own_post(), _own_post()],
            comments=[_own_comment(), {"count": 0}],
        )
        assert (
            _run(_content(page).delete(ACT, CID, confirm=True))["status"] == "verified"
        )
        page = _Page(posts=[_own_post(), None], comments=[_own_comment(), {"count": 0}])
        assert (
            _run(_content(page).delete(ACT, CID, confirm=True))["status"]
            == "unverified"
        )

    def test_edit_comment_verified(self):
        new_lines = OWN_LINES[:4] + ["Neuer Kommentar", "Gefällt mir"]
        page = _Page(
            posts=[_own_post()],
            comments=[_own_comment(), _own_comment(new_lines)],
            menu={"items": ["Bearbeiten", "Löschen"], "count": 1},
        )
        out = _run(_content(page).edit(ACT, CID, "Neuer Kommentar", confirm=True))
        assert out["status"] == "verified"
        assert page.clicks == ["comment-menu", "entry", "save"]

    def test_exception_after_final_click_keeps_clicked(self):
        page = _Page(posts=[_own_post()], comments=[_own_comment()], raise_on="confirm")
        c = _content(page)
        with pytest.raises(RuntimeError):
            _run(c.delete(ACT, CID, confirm=True))
        assert c.clicked is True


# -- tool layer: ledger, pacer, dry run ----------------------------------------------


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv(outreach.LEDGER_ENV, str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv(outreach.NOTES_ENV, str(tmp_path / "notes.json"))
    return outreach.Ledger.default()


class _Reader:
    def __init__(self, result=None, clicked=False, raise_exc=False):
        self.result = result or {"status": "verified", "done": True, "verified": True}
        self.clicked = clicked
        self.raise_exc = raise_exc
        self.calls: list[tuple] = []

    async def delete(self, activity, comment, *, confirm):
        self.calls.append(("delete", activity, comment, confirm))
        if self.raise_exc:
            raise RuntimeError("boom")
        return dict(self.result) if confirm else {"status": "dry_run", "done": False}

    async def edit(self, activity, comment, new_text, *, confirm):
        self.calls.append(("edit", activity, comment, new_text, confirm))
        if self.raise_exc:
            raise RuntimeError("boom")
        return dict(self.result) if confirm else {"status": "dry_run", "done": False}


def _call(monkeypatch, reader, name, args):
    import linkedin_mcp_server.tools.mivia as m
    import linkedin_mcp_server.tools.mivia_own_content as t

    async def fake_run(ctx, tool, body):
        return await body(object())

    monkeypatch.setattr(m, "_run", fake_run)
    monkeypatch.setattr(t, "_reader", lambda ex: reader)
    mcp = FastMCP("t")
    m.register_mivia_tools(mcp)

    async def go():
        async with Client(mcp) as c:
            return (await c.call_tool(name, args)).structured_content

    return asyncio.run(go())


def _used(kind):
    return outreach.Pacer(outreach.Ledger.default()).state(kind)["today"]


def _attempt_rows():
    return list(outreach.Ledger.default().latest_by_attempt().values())


class TestTools:
    def test_registered_tagged_and_destructive(self, ledger, monkeypatch):
        import linkedin_mcp_server.tools.mivia as m

        mcp = FastMCP("t")
        m.register_mivia_tools(mcp)
        tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
        for name in (
            "delete_own_post",
            "edit_own_post",
            "delete_own_comment",
            "edit_own_comment",
        ):
            assert "mivia" in tools[name].tags
            props = tools[name].parameters["properties"]
            assert props["dry_run"]["default"] is True

    def test_dry_run_is_default_and_books_no_write(self, ledger, monkeypatch):
        reader = _Reader()
        out = _call(monkeypatch, reader, "delete_own_post", {"post_url": POST})
        assert out["status"] == "dry_run" and out["dry_run"] is True
        assert reader.calls == [("delete", ACT, None, False)]
        assert _used("post_delete") == 0
        assert not [r for r in _attempt_rows() if r.get("kind") == "post_delete"]

    def test_live_delete_books_once_and_blocks_repeat(self, ledger, monkeypatch):
        reader = _Reader()
        out = _call(
            monkeypatch, reader, "delete_own_post", {"post_url": POST, "dry_run": False}
        )
        assert out["status"] == "verified" and out["attempt"]
        assert _used("post_delete") == 1
        row = _attempt_rows()[-1]
        assert row["kind"] == "post_delete" and row["status"] == "verified"
        assert row["activity"] == ACT
        again = _call(
            monkeypatch, reader, "delete_own_post", {"post_url": POST, "dry_run": False}
        )
        assert again["status"] == "already_attempted"
        assert len(reader.calls) == 1

    def test_not_done_releases_budget_and_target(self, ledger, monkeypatch):
        reader = _Reader({"status": "not_own_post", "done": False})
        out = _call(
            monkeypatch, reader, "delete_own_post", {"post_url": POST, "dry_run": False}
        )
        assert out["status"] == "not_own_post"
        assert _attempt_rows()[-1]["status"] == "not_done"
        assert _used("post_delete") == 0
        out = _call(
            monkeypatch, reader, "delete_own_post", {"post_url": POST, "dry_run": False}
        )
        assert out["status"] == "not_own_post"

    @pytest.mark.parametrize("clicked,status", [(False, "not_done"), (True, "unknown")])
    def test_exception_maps_to_clicked_flag(self, ledger, monkeypatch, clicked, status):
        reader = _Reader(clicked=clicked, raise_exc=True)
        with pytest.raises(Exception):
            _call(
                monkeypatch,
                reader,
                "delete_own_comment",
                {"post_url": POST, "comment_id": CID, "dry_run": False},
            )
        assert _attempt_rows()[-1]["status"] == status
        assert _used("comment_delete") == (1 if clicked else 0)

    def test_pace_budget_checked_before_browser(self, ledger, monkeypatch):
        monkeypatch.setitem(outreach.PACE_BUDGETS, "post_edit", {"day": 0, "week": 0})
        reader = _Reader()
        out = _call(
            monkeypatch,
            reader,
            "edit_own_post",
            {"post_url": POST, "new_text": "Neu", "dry_run": False},
        )
        assert out["status"] == "pace_budget_spent" and reader.calls == []

    def test_budgets_are_conservative_and_counted_as_writes(self):
        for kind in ("post_delete", "post_edit", "comment_delete", "comment_edit"):
            assert outreach.PACE_BUDGETS[kind]["day"] <= 5
            assert kind in outreach.PACE_WRITE_KINDS

    @pytest.mark.parametrize(
        "name,args,status",
        [
            ("edit_own_post", {"post_url": POST, "new_text": "  "}, "invalid_text"),
            ("edit_own_post", {"post_url": POST, "new_text": "a\tb"}, "invalid_text"),
            (
                "edit_own_comment",
                {"post_url": POST, "comment_id": CID, "new_text": "x" * 1251},
                "invalid_text",
            ),
            ("delete_own_post", {"post_url": "https://example.com/"}, "invalid_post"),
            (
                "delete_own_comment",
                {"post_url": POST, "comment_id": "abc"},
                "invalid_comment",
            ),
            (
                "delete_own_comment",
                {
                    "post_url": POST,
                    "comment_id": f"urn:li:comment:(activity:7999999999999999999,{CID})",
                },
                "invalid_comment",
            ),
        ],
    )
    def test_browser_free_refusals(self, ledger, monkeypatch, name, args, status):
        reader = _Reader()
        assert _call(monkeypatch, reader, name, args)["status"] == status
        assert reader.calls == []

    def test_comment_edit_annotates_original_comment_row(self, ledger, monkeypatch):
        ledger.append(
            {
                "attempt": "orig",
                "kind": "comment",
                "activity": ACT,
                "text_sha": outreach.text_sha("Alter Kommentar"),
                "status": "posted",
                "started_at": "2026-10-01T10:00:00+02:00",
            }
        )
        reader = _Reader(
            {
                "status": "verified",
                "done": True,
                "verified": True,
                "old_text": "Alter Kommentar",
            }
        )
        out = _call(
            monkeypatch,
            reader,
            "edit_own_comment",
            {
                "post_url": POST,
                "comment_id": CID,
                "new_text": "Neuer Kommentar",
                "dry_run": False,
            },
        )
        assert out["status"] == "verified" and out["comment_row"] == "orig"
        assert "old_text" not in out
        orig = outreach.Ledger.default().latest_by_attempt()["orig"]
        assert orig["edited_by"] == out["attempt"]
        assert orig["edited_text_sha"] == outreach.text_sha("Neuer Kommentar")
        # The original text stays blocked for comment_on_post.
        assert orig["text_sha"] == outreach.text_sha("Alter Kommentar")
        edit_row = outreach.Ledger.default().latest_by_attempt()[out["attempt"]]
        assert edit_row["old_sha"] == outreach.text_sha("Alter Kommentar")
