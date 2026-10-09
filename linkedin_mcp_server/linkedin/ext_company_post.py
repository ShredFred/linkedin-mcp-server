"""Fork extension: compose a post **as a company page** -- draft, scheduled or live.

Built 2026-10-09, after the MiViA page (81728804) granted the signed-in member
a Content-Admin role. ``create_post`` deliberately refuses ``as_company``; this
module is the implementation it pointed at.

**The entry point is the whole design, and the first attempt got it wrong.**
``/feed/?shareActive=true`` opens the *member* composer: measured on 2026-10-09
it carries five buttons, no author control and no schedule clock, so posting as
a page from there would have meant switching an author that cannot be switched.
The page's own admin view does carry them. Opening the composer from
``/company/<id>/admin/page-posts/published/`` means **the author is the page by
construction** -- so this module never switches an author, it only *verifies*
one, which is a much smaller thing to get wrong.

Measured controls inside that dialog (de locale, 2026-10-09):

* author and audience: a button reading ``MiViA Auf Alle posten``
* editor: ``[role="textbox"]`` inside the dialog -- note that this is *not*
  ``componentkey="ShareBox_textEditor"``, which belongs to the member composer
* media: ``aria-label="Mediendatei hinzufügen"``
* schedule: ``aria-label="Termin für Beitrag festlegen"``
* publish: a button reading ``Posten``, disabled while the editor is empty

Everything is fail-closed: every lookup demands exactly one visible match, the
author is re-read immediately before the commit click, and a schedule is only
committed when the composer shows the time back. A dry run never uploads an
image and discards the composer at the end.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from linkedin_mcp_server.linkedin.ext_post import _IMAGE_SUFFIXES
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession

logger = logging.getLogger(__name__)

MODES = ("draft", "schedule", "publish")

ADMIN_POSTS_URL = "https://www.linkedin.com/company/{page_id}/admin/page-posts/published/"

# Opens the page composer from the admin view. Text, not aria-label.
_START_WORDS = ["beitrag beginnen", "start a post", "beitrag erstellen", "create a post"]
_MEDIA_LABELS = ["mediendatei hinzufügen", "add media", "medieninhalte", "foto hinzufügen"]
_SCHEDULE_LABELS = [
    "termin für beitrag festlegen",
    "termin fuer beitrag festlegen",
    "schedule post",
    "beitrag planen",
]
_POST_WORDS = ["posten", "post", "veröffentlichen", "publish"]
_SCHEDULE_CONFIRM_WORDS = ["weiter", "next", "fertig", "done", "planen", "schedule"]
_DISCARD_LABELS = ["verwerfen", "discard", "schließen", "close", "dismiss"]
_DRAFT_WORDS = [
    "als entwurf speichern",
    "save as draft",
    "entwurf speichern",
    "save draft",
    "speichern",
]
_NEXT_WORDS = ["weiter", "next", "fertig", "done"]

# The editor of the page composer. Dialog-scoped and locale-independent: the
# aria-label is German here and would not survive a locale switch.
_EDITOR_IN_DIALOG = '[role="dialog"] [role="textbox"], dialog [role="textbox"]'


def check_mode(mode: str) -> dict[str, Any] | None:
    if mode not in MODES:
        return {
            "status": "invalid_input",
            "field": "mode",
            "message": f"mode must be one of {', '.join(MODES)}",
        }
    return None


def check_page_id(page_id: str) -> dict[str, Any] | None:
    if not str(page_id).strip().isdigit():
        return {
            "status": "invalid_input",
            "field": "page_id",
            "message": "page_id is the numeric page id, e.g. 81728804.",
        }
    return None


def check_schedule(mode: str, scheduled_at: str | None) -> dict[str, Any] | None:
    """A schedule must be a future wall-clock time, to the minute.

    LinkedIn accepts times on its own grid and a few minutes ahead only; a past
    time can silently become "now", which would publish immediately. Refused
    here rather than discovered in the dialog.
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


# Mark exactly one visible control, inside the dialog or on the page.
# Returns what it saw when the match is not unique, so a failure measures.
_MARK_JS = r"""(arg) => {
  document.querySelectorAll('[data-ext-company]').forEach(e => e.removeAttribute('data-ext-company'));
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const norm = v => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const dialog = document.querySelector('[role="dialog"], dialog');
  const root = arg.scope === 'dialog' ? dialog : document;
  if (!root) return {count: 0, no_dialog: true};
  const sel = 'button, [role="button"], [role="menuitem"]';
  const nodes = [...root.querySelectorAll(sel)].filter(visible);
  const hits = nodes.filter(el =>
    arg.labels.some(l => norm(el.getAttribute('aria-label')) === l) ||
    arg.words.some(w => norm(el.innerText) === w));
  if (hits.length !== 1) {
    return {count: hits.length,
            seen: nodes.slice(0, 20).map(el => ({
              text: norm(el.innerText).slice(0, 60),
              label: norm(el.getAttribute('aria-label')).slice(0, 60)}))};
  }
  hits[0].setAttribute('data-ext-company', arg.tag);
  return {count: 1,
          disabled: hits[0].disabled === true || hits[0].getAttribute('aria-disabled') === 'true'};
}"""

