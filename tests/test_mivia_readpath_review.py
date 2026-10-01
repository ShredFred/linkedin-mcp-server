"""Adversarial review 2026-10-01 of the read/report paths: one test per finding."""

import asyncio
import json
from datetime import datetime

import pytest

from linkedin_mcp_server import mivia_daily
from linkedin_mcp_server import mivia_outreach as outreach
from linkedin_mcp_server.linkedin.mivia_engagement import (
    MiviaEngagementReader,
    SeenStore,
)
from linkedin_mcp_server.linkedin.mivia_events import MiviaEventFinder


def test_day_start_uses_midnight_offset_on_dst_day():
    midnight = datetime(2026, 10, 25).astimezone()
    noon = datetime(2026, 10, 25, 12).astimezone()
    if midnight.utcoffset() == noon.utcoffset():
        pytest.skip("local zone has no DST change on 2026-10-25")
    assert outreach.day_start(noon) == midnight
    assert outreach.day_start(noon).utcoffset() == midnight.utcoffset()


def test_non_object_ledger_line_is_corrupt_not_attributeerror(tmp_path):
    path = tmp_path / "l.jsonl"
    path.write_text('{"kind": "pace", "action": "search"}\nnull\n', encoding="utf-8")
    with pytest.raises(outreach.LedgerCorrupt):
        outreach.Ledger(path).rows()


def test_unreadable_pace_count_books_one(tmp_path):
    path = tmp_path / "l.jsonl"
    path.write_text(
        json.dumps({"kind": "pace", "action": "search", "count": "x"}) + "\n",
        encoding="utf-8",
    )
    pacer = outreach.Pacer(outreach.Ledger(path))
    assert pacer.used("search", outreach.day_start()) == 1


def test_sent_messages_skips_row_without_recipient(tmp_path):
    path = tmp_path / "l.jsonl"
    now = datetime.now().astimezone().isoformat()
    rows = [
        {"attempt": "a", "kind": "message", "status": "sent", "at": now},
        {"attempt": "b", "kind": "message", "status": "sent", "at": now,
         "recipient": "anna"},
    ]  # fmt: skip
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    got = outreach.sent_messages(outreach.Ledger(path))
    assert [r["recipient"] for r in got] == ["anna"]


class _Page:
    def __init__(self, state):
        self.state = state

    async def evaluate(self, _js):
        return self.state


async def _noop(*_a, **_k):
    return None


def test_reactors_complete_false_when_duplicates_hide_the_cut(monkeypatch):
    items = [{"href": "/in/dup/", "lines": ["Dup"], "icons": []}] * 2 + [
        {"href": f"/in/p{i}/", "lines": [f"P{i}"], "icons": []} for i in range(4)
    ]
    page = _Page({"items": items, "more": False, "failed": False})
    monkeypatch.setattr(MiviaEngagementReader, "_page", property(lambda s: page))
    reader = MiviaEngagementReader.__new__(MiviaEngagementReader)
    reader._goto = _noop
    reader._pause = _noop
    out = asyncio.run(reader.read_reactors("1", 3))
    assert len(out["reactors"]) == 2  # dup collapsed, then p0; p1..p3 cut off
    assert out["complete"] is False


def test_by_keyword_dedupes_shifted_pages():
    finder = MiviaEventFinder.__new__(MiviaEventFinder)
    page1 = [{"id": str(i), "lines": [f"T{i}"]} for i in range(10)]
    page2 = [{"id": "9", "lines": ["T9"]}, {"id": "10", "lines": ["T10"]}]
    states = [{"items": page1}, {"items": page2}]

    async def read(_url):
        return states.pop(0)

    class _S:
        delay = staticmethod(_noop)

    finder._read = read
    finder._session = _S()
    events = asyncio.run(finder.by_keyword("x", max_pages=2))
    assert [e["event_id"] for e in events] == [str(i) for i in range(11)]


def test_second_idless_comment_of_same_person_is_new(tmp_path):
    comments = [{"id": "u1", "comment_id": None, "name": "A", "text": "erst"}]

    class _Eng:
        async def read_post_page(self, _aid):
            return {"reaction_count": 0, "comments": list(comments)}

    col = mivia_daily.Collector.__new__(mivia_daily.Collector)
    col.seen = SeenStore(tmp_path / "seen.json")
    col.engagement = _Eng()
    col._take = lambda *a, **k: None
    first = asyncio.run(col._engagers("1", reactors=False, source="t"))
    assert len(first["new_comments"]) == 1
    comments.append({**comments[0], "text": "zweit"})
    second = asyncio.run(col._engagers("1", reactors=False, source="t"))
    assert [c["text"] for c in second["new_comments"]] == ["zweit"]
