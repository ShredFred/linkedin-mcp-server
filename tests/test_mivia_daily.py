"""MiViA fork: daily collector -- pure parts and exit codes (no browser)."""

from __future__ import annotations

import asyncio
import json

import pytest

from linkedin_mcp_server import mivia_daily


def test_hashtag_filter_is_case_insensitive_and_needs_the_hash():
    text = "One of several highlights at the HeatTreatmentCongress ... #ifhtse #HK2026"
    assert mivia_daily.has_hashtag(text, ["hk2026"])
    assert mivia_daily.has_hashtag(text, ["#ifhtse"])
    # Without '#' the plain word does not count: "HeatTreatmentCongress" appears
    # in the text but not as a hashtag.
    assert not mivia_daily.has_hashtag(text, ["heattreatmentcongress"])
    assert not mivia_daily.has_hashtag("", ["hk2026"])


def test_company_posts_view_as_member_and_admin_redirect_refused(tmp_path, monkeypatch):
    assert mivia_daily.company_posts_url("mivia").endswith(
        "/company/mivia/posts/?viewAsMember=true"
    )
    monkeypatch.setenv("MIVIA_LINKEDIN_LEDGER", str(tmp_path / "ledger.jsonl"))

    class Page:
        url = "https://www.linkedin.com/company/81728804/admin/dashboard/"

        async def evaluate(self, *_a):
            return [{"urn": "urn:li:activity:1", "text": "fremd"}]

    class Session:
        page = Page()

        async def check_rate_limit(self):
            return None

        async def delay(self, _s):
            return None

    class Nav:
        async def _navigate_to_page(self, _url):
            return None

    class Ex:
        mivia_session = Session()
        mivia_navigator = Nav()

    c = mivia_daily.Collector(Ex(), {}, tmp_path)
    with pytest.raises(RuntimeError, match="admin view"):
        asyncio.run(c._urns(mivia_daily.company_posts_url("mivia"), 3))


def test_event_scout_spreads_over_days_within_the_search_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("MIVIA_LINKEDIN_LEDGER", str(tmp_path / "ledger.jsonl"))
    from linkedin_mcp_server import mivia_outreach as outreach

    class Session:
        page = None

        async def delay(self, _s):
            return None

    class Ex:
        mivia_session = Session()
        mivia_navigator = object()

    cfg = {
        "events": {
            "enabled": True,
            "keywords": ["a", "b", "c"],
            "organisers": ["o1", "o2"],
            "search_reserve": 0,
        }
    }
    c = mivia_daily.Collector(Ex(), cfg, tmp_path)

    class Finder:
        async def by_keyword(self, k):
            return [{"event_id": f"k{k}", "found_by": f"keyword:{k}", "past": False}]

        async def by_organiser(self, o, include_past=False):
            return [{"event_id": "ka", "found_by": f"organiser:{o}", "past": False}]

    c.events = Finder()
    # Only 2 searches left today.
    ledger = outreach.Ledger(tmp_path / "ledger.jsonl")
    ledger.append(
        {
            "kind": "pace",
            "action": "search",
            "count": outreach.PACE_BUDGETS["search"]["day"] - 2,
            "tool": "t",
        }
    )
    first = asyncio.run(c.event_scout())
    assert first["deferred"] and first["progress"] == "2/5"
    # Next day: budget again (simulate by clearing the ledger).
    (tmp_path / "ledger.jsonl").write_text("", encoding="utf-8")
    second = asyncio.run(c.event_scout())
    assert second["due"] and not second.get("deferred")
    assert {e["event_id"] for e in second["upcoming"]} == {"ka", "kb", "kc"}
    assert second["past"] == []  # past events are reported separately (none here)
    assert sorted(second["upcoming"][0]["found_by"])  # merged sources
    third = asyncio.run(c.event_scout())
    assert third == {"enabled": True, "due": False, "last_run": third["last_run"]}


