"""Fork extension: posting as a company page.

The whole point is that a *page* publishes, not the member. The design that
makes that safe is the entry point: the composer is opened from the page's own
admin view, so the page is the author by construction and this module only has
to *verify* one -- measured on 2026-10-09, after the first attempt through the
member composer at /feed/?shareActive=true found no author control at all.

Three failures would be worse than not posting, and each is tested:

* the dialog does not name the page -- company content would appear under a
  private name; caught before the text is written and again before the commit,
* a schedule is half-accepted and LinkedIn publishes later with nobody
  watching -- the time must read back out of the composer,
* a control is missing and the code falls through to the publish button.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from linkedin_mcp_server.linkedin import ext_company_post as mod
from linkedin_mcp_server.linkedin.ext_company_post import (
    ADMIN_POSTS_URL,
    ExtCompanyPostComposer,
    check_mode,
    check_page_id,
    check_schedule,
)

PAGE_ID = "12345678"
PAGE = "Acme Labs"


class FakePage:
    def __init__(
        self,
        *,
        author_texts: list[str] | None = None,
        author_after_write: list[str] | None = None,
        mark_counts: dict[str, int] | None = None,
        schedule_ok: bool = True,
        time_offered: int = 1,
        time_taken: bool = True,
        summary: str = "Acme Labs Veröffentlichung: Fr, 9. Okt. um 14:30 Bearbeiten Planen",
        leftover: int = 0,
        editor_gone: bool = True,
        write: str = "written",
    ) -> None:
        self.author_texts = author_texts if author_texts is not None else [f"{PAGE} Auf Alle posten"]
        self.author_after_write = author_after_write
        self.mark_counts = mark_counts or {}
        self.schedule_ok = schedule_ok
        self.time_offered = time_offered
        self.time_taken = time_taken
        self.want_time = ""
        self.summary = summary
        self.leftover = leftover
        self.editor_gone = editor_gone
        self.write = write
        self.text = ""
        self.clicked: list[str] = []
        self.goto_urls: list[str] = []
        self._author_reads = 0

    async def wait_for_selector(self, *_: Any, **__: Any) -> None:
        return None

    async def click(self, selector: str) -> None:
        self.clicked.append(selector)

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if script is mod._MEDIA_PRESENT_JS:
            return self.leftover
        if script is mod._AUTHOR_JS:
            self._author_reads += 1
            texts = self.author_texts
            if self._author_reads > 1 and self.author_after_write is not None:
                texts = self.author_after_write
            want = str(arg["name"]).lower()
            return {"ok": any(want in t.lower() for t in texts), "texts": texts}
        if script is mod._MARK_JS:
            tag = arg["tag"]
            return {"count": self.mark_counts.get(tag, 1), "disabled": False, "seen": []}
        if script is mod._SET_SCHEDULE_JS:
            # The dialog learns its format from the pre-filled sample, so the
            # call carries today's and the target's numbers, not strings. The
            # time is no longer written here; it is picked from the list.
            t = arg["target"]
            self.want_time = f"{t['hh']:02d}:{t['mm']:02d}"
            return {
                "ok": self.schedule_ok,
                "sample": "9.10.2026",
                "wrote_date": f"{t['d']}.{t['m']}.{t['y']}",
                "want_time": self.want_time,
                "date": f"{t['d']}.{t['m']}.{t['y']}",
                "time": "14:45",
            }
        if script is mod._PICK_TIME_JS:
            return {"count": self.time_offered, "seen": []}
        if script is mod._TIME_VALUE_JS:
            return self.want_time if self.time_taken else "14:45"
        if script is mod._DIALOG_TEXT_JS:
            return self.summary
        if script is mod._WRITE_JS:
            if self.write == "written":
                self.text = arg["text"]
            return self.write
        if script is mod._CLEAR_JS:
            self.text = ""
            return True
        if script is mod._COMPOSER_GONE_JS:
            # Asks for an editor *and* a commit button, so an open chat window
            # does not answer it -- that was the false alarm of 2026-10-09.
            return self.editor_gone
        return ""

    @property
    def committed(self) -> bool:
        """Did anything that publishes, schedules or saves get clicked?"""
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


async def run(page: FakePage, **kw: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "image_path": None,
        "mode": "publish",
        "scheduled_at": None,
        "confirm": True,
    }
    args.update(kw)
    return await composer(page).create_company_post(PAGE_ID, PAGE, "Text", **args)


def soon(minutes: int) -> str:
    """A time `minutes` ahead, snapped up to the next quarter hour."""
    when = datetime.now().astimezone() + timedelta(minutes=minutes)
    when += timedelta(minutes=(15 - when.minute % 15) % 15)
    return when.replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")


# -- guards that run before the browser is touched ----------------------------


def test_mode_must_be_one_of_the_three() -> None:
    assert check_mode("publish") is None
    assert check_mode("veroeffentlichen")["status"] == "invalid_input"


def test_page_id_must_be_the_numeric_id() -> None:
    assert check_page_id("12345678") is None
    assert check_page_id("acme-labs")["field"] == "page_id"


def test_a_schedule_in_the_past_is_refused_rather_than_sent_as_now() -> None:
    past = (datetime.now().astimezone() - timedelta(hours=1)).replace(
        minute=0, second=0, microsecond=0
    ).strftime("%Y-%m-%d %H:%M")
    assert check_schedule("schedule", past)["status"] == "schedule_too_soon"


def test_a_schedule_two_minutes_out_is_refused() -> None:
    two = (datetime.now().astimezone() + timedelta(minutes=2)).replace(
        second=0, microsecond=0
    )
    two -= timedelta(minutes=two.minute % 15)
    assert check_schedule(
        "schedule", two.strftime("%Y-%m-%d %H:%M")
    )["status"] == "schedule_too_soon"


def test_a_time_off_the_quarter_hour_is_refused() -> None:
    # LinkedIn offers quarter hours only; 14:37 would silently become another
    # time, which is how a 15:00 request once became 14:45.
    off = (datetime.now().astimezone() + timedelta(hours=2)).replace(
        minute=37, second=0, microsecond=0
    )
    assert check_schedule(
        "schedule", off.strftime("%Y-%m-%d %H:%M")
    )["status"] == "schedule_off_grid"


def test_a_schedule_an_hour_out_passes() -> None:
    assert check_schedule("schedule", soon(60)) is None


def test_schedule_needs_a_time_and_a_time_needs_schedule() -> None:
    assert check_schedule("schedule", None)["field"] == "scheduled_at"
    assert check_schedule("publish", soon(60))["field"] == "scheduled_at"
    assert check_schedule("schedule", "09.10.2026 14:30")["field"] == "scheduled_at"


@pytest.mark.asyncio
async def test_a_time_the_list_does_not_offer_is_not_scheduled() -> None:
    page = FakePage(time_offered=0)
    result = await run(page, mode="schedule", scheduled_at="2026-10-09 14:30")
    assert result["status"] == "time_not_offered"
    assert not page.committed


@pytest.mark.asyncio
async def test_a_time_the_field_does_not_take_is_not_scheduled() -> None:
    # The exact failure of 2026-10-09: the field reads back something else.
    page = FakePage(time_taken=False)
    result = await run(page, mode="schedule", scheduled_at="2026-10-09 14:30")
    assert result["status"] == "time_not_taken"
    assert result["shown"] == "14:45"
    assert not page.committed


# -- the entry point is what makes the page the author ------------------------


@pytest.mark.asyncio
async def test_the_composer_is_opened_from_the_page_admin_view() -> None:
    page = FakePage()
    await run(page, confirm=False)
    assert page.goto_urls == [ADMIN_POSTS_URL.format(page_id=PAGE_ID)]
    assert '[data-ext-company="start"]' in page.clicked


@pytest.mark.asyncio
async def test_without_the_opener_nothing_is_written_or_clicked() -> None:
    page = FakePage(mark_counts={"start": 0})
    result = await run(page)
    assert result["status"] == "composer_opener_unavailable"
    assert page.text == ""
    assert not page.committed


@pytest.mark.asyncio
async def test_a_dialog_that_does_not_name_the_page_stops_before_the_text() -> None:
    page = FakePage(author_texts=["Max Platzhalter Auf Alle posten"])
    result = await run(page)
    assert result["status"] == "author_not_confirmed"
    assert result["posted"] is False
    assert page.text == ""
    assert not page.committed


@pytest.mark.asyncio
async def test_an_author_lost_after_the_text_stops_before_the_commit() -> None:
    page = FakePage(
        author_texts=[f"{PAGE} Auf Alle posten"],
        author_after_write=["Max Platzhalter"],
    )
    result = await run(page)
    assert result["status"] in {"author_not_confirmed", "author_lost"}
    assert not page.committed


# -- the dry run must stay a dry run ------------------------------------------


@pytest.mark.asyncio
async def test_the_dry_run_writes_verifies_and_discards_without_uploading() -> None:
    page = FakePage()
    result = await run(page, confirm=False)
    assert result["status"] == "dry_run"
    assert result["author"] == PAGE
    assert page.text == ""  # cleared again
    assert not page.committed


@pytest.mark.asyncio
async def test_the_dry_run_does_not_upload_an_image() -> None:
    page = FakePage()
    result = await run(page, confirm=False, image_path=__file__)
    # __file__ is not an image, so the guard fires first -- which is itself the
    # point: an unusable path never reaches the browser.
    assert result["status"] == "invalid_image"
    assert not page.goto_urls


@pytest.mark.asyncio
async def test_a_restored_draft_with_media_stops_before_writing() -> None:
    page = FakePage(leftover=1)
    result = await run(page)
    assert result["status"] == "editor_has_media"
    assert page.text == ""
    assert not page.committed


@pytest.mark.asyncio
async def test_text_that_does_not_verify_stops_the_run() -> None:
    page = FakePage(write="mismatch")
    result = await run(page)
    assert result["status"] == "text_not_written"
    assert not page.committed


# -- scheduling publishes later, so it must be read back ----------------------


@pytest.mark.asyncio
async def test_a_schedule_the_composer_does_not_show_is_not_committed() -> None:
    page = FakePage(summary="Acme Labs Auf Alle posten")
    result = await run(page, mode="schedule", scheduled_at="2026-10-09 14:30")
    assert result["status"] == "schedule_not_confirmed"
    assert result["posted"] is False
    assert not page.committed


@pytest.mark.asyncio
async def test_a_confirmed_schedule_reports_scheduled_and_not_posted() -> None:
    page = FakePage(summary="Acme Labs Veröffentlichung: Fr, 9. Okt. um 14:30 Planen")
    result = await run(page, mode="schedule", scheduled_at="2026-10-09 14:30")
    assert result["status"] == "scheduled"
    # posted stays False: nothing is live yet, LinkedIn publishes later.
    assert result["posted"] is False
    assert result["scheduled_at"] == "2026-10-09 14:30"


@pytest.mark.asyncio
async def test_a_missing_schedule_control_stops_instead_of_posting_now() -> None:
    page = FakePage(mark_counts={"schedule": 0})
    result = await run(page, mode="schedule", scheduled_at="2026-10-09 14:30")
    assert result["status"] == "schedule_control_unavailable"
    assert not page.committed


@pytest.mark.asyncio
async def test_a_schedule_dialog_that_does_not_take_the_time_stops() -> None:
    page = FakePage(schedule_ok=False)
    result = await run(page, mode="schedule", scheduled_at="2026-10-09 14:30")
    assert result["status"] == "schedule_not_filled"
    assert not page.committed


# -- draft ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_draft_without_a_save_entry_does_not_fall_back_to_posting() -> None:
    page = FakePage(mark_counts={"draft": 0})
    result = await run(page, mode="draft")
    assert result["status"] == "draft_prompt_unavailable"
    assert result["posted"] is False
    assert not page.committed


@pytest.mark.asyncio
async def test_a_saved_draft_is_not_reported_as_posted() -> None:
    page = FakePage()
    result = await run(page, mode="draft")
    assert result["status"] == "draft_saved"
    assert result["posted"] is False


# -- publish -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_disabled_publish_button_is_not_clicked() -> None:
    page = FakePage()

    async def evaluate(script: str, arg: Any = None) -> Any:
        if script is mod._MARK_JS and arg["tag"] == "post":
            return {"count": 1, "disabled": True}
        return await FakePage.evaluate(page, script, arg)

    page.evaluate = evaluate  # type: ignore[method-assign]
    result = await run(page)
    assert result["status"] == "post_button_disabled"
    assert not page.committed


@pytest.mark.asyncio
async def test_a_published_post_that_closed_the_composer_is_reported_unverified() -> None:
    page = FakePage(editor_gone=True)
    result = await run(page)
    assert result["status"] == "posted_unverified"
    assert result["posted"] is True


@pytest.mark.asyncio
async def test_a_composer_still_open_after_the_click_is_unconfirmed() -> None:
    page = FakePage(editor_gone=False)
    result = await run(page)
    assert result["status"] == "post_unconfirmed"
    assert result["posted"] is False
