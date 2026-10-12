"""Fork extension: compose (and on explicit confirmation publish) a personal post.

Measured 2026-09-29 (de locale): ``/feed/?shareActive=true`` redirects to
``/sharing/compose`` and opens a dialog whose editor carries
``componentkey="ShareBox_textEditor"`` -- a locale-independent anchor. The
publish button has no stable attribute; it is found as the one visible dialog
button whose text is in the per-locale table below (documented exception).

A dry run fills the editor, verifies the text, then clears it again and leaves
the page, so nothing is published and no draft is left behind.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from linkedin_mcp_server.linkedin import ext_post_outcome as outcome
from linkedin_mcp_server.linkedin.ext_composer_labels import words
from linkedin_mcp_server.linkedin.ext_mentions import (
    MentionWriter,
    Segment,
    mentions_of,
    plain_text,
    resolve_targets,
)
from linkedin_mcp_server.linkedin.ext_media import MediaAttacher
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession

logger = logging.getLogger(__name__)

SHARE_URL = "https://www.linkedin.com/feed/?shareActive=true"
# Every navigation goes through PageNavigator._navigate_to_page: rate-limit
# gate, 429 detection and the LinkedIn host assertion apply to cleanup too.
FEED_URL = "https://www.linkedin.com/feed/"
RECENT_ACTIVITY_URL = "https://www.linkedin.com/in/me/recent-activity/all/"
_EDITOR = '[componentkey="ShareBox_textEditor"][contenteditable="true"]'
# Labels come from the one bilingual table (ext_composer_labels).
_POST_WORDS = words("post")
_MEDIA_LABELS = words("media")
_NEXT_WORDS = words("next")
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_CLOSE_LABELS = words("close")
_DISCARD_WORDS = words("discard")

# Abandon the composer after an image step failed: close the visible dialog,
# then confirm the discard prompt if LinkedIn shows one. Only buttons whose
# aria-label / text is in the close or discard lists are ever clicked, never a
# post button, so a failing cleanup can not publish anything.
_DISCARD_JS = r"""(arg) => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const norm = v => String(v || '').trim().toLowerCase();
  const buttons = () => [...document.querySelectorAll('[role="dialog"] button, dialog button')]
    .filter(visible);
  const steps = [];
  if (!arg.discard_only) {
    const close = buttons().filter(b => arg.close.includes(norm(b.getAttribute('aria-label'))));
    if (close.length) { close[close.length - 1].click(); steps.push('closed'); }
  }
  const discard = buttons().filter(b => arg.discard.includes(norm(b.innerText))
    && !arg.post.includes(norm(b.innerText)));
  if (discard.length === 1) { discard[0].click(); steps.push('discarded'); }
  return steps.join('+') || 'nothing_to_close';
}"""

_CLEAR_JS = r"""(selector) => {
  const editor = document.querySelector(selector);
  if (!editor) return false;
  editor.focus();
  document.execCommand('selectAll', false);
  document.execCommand('delete', false);
  return !(editor.innerText || '').trim();
}"""

# A dry run clears the text but cannot take an attached image out again, and
# LinkedIn restores a share draft when the composer is opened next. Without this
# check the productive run would silently publish two images (or a stale one).
_MEDIA_PRESENT_JS = r"""(selector) => {
  const dialog = document.querySelector(selector)?.closest('[role="dialog"], dialog');
  if (!dialog) return 0;
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  return [...dialog.querySelectorAll('img, video')]
      .filter(el => visible(el) && (el.width > 80 || el.videoWidth > 80)).length;
}"""

# The newest own activity on /in/me/recent-activity/all/. Preferred match: the
# card that carries the first line of the text just posted; otherwise the first
# activity urn on the page, flagged as such.
_ACTIVITY_URN_JS = r"""(firstLine) => {
  const want = String(firstLine || '').toLowerCase();
  const cards = [...document.querySelectorAll('[data-urn], [data-id], [data-activity-urn]')]
    .map(el => ({
      el,
      urn: el.getAttribute('data-urn') || el.getAttribute('data-id')
           || el.getAttribute('data-activity-urn') || '',
    }))
    .filter(c => /^urn:li:activity:\d+$/.test(c.urn));
  if (!cards.length) return null;
  const hit = want && cards.find(c => (c.el.innerText || '').toLowerCase().includes(want));
  return {urn: (hit || cards[0]).urn, matched: !!hit, newest: !!hit && hit === cards[0]};
}"""

_FIND_BUTTON_JS = r"""(arg) => {
  const dialog = document.querySelector(arg.selector)?.closest('[role="dialog"], dialog');
  if (!dialog) return null;
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const matches = [...dialog.querySelectorAll('button')].filter(b => visible(b) && (
    arg.words.includes((b.innerText || '').trim().toLowerCase()) ||
    arg.labels.includes((b.getAttribute('aria-label') || '').trim().toLowerCase())
  ));
  if (matches.length !== 1) return {count: matches.length};
  const b = matches[0];
  b.setAttribute('data-ext-target', arg.tag);
  return {count: 1, disabled: b.disabled || b.getAttribute('aria-disabled') === 'true'};
}"""


class ExtPostComposer:
    def __init__(self, session: PageSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator
        self.clicked = False

    @property
    def _page(self) -> Any:
        return self._session.page

    async def _button(
        self, tag: str, words: list[str], labels: list[str]
    ) -> dict[str, Any] | None:
        return await self._page.evaluate(
            _FIND_BUTTON_JS,
            {"selector": _EDITOR, "words": words, "labels": labels, "tag": tag},
        )

    async def _leave(self) -> None:
        cleared = await self._page.evaluate(_CLEAR_JS, _EDITOR)
        if not cleared:
            logger.warning("create_post dry run: editor could not be cleared")
        await self._navigator._navigate_to_page(FEED_URL)

    async def _discard(self, result: dict[str, Any]) -> None:
        """Fail-safe cleanup: errors are recorded, never raised."""
        try:
            arg = {
                "close": _CLOSE_LABELS,
                "discard": _DISCARD_WORDS,
                "post": _POST_WORDS,
                "discard_only": False,
            }
            steps = await self._page.evaluate(_DISCARD_JS, arg)
            # The discard prompt only mounts after the close click; a second
            # pass that clicks nothing but the discard button confirms it.
            await asyncio.sleep(1.0)
            second = await self._page.evaluate(
                _DISCARD_JS, {**arg, "discard_only": True}
            )
            if second != "nothing_to_close":
                steps = f"{steps}+{second}"
            result["cleanup"] = steps
            await self._navigator._navigate_to_page(FEED_URL)
        except Exception as exc:  # noqa: BLE001 - cleanup must not raise
            result["cleanup_error"] = f"{type(exc).__name__}: {exc}"[:200]

    async def create_post(
        self,
        text: str,
        *,
        image_path: str | None,
        confirm_post: bool,
        segments: list[Segment] | None = None,
        media: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        segments = segments if segments is not None else [("text", text)]
        text = plain_text(segments)
        result: dict[str, Any] = {
            "url": SHARE_URL,
            "posted": False,
            "confirm_post": confirm_post,
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

        # Mention targets are resolved before the composer opens: resolving a
        # slug navigates, and a navigation would close the composer.
        wanted = mentions_of(segments) + [t for m in media or [] for t in m["tags"]]
        if wanted:
            unresolved = await resolve_targets(
                self._page, self._navigator._navigate_to_page, wanted
            )
            if unresolved:
                return {**result, **unresolved}
            result["mentions"] = [m.describe() for m in mentions_of(segments)]

        # Duplicate check against the live profile, not only the ledger: a
        # post made by hand or by an earlier unconfirmed run is visible here.
        # Only before a real publish: a dry run must not cost an extra
        # navigation (rate limit), and its navigation contract stays SHARE+FEED.
        live = await self.find_live(text) if confirm_post else {"matches": []}
        dup = outcome.recent_duplicate(live["matches"])
        if dup and confirm_post:
            return {
                **result,
                "status": "duplicate_live",
                "outcome": "not_posted",
                "retry_safe": False,
                "post_url": dup["post_url"],
                "age_seconds": dup["age_seconds"],
                "message": "An identical post is already live within the last 24 h "
                "(or its age is unreadable); nothing was written.",
            }
        if dup:
            result["live_duplicate"] = dup

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
                "(a restored draft). Remove it in the browser before posting; "
                "nothing was written.",
            }

        if media:
            # Images with alt text and tags (ext_media). A dry run attaches
            # too: discarding the composer was measured to leave no draft,
            # and only the real dialog can confirm alt text and tags.
            try:
                attached = await MediaAttacher(
                    self._page,
                    editor_selector=_EDITOR,
                    media_words=_MEDIA_LABELS,
                    allow_tags=True,
                    kinds=("image", "video"),
                ).attach(media, navigate=self._navigator._navigate_to_page)
            except BaseException:
                # A half-attached composer must not stay open (draft risk).
                await self._discard(result)
                raise
            if attached["status"] != "attached":
                result.update(attached)
                await self._discard(result)
                return result
            result["media"] = attached["media"]
            try:
                await self._page.wait_for_selector(_EDITOR, timeout=15_000)
            except Exception:
                result["status"] = "composer_lost_after_image"
                await self._discard(result)
                return result

        if image is not None:
            media_button = await self._button("media", [], _MEDIA_LABELS)
            if not media_button or media_button.get("count") != 1:
                result["status"] = "media_button_unavailable"
                await self._discard(result)
                return result
        if image is not None and not confirm_post:
            # Dry run: an uploaded image can not be removed again and LinkedIn
            # keeps it as a draft, so the dry run checks file and media button
            # only and never uploads.
            result["image"] = image.name
            result["image_step"] = "not_uploaded_dry_run"
        elif image is not None:
            async with self._page.expect_file_chooser(timeout=10_000) as chooser_info:
                await self._page.click('[data-ext-target="media"]')
            chooser = await chooser_info.value
            await chooser.set_files(str(image))
            await self._session.delay(5.0)
            nxt = await self._page.evaluate(
                # The image editor is its own dialog next to the share dialog,
                # and hidden dialogs linger in the DOM: search visible buttons
                # in every dialog, not the last dialog.
                r"""(words) => { const b = [...document.querySelectorAll('[role="dialog"] button, dialog button')]
                  .filter(x => x.offsetWidth && words.includes((x.innerText || '').trim().toLowerCase()));
                  if (b.length !== 1) return b.length; b[0].click(); return 1; }""",
                _NEXT_WORDS,
            )
            result["image_step"] = "next_clicked" if nxt == 1 else f"next_buttons={nxt}"
            try:
                await self._page.wait_for_selector(_EDITOR, timeout=15_000)
            except Exception:
                result["status"] = "composer_lost_after_image"
                await self._discard(result)
                return result
            result["image"] = image.name

        # The one write routine for every composer: text by execCommand,
        # mentions by typeahead with an identifier check (ext_mentions).
        wrote = await MentionWriter(self._page, _EDITOR).write(segments)
        if wrote["status"] != "written":
            await self._leave()
            return {**result, **wrote}
        if wrote.get("mentions"):
            result["mentions_written"] = wrote["mentions"]

        post = await self._button("post", _POST_WORDS, [])
        if not post or post.get("count") != 1:
            await self._leave()
            return {**result, "status": "post_button_unavailable", "detail": post}
        if not confirm_post:
            if media:
                await self._page.evaluate(_CLEAR_JS, _EDITOR)
                await self._discard(result)
                return {
                    **result,
                    "status": "dry_run",
                    "would_post": text,
                    "message": "Composer filled with text and images, alt text and "
                    "tags read back; discarded, nothing published.",
                }
            await self._leave()
            return {
                **result,
                "status": "dry_run",
                "would_post": text,
                "message": "Editor filled and verified, post button found"
                + (" (disabled)" if post.get("disabled") else "")
                + "; text cleared again, nothing published. Set confirm_post=true to publish.",
            }
        if post.get("disabled"):
            await self._leave()
            return {**result, "status": "post_button_disabled"}

        # An image still uploading would be posted without it, or the click
        # is swallowed: wait for the progress indicator to go, else stop.
        if not await outcome.wait_for_upload(self._page, _EDITOR, self._session.delay):
            await self._discard(result)
            return {
                **result,
                "status": "media_upload_pending",
                "outcome": "not_posted",
                "retry_safe": True,
                "message": "An upload was still in progress after 60 s; composer discarded, nothing published.",
            }

        # Set directly before the publish click: an exception before it means
        # nothing was published, one after it may have published.
        self.clicked = True
        await self._page.click('[data-ext-target="post"]')
        decided = await outcome.decide(
            page=self._page,
            editor=_EDITOR,
            sleep=self._session.delay,
            navigate=self._navigator._navigate_to_page,
            discard=self._discard,
            post_words=_POST_WORDS,
            text=text,
            activity_url=RECENT_ACTIVITY_URL,
        )
        return {**result, **decided}

    async def find_live(self, text: str, *, url: str = RECENT_ACTIVITY_URL) -> dict[str, Any]:
        """Read-only: is this text in recent activity? (one read, no retries)."""
        rb = await outcome.read_activity(
            self._page,
            self._navigator._navigate_to_page,
            self._session.delay,
            text,
            url,
            waits=(0.0,),
            want_fresh=False,
        )
        return rb

    async def verify_post(
        self, text: str, *, url: str = RECENT_ACTIVITY_URL, wait: bool = False
    ) -> dict[str, Any]:
        """Read-only lookup of an own post by text; ``wait`` uses the ~60 s backoff."""
        rb = await outcome.read_activity(
            self._page,
            self._navigator._navigate_to_page,
            self._session.delay,
            text,
            url,
            waits=outcome.READBACK_WAITS if wait else (0.0,),
            want_fresh=False,
        )
        hit = rb["matches"][0] if rb["matches"] else None
        return {
            "status": "found" if hit else "not_found",
            "found": bool(hit),
            "post_url": (hit or {}).get("post_url"),
            "activity_id": (hit.get("urn") or "").rsplit(":", 1)[-1] or None if hit else None,
            "age_seconds": (hit or {}).get("age_seconds"),
            "matches": rb["matches"],
            "attempts": rb["attempts"],
            "activity_url": url,
            "text_prefix": outcome.prefix(text),
        }
