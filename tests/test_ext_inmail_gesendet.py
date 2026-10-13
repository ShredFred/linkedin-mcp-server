"""Fork extension 2026-10-13: sales_nav_sent_inmails and a robust inmail_credits."""

from __future__ import annotations

import asyncio
import re

import pytest

from linkedin_mcp_server import ext_outreach as outreach
from linkedin_mcp_server.linkedin import ext_inmail as mod
from linkedin_mcp_server.linkedin.ext_inmail import (
    ExtInmail,
    credit_mentions,
    credits_line,
    diagnose_credits_page,
    parse_credits,
    parse_sent_item,
)

INBOX_TEXT = "Posteingang\n" + "x" * 300


# -- parsers -------------------------------------------------------------------


class TestCreditWordings:
    @pytest.mark.parametrize(
        "line,expected",
        [
            ("InMail-Guthaben: 116 verbleibend", 116),
            ("116 InMail-Guthaben-Einheiten", 116),
            ("InMail credits: 1,116 remaining", 1116),
            ("Verbleibende InMail-Guthaben: 42", 42),
            ("42 InMail-Guthaben verfügbar", 42),
            ("InMail-Guthaben verbleibend: 7", 7),
            ("50 InMail credits left", 50),
            ("Remaining InMail credits: 9", 9),
            ("InMail credits remaining 3", 3),
        ],
    )
    def test_line_is_read(self, line, expected):
        found = credits_line(f"Kopf\n  {line}  \nRest")
        assert found == line
        assert parse_credits(found)["remaining"] == expected

    def test_use_wording_is_not_a_left_line(self):
        assert credits_line("Use 1 of 116 InMail credits") is None

    def test_preview_line_is_not_the_header(self):
        # review 2026-10-13: a long preview saying "50 InMail credits left"
        preview = "Sie: Kurze Info, ich habe noch 50 InMail credits left und schreibe Ihnen deshalb heute"
        text = preview + "\nInMail-Guthaben: 116 verbleibend"
        assert credits_line(text) == "InMail-Guthaben: 116 verbleibend"

    def test_disagreeing_lines_give_none(self):
        text = "InMail-Guthaben: 116 verbleibend\n50 InMail credits left"
        assert credits_line(text) is None
        assert (
            diagnose_credits_page("https://www.linkedin.com/sales/inbox/", "", text)
            == "ambiguous_credits_line"
        )

    def test_no_line(self):
        assert credits_line("Posteingang\nNachricht") is None


class TestDiagnosis:
    def test_login(self):
        assert (
            diagnose_credits_page("https://www.linkedin.com/login?x", "", "")
            == "login_page"
        )
        assert (
            diagnose_credits_page(
                "https://www.linkedin.com/sales/inbox/",
                "",
                "Anmelden\nE-Mail\nPasswort",
            )
            == "login_page"
        )

    def test_upsell(self):
        assert (
            diagnose_credits_page(
                "https://www.linkedin.com/sales/",
                "",
                "Sales Navigator kostenlos testen",
            )
            == "sales_nav_upsell"
        )

    def test_redirect(self):
        assert (
            diagnose_credits_page("https://www.linkedin.com/feed/", "Feed", "x" * 500)
            == "not_sales_inbox"
        )

    def test_not_rendered_and_unknown(self):
        url = "https://www.linkedin.com/sales/inbox/"
        assert diagnose_credits_page(url, "", "kurz") == "inbox_not_rendered"
        assert diagnose_credits_page(url, "", INBOX_TEXT) == "no_credits_line"

    def test_mentions_skip_long_previews(self):
        text = "InMail-Kontingent 116\n" + "Sie: " + "lang InMail " * 20 + "\nInMail"
        assert credit_mentions(text) == ["InMail-Kontingent 116", "InMail"]