def test_employer_lookup_refuses_other_triggers_and_returns_two_fields(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MIVIA_LINKEDIN_LEDGER", str(tmp_path / "ledger.jsonl"))
    queue = tmp_path / "q.json"
    queue.write_text(
        json.dumps(
            [
                {
                    "name": "Rea",
                    "profil_url": "https://www.linkedin.com/in/rea/",
                    "anlass": "eigener Beitrag",
                    "art": "Reaktion (like)",
                },
                {
                    "name": "Fol",
                    "profil_url": "https://www.linkedin.com/in/fol/",
                    "anlass": "Follower",
                    "art": "Follower seit 2026-09",
                },
                {
                    "name": "Vis",
                    "profil_url": "https://www.linkedin.com/in/vis/",
                    "anlass": "eigener Beitrag",
                    "art": "Profilbesuch",
                },
            ]
        ),
        encoding="utf-8",
    )

    class Session:
        page = None

        async def delay(self, _s):
            return None

    class Ex:
        mivia_session = Session()
        mivia_navigator = object()

    c = mivia_daily.Collector(
        Ex(), {"employer_lookup": {"enabled": True, "queue": str(queue)}}, tmp_path
    )
    looked = []

    class Finder:
        async def current_employer(self, url):
            looked.append(url)
            return {
                "status": "ok",
                "employer": "Acme",
                "role": "Lead",
                "photo": "x",
                "skills": ["y"],
            }

    c.events = Finder()
    res = asyncio.run(c.employer_lookup())
    assert looked == ["https://www.linkedin.com/in/rea/"]
    assert set(res[0]) == {
        "name",
        "profil_url",
        "anlass",
        "art",
        "beitrag",
        "status",
        "employer",
        "role",
    }


def test_post_head_skips_card_labels():
    text = "Nummer des Feedbeitrags 1\nFeed-Beitrag\n4 Monat(e)\nVor 4 Jahren waren wir das erste Mal auf der CONTROL."
    assert (
        mivia_daily.post_head(text)
        == "Vor 4 Jahren waren wir das erste Mal auf der CONTROL."
    )


def test_activity_id():
    assert (
        mivia_daily.activity_id("urn:li:activity:7510583477378134016")
        == "7510583477378134016"
    )


class _BusyError(Exception):
    pass


_BusyError.__name__ = "BrowserBusyError"


@pytest.mark.parametrize(
    "exc_name,status,code",
    [
        ("BrowserBusyError", "browser_busy", 4),
        ("AuthenticationError", "login_required", 5),
        ("RuntimeError", "failed", 1),
    ],
)
def test_exit_codes_and_report_on_startup_failure(
    tmp_path, monkeypatch, exc_name, status, code
):
    exc_type = type(exc_name, (Exception,), {})

    async def fail(*_a, **_k):
        raise exc_type("nope")

    import linkedin_mcp_server.drivers.browser as browser

    monkeypatch.setattr(browser, "get_or_create_browser", fail)
    monkeypatch.setattr(browser, "set_headless", lambda _h: None)
    cfg = tmp_path / "cfg.json"
    cfg.write_text("{}", encoding="utf-8")
    out = tmp_path / "out.json"
    rc = mivia_daily.main(
        ["--config", str(cfg), "--out", str(out), "--state-dir", str(tmp_path)]
    )
    assert rc == code
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["status"] == status and report["schema"] == "mivia_daily.v1"


def test_a_failing_part_does_not_stop_the_others(tmp_path, monkeypatch):
    monkeypatch.setenv("MIVIA_LINKEDIN_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("MIVIA_LINKEDIN_ENGAGERS_SEEN", str(tmp_path / "seen.json"))

    class Ex:
        mivia_session = object()
        mivia_navigator = object()

    c = mivia_daily.Collector(Ex(), {"radar": {"enabled": False}}, tmp_path)

    async def boom():
        raise ValueError("kaputt")

    async def ok():
        return {"enabled": True, "total_viewers": 1, "new_viewers": []}

    c.own_posts = boom  # type: ignore[method-assign]
    c.viewers = ok  # type: ignore[method-assign]
    report = asyncio.run(c.run())
    assert report["posts"] == []
    assert report["viewers"]["total_viewers"] == 1
    assert report["errors"][0]["part"] == "own_posts"


def test_harvest_reads_only_ordered_pages_within_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("MIVIA_LINKEDIN_LEDGER", str(tmp_path / "ledger.jsonl"))
    from linkedin_mcp_server import mivia_outreach as outreach

    class Session:
        page = None

        async def delay(self, _s):
            return None

    class Ex:
        mivia_session = Session()
        mivia_navigator = object()

    good, other = "7286622235937701888", "7457346711301214208"
    cfg = {
        "harvest": {
            "enabled": True,
            "max_pages": 4,
            "search_reserve": 0,
            "orders": [
                {"event_id": "bad", "pages": 1},
                {"event_id": good, "register_id": "r1", "start_page": 3, "pages": 3},
                {"event_id": other, "register_id": "r2", "pages": 5},
            ],
        }
    }
    c = mivia_daily.Collector(Ex(), cfg, tmp_path)
    calls = []

    class Actions:
        async def get_event_attendees(self, eid, page, pages):
            calls.append((eid, page, pages))
            people = [
                {"slug": f"{eid[-2:]}-{page}-{i}", "name": "X"} for i in range(pages)
            ]
            people.append({"slug": "me", "action": "self"})
            return {
                "attendees": people,
                "pages_read": pages,
                "complete": False,
                "next_page": page + pages,
            }

    c.actions = Actions()
    res = asyncio.run(c.harvest())
    assert res[0]["error"] == "bad_event_id"
    # One page per call, cap 4 pages in total.
    assert calls == [(good, 3, 1), (good, 4, 1), (good, 5, 1), (other, 1, 1)]
    assert res[1]["last_page"] == 5 and not res[1]["complete"]
    assert all(a.get("action") != "self" for a in res[1]["attendees"])
    assert res[2]["last_page"] == 1
    # Budget spent: nothing is read.
    ledger = outreach.Ledger(tmp_path / "ledger.jsonl")
    ledger.append(
        {
            "kind": "pace",
            "action": "search",
            "count": outreach.PACE_BUDGETS["search"]["day"],
            "tool": "t",
        }
    )
    calls.clear()
    spent = asyncio.run(c.harvest())
    assert [r for r in spent if "attendees" in r] == []
    assert [r.get("deferred") for r in spent[1:]] == ["search_budget_spent"] * 2
    assert calls == []


def _harvest_collector(tmp_path, monkeypatch, orders, max_pages=10):
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
            "max_pages": max_pages,
            "search_reserve": 0,
            "orders": orders,
        }
    }
    return mivia_daily.Collector(Ex(), cfg, tmp_path)


