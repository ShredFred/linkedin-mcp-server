"""fork R5: whole-day flow simulation over the real tool functions.

A fake extractor stands in for the browser; ledger, pacer, duplicate check and
the tools themselves are the real code. The invariants checked across every
scenario:

* nobody receives the same text twice (counted at the fake send_message),
* the daily cap and the pacer budget are never exceeded,
* a pure read failure or a pre-click failure never blocks a person for good.

No browser, no network, no sleeps.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import datetime, timedelta

import pytest
from fastmcp import Client, FastMCP

from linkedin_mcp_server import ext_outreach as outreach

TEXT = "Guten Tag, kurze Frage zu Ihrer Gefügeanalyse. Beste Grüße"
CANARY = outreach.DEFAULT_CANARY


class _NoWait:
    @staticmethod
    def uniform(a, b):
        return 0.0


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(outreach.LEDGER_ENV, str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv(outreach.NOTES_ENV, str(tmp_path / "notes.json"))
    import linkedin_mcp_server.tools.ext as m
    import linkedin_mcp_server.tools.ext_stage2 as s2

    monkeypatch.setattr(m, "random", _NoWait)
    monkeypatch.setattr(s2, "random", _NoWait)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(m.asyncio, "sleep", no_sleep)


class _Page:
    url = "https://www.linkedin.com/feed/"


class _Session:
    page = _Page()


class FakeLinkedIn:
    """Records every send; behaviour per recipient, consumed one call at a time.

    ``plan[user]`` is a list of outcomes for successive send attempts:
    ``ok``, ``raise`` (before the click), ``notsent`` (retry_safe),
    ``unconfirmed`` (may have left), ``readfail`` (sent, read-back fails).
    """

    ext_session = _Session()

    def __init__(self, plan=None):
        self.plan = {k: list(v) for k, v in (plan or {}).items()}
        self.delivered: Counter[str] = Counter()
        self.threads: dict[str, str] = {}
        self.read_fail: set[str] = set()
        self.replies: dict[str, str] = {}
        self.conversation_reads = 0

    def _next(self, user):
        steps = self.plan.get(user)
        return steps.pop(0) if steps else "ok"

    async def send_message(self, username, message, *, confirm_send):
        step = self._next(username)
        if step == "raise":
            raise RuntimeError("profile page did not load")
        if step == "notsent":
            return {"sent": False, "retry_safe": True, "status": "composer_missing"}
        self.delivered[username] += 1
        self.threads[username] = message
        if step == "unconfirmed":
            return {"sent": False, "retry_safe": False, "status": "send_unconfirmed"}
        if step == "readfail":
            self.read_fail.add(username)
        return {"sent": True, "url": f"/messaging/thread/T-{username}/"}

    async def get_inbox(self, limit):
        return {"references": {"inbox": []}}

    async def get_conversation(self, thread_id=None, linkedin_username=None):
        self.conversation_reads += 1
        user = (thread_id or "").removeprefix("T-") or linkedin_username
        if user in self.read_fail:
            raise TimeoutError("thread did not load")
        text = "Profil von Jane Doe anzeigen\n" + self.threads.get(user, "")
        if user in self.replies:
            text += f"\nProfil von {user.title()} anzeigen\n{self.replies[user]}\n"
        return {"sections": {"conversation": text}}


def _call(name, args, fake, monkeypatch):
    import linkedin_mcp_server.tools.ext as m
    import linkedin_mcp_server.tools.ext_stage2 as s2

    async def fake_run(ctx, tool, body):
        return await body(fake)

    monkeypatch.setattr(m, "_run", fake_run)
    monkeypatch.setattr(s2, "_run", fake_run)
    mcp = FastMCP("t")
    m.register_ext_tools(mcp)
    s2.register_ext_stage2_tools(mcp)

    async def go():
        async with Client(mcp) as c:
            return (
                await c.call_tool(name, args, raise_on_error=False)
            ).structured_content

    return asyncio.run(go())


def _batch(fake, monkeypatch, recipients, **extra):
    args = {
        "message": TEXT,
        "recipients": recipients,
        "campaign": "r5",
        "confirm_send": True,
        "batch_size": 3,
        **extra,
    }
    return _call("send_campaign_batch", args, fake, monkeypatch)


def _non_canary_counted_today():
    ledger = outreach.Ledger.default()
    return ledger.count_since("message", outreach.day_start(), exclude=[CANARY])


def _assert_no_double_send(fake):
    doubles = {u: n for u, n in fake.delivered.items() if n > 1 and u != CANARY}
    assert not doubles, doubles


def _run_until_settled(fake, monkeypatch, recipients, calls=20, **extra):
    statuses = []
    for _ in range(calls):
        out = _batch(fake, monkeypatch, recipients, **extra)
        statuses.append(out["status"])
        _assert_no_double_send(fake)
        if out["status"] in {"done", "daily_cap_reached", "pace_budget_spent"}:
            break
    return statuses, out


# --- a campaign day ---------------------------------------------------------


def test_campaign_day_with_partial_failures_and_restarts(monkeypatch):
    people = [f"person-{i}" for i in range(9)]
    fake = FakeLinkedIn(
        {
            "person-1": ["raise", "ok"],  # page did not load, later fine
            "person-3": ["notsent", "ok"],  # composer missing, later fine
            "person-5": ["unconfirmed"],  # may have left: never again
            "person-7": ["readfail"],  # left, read-back fails: never again
        }
    )
    statuses, last = _run_until_settled(fake, monkeypatch, people)
    assert statuses[0] == "canary_verified"
    assert last["status"] == "done", statuses
    assert fake.delivered[CANARY] == 1
    # Every person received the text exactly once -- including the two whose
    # first attempt failed before the click (not permanently blocked).
    assert {p: fake.delivered[p] for p in people} == {p: 1 for p in people}
    assert _non_canary_counted_today() == len(people)
    assert _non_canary_counted_today() <= outreach.MESSAGES_PER_DAY_DEFAULT


def test_restarting_a_finished_batch_sends_nothing(monkeypatch):
    fake = FakeLinkedIn()
    _run_until_settled(fake, monkeypatch, ["anna", "bert"])
    before = dict(fake.delivered)
    for _ in range(3):
        out = _batch(fake, monkeypatch, ["Anna", "https://www.linkedin.com/in/bert/"])
        assert out["status"] == "done"
    assert dict(fake.delivered) == before


def test_crash_mid_send_leaves_attempted_and_blocks_the_retry(monkeypatch):
    """Process died between the attempted row and the outcome: the next run
    must treat the person as possibly sent."""
    fake = FakeLinkedIn()
    _batch(fake, monkeypatch, [])  # canary
    ledger = outreach.Ledger.default()
    ledger.append(
        {
            "attempt": "crashed",
            "kind": "message",
            "recipient": "anna",
            "text_sha": outreach.text_sha(TEXT),
            "status": "attempted",
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
    )
    # A torn half row from the same crash must not stop the next run either.
    with ledger.path.open("a", encoding="utf-8") as handle:
        handle.write('{"attempt": "torn", "kind": "mess')
    statuses, last = _run_until_settled(fake, monkeypatch, ["anna", "bert"])
    assert fake.delivered["anna"] == 0
    assert fake.delivered["bert"] == 1
    assert "anna" in last["already_sent"]
    # The crashed attempt counts against the day.
    assert _non_canary_counted_today() == 2


def test_cancellation_during_send_blocks_and_counts(monkeypatch):
    fake = FakeLinkedIn()
    _batch(fake, monkeypatch, [])  # canary

    async def cancelled(username, message, *, confirm_send):
        fake.delivered[username] += 1  # the click may have happened
        raise asyncio.CancelledError()

    fake.send_message = cancelled
    with pytest.raises(BaseException):
        _batch(fake, monkeypatch, ["anna"])
    fake2 = FakeLinkedIn()
    fake2.threads = dict(fake.threads)
    out = _batch(fake2, monkeypatch, ["anna"])
    assert out["status"] == "done" and fake2.delivered["anna"] == 0


def test_parallel_batches_do_not_send_twice(monkeypatch):
    import linkedin_mcp_server.tools.ext as m

    fake = FakeLinkedIn()
    _batch(fake, monkeypatch, [])  # canary
    people = ["anna", "bert", "carl"]

    async def fake_run(ctx, tool, body):
        return await body(fake)

    monkeypatch.setattr(m, "_run", fake_run)
    mcp = FastMCP("t")
    m.register_ext_tools(mcp)
    args = {
        "message": TEXT,
        "recipients": people,
        "campaign": "r5",
        "confirm_send": True,
        "batch_size": 3,
    }

    async def go():
        async with Client(mcp) as c:
            return await asyncio.gather(
                *(
                    c.call_tool("send_campaign_batch", args, raise_on_error=False)
                    for _ in range(3)
                )
            )

    asyncio.run(go())
    _assert_no_double_send(fake)
    assert sum(fake.delivered[p] for p in people) == 3


# --- caps and the day boundary ---------------------------------------------


def test_daily_cap_is_never_exceeded_over_many_calls(monkeypatch):
    people = [f"p{i:02d}" for i in range(20)]
    fake = FakeLinkedIn()
    statuses, last = _run_until_settled(
        fake, monkeypatch, people, calls=30, messages_per_day=5
    )
    assert last["status"] == "daily_cap_reached", statuses
    assert sum(fake.delivered[p] for p in people) == 5
    assert _non_canary_counted_today() == 5
    # A second campaign the same day with a higher cap stops at the hard max.
    other = [f"q{i:02d}" for i in range(50)]
    statuses, last = _run_until_settled(
        fake, monkeypatch, other, calls=60, messages_per_day=40
    )
    assert _non_canary_counted_today() <= outreach.MESSAGES_PER_DAY_MAX
    pace = outreach.Pacer(outreach.Ledger.default()).state("message")
    assert pace["today"] <= pace["per_day"]
    _assert_no_double_send(fake)


def _old_send(ledger, user, when):
    ledger.append(
        {
            "attempt": f"old-{user}",
            "kind": "message",
            "recipient": user,
            "text_sha": "0" * 64,
            "status": "verified",
            "started_at": when.isoformat(timespec="seconds"),
        }
    )


def test_yesterdays_sends_do_not_count_today_but_count_for_the_week(monkeypatch):
    ledger = outreach.Ledger.default()
    yesterday = outreach.day_start() - timedelta(minutes=1)
    for i in range(10):
        _old_send(ledger, f"y{i}", yesterday)
    assert _non_canary_counted_today() == 0
    pace = outreach.Pacer(ledger).state("message")
    assert pace["today"] == 0 and pace["last_7_days"] == 10
    fake = FakeLinkedIn()
    _, last = _run_until_settled(
        fake, monkeypatch, ["anna", "bert"], messages_per_day=1
    )
    assert last["status"] == "daily_cap_reached"
    assert fake.delivered["anna"] + fake.delivered["bert"] == 1


def test_weekly_budget_stops_the_batch_even_with_day_quota_left(monkeypatch):
    ledger = outreach.Ledger.default()
    start = outreach.day_start() - timedelta(days=6)
    for i in range(outreach.MESSAGES_PER_WEEK_MAX):
        _old_send(ledger, f"w{i}", start + timedelta(minutes=i))
    fake = FakeLinkedIn()
    statuses, _ = _run_until_settled(fake, monkeypatch, ["anna"], calls=3)
    assert sum(n for u, n in fake.delivered.items() if u != CANARY) == 0, statuses


# --- after the day: follow-ups and replies ----------------------------------


def test_follow_up_list_after_the_day_reads_replies_and_writes_no_block(monkeypatch):
    people = ["anna", "bert", "carl"]
    fake = FakeLinkedIn()
    _run_until_settled(fake, monkeypatch, people)
    fake.replies["anna"] = "Gern, rufen Sie an."
    fake.read_fail.add("carl")
    ledger = outreach.Ledger.default()
    before = [r for r in ledger.rows() if r.get("kind") == "message"]
    out = _call("follow_up_list", {"follow_up_days": 1}, fake, monkeypatch)
    by = {e["recipient"]: e for e in out["entries"]}
    assert by["anna"]["replied"] is True
    assert by["bert"]["replied"] is False and by["bert"]["due"] is False  # age 0
    assert by["carl"]["status"] == "unreadable"
    assert out["complete"] is False
    # Reading wrote no message rows: a read error cannot block anyone.
    assert [r for r in ledger.rows() if r.get("kind") == "message"] == before
    # A new text to carl is still possible after his thread failed to load.
    assert not ledger.already_contacted("message", "carl", outreach.text_sha("Neu"))


def test_reply_after_with_our_own_text_repeated_later():
    """The salutation line repeats in a follow-up; the anchor keeps the reply
    between the two messages visible."""
    first = "Guten Tag Frau Köhler,\nerste Nachricht zur Gefügeanalyse."
    second = "Guten Tag Frau Köhler,\nzweite Nachricht, nur zur Erinnerung."
    text = (
        "Profil von Jane Doe anzeigen\n" + first + "\n"
        "Profil von Eva Köhler anzeigen\nDanke, melde mich.\n"
        "Profil von Jane Doe anzeigen\n" + second + "\n"
    )
    got = outreach.reply_after(text, first, anchor=outreach.text_anchor(first))
    assert got["replied"] is True and got["by"] == "Eva Köhler"
    got = outreach.reply_after(text, second, anchor=outreach.text_anchor(second))
    assert got["replied"] is False


def test_follow_up_list_dues_only_old_unanswered(monkeypatch):
    fake = FakeLinkedIn()
    _run_until_settled(fake, monkeypatch, ["anna", "bert"])
    ledger = outreach.Ledger.default()
    # Age the ledger: rewrite started_at/at six days back.
    old = (datetime.now().astimezone() - timedelta(days=6)).isoformat(
        timespec="seconds"
    )
    rows = []
    for row in ledger.rows():
        for key in ("started_at", "at"):
            if key in row:
                row[key] = old
        rows.append(json.dumps(row, ensure_ascii=False))
    ledger.path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    fake.replies["bert"] = "Kein Bedarf."
    out = _call("follow_up_list", {"follow_up_days": 5}, fake, monkeypatch)
    assert [e["recipient"] for e in out["due"]] == ["anna"]
    assert [e["recipient"] for e in out["replied"]] == ["bert"]


def test_upstream_deadline_answer_books_unknown_and_blocks_the_retry(monkeypatch):
    """#1233: when the tool deadline cuts in after a possible submit, the sender
    answers ``send_unconfirmed`` with ``retry_safe=False`` instead of raising.
    The guarded path must book that as ``unknown`` -- blocking and counted,
    never ``not_sent`` and never ``verified``."""
    from linkedin_mcp_server.linkedin import contracts

    fake = FakeLinkedIn()
    _batch(fake, monkeypatch, [])  # canary

    async def deadline(username, message, *, confirm_send):
        fake.delivered[username] += 1  # the click may have happened
        return contracts.message_action_result(
            "https://www.linkedin.com/messaging/",
            "send_unconfirmed",
            "The tool deadline arrived before LinkedIn confirmed the send.",
            recipient_selected=True,
            retry_safe=False,
        )

    fake.send_message = deadline
    _batch(fake, monkeypatch, ["anna"])
    rows = [
        row
        for row in outreach.Ledger.default().latest_by_attempt().values()
        if row.get("recipient") == "anna"
    ]
    assert [row["status"] for row in rows] == ["unknown"]
    assert _non_canary_counted_today() == 1
    fake2 = FakeLinkedIn()
    out = _batch(fake2, monkeypatch, ["anna"])
    assert out["status"] == "done" and fake2.delivered["anna"] == 0
