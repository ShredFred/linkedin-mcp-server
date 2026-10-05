"""Every fork write tool answers before the tool deadline (upstream #1233).

FastMCP runs a tool inside ``anyio.fail_after()``. A deadline that lands
there discards the tool's answer; the client used to see only "Error calling
tool" while the ledger said unknown. Each write tool now runs its click under
the reply budget of message_sender (#1233) and answers itself:

- deadline before the click marker -> the tool's not-done status,
  retry_safe=true, the ledger row releases (no block, not counted);
- deadline after the marker -> unknown, retry_safe=false, blocking;
- a send confirmed but its read-back cut -> unverified, retry_safe=false;
- a cancellation the budget does not own still propagates and is booked by
  the same marker (before -> releasing, after -> unknown).

No browser, no network. The deadline is applied the way FastMCP applies it
(``anyio.fail_after`` around the tool function).
"""

from __future__ import annotations

import asyncio

import anyio
import pytest
from fastmcp import Client, FastMCP

from linkedin_mcp_server import ext_outreach as outreach

TEXT = "Guten Tag, kurze Frage zu Ihrer Gefügeanalyse. Beste Grüße"
DEADLINE = 0.6
RELEASING = {"not_sent", "not_posted", "not_done"}


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


class _Page:
    url = "https://www.linkedin.com/feed/"


class _Session:
    page = _Page()


class _Ex:
    ext_session = _Session()
    ext_navigator = None


def _with_extractor(monkeypatch, ex):
    import linkedin_mcp_server.tools.ext as m

    async def ready(ctx, tool_name=None):
        return ex

    monkeypatch.setattr(m, "get_ready_extractor", ready)


def _under_deadline(name, **kwargs):
    """Call a tool the way FastMCP does: inside anyio.fail_after."""
    fn = _tools()[name]

    async def go():
        with anyio.fail_after(DEADLINE):
            return await fn(ctx=None, **kwargs)

    return asyncio.run(go())


def _cancelled(name, **kwargs):
    fn = _tools()[name]
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(fn(ctx=None, **kwargs))


def _final(kind):
    rows = [
        r
        for r in outreach.Ledger.default().latest_by_attempt().values()
        if r.get("kind") == kind
    ]
    assert len(rows) == 1, rows
    return rows[0]["status"]


def _stall(owner, marker, phase):
    """An action that stalls before ('pre') or after ('post') its click."""

    async def act(*args, **kwargs):
        setattr(owner, marker, phase == "post")
        await anyio.Event().wait()

    return act


def _check(out, phase, not_done):
    assert out["deadline_reached"] is True
    if phase == "pre":
        assert out["status"] == not_done
        assert out["retry_safe"] is True
    else:
        assert out["status"] == "unknown"
        assert out["retry_safe"] is False


# -- message family: send_message ---------------------------------------------


def _msg_ex(phase):
    ex = _Ex()
    ex.message_submit_dispatched = False
    ex.send_message = _stall(ex, "message_submit_dispatched", phase)
    return ex


@pytest.mark.parametrize("phase", ["pre", "post"])
def test_send_message_deadline(monkeypatch, phase):
    _with_extractor(monkeypatch, _msg_ex(phase))
    out = _under_deadline(
        "send_message", linkedin_username="anna", message=TEXT, confirm_send=True
    )
    _check(out, phase, "not_sent")
    assert out["verified"] is False
    blocked = outreach.Ledger.default().already_contacted(
        "message", "anna", outreach.text_sha(TEXT)
    )
    assert _final("message") == ("not_sent" if phase == "pre" else "unknown")
    assert bool(blocked) is (phase == "post")


def test_send_message_readback_cut_is_unverified(monkeypatch):
    ex = _Ex()
    ex.message_submit_dispatched = False

    async def send_message(username, message, *, confirm_send):
        ex.message_submit_dispatched = True
        return {"sent": True, "url": "https://www.linkedin.com/messaging/thread/2-a/"}

    async def get_conversation(**lookup):
        await anyio.Event().wait()

    ex.send_message = send_message
    ex.get_conversation = get_conversation
    import linkedin_mcp_server.tools.ext as m

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(m.asyncio, "sleep", no_sleep)
    _with_extractor(monkeypatch, ex)
    out = _under_deadline(
        "send_message", linkedin_username="anna", message=TEXT, confirm_send=True
    )
    assert out["status"] == "unverified"
    assert out["retry_safe"] is False
    assert out["deadline_reached"] is True
    assert _final("message") == "unverified"


@pytest.mark.parametrize("dispatched,want", [(False, "not_sent"), (True, "unknown")])
def test_send_message_client_cancel_by_marker(monkeypatch, dispatched, want):
    ex = _Ex()
    ex.message_submit_dispatched = False

    async def send_message(username, message, *, confirm_send):
        ex.message_submit_dispatched = dispatched
        raise asyncio.CancelledError()

    ex.send_message = send_message
    _with_extractor(monkeypatch, ex)
    _cancelled(
        "send_message", linkedin_username="anna", message=TEXT, confirm_send=True
    )
    assert _final("message") == want


