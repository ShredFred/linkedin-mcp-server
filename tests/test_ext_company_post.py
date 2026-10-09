"""Fork extension: posting as a company page.

The whole point of this module is that a *page* publishes, not the member. Two
failure modes are worse than not posting at all, and both are tested here
rather than hoped for:

* the author switch silently does not take, and company content appears under
  a private name -- caught before the text is written and again before the
  commit click,
* a schedule is half-accepted and LinkedIn publishes later without anyone
  watching -- the time must be readable back in the composer or nothing is
  clicked.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from linkedin_mcp_server.linkedin import ext_company_post as mod
from linkedin_mcp_server.linkedin.ext_company_post import (
    ExtCompanyPostComposer,
    check_mode,
    check_schedule,
)

PAGE = "MiViA"


class FakePage:
    def __init__(
        self,
        *,
        author_texts: list[str] | None = None,
        author_after_switch: list[str] | None = None,
        mark_counts: dict[str, int] | None = None,
        pick_count: int = 1,
        schedule_ok: bool = True,
        summary: str = "Geplant für 09.10.2026 13:30",
        leftover: int = 0,
        editor_gone: bool = True,
    ) -> None:
        self.leftover = leftover
        self.editor_gone = editor_gone
        self.author_texts = author_texts if author_texts is not None else [PAGE]
        self.author_after_switch = author_after_switch
        self.mark_counts = mark_counts or {}
        self.pick_count = pick_count
        self.schedule_ok = schedule_ok
        self.summary = summary
        self.text = ""
        self.clicked: list[str] = []
        self.goto_urls: list[str] = []
        self._author_reads = 0

    async def goto(self, url: str, **_: Any) -> None:
        self.goto_urls.append(url)

    async def wait_for_selector(self, *_: Any, **__: Any) -> None:
        return None

    async def click(self, selector: str) -> None:
        self.clicked.append(selector)

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if script is mod._MEDIA_PRESENT_JS:
            return self.leftover
        if script is mod._AUTHOR_READ_JS:
            self._author_reads += 1
            texts = self.author_texts
            if self._author_reads > 1 and self.author_after_switch is not None:
                texts = self.author_after_switch
            return {"count": len(texts), "texts": texts, "all": []}
        if script is mod._PICK_AUTHOR_JS:
            return {"count": self.pick_count, "seen": []}
        if script is mod._MARK_JS:
            tag = arg["tag"]
            return {"count": self.mark_counts.get(tag, 1), "disabled": False, "seen": []}
        if script is mod._SET_SCHEDULE_JS:
            return {"ok": self.schedule_ok, "date": arg["date"], "time": arg["time"]}
        if script is mod._SCHEDULE_SUMMARY_JS:
            return self.summary
        if script is mod._WRITE_JS:
            self.text = arg["text"]
            return "written"
        if script is mod._CLEAR_JS:
            self.text = ""
            return True
        if script is mod._DISCARD_JS:
            return "closed"
        if script.startswith("(s) => !document.querySelector"):
            # The composer closes when the commit went through; that is what
            # separates "scheduled" from "schedule_unconfirmed".
            return self.editor_gone
        return ""

    @property
    def committed(self) -> bool:
        """Did anything that publishes or saves get clicked?"""
        return any(
            s.endswith('"post"]') or s.endswith('"draft"]') for s in self.clicked
        )


class FakeSession:
    def __init__(self, page: FakePage) -> None:
        self.page = page

    async def check_rate_limit(self) -> None:
        return None

    async def delay(self, _: float) -> None:
        return None


class FakeNavigator:
    def __init__(self, page: FakePage) -> None:
        self._page = page

    async def _navigate_to_page(self, url: str) -> None:
        self._page.goto_urls.append(url)


def composer(page: FakePage) -> ExtCompanyPostComposer:
    return ExtCompanyPostComposer(FakeSession(page), FakeNavigator(page))  # type: ignore[arg-type]


def soon(minutes: int) -> str:
    return (datetime.now().astimezone() + timedelta(minutes=minutes)).strftime(
        "%Y-%m-%d %H:%M"
    )


# -- the guards that run before the browser is touched ------------------------


def test_mode_must_be_one_of_the_three() -> None:
    assert check_mode("publish") is None
    assert check_mode("veroeffentlichen")["status"] == "invalid_input"


def test_a_schedule_in_the_past_is_refused_rather_than_sent_as_now() -> None:
    past = (datetime.now().astimezone() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    assert check_schedule("schedule", past)["status"] == "schedule_too_soon"


def test_a_schedule_two_minutes_out_is_refused() -> None:
    assert check_schedule("schedule", soon(2))["status"] == "schedule_too_soon"


def test_a_schedule_an_hour_out_passes() -> None:
    assert check_schedule("schedule", soon(60)) is None


def test_schedule_needs_a_time_and_a_time_needs_schedule() -> None:
    assert check_schedule("schedule", None)["field"] == "scheduled_at"
    assert check_schedule("publish", soon(60))["field"] == "scheduled_at"
    assert check_schedule("schedule", "09.10.2026 13:30")["field"] == "scheduled_at"


# -- the author is the whole point --------------------------------------------


@pytest.mark.asyncio
async def test_without_a_confirmed_author_nothing_is_written_or_clicked() -> None:
    page = FakePage(author_texts=["Frederik Stadler"])
    result = await composer(page).create_company_post(
        PAGE, "Text", image_path=None, mode="publish", scheduled_at=None, confirm=True
    )
    assert result["status"] == "author_not_confirmed"
    assert result["posted"] is False
    assert page.text == ""
    assert not page.committed


@pytest.mark.asyncio
async def test_an_ambiguous_author_entry_is_not_guessed() -> None:
    page = FakePage(pick_count=2)
    result = await composer(page).create_company_post(
        PAGE, "Text", image_path=None, mode="publish", scheduled_at=None, confirm=True
    )
    assert result["status"] == "author_option_unavailable"
    assert not page.committed


@pytest.mark.asyncio
async def test_an_author_lost_after_the_text_stops_before_the_commit() -> None:
    # Confirmed during the switch, gone when re-read right before the click.
    page = FakePage(author_texts=[PAGE], author_after_switch=["Frederik Stadler"])
    page._author_reads = 0
    result = await composer(page).create_company_post(
        PAGE, "Text", image_path=None, mode="publish", scheduled_at=None, confirm=True
    )
    assert result["status"] in {"author_not_confirmed", "author_lost"}
    assert not page.committed


# -- the dry run must stay a dry run ------------------------------------------


@pytest.mark.asyncio
async def test_the_dry_run_writes_verifies_and_discards_without_uploading() -> None:
    page = FakePage()
    result = await composer(page).create_company_post(
        PAGE,
        "Erste Zeile\n\nzweite",
        image_path=None,
        mode="publish",
        scheduled_at=None,
        confirm=False,
    )
    assert result["status"] == "dry_run"
    assert result["author"] == PAGE
    assert page.text == ""  # cleared again
    assert not page.committed


@pytest.mark.asyncio
async def test_a_restored_draft_with_media_stops_before_the_author_switch() -> None:
    page = FakePage(leftover=1)
    result = await composer(page).create_company_post(
        PAGE, "Text", image_path=None, mode="publish", scheduled_at=None, confirm=True
    )
    assert result["status"] == "editor_has_media"
    assert page.text == ""
    assert not page.clicked


# -- scheduling publishes later, so it must be read back ----------------------


@pytest.mark.asyncio
async def test_a_schedule_the_composer_does_not_show_is_not_committed() -> None:
    page = FakePage(summary="Sofort veröffentlichen")
    result = await composer(page).create_company_post(
        PAGE,
        "Text",
        image_path=None,
        mode="schedule",
        scheduled_at="2026-10-09 13:30",
        confirm=True,
    )
    assert result["status"] == "schedule_not_confirmed"
    assert result["posted"] is False
    assert not page.committed


@pytest.mark.asyncio
async def test_a_confirmed_schedule_reports_scheduled_and_not_posted() -> None:
    page = FakePage(summary="Geplant für 09.10.2026 13:30")
    result = await composer(page).create_company_post(
        PAGE,
        "Text",
        image_path=None,
        mode="schedule",
        scheduled_at="2026-10-09 13:30",
        confirm=True,
    )
    assert result["status"] == "scheduled"
    # posted stays False: nothing is live yet, LinkedIn publishes later.
    assert result["posted"] is False
    assert result["scheduled_at"] == "2026-10-09 13:30"


@pytest.mark.asyncio
async def test_a_missing_schedule_control_stops_instead_of_posting_now() -> None:
    page = FakePage(mark_counts={"schedule": 0})
    result = await composer(page).create_company_post(
        PAGE,
        "Text",
        image_path=None,
        mode="schedule",
        scheduled_at="2026-10-09 13:30",
        confirm=True,
    )
    assert result["status"] == "schedule_control_unavailable"
    assert not page.committed


# -- draft ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_draft_without_a_save_entry_does_not_fall_back_to_posting() -> None:
    page = FakePage(mark_counts={"draft": 0})
    result = await composer(page).create_company_post(
        PAGE, "Text", image_path=None, mode="draft", scheduled_at=None, confirm=True
    )
    assert result["status"] == "draft_prompt_unavailable"
    assert result["posted"] is False
    assert not page.committed


@pytest.mark.asyncio
async def test_a_saved_draft_is_not_reported_as_posted() -> None:
    page = FakePage()
    result = await composer(page).create_company_post(
        PAGE, "Text", image_path=None, mode="draft", scheduled_at=None, confirm=True
    )
    assert result["status"] == "draft_saved"
    assert result["posted"] is False