# The author/audience button of the page composer, e.g. "MiViA Auf Alle posten".
# Read, never clicked: opening the composer from the page's admin view already
# makes the page the author.
_AUTHOR_JS = r"""(arg) => {
  const dialog = document.querySelector('[role="dialog"], dialog');
  if (!dialog) return {ok: false, why: 'no_dialog'};
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const norm = v => String(v || '').replace(/\s+/g, ' ').trim();
  const buttons = [...dialog.querySelectorAll('button, [role="button"]')].filter(visible);
  const texts = buttons.map(b => norm(b.innerText)).filter(Boolean);
  const want = String(arg.name || '').toLowerCase();
  return {ok: texts.some(t => t.toLowerCase().includes(want)), texts: texts.slice(0, 8)};
}"""

_WRITE_JS = r"""(arg) => {
  const canon = v => String(v || '').replace(/[ \t ]*\n[\s ]*/g, '\n').trim();
  const editor = document.querySelector(arg.selector);
  if (!editor) return 'missing';
  if (canon(editor.innerText)) return 'occupied';
  editor.focus();
  if (document.activeElement !== editor) return 'unfocused';
  let ok = true;
  arg.text.split('\n').forEach((line, index) => {
    if (index > 0) ok = document.execCommand('insertParagraph', false) === true && ok;
    if (line) ok = document.execCommand('insertText', false, line) === true && ok;
  });
  if (!ok) return 'unsupported';
  return canon(editor.innerText) === canon(arg.text) ? 'written' : 'mismatch';
}"""

_CLEAR_JS = r"""(selector) => {
  const editor = document.querySelector(selector);
  if (!editor) return false;
  editor.focus();
  document.execCommand('selectAll', false);
  document.execCommand('delete', false);
  return !(editor.innerText || '').trim();
}"""

_MEDIA_PRESENT_JS = r"""(selector) => {
  const dialog = document.querySelector(selector)?.closest('[role="dialog"], dialog');
  if (!dialog) return 0;
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  return [...dialog.querySelectorAll('img, video')]
      .filter(el => visible(el) && (el.width > 80 || el.videoWidth > 80)).length;
}"""

_SET_SCHEDULE_JS = r"""(arg) => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const dialogs = [...document.querySelectorAll('[role="dialog"], dialog')].filter(visible);
  const root = dialogs[dialogs.length - 1];
  if (!root) return {ok: false, why: 'no_dialog'};
  const inputs = [...root.querySelectorAll('input')].filter(visible);
  const setter = (el, v) => {
    const d = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value');
    if (d && d.set) d.set.call(el, v); else el.value = v;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  };
  const label = i => String(i.getAttribute('aria-label') || '').toLowerCase();
  const dateInput = inputs.find(i => (i.type || '').toLowerCase() === 'date')
                 || inputs.find(i => /datum|date/.test(label(i)));
  const timeInput = inputs.find(i => (i.type || '').toLowerCase() === 'time')
                 || inputs.find(i => /zeit|uhrzeit|time/.test(label(i)));
  if (!dateInput || !timeInput) {
    return {ok: false, why: 'inputs_missing',
            seen: inputs.slice(0, 10).map(i => ({type: i.type,
                                                 label: label(i).slice(0, 50),
                                                 value: i.value}))};
  }
  setter(dateInput, arg.date);
  setter(timeInput, arg.time);
  return {ok: dateInput.value === arg.date && timeInput.value === arg.time,
          date: dateInput.value, time: timeInput.value};
}"""