def test_send_message_through_fastmcp(monkeypatch):
    """End to end: the real FastMCP timeout, the client gets a status."""
    import linkedin_mcp_server.tools.ext as m

    _with_extractor(monkeypatch, _msg_ex("pre"))
    mcp = FastMCP("t")
    m.register_ext_tools(mcp, tool_timeout=DEADLINE)

    async def go():
        async with Client(mcp) as c:
            return await c.call_tool(
                "send_message",
                {"linkedin_username": "anna", "message": TEXT, "confirm_send": True},
                raise_on_error=False,
            )

    res = asyncio.run(asyncio.wait_for(go(), 15))
    assert not res.is_error
    assert res.structured_content["status"] == "not_sent"
    assert res.structured_content["retry_safe"] is True


def test_real_message_sender_marker_contract():
    """MessageSender exposes the marker the tools read, reset per call."""
    from linkedin_mcp_server.linkedin.message_sender import MessageSender

    class S:
        page = None

    sender = MessageSender(S(), None)
    assert sender.submit_dispatched is False


# -- send_campaign_batch --------------------------------------------------------


def test_batch_stops_before_a_pause_past_the_deadline(monkeypatch):
    import linkedin_mcp_server.tools.ext as m

    class Gap:
        @staticmethod
        def uniform(a, b):
            return 30.0

    monkeypatch.setattr(m, "random", Gap)
    ex = _Ex()

    async def send_message(username, message, *, confirm_send):
        return {
            "sent": True,
            "url": f"https://www.linkedin.com/messaging/thread/{username}/",
        }

    async def get_conversation(**lookup):
        return {"sections": {"c": TEXT}, "references": {}}

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(m.asyncio, "sleep", no_sleep)
    ex.send_message = send_message
    ex.get_conversation = get_conversation
    _with_extractor(monkeypatch, ex)
    outreach.Ledger.default().append(
        {
            "attempt": "canary",
            "kind": "message",
            "recipient": outreach.recipient_key(outreach.DEFAULT_CANARY),
            "text_sha": outreach.text_sha(TEXT),
            "status": "verified",
            "started_at": "2026-10-01T08:00:00+02:00",
        }
    )
    out = _under_deadline(
        "send_campaign_batch",
        message=TEXT,
        recipients=["anna", "bert"],
        campaign="dl",
        confirm_send=True,
        batch_size=2,
    )
    assert out["status"] == "stopped_on_deadline"
    assert out["remaining"] == ["bert"]
    assert [r["status"] for r in out["results"]] == ["verified"]


# -- connect_with_person ----------------------------------------------------------


@pytest.mark.parametrize("phase", ["pre", "post"])
def test_connect_deadline(monkeypatch, phase):
    ex = _Ex()
    ex.invite_send_clicked = False
    ex.connect_with_person = _stall(ex, "invite_send_clicked", phase)
    _with_extractor(monkeypatch, ex)
    out = _under_deadline(
        "connect_with_person", linkedin_username="anna", confirm_send=True
    )
    _check(out, phase, "not_sent")
    blocked = outreach.Ledger.default().already_contacted("invite", "anna", None)
    assert bool(blocked) is (phase == "post")


# -- create_post --------------------------------------------------------------------


@pytest.mark.parametrize("phase", ["pre", "post"])
def test_create_post_deadline(monkeypatch, phase):
    import linkedin_mcp_server.tools.ext as m

    class Composer:
        clicked = False

    composer = Composer()
    composer.create_post = _stall(composer, "clicked", phase)
    monkeypatch.setattr(m, "_composer", lambda ex: composer)
    _with_extractor(monkeypatch, _Ex())
    out = _under_deadline("create_post", text=TEXT, confirm_post=True)
    _check(out, phase, "not_posted")
    assert out["posted"] is False
    assert _final("post") == ("not_posted" if phase == "pre" else "unknown")


# -- comment_on_post (comment and reply share the path) ------------------------


@pytest.mark.parametrize("reply", [False, True])
@pytest.mark.parametrize("phase", ["pre", "post"])
def test_comment_deadline(monkeypatch, phase, reply):
    import linkedin_mcp_server.tools.ext_stage2 as s2

    class Actions:
        comment_submitted = False

    actions = Actions()
    actions.comment = _stall(actions, "comment_submitted", phase)
    actions.reply = _stall(actions, "comment_submitted", phase)
    monkeypatch.setattr(s2, "_actions", lambda ex: actions)
    _with_extractor(monkeypatch, _Ex())
    kwargs = {"post_url": "7300000000000000001", "text": TEXT, "confirm": True}
    if reply:
        kwargs["reply_to"] = "7300000000000000099"
    out = _under_deadline("comment_on_post", **kwargs)
    _check(out, phase, "not_posted")
    assert _final("comment") == ("not_posted" if phase == "pre" else "unknown")


# -- repost_post --------------------------------------------------------------------