class TestParseSentItem:
    def test_full_row(self):
        row = parse_sent_item(
            {
                "href": "/sales/inbox/2-abc",
                "lines": [
                    "Erika Muster",
                    "InMail",
                    "12. Okt.",
                    "Gefügeanalyse",
                    "Sie: Guten Tag Frau Muster, ich",
                ],
            }
        )
        assert row == {
            "recipient": "Erika Muster",
            "when": "12. Okt.",
            "subject": "Gefügeanalyse",
            "subject_source": "line_before_snippet",
            "text_start": "Guten Tag Frau Muster, ich",
            "own_last": True,
            "thread_url": "https://www.linkedin.com/sales/inbox/2-abc",
        }

    def test_reply_is_not_own(self):
        row = parse_sent_item(
            {"href": "", "lines": ["Max M", "10:42", "Danke, passt."]}
        )
        assert row["own_last"] is False
        assert row["subject"] is None
        assert row["when"] == "10:42"
        assert row["thread_url"] is None

    def test_labelled_subject_and_cap(self):
        row = parse_sent_item(
            {"lines": ["A B", "Gestern", "Betreff: Termin", "You: " + "y" * 500]}
        )
        assert row["subject"] == "Termin"
        assert row["subject_source"] == "labelled"
        assert len(row["text_start"]) == mod.SENT_SNIPPET_MAX

    def test_two_time_lines_never_become_subject(self):
        # review 2026-10-13: "Heute" took when, "12:30" became the subject
        row = parse_sent_item({"lines": ["Erika", "Heute", "12:30", "Sie: Hallo"]})
        assert row["when"] == "Heute 12:30"
        assert row["subject"] is None
        assert row["recipient"] == "Erika"

    def test_time_first_is_not_recipient(self):
        row = parse_sent_item({"lines": ["10:42", "Max M", "Sie: Hallo 12"]})
        assert row["recipient"] == "Max M" and row["when"] == "10:42"

    def test_garbage(self):
        assert parse_sent_item(None) is None
        assert parse_sent_item({"lines": ["", "InMail"]}) is None


# -- browser fakes -------------------------------------------------------------


class _Loc:
    def __init__(self, page, key):
        self.page, self.key = page, key

    def filter(self, has_text=None):
        return _Loc(self.page, (self.key, has_text.pattern))

    @property
    def first(self):
        return self

    @property
    def last(self):
        return self

    async def count(self):
        return 1 if self._hit() else 0

    def _hit(self):
        if not isinstance(self.key, tuple):
            return False
        css, pattern = self.key
        if "aria-haspopup" in css or css == "button":
            return bool(re.search(pattern, self.page.label))
        return self.page.menu_open and bool(re.search(pattern, "Gesendet"))

    async def inner_text(self):
        return self.page.label

    async def click(self):
        self.page.clicks.append(self.key)
        if "aria-haspopup" in self.key[0] or self.key[0] == "button":
            self.page.menu_open = True
        else:
            self.page.label = self.page.after_option
            self.page.menu_open = False


class _Kbd:
    def __init__(self):
        self.pressed = []

    async def press(self, key):
        self.pressed.append(key)


class _Page:
    url = "https://www.linkedin.com/sales/inbox/"

    def __init__(
        self, texts=(), label="Posteingang", after_option="Gesendet", batches=()
    ):
        self.texts = list(texts)
        self.label = label
        self.after_option = after_option
        self.batches = list(batches)
        self.menu_open = False
        self.clicks = []
        self.typed = []
        self.keyboard = _Kbd()
        self.evals = []

    def locator(self, css):
        return _Loc(self, css)

    async def title(self):
        return "Sales Navigator"

    async def evaluate(self, js, arg=None):
        self.evals.append(js)
        if "innerText : ''" in js:
            return self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        if js is mod._SENT_ITEMS_JS:
            return self.batches.pop(0) if len(self.batches) > 1 else self.batches[0]
        return True

    async def fill(self, *a, **k):  # pragma: no cover - must never be called
        self.typed.append(a)


def _reader(page):
    obj = object.__new__(ExtInmail)
    obj._session = type("S", (), {"page": page})()
    obj.gotos = []

    async def goto(url):
        obj.gotos.append(url)

    async def no_wait(low, high):
        return None

    obj._goto = goto
    obj._wait = no_wait
    return obj


class TestCreditsRead:
    def test_waits_for_late_header(self):
        page = _Page(texts=["Laden", "Laden", "InMail-Guthaben: 116 verbleibend"])
        out = asyncio.run(_reader(page).inmail_credits(timeout_s=30))
        assert out == {"remaining": 116, "line": "InMail-Guthaben: 116 verbleibend"}

    def test_no_line_says_why(self):
        page = _Page(texts=[INBOX_TEXT])
        out = asyncio.run(_reader(page).inmail_credits(timeout_s=0))
        assert out["remaining"] is None and out["line"] is None
        assert out["reason"] == "no_credits_line"
        assert out["url"] == _Page.url
        assert out["title"] == "Sales Navigator"
        assert out["credit_mentions"] == []


