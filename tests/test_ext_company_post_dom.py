"""create_company_post page scripts against a real DOM (headless Chromium).

Two productive runs on 2026-10-09 failed, both on the same mistake: naming a
dialog by its *position*. "The first dialog" picked the composer while the
image editor held the date fields. "The last dialog" picked LinkedIn's
messaging overlay -- a role="dialog" with its own role="textbox" -- so the
author check found chat buttons and stopped.

The fixture therefore always has the chat overlay open, and in the position
that broke the code: after the composer. A test that leaves it out would pass
on the bug.

The markup mirrors what composer_probe measured on the page admin view, down
to the author button reading "MiViA Auf Alle posten" and the date field being
an ``input type="text"`` pre-filled ``9.10.2026`` -- not an ISO value, which is
the other thing that went wrong.
"""

from __future__ import annotations

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.linkedin import ext_company_post as cp

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

# LinkedIn's messaging overlay, measured in the failing run of 2026-10-09.
# It is a dialog, it has a textbox, and it sits after the composer.
CHAT = """
<div role="dialog" id="chat">
  <button aria-label="Ihre Unterhaltung mit Frederik Stadler und Alexander Gerstendörfer schließen"></button>
  <button aria-label="Mit „😃“ antworten 😃">Mit „😃“ antworten 😃</button>
  <div role="textbox" aria-label="Nachrichtenfeld erweitern"></div>
  <input type="text" aria-label="Suche in Nachrichten">
  <button>Senden</button>
</div>
"""

COMPOSER = """
<div role="dialog" id="composer">
  <button aria-label="Verwerfen"></button>
  <button type="button">MiViA Auf Alle posten</button>
  <div role="textbox" aria-label="Texteditor zum Erstellen von Inhalten"
       contenteditable="true"></div>
  <button aria-label="Emoji-Tastatur öffnen">Emoji-Tastatur öffnen</button>
  <button aria-label="Mediendatei hinzufügen"></button>
  <button aria-label="Termin für Beitrag festlegen"></button>
  <button id="commit">POSTWORD</button>
</div>
"""

SCHEDULE = """
<div role="dialog" id="schedule">
  <button aria-label="Verwerfen"></button>
  <input type="text" aria-label="Date" value="DATEVALUE">
  <input type="text" role="combobox" aria-label="Time" value="TIMEVALUE">
  <button aria-label="Zeitauswahl erweitern"></button>
  <ul>
    <li role="option">14:45</li>
    <li role="option">15:00</li>
    <li role="option">15:15</li>
    <li role="option">15:30</li>
  </ul>
  <button>Alle geplanten Beiträge anzeigen</button>
  <button>Zurück</button>
  <button>Weiter</button>
</div>
"""

TODAY = {"y": 2026, "m": 10, "d": 9}


@pytest.fixture
async def page():
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            pg = await browser.new_page()
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            yield pg
        finally:
            await browser.close()


async def set_page(page, *, commit="Posten", schedule=None):
    html = COMPOSER.replace("POSTWORD", commit) + CHAT
    if schedule:
        date_value, time_value = schedule
        html += SCHEDULE.replace("DATEVALUE", date_value).replace("TIMEVALUE", time_value)
    await page.set_content(f"<main>{html}</main>")


async def mark(page, tag, *, words=None, labels=None, scope="dialog",
               anchor='[role="textbox"]', anchor_words=None):
    return await page.evaluate(
        cp._MARK_JS,
        {
            "words": words or [],
            "labels": labels or [],
            "tag": tag,
            "scope": scope,
            "anchor": anchor,
            "anchor_words": cp.COMPOSER_WORDS if anchor_words is None else anchor_words,
        },
    )


async def dialog_of(page, tag):
    """Which dialog did the marked element end up in?"""
    return await page.evaluate(
        "(t) => document.querySelector(`[data-ext-company='${t}']`)"
        ".closest('[role=dialog]').id",
        tag,
    )


# -- the chat overlay must not be mistaken for the composer -------------------