@pytest.mark.parametrize("phase", ["pre", "post"])
def test_repost_deadline(monkeypatch, phase):
    import linkedin_mcp_server.tools.ext_stage2 as s2

    holder = {}

    class Fake:
        repost_clicked = False

        def __init__(self, session, navigator):
            holder["r"] = self
            self.repost = _stall(self, "repost_clicked", phase)

    monkeypatch.setattr(s2, "ExtReposter", Fake)
    _with_extractor(monkeypatch, _Ex())
    out = _under_deadline("repost_post", post_url="7300000000000000001", confirm=True)
    _check(out, phase, "not_done")
    assert out["done"] is False
    assert _final("repost") == ("not_done" if phase == "pre" else "unknown")


# -- withdraw_invitations -------------------------------------------------------------


@pytest.mark.parametrize("phase", ["pre", "post"])
def test_withdraw_deadline(monkeypatch, phase):
    import linkedin_mcp_server.tools.ext_stage2 as s2

    class Actions:
        withdraw_clicked = False

        async def sent_invitations_with_age(self, n):
            return [{"slug": "anna", "name": "Anna", "age_days": 30}]

    actions = Actions()
    actions.withdraw = _stall(actions, "withdraw_clicked", phase)
    monkeypatch.setattr(s2, "_actions", lambda ex: actions)
    _with_extractor(monkeypatch, _Ex())
    out = _under_deadline(
        "withdraw_invitations", confirm_withdraw=True, usernames=["anna"]
    )
    assert out["status"] == "stopped"
    _check(out["results"][-1], phase, "not_done")
    assert _final("withdraw") == ("not_done" if phase == "pre" else "unknown")


# -- send_inmail / edit_sent_message ----------------------------------------------------


@pytest.mark.parametrize("phase", ["pre", "post"])
def test_inmail_deadline(monkeypatch, phase):
    import linkedin_mcp_server.tools.ext_inmail as mi

    class Reader:
        clicked = False

        async def inmail_target(self, username):
            return {"status": "ok", "name": "Anna Muster"}

    reader = Reader()
    reader.inmail = _stall(reader, "clicked", phase)
    monkeypatch.setattr(mi, "_reader", lambda ex: reader)
    _with_extractor(monkeypatch, _Ex())
    out = _under_deadline(
        "send_inmail",
        subject="Gefügeanalyse",
        body="Guten Tag Frau Muster, kurze Frage zu Ihrem Labor. Beste Grüße",
        linkedin_username="anna",
        confirm=True,
    )
    _check(out, phase, "not_sent")
    blocked = outreach.Ledger.default().already_contacted("inmail", "anna", None)
    assert bool(blocked) is (phase == "post")


@pytest.mark.parametrize("phase", ["pre", "post"])
def test_edit_sent_message_deadline(monkeypatch, phase):
    import linkedin_mcp_server.tools.ext_inmail as mi

    class Reader:
        clicked = False

        async def thread_messages(self, url):
            return {
                "messages": [{"own": True, "index": 0, "text": "Alter Text hier"}],
                "partner": "anna",
            }

    reader = Reader()
    reader.edit = _stall(reader, "clicked", phase)
    monkeypatch.setattr(mi, "_reader", lambda ex: reader)
    _with_extractor(monkeypatch, _Ex())
    out = _under_deadline(
        "edit_sent_message",
        thread="2-abcdefghijkl",
        new_text="Neuer Text hier",
        confirm=True,
    )
    _check(out, phase, "not_sent")
    assert _final("message_edit") == ("not_sent" if phase == "pre" else "unknown")


# -- own content --------------------------------------------------------------------------


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
@pytest.mark.parametrize("phase", ["pre", "post"])
def test_own_content_deadline(monkeypatch, name, kind, extra, phase):
    import linkedin_mcp_server.tools.ext_own_content as oc

    class Reader:
        clicked = False

    reader = Reader()
    reader.delete = _stall(reader, "clicked", phase)
    reader.edit = _stall(reader, "clicked", phase)
    monkeypatch.setattr(oc, "_reader", lambda ex: reader)
    _with_extractor(monkeypatch, _Ex())
    out = _under_deadline(name, post_url="7300000000000000001", dry_run=False, **extra)
    _check(out, phase, "not_done")
    assert _final(kind) == ("not_done" if phase == "pre" else "unknown")


# -- the helper itself -----------------------------------------------------------------------


def test_no_time_left_does_not_start_the_action():
    from linkedin_mcp_server.tools.ext import _before_deadline, _DeadlineHit

    started = []

    async def act():
        started.append(1)
        return {}

    async def go():
        return await _before_deadline(act, lambda: True, budget=0.0)

    hit = asyncio.run(go())
    assert isinstance(hit, _DeadlineHit) and hit.clicked is False
    assert started == []


def test_unreadable_marker_fails_closed():
    from linkedin_mcp_server.tools.ext import _marker_set

    def boom():
        raise RuntimeError

    assert _marker_set(boom) is True