ROWS = [
    {"href": "/sales/inbox/2-a", "lines": ["A A", "12. Okt.", "Betr", "Sie: Hallo"]},
    {"href": "/sales/inbox/2-b", "lines": ["B B", "11. Okt.", "Sie: Guten Tag"]},
    {"href": "/sales/inbox/2-c", "lines": ["C C", "10. Okt.", "Sie: Moin"]},
]

INBOX_ROWS = [{"href": "/sales/inbox/2-z", "lines": ["Z Z", "12:00", "Antwort von Z"]}]


class TestSentView:
    def test_switches_filter_and_reads(self):
        page = _Page(batches=[INBOX_ROWS, ROWS[:2], ROWS, ROWS])
        out = asyncio.run(_reader(page).sent_inmails(limit=30))
        assert out["status"] == "ok" and out["view"] == "sent"
        assert [r["recipient"] for r in out["items"]] == ["A A", "B B", "C C"]
        assert out["complete"] is True and out["boundary"] == "end_of_list"
        assert page.typed == []
        assert len(page.clicks) == 2  # filter trigger, then the "Gesendet" option

    def test_limit_boundary(self):
        page = _Page(batches=[INBOX_ROWS, ROWS])
        out = asyncio.run(_reader(page).sent_inmails(limit=2))
        assert out["count"] == 2
        assert out["complete"] is False and out["boundary"] == "limit"

    def test_unconfirmed_view_returns_no_rows(self):
        page = _Page(batches=[ROWS], after_option="Posteingang")
        out = asyncio.run(_reader(page).sent_inmails(limit=5))
        assert out["status"] == "view_not_confirmed"
        assert out["items"] == [] and out["complete"] is False

    def test_list_unchanged_after_switch_gives_no_rows(self):
        # review 2026-10-13: label switched, list still the inbox
        page = _Page(batches=[INBOX_ROWS])
        out = asyncio.run(_reader(page).sent_inmails(limit=5))
        assert out["status"] == "view_not_confirmed"
        assert out["reason"] == "list_unchanged" and out["items"] == []

    def test_empty_inbox_list_stops_before_switch(self):
        # review 2026-10-13 round 2: without an inbox snapshot the race guard is off
        page = _Page(batches=[[]])
        out = asyncio.run(_reader(page).sent_inmails(limit=5))
        assert out["status"] == "inbox_list_not_loaded" and out["items"] == []
        assert page.clicks == []

    def test_already_sent_view_needs_no_click(self):
        page = _Page(batches=[ROWS], label="Gesendet")
        out = asyncio.run(_reader(page).sent_inmails(limit=5))
        assert out["status"] == "ok" and out["count"] == 3
        assert page.clicks == []

    def test_no_filter(self):
        page = _Page(batches=[ROWS], label="Etwas anderes")
        out = asyncio.run(_reader(page).sent_inmails(limit=5))
        assert out["status"] == "filter_not_found"
        assert page.clicks == []


# -- tool gate -----------------------------------------------------------------


class TestTool:
    @pytest.fixture(autouse=True)
    def _ledger(self, tmp_path, monkeypatch):
        monkeypatch.setenv(outreach.LEDGER_ENV, str(tmp_path / "ledger.jsonl"))

    def _call(self, args):
        from fastmcp import Client, FastMCP

        from linkedin_mcp_server.tools.ext_inmail import register_ext_inmail_tools

        mcp = FastMCP("t")
        register_ext_inmail_tools(mcp)

        async def go():
            async with Client(mcp) as c:
                tools = {t.name: t for t in await c.list_tools()}
                out = (
                    await c.call_tool("sales_nav_sent_inmails", args)
                ).structured_content
                return tools, out

        return asyncio.run(go())

    def test_read_only_and_limit_checked(self):
        tools, out = self._call({"limit": 0})
        tool = tools["sales_nav_sent_inmails"]
        assert tool.annotations.read_only_hint is True
        assert not tool.annotations.destructive_hint
        assert out["status"] == "invalid_limit"

    def test_pace_refusal_before_browser(self, monkeypatch):
        monkeypatch.setitem(outreach.PACE_BUDGETS, "page_read", {"day": 0, "week": 0})
        _, out = self._call({"limit": 5})
        assert out["status"] == "pace_budget_spent"
