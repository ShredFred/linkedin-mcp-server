"""MiViA fork: send_inmail and edit_sent_message -- checks, parsing, gating."""

from __future__ import annotations

import pytest

from linkedin_mcp_server.mivia_message_checks import check_message

# -- 2026-09-30 round 3: selector fallback chains, status instead of timeout --


class _Loc:
    """Fake locator: ``hits`` maps a selector (or (css, regex)) to a count."""

    def __init__(self, hits, key, clicks):
        self.hits, self.key, self.clicks = hits, key, clicks

    def locator(self, css):
        return _Loc(self.hits, css, self.clicks)

    def filter(self, has_text=None):
        return _Loc(self.hits, (self.key, has_text.pattern), self.clicks)

    @property
    def first(self):
        return self

    @property
    def last(self):
        return self

    async def count(self):
        return self.hits.get(self.key, 0)

    async def click(self):
        self.clicks.append(self.key)

    async def is_disabled(self):
        return False

    async def wait_for(self, **kw):
        return None

    async def inner_text(self):
        return self.hits.get("text", "")

    async def evaluate(self, js, arg=None):
        self.hits["text"] = arg
        return True

    async def scroll_into_view_if_needed(self):
        return None

    async def hover(self):
        return None


class _Kbd:
    def __init__(self):
        self.pressed = []

    async def press(self, key):
        self.pressed.append(key)


class _Page(_Loc):
    def __init__(self, hits, menu):
        super().__init__(hits, None, [])
        self.keyboard = _Kbd()
        self.menu = menu

    async def evaluate(self, js, arg=None):
        return [{"t": t} for t in self.menu]


def _editor(hits, menu=("Bearbeiten",)):
    from linkedin_mcp_server.linkedin.mivia_inmail import MiviaInmail

    obj = object.__new__(MiviaInmail)
    obj._session = type("S", (), {"page": _Page(hits, list(menu))})()

    async def no_wait(low, high):
        return None

    obj._wait = no_wait
    return obj


_MSG = {"index": 0, "own": True, "text": "alt"}
_BASE = {
    "button.msg-s-event-listitem__options-trigger": 1,
    '.artdeco-dropdown__content :text-is("Bearbeiten"):visible': 1,
    "form.msg-edit-form__base-form": 1,
    '[contenteditable="true"]': 1,
    "text": "alt",
}


def _run_edit(hits, **kw):
    import asyncio

    ed = _editor(hits, **kw)
    return ed, asyncio.run(ed.edit("u", dict(_MSG), "neu", confirm=False))


class TestEditFallbacks:
    def test_aria_label_fallbacks_reach_dry_run(self):
        hits = {
            **_BASE,
            "button.msg-s-event-listitem__options-trigger": 0,
            'button[aria-label*="Optionen"]': 1,
            'button[aria-label^="Cancel"]': 1,
            'button[aria-label^="Save"]': 1,
        }
        ed, out = _run_edit(hits)
        assert out["status"] == "dry_run"
        assert 'button[aria-label^="Cancel"]' in ed._page.clicks
        assert 'button[aria-label^="Save"]' not in ed._page.clicks

    def test_role_button_text_fallback(self):
        hits = {
            **_BASE,
            ('button, [role="button"]', r"^\s*(Abbrechen|Cancel)\s*$"): 1,
            ('button, [role="button"]', r"^\s*(Speichern|Save)\s*$"): 1,
        }
        _, out = _run_edit(hits)
        assert out["status"] == "dry_run"

    def test_no_options_trigger_is_editor_mismatch(self):
        hits = {**_BASE, "button.msg-s-event-listitem__options-trigger": 0}
        ed, out = _run_edit(hits, menu=())
        assert out == {
            "status": "editor_mismatch",
            "edited": False,
            "reason": "no_options_trigger",
        }
        assert ed._page.clicks == []

    def test_no_menu_item_is_editor_mismatch(self):
        hits = {**_BASE, '.artdeco-dropdown__content :text-is("Bearbeiten"):visible': 0}
        _, out = _run_edit(hits)
        assert out["reason"] == "no_edit_menu_item"

    def test_no_cancel_is_form_mismatch_and_editor_untouched(self):
        hits = {**_BASE, "button.msg-edit-form__save-button": 1}
        ed, out = _run_edit(hits)
        assert out["status"] == "edit_form_mismatch"
        assert ed._page.hits["text"] == "alt"
        assert "button.msg-edit-form__save-button" not in ed._page.clicks

    def test_no_save_cancels(self):
        hits = {**_BASE, "button.msg-edit-form__dismiss-button": 1}
        ed, out = _run_edit(hits)
        assert out["status"] == "save_button_unavailable"
        assert ed._page.clicks[-1] == "button.msg-edit-form__dismiss-button"

    def test_foreign_message_refused_before_any_click(self):
        import asyncio

        ed = _editor(dict(_BASE))
        out = asyncio.run(ed.edit("u", {**_MSG, "own": False}, "neu", confirm=True))
        assert out["status"] == "not_own_message"
        assert ed._page.clicks == []


