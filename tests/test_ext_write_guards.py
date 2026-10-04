"""fork 2026-10-01: create_post pacing/duplicates, comment text checks
and fresh-comment read-back, withdraw booking only at the click.

No browser: ``_run`` is replaced by a direct call with stub objects.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client, FastMCP

from linkedin_mcp_server import ext_outreach as outreach


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(outreach.LEDGER_ENV, str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv(outreach.NOTES_ENV, str(tmp_path / "notes.json"))


def _call(name, args):
    import linkedin_mcp_server.tools.ext as m

    mcp = FastMCP("t")
    m.register_ext_tools(mcp)

    async def go():
        async with Client(mcp) as c:
            return (
                await c.call_tool(name, args, raise_on_error=False)
            ).structured_content

    return asyncio.run(go())


def _used(action):
    return outreach.Pacer(outreach.Ledger.default()).state(action)["today"]


def _attempt_rows(kind):
    return [
        r
        for r in outreach.Ledger.default().latest_by_attempt().values()
        if r.get("kind") == kind
    ]


def _fake_run(monkeypatch, module, extractor=object()):
    async def fake_run(ctx, tool, body):
        return await body(extractor)

    monkeypatch.setattr(module, "_run", fake_run)


# -- 1. create_post ---------------------------------------------------------


class _Composer:
    def __init__(self, result=None, raise_after_click=None):
        self.result = result or {"status": "posted_verified", "posted": True}
        self.raise_after_click = raise_after_click
        self.calls = []
        self.clicked = False

    async def create_post(self, text, *, image_path, confirm_post):
        self.calls.append(confirm_post)
        if self.raise_after_click is not None:
            self.clicked = self.raise_after_click
            raise RuntimeError("browser gone")
        return self.result


def _post(monkeypatch, composer, text="Neuer Beitrag", confirm=True):
    import linkedin_mcp_server.tools.ext as m

    _fake_run(monkeypatch, m)
    monkeypatch.setattr(m, "_composer", lambda ex: composer)
    return _call("create_post", {"text": text, "confirm_post": confirm})


def test_post_verified_books_once_and_blocks_same_text(monkeypatch):
    composer = _Composer()
    assert _post(monkeypatch, composer)["status"] == "posted_verified"
    assert _used("post") == 1
    again = _post(monkeypatch, composer)
    assert again["status"] == "duplicate_text"
    assert composer.calls == [True]
    assert _used("post") == 1


@pytest.mark.parametrize("status", ["post_unconfirmed", "posted_unverified"])
def test_post_maybe_published_blocks_immediate_repost(monkeypatch, status):
    composer = _Composer({"status": status, "posted": status != "post_unconfirmed"})
    _post(monkeypatch, composer)
    assert _used("post") == 1
    assert _post(monkeypatch, composer)["status"] == "duplicate_text"
    assert composer.calls == [True]


def test_post_not_published_releases_text_and_budget(monkeypatch):
    composer = _Composer({"status": "post_button_unavailable", "posted": False})
    _post(monkeypatch, composer)
    assert _used("post") == 0
    assert _attempt_rows("post")[-1]["status"] == "not_posted"
    _post(monkeypatch, composer)
    assert composer.calls == [True, True]


@pytest.mark.parametrize("clicked,status", [(False, "not_posted"), (True, "unknown")])
def test_post_exception_before_or_after_click(monkeypatch, clicked, status):
    composer = _Composer(raise_after_click=clicked)
    _post(monkeypatch, composer)
    assert _attempt_rows("post")[-1]["status"] == status
    assert _used("post") == (1 if clicked else 0)


def test_post_dry_run_books_nothing(monkeypatch):
    composer = _Composer({"status": "dry_run", "posted": False})
    assert _post(monkeypatch, composer, confirm=False)["status"] == "dry_run"
    assert _used("post") == 0
    assert not outreach.Ledger.default().rows()


def test_post_budget_spent_refuses_before_browser(monkeypatch):
    composer = _Composer()
    for i in range(outreach.PACE_BUDGETS["post"]["day"]):
        assert _post(monkeypatch, composer, text=f"Beitrag {i}")["status"] == (
            "posted_verified"
        )
    out = _post(monkeypatch, composer, text="noch einer")
    assert out["status"] == "pace_budget_spent"
    assert len(composer.calls) == outreach.PACE_BUDGETS["post"]["day"]


def test_post_old_duplicate_outside_window_is_allowed(monkeypatch):
    import linkedin_mcp_server.tools.ext as m

    ledger = outreach.Ledger.default()
    ledger.append(
        {
            "attempt": "old",
            "kind": "post",
            "text_sha": outreach.text_sha("Neuer Beitrag"),
            "status": "posted_verified",
            "started_at": "2026-01-01T10:00:00+01:00",
        }
    )
    assert m.POST_REPEAT_DAYS == 30
    composer = _Composer()
    assert _post(monkeypatch, composer)["status"] == "posted_verified"


def test_post_duplicate_rechecked_under_lock(monkeypatch):
    import linkedin_mcp_server.tools.ext as m

    composer = _Composer()
    real = m._book_attempt

    def racing(ledger, kind, row, **k):
        ledger.append({**row, "attempt": "other"})
        return real(ledger, kind, row, **k)

    monkeypatch.setattr(m, "_book_attempt", racing)
    out = _post(monkeypatch, composer)
    assert out["status"] == "duplicate_text"
    assert composer.calls == []


def test_post_is_a_counted_write_kind():
    assert "post" in outreach._LEDGER_KINDS
    assert "post" in outreach.PACE_WRITE_KINDS
    assert "posted_verified" in outreach._COUNTED


# -- 2. comment_on_post: text checks and fresh read-back ---------------------


@pytest.mark.parametrize(
    "text",
    [
        "Guter​Beitrag",  # zero-width space
        "Beitrag ‏",  # right-to-left mark
        "Beitrag \x85",  # C1 control
        "\U0001f600" * 626,  # 1252 UTF-16 units, 626 code points
    ],
)
def test_comment_refuses_invisible_and_utf16_overlength(text):
    out = _call(
        "comment_on_post",
        {
            "post_url": "urn:li:activity:7123456789012345678",
            "text": text,
            "confirm": True,
        },
    )
    assert out["status"] == "invalid_text"
    assert not outreach.Ledger.default().rows()


class _Locator:
    def __init__(self, page):
        self.page = page
        self.first = self

    async def count(self):
        return 1

    async def click(self):
        if self.page.submit_marked:
            self.page.clicked = True

    async def inner_text(self):
        return self.page.typed

    async def evaluate(self, js):
        self.page.submit_marked = True
        return {"disabled": False}


class _Keyboard:
    def __init__(self, page):
        self.page = page

    async def insert_text(self, text):
        self.page.typed += text

    async def press(self, key):
        if key == "Shift+Enter":
            self.page.typed += "\n"


class _Page:
    def __init__(self, before, after):
        self.before = before
        self.after = after
        self.typed = ""
        self.clicked = False
        self.submit_marked = False
        self.keyboard = _Keyboard(self)

    def locator(self, selector):
        return _Locator(self)

    async def evaluate(self, js, *a):
        return self.after if self.clicked else self.before


class _Session:
    def __init__(self, page):
        self.page = page

    async def delay(self, *a):
        return None


def _comment_actions(page):
    from linkedin_mcp_server.linkedin.ext_actions import ExtActions

    actions = ExtActions(_Session(page), None)

    async def goto(url):
        return None

    actions._goto = goto
    return actions


OLD = {"key": "replaceableComment_urn:li:comment:(a,1)", "text": "Starker Beitrag"}
NEW = {"key": "replaceableComment_urn:li:comment:(a,2)", "text": "Starker Beitrag"}


def test_comment_readback_ignores_older_identical_comment():
    page = _Page(before=[OLD], after=[OLD])
    out = asyncio.run(
        _comment_actions(page).comment("1", "Starker Beitrag", confirm=True)
    )
    assert out["status"] == "unverified"
    assert out["verified"] is False


def test_comment_readback_counts_new_comment():
    page = _Page(before=[OLD], after=[OLD, NEW])
    out = asyncio.run(
        _comment_actions(page).comment("1", "Starker Beitrag", confirm=True)
    )
    assert out["status"] == "posted"


def test_comment_readback_keyless_comment_never_counts():
    from linkedin_mcp_server.linkedin.ext_actions import _matching_comment_keys

    assert _matching_comment_keys([{"key": "", "text": "x"}, "x", None], "x") == set()


# -- 3. withdraw_invitations: booking only at the click ----------------------


class _Withdrawer:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def sent_invitations_with_age(self, limit):
        return [
            {"slug": "anna", "name": "Anna", "age_days": 40},
            {"slug": "bert", "name": "Bert", "age_days": 40},
        ]

    async def withdraw(self, name, slug):
        self.calls.append(slug)
        return {"slug": slug, "name": name, "status": self.outcomes.pop(0)}


def _withdraw(monkeypatch, actions, usernames):
    import linkedin_mcp_server.tools.ext_stage2 as s2

    _fake_run(monkeypatch, s2)
    monkeypatch.setattr(s2, "_actions", lambda ex: actions)

    async def no_sleep(*a):
        return None

    monkeypatch.setattr(s2.asyncio, "sleep", no_sleep)
    return _call(
        "withdraw_invitations",
        {"confirm_withdraw": True, "usernames": usernames, "older_than_days": 30},
    )


def test_withdraw_not_found_spends_no_budget(monkeypatch):
    out = _withdraw(monkeypatch, _Withdrawer(["not_found"]), ["anna"])
    assert out["status"] == "stopped"
    assert _used("withdraw") == 0
    assert _attempt_rows("withdraw")[-1]["status"] == "not_found"


def test_withdraw_counts_each_clicked_withdrawal_once(monkeypatch):
    out = _withdraw(
        monkeypatch, _Withdrawer(["withdrawn", "withdrawn"]), ["anna", "bert"]
    )
    assert out["withdrawn"] == 2
    assert _used("withdraw") == 2
    assert not [
        r
        for r in outreach.Ledger.default().rows()
        if r.get("kind") == "pace" and r.get("action") == "withdraw"
    ]


def test_bidi_controls_are_invisible_controls():
    from linkedin_mcp_server.linkedin.contracts import is_invisible_control

    for code in (0x202A, 0x202E, 0x2066, 0x2069):
        assert is_invisible_control(chr(code))
    assert not is_invisible_control("\u200d")
    assert not is_invisible_control("\u00e4")
