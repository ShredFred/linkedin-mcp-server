"""MiViA fork: compose (and on explicit confirmation publish) a personal post.

Measured 2026-09-29 (de locale): ``/feed/?shareActive=true`` redirects to
``/sharing/compose`` and opens a dialog whose editor carries
``componentkey="ShareBox_textEditor"`` -- a locale-independent anchor. The
publish button has no stable attribute; it is found as the one visible dialog
button whose text is in the per-locale table below (documented exception).

A dry run fills the editor, verifies the text, then clears it again and leaves
the page, so nothing is published and no draft is left behind.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession

logger = logging.getLogger(__name__)

SHARE_URL = "https://www.linkedin.com/feed/?shareActive=true"
RECENT_ACTIVITY_URL = "https://www.linkedin.com/in/me/recent-activity/all/"
_EDITOR = '[componentkey="ShareBox_textEditor"][contenteditable="true"]'
_POST_WORDS = ["posten", "post", "veröffentlichen", "publish"]
_MEDIA_LABELS = ["medieninhalte", "medien", "add media", "media", "foto", "photo"]
_NEXT_WORDS = ["weiter", "next", "fertig", "done"]
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_CLOSE_LABELS = ["schließen", "dismiss", "close", "verwerfen", "discard"]
_DISCARD_WORDS = ["verwerfen", "discard"]

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
  const close = buttons().filter(b => arg.close.includes(norm(b.getAttribute('aria-label'))));
  if (close.length) { close[close.length - 1].click(); steps.push('closed'); }
  const discard = buttons().filter(b => arg.discard.includes(norm(b.innerText))
    && !arg.post.includes(norm(b.innerText)));
  if (discard.length === 1) { discard[0].click(); steps.push('discarded'); }
  return steps.join('+') || 'nothing_to_close';
}"""

_CANON_JS = r"""
  const mcpCanon = value => String(value || '')
      .replace(/[ \t ]*\n[\s ]*/g, '\n').trim();
"""

_WRITE_JS = (
    "(arg) => {"
    + _CANON_JS
    + r"""
  const editor = document.querySelector(arg.selector);
  if (!editor) return 'missing';
  if (mcpCanon(editor.innerText)) return 'occupied';
  editor.focus();
  if (document.activeElement !== editor) return 'unfocused';
  let ok = true;
  arg.text.split('\n').forEach((line, index) => {
    if (index > 0) ok = document.execCommand('insertParagraph', false) === true && ok;
    if (line) ok = document.execCommand('insertText', false, line) === true && ok;
  });
  if (!ok) return 'unsupported';
  return mcpCanon(editor.innerText) === mcpCanon(arg.text) ? 'written' : 'mismatch';
}"""
)

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
  b.setAttribute('data-mivia-target', arg.tag);
  return {count: 1, disabled: b.disabled || b.getAttribute('aria-disabled') === 'true'};
}"""


class MiviaPostComposer:
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
        await self._page.goto(
            "https://www.linkedin.com/feed/", wait_until="domcontentloaded"
        )

    async def _discard(self, result: dict[str, Any]) -> None:
        """Fail-safe cleanup: errors are recorded, never raised."""
        try:
            result["cleanup"] = await self._page.evaluate(
                _DISCARD_JS,
                {
                    "close": _CLOSE_LABELS,
                    "discard": _DISCARD_WORDS,
                    "post": _POST_WORDS,
                },
            )
            await self._page.goto(
                "https://www.linkedin.com/feed/", wait_until="domcontentloaded"
            )
        except Exception as exc:  # noqa: BLE001 - cleanup must not raise
            result["cleanup_error"] = f"{type(exc).__name__}: {exc}"[:200]

    async def create_post(
        self, text: str, *, image_path: str | None, confirm_post: bool
    ) -> dict[str, Any]:
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
            await self._page.goto(
                "https://www.linkedin.com/feed/", wait_until="domcontentloaded"
            )
            return {
                **result,
                "status": "editor_has_media",
                "attached": leftover,
                "message": "The share composer already carries attached media "
                "(a restored draft). Remove it in the browser before posting; "
                "nothing was written.",
            }

        if image is not None:
            media = await self._button("media", [], _MEDIA_LABELS)
            if not media or media.get("count") != 1:
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
                await self._page.click('[data-mivia-target="media"]')
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

        written = await self._page.evaluate(
            _WRITE_JS, {"selector": _EDITOR, "text": text}
        )
        if written != "written":
            await self._leave()
            return {**result, "status": f"text_{written}"}

        post = await self._button("post", _POST_WORDS, [])
        if not post or post.get("count") != 1:
            await self._leave()
            return {**result, "status": "post_button_unavailable", "detail": post}
        if not confirm_post:
            await self._leave()
            return {
                **result,
                "status": "dry_run",
                "message": "Editor filled and verified, post button found"
                + (" (disabled)" if post.get("disabled") else "")
                + "; text cleared again, nothing published. Set confirm_post=true to publish.",
            }
        if post.get("disabled"):
            await self._leave()
            return {**result, "status": "post_button_disabled"}

        # Set directly before the publish click: an exception before it means
        # nothing was published, one after it may have published.
        self.clicked = True
        await self._page.click('[data-mivia-target="post"]')
        try:
            await self._page.wait_for_selector(
                _EDITOR, state="detached", timeout=20_000
            )
        except Exception:
            return {
                **result,
                "status": "post_unconfirmed",
                "retry_safe": False,
                "message": "Clicked post but the composer did not close; check the profile before retrying.",
            }
        # Read back: the newest activity should carry the text.
        await self._session.delay(4.0)
        await self._navigator._navigate_to_page(RECENT_ACTIVITY_URL)
        await self._session.delay(4.0)
        body = await self._page.evaluate(
            "() => (document.querySelector('main')||document.body).innerText"
        )
        from linkedin_mcp_server.mivia_outreach import canonical_text

        first_line = canonical_text(text.split("\n", 1)[0])[:80]
        found = first_line in canonical_text(body or "")
        # The activity id is what a follow-up comment needs; without it the
        # first comment has to be found by hand, which is exactly the step this
        # tool exists to remove.
        activity = await self._page.evaluate(_ACTIVITY_URN_JS, first_line)
        urn = (activity or {}).get("urn")
        # The page text holds older posts too: an earlier post with the same
        # first line made a failed publish read as verified. Only the newest
        # activity carrying the line counts.
        found = found and bool((activity or {}).get("newest"))
        return {
            **result,
            "posted": True,
            "verified": found,
            "retry_safe": False,
            "status": "posted_verified" if found else "posted_unverified",
            "activity_id": urn.rsplit(":", 1)[1] if urn else None,
            "post_url": (
                f"https://www.linkedin.com/feed/update/{urn}/" if urn else None
            ),
            "post_url_matched_text": bool((activity or {}).get("matched")),
        }
