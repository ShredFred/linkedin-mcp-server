"""Fork extension: hardening of the read paths (network reader, daily collector)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest

from linkedin_mcp_server import ext_daily
from linkedin_mcp_server import ext_outreach as outreach
from linkedin_mcp_server.linkedin import ext_network as net

EVENT = "7370000000000000001"


class _Session:
    def __init__(self, page=None):
        self.page = page
        self.t = 0.0

    def monotonic(self):
        self.t += 1.0
        return self.t

    async def delay(self, _s):
        return None

    async def check_rate_limit(self):
        return None


class _Nav:
    def __init__(self):
        self.urls = []

    async def _navigate_to_page(self, url):
        self.urls.append(url)


def _reader(page):
    return net.ExtNetworkReader(_Session(page), _Nav())


# -- network reader ----------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1, 10**6, True, "5"])
def test_list_limits_are_validated(bad):
    reader = _reader(page=None)
    with pytest.raises(ValueError):
        asyncio.run(reader.list_connections(None, bad))
    with pytest.raises(ValueError):
        asyncio.run(reader.list_sent_invitations(bad))
    assert reader._navigator.urls == []  # refused before any page load


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start_page": 0, "max_pages": 1},
        {"start_page": 1, "max_pages": 0},
        {"start_page": 1, "max_pages": -3},
        {"start_page": 1, "max_pages": 10**5},
    ],
)
def test_attendee_paging_arguments_are_validated(kwargs):
    # max_pages=0 used to return next_page=start_page+1: start_page was skipped.
    reader = _reader(page=None)
    with pytest.raises(ValueError):
        asyncio.run(reader.get_event_attendees(EVENT, **kwargs))
    with pytest.raises(ValueError):
        asyncio.run(reader.get_event_attendees("../x", 1, 1))
    assert reader._navigator.urls == []


def test_attendee_url_refuses_bad_page():
    with pytest.raises(ValueError):
        net.event_attendees_url(EVENT, 0)


class _InvitationsPage:
    def __init__(self, n, header):
        self.cards = [
            {
                "slug": f"p{i}",
                "lines": [f"P {i}", "Head", "Vor 1 Tag", "x"],
                "actions": [],
            }
            for i in range(n)
        ]
        self.header = header

    async def evaluate(self, js, *_a):
        if js is net._COUNT_JS:
            return len(self.cards)
        if js is net._CARDS_JS:
            return self.cards
        if "\\(" in js:  # header total
            return self.header
        return True


def test_sent_invitations_cut_at_limit_is_not_complete():
    # Before: complete = len >= min(total, limit) -> True at limit with 50 open.
    res = asyncio.run(_reader(_InvitationsPage(10, "50")).list_sent_invitations(10))
    assert res["count"] == 10
    assert res["complete"] is False
    res = asyncio.run(_reader(_InvitationsPage(5, "5")).list_sent_invitations(10))
    assert res["complete"] is True


class _DupPage(_InvitationsPage):
    async def evaluate(self, js, *_a):
        if js is net._CARDS_JS:
            return self.cards + self.cards  # nested list items: every card twice
        return await super().evaluate(js, *_a)


def test_duplicate_cards_are_counted_once():
    res = asyncio.run(_reader(_DupPage(3, "3")).list_sent_invitations(10))
    assert [i["slug"] for i in res["invitations"]] == ["p0", "p1", "p2"]
    assert res["complete"] is True


def test_connections_report_completeness():
    page = _InvitationsPage(4, None)
    res = asyncio.run(_reader(page).list_connections(None, 4))
    assert res["count"] == 4 and res["complete"] is False  # stopped at limit
    res = asyncio.run(_reader(page).list_connections(None, 10))
    assert res["count"] == 4 and res["complete"] is True  # list stopped growing


def test_projection_cut_resumes_on_the_cut_page():
    raw = {
        "event_id": EVENT,
        "start_page": 1,
        "pages_read": 2,
        "next_page": 3,
        "complete": False,
        "attendees": [{"slug": f"a{i}", "page": 1 + i // 10} for i in range(20)],
    }
    out = net.project_attendees(raw, limit=15)
    assert out["count"] == 15
    assert out["complete"] is False
    # a15..a19 sit on page 2; continuing at page 3 would have skipped them.
    assert out["next_page"] == 2


# -- daily collector ---------------------------------------------------------


class _Ex:
    def __init__(self, page=None):
        self.ext_session = _Session(page)
        self.ext_navigator = _Nav()


def _collector(tmp_path, monkeypatch, cfg=None, page=None):
    monkeypatch.setenv("LINKEDIN_MCP_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setattr(ext_daily, "read_event_count", _count(25))
    return ext_daily.Collector(_Ex(page), cfg or {}, tmp_path)


def _count(n):
    async def read(_page, _session):
        return n

    return read


def test_corrupt_state_file_is_set_aside_not_fatal(tmp_path, monkeypatch):
    c = _collector(tmp_path, monkeypatch)
    c.state_path.write_text("{ broken", encoding="utf-8")
    assert c._state() == {}
    assert c.state_path.with_suffix(".corrupt").exists()
    assert c.errors and c.errors[0]["error"] == "state_file_corrupt"
    c.state_path.write_text("[1, 2]", encoding="utf-8")
    assert c._state() == {}


def test_age_days_accepts_naive_and_garbage_stamps():
    naive = datetime.now().replace(microsecond=0).isoformat()
    assert ext_daily.age_days(naive) == 0
    assert ext_daily.age_days("kein datum") is None
    assert ext_daily.age_days(None) is None


class _Attendees:
    def __init__(self, fail_on=None, pages=None):
        self.calls = []
        self.fail_on = fail_on
        self.pages = pages or {}

    async def get_event_attendees(self, event_id, page, max_pages):
        self.calls.append((page, max_pages))
        if page == self.fail_on:
            raise RuntimeError("tab crashed")
        people = self.pages.get(page, [{"slug": f"s{page}-{i}"} for i in range(10)])
        last = page >= 3
        return {
            "attendees": people,
            "pages_read": 1,
            "complete": last,
            "next_page": None if last else page + 1,
        }


def _search_used(tmp_path):
    return outreach.Pacer(outreach.Ledger(tmp_path / "ledger.jsonl")).state("search")[
        "today"
    ]


def test_event_scan_failure_keeps_pages_read_and_books_only_those(
    tmp_path, monkeypatch
):
    c = _collector(tmp_path, monkeypatch)
    c.actions = _Attendees(fail_on=2)
    c.state_path.write_text(
        json.dumps({"events": {EVENT: {"attendees": ["old"], "baseline_done": True}}}),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="tab crashed"):
        asyncio.run(c.event(EVENT, {"search_reserve": 0}))
    ev = json.loads(c.state_path.read_text(encoding="utf-8"))["events"][EVENT]
    assert "s1-0" in ev["attendees"]  # page 1 kept despite the failure
    assert ev["resume_page"] == 2
    # Pages 1 and 2 were attempted; page 3 was never booked (before: all 3).
    assert _search_used(tmp_path) == 2


def test_event_scan_finishes_page_by_page(tmp_path, monkeypatch):
    c = _collector(tmp_path, monkeypatch)
    c.actions = _Attendees()
    res = asyncio.run(c.event(EVENT, {"search_reserve": 0}))
    assert res["scan_finished"] is True and res["pages"] == "1-3/3"
    assert [m for _, m in c.actions.calls] == [1, 1, 1]
    assert _search_used(tmp_path) == 3


def test_event_scan_with_naive_last_scan_does_not_crash(tmp_path, monkeypatch):
    c = _collector(tmp_path, monkeypatch)
    c.actions = _Attendees()
    naive = datetime.now().replace(microsecond=0).isoformat()
    c.state_path.write_text(
        json.dumps({"events": {EVENT: {"last_full_scan": naive, "scanned_count": 25}}}),
        encoding="utf-8",
    )
    res = asyncio.run(c.event(EVENT, {}))
    assert res["full_scan"] is False  # scanned today, no growth


def test_event_refuses_bad_event_id(tmp_path, monkeypatch):
    c = _collector(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        asyncio.run(c.event("../../feed", {}))
    assert c.navigator.urls == []


def test_followers_with_absolute_hrefs_are_not_new_every_run(tmp_path, monkeypatch):
    href = "https://www.linkedin.com/in/anna/"
    cfg = {"page_followers": {"enabled": True, "page_id": "12345678"}}
    c = _collector(tmp_path, monkeypatch, cfg)

    class Events:
        async def page_followers(self, _pid, known, limit):
            return {
                "available": True,
                "followers": [{"href": href, "followed_month": "2099-01"}],
            }

    c.events = Events()
    monkeypatch.setattr(c, "seen", _Seen())
    first = asyncio.run(c.followers())
    assert first["first_run"] is True
    second = asyncio.run(c.followers())
    assert second["new_followers"] == []  # before: 'https' was the known href


class _Seen:
    def __init__(self):
        self.data = {}

    def keys(self, k):
        return set(self.data.get(k, set()))

    def remember(self, k, keys):
        self.data.setdefault(k, set()).update(keys)


def test_malformed_harvest_order_does_not_drop_the_others(tmp_path, monkeypatch):
    cfg = {
        "harvest": {
            "enabled": True,
            "search_reserve": 0,
            "orders": [
                {"event_id": EVENT, "pages": "viele"},
                {"event_id": EVENT, "pages": 1},
            ],
        }
    }
    c = _collector(tmp_path, monkeypatch, cfg)
    c.actions = _Attendees()
    rows = asyncio.run(c.harvest())
    assert rows[0]["error"] == "bad_order"
    assert rows[1]["last_page"] == 1


def test_event_scout_out_of_range_cursor_restarts(tmp_path, monkeypatch):
    cfg = {"events": {"enabled": True, "keywords": ["a"], "search_reserve": 0}}
    c = _collector(tmp_path, monkeypatch, cfg)
    c.state_path.write_text(
        json.dumps({"event_scout": {"cursor": -4}}), encoding="utf-8"
    )

    class Finder:
        async def by_keyword(self, k):
            return [{"event_id": "1", "found_by": k, "past": False}]

    c.events = Finder()
    res = asyncio.run(c.event_scout())
    assert res["found"] == 1
