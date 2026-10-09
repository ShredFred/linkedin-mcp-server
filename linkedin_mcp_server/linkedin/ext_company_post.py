"""Fork extension: compose a post **as a company page** -- draft, scheduled or live.

Built 2026-10-09, after the MiViA page (81728804) granted the signed-in member
a Content-Admin role. ``create_post`` deliberately refuses ``as_company``; this
module is the implementation it pointed at.

Three things make this riskier than a personal post, and each one is answered
by a verification rather than by a hopeful click:

1. **The author.** The share composer opens as the *member*. Posting as a page
   means switching the author first, and a switch that silently fails publishes
   company content under a private name. The author shown in the composer is
   therefore read back and compared against the page before anything is
   clicked, and again right before the final click.
2. **The wording.** Author menu, schedule dialog and draft prompt are plain
   localised text with no stable attributes. Every lookup demands *exactly one*
   visible match; zero or several is a stop, never a guess.
3. **Scheduling publishes later.** A scheduled post goes out without a further
   click, so a half-finished schedule is worse than a failed one: it can leave
   a live post nobody is watching. The schedule is confirmed by reading the
   composer's own summary back before the final click, and the result says
   plainly when it could not be confirmed (``unverified``).

A dry run never uploads an image, never opens the schedule dialog beyond
reading it, and leaves the composer discarded.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from linkedin_mcp_server.linkedin.ext_post import (
    _CLEAR_JS,
    _CLOSE_LABELS,
    _DISCARD_JS,
    _DISCARD_WORDS,
    _EDITOR,
    _IMAGE_SUFFIXES,
    _MEDIA_LABELS,
    _MEDIA_PRESENT_JS,
    _NEXT_WORDS,
    _POST_WORDS,
    _WRITE_JS,
    FEED_URL,
    SHARE_URL,
)
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession

logger = logging.getLogger(__name__)

MODES = ("draft", "schedule", "publish")

# The control that opens the author list. LinkedIn renders it as a button
# carrying the current author's name; its aria-label or adjacent text is the
# only locale-independent-ish anchor we have, so several spellings are tried
# and more than one hit is a stop.
_AUTHOR_BUTTON_LABELS = [
    "posten als",
    "post as",
    "beitrag verfassen als",
    "autor auswählen",
    "select author",
    "author",
    "veröffentlichen als",
    "publish as",
]
_SCHEDULE_LABELS = [
    "beitrag planen",
    "planen",
    "schedule post",
    "schedule",
    "zeitplan",
]
_SCHEDULE_CONFIRM_WORDS = ["planen", "schedule", "weiter", "next", "fertig", "done"]
_DRAFT_WORDS = ["als entwurf speichern", "save as draft", "entwurf speichern", "save draft"]

_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def check_mode(mode: str) -> dict[str, Any] | None:
    if mode not in MODES:
        return {
            "status": "invalid_input",
            "field": "mode",
            "message": f"mode must be one of {', '.join(MODES)}",
        }
    return None


def check_schedule(mode: str, scheduled_at: str | None) -> dict[str, Any] | None:
    """A schedule must be a future wall-clock time, to the minute.

    LinkedIn only accepts times on its own grid and at least a few minutes
    ahead; a past time silently becomes "now" in some locales, which would
    publish immediately. Refused here rather than discovered in the dialog.
    """
    if mode != "schedule":
        if scheduled_at:
            return {
                "status": "invalid_input",
                "field": "scheduled_at",
                "message": "scheduled_at is only valid with mode=schedule.",
            }
        return None
    if not scheduled_at:
        return {
            "status": "invalid_input",
            "field": "scheduled_at",
            "message": "mode=schedule needs scheduled_at as 'YYYY-MM-DD HH:MM' (local time).",
        }
    try:
        when = datetime.strptime(scheduled_at, "%Y-%m-%d %H:%M").astimezone()
    except ValueError:
        return {
            "status": "invalid_input",
            "field": "scheduled_at",
            "message": "scheduled_at must read 'YYYY-MM-DD HH:MM' (local time).",
        }
    ahead = (when - datetime.now().astimezone()).total_seconds()
    if ahead < 300:
        return {
            "status": "schedule_too_soon",
            "message": "LinkedIn needs the scheduled time at least 5 minutes ahead; "
            f"{scheduled_at} is {int(ahead)} s away.",
        }
    if ahead > 90 * 24 * 3600:
        return {
            "status": "schedule_too_far",
            "message": "LinkedIn schedules at most 90 days ahead.",
        }
    return None


# Read the author currently selected in the composer. Returns the visible text
# of the author control, which carries the page or member name.
_AUTHOR_READ_JS = r"""(arg) => {
  const dialog = document.querySelector(arg.selector)?.closest('[role="dialog"], dialog');
  if (!dialog) return null;
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const norm = v => String(v || '').trim().toLowerCase();
  const buttons = [...dialog.querySelectorAll('button')].filter(visible);
  const hits = buttons.filter(b =>
    arg.labels.includes(norm(b.getAttribute('aria-label'))) ||
    arg.labels.some(l => norm(b.getAttribute('aria-label')).startsWith(l)) ||
    arg.labels.some(l => norm(b.innerText).startsWith(l)));
  // Fall back to the first button that sits above the editor and shows a name.
  const text = b => (b.innerText || '').replace(/\s+/g, ' ').trim();
  return {
    count: hits.length,
    texts: hits.map(text).slice(0, 5),
    all: buttons.slice(0, 12).map(b => ({
      text: text(b).slice(0, 80),
      label: (b.getAttribute('aria-label') || '').slice(0, 80),
    })),
  };
}"""

# Mark the author control for clicking; exactly one match or nothing happens.
_MARK_JS = r"""(arg) => {
  document.querySelectorAll('[data-ext-company]').forEach(e => e.removeAttribute('data-ext-company'));
  const root = arg.scope === 'dialog'
    ? document.querySelector(arg.selector)?.closest('[role="dialog"], dialog')
    : document;
  if (!root) return {count: 0};
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const norm = v => String(v || '').trim().toLowerCase();
  const nodes = [...root.querySelectorAll(arg.tagname || 'button')].filter(visible);
  const hits = nodes.filter(b =>
    arg.labels.some(l => norm(b.getAttribute('aria-label')) === l) ||
    arg.words.some(w => norm(b.innerText) === w));
  if (hits.length !== 1) {
    return {count: hits.length,
            seen: nodes.slice(0, 15).map(b => ({
              text: (b.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 60),
              label: (b.getAttribute('aria-label') || '').slice(0, 60)}))};
  }
  hits[0].setAttribute('data-ext-company', arg.tag);
  return {count: 1,
          disabled: hits[0].disabled || hits[0].getAttribute('aria-disabled') === 'true'};
}"""

# Pick the page in the opened author list: an option whose text contains the
# page name. Exactly one, otherwise nothing is clicked.
_PICK_AUTHOR_JS = r"""(arg) => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const norm = v => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const want = norm(arg.name);
  const options = [...document.querySelectorAll(
      '[role="radio"], [role="option"], [role="menuitem"], [role="menuitemradio"], label')]
    .filter(visible);
  const hits = options.filter(o => norm(o.innerText).includes(want));
  if (hits.length !== 1) {
    return {count: hits.length,
            seen: options.slice(0, 15).map(o => norm(o.innerText).slice(0, 60))};
  }
  hits[0].setAttribute('data-ext-company', 'author-option');
  return {count: 1};
}"""

# Fill the schedule dialog: a date input and a time input. Values are set via
# the native setter plus input/change so React picks them up.
_SET_SCHEDULE_JS = r"""(arg) => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const dialogs = [...document.querySelectorAll('[role="dialog"], dialog')].filter(visible);
  const root = dialogs[dialogs.length - 1];
  if (!root) return {ok: false, why: 'no_dialog'};
  const inputs = [...root.querySelectorAll('input')].filter(visible);
  const setter = (el, v) => {
    const proto = Object.getPrototypeOf(el);
    const d = Object.getOwnPropertyDescriptor(proto, 'value');
    if (d && d.set) d.set.call(el, v); else el.value = v;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  };
  const byKind = k => inputs.find(i =>
    (i.type || '').toLowerCase() === k ||
    /date/i.test(i.getAttribute('aria-label') || '') && k === 'date' ||
    /time|uhrzeit/i.test(i.getAttribute('aria-label') || '') && k === 'time');
  const dateInput = byKind('date');
  const timeInput = byKind('time');
  if (!dateInput || !timeInput) {
    return {ok: false, why: 'inputs_missing',
            seen: inputs.map(i => ({type: i.type,
                                    label: (i.getAttribute('aria-label') || '').slice(0, 50),
                                    value: i.value}))};
  }
  setter(dateInput, arg.date);
  setter(timeInput, arg.time);
  return {ok: dateInput.value === arg.date && timeInput.value === arg.time,
          date: dateInput.value, time: timeInput.value};
}"""

# After the schedule is accepted the composer shows it; read it back.
_SCHEDULE_SUMMARY_JS = r"""(selector) => {
  const dialog = document.querySelector(selector)?.closest('[role="dialog"], dialog');
  if (!dialog) return '';
  return (dialog.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 400);
}"""


class ExtCompanyPostComposer:
    """Compose, schedule or draft a post authored by a company page."""

    def __init__(self, session: PageSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator
        self.clicked = False

    @property
    def _page(self) -> Any:
        return self._session.page

    async def _mark(
        self,
        tag: str,
        *,
        words: list[str] | None = None,
        labels: list[str] | None = None,
        scope: str = "dialog",
        tagname: str = "button",
    ) -> dict[str, Any]:
        return await self._page.evaluate(
            _MARK_JS,
            {
                "selector": _EDITOR,
                "words": words or [],
                "labels": labels or [],
                "tag": tag,
                "scope": scope,
                "tagname": tagname,
            },
        )

    async def _discard(self, result: dict[str, Any]) -> None:
        """Fail-safe cleanup; never raises, never clicks a publish button."""
        try:
            arg = {
                "close": _CLOSE_LABELS,
                "discard": _DISCARD_WORDS,
                "post": _POST_WORDS,
                "discard_only": False,
            }
            steps = await self._page.evaluate(_DISCARD_JS, arg)
            await asyncio.sleep(1.0)
            second = await self._page.evaluate(_DISCARD_JS, {**arg, "discard_only": True})
            if second != "nothing_to_close":
                steps = f"{steps}+{second}"
            result["cleanup"] = steps
            await self._navigator._navigate_to_page(FEED_URL)
        except Exception as exc:  # noqa: BLE001 - cleanup must not raise
            result["cleanup_error"] = f"{type(exc).__name__}: {exc}"[:200]

    async def _author_text(self) -> dict[str, Any] | None:
        return await self._page.evaluate(
            _AUTHOR_READ_JS, {"selector": _EDITOR, "labels": _AUTHOR_BUTTON_LABELS}
        )

    async def _switch_author(self, page_name: str, result: dict[str, Any]) -> bool:
        """Switch the composer author to *page_name*; True only when verified."""
        opener = await self._mark("author", labels=_AUTHOR_BUTTON_LABELS)
        if opener.get("count") != 1:
            result["status"] = "author_control_unavailable"
            result["found"] = opener
            result["message"] = (
                "The author control of the share composer was not uniquely "
                "identifiable; nothing was clicked."
            )
            return False
        await self._page.click('[data-ext-company="author"]')
        await self._session.delay(1.5)

        picked = await self._page.evaluate(_PICK_AUTHOR_JS, {"name": page_name})
        if picked.get("count") != 1:
            result["status"] = "author_option_unavailable"
            result["found"] = picked
            result["message"] = (
                f"No single author entry matching {page_name!r}. Is the member a "
                "page admin? Nothing was clicked."
            )
            return False
        await self._page.click('[data-ext-company="author-option"]')
        await self._session.delay(1.5)

        # Some locales need an explicit confirm in the author sheet.
        confirm = await self._mark("author-done", words=_NEXT_WORDS, scope="document")
        if confirm.get("count") == 1:
            await self._page.click('[data-ext-company="author-done"]')
            await self._session.delay(1.5)

        shown = await self._author_text()
        texts = " ".join((shown or {}).get("texts") or [])
        if page_name.lower() not in texts.lower():
            result["status"] = "author_not_confirmed"
            result["author_shown"] = shown
            result["message"] = (
                f"The composer does not show {page_name!r} as author after the "
                "switch; nothing was written."
            )
            return False
        result["author"] = page_name
        return True

    async def create_company_post(
        self,
        page_name: str,
        text: str,
        *,
        image_path: str | None,
        mode: str,
        scheduled_at: str | None,
        confirm: bool,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "url": SHARE_URL,
            "page": page_name,
            "mode": mode,
            "posted": False,
            "confirm": confirm,
        }
        image: Path | None = None
        if image_path:
            image = Path(image_path).expanduser()
            if not image.is_file() or image.suffix.lower() not in _IMAGE_SUFFIXES:
                return {
                    **result,
                    "status": "invalid_image",
                    "message": f"not an image file: {image}",
                }

        await self._navigator._navigate_to_page(SHARE_URL)
        await self._session.check_rate_limit()
        try:
            await self._page.wait_for_selector(_EDITOR, timeout=15_000)
        except Exception:
            return {
                **result,
                "status": "composer_unavailable",
                "message": "share editor did not open",
            }

        leftover = await self._page.evaluate(_MEDIA_PRESENT_JS, _EDITOR)
        if leftover:
            await self._navigator._navigate_to_page(FEED_URL)
            return {
                **result,
                "status": "editor_has_media",
                "attached": leftover,
                "message": "The share composer already carries attached media "
                "(a restored draft). Remove it in the browser first; nothing was written.",
            }

        # 1. Author first. Writing into a composer that still belongs to the
        #    member and switching afterwards risks losing the text.
        if not await self._switch_author(page_name, result):
            await self._discard(result)
            return result

        # 2. Text.
        written = await self._page.evaluate(
            _WRITE_JS, {"selector": _EDITOR, "text": text}
        )
        if written != "written":
            result["status"] = "text_not_written"
            result["write"] = written
            await self._discard(result)
            return result

        # 3. Image. A dry run never uploads: an attached image cannot be taken
        #    out again and LinkedIn keeps it as a restored draft.
        if image is not None:
            media = await self._mark("media", labels=_MEDIA_LABELS)
            if media.get("count") != 1:
                result["status"] = "media_button_unavailable"
                result["found"] = media
                await self._discard(result)
                return result
            if not confirm:
                result["image"] = image.name
                result["image_step"] = "not_uploaded_dry_run"
            else:
                async with self._page.expect_file_chooser(timeout=10_000) as info:
                    await self._page.click('[data-ext-company="media"]')
                chooser = await info.value
                await chooser.set_files(str(image))
                await self._session.delay(5.0)
                nxt = await self._mark("media-next", words=_NEXT_WORDS, scope="document")
                if nxt.get("count") == 1:
                    await self._page.click('[data-ext-company="media-next"]')
                    await self._session.delay(2.0)
                result["image"] = image.name
                result["image_step"] = "uploaded"

        # 4. Schedule, when asked for.
        if mode == "schedule":
            assert scheduled_at  # checked by check_schedule before we got here
            day, clock = scheduled_at.split(" ")
            opener = await self._mark("schedule", labels=_SCHEDULE_LABELS)
            if opener.get("count") != 1:
                result["status"] = "schedule_control_unavailable"
                result["found"] = opener
                await self._discard(result)
                return result
            if not confirm:
                result["schedule_step"] = "not_opened_dry_run"
            else:
                await self._page.click('[data-ext-company="schedule"]')
                await self._session.delay(1.5)
                filled = await self._page.evaluate(
                    _SET_SCHEDULE_JS, {"date": day, "time": clock}
                )
                if not filled.get("ok"):
                    result["status"] = "schedule_not_filled"
                    result["found"] = filled
                    await self._discard(result)
                    return result
                done = await self._mark(
                    "schedule-done", words=_SCHEDULE_CONFIRM_WORDS, scope="document"
                )
                if done.get("count") != 1:
                    result["status"] = "schedule_confirm_unavailable"
                    result["found"] = done
                    await self._discard(result)
                    return result
                await self._page.click('[data-ext-company="schedule-done"]')
                await self._session.delay(2.0)
                summary = await self._page.evaluate(_SCHEDULE_SUMMARY_JS, _EDITOR)
                result["composer_summary"] = summary
                if clock not in summary:
                    result["status"] = "schedule_not_confirmed"
                    result["message"] = (
                        f"The composer does not show {clock} after the schedule "
                        "dialog; nothing was published."
                    )
                    await self._discard(result)
                    return result
                result["scheduled_at"] = scheduled_at

        # 5. Dry run ends here: clear the text and leave.
        if not confirm:
            cleared = await self._page.evaluate(_CLEAR_JS, _EDITOR)
            if not cleared:
                logger.warning("create_company_post dry run: editor not cleared")
            await self._discard(result)
            return {**result, "status": "dry_run"}

        # 6. The author is read once more: everything above could have
        #    re-rendered the composer, and this is the last moment at which a
        #    wrong author is still harmless.
        shown = await self._author_text()
        texts = " ".join((shown or {}).get("texts") or [])
        if page_name.lower() not in texts.lower():
            result["status"] = "author_lost"
            result["author_shown"] = shown
            result["message"] = (
                "The composer no longer shows the page as author; nothing was published."
            )
            await self._discard(result)
            return result

        # 7. Commit.
        if mode == "draft":
            close = await self._mark("close", labels=_CLOSE_LABELS)
            if close.get("count") != 1:
                result["status"] = "close_unavailable"
                result["found"] = close
                return result
            await self._page.click('[data-ext-company="close"]')
            await self._session.delay(1.5)
            save = await self._mark("draft", words=_DRAFT_WORDS, scope="document")
            if save.get("count") != 1:
                result["status"] = "draft_prompt_unavailable"
                result["found"] = save
                result["message"] = (
                    "The discard/draft prompt did not offer a single save entry; "
                    "the composer may still be open. Check the browser."
                )
                return result
            self.clicked = True
            await self._page.click('[data-ext-company="draft"]')
            await self._session.delay(2.0)
            return {**result, "status": "draft_saved", "posted": False}

        words = _SCHEDULE_CONFIRM_WORDS if mode == "schedule" else _POST_WORDS
        button = await self._mark("post", words=words)
        if button.get("count") != 1:
            result["status"] = "post_button_unavailable"
            result["found"] = button
            await self._discard(result)
            return result
        if button.get("disabled"):
            result["status"] = "post_button_disabled"
            await self._discard(result)
            return result

        self.clicked = True
        await self._page.click('[data-ext-company="post"]')
        await self._session.delay(4.0)

        gone = await self._page.evaluate(
            "(s) => !document.querySelector(s)", _EDITOR
        )
        if mode == "schedule":
            status = "scheduled" if gone else "schedule_unconfirmed"
        else:
            status = "posted_unverified" if gone else "post_unconfirmed"
        return {**result, "status": status, "posted": mode == "publish" and gone}
