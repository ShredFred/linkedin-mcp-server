from datetime import datetime

from linkedin_mcp_server import ext_outreach as o


def test_temp_raise_runs_through_until_and_falls_back(monkeypatch):
    monkeypatch.setattr(o, "PACE_TEMP_RAISES", {"search": {"day": 130, "week": 750, "until": "2026-10-20"}})
    assert o.pace_budget("search", datetime(2026, 10, 13, 9).astimezone()) == {"day": 130, "week": 750}
    assert o.pace_budget("search", datetime(2026, 10, 20, 23).astimezone()) == {"day": 130, "week": 750}
    assert o.pace_budget("search", datetime(2026, 10, 21, 0, 5).astimezone()) == o.PACE_BUDGETS["search"]
    assert o.pace_budget("page_read", datetime(2026, 10, 13).astimezone()) == o.PACE_BUDGETS["page_read"]


def test_unreadable_until_is_no_raise(monkeypatch):
    monkeypatch.setattr(o, "PACE_TEMP_RAISES", {"search": {"day": 130, "week": 750, "until": "bald"}})
    assert o.pace_budget("search") == o.PACE_BUDGETS["search"]
