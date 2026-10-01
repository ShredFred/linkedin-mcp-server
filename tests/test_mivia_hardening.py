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


# -- 2026-09-30 round 2: ledger_corrupt status, undated rows, invite order ----


def _corrupt_ledger():
    ledger = outreach.Ledger.default()
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text('garbage\n{"kind": "pace"}\n', encoding="utf-8")


@pytest.mark.parametrize(
    "name,args",
    [
        ("outreach_quota", {}),
        ("pace_status", {}),
        ("connect_guarded", {"linkedin_username": "dieter", "confirm_send": False}),
        (
            "send_message_verified",
            {"linkedin_username": "dieter", "message": "Hallo", "confirm_send": False},
        ),
        ("follow_up_list", {}),
    ],
)
def test_corrupt_ledger_is_a_status_in_every_tool(name, args):
    _corrupt_ledger()
    out = _call(name, args)
    assert out["status"] == "ledger_corrupt"
    assert out["line"] == 1


def test_corrupt_ledger_inside_run_is_a_status(monkeypatch):
    import linkedin_mcp_server.tools.mivia as m

    async def fake_ready(ctx, tool_name):
        raise outreach.LedgerCorrupt(outreach.Ledger.default().path, 1)

    monkeypatch.setattr(m, "get_ready_extractor", fake_ready)
    out = _call("list_sent_invitations", {})
    assert out["status"] == "ledger_corrupt"


class _Conv:
    def __init__(self):
        self.calls = 0

    async def get_conversation(self, **kw):
        self.calls += 1
        return {"sections": {"conversation": ""}}


def test_follow_up_list_skips_undated_rows(monkeypatch):
    import json

    ledger = outreach.Ledger.default()
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"attempt": "a", "kind": "message", "recipient": "ohne", "status": "sent"},
        {
            "at": "kaputt",
            "attempt": "b",
            "kind": "message",
            "recipient": "kaputt",
            "status": "sent",
        },
        {
            "at": "2026-09-01T10:00:00+02:00",
            "attempt": "c",
            "kind": "message",
            "recipient": "gut",
            "status": "sent",
        },
    ]
    ledger.path.write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    import linkedin_mcp_server.tools.mivia_stage2 as s2

    conv = _Conv()

    async def fake_run(ctx, tool, body):
        return await body(conv)

    monkeypatch.setattr(s2, "_run", fake_run)
    out = _call("follow_up_list", {}, extractor=conv, monkeypatch=monkeypatch)
    assert [e["recipient"] for e in out["entries"]] == ["gut"]
    assert out["skipped_undated"] == 2
    assert conv.calls == 1


class _Invite:
    def __init__(self):
        self.budget_at_call = None

    async def connect_with_person(self, username, note=None):
        self.budget_at_call = _used("invite")
        return {"status": "connected"}


def test_invite_budget_booked_before_browser(monkeypatch):
    ex = _Invite()
    out = _call(
        "connect_guarded",
        {"linkedin_username": "dieter", "confirm_send": True},
        extractor=ex,
        monkeypatch=monkeypatch,
    )
    assert out["result"]["status"] == "connected"
    # deliberate: an attempt that dies mid-dialog may have sent and must count
    assert ex.budget_at_call == 1


@pytest.mark.parametrize(
    "env,length,ok",
    [
        (None, 200, True),
        (None, 201, False),
        ("300", 300, True),
        ("300", 301, False),
        ("999", 301, False),
        ("abc", 201, False),
    ],
)
def test_invite_note_limit_default_200_configurable(monkeypatch, env, length, ok):
    import linkedin_mcp_server.tools.mivia as m

    if env is None:
        monkeypatch.delenv(m.INVITE_NOTE_MAX_ENV, raising=False)
    else:
        monkeypatch.setenv(m.INVITE_NOTE_MAX_ENV, env)
    out = m.check_invite_note("x" * length)
    if ok:
        assert out is None or out["status"] != "note_too_long"
    else:
        assert out["status"] == "note_too_long"


# -- 2026-09-30 round 3: every registered mivia tool sits behind the guard ----


def _registered_tools():
    import linkedin_mcp_server.tools.mivia as m

    mcp = FastMCP("t")
    m.register_mivia_tools(mcp)
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


_TOOLS = _registered_tools()


def test_registry_is_not_empty():
    assert len(_TOOLS) >= 25


@pytest.mark.parametrize("name", sorted(_TOOLS))
def test_every_tool_maps_ledger_corrupt_to_status(name):
    import linkedin_mcp_server.tools.mivia as m

    fn = _TOOLS[name].fn
    assert getattr(fn, "_mivia_ledger_guarded", False), (
        f"{name} not behind _ledger_guard"
    )

    async def boom(*a, **k):
        raise outreach.LedgerCorrupt(outreach.Ledger.default().path, 7)

    out = asyncio.run(m._ledger_guard(boom)())
    assert out["status"] == "ledger_corrupt" and out["line"] == 7


