"""Fork extension: compose a post **as a company page** -- draft, scheduled or live.

Built 2026-10-09, after a company page granted the signed-in member
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

* author and audience: a button reading ``Acme Labs Auf Alle posten``
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

from linkedin_mcp_server.linkedin.ext_mentions import (
    MentionWriter,
    Segment,
    mentions_of,
    plain_text,
    resolve_targets,
)
from linkedin_mcp_server.linkedin.ext_composer_labels import words
from linkedin_mcp_server.linkedin.ext_media import MediaAttacher
from linkedin_mcp_server.linkedin.ext_post import _IMAGE_SUFFIXES
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession

logger = logging.getLogger(__name__)

MODES = ("draft", "schedule", "publish")

ADMIN_POSTS_URL = "https://www.linkedin.com/company/{page_id}/admin/page-posts/published/"

# Opens the page composer from the admin view. Text, not aria-label.
_START_WORDS = words("start_post")
_MEDIA_LABELS = words("start_post_media")
_SCHEDULE_LABELS = words("schedule")
_POST_WORDS = words("post")
# Confirms the schedule dialog and returns to the composer.
_SCHEDULE_NEXT_WORDS = words("next")
# The composer's commit button once a time is set -- measured: "Planen".
_SCHEDULE_COMMIT_WORDS = words("schedule_commit")
# What makes a dialog the post composer rather than a chat window: it has a
# commit button. Both wordings count, because the button renames itself to
# "Planen" once a time is set.
COMPOSER_WORDS = words("post", "schedule_commit")
# The time is a combobox over a quarter-hour list. Writing into it looks
# like it works -- the field reads the new value -- and the component
# throws it away on confirm, which is how a 15:00 request became 14:45.
# So the time is picked from the list instead of typed.
_EXPAND_TIME_LABELS = words("expand_time")
_DISCARD_LABELS = words("close")
_DRAFT_WORDS = words("draft")
_NEXT_WORDS = words("next")

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
            "message": "page_id is the numeric page id, e.g. 12345678.",
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
    if when.minute % 15:
        return {
            "status": "schedule_off_grid",
            "message": "LinkedIn offers quarter hours only; "
            f"{scheduled_at} is not on the 15-minute grid.",
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
  const mcpVisible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const mcpNorm = v => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const mcpDialogs = () => [...document.querySelectorAll('[role="dialog"], dialog')]
      .filter(mcpVisible);
  // `anchor` must be present, and if `anchor_words` is given the dialog must
  // also carry a visible button with one of those words. Document order only
  // breaks ties between dialogs that both qualify.
  const mcpDialogFor = (anchor, words) => mcpDialogs().find(d => {
    if (anchor && !d.querySelector(anchor)) return false;
    if (!words || !words.length) return true;
    return [...d.querySelectorAll('button, [role="button"]')].filter(mcpVisible)
        .some(b => words.includes(mcpNorm(b.innerText)));
  });

  document.querySelectorAll('[data-ext-company]').forEach(e => e.removeAttribute('data-ext-company'));
  const visible = mcpVisible;
  const norm = mcpNorm;
  const dialog = mcpDialogFor(arg.anchor, arg.anchor_words);
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

# The author/audience button of the page composer, e.g. "Acme Labs Auf Alle posten".
# Read, never clicked: opening the composer from the page's admin view already
# makes the page the author.
_AUTHOR_JS = r"""(arg) => {
  const mcpVisible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const mcpNorm = v => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const mcpDialogs = () => [...document.querySelectorAll('[role="dialog"], dialog')]
      .filter(mcpVisible);
  // `anchor` must be present, and if `anchor_words` is given the dialog must
  // also carry a visible button with one of those words. Document order only
  // breaks ties between dialogs that both qualify.
  const mcpDialogFor = (anchor, words) => mcpDialogs().find(d => {
    if (anchor && !d.querySelector(anchor)) return false;
    if (!words || !words.length) return true;
    return [...d.querySelectorAll('button, [role="button"]')].filter(mcpVisible)
        .some(b => words.includes(mcpNorm(b.innerText)));
  });

  const visible = mcpVisible;
  const dialog = mcpDialogFor('[role="textbox"]', arg.commit_words);
  if (!dialog) return {ok: false, why: 'no_composer_dialog',
                       dialogs: mcpDialogs().length};
  const norm = v => String(v || '').replace(/\s+/g, ' ').trim();
  const buttons = [...dialog.querySelectorAll('button, [role="button"]')].filter(visible);
  const texts = buttons.map(b => norm(b.innerText)).filter(Boolean);
  const want = String(arg.name || '').toLowerCase();
  return {ok: texts.some(t => t.toLowerCase().includes(want)), texts: texts.slice(0, 8)};
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

# The schedule dialog, measured 2026-10-09 (de locale): the date is an
# ``input type="text"`` pre-filled with today as ``9.10.2026`` -- not an ISO
# value and not ``type="date"`` -- and the time an ``input role="combobox"``
# reading ``14:00``. Writing an ISO date there fails the read-back and stops
# the run, which is safe but useless.
#
# So the format is not hard-coded, it is *learned*: the dialog pre-fills today,
# the caller passes today's numbers, and the layout follows from comparing the
# two. A locale that writes ``10/9/2026`` or ``2026-10-09`` is rendered the same
# way without another release.
_SET_SCHEDULE_JS = r"""(arg) => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const dialogs = [...document.querySelectorAll('[role="dialog"], dialog')].filter(visible);
  const label = i => String(i.getAttribute('aria-label') || '').toLowerCase();
  const hasFields = d => [...d.querySelectorAll('input')].filter(visible)
      .some(i => (i.type || '').toLowerCase() === 'date' || /datum|date/.test(label(i)));
  // The dialog that holds the date field, whatever its position. A messaging
  // overlay has inputs too; it has no date field.
  const root = dialogs.find(hasFields);
  if (!root) return {ok: false, why: 'no_schedule_dialog', dialogs: dialogs.length};
  const inputs = [...root.querySelectorAll('input')].filter(visible);
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

  // -- learn the date layout from the pre-filled sample -----------------------
  const sample = String(dateInput.value || '');
  const renderDate = () => {
    if ((dateInput.type || '').toLowerCase() === 'date') return arg.iso;
    const sep = (sample.match(/[^0-9]/) || [])[0];
    if (!sep) return null;
    const parts = sample.split(sep);
    if (parts.length !== 3) return null;
    const nums = parts.map(x => parseInt(x, 10));
    const yearAt = parts.findIndex(x => x.length === 4) >= 0
      ? parts.findIndex(x => x.length === 4)
      : nums.indexOf(arg.today.y);
    if (yearAt < 0) return null;
    const rest = [0, 1, 2].filter(i => i !== yearAt);
    let dayAt, monthAt;
    if (arg.today.d !== arg.today.m) {
      dayAt = rest.find(i => nums[i] === arg.today.d);
      monthAt = rest.find(i => nums[i] === arg.today.m);
      if (dayAt === undefined || monthAt === undefined || dayAt === monthAt) return null;
    } else {
      // Ambiguous sample (same day and month). Fall back to the convention
      // that goes with the separator rather than guessing silently.
      [dayAt, monthAt] = sep === '/' ? [rest[1], rest[0]] : [rest[0], rest[1]];
      if (yearAt === 0) { dayAt = rest[1]; monthAt = rest[0]; }
    }
    const padded = i => parts[i].length === 2 && nums[i] < 10;
    const out = [];
    out[yearAt] = String(arg.target.y);
    out[dayAt] = padded(dayAt) || parts[dayAt].length === 2
      ? String(arg.target.d).padStart(2, '0') : String(arg.target.d);
    out[monthAt] = padded(monthAt) || parts[monthAt].length === 2
      ? String(arg.target.m).padStart(2, '0') : String(arg.target.m);
    return out.join(sep);
  };

  const renderTime = () => {
    const t = String(timeInput.value || '');
    const ampm = t.match(/\s*([ap]\.?m\.?)/i);
    let h = arg.target.hh;
    if (ampm) {
      const suffix = arg.target.hh >= 12 ? ampm[1].replace(/a/i, c => c === 'a' ? 'p' : 'P')
                                         : ampm[1].replace(/p/i, c => c === 'p' ? 'a' : 'A');
      h = arg.target.hh % 12 === 0 ? 12 : arg.target.hh % 12;
      const gap = /\s/.test(t) ? ' ' : '';
      return String(h) + ':' + String(arg.target.mm).padStart(2, '0') + gap + suffix;
    }
    return String(h).padStart(2, '0') + ':' + String(arg.target.mm).padStart(2, '0');
  };

  const wantDate = renderDate();
  const wantTime = renderTime();
  if (!wantDate) {
    return {ok: false, why: 'date_format_unreadable', sample: sample};
  }

  const setter = (el, v) => {
    const d = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value');
    if (d && d.set) d.set.call(el, v); else el.value = v;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  };
  if (dateInput.value !== wantDate) setter(dateInput, wantDate);
  return {ok: dateInput.value === wantDate,
          sample: sample, wrote_date: wantDate, want_time: wantTime,
          date: dateInput.value, time: timeInput.value};
}"""

# Mark the quarter-hour entry of the opened time list.
# Is the post composer gone? Same notion of "composer" as everywhere else:
# an editor *and* a commit button. Asking only for a textbox answers a
# question about the chat window.
_COMPOSER_GONE_JS = r"""(arg) => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const norm = v => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const dialogs = [...document.querySelectorAll('[role="dialog"], dialog')].filter(visible);
  const composer = dialogs.find(d => {
    if (!d.querySelector('[role="textbox"]')) return false;
    return [...d.querySelectorAll('button, [role="button"]')].filter(visible)
        .some(b => arg.commit_words.includes(norm(b.innerText)));
  });
  return !composer;
}"""

_PICK_TIME_JS = r"""(arg) => {
  document.querySelectorAll('[data-ext-time]').forEach(e => e.removeAttribute('data-ext-time'));
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const norm = v => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const want = norm(arg.time);
  const options = [...document.querySelectorAll('[role="option"]')].filter(visible);
  const hits = options.filter(o => norm(o.innerText) === want);
  if (hits.length !== 1) {
    return {count: hits.length,
            seen: options.slice(0, 12).map(o => norm(o.innerText))};
  }
  hits[0].setAttribute('data-ext-time', '1');
  return {count: 1};
}"""

_TIME_VALUE_JS = r"""() => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const label = i => String(i.getAttribute('aria-label') || '').toLowerCase();
  const input = [...document.querySelectorAll('input')].filter(visible)
      .find(i => /zeit|uhrzeit|time/.test(label(i)));
  return input ? input.value : '';
}"""

_DIALOG_TEXT_JS = r"""(arg) => {
  const mcpVisible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const mcpNorm = v => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const mcpDialogs = () => [...document.querySelectorAll('[role="dialog"], dialog')]
      .filter(mcpVisible);
  // `anchor` must be present, and if `anchor_words` is given the dialog must
  // also carry a visible button with one of those words. Document order only
  // breaks ties between dialogs that both qualify.
  const mcpDialogFor = (anchor, words) => mcpDialogs().find(d => {
    if (anchor && !d.querySelector(anchor)) return false;
    if (!words || !words.length) return true;
    return [...d.querySelectorAll('button, [role="button"]')].filter(mcpVisible)
        .some(b => words.includes(mcpNorm(b.innerText)));
  });

  const root = mcpDialogFor('[role="textbox"]', arg.commit_words);
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
        anchor: str = '[role="textbox"]',
        anchor_words: list[str] | None = None,
    ) -> dict[str, Any]:
        return await self._page.evaluate(
            _MARK_JS,
            {
                "words": words or [],
                "labels": labels or [],
                "tag": tag,
                "scope": scope,
                "anchor": anchor,
                "anchor_words": COMPOSER_WORDS if anchor_words is None else anchor_words,
            },
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

    async def resolve_page_id(self, slug: str) -> dict[str, Any]:
        """Numeric page id of a company slug, read on the company page.

        Not measured live (2026-10-09). Exactly one id bound to the slug's
        universalName, or ``company_page_unresolved``; pass the numeric id to
        skip this lookup.
        """
        from linkedin_mcp_server.linkedin.ext_mentions import _COMPANY_ID_JS

        await self._navigator._navigate_to_page(
            f"https://www.linkedin.com/company/{slug}/"
        )
        ids = await self._page.evaluate(_COMPANY_ID_JS, slug)
        if not isinstance(ids, list) or len(ids) != 1:
            return {
                "status": "company_page_unresolved",
                "slug": slug,
                "found": len(ids) if isinstance(ids, list) else None,
                "message": "Pass the numeric page id instead.",
            }
        return {"status": "ok", "page_id": str(ids[0])}

    async def _author_ok(self, page_name: str) -> dict[str, Any]:
        return await self._page.evaluate(
            _AUTHOR_JS, {"name": page_name, "commit_words": COMPOSER_WORDS}
        )

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
        segments: list[Segment] | None = None,
        media: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        segments = segments if segments is not None else [("text", text)]
        text = plain_text(segments)
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

        wanted = mentions_of(segments)
        if wanted:
            # Before the composer: resolving a slug navigates.
            unresolved = await resolve_targets(
                self._page, self._navigator._navigate_to_page, wanted
            )
            if unresolved:
                return {**result, **unresolved}
            result["mentions"] = [m.describe() for m in wanted]

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

        # 4. Schedule -- before the image and before the text. The image
        #    editor leaves a second dialog open, and the schedule dialog
        #    re-renders the composer, which drops text written through
        #    execCommand: measured on 2026-10-09, the composer came back
        #    showing its placeholder. So the text goes in last, right before
        #    the commit, where nothing re-renders after it.
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
                when = datetime.strptime(scheduled_at, "%Y-%m-%d %H:%M")
                today = datetime.now()
                filled = await self._page.evaluate(
                    _SET_SCHEDULE_JS,
                    {
                        "iso": day,
                        "today": {"y": today.year, "m": today.month, "d": today.day},
                        "target": {
                            "y": when.year,
                            "m": when.month,
                            "d": when.day,
                            "hh": when.hour,
                            "mm": when.minute,
                        },
                    },
                )
                if not filled.get("ok"):
                    result["status"] = "schedule_not_filled"
                    result["found"] = filled
                    await self._discard(result)
                    return result
                result["schedule_date"] = filled.get("wrote_date")

                # The time is chosen from the quarter-hour list. Typing into
                # the combobox reads back correctly and is discarded on
                # confirm -- that is how a 15:00 request became 14:45.
                want_time = filled.get("want_time") or clock
                expand = await self._mark(
                    "time-expand", labels=_EXPAND_TIME_LABELS, anchor="input",
                    anchor_words=_SCHEDULE_NEXT_WORDS,
                )
                if expand.get("count") != 1:
                    result["status"] = "time_list_unavailable"
                    result["found"] = expand
                    await self._discard(result)
                    return result
                await self._page.click('[data-ext-company="time-expand"]')
                await self._session.delay(1.5)
                picked = await self._page.evaluate(_PICK_TIME_JS, {"time": want_time})
                if picked.get("count") != 1:
                    result["status"] = "time_not_offered"
                    result["found"] = picked
                    result["message"] = (
                        f"{want_time} is not among the offered quarter hours; "
                        "nothing was scheduled."
                    )
                    await self._discard(result)
                    return result
                await self._page.click("[data-ext-time]")
                await self._session.delay(1.5)
                now_time = await self._page.evaluate(_TIME_VALUE_JS)
                if now_time != want_time:
                    result["status"] = "time_not_taken"
                    result["shown"] = now_time
                    await self._discard(result)
                    return result

                done = await self._mark(
                    "schedule-done",
                    words=_SCHEDULE_NEXT_WORDS,
                    anchor="input",
                    anchor_words=_SCHEDULE_NEXT_WORDS,
                )
                if done.get("count") != 1:
                    result["status"] = "schedule_confirm_unavailable"
                    result["found"] = done
                    await self._discard(result)
                    return result
                await self._page.click('[data-ext-company="schedule-done"]')
                await self._session.delay(2.0)
                summary = await self._page.evaluate(
                    _DIALOG_TEXT_JS, {"commit_words": COMPOSER_WORDS}
                )
                result["composer_summary"] = summary
                if want_time not in summary:
                    result["status"] = "schedule_not_confirmed"
                    result["message"] = (
                        f"The composer does not show {want_time} after the "
                        "schedule dialog; nothing was published."
                    )
                    await self._discard(result)
                    return result
                result["scheduled_at"] = scheduled_at

        # 5a. Images with alt text (ext_media). Tags are refused here: the
        #     page composer's tag suggestions carry no identifier (measured).
        if media:
            attached = await MediaAttacher(
                self._page,
                editor_selector=_EDITOR_IN_DIALOG,
                media_words=_MEDIA_LABELS,
                allow_tags=False,
                kinds=("image", "document"),
            ).attach(media, navigate=self._navigator._navigate_to_page)
            if attached["status"] != "attached":
                result.update(attached)
                await self._discard(result)
                return result
            result["media"] = attached["media"]

        # 5. Image. A dry run never uploads: an attached image cannot be taken
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
                if nxt.get("count") != 1:
                    result["status"] = "image_editor_stuck"
                    result["found"] = nxt
                    result["message"] = (
                        "The image editor offered no single continue button; it "
                        "would stay open over every later step. Nothing published."
                    )
                    await self._discard(result)
                    return result
                await self._page.click('[data-ext-company="media-next"]')
                await self._session.delay(2.0)
                result["image"] = image.name
                result["image_step"] = "uploaded"

        # 5b. Text last: every step above re-renders the composer, and a
        #     re-render drops what execCommand wrote.
        #     The page composer is Quill (measured 2026-10-09): its
        #     suggestions carry no identifier, so a mention is verified on the
        #     inserted a.ql-mention instead -- inside the shared writer.
        wrote = await MentionWriter(self._page, _EDITOR_IN_DIALOG).write(segments)
        if wrote["status"] != "written":
            mention = wrote["status"].startswith("mention")
            result["status"] = wrote["status"] if mention else "text_not_written"
            result["write"] = wrote["status"]
            for key in ("mention", "linked_to", "count", "seen", "shown"):
                if key in wrote:
                    result[key] = wrote[key]
            await self._discard(result)
            return result
        if wrote.get("mentions"):
            result["mentions_written"] = wrote["mentions"]

        # 6. Dry run ends here.
        if not confirm:
            cleared = await self._page.evaluate(_CLEAR_JS, _EDITOR_IN_DIALOG)
            if not cleared:
                logger.warning("create_company_post dry run: editor not cleared")
            await self._discard(result)
            return {**result, "status": "dry_run", "would_post": text}

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

        words = _SCHEDULE_COMMIT_WORDS if mode == "schedule" else _POST_WORDS
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
            _COMPOSER_GONE_JS, {"commit_words": COMPOSER_WORDS}
        )
        if mode == "schedule":
            status = "scheduled" if gone else "schedule_unconfirmed"
        else:
            status = "posted_unverified" if gone else "post_unconfirmed"
        return {**result, "status": status, "posted": mode == "publish" and gone}