def test_harvest_keeps_pages_read_before_a_failure(tmp_path, monkeypatch):
    eid = "7286622235937701888"
    c = _harvest_collector(
        tmp_path, monkeypatch, [{"event_id": eid, "start_page": 1, "pages": 4}]
    )

    class Actions:
        async def get_event_attendees(self, e, page, pages):
            if page == 3:
                raise TimeoutError("page did not load")
            return {
                "attendees": [{"slug": f"p{page}", "name": "X"}],
                "pages_read": 1,
                "complete": False,
                "next_page": page + 1,
            }

    c.actions = Actions()
    (row,) = asyncio.run(c.harvest())
    assert row["error"].startswith("TimeoutError")
    assert [a["slug"] for a in row["attendees"]] == ["p1", "p2"]
    assert row["last_page"] == 2 and row["complete"] is False
    # Only the pages actually attempted are booked (3), not the 4 ordered.
    assert c.pacer.state("search")["today"] == 3


def test_harvest_pace_limit_mid_run_keeps_partial_and_defers_rest(
    tmp_path, monkeypatch
):
    from linkedin_mcp_server import mivia_outreach as outreach

    a, b = "7286622235937701888", "7457346711301214208"
    c = _harvest_collector(
        tmp_path,
        monkeypatch,
        [{"event_id": a, "pages": 3}, {"event_id": b, "pages": 2}],
    )
    taken = []
    real_take = c._take

    def take(action, n=1):
        taken.append(n)
        if len(taken) == 2:
            raise outreach.PaceExceeded({"action": action, "left": 0})
        real_take(action, n)

    c._take = take

    class Actions:
        async def get_event_attendees(self, e, page, pages):
            return {
                "attendees": [{"slug": f"{e[-1]}{page}", "name": "X"}],
                "pages_read": 1,
                "complete": False,
                "next_page": page + 1,
            }

    c.actions = Actions()
    res = asyncio.run(c.harvest())
    assert res[0]["partial"] == "search_budget_spent" and res[0]["last_page"] == 1
    assert len(res[0]["attendees"]) == 1
    assert res[1] == {
        "event_id": b,
        "register_id": None,
        "deferred": "search_budget_spent",
    }


