"""R9: harvest must not close an event on an empty page in the middle."""

from __future__ import annotations

import asyncio

from linkedin_mcp_server import mivia_daily

EID = "7286622235937701888"


def _collector(tmp_path, monkeypatch, orders):
    monkeypatch.setenv("MIVIA_LINKEDIN_LEDGER", str(tmp_path / "ledger.jsonl"))

    class Session:
        page = None

        async def delay(self, _s):
            return None

    class Ex:
        mivia_session = Session()
        mivia_navigator = object()

    cfg = {
        "harvest": {
            "enabled": True,
            "max_pages": 10,
            "search_reserve": 0,
            "orders": orders,
        }
    }
    return mivia_daily.Collector(Ex(), cfg, tmp_path)


class _Actions:
    """Pages 1-2 have attendees, page 3 is empty (glitch or end)."""

    def __init__(self, empty=3, recovers=False):
        self.empty, self.recovers, self.calls = empty, recovers, []

    async def get_event_attendees(self, e, page, pages):
        self.calls.append(page)
        if page == self.empty and not (self.recovers and self.calls.count(page) > 1):
            return {"attendees": [], "complete": True, "next_page": None}
        last = page >= self.empty + (1 if self.recovers else 0)
        return {
            "attendees": [{"slug": f"p{page}", "name": "X"}],
            "complete": last,
            "next_page": None if last else page + 1,
        }


def _run(c, orders):
    c.cfg["harvest"]["orders"] = orders
    return asyncio.run(c.harvest())


def test_empty_middle_page_is_not_complete_and_resumes_there(tmp_path, monkeypatch):
    c = _collector(tmp_path, monkeypatch, [])
    c.actions = _Actions()
    (row,) = _run(c, [{"event_id": EID, "start_page": 1, "pages": 5}])
    assert row["complete"] is False
    assert row["empty_page"] == 3
    assert row["last_page"] == 2  # planner resumes at last_page + 1 = 3
    assert [a["slug"] for a in row["attendees"]] == ["p1", "p2"]
    # Booked per page read: 3 pages read, not the 5 ordered.
    assert c.pacer.state("search")["today"] == 3


def test_second_empty_read_of_the_same_page_closes(tmp_path, monkeypatch):
    c = _collector(tmp_path, monkeypatch, [])
    c.actions = _Actions()
    _run(c, [{"event_id": EID, "start_page": 1, "pages": 5}])
    (row,) = _run(c, [{"event_id": EID, "start_page": 3, "pages": 5}])
    assert c.actions.calls == [1, 2, 3, 3]
    assert row["complete"] is True and "empty_page" not in row
    assert c.pacer.state("search")["today"] == 4
    assert "harvest_empty" not in c._state()


def test_glitch_page_recovers_on_resume(tmp_path, monkeypatch):
    c = _collector(tmp_path, monkeypatch, [])
    c.actions = _Actions(recovers=True)
    _run(c, [{"event_id": EID, "start_page": 1, "pages": 5}])
    (row,) = _run(c, [{"event_id": EID, "start_page": 3, "pages": 5}])
    assert [a["slug"] for a in row["attendees"]] == ["p3", "p4"]
    assert row["complete"] is True
    assert "harvest_empty" not in c._state()


def test_empty_page_at_counted_last_page_closes_at_once(tmp_path, monkeypatch):
    c = _collector(tmp_path, monkeypatch, [])
    c.actions = _Actions()
    (row,) = _run(c, [{"event_id": EID, "start_page": 1, "pages": 5, "total_pages": 3}])
    assert row["complete"] is True and "empty_page" not in row


def test_other_event_does_not_close_the_remembered_page(tmp_path, monkeypatch):
    c = _collector(tmp_path, monkeypatch, [])
    c.actions = _Actions()
    _run(c, [{"event_id": EID, "start_page": 1, "pages": 5}])
    other = "7286622235937701999"
    (row,) = _run(c, [{"event_id": other, "start_page": 3, "pages": 1}])
    assert row["complete"] is False and row["empty_page"] == 3
