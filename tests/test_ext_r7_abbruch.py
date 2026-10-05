"""R7 Abbruch: FastMCP cancels a tool via anyio.fail_after with CancelledError.

CancelledError is a BaseException, so every ``except Exception`` lets it
through. For each writing fork tool this checks what the ledger holds when
the cancellation lands (a) before the click, (b) right after the click and
(c) during the read-back, and that the cancellation is re-raised.

Expected: (a) no open booking -- either no row at all, or a releasing outcome
(not_sent / not_posted / not_done); where the tool cannot tell (a) from (b)
it fails closed. (b)/(c) never end as a releasing status. No row may stay at
a bare "attempted" either: it blocks forever, but says nothing.

No browser, no network.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest
from fastmcp import Client, FastMCP

from linkedin_mcp_server import ext_outreach as outreach

RELEASING = {"not_sent", "not_posted", "not_done"}
TEXT = "Guten Tag, kurze Frage zu Ihrer Gefügeanalyse. Beste Grüße"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(outreach.LEDGER_ENV, str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv(outreach.NOTES_ENV, str(tmp_path / "notes.json"))
    import linkedin_mcp_server.tools.ext as m
    import linkedin_mcp_server.tools.ext_stage2 as s2

    class _NoWait:
        @staticmethod
        def uniform(a, b):
            return 0.0

    monkeypatch.setattr(m, "random", _NoWait)
    monkeypatch.setattr(s2, "random", _NoWait)


class _Capture:
    """Stands in for FastMCP: keeps the decorated tool functions by name."""

    def __init__(self):
        self.fns = {}

    def tool(self, *args, **kwargs):
        def register(fn):
            self.fns[fn.__name__] = fn
            return fn

        return register

    def __getattr__(self, name):
        return lambda *a, **k: lambda fn: fn


def _tools():
    from linkedin_mcp_server.tools.ext import register_ext_tools
    from linkedin_mcp_server.tools.ext_inmail import register_ext_inmail_tools
    from linkedin_mcp_server.tools.ext_own_content import (
        register_ext_own_content_tools,
    )
    from linkedin_mcp_server.tools.ext_stage2 import register_ext_stage2_tools

    cap = _Capture()
    for reg in (
        register_ext_tools,
        register_ext_stage2_tools,
        register_ext_inmail_tools,
        register_ext_own_content_tools,
    ):
        reg(cap)
    return cap.fns


def _with_extractor(monkeypatch, ex):
    import linkedin_mcp_server.tools.ext as m

    async def ready(ctx, tool_name=None):
        if isinstance(ex, BaseException):
            raise ex
        return ex

    monkeypatch.setattr(m, "get_ready_extractor", ready)


def _cancelled(name, **kwargs):
    fn = _tools()[name]
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(fn(ctx=None, **kwargs))


def _attempts(kind):
    return [
        r
        for r in outreach.Ledger.default().latest_by_attempt().values()
        if r.get("kind") == kind
    ]


def _final(kind):
    rows = _attempts(kind)
    assert len(rows) == 1, rows
    assert rows[0]["status"] != "attempted", "bare attempted row left behind"
    return rows[0]["status"]


class _Page:
    url = "https://www.linkedin.com/feed/"


class _Session:
    page = _Page()


class _Ex:
    ext_session = _Session()
    ext_navigator = None


# -- (a0) cancelled before the tool reached the browser -------------------------


@pytest.mark.parametrize(
    "name,kwargs,kind",
    [
        (
            "send_message",
            {"linkedin_username": "anna", "message": TEXT, "confirm_send": True},
            "message",
        ),
        (
            "connect_with_person",
            {"linkedin_username": "anna", "confirm_send": True},
            "invite",
        ),
        ("create_post", {"text": TEXT, "confirm_post": True}, "post"),
        (
            "comment_on_post",
            {"post_url": "7300000000000000001", "text": TEXT, "confirm": True},
            "comment",
        ),
    ],
)
def test_cancel_before_browser_books_nothing(monkeypatch, name, kwargs, kind):
    _with_extractor(monkeypatch, asyncio.CancelledError())
    _cancelled(name, **kwargs)
    assert _attempts(kind) == []


# -- send_message / send_campaign_batch --------------------------------


def _msg_ex(phase):
    ex = _Ex()

    async def send_message(username, message, *, confirm_send):
        if phase == "b":
            raise asyncio.CancelledError()
        return {"sent": True, "url": "https://www.linkedin.com/messaging/thread/2-abc/"}

    async def get_conversation(**lookup):
        raise asyncio.CancelledError()

    async def get_inbox(n):
        return {}

    ex.send_message = send_message
    ex.get_conversation = get_conversation
    ex.get_inbox = get_inbox
    return ex


@pytest.mark.parametrize("phase,want", [("b", "unknown"), ("c", "unverified")])
def test_send_message(monkeypatch, phase, want):
    import linkedin_mcp_server.tools.ext as m

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(m.asyncio, "sleep", no_sleep)
    _with_extractor(monkeypatch, _msg_ex(phase))
    _cancelled(
        "send_message",
        linkedin_username="anna",
        message=TEXT,
        confirm_send=True,
    )
    assert _final("message") == want
    # Blocks the same text to the same person and counts against the cap.
    assert outreach.Ledger.default().already_contacted(
        "message", "anna", outreach.text_sha(TEXT)
    )


def test_send_message_cancel_in_sleep_before_readback(monkeypatch):
    """(c) at its first await: the pause before the read-back."""
    import linkedin_mcp_server.tools.ext as m

    async def cancel_sleep(_s):
        raise asyncio.CancelledError()

    monkeypatch.setattr(m.asyncio, "sleep", cancel_sleep)
    _with_extractor(monkeypatch, _msg_ex("c"))
    _cancelled(
        "send_message",
        linkedin_username="anna",
        message=TEXT,
        confirm_send=True,
    )
    assert _final("message") == "unverified"


def test_campaign_batch_cancel_keeps_earlier_rows(monkeypatch):
    import linkedin_mcp_server.tools.ext as m

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(m.asyncio, "sleep", no_sleep)
    calls = []
    ex = _Ex()

    async def send_message(username, message, *, confirm_send):
        calls.append(username)
        if username == "bert":
            raise asyncio.CancelledError()
        return {
            "sent": True,
            "url": f"https://www.linkedin.com/messaging/thread/{username}/",
        }

    async def get_conversation(**lookup):
        return {"sections": {"c": message_block()}, "references": {}}

    def message_block():
        return TEXT

    ex.send_message = send_message
    ex.get_conversation = get_conversation
    ex.get_inbox = lambda n: {}
    _with_extractor(monkeypatch, ex)
    led = outreach.Ledger.default()
    led.append(
        {
            "attempt": "canary",
            "kind": "message",
            "recipient": outreach.recipient_key(outreach.DEFAULT_CANARY),
            "text_sha": outreach.text_sha(TEXT),
            "status": "verified",
            "started_at": "2026-10-01T08:00:00+02:00",
        }
    )
    _cancelled(
        "send_campaign_batch",
        message=TEXT,
        recipients=["anna", "bert", "carl"],
        campaign="r7",
        confirm_send=True,
        batch_size=3,
    )
    by = {
        r["recipient"]: r["status"]
        for r in _attempts("message")
        if r["recipient"] != outreach.recipient_key(outreach.DEFAULT_CANARY)
    }
    assert by == {"anna": "verified", "bert": "unknown"}
    assert calls == ["anna", "bert"]


# -- connect_with_person ---------------------------------------------------------------


@pytest.mark.parametrize(
    "phase,want", [("a", "not_sent"), ("b", "unknown"), ("c", "unknown")]
)
def test_connect_with_person(monkeypatch, phase, want):
    ex = _Ex()
    ex.invite_send_clicked = False

    async def connect_with_person(username, note=None):
        ex.invite_send_clicked = phase != "a"
        raise asyncio.CancelledError()

    ex.connect_with_person = connect_with_person
    _with_extractor(monkeypatch, ex)
    _cancelled("connect_with_person", linkedin_username="anna", confirm_send=True)
    assert _final("invite") == want
    blocked = outreach.Ledger.default().already_contacted("invite", "anna", None)
    assert bool(blocked) is (want not in RELEASING)


# -- create_post ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "phase,want", [("a", "not_posted"), ("b", "unknown"), ("c", "unknown")]
)
def test_create_post(monkeypatch, phase, want):
    import linkedin_mcp_server.tools.ext as m

    class Composer:
        clicked = False

        async def create_post(self, text, image_path=None, confirm_post=False):
            self.clicked = phase != "a"
            raise asyncio.CancelledError()

    monkeypatch.setattr(m, "_composer", lambda ex: Composer())
    _with_extractor(monkeypatch, _Ex())
    _cancelled("create_post", text=TEXT, confirm_post=True)
    assert _final("post") == want


# -- comment_on_post ---------------------------------------------------------------


@pytest.mark.parametrize(
    "phase,want", [("a", "not_posted"), ("b", "unknown"), ("c", "unknown")]
)
def test_comment_on_post(monkeypatch, phase, want):
    import linkedin_mcp_server.tools.ext_stage2 as s2

    class Actions:
        async def comment(self, activity_id, text, confirm):
            self.comment_submitted = phase != "a"
            raise asyncio.CancelledError()

    monkeypatch.setattr(s2, "_actions", lambda ex: Actions())
    _with_extractor(monkeypatch, _Ex())
    _cancelled(
        "comment_on_post", post_url="7300000000000000001", text=TEXT, confirm=True
    )
    assert _final("comment") == want
    if want in RELEASING:
        # The text and the post are free again for a retry.
        class Ok:
            async def comment(self, activity_id, text, confirm):
                return {"posted": True, "status": "posted"}

        monkeypatch.setattr(s2, "_actions", lambda ex: Ok())
        out = asyncio.run(
            _tools()["comment_on_post"](
                ctx=None, post_url="7300000000000000001", text=TEXT, confirm=True
            )
        )
        assert out["status"] == "posted"


def test_comment_reader_without_marker_fails_closed(monkeypatch):
    import linkedin_mcp_server.tools.ext_stage2 as s2

    class Actions:
        def __setattr__(self, name, value):  # refuses the marker
            pass

        async def comment(self, activity_id, text, confirm):
            raise asyncio.CancelledError()

    monkeypatch.setattr(s2, "_actions", lambda ex: Actions())
    _with_extractor(monkeypatch, _Ex())
    _cancelled(
        "comment_on_post", post_url="7300000000000000001", text=TEXT, confirm=True
    )
    assert _final("comment") == "unknown"


# -- withdraw_invitations ----------------------------------------------------------


@pytest.mark.parametrize("phase", ["a", "b", "c"])
def test_withdraw(monkeypatch, phase):
    import linkedin_mcp_server.tools.ext_stage2 as s2

    class Actions:
        async def sent_invitations_with_age(self, n):
            return [{"slug": "anna", "name": "Anna", "age_days": 30}]

        async def withdraw(self, name, slug):
            raise asyncio.CancelledError()

    monkeypatch.setattr(s2, "_actions", lambda ex: Actions())
    _with_extractor(monkeypatch, _Ex())
    _cancelled(
        "withdraw_invitations",
        confirm_withdraw=True,
        usernames=["anna"],
    )
    # No click marker in ExtActions.withdraw: every phase fails closed and
    # books one withdraw unit (budget only; withdraw blocks no person).
    assert _final("withdraw") == "unknown"


# -- send_inmail / edit_sent_message ----------------------------------------------


@pytest.mark.parametrize(
    "phase,want", [("a", "not_sent"), ("b", "unknown"), ("c", "unknown")]
)
def test_send_inmail(monkeypatch, phase, want):
    import linkedin_mcp_server.tools.ext_inmail as mi

    class Reader:
        clicked = False

        async def inmail_target(self, username):
            return {"status": "ok", "name": "Anna Muster"}

        async def inmail(self, target, subject, body, confirm):
            self.clicked = phase != "a"
            raise asyncio.CancelledError()

    monkeypatch.setattr(mi, "_reader", lambda ex: Reader())
    _with_extractor(monkeypatch, _Ex())
    _cancelled(
        "send_inmail",
        subject="Gefügeanalyse",
        body="Guten Tag Frau Muster, kurze Frage zu Ihrem Labor. Beste Grüße",
        linkedin_username="anna",
        confirm=True,
    )
    assert _final("inmail") == want
    blocked = outreach.Ledger.default().already_contacted("inmail", "anna", None)
    assert bool(blocked) is (want not in RELEASING)


@pytest.mark.parametrize(
    "phase,want", [("a", "not_sent"), ("b", "unknown"), ("c", "unknown")]
)
def test_edit_sent_message(monkeypatch, phase, want):
    import linkedin_mcp_server.tools.ext_inmail as mi

    class Reader:
        clicked = False

        async def thread_messages(self, url):
            return {
                "messages": [{"own": True, "index": 0, "text": "Alter Text hier"}],
                "partner": "anna",
            }

        async def edit(self, url, message, new_text, confirm):
            self.clicked = phase != "a"
            raise asyncio.CancelledError()

    monkeypatch.setattr(mi, "_reader", lambda ex: Reader())
    _with_extractor(monkeypatch, _Ex())
    _cancelled(
        "edit_sent_message",
        thread="2-abcdefghijkl",
        new_text="Neuer Text hier",
        confirm=True,
    )
    assert _final("message_edit") == want


# -- own content -------------------------------------------------------------------


_OWN = [
    ("delete_own_post", "post_delete", {}),
    ("edit_own_post", "post_edit", {"new_text": "Neuer Beitragstext"}),
    ("delete_own_comment", "comment_delete", {"comment_id": "7300000000000000099"}),
    (
        "edit_own_comment",
        "comment_edit",
        {"comment_id": "7300000000000000099", "new_text": "Neuer Kommentar"},
    ),
]


@pytest.mark.parametrize("name,kind,extra", _OWN)
@pytest.mark.parametrize(
    "phase,want", [("a", "not_done"), ("b", "unknown"), ("c", "unknown")]
)
def test_own_content(monkeypatch, name, kind, extra, phase, want):
    import linkedin_mcp_server.tools.ext_own_content as oc

    class Reader:
        clicked = False

        async def delete(self, activity, comment, confirm):
            self.clicked = phase != "a"
            raise asyncio.CancelledError()

        async def edit(self, activity, comment, new_text, confirm):
            self.clicked = phase != "a"
            raise asyncio.CancelledError()

    monkeypatch.setattr(oc, "_reader", lambda ex: Reader())
    _with_extractor(monkeypatch, _Ex())
    _cancelled(name, post_url="7300000000000000001", dry_run=False, **extra)
    assert _final(kind) == want


# -- invite_to_event / set_contact_note -------------------------------------------


def test_invite_to_event_books_no_invite(monkeypatch):
    import linkedin_mcp_server.tools.ext_stage2 as s2

    class Actions:
        async def event_capabilities(self, event_id):
            raise asyncio.CancelledError()

    monkeypatch.setattr(s2, "_actions", lambda ex: Actions())
    _with_extractor(monkeypatch, _Ex())
    _cancelled(
        "invite_to_event",
        event_id="7300000000000000001",
        usernames=["anna"],
        confirm_send=True,
    )
    # The dialog is not automated: only the page_read pace row, no attempt.
    rows = outreach.Ledger.default().rows()
    assert all(r.get("kind") == "pace" for r in rows)


def test_set_contact_note_has_no_cancellation_point():
    """No await inside: a cancellation can only land before it starts."""
    src = inspect.getsource(_tools()["set_contact_note"])
    assert "await " not in src


# -- end to end: the real FastMCP timeout ------------------------------------------


def test_real_tool_timeout_books_unknown_after_click(monkeypatch):
    """anyio.fail_after in FastMCP cancels the tool; the ledger still closes."""
    import linkedin_mcp_server.tools.ext as m

    ex = _Ex()
    ex.invite_send_clicked = False

    async def connect_with_person(username, note=None):
        ex.invite_send_clicked = True
        await asyncio.sleep(30)

    ex.connect_with_person = connect_with_person
    _with_extractor(monkeypatch, ex)
    mcp = FastMCP("t")
    m.register_ext_tools(mcp, tool_timeout=0.3)

    async def go():
        async with Client(mcp) as c:
            return await c.call_tool(
                "connect_with_person",
                {"linkedin_username": "anna", "confirm_send": True},
                raise_on_error=False,
            )

    res = asyncio.run(asyncio.wait_for(go(), 15))
    assert res.is_error
    assert _final("invite") == "unknown"