_DIALOG_TEXT_JS = r"""() => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const dialogs = [...document.querySelectorAll('[role="dialog"], dialog')].filter(visible);
  const root = dialogs[dialogs.length - 1];
  return root ? (root.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 500) : '';
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
    ) -> dict[str, Any]:
        return await self._page.evaluate(
            _MARK_JS,
            {"words": words or [], "labels": labels or [], "tag": tag, "scope": scope},
        )

    async def _discard(self, result: dict[str, Any]) -> None:
        """Fail-safe cleanup; never raises, never clicks a publish button."""
        try:
            found = await self._mark("discard", labels=_DISCARD_LABELS)
            if found.get("count") == 1:
                await self._page.click('[data-ext-company="discard"]')
                await asyncio.sleep(1.0)
                # A discard prompt may appear; confirm it, never a post button.
                again = await self._mark("discard2", words=["verwerfen", "discard"], scope="document")
                if again.get("count") == 1:
                    await self._page.click('[data-ext-company="discard2"]')
                result["cleanup"] = "discarded"
            else:
                result["cleanup"] = "nothing_to_discard"
        except Exception as exc:  # noqa: BLE001 - cleanup must not raise
            result["cleanup_error"] = f"{type(exc).__name__}: {exc}"[:200]

    async def _author_ok(self, page_name: str) -> dict[str, Any]:
        return await self._page.evaluate(_AUTHOR_JS, {"name": page_name})

    async def create_company_post(
        self,
        page_id: str,
        page_name: str,
        text: str,
        *,
        image_path: str | None,
        mode: str,
        scheduled_at: str | None,
        confirm: bool,
    ) -> dict[str, Any]:
        url = ADMIN_POSTS_URL.format(page_id=page_id)
        result: dict[str, Any] = {
            "url": url,
            "page_id": page_id,
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

        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        await self._session.delay(3.0)

        # 1. Open the page composer from the admin view. This is what makes the
        #    page the author; there is no author switch to get wrong.
        opener = await self._mark("start", words=_START_WORDS, scope="document")
        if opener.get("count") != 1:
            return {
                **result,
                "status": "composer_opener_unavailable",
                "found": opener,
                "message": "No single 'start a post' control on the page admin view. "
                "Is the member still a page admin?",
            }
        await self._page.click('[data-ext-company="start"]')
        await self._session.delay(3.0)

        try:
            await self._page.wait_for_selector(_EDITOR_IN_DIALOG, timeout=15_000)
        except Exception:
            return {
                **result,
                "status": "composer_unavailable",
                "message": "the page composer did not open",
            }

        # 2. Verify the author. Not switched -- verified.
        author = await self._author_ok(page_name)
        if not author.get("ok"):
            result["status"] = "author_not_confirmed"
            result["author_shown"] = author
            result["message"] = (
                f"The composer does not name {page_name!r}; nothing was written."
            )
            await self._discard(result)
            return result
        result["author"] = page_name

        leftover = await self._page.evaluate(_MEDIA_PRESENT_JS, _EDITOR_IN_DIALOG)
        if leftover:
            result["status"] = "editor_has_media"
            result["attached"] = leftover
            result["message"] = (
                "The composer already carries attached media (a restored draft). "
                "Remove it in the browser first; nothing was written."
            )
            await self._discard(result)
            return result

        # 3. Text.
        written = await self._page.evaluate(
            _WRITE_JS, {"selector": _EDITOR_IN_DIALOG, "text": text}
        )
        if written != "written":
            result["status"] = "text_not_written"
            result["write"] = written
            await self._discard(result)
            return result

        # 4. Image. A dry run never uploads: an attached image cannot be taken
        #    out again and LinkedIn restores it as a draft.
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
                async with self._page.expect_file_chooser(timeout=15_000) as info:
                    await self._page.click('[data-ext-company="media"]')
                chooser = await info.value
                await chooser.set_files(str(image))
                await self._session.delay(6.0)
                nxt = await self._mark("media-next", words=_NEXT_WORDS, scope="document")
                if nxt.get("count") == 1:
                    await self._page.click('[data-ext-company="media-next"]')
                    await self._session.delay(2.0)
                result["image"] = image.name
                result["image_step"] = "uploaded"

        # 5. Schedule.
        if mode == "schedule":
            assert scheduled_at  # guaranteed by check_schedule
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
                await self._session.delay(2.0)
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
                summary = await self._page.evaluate(_DIALOG_TEXT_JS)
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

        # 6. Dry run ends here.
        if not confirm:
            cleared = await self._page.evaluate(_CLEAR_JS, _EDITOR_IN_DIALOG)
            if not cleared:
                logger.warning("create_company_post dry run: editor not cleared")
            await self._discard(result)
            return {**result, "status": "dry_run"}

        # 7. Last look at the author: everything above could have re-rendered
        #    the dialog, and this is the last harmless moment.
        author = await self._author_ok(page_name)
        if not author.get("ok"):
            result["status"] = "author_lost"
            result["author_shown"] = author
            result["message"] = (
                "The composer no longer names the page; nothing was published."
            )
            await self._discard(result)
            return result

        # 8. Commit.
        if mode == "draft":
            close = await self._mark("close", labels=_DISCARD_LABELS)
            if close.get("count") != 1:
                result["status"] = "close_unavailable"
                result["found"] = close
                return result
            await self._page.click('[data-ext-company="close"]')
            await self._session.delay(2.0)
            save = await self._mark("draft", words=_DRAFT_WORDS, scope="document")
            if save.get("count") != 1:
                result["status"] = "draft_prompt_unavailable"
                result["found"] = save
                result["message"] = (
                    "The close prompt offered no single save-as-draft entry; the "
                    "composer may still be open. Check the browser."
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
        await self._session.delay(5.0)

        gone = await self._page.evaluate(
            "(s) => !document.querySelector(s)", _EDITOR_IN_DIALOG
        )
        if mode == "schedule":
            status = "scheduled" if gone else "schedule_unconfirmed"
        else:
            status = "posted_unverified" if gone else "post_unconfirmed"
        return {**result, "status": status, "posted": mode == "publish" and gone}
