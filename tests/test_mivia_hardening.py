"""MiViA fork hardening 2026-09-30: budget booking, error codes, idempotency.

No browser: ``_run`` is replaced by a direct call with a stub extractor.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client, FastMCP

from linkedin_mcp_server import mivia_outreach as outreach

COMPANY_URL = "https://www.linkedin.com/company/x/"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(outreach.LEDGER_ENV, str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv(outreach.NOTES_ENV, str(tmp_path / "notes.json"))


def _call(name, args, *, extractor=None, monkeypatch=None):
    import linkedin_mcp_server.tools.mivia as m

    if monkeypatch is not None:

        async def fake_run(ctx, tool, body):
            return await body(extractor)

        monkeypatch.setattr(m, "_run", fake_run)
    mcp = FastMCP("t")
    m.register_mivia_tools(mcp)

    async def go():
        async with Client(mcp) as c:
            return (await c.call_tool(name, args)).structured_content

    return asyncio.run(go())


def _used(action):
    return outreach.Pacer(outreach.Ledger.default()).state(action)["today"]


# -- 1. InMail budget is booked at the send, not at the call ------------------


class _Reader:
    def __init__(self, target, sent=None):
        self.target = target
        self.sent = sent
        self.calls = []

    async def inmail_target(self, username):
        return self.target

    async def inmail(self, target, subject, body, confirm):
        self.calls.append(confirm)
        return self.sent or {"status": "dry_run"}


def _inmail(monkeypatch, reader, **over):
    import linkedin_mcp_server.tools.mivia_inmail as mi

    monkeypatch.setattr(mi, "_reader", lambda ex: reader)
    args = {
        "linkedin_username": "dieter",
        "subject": "S",
        "body": "B",
        "confirm": True,
        **over,
    }
    return _call("send_inmail", args, extractor=object(), monkeypatch=monkeypatch)


@pytest.mark.parametrize(
    "status", ["first_degree", "open_profile", "no_inmail_credits"]
)
def test_inmail_abort_before_send_books_nothing(monkeypatch, status):
    reader = _Reader({"status": status, "name": "Dieter König"})
    assert _inmail(monkeypatch, reader)["status"] == status
    assert reader.calls == []
    assert _used("inmail") == 0


def test_inmail_salutation_abort_books_nothing(monkeypatch):
    reader = _Reader({"status": "ok", "name": "Dieter König"})
    out = _inmail(monkeypatch, reader, body="Hallo Herr Sperling, Text")
    assert out["status"] == "content_check_failed"
    assert _used("inmail") == 0


def test_inmail_real_send_books_once(monkeypatch):
    reader = _Reader(
        {"status": "ok", "name": "Dieter König"}, {"status": "verified", "sent": True}
    )
    assert _inmail(monkeypatch, reader)["status"] == "verified"
    assert reader.calls == [True]
    assert _used("inmail") == 1


def test_inmail_budget_rechecked_at_send(monkeypatch):
    # Spent between peek and send (a parallel call): refused, no attempt row.
    reader = _Reader(
        {"status": "ok", "name": "Dieter König"}, {"status": "verified", "sent": True}
    )

    async def target(username):
        monkeypatch.setitem(outreach.PACE_BUDGETS, "inmail", {"day": 0, "week": 0})
        return reader.target

    reader.inmail_target = target
    assert _inmail(monkeypatch, reader)["status"] == "pace_budget_spent"
    assert reader.calls == []
    assert not [
        r for r in outreach.Ledger.default().rows() if r.get("kind") == "inmail"
    ]


def test_peek_does_not_book():
    pacer = outreach.Pacer(outreach.Ledger.default())
    pacer.peek("profile_view")
    assert _used("profile_view") == 0
    pacer.take("profile_view", tool="t")
    assert _used("profile_view") == 1


# -- 2. clear codes instead of exceptions ---------------------------------------


@pytest.mark.parametrize(
    "name,args",
    [
        (
            "send_inmail",
            {"linkedin_username": COMPANY_URL, "subject": "S", "body": "B"},
        ),
        (
            "send_message_verified",
            {"linkedin_username": "", "message": "x", "confirm_send": False},
        ),
        (
            "send_message_verified",
            {"linkedin_username": COMPANY_URL, "message": "x", "confirm_send": False},
        ),
        ("connect_guarded", {"linkedin_username": "  ", "confirm_send": False}),
        ("set_contact_note", {"linkedin_username": COMPANY_URL, "note": "x"}),
        ("withdraw_invitations", {"usernames": [COMPANY_URL]}),
        ("invite_to_event", {"event_id": "1", "usernames": [""]}),
    ],
)
def test_invalid_recipient_is_a_status(name, args):
    assert _call(name, args)["status"] == "invalid_recipient"


def test_campaign_refuses_list_with_invalid_entry():
    out = _call(
        "send_campaign_batch",
        {
            "message": "Text",
            "recipients": ["dieter", COMPANY_URL],
            "campaign": "c",
            "confirm_send": False,
        },
    )
    assert out["status"] == "invalid_recipients"
    assert len(out["invalid"]) == 1


def test_campaign_requires_name():
    out = _call(
        "send_campaign_batch",
        {
            "message": "Text",
            "recipients": ["a"],
            "campaign": " ",
            "confirm_send": False,
        },
    )
    assert out["status"] == "campaign_required"


def test_campaign_dedupes_recipients():
    out = _call(
        "send_campaign_batch",
        {
            "message": "Text",
            "campaign": "c",
            "confirm_send": False,
            "recipients": [
                "dieter",
                "https://www.linkedin.com/in/dieter/",
                "Dieter",
                "jörg",
            ],
        },
    )
    assert out["status"] == "dry_run"
    assert [outreach.recipient_key(r) for r in out["remaining"]] == ["dieter", "jörg"]


@pytest.mark.parametrize(
    "note,status",
    [
        ("", "invalid_note"),
        ("Zeile\nzwei", "invalid_note"),
        ("x" * 301, "note_too_long"),
        ("Termin: https://calendly.com/mivia", "content_check_failed"),
    ],
)
def test_connect_note_checked_before_browser(note, status):
    out = _call(
        "connect_guarded",
        {"linkedin_username": "dieter", "confirm_send": True, "note": note},
    )
    assert out["status"] == status
    assert _used("invite") == 0


def test_connect_unicode_note_passes_dry_run():
    out = _call(
        "connect_guarded",
        {
            "linkedin_username": "jörg-müller",
            "confirm_send": False,
            "note": "Grüße aus Göttingen – Härtung?",
        },
    )
    assert out["status"] == "dry_run"


def test_withdraw_confirm_without_names_reads_nothing():
    out = _call("withdraw_invitations", {"confirm_withdraw": True})
    assert out["status"] == "nothing_selected"
    assert _used("page_read") == 0


# -- set_contact_note: limits, corrupt file, idempotency --------------------------


def test_contact_note_idempotent_and_unicode():
    a = _call(
        "set_contact_note",
        {"linkedin_username": "jörg", "tags": ["HK", "HK", " "], "note": "Härterei"},
    )
    b = _call(
        "set_contact_note",
        {"linkedin_username": "https://www.linkedin.com/in/jörg/", "tags": ["HK"]},
    )
    assert a["status"] == b["status"] == "saved"
    assert b["entry"]["tags"] == ["HK"]
    assert b["entry"]["note"] == "Härterei"


@pytest.mark.parametrize(
    "args,status",
    [
        ({"note": "x" * 1001}, "note_too_long"),
        ({"note": "a\rb"}, "invalid_note"),
        ({"tags": ["t" * 61]}, "tag_too_long"),
    ],
)
def test_contact_note_limits(args, status):
    assert (
        _call("set_contact_note", {"linkedin_username": "dieter", **args})["status"]
        == status
    )


def test_contact_note_corrupt_file_is_reported_not_overwritten(tmp_path):
    path = tmp_path / "notes.json"
    path.write_text("{kaputt", encoding="utf-8")
    out = _call("set_contact_note", {"linkedin_username": "dieter", "note": "x"})
    assert out["status"] == "notes_unreadable"
    assert path.read_text(encoding="utf-8") == "{kaputt"


# -- ledger robustness -------------------------------------------------------------


def test_torn_last_ledger_line_is_skipped(tmp_path):
    ledger = outreach.Ledger.default()
    ledger.append({"kind": "pace", "action": "page_read", "count": 1})
    with ledger.path.open("a", encoding="utf-8") as h:
        h.write('{"kind": "pace", "act')
    assert len(ledger.rows()) == 1
    assert _call("pace_status", {})["actions"]["page_read"]["today"] == 1


def test_corrupt_middle_ledger_line_refuses():
    ledger = outreach.Ledger.default()
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text(
        'garbage\n{"kind": "pace", "action": "x"}\n', encoding="utf-8"
    )
    with pytest.raises(outreach.LedgerCorrupt):
        ledger.rows()
