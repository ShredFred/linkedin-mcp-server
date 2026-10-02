"""repost_post (2026-10-02): menu classification, the fail-closed browser walk
and the ledger around it. No browser, no network."""

from __future__ import annotations

import asyncio

import pytest

from linkedin_mcp_server import mivia_outreach as outreach
from linkedin_mcp_server.linkedin import mivia_repost as rp

ACT = "7300000000000000001"
INSTANT_DE = "Reposten Beitrag sofort im Netzwerk teilen"
THOUGHTS_DE = "Mit Ihren Gedanken reposten Neuen Beitrag mit diesem Beitrag erstellen"
UNDO_DE = "Repost rückgängig machen"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(outreach.LEDGER_ENV, str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv(outreach.NOTES_ENV, str(tmp_path / "notes.json"))


# -- classification -------------------------------------------------------------


@pytest.mark.parametrize(
    "text,want",
    [
        (INSTANT_DE, "instant"),
        ("Repost Instantly bring this post to others' feeds", "instant"),
        (THOUGHTS_DE, "thoughts"),
        ("Repost with your thoughts", "thoughts"),
        (UNDO_DE, "undo"),
        ("Undo repost", "undo"),
        # measured 2026-10-02 (de)
        ("Sofort teilen", "instant"),
        ("Mit Kommentar teilen", "thoughts"),
        ("Link kopieren", None),
        ("", None),
        (None, None),
        (42, None),
    ],
)
def test_classify(text, want):
    assert rp.classify_menu_entry(text) == want


def test_pick_needs_exactly_one():
    two = [{"index": 0, "text": INSTANT_DE}, {"index": 1, "text": "Reposten"}]
    assert rp.pick_entry(two, "instant") == (None, False)
    entry, has_undo = rp.pick_entry(
        [{"index": 0, "text": UNDO_DE}, {"index": 1, "text": THOUGHTS_DE}], "undo"
    )
    assert entry["index"] == 0 and has_undo


@pytest.mark.parametrize("items", [None, "x", [None, 3, "a"], [{"text": None}]])
def test_pick_survives_junk(items):
    assert rp.pick_entry(items, "instant") == (None, False)


# -- browser walk with a fake page -------------------------------------------------


class _Locator:
    def __init__(self, page, sel):
        self.page, self.sel = page, sel

    @property
    def first(self):
        return self

    async def click(self):
        self.page.clicks.append(self.sel)


class _Keyboard:
    def __init__(self, page):
        self.page = page

    async def press(self, key):
        self.page.keys.append(key)


class _Page:
    def __init__(self, buttons=1, menus=()):
        self.buttons = buttons
        self.menus = list(menus)
        self.clicks: list[str] = []
        self.keys: list[str] = []
        self.keyboard = _Keyboard(self)

    async def evaluate(self, js, *args):
        if js is rp._MARK_REPOST_BUTTON_JS:
            return {"count": self.buttons}
        if js is rp._MARK_PRESENT_JS:
            return True
        if js is rp._BODY_LINES_JS:
            return ["button: Reposten"] if not self.menus else []
        if js is rp._MENU_ITEMS_JS:
            texts = self.menus.pop(0) if self.menus else []
            return [{"index": i, "text": t} for i, t in enumerate(texts)]
        raise AssertionError(js)

    def locator(self, sel):
        return _Locator(self, sel)


class _Session:
    def __init__(self, page):
        self.page = page

    async def delay(self, s):
        return None


def _reposter(page):
    r = rp.MiviaReposter(_Session(page), None)

    async def goto(url):
        page.clicks.append("goto")

    r._goto = goto
    return r


def _run(coro):
    return asyncio.run(coro)


def test_dry_run_clicks_no_entry():
    page = _Page(menus=[[INSTANT_DE, THOUGHTS_DE]])
    res = _run(_reposter(page).repost(ACT))
    assert res["status"] == "dry_run" and res["would_click"] == INSTANT_DE
    assert not any("data-mivia-menu" in c for c in page.clicks)
    assert page.keys == ["Escape"]


def test_confirm_verified_by_undo_entry_after_reload():
    page = _Page(menus=[[INSTANT_DE, THOUGHTS_DE], [UNDO_DE, THOUGHTS_DE]])
    r = _reposter(page)
    res = _run(r.repost(ACT, confirm=True))
    assert res["status"] == "reposted" and res["verified"] is True
    assert '[data-mivia-menu="0"]' in page.clicks and r.repost_clicked


def test_confirm_without_readback_is_unverified():
    page = _Page(menus=[[INSTANT_DE, THOUGHTS_DE], []])
    res = _run(_reposter(page).repost(ACT, confirm=True))
    assert res["status"] == "unverified" and res["done"] is True


def test_undo_verified_when_entry_gone():
    page = _Page(menus=[[UNDO_DE, THOUGHTS_DE], [INSTANT_DE, THOUGHTS_DE]])
    res = _run(_reposter(page).repost(ACT, undo=True, confirm=True))
    assert res["status"] == "undone"


def test_undo_still_present_is_undo_unverified():
    page = _Page(menus=[[UNDO_DE], [UNDO_DE]])
    res = _run(_reposter(page).repost(ACT, undo=True, confirm=True))
    assert res["status"] == "undo_unverified"


@pytest.mark.parametrize(
    "buttons,menus,undo,want",
    [
        (0, [], False, "no_repost_button"),
        (2, [], False, "repost_button_ambiguous"),
        (1, [[]], False, "menu_missing"),
        (1, [["Link kopieren"]], False, "menu_unclear"),
        (1, [[INSTANT_DE, "Reposten"]], False, "menu_unclear"),
        (1, [[UNDO_DE, THOUGHTS_DE]], False, "already_reposted"),
        (1, [[INSTANT_DE]], True, "not_reposted"),
    ],
)
def test_nothing_clicked(buttons, menus, undo, want):
    page = _Page(buttons=buttons, menus=menus)
    r = _reposter(page)
    res = _run(r.repost(ACT, undo=undo, confirm=True))
    assert res["status"] == want and res["done"] is False
    assert not r.repost_clicked
    assert not any("data-mivia-menu" in c for c in page.clicks)


# -- tool and ledger -----------------------------------------------------------------


def _tool():
    from linkedin_mcp_server.tools.mivia_stage2 import register_mivia_stage2_tools

    fns = {}

    class Capture:
        def tool(self, *args, **kwargs):
            def register(fn):
                fns[fn.__name__] = fn
                return fn

            return register

    register_mivia_stage2_tools(Capture())  # type: ignore[arg-type]
    return fns["repost_post"]


class _Ex:
    mivia_session = None
    mivia_navigator = None


def _fake(monkeypatch, result=None, exc=None, clicked=True):
    import linkedin_mcp_server.tools.mivia as m
    import linkedin_mcp_server.tools.mivia_stage2 as s2

    calls = []

    class Fake:
        def __init__(self, session, navigator):
            self.repost_clicked = False

        async def repost(self, activity, *, undo=False, confirm=False):
            calls.append((activity, undo, confirm))
            self.repost_clicked = clicked
            if exc is not None:
                raise exc
            return dict(result)

    async def ready(ctx, tool_name=None):
        return _Ex()

    monkeypatch.setattr(s2, "MiviaReposter", Fake)
    monkeypatch.setattr(m, "get_ready_extractor", ready)
    return calls


def _rows(kind):
    return [
        r
        for r in outreach.Ledger.default().latest_by_attempt().values()
        if r.get("kind") == kind
    ]


def _call(**kw):
    return asyncio.run(_tool()(ctx=None, post_url=ACT, **kw))


def test_thoughts_refused_before_anything(monkeypatch):
    calls = _fake(monkeypatch, {"status": "dry_run", "done": False})
    res = _call(thoughts="Lesenswert", confirm=True)
    assert res["status"] == "not_supported" and calls == []


def test_invalid_url(monkeypatch):
    _fake(monkeypatch, {})
    res = asyncio.run(_tool()(ctx=None, post_url="https://example.com/x"))
    assert res["status"] == "invalid_post_url" and res["done"] is False


def test_dry_run_books_no_attempt(monkeypatch):
    _fake(monkeypatch, {"status": "dry_run", "done": False})
    assert _call()["status"] == "dry_run"
    assert _rows("repost") == []


def test_confirm_books_and_blocks_second(monkeypatch):
    calls = _fake(monkeypatch, {"status": "reposted", "done": True, "verified": True})
    assert _call(confirm=True)["status"] == "reposted"
    assert [r["status"] for r in _rows("repost")] == ["reposted"]
    second = _call(confirm=True)
    assert second["status"] == "already_reposted" and len(calls) == 1


def test_verified_undo_releases_post(monkeypatch):
    _fake(monkeypatch, {"status": "reposted", "done": True})
    _call(confirm=True)
    _fake(monkeypatch, {"status": "undone", "done": True})
    assert _call(confirm=True, undo=True)["status"] == "undone"
    calls = _fake(monkeypatch, {"status": "reposted", "done": True})
    assert _call(confirm=True)["status"] == "reposted" and len(calls) == 1


def test_unverified_undo_keeps_block(monkeypatch):
    _fake(monkeypatch, {"status": "reposted", "done": True})
    _call(confirm=True)
    _fake(monkeypatch, {"status": "undo_unverified", "done": True})
    _call(confirm=True, undo=True)
    calls = _fake(monkeypatch, {"status": "reposted", "done": True})
    assert _call(confirm=True)["status"] == "already_reposted" and calls == []


def test_unknown_done_status_is_unverified(monkeypatch):
    _fake(monkeypatch, {"status": "weird", "done": True})
    assert _call(confirm=True)["status"] == "unverified"
    assert _rows("repost")[0]["status"] == "unverified"


def test_done_must_be_literally_true(monkeypatch):
    _fake(monkeypatch, {"status": "reposted", "done": "yes"})
    res = _call(confirm=True)
    assert res["done"] is False and _rows("repost")[0]["status"] == "not_done"


def test_menu_missing_releases(monkeypatch):
    _fake(monkeypatch, {"status": "menu_missing", "done": False})
    _call(confirm=True)
    assert _rows("repost")[0]["status"] == "not_done"
    calls = _fake(monkeypatch, {"status": "reposted", "done": True})
    assert _call(confirm=True)["status"] == "reposted" and len(calls) == 1


def test_nothing_clicked_releases(monkeypatch):
    _fake(monkeypatch, {"status": "menu_unclear", "done": False})
    _call(confirm=True)
    assert _rows("repost")[0]["status"] == "not_done"
    calls = _fake(monkeypatch, {"status": "reposted", "done": True})
    assert _call(confirm=True)["status"] == "reposted" and len(calls) == 1


@pytest.mark.parametrize("clicked,want", [(False, "not_done"), (True, "unknown")])
def test_cancel(monkeypatch, clicked, want):
    _fake(monkeypatch, exc=asyncio.CancelledError(), clicked=clicked)
    with pytest.raises(asyncio.CancelledError):
        _call(confirm=True)
    assert _rows("repost")[0]["status"] == want


def test_pending_undo_refuses_second_undo(monkeypatch):
    _fake(monkeypatch, exc=asyncio.CancelledError(), clicked=True)
    with pytest.raises(asyncio.CancelledError):
        _call(confirm=True, undo=True)
    calls = _fake(monkeypatch, {"status": "undone", "done": True})
    assert _call(confirm=True, undo=True)["status"] == "repost_pending"
    assert calls == []


def test_budget(monkeypatch):
    calls = _fake(monkeypatch, {"status": "reposted", "done": True})
    day = outreach.PACE_BUDGETS["repost"]["day"]
    for i in range(day):
        post = str(int(ACT) + 1 + i)
        assert (
            asyncio.run(_tool()(ctx=None, post_url=post, confirm=True))["status"]
            == "reposted"
        )
    res = _call(confirm=True)
    assert res["status"] == "pace_budget_spent" and res["done"] is False
    assert len(calls) == outreach.PACE_BUDGETS["repost"]["day"]


def test_undo_budget_counts_its_states(monkeypatch):
    _fake(monkeypatch, {"status": "undone", "done": True})
    day = outreach.PACE_BUDGETS["repost_undo"]["day"]
    for i in range(day):
        status = "undone" if i % 2 else "undo_unverified"
        _fake(monkeypatch, {"status": status, "done": True})
        post = str(int(ACT) + 1 + i)
        asyncio.run(_tool()(ctx=None, post_url=post, confirm=True, undo=True))
    res = _call(confirm=True, undo=True)
    assert res["status"] == "pace_budget_spent"


def test_done_states_all_counted():
    for status in ("reposted", "unverified", "undone", "undo_unverified", "unknown"):
        assert status in outreach._COUNTED
    assert "not_done" not in outreach._COUNTED


def test_kinds_registered():
    for kind in ("repost", "repost_undo"):
        assert kind in outreach.PACE_BUDGETS
        assert kind in outreach.PACE_WRITE_KINDS
        assert kind in outreach._LEDGER_KINDS


def test_measured_menu_dry_run():
    page = _Page(menus=[["Mit Kommentar teilen", "Sofort teilen"]])
    res = _run(_reposter(page).repost(ACT))
    assert res["status"] == "dry_run" and res["would_click"] == "Sofort teilen"


def test_menu_missing_reports_new_lines():
    assert rp.new_lines(["a", "b"], ["a", "c", "c", "d"]) == ["c", "d"]
    assert rp.new_lines(None, "x") == []
    assert len(rp.new_lines([], [str(i) for i in range(99)])) == 40


@pytest.mark.parametrize(
    "text,want",
    [
        ("Löschen", None),
        ("Entfernen", None),
        ("Teilen rückgängig machen", "undo"),
        ("Repost entfernen", "undo"),
    ],
)
def test_undo_needs_share_context(text, want):
    assert rp.classify_menu_entry(text) == want


def test_undo_in_unrecognised_menu_clicks_nothing():
    page = _Page(menus=[["Link kopieren"]])
    r = _reposter(page)
    res = _run(r.repost(ACT, undo=True, confirm=True))
    assert res["status"] == "menu_unclear" and not r.repost_clicked


def test_undo_readback_in_unrecognised_menu_is_unverified():
    page = _Page(menus=[[UNDO_DE, THOUGHTS_DE], ["Link kopieren"]])
    res = _run(_reposter(page).repost(ACT, undo=True, confirm=True))
    assert res["status"] == "undo_unverified" and res["verified"] is False


def test_is_share_menu_survives_junk():
    assert not rp.is_share_menu(None)
    assert not rp.is_share_menu([None, {"text": 3}])
    assert rp.is_share_menu([{"text": "Sofort teilen"}])


def test_entry_without_index_clicks_nothing(monkeypatch):
    page = _Page(menus=[[INSTANT_DE]])
    r = _reposter(page)
    monkeypatch.setattr(rp, "pick_entry", lambda items, wanted: ({"text": "x"}, False))
    res = _run(r.repost(ACT, confirm=True))
    assert res["status"] == "menu_unclear" and not r.repost_clicked