# -- 2026-10-01: six review findings ------------------------------------------


def test_append_after_torn_last_line_starts_a_new_line():
    ledger = outreach.Ledger.default()
    ledger.append({"kind": "pace", "action": "page_read", "count": 1})
    with ledger.path.open("a", encoding="utf-8") as h:
        h.write('{"kind": "pace", "act')
    ledger.append({"kind": "pace", "action": "search", "count": 1})
    assert [r["action"] for r in ledger.rows()] == ["page_read", "search"]
    # real corruption in the middle still refuses
    with ledger.path.open("a", encoding="utf-8") as h:
        h.write("garbage\n")
    ledger.append({"kind": "pace", "action": "like", "count": 1})
    with pytest.raises(outreach.LedgerCorrupt):
        ledger.rows()


def _register(monkeypatch, extractor):
    import linkedin_mcp_server.tools.mivia as m

    async def fake_run(ctx, tool, body):
        return await body(extractor)

    async def no_sleep(*a, **k):
        return None

    monkeypatch.setattr(m, "_run", fake_run)
    monkeypatch.setattr(m.asyncio, "sleep", no_sleep)
    mcp = FastMCP("t")
    m.register_mivia_tools(mcp)
    return mcp


def _gather(mcp, name, args, n=2):
    async def go():
        async with Client(mcp) as c:
            outs = await asyncio.gather(*(c.call_tool(name, args) for _ in range(n)))
            return [o.structured_content for o in outs]

    return asyncio.run(go())


def _attempted(kind):
    return [
        r
        for r in outreach.Ledger.default().rows()
        if r.get("kind") == kind and r.get("status") == "attempted"
    ]


def _first_two_checks_free(monkeypatch):
    """Every pre-lock check passes, as when two calls race; only the re-check
    under the pacer lock (called from the duplicate lambda) is real."""
    import sys

    real = outreach.Ledger.already_contacted

    def check(self, kind, recipient, sha):
        if sys._getframe(1).f_code.co_name != "<lambda>":
            return None
        return real(self, kind, recipient, sha)

    monkeypatch.setattr(outreach.Ledger, "already_contacted", check)


class _Sender:
    def __init__(self):
        self.sends = 0
        self._mivia_session = type("S", (), {"page": None})()

    async def send_message(self, username, message, confirm_send):
        self.sends += 1
        return {"sent": True, "url": "https://www.linkedin.com/messaging/thread/T1/"}

    async def get_conversation(self, **kw):
        return {"sections": {"conversation": "Hallo Dieter"}}


def test_parallel_message_sends_once(monkeypatch):
    ex = _Sender()
    mcp = _register(monkeypatch, ex)
    _first_two_checks_free(monkeypatch)
    outs = _gather(
        mcp,
        "send_message_verified",
        {
            "linkedin_username": "dieter",
            "message": "Hallo Dieter",
            "confirm_send": True,
        },
    )
    assert sorted(o["status"] for o in outs) == ["duplicate", "verified"]
    assert len(_attempted("message")) == 1
    assert ex.sends == 1


class _Connector:
    def __init__(self, raw="connected"):
        self.calls = 0
        self.raw = raw

    async def connect_with_person(self, username, note=None):
        self.calls += 1
        return {"status": self.raw}


def test_parallel_invites_send_once(monkeypatch):
    ex = _Connector()
    mcp = _register(monkeypatch, ex)
    _first_two_checks_free(monkeypatch)
    outs = _gather(
        mcp, "connect_guarded", {"linkedin_username": "dieter", "confirm_send": True}
    )
    assert sorted(o.get("status", "sent") for o in outs) == ["duplicate", "sent"]
    assert len(_attempted("invite")) == 1
    assert ex.calls == 1


def test_connect_unknown_raw_status_blocks(monkeypatch):
    out = _call(
        "connect_guarded",
        {"linkedin_username": "dieter", "confirm_send": True},
        extractor=_Connector(raw="weird_new_state"),
        monkeypatch=monkeypatch,
    )
    assert out["result"]["status"] == "weird_new_state"
    previous = outreach.Ledger.default().already_contacted("invite", "dieter", None)
    assert previous["status"] == "unknown"


class _InboxGuess:
    """Send without thread URL; the newest inbox thread belongs to *partner*."""

    def __init__(self, partner):
        self.partner = partner
        self._mivia_session = type("S", (), {"page": None})()

    async def send_message(self, username, message, confirm_send):
        return {"sent": True, "url": "https://www.linkedin.com/messaging/compose/"}

    async def get_inbox(self, limit):
        thread = "https://www.linkedin.com/messaging/thread/A1/"
        return {"references": {"inbox": [{"kind": "conversation", "url": thread}]}}

    async def get_conversation(self, thread_id=None, linkedin_username=None):
        if linkedin_username:
            raise RuntimeError("lookup by name failed")
        return {
            "sections": {
                "conversation": "Profil von Ich anzeigen\nHallo Bernd, wie geht es?"
            },
            "references": {
                "conversation": [
                    {
                        "kind": "person",
                        "url": f"https://www.linkedin.com/in/{self.partner}/",
                    }
                ]
            },
        }