def test_monthly_search_limit_stops_harvest_without_marking_complete(
    tmp_path, monkeypatch
):
    from linkedin_mcp_server.linkedin.mivia_network import (
        SearchLimitReached,
        is_search_limit_text,
    )

    assert is_search_limit_text("You've reached the monthly limit for profile searches")
    assert is_search_limit_text("Sie haben das monatliche Limit für Suchen erreicht")
    assert not is_search_limit_text("Keine Ergebnisse gefunden")
    a, b = "7286622235937701888", "7457346711301214208"
    c = _harvest_collector(
        tmp_path,
        monkeypatch,
        [{"event_id": a, "pages": 3}, {"event_id": b, "pages": 2}],
    )

    class Actions:
        async def get_event_attendees(self, e, page, pages):
            if page == 2:
                raise SearchLimitReached("limit")
            return {
                "attendees": [{"slug": "p1", "name": "X"}],
                "pages_read": 1,
                "complete": False,
                "next_page": page + 1,
            }

    c.actions = Actions()
    res = asyncio.run(c.harvest())
    assert res[0]["partial"] == "monthly_search_limit" and res[0]["complete"] is False
    assert res[0]["last_page"] == 1
    assert (
        res[1]["deferred"] == "search_budget_spent"
    )  # rest of the run is not attempted


def test_limit_hit_blocks_searches_until_the_monthly_reset(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from linkedin_mcp_server import mivia_outreach as outreach

    monkeypatch.setenv("MIVIA_LINKEDIN_LEDGER", str(tmp_path / "ledger.jsonl"))
    pacer = outreach.Pacer(outreach.Ledger(tmp_path / "ledger.jsonl"))
    pacer.take("search", 7, tool="t")
    assert pacer.state("search")["left"] > 0
    pacer.record_limit_hit("search", tool="t")
    st = pacer.state("search")
    assert st["left"] == 0 and st["used_this_month_at_hit"] == 7 and st["resets_at"]
    with pytest.raises(outreach.PaceExceeded):
        pacer.take("search", 1)
    # Other actions are not affected.
    assert pacer.state("page_read")["left"] > 0
    # A hit from last month no longer blocks.
    last_month = outreach.month_start_pst() - timedelta(days=2)
    (tmp_path / "ledger.jsonl").write_text(
        json.dumps(
            {"kind": "limit_hit", "action": "search", "at": last_month.isoformat()}
        )
        + "\n",
        encoding="utf-8",
    )
    assert pacer.state("search")["left"] > 0
    assert (
        outreach.month_start_pst(
            datetime(2026, 10, 1, 7, 30, tzinfo=timezone.utc)
        ).month
        == 9
    )


def test_harvest_after_a_limit_hit_defers_with_the_monthly_reason(
    tmp_path, monkeypatch
):
    c = _harvest_collector(
        tmp_path, monkeypatch, [{"event_id": "7286622235937701888", "pages": 2}]
    )
    c.pacer.record_limit_hit("search")
    (row,) = asyncio.run(c.harvest())
    assert row["deferred"] == "monthly_search_limit"


def test_any_part_hitting_the_monthly_limit_sets_the_pacer_lock(tmp_path, monkeypatch):
    from linkedin_mcp_server.linkedin.mivia_network import SearchLimitReached

    c = _harvest_collector(tmp_path, monkeypatch, [])

    async def radar():
        raise SearchLimitReached("limit")

    assert asyncio.run(c._part("radar", radar())) is None
    assert c.errors[-1]["error"] == "monthly_search_limit"
    assert (
        c.pacer.state("search")["left"] == 0
        and c.pacer.state("search")["month_limit_hit"]
    )