async def test_the_author_is_read_from_the_composer_not_from_the_chat(page):
    await set_page(page)
    got = await page.evaluate(
        cp._AUTHOR_JS, {"name": "MiViA", "commit_words": cp.COMPOSER_WORDS}
    )
    assert got["ok"] is True
    assert any("MiViA" in t for t in got["texts"])
    # The exact failure of 2026-10-09: chat buttons showing up as the author.
    assert not any("antworten" in t.lower() for t in got["texts"])


async def test_a_page_the_composer_does_not_name_is_refused(page):
    await set_page(page)
    got = await page.evaluate(
        cp._AUTHOR_JS, {"name": "Acme GmbH", "commit_words": cp.COMPOSER_WORDS}
    )
    assert got["ok"] is False


async def test_the_schedule_control_is_found_in_the_composer(page):
    await set_page(page)
    got = await mark(page, "schedule", labels=cp._SCHEDULE_LABELS)
    assert got["count"] == 1
    where = await dialog_of(page, "schedule")
    assert where == "composer"


async def test_the_commit_button_is_found_in_the_composer_not_the_chat(page):
    await set_page(page)
    got = await mark(page, "post", words=cp._POST_WORDS)
    assert got["count"] == 1
    where = await dialog_of(page, "post")
    assert where == "composer"


async def test_a_composer_whose_button_reads_planen_is_still_the_composer(page):
    # After a time is set the commit button renames itself.
    await set_page(page, commit="Planen")
    got = await page.evaluate(
        cp._AUTHOR_JS, {"name": "MiViA", "commit_words": cp.COMPOSER_WORDS}
    )
    assert got["ok"] is True
    marked = await mark(page, "post", words=cp._SCHEDULE_COMMIT_WORDS)
    assert marked["count"] == 1


# -- writing and reading the text ---------------------------------------------


async def test_text_is_written_into_the_composer_editor(page):
    await set_page(page)
    out = await page.evaluate(
        cp._WRITE_JS, {"selector": cp._EDITOR_IN_DIALOG, "text": "Erste Zeile\n\nzweite"}
    )
    assert out == "written"
    body = await page.evaluate(
        '() => document.querySelector("#composer [role=textbox]").innerText'
    )
    assert "Erste Zeile" in body and "zweite" in body
    # and nothing landed in the chat
    chat = await page.evaluate(
        '() => document.querySelector("#chat [role=textbox]").innerText'
    )
    assert chat.strip() == ""


# -- the date format is learned, not assumed ----------------------------------


async def write_schedule(page, sample_date, sample_time, target):
    await set_page(page, schedule=(sample_date, sample_time))
    return await page.evaluate(
        cp._SET_SCHEDULE_JS,
        {"iso": f"{target['y']:04d}-{target['m']:02d}-{target['d']:02d}",
         "today": TODAY, "target": target},
    )


TARGET_TODAY = {"y": 2026, "m": 10, "d": 9, "hh": 14, "mm": 30}
TARGET_OTHER = {"y": 2026, "m": 11, "d": 3, "hh": 9, "mm": 5}


async def test_german_sample_keeps_the_german_layout(page):
    got = await write_schedule(page, "9.10.2026", "14:00", TARGET_TODAY)
    assert got["ok"] is True
    assert got["wrote_date"] == "9.10.2026"
    assert got["want_time"] == "14:30"


async def test_german_sample_renders_another_day_the_same_way(page):
    got = await write_schedule(page, "9.10.2026", "14:00", TARGET_OTHER)
    assert got["ok"] is True
    assert got["wrote_date"] == "3.11.2026"
    assert got["want_time"] == "09:05"


async def test_a_padded_german_sample_stays_padded(page):
    got = await write_schedule(page, "09.10.2026", "14:00", TARGET_OTHER)
    assert got["ok"] is True
    assert got["wrote_date"] == "03.11.2026"


async def test_a_us_sample_gets_month_first(page):
    got = await write_schedule(page, "10/9/2026", "2:00 PM", TARGET_OTHER)
    assert got["ok"] is True
    assert got["wrote_date"] == "11/3/2026"
    assert got["want_time"] == "9:05 AM"


