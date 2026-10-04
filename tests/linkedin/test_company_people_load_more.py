"""Upstream #1077: bounded "Show more results" loop on /company/<x>/people/."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from linkedin_mcp_server.linkedin import capture as capture_module
from linkedin_mcp_server.linkedin.capture import CaptureMode, SectionCapture
from linkedin_mcp_server.linkedin.company import CompanyReader


class _ScriptedPeoplePage:
    """Answers the two loader programs from a list of row counts.

    ``rows[i]`` is the row count after ``i`` clicks; the button is present
    while ``i < buttons``.
    """

    def __init__(self, rows: list[int], buttons: int, click_ok: bool = True):
        self.rows = rows
        self.buttons = buttons
        self.click_ok = click_ok
        self.clicks = 0

    async def evaluate(self, program: str) -> Any:
        if "companyPeopleClickMore" in program:
            if not self.click_ok:
                return False
            self.clicks += 1
            return True
        assert "companyPeopleLoadState" in program
        i = min(self.clicks, len(self.rows) - 1)
        return {"rows": self.rows[i], "more": self.clicks < self.buttons}


def _capture(page: _ScriptedPeoplePage) -> tuple[SectionCapture, list[float]]:
    delays: list[float] = []

    async def delay(seconds: float) -> None:
        delays.append(seconds)

    session = SimpleNamespace(page=page, delay=delay)
    return SectionCapture(session, None, None), delays  # type: ignore[arg-type]


async def test_clicks_until_button_disappears():
    page = _ScriptedPeoplePage([12, 24, 30], buttons=2)
    capture, delays = _capture(page)
    out = await capture._load_more_company_people(None)
    assert out == {"rows": 30, "rounds": 2, "stop": "no_button"}
    assert len(delays) == 2


async def test_stops_at_requested_limit():
    page = _ScriptedPeoplePage([12, 24, 36, 48, 60], buttons=10)
    capture, _ = _capture(page)
    out = await capture._load_more_company_people(30)
    assert out == {"rows": 36, "rounds": 2, "stop": "limit"}


async def test_limit_already_met_does_not_click():
    page = _ScriptedPeoplePage([12], buttons=5)
    capture, _ = _capture(page)
    out = await capture._load_more_company_people(10)
    assert out["stop"] == "limit" and page.clicks == 0


async def test_no_button_from_the_start():
    page = _ScriptedPeoplePage([7], buttons=0)
    capture, _ = _capture(page)
    out = await capture._load_more_company_people(None)
    assert out == {"rows": 7, "rounds": 0, "stop": "no_button"}


async def test_click_that_finds_no_button_stops():
    page = _ScriptedPeoplePage([12], buttons=5, click_ok=False)
    capture, _ = _capture(page)
    out = await capture._load_more_company_people(None)
    assert out["stop"] == "no_button" and out["rounds"] == 0


async def test_stagnating_rows_stop_after_two_stale_rounds():
    page = _ScriptedPeoplePage([12, 12, 12, 12, 12], buttons=99)
    capture, _ = _capture(page)
    out = await capture._load_more_company_people(None)
    assert out == {"rows": 12, "rounds": 2, "stop": "stagnated"}


async def test_one_stale_round_is_tolerated():
    page = _ScriptedPeoplePage([12, 12, 24, 30], buttons=3)
    capture, _ = _capture(page)
    out = await capture._load_more_company_people(None)
    assert out == {"rows": 30, "rounds": 3, "stop": "no_button"}


async def test_hard_round_cap(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(capture_module, "COMPANY_PEOPLE_MAX_ROUNDS", 3)
    page = _ScriptedPeoplePage(list(range(10, 1000, 10)), buttons=999)
    capture, _ = _capture(page)
    out = await capture._load_more_company_people(10_000)
    assert out == {"rows": 40, "rounds": 3, "stop": "round_cap"}


async def test_reader_forwards_limit():
    cap = SimpleNamespace(
        capture=AsyncMock(
            return_value=SimpleNamespace(text="", references=[], error=None)
        )
    )
    reader = CompanyReader(None, cap)  # type: ignore[arg-type]
    await reader.get_company_employees("example-co", limit=40)
    plan = cap.capture.await_args.args[2]
    assert plan.mode == CaptureMode.COMPANY_PEOPLE and plan.max_rows == 40