@pytest.mark.parametrize(
    "partner,status", [("anna", "unverified"), ("bernd", "verified")]
)
def test_inbox_fallback_checks_partner(monkeypatch, partner, status):
    _register(monkeypatch, None)  # patches asyncio.sleep
    out = _call(
        "send_message_verified",
        {
            "linkedin_username": "bernd",
            "message": "Hallo Bernd, wie geht es?",
            "confirm_send": True,
        },
        extractor=_InboxGuess(partner),
        monkeypatch=monkeypatch,
    )
    assert out["status"] == status


class _Loc:
    """Minimal locator stand-in for MiviaInmail.inmail."""

    def __init__(self, page):
        self.page = page
        self.value = ""

    first = property(lambda self: self)
    last = property(lambda self: self)

    def locator(self, *a, **k):
        return self

    def filter(self, *a, **k):
        return self

    async def count(self):
        return 1

    async def wait_for(self, **k):
        return None

    async def click(self):
        if self is self.page.send:
            self.page.clicked = True

    async def is_disabled(self):
        return False

    async def fill(self, value):
        self.value = value

    async def input_value(self):
        return self.value

    async def inner_text(self):
        if self.page.clicked:
            raise TimeoutError("read-back timed out")
        return "Header"


class _Page:
    def __init__(self):
        self.clicked = False
        self.url = "https://www.linkedin.com/sales/inbox/x"
        self.send = _Loc(self)
        self.subject = _Loc(self)
        self.body = _Loc(self)
        self.dialog = _Loc(self)

    def locator(self, sel, *a, **k):
        return self.body if "textarea" in str(sel) else self.dialog

    async def evaluate(self, *a, **k):
        return "2nd"


def _fake_inmail_reader(monkeypatch):
    import linkedin_mcp_server.scraping.mivia_inmail as sm

    page = _Page()
    reader = sm.MiviaInmail.__new__(sm.MiviaInmail)

    async def noop(*a, **k):
        return None

    async def fake_first(scope, chain, *, last=False):
        return page.send if last else page.subject

    reader._wait = noop
    reader._goto = noop
    monkeypatch.setattr(sm.MiviaInmail, "_page", property(lambda s: page))
    monkeypatch.setattr(sm, "first_match", fake_first)
    monkeypatch.setattr(sm, "parse_degree", lambda t: 2)
    monkeypatch.setattr(
        sm,
        "parse_credits",
        lambda t: {"free": False, "none_left": False, "cost": 1, "remaining": 5},
    )
    return reader


def test_inmail_readback_error_after_click_is_unverified(monkeypatch):
    reader = _fake_inmail_reader(monkeypatch)
    target = {"status": "ok", "name": "Dieter Maier", "sales_url": "https://x/"}

    class _Wrap:
        async def inmail_target(self, username):
            return target

        async def inmail(self, target, subject, body, confirm):
            return await reader.inmail(target, subject, body, confirm=confirm)

    out = _inmail(monkeypatch, _Wrap())
    assert out["status"] == "unverified"
    assert out["sent"] is True
    assert "TimeoutError" in out["verify_error"]
    rows = [r for r in outreach.Ledger.default().rows() if r.get("attempt")]
    assert [r["status"] for r in rows] == ["attempted", "unverified"]


def test_pace_lock_busy_is_a_status(monkeypatch):
    import linkedin_mcp_server.tools.mivia as m

    ledger = outreach.Ledger.default()
    lock = ledger.path.with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("held", encoding="utf-8")  # fresh, so not stale
    real = outreach._file_lock
    monkeypatch.setattr(
        outreach, "_file_lock", lambda path, **k: real(path, timeout=0.1)
    )
    assert m._pace("page_read", tool="t")["status"] == "pace_lock_busy"
    out = m._book_attempt(
        ledger, "message", {"attempt": "a", "kind": "message"}, tool="t"
    )
    assert out["status"] == "pace_lock_busy"
    assert ledger.rows() == []


def test_stale_lock_unlink_permission_error_keeps_waiting(tmp_path, monkeypatch):
    import os
    import time

    lock = tmp_path / "x.lock"
    lock.write_text("old", encoding="utf-8")
    old = time.time() - 3600
    os.utime(lock, (old, old))
    calls = {"n": 0}
    real_unlink = type(lock).unlink

    def flaky_unlink(self, missing_ok=False):
        if self == lock and calls["n"] == 0:
            calls["n"] += 1
            raise PermissionError("in use")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(type(lock), "unlink", flaky_unlink)
    with outreach._file_lock(lock, timeout=2.0):
        assert lock.exists()
    assert calls["n"] == 1
    assert not lock.exists()
