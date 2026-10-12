"""send_inmail to an Open Profile: only on request, only when the line says free.

Without ``allow_open_profile`` an Open Profile stops the send exactly as
before. With it, the send goes on only when the composer's credit line says
"free" and names no cost and no exhausted balance; every other credit case
still stops. No browser, no network: the composer is a fake.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client, FastMCP

from linkedin_mcp_server import ext_outreach as outreach
from linkedin_mcp_server.linkedin.ext_inmail import (
    ExtInmail,
    credit_refusal,
    parse_credits,
)

FREE_LINE = "Kostenlose Nachricht an ein Open Profile" + "\n" + "Empfängerinformationen"
PAID_LINE = "1 von 150 InMail-Guthaben verwenden"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(outreach.LEDGER_ENV, str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv(outreach.NOTES_ENV, str(tmp_path / "notes.json"))


# -- credit_refusal -------------------------------------------------------------


@pytest.mark.parametrize(
    "credits,without,with_flag",
    [
        # The one case the flag opens.
        ({"free": True, "cost": None}, "open_profile", None),
        # Free, but the balance is said to be exhausted: not proven free.
        (
            {"free": True, "cost": None, "none_left": True},
            "open_profile",
            "open_profile",
        ),
        # A free marker beside a cost is not free (parse_credits never makes it,
        # but credit_refusal must not trust that).
        ({"free": True, "cost": 1}, "open_profile", "open_profile"),
        ({"free": True, "cost": 2}, "open_profile", "open_profile"),
        # Not free: unchanged, flag or not.
        ({"none_left": True}, "no_inmail_credits", "no_inmail_credits"),
        ({"cost": None}, "not_an_inmail_composer", "not_an_inmail_composer"),
        ({"cost": 2}, "unexpected_inmail_cost", "unexpected_inmail_cost"),
        ({"cost": 1, "remaining": 0}, "no_inmail_credits", "no_inmail_credits"),
        ({"cost": 1, "remaining": 5}, None, None),
        ({}, "not_an_inmail_composer", "not_an_inmail_composer"),
    ],
)
def test_credit_refusal_flag(credits, without, with_flag):
    assert credit_refusal(credits) == without
    assert credit_refusal(credits, allow_open_profile=False) == without
    assert credit_refusal(credits, allow_open_profile=True) == with_flag


def test_parsed_lines():
    assert parse_credits(FREE_LINE)["free"] is True
    assert credit_refusal(parse_credits(FREE_LINE), allow_open_profile=True) is None
    assert credit_refusal(parse_credits(FREE_LINE)) == "open_profile"
    # A paid line mentioning "kostenlos" is not free.
    paid_free = parse_credits(PAID_LINE + ", kostenlos")
    assert paid_free["free"] is False
    assert credit_refusal(paid_free, allow_open_profile=True) is None  # priced at 1
    assert (
        credit_refusal(
            parse_credits("Kostenlos, 0 InMail-Guthaben verwenden"),
            allow_open_profile=True,
        )
        is not None
    )


# -- the composer (ExtInmail._inmail) with a fake page --------------------------


class _El:
    def __init__(self, page, key):
        self.page, self.key = page, key
        self.value = ""

    def locator(self, css):
        return self.page.el(css)

    def filter(self, has_text=None):
        return self.page.el((self.key, getattr(has_text, "pattern", has_text)))

    @property
    def first(self):
        return self

    @property
    def last(self):
        return self

    async def count(self):
        return 0 if "schließen" in str(self.key) else 1

    async def click(self):
        self.page.clicks.append(self.key)

    async def wait_for(self, **kw):
        return None

    async def inner_text(self):
        return self.page.header

    async def fill(self, text):
        self.value = text

    async def input_value(self):
        return self.value

    async def is_disabled(self):
        return False


class _FakePage:
    url = "https://www.linkedin.com/sales/lead/x"

    def __init__(self, header):
        self.header = header
        self.clicks = []
        self.els = {}
        self.balance_reads = 0

    def el(self, key):
        return self.els.setdefault(key, _El(self, key))

    def locator(self, css):
        return self.el(css)

    async def evaluate(self, js, arg=None):
        return "Anna Muster · 2."


def _composer(header, balances=(150, 150)):
    obj = object.__new__(ExtInmail)
    page = _FakePage(header)
    obj._session = type("S", (), {"page": page})()

    async def no_wait(low, high):
        return None

    async def goto(url):
        return None

    left = list(balances)

    async def credits():
        page.balance_reads += 1
        return {"remaining": left.pop(0) if left else None}

    obj._wait = no_wait
    obj._goto = goto
    obj.inmail_credits = credits
    return obj, page


TARGET = {"status": "ok", "name": "Anna Muster", "sales_url": "https://x/sales"}


def _send(header, *, confirm, allow, balances=(150, 150)):
    obj, page = _composer(header, balances)
    out = asyncio.run(
        obj.inmail(
            TARGET, "Betreff", "Guten Tag", confirm=confirm, allow_open_profile=allow
        )
    )
    from linkedin_mcp_server.linkedin.ext_inmail import _SN_SEND

    sent_clicks = [c for c in page.clicks if c in _SN_SEND]
    _send.page = page
    return out, sent_clicks


def test_composer_open_profile_without_flag_stops():
    out, sends = _send(FREE_LINE, confirm=True, allow=False)
    assert out["status"] == "open_profile"
    assert out["sent"] is False
    assert sends == []


def test_composer_open_profile_with_flag_dry_run():
    out, sends = _send(FREE_LINE, confirm=False, allow=True)
    assert out["status"] == "dry_run"
    assert out["open_profile"] is True
    assert sends == []


def test_composer_open_profile_with_flag_sends():
    out, sends = _send(FREE_LINE, confirm=True, allow=True)
    assert out["sent"] is True
    assert out["open_profile"] is True
    assert len(sends) == 1


@pytest.mark.parametrize(
    "header,status",
    [
        ("Kostenlos, keine InMail-Guthaben mehr", "open_profile"),
        ("2 von 150 InMail-Guthaben verwenden", "unexpected_inmail_cost"),
        ("Neue Nachricht", "not_an_inmail_composer"),
    ],
)
def test_composer_flag_does_not_open_other_cases(header, status):
    credits = parse_credits(header)
    assert credit_refusal(credits, allow_open_profile=True) == status
    out, sends = _send(header, confirm=True, allow=True)
    assert out["status"] == status
    assert out["sent"] is False
    assert sends == []
    assert "open_profile" not in out or out["status"] == "open_profile"


def test_composer_paid_line_unmarked():
    out, sends = _send(PAID_LINE, confirm=True, allow=True)
    assert out["sent"] is True
    assert "open_profile" not in out


# -- the tool: flag passed through, ledger marks the row -------------------------


class _Reader:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.clicked = False

    async def inmail_target(self, username):
        return {"status": "ok", "name": "Dieter Köhler"}

    async def inmail(self, target, subject, body, confirm, allow_open_profile=False):
        self.calls.append((confirm, allow_open_profile))
        self.clicked = confirm
        return self.result


def _tool(monkeypatch, reader, **over):
    import linkedin_mcp_server.tools.ext as m
    import linkedin_mcp_server.tools.ext_inmail as mi

    async def fake_run(ctx, tool, body):
        return await body(object())

    monkeypatch.setattr(m, "_run", fake_run)
    monkeypatch.setattr(mi, "_reader", lambda ex: reader)
    mcp = FastMCP("t")
    m.register_ext_tools(mcp)
    args = {
        "linkedin_username": "dieter",
        "subject": "S",
        "body": "B",
        "confirm": True,
        **over,
    }

    async def go():
        async with Client(mcp) as c:
            return (await c.call_tool("send_inmail", args)).structured_content

    return asyncio.run(go())


def _rows():
    return [
        r
        for r in outreach.Ledger.default().latest_by_attempt().values()
        if r.get("kind") == "inmail"
    ]


def test_tool_default_does_not_ask_for_open_profile(monkeypatch):
    reader = _Reader(
        {
            "status": "open_profile",
            "sent": False,
            "credits": {"free": True, "cost": None},
        }
    )
    out = _tool(monkeypatch, reader)
    assert out["status"] == "open_profile"
    assert reader.calls == [(True, False)]
    rows = _rows()
    assert [r["status"] for r in rows] == ["not_sent"]
    # Not an Open Profile send: the row keeps its earlier shape.
    assert "open_profile" not in rows[0] and "free" not in rows[0]


def test_tool_flag_passed_and_ledger_marks_free(monkeypatch):
    reader = _Reader(
        {
            "status": "verified",
            "sent": True,
            "open_profile": True,
            "credits": {"free": True, "cost": None, "remaining": 150},
            "credits_after": {"remaining": 150},
            "credit_check": "measured",
            "credit_spent_despite_free": False,
        }
    )
    out = _tool(monkeypatch, reader, allow_open_profile=True)
    assert out["status"] == "verified"
    assert reader.calls == [(True, True)]
    (row,) = _rows()
    assert row["status"] == "verified"
    assert row["open_profile"] is True and row["free"] is True
    assert row["credit_spent_despite_free"] is False
    assert "credit_check" in row
    # One InMail per person still holds for a free one.
    again = _tool(monkeypatch, reader, allow_open_profile=True)
    assert again["status"] == "duplicate"


def test_tool_flag_keeps_salutation_check(monkeypatch):
    reader = _Reader({"status": "verified", "sent": True})
    out = _tool(
        monkeypatch, reader, allow_open_profile=True, body="Hallo Herr Sperling, Text"
    )
    assert out["status"] == "content_check_failed"
    assert reader.calls == []


def test_free_send_checked_against_inbox_balance():
    # The free line names no balance: the inbox read before the send is the
    # baseline, so a drop after a "free" send is reported.
    out, _ = _send(FREE_LINE, confirm=True, allow=True, balances=(150, 150))
    assert out["credit_check"] == "measured"
    assert out["credit_spent_despite_free"] is False
    out, _ = _send(FREE_LINE, confirm=True, allow=True, balances=(150, 149))
    assert out["credit_check"] == "measured"
    assert out["credit_spent_despite_free"] is True


def test_free_send_unknown_balance_is_said():
    out, _ = _send(FREE_LINE, confirm=True, allow=True, balances=())
    assert out["sent"] is True
    assert out["credit_check"] == "unavailable"
    assert out["credit_spent_despite_free"] is None


def test_free_without_panel_marker_stops_even_with_flag():
    # A quoted post saying "for free" with no recipient-panel marker must not
    # read as a proven Open Profile.
    out, sends = _send(
        "Neue Nachricht" + "\n" + "we offer this for free", confirm=True, allow=True
    )
    assert out["status"] == "open_profile"
    assert out["sent"] is False
    assert sends == []


def test_no_extra_balance_read_without_flag_or_dry_run():
    _send(PAID_LINE, confirm=True, allow=False)
    assert _send.page.balance_reads == 1  # only the existing read-back
    _send(FREE_LINE, confirm=False, allow=True)
    assert _send.page.balance_reads == 0


def test_flag_books_the_balance_read_on_the_pacer(monkeypatch):
    monkeypatch.setitem(outreach.PACE_BUDGETS, "page_read", {"day": 0, "week": 0})
    reader = _Reader({"status": "verified", "sent": True})
    out = _tool(monkeypatch, reader, allow_open_profile=True)
    assert out["status"] == "pace_budget_spent"
    assert reader.calls == []
    # Without the flag no extra page read is booked or refused.
    reader = _Reader({"status": "verified", "sent": True})
    assert _tool(monkeypatch, reader)["status"] == "verified"


def test_free_send_stops_when_inbox_shows_no_credits():
    out, sends = _send(FREE_LINE, confirm=True, allow=True, balances=(0, 0))
    assert out["status"] == "open_profile"
    assert sends == []