class TestFirstMatch:
    def test_order_and_none(self):
        import asyncio

        from linkedin_mcp_server.linkedin.mivia_inmail import (
            _SN_SEND,
            _SN_SUBJECT,
            first_match,
        )

        page = _Page(
            {'input[placeholder^="Subject"]': 1, 'button[aria-label^="Senden"]': 2}, []
        )
        assert (
            asyncio.run(first_match(page, _SN_SUBJECT)).key
            == 'input[placeholder^="Subject"]'
        )
        assert (
            asyncio.run(first_match(page, _SN_SEND, last=True)).key
            == 'button[aria-label^="Senden"]'
        )
        assert asyncio.run(first_match(_Page({}, []), _SN_SEND)) is None


class TestMessageChecks:
    def test_clean_text_passes(self):
        text = "Hallo Herr König,\nkurz zur HK: https://calendly.com/mivia_jessica-schneider/30min"
        assert check_message(text, "Dieter König") == []

    def test_wrong_calendly_and_http(self):
        codes = {
            f["code"]
            for f in check_message("x https://calendly.com/mivia y http://a.de")
        }
        assert codes == {"calendly_wrong_account", "link_not_https"}

    def test_shortener(self):
        assert check_message("https://bit.ly/abc")[0]["code"] == "link_shortener"

    def test_placeholder(self):
        codes = [f["code"] for f in check_message("Hallo {vorname}, XXX")]
        assert codes.count("unfilled_placeholder") == 2

    def test_salutation_mismatch(self):
        found = check_message("Sehr geehrter Herr Sperling,\nText", "Dieter König")
        assert found[0]["code"] == "salutation_mismatch"

    def test_salutation_umlaut_folding_and_first_name(self):
        assert check_message("Hallo Herr Koenig, Text", "Dieter König") == []
        assert check_message("Hallo Herr König, Text", "Dieter Koenig") == []
        assert check_message("Hi Dieter, Text", "Dieter König") == []
        assert check_message("Liebe Frau Dr. Karlsohn, Text", "Anna Karlsohn") == []

    def test_salutation_without_name_is_unverifiable(self):
        assert (
            check_message("Hallo Frederik,", None)[0]["code"]
            == "salutation_unverifiable"
        )

    def test_no_salutation_no_finding(self):
        assert check_message("Kurze Frage zur HK.", None) == []


from linkedin_mcp_server import mivia_outreach as outreach  # noqa: E402
from linkedin_mcp_server.linkedin.mivia_inmail import (  # noqa: E402
    parse_credits,
    parse_degree,
    pick_own_message,
    thread_url,
)
from linkedin_mcp_server.tools.mivia_inmail import precheck_inmail  # noqa: E402


class TestInmailParsing:
    def test_composer_credit_line(self):
        c = parse_credits("Neue Nachricht an X\n1 von 150 InMail-Guthaben verwenden")
        assert (c["cost"], c["remaining"], c["free"], c["none_left"]) == (
            1,
            150,
            False,
            False,
        )

    def test_inbox_line_and_units(self):
        assert parse_credits("InMail-Guthaben: 150 verbleibend")["remaining"] == 150
        assert (
            parse_credits("Ihnen stehen 1.200 InMail-Guthaben-Einheiten zur Verfügung")[
                "remaining"
            ]
            == 1200
        )

    def test_150_is_not_zero(self):
        # "150 InMail-Guthaben" contains "0 InMail-Guthaben" -- live bug 2026-09-30.
        assert (
            parse_credits("1 von 150 InMail-Guthaben verwenden")["none_left"] is False
        )
        assert parse_credits("1 von 0 InMail-Guthaben verwenden")["none_left"] is True

    def test_open_profile_only_without_cost(self):
        assert parse_credits("Kostenlose Nachricht an ein Open Profile")["free"] is True
        assert (
            parse_credits("1 von 150 InMail-Guthaben verwenden, kostenlos")["free"]
            is False
        )

    def test_degree(self):
        assert parse_degree("Santiago\n·3.\nCreator") == 3
        assert parse_degree("Frederik Stadler • 1.") == 1
        assert parse_degree("kein Grad") is None

    def test_thread_url(self):
        tid = "2-NWE3ZmFlMmEtZGMzMS00ODA0LTgyZWQtZjk4NjdmMTFiNzY3XzEwMA=="
        want = f"https://www.linkedin.com/messaging/thread/{tid}/"
        assert thread_url(tid) == want
        assert thread_url(want + "?x=1") == want
        with pytest.raises(ValueError):
            thread_url("../etc")