async def test_an_iso_sample_stays_iso(page):
    got = await write_schedule(page, "2026-10-09", "14:00", TARGET_OTHER)
    assert got["ok"] is True
    assert got["wrote_date"] == "2026-11-03"


async def test_a_twelve_hour_sample_in_the_afternoon(page):
    got = await write_schedule(page, "9.10.2026", "2:00 PM", TARGET_TODAY)
    assert got["ok"] is True
    assert got["want_time"] == "2:30 PM"


async def test_the_schedule_dialog_is_found_past_the_chat_overlay(page):
    # The chat carries an input as well; only the schedule dialog has a date.
    got = await write_schedule(page, "9.10.2026", "14:00", TARGET_TODAY)
    assert got["ok"] is True
    where = await page.evaluate(
        '() => document.querySelector("#schedule input").value'
    )
    assert where == "9.10.2026"


async def test_without_a_schedule_dialog_nothing_is_written(page):
    await set_page(page)  # no schedule dialog at all
    got = await page.evaluate(
        cp._SET_SCHEDULE_JS,
        {"iso": "2026-10-09", "today": TODAY, "target": TARGET_TODAY},
    )
    assert got["ok"] is False
    assert got["why"] == "no_schedule_dialog"


async def test_the_confirm_button_of_the_schedule_dialog_is_its_own(page):
    await set_page(page, schedule=("9.10.2026", "14:00"))
    got = await mark(
        page,
        "schedule-done",
        words=cp._SCHEDULE_NEXT_WORDS,
        anchor="input",
        anchor_words=cp._SCHEDULE_NEXT_WORDS,
    )
    assert got["count"] == 1
    where = await dialog_of(page, "schedule-done")
    assert where == "schedule"


# -- the time is picked from the list, never typed -----------------------------


async def test_the_wanted_quarter_hour_is_marked(page):
    await set_page(page, schedule=("9.10.2026", "14:45"))
    got = await page.evaluate(cp._PICK_TIME_JS, {"time": "15:00"})
    assert got["count"] == 1
    text = await page.evaluate('() => document.querySelector("[data-ext-time]").innerText')
    assert text.strip() == "15:00"


async def test_a_time_outside_the_list_marks_nothing(page):
    await set_page(page, schedule=("9.10.2026", "14:45"))
    got = await page.evaluate(cp._PICK_TIME_JS, {"time": "15:07"})
    assert got["count"] == 0
    assert "14:45" in got["seen"]
    marked = await page.evaluate('() => !!document.querySelector("[data-ext-time]")')
    assert marked is False


async def test_the_time_field_is_read_back_from_the_dialog(page):
    await set_page(page, schedule=("9.10.2026", "14:45"))
    assert await page.evaluate(cp._TIME_VALUE_JS) == "14:45"


async def test_the_date_is_not_rewritten_when_it_already_matches(page):
    got = await write_schedule(page, "9.10.2026", "14:45", TARGET_TODAY)
    assert got["ok"] is True
    assert got["wrote_date"] == "9.10.2026"


# -- "is the composer gone?" must not answer about the chat window ------------


async def test_a_closed_composer_counts_as_gone_even_with_the_chat_open(page):
    # Exactly the false alarm of 2026-10-09: the post was scheduled, the
    # composer had closed, and the chat window's textbox kept the old check
    # from seeing it.
    await page.set_content(f"<main>{CHAT}</main>")
    gone = await page.evaluate(
        cp._COMPOSER_GONE_JS, {"commit_words": cp.COMPOSER_WORDS}
    )
    assert gone is True


async def test_an_open_composer_is_not_gone(page):
    await set_page(page)
    gone = await page.evaluate(
        cp._COMPOSER_GONE_JS, {"commit_words": cp.COMPOSER_WORDS}
    )
    assert gone is False


async def test_a_composer_showing_planen_is_not_gone_either(page):
    await set_page(page, commit="Planen")
    gone = await page.evaluate(
        cp._COMPOSER_GONE_JS, {"commit_words": cp.COMPOSER_WORDS}
    )
    assert gone is False
