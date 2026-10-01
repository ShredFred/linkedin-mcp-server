"""Hardening round 2026-10-01: pacer lock, ledger rows, follow-up completeness,
attendee paging. No browser, no live action."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import time
from datetime import datetime

import pytest

from linkedin_mcp_server import mivia_outreach as outreach


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(outreach.LEDGER_ENV, str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv(outreach.NOTES_ENV, str(tmp_path / "notes.json"))


# -- ledger: a torn row cut right after a nested "}" -----------------------------


def test_torn_row_ending_in_brace_is_skipped_not_corrupt():
    ledger = outreach.Ledger.default()
    ledger.append({"kind": "pace", "action": "page_read", "count": 1})
    with ledger.path.open("a", encoding="utf-8") as h:
        h.write('{"kind": "message", "meta": {"a": 1}')  # crash after nested }
    ledger.append({"kind": "pace", "action": "page_read", "count": 1})
    assert len(ledger.rows()) == 2  # fragment sealed and skipped, both rows kept


def test_real_corruption_still_refuses():
    ledger = outreach.Ledger.default()
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text('{"a": 1 "b": 2}\n{"kind": "pace"}\n', encoding="utf-8")
    with pytest.raises(outreach.LedgerCorrupt):
        ledger.rows()


# -- lock: owner token and stale takeover ---------------------------------------


def test_release_does_not_delete_a_lock_taken_over(tmp_path):
    lock = tmp_path / "x.lock"
    with outreach._file_lock(lock):
        # Simulate: our lock was judged stale and another process now owns it.
        # (Windows cannot move an open file; overwrite the owner token instead.)
        lock.write_bytes(b"other-owner")
    assert lock.read_bytes() == b"other-owner"


def test_stale_lock_is_taken_over(tmp_path):
    lock = tmp_path / "x.lock"
    lock.write_bytes(b"dead")
    old = time.time() - 120
    os.utime(lock, (old, old))
    with outreach._file_lock(lock, timeout=2):
        assert lock.read_bytes() != b"dead"
    assert not lock.exists()
    assert not list(tmp_path.glob("x.lock.stale-*"))


def test_takeover_gives_back_a_fresh_lock(tmp_path):
    lock = tmp_path / "x.lock"
    lock.write_bytes(b"fresh-owner")  # fresh mtime: the stat said stale, race
    outreach._take_over_stale(lock, stale_after=60)
    assert lock.read_bytes() == b"fresh-owner"
    assert not list(tmp_path.glob("x.lock.stale-*"))


def _append_many(path: str, tag: str, n: int) -> None:
    os.environ[outreach.LEDGER_ENV] = path
    ledger = outreach.Ledger.default()
    pad = "x" * 3000  # larger than one small write: interleaving would show
    for i in range(n):
        ledger.append(
            {"kind": "pace", "action": "page_read", "tag": tag, "i": i, "pad": pad}
        )


def test_two_processes_append_without_losing_rows(tmp_path):
    path = str(tmp_path / "ledger.jsonl")
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_append_many, args=(path, t, 40)) for t in "ab"]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    os.environ[outreach.LEDGER_ENV] = path
    rows = outreach.Ledger.default().rows()
    assert len(rows) == 80
    assert sorted((r["tag"], r["i"]) for r in rows) == sorted(
        (t, i) for t in "ab" for i in range(40)
    )


# -- day boundary on the DST change day -------------------------------------------


def test_day_start_on_dst_day_is_local_midnight():
    now = datetime(2026, 10, 25, 0, 30).astimezone()
    start = outreach.day_start(now)
    assert (start.hour, start.minute) == (0, 0)
    assert start <= now


# -- follow_up_list: completeness and booking ------------------------------------


def _call(name, args, extractor, monkeypatch):
    import linkedin_mcp_server.tools.mivia as m
    import linkedin_mcp_server.tools.mivia_stage2 as s2
    from fastmcp import Client, FastMCP

    async def fake_run(ctx, tool, body):
        return await body(extractor)

    monkeypatch.setattr(m, "_run", fake_run)
    monkeypatch.setattr(s2, "_run", fake_run)

    async def no_sleep(*a, **k):
        return None

    monkeypatch.setattr(s2.asyncio, "sleep", no_sleep)
    mcp = FastMCP("t")
    m.register_mivia_tools(mcp)

    async def go():
        async with Client(mcp) as c:
            return (await c.call_tool(name, args)).structured_content

    return asyncio.run(go())


class _Conv:
    calls = 0

    async def get_conversation(self, **kw):
        self.calls += 1
        return {"sections": {"conversation": ""}}


def _sent(n):
    ledger = outreach.Ledger.default()
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text(
        "".join(
            json.dumps(
                {
                    "at": f"2026-09-{i + 1:02d}T10:00:00+02:00",
                    "attempt": f"a{i}",
                    "kind": "message",
                    "recipient": f"p{i}",
                    "status": "sent",
                    "text_head": "Hallo",
                }
            )
            + "\n"
            for i in range(n)
        ),
        encoding="utf-8",
    )


def _page_reads():
    return outreach.Pacer(outreach.Ledger.default()).state("page_read")["today"]


def test_follow_up_list_cut_by_max_threads_says_so(monkeypatch):
    _sent(3)
    out = _call("follow_up_list", {"max_threads": 2}, _Conv(), monkeypatch)
    assert out["threads_read"] == 2
    assert out["has_more"] is True
    assert out["complete"] is False


def test_follow_up_list_full_read_is_complete(monkeypatch):
    _sent(2)
    out = _call("follow_up_list", {}, _Conv(), monkeypatch)
    assert out["has_more"] is False
    assert out["complete"] is True


def test_follow_up_list_unreadable_thread_is_not_complete(monkeypatch):
    _sent(1)

    class Broken:
        async def get_conversation(self, **kw):
            raise RuntimeError("timeout")

    out = _call("follow_up_list", {}, Broken(), monkeypatch)
    assert out["complete"] is False


def test_follow_up_list_empty_ledger_books_nothing(monkeypatch):
    conv = _Conv()
    out = _call("follow_up_list", {}, conv, monkeypatch)
    assert out["complete"] is True and out["entries"] == []
    assert conv.calls == 0
    assert _page_reads() == 0


# -- reply_after edge cases --------------------------------------------------------


def test_reply_after_old_row_without_anchor_uses_head():
    text = (
        "Profil von Jessica Schneider anzeigen\nGuten Tag Herr X,\nwir ...\n"
        "Profil von Dieter Muster anzeigen\nDanke, gern.\n"
    )
    out = outreach.reply_after(text, "Guten Tag Herr X,", anchor=None)
    assert out["replied"] is True and out["by"] == "Dieter Muster"


def test_reply_after_group_chat_is_unclear():
    text = (
        "Profil von Jessica Schneider anzeigen\nHallo zusammen\n"
        "Profil von A B anzeigen\nja\nProfil von C D anzeigen\nnein\n"
    )
    out = outreach.reply_after(text, "Hallo zusammen")
    assert out["replied"] is None and out["reason"] == "group_chat"


# -- event attendees: a repeated page is not the end ------------------------------


def test_repeated_attendee_page_is_not_complete():
    from linkedin_mcp_server.linkedin.mivia_network import MiviaNetworkReader

    cards = [
        {"slug": f"p{i}", "lines": [f"Name {i}", "Rolle", "Ort"], "actions": []}
        for i in range(10)
    ]

    class Session:
        page = None

        def monotonic(self):
            return 0.0

        async def delay(self, s):
            pass

        async def check_rate_limit(self):
            pass

    class Nav:
        async def _navigate_to_page(self, url):
            pass

    reader = MiviaNetworkReader(Session(), Nav())

    async def same_cards():
        return cards

    async def nothing(*a, **k):
        return None

    reader._cards = same_cards
    reader._wait_for_cards = nothing
    reader._wait_for_actions = nothing
    out = asyncio.run(reader.get_event_attendees("7457346711301214208", 1, 3))
    assert out["count"] == 10
    assert out["complete"] is False
    assert out["next_page"] is None
    assert any("repeated" in w for w in out["warnings"])