class TestPickOwnMessage:
    msgs = [
        {"index": 0, "own": False, "text": "Hallo Jessica"},
        {"index": 1, "own": True, "text": "Fassung A erste"},
        {"index": 2, "own": True, "text": "Fassung A zweite"},
        {"index": 3, "own": True, "text": "Letzte"},
    ]

    def test_default_is_last_own(self):
        assert pick_own_message(self.msgs, None)["message"]["index"] == 3

    def test_match_unique_ambiguous_missing(self):
        assert pick_own_message(self.msgs, "zweite")["message"]["index"] == 2
        assert pick_own_message(self.msgs, "Fassung A")["status"] == "ambiguous_match"
        assert (
            pick_own_message(self.msgs, "Hallo Jessica")["status"]
            == "message_not_found"
        )
        assert pick_own_message(self.msgs[:1], None)["status"] == "no_own_message"


class TestPrecheck:
    def test_subject_required_and_single_line(self):
        assert precheck_inmail("a", "", "Text")["status"] == "subject_required"
        assert (
            precheck_inmail("a", "Zeile\nzwei", "Text")["status"] == "invalid_subject"
        )

    def test_body_rules(self):
        assert precheck_inmail("a", "Betreff", "x\ry")["status"] == "invalid_message"
        assert (
            precheck_inmail("a", "Betreff", "https://calendly.com/mivia")["status"]
            == "content_check_failed"
        )
        assert precheck_inmail("a", "Betreff", "Hallo Herr König, Text") is None


def _tools():
    import asyncio

    from fastmcp import FastMCP

    from linkedin_mcp_server.tools.mivia_inmail import register_mivia_inmail_tools

    mcp = FastMCP("t")
    register_mivia_inmail_tools(mcp)
    return mcp, asyncio


class TestToolGates:
    @pytest.fixture(autouse=True)
    def _ledger(self, tmp_path, monkeypatch):
        monkeypatch.setenv(outreach.LEDGER_ENV, str(tmp_path / "ledger.jsonl"))

    def _call(self, name, args):
        mcp, asyncio = _tools()
        from fastmcp import Client

        async def go():
            async with Client(mcp) as c:
                return (await c.call_tool(name, args)).structured_content

        return asyncio.run(go())

    def test_registered_and_tagged(self):
        mcp, asyncio = _tools()
        tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
        for name in ("send_inmail", "inmail_credits", "edit_sent_message"):
            assert "mivia" in tools[name].tags

    def test_inmail_duplicate_blocks_before_browser(self):
        outreach.Ledger.default().append(
            {
                "attempt": "x",
                "kind": "inmail",
                "recipient": outreach.recipient_key("dieter"),
                "status": "verified",
                "started_at": "2026-09-30T10:00:00+02:00",
            }
        )
        out = self._call(
            "send_inmail",
            {
                "linkedin_username": "dieter",
                "subject": "S",
                "body": "B",
                "confirm": True,
            },
        )
        assert out["status"] == "duplicate"

    def test_inmail_pace_blocks_before_browser(self, monkeypatch):
        monkeypatch.setitem(outreach.PACE_BUDGETS, "inmail", {"day": 0, "week": 0})
        out = self._call(
            "send_inmail",
            {
                "linkedin_username": "dieter",
                "subject": "S",
                "body": "B",
                "confirm": True,
            },
        )
        assert out["status"] == "pace_budget_spent"

    def test_inmail_needs_recipient(self):
        assert (
            self._call("send_inmail", {"subject": "S", "body": "B"})["status"]
            == "recipient_required"
        )

    def test_edit_refuses_bad_thread_and_content(self):
        assert (
            self._call("edit_sent_message", {"thread": "x", "new_text": "a"})["status"]
            == "invalid_thread"
        )
        out = self._call(
            "edit_sent_message", {"thread": "2-abcdefghijkl", "new_text": "http://x.de"}
        )
        assert out["status"] == "content_check_failed"


class TestHardening20260930:
    def test_subject_salutation_not_blocked_before_profile_read(self):
        assert precheck_inmail("a", "Hallo Herr König", "Text") is None

    def test_edit_landed_only_on_target_index(self):
        from linkedin_mcp_server.linkedin.mivia_inmail import edit_landed

        msgs = [
            {"index": 0, "own": True, "text": "Danke und bis bald"},
            {"index": 1, "own": True, "text": "Alter Text"},
        ]
        assert not edit_landed(msgs, {"index": 1}, "Danke")
        msgs[1]["text"] = "Danke (bearbeitet)"
        assert edit_landed(msgs, {"index": 1}, "Danke")
        assert not edit_landed(msgs, {"index": 5}, "Danke")
        assert not edit_landed(
            [{"index": 1, "own": False, "text": "Danke"}], {"index": 1}, "Danke"
        )

    def test_umlaut_and_nbsp_canon(self):
        from linkedin_mcp_server.linkedin.mivia_inmail import canon

        assert canon("Grüße  an  Jörg\n") == "Grüße an Jörg"
