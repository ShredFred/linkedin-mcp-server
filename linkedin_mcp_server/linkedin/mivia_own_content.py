"""MiViA fork: take back an own post or comment -- delete or edit (2026-10-01).

The way back for what ``create_post`` and ``comment_on_post`` published.

**Not measured against the live page.** Written without a single click on a
real post: the anchors below come from the measured post page
(``[componentkey^="update-card-focus"]``, comment cards
``[componentkey^="replaceableComment_urn:li:comment:(activity:A,C)"]``) and
from LinkedIn's long-standing UI wording ("Beitrag löschen", "Bearbeiten",
"Änderungen speichern"). Everything else is a defensive chain: visible
elements only, exactly one hit or a status, and every menu entry is found by
its exact text inside the menu that was just opened.

Order of checks, each one a status instead of a click into nothing:

1. The signed-in member's slug from the ``/in/me/`` redirect.
2. The target card (post or comment) on the post page, its author link --
   author != signed-in member stops here, before any click.
3. The card's own options button; then the menu entry by exact text.
4. Dry run stops here: menu read, entry present, menu closed.
5. Delete: confirmation dialog, its one "Löschen" button. Edit: editor,
   prefill must equal the text read from the card, replace, compare, then the
   one save button.
6. ``clicked`` is set right before the final click; the result is confirmed by
   reloading the page (deleted = card gone; edited = text equal).
"""

from __future__ import annotations

import logging
import random
import re
from typing import Any
from urllib.parse import unquote

from linkedin_mcp_server.linkedin.mivia_actions import MiviaActions
from linkedin_mcp_server.linkedin.mivia_engagement import (
    parse_person_ref,
    split_comment_lines,
)
from linkedin_mcp_server.linkedin.mivia_inmail import canon, strip_edit_marker

logger = logging.getLogger(__name__)

ME_URL = "https://www.linkedin.com/in/me/"
_ATTR = "data-mivia-own"

_COMMENT_URN_RE = re.compile(
    r"comment:\(\s*(?:urn:li:)?(?:activity|ugcPost|share):(\d{16,22})\s*,\s*(\d{10,22})\s*\)"
)
_TRUNCATED_RE = re.compile(
    r"\s*(?:…|\.\.\.)\s*(?:mehr|more|see more|mehr anzeigen)?\s*$", re.IGNORECASE
)
_GONE_RE = re.compile(
    r"nicht mehr verfügbar|nicht verfügbar|wurde gelöscht|existiert nicht"
    r"|no longer available|cannot be displayed|can't be displayed|been deleted"
    r"|doesn.t exist|isn.t available",
    re.IGNORECASE,
)

# Exact menu wording (lower case). The short forms are accepted because the
# menu is the one we just opened on our own card; "Bearbeiten" on someone
# else's card never gets this far (author check).
DELETE_POST_WORDS = ["beitrag löschen", "delete post", "löschen", "delete"]
EDIT_POST_WORDS = ["beitrag bearbeiten", "edit post", "bearbeiten", "edit"]
DELETE_COMMENT_WORDS = ["kommentar löschen", "delete comment", "löschen", "delete"]
EDIT_COMMENT_WORDS = ["kommentar bearbeiten", "edit comment", "bearbeiten", "edit"]
CONFIRM_DELETE_WORDS = [
    "löschen",
    "delete",
    "beitrag löschen",
    "delete post",
    "kommentar löschen",
    "delete comment",
]
SAVE_WORDS = ["speichern", "save", "änderungen speichern", "save changes"]
CANCEL_WORDS = ["abbrechen", "cancel"]
DISCARD_WORDS = ["verwerfen", "discard", "änderungen verwerfen"]


def parse_comment_ref(comment: str) -> tuple[str | None, str]:
    """(activity id or None, comment id) from an id, URN or URL; ValueError else."""
    raw = unquote(unquote((comment or "").strip()))
    if re.fullmatch(r"\d{10,22}", raw):
        return None, raw
    match = _COMMENT_URN_RE.search(raw)
    if not match:
        raise ValueError(
            "comment_id must be the numeric comment id or a urn:li:comment:(activity:A,C)"
        )
    return match.group(1), match.group(2)


def slug_of(href: str | None) -> str | None:
    """Lower-cased, percent-decoded member slug of a profile link; None otherwise."""
    ref = parse_person_ref(href or "")
    if ref.get("kind") != "member":
        return None
    return unquote(str(ref["id"])).strip().lower() or None


def same_member(href: str | None, own_slug: str | None) -> bool:
    """True only when the link points at exactly the signed-in member."""
    got = slug_of(href)
    return bool(got and own_slug and got == own_slug.strip().lower())


def text_matches(read: str | None, expected: str) -> bool:
    """Exact comparison after whitespace canon and LinkedIn's edit marker."""
    if read is None:
        return False
    return canon(strip_edit_marker(read)[0]) == canon(expected)


def prefill_matches(prefill: str, shown: str | None) -> bool:
    """The editor holds the text the card shows.

    The card may truncate ("… mehr"); then the editor must start with the
    shown part. Without a shown text there is nothing to compare: refused.
    """
    if not shown:
        return False
    full = canon(strip_edit_marker(prefill)[0])
    seen = canon(strip_edit_marker(shown)[0])
    cut = canon(_TRUNCATED_RE.sub("", seen))
    if cut != seen:
        return bool(cut) and full.startswith(cut)
    return full == seen


_VISIBLE_JS = r"""
  const vis = el => !!(el && el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden');
  const inComment = el => el.closest('[componentkey^="replaceableComment_urn:li:comment:"]');
  const clearTag = tag => document.querySelectorAll('[data-mivia-own="' + tag + '"]')
      .forEach(e => e.removeAttribute('data-mivia-own'));
"""

# The post card: by its exact activity urn first, then the measured
# update-card-focus wrapper. Author = first person/company link outside any
# comment card. The options trigger is a visible button outside comment cards
# whose aria-label names the control menu.
_POST_CARD_JS = (
    "(activityId) => {"
    + _VISIBLE_JS
    + r"""
  clearTag('post-menu');
  const urn = 'urn:li:activity:' + activityId;
  // The focus wrapper carries no urn, so it is only trusted when it is the
  // single card on the page: a second one may be another own post, which
  // would pass the author check and get edited or deleted instead.
  const focus = document.querySelectorAll('[componentkey^="update-card-focus"]');
  const card = document.querySelector('[data-urn="' + urn + '"], [data-id="' + urn + '"], [data-activity-urn="' + urn + '"]')
            || (focus.length === 1 ? focus[0] : null);
  if (!card) return null;
  const textEl = [...card.querySelectorAll(
      '[data-testid="expandable-text-box"], .update-components-text, .feed-shared-inline-show-more-text, .feed-shared-update-v2__description')]
      .find(e => !inComment(e));
  // Every person/company link above the post text: a social-context header
  // ("X hat das kommentiert") or a bare repost puts a second name there.
  const before = a => !textEl || !!(a.compareDocumentPosition(textEl) & Node.DOCUMENT_POSITION_FOLLOWING);
  const actors = [...card.querySelectorAll('a[href*="/in/"], a[href*="/company/"]')]
      .filter(a => !inComment(a) && !(textEl && textEl.contains(a)) && before(a));
  const actor = actors[0];
  const label = /steuerungsmen|kontrollmen|control menu|weitere aktionen|more actions|optionen f(ü|u)r|options for/i;
  const menus = [...card.querySelectorAll('button')]
      .filter(b => vis(b) && !inComment(b) && label.test(b.getAttribute('aria-label') || ''));
  if (menus.length === 1) menus[0].setAttribute('data-mivia-own', 'post-menu');
  return {actor_href: actor ? actor.getAttribute('href') : null,
          actor_hrefs: actors.map(a => a.getAttribute('href')),
          text: textEl ? (textEl.innerText || '').trim() : null,
          menu_count: menus.length};
}"""
)

# One comment card by its comment id. Author = first link whose nearest
# comment card is this card (a reply's author is not ours to test). Text lines
# without nested replies.
_COMMENT_CARD_JS = (
    "(commentId) => {"
    + _VISIBLE_JS
    + r"""
  clearTag('comment-menu');
  const cards = [...document.querySelectorAll('[componentkey^="replaceableComment_urn:li:comment:"]')]
      .filter(c => (c.getAttribute('componentkey') || '').endsWith(',' + commentId + ')'));
  if (cards.length !== 1) return {count: cards.length,
      total: document.querySelectorAll('[componentkey^="replaceableComment_urn:li:comment:"]').length};
  const card = cards[0];
  const own = el => inComment(el) === card;
  const actor = [...card.querySelectorAll('a[href*="/in/"], a[href*="/company/"]')].find(own);
  let text = card.innerText || '';
  card.querySelectorAll('[componentkey^="replaceableComment_urn:li:comment:"]').forEach(r => {
    const t = r.innerText || '';
    if (t) text = text.replace(t, '');
  });
  const label = /optionen|options|weitere|more/i;
  const menus = [...card.querySelectorAll('button')]
      .filter(b => vis(b) && own(b) && label.test(b.getAttribute('aria-label') || ''));
  if (menus.length) menus[0].setAttribute('data-mivia-own', 'comment-menu');
  return {count: 1, actor_href: actor ? actor.getAttribute('href') : null,
          lines: text.split('\n').map(s => s.trim()).filter(Boolean),
          menu_count: menus.length};
}"""
)

# Entries of the open (visible) menu; the one whose text is in arg.words is
# tagged. A container holding a matching child is dropped, so a <li> around a
# role=button counts once.
_MENU_PICK_JS = (
    "(arg) => {"
    + _VISIBLE_JS
    + r"""
  clearTag(arg.tag);
  const sel = '[role="menu"] [role="menuitem"], [role="menuitem"], .artdeco-dropdown__content [role="button"], .artdeco-dropdown__content li, [role="menu"] li';
  const items = [...document.querySelectorAll(sel)].filter(vis);
  const first = el => ((el.innerText || '').trim().split('\n')[0] || '').trim();
  const texts = [...new Set(items.map(first).filter(Boolean))];
  let hits = items.filter(el => arg.words.includes(first(el).toLowerCase()));
  hits = hits.filter(h => !hits.some(o => o !== h && h.contains(o)));
  // Prefer the most specific wording ("Beitrag löschen" over "Löschen").
  for (const w of arg.words) {
    const exact = hits.filter(h => first(h).toLowerCase() === w);
    if (exact.length) { hits = exact; break; }
  }
  if (hits.length === 1) hits[0].setAttribute('data-mivia-own', arg.tag);
  return {items: texts, count: hits.length};
}"""
)

# A button by exact text inside the topmost visible dialog (scope "dialog") or
# inside a tagged element (scope = tag name).
_BUTTON_PICK_JS = (
    "(arg) => {"
    + _VISIBLE_JS
    + r"""
  clearTag(arg.tag);
  let scope;
  if (arg.scope === 'dialog') {
    const dialogs = [...document.querySelectorAll('[role="alertdialog"], [role="dialog"], dialog[open]')].filter(vis);
    scope = dialogs[dialogs.length - 1];
  } else {
    scope = document.querySelector('[data-mivia-own="' + arg.scope + '"]');
  }
  if (!scope) return {count: 0, scope: false};
  const hits = [...scope.querySelectorAll('button, [role="button"]')].filter(b => vis(b) && (
      arg.words.includes((b.innerText || '').trim().toLowerCase()) ||
      arg.words.includes((b.getAttribute('aria-label') || '').trim().toLowerCase())));
  if (hits.length === 1) hits[0].setAttribute('data-mivia-own', arg.tag);
  const b = hits[0];
  return {count: hits.length, scope: true,
          disabled: !!b && (b.disabled || b.getAttribute('aria-disabled') === 'true')};
}"""
)

# The editor after "Bearbeiten": for a post the share editor inside the
# topmost visible dialog, for a comment the visible editor inside its card.
_EDITOR_PICK_JS = (
    "(arg) => {"
    + _VISIBLE_JS
    + r"""
  clearTag(arg.tag);
  let scope;
  if (arg.comment_id) {
    scope = [...document.querySelectorAll('[componentkey^="replaceableComment_urn:li:comment:"]')]
        .find(c => (c.getAttribute('componentkey') || '').endsWith(',' + arg.comment_id + ')'));
  } else {
    const dialogs = [...document.querySelectorAll('[role="dialog"], dialog[open]')].filter(vis);
    scope = dialogs[dialogs.length - 1];
  }
  if (!scope) return {count: 0};
  scope.setAttribute('data-mivia-own', arg.tag + '-scope');
  const hits = [...scope.querySelectorAll('[contenteditable="true"], [role="textbox"][contenteditable]')]
      .filter(e => vis(e) && (!arg.comment_id || inComment(e) === scope))
      .filter((e, _, all) => !all.some(o => o !== e && o.contains(e)));
  if (hits.length === 1) hits[0].setAttribute('data-mivia-own', arg.tag);
  return {count: hits.length, text: hits.length === 1 ? (hits[0].innerText || '') : null};
}"""
)

_REPLACE_EDITOR_JS = r"""(el, text) => {
  el.focus();
  const sel = window.getSelection();
  const range = document.createRange();
  range.selectNodeContents(el);
  sel.removeAllRanges(); sel.addRange(range);
  document.execCommand('delete', false);
  let ok = true;
  text.split('\n').forEach((line, i) => {
    if (i) ok = document.execCommand('insertParagraph', false) && ok;
    if (line) ok = document.execCommand('insertText', false, line) && ok;
  });
  return ok;
}"""

_PAGE_TEXT_JS = r"""() => (document.querySelector('main') || document.body).innerText.slice(0, 4000)"""


class MiviaOwnContent(MiviaActions):
    """Delete or edit an own post or comment. Every write sits behind ``confirm``."""

    clicked = False

    async def _wait(self, low: float, high: float) -> None:
        await self._session.delay(random.uniform(low, high))

    def _tagged(self, tag: str) -> Any:
        return self._page.locator(f'[{_ATTR}="{tag}"]').first

    async def _escape(self) -> None:
        try:
            await self._page.keyboard.press("Escape")
        except Exception:
            logger.debug("Escape failed", exc_info=True)

    async def own_slug(self) -> str | None:
        """Slug of the signed-in member from the ``/in/me/`` redirect."""
        await self._goto(ME_URL)
        match = re.search(r"/in/([^/?#]+)", self._page.url or "")
        slug = unquote(match.group(1)).strip().lower() if match else None
        return None if slug in (None, "", "me") else slug

    async def _post_page(self, activity_id: str) -> str:
        url = f"https://www.linkedin.com/feed/update/urn:li:activity:{activity_id}/"
        await self._goto(url)
        return url

    async def _pick(self, js: str, arg: dict[str, Any]) -> dict[str, Any]:
        return await self._page.evaluate(js, arg) or {"count": 0}

    # -- shared steps ------------------------------------------------------------

    async def _locate(self, activity_id: str, comment_id: str | None) -> dict[str, Any]:
        """Owner check and card read; ``{"status": "ok", ...}`` or a refusal."""
        own = await self.own_slug()
        if not own:
            return {"status": "own_identity_unknown"}
        await self._post_page(activity_id)
        post = await self._page.evaluate(_POST_CARD_JS, activity_id)
        if not post:
            return {"status": "post_not_found"}
        if comment_id is None:
            if not post.get("actor_href"):
                return {"status": "author_unknown"}
            authors = {slug_of(h) or h for h in (post.get("actor_hrefs") or []) if h}
            if len(authors) > 1:
                return {"status": "author_ambiguous", "authors": sorted(authors)}
            if not same_member(post["actor_href"], own):
                return {
                    "status": "not_own_post",
                    "author": slug_of(post["actor_href"]) or post["actor_href"],
                }
            if post.get("menu_count") != 1:
                return {
                    "status": "menu_unavailable",
                    "menu_count": post.get("menu_count"),
                }
            return {
                "status": "ok",
                "own": own,
                "text": post.get("text"),
                "menu": "post-menu",
            }
        card = await self._page.evaluate(_COMMENT_CARD_JS, comment_id)
        if not card or card.get("count") != 1:
            return {
                "status": "comment_not_found"
                if not card or not card.get("count")
                else "comment_ambiguous",
                "count": (card or {}).get("count", 0),
            }
        if not card.get("actor_href"):
            return {"status": "author_unknown"}
        if not same_member(card["actor_href"], own):
            return {
                "status": "not_own_comment",
                "author": slug_of(card["actor_href"]) or card["actor_href"],
            }
        if not card.get("menu_count"):
            return {"status": "menu_unavailable", "menu_count": 0}
        text = split_comment_lines(card.get("lines") or []).get("text")
        return {"status": "ok", "own": own, "text": text, "menu": "comment-menu"}

    async def _open_menu_entry(self, menu_tag: str, words: list[str]) -> dict[str, Any]:
        """Open the card's menu and tag the entry; menu stays open on success."""
        trigger = self._tagged(menu_tag)
        if await trigger.count() == 0:
            return {"status": "menu_unavailable"}
        await trigger.scroll_into_view_if_needed()
        await trigger.click()
        await self._wait(0.8, 1.4)
        picked = await self._pick(_MENU_PICK_JS, {"words": words, "tag": "entry"})
        if picked.get("count") != 1:
            await self._escape()
            return {
                "status": "menu_item_missing"
                if not picked.get("count")
                else "menu_item_ambiguous",
                "menu": picked.get("items", []),
            }
        return {"status": "ok", "menu": picked.get("items", [])}

    # -- delete ------------------------------------------------------------------

    async def delete(
        self, activity_id: str, comment_id: str | None, *, confirm: bool
    ) -> dict[str, Any]:
        """Delete an own post (comment_id None) or an own comment."""
        self.clicked = False
        what = "comment" if comment_id else "post"
        base: dict[str, Any] = {"target": what, "done": False}
        found = await self._locate(activity_id, comment_id)
        if found["status"] != "ok":
            return {**base, **found}
        words = DELETE_COMMENT_WORDS if comment_id else DELETE_POST_WORDS
        entry = await self._open_menu_entry(found["menu"], words)
        if entry["status"] != "ok":
            return {**base, **entry}
        base["old_text"] = found.get("text")
        if not confirm:
            await self._escape()
            return {**base, "status": "dry_run", "menu": entry["menu"]}
        await self._tagged("entry").click()
        await self._wait(1.0, 1.8)
        button = await self._pick(
            _BUTTON_PICK_JS,
            {"scope": "dialog", "words": CONFIRM_DELETE_WORDS, "tag": "confirm"},
        )
        if button.get("count") != 1 or button.get("disabled"):
            await self._cancel_dialog()
            return {
                **base,
                "status": "confirm_dialog_missing",
                "buttons": button.get("count"),
            }
        self.clicked = True
        await self._tagged("confirm").click()
        base["done"] = True
        try:
            await self._wait(3.0, 5.0)
            gone = await self._is_gone(activity_id, comment_id)
        except Exception as exc:
            logger.warning("delete read-back failed", exc_info=True)
            return {
                **base,
                "status": "unverified",
                "verified": False,
                "verify_error": f"{type(exc).__name__}: {exc}",
            }
        return {
            **base,
            "status": "verified" if gone else "unverified",
            "verified": gone,
        }

    async def _is_gone(self, activity_id: str, comment_id: str | None) -> bool:
        url = await self._post_page(activity_id)
        post = await self._page.evaluate(_POST_CARD_JS, activity_id)
        if comment_id:
            # The post must have loaded, or "no card" proves nothing.
            if not post:
                return False
            card = await self._page.evaluate(_COMMENT_CARD_JS, comment_id)
            # Zero cards in total means the comment list did not render
            # (collapsed, lazy): absence proves nothing then.
            return (
                bool(card)
                and card.get("count") == 0
                and int(card.get("total") or 0) > 0
            )
        if post:
            return False
        text = await self._page.evaluate(_PAGE_TEXT_JS)
        # Only a redirect to the feed itself counts; a login wall or checkpoint
        # also "moves" the page and proves nothing about the post.
        moved = re.fullmatch(
            r"https://www\.linkedin\.com/feed/?(?:\?.*)?", self._page.url or url
        )
        return bool(_GONE_RE.search(text or "")) or bool(moved)

    async def _cancel_dialog(self) -> None:
        try:
            picked = await self._pick(
                _BUTTON_PICK_JS,
                {"scope": "dialog", "words": CANCEL_WORDS, "tag": "cancel"},
            )
            if picked.get("count") == 1:
                await self._tagged("cancel").click()
            else:
                await self._escape()
            await self._wait(0.6, 1.0)
            discard = await self._pick(
                _BUTTON_PICK_JS,
                {"scope": "dialog", "words": DISCARD_WORDS, "tag": "discard"},
            )
            if discard.get("count") == 1:
                await self._tagged("discard").click()
        except Exception:
            logger.warning("cancelling the dialog failed", exc_info=True)

    # -- edit --------------------------------------------------------------------

    async def edit(
        self,
        activity_id: str,
        comment_id: str | None,
        new_text: str,
        *,
        confirm: bool,
    ) -> dict[str, Any]:
        """Edit an own post (comment_id None) or an own comment."""
        self.clicked = False
        what = "comment" if comment_id else "post"
        base: dict[str, Any] = {"target": what, "done": False}
        found = await self._locate(activity_id, comment_id)
        if found["status"] != "ok":
            return {**base, **found}
        old = found.get("text")
        base["old_text"] = old
        if old is not None and text_matches(old, new_text):
            return {**base, "status": "unchanged"}
        words = EDIT_COMMENT_WORDS if comment_id else EDIT_POST_WORDS
        entry = await self._open_menu_entry(found["menu"], words)
        if entry["status"] != "ok":
            return {**base, **entry}
        if not confirm:
            await self._escape()
            return {**base, "status": "dry_run", "menu": entry["menu"]}
        await self._tagged("entry").click()
        await self._wait(1.0, 2.0)
        picked = await self._pick(
            _EDITOR_PICK_JS, {"comment_id": comment_id, "tag": "editor"}
        )
        scope = "editor-scope"
        if picked.get("count") != 1:
            await self._abort_edit(scope)
            return {**base, "status": "editor_missing", "editors": picked.get("count")}
        if not prefill_matches(picked.get("text") or "", old):
            await self._abort_edit(scope)
            return {
                **base,
                "status": "editor_prefill_mismatch",
                "prefilled": canon(picked.get("text") or "")[:200],
            }
        editor = self._tagged("editor")
        inserted = await editor.evaluate(_REPLACE_EDITOR_JS, new_text)
        await self._wait(0.8, 1.4)
        typed = await editor.inner_text()
        if not inserted or canon(typed) != canon(new_text):
            await self._abort_edit(scope)
            return {**base, "status": "editor_mismatch", "typed": canon(typed)[:200]}
        save = await self._pick(
            _BUTTON_PICK_JS, {"scope": scope, "words": SAVE_WORDS, "tag": "save"}
        )
        if save.get("count") != 1 or save.get("disabled"):
            await self._abort_edit(scope)
            return {
                **base,
                "status": "save_button_unavailable",
                "buttons": save.get("count"),
            }
        self.clicked = True
        await self._tagged("save").click()
        base["done"] = True
        try:
            await self._wait(3.0, 5.0)
            reread = await self._reread_text(activity_id, comment_id)
        except Exception as exc:
            logger.warning("edit read-back failed", exc_info=True)
            return {
                **base,
                "status": "unverified",
                "verified": False,
                "verify_error": f"{type(exc).__name__}: {exc}",
            }
        landed = text_matches(reread, new_text)
        return {
            **base,
            "status": "verified" if landed else "unverified",
            "verified": landed,
            "read_back": canon(reread or "")[:200],
        }

    async def _reread_text(
        self, activity_id: str, comment_id: str | None
    ) -> str | None:
        await self._post_page(activity_id)
        post = await self._page.evaluate(_POST_CARD_JS, activity_id)
        if not comment_id:
            return (post or {}).get("text")
        card = await self._page.evaluate(_COMMENT_CARD_JS, comment_id)
        if not card or card.get("count") != 1:
            return None
        return split_comment_lines(card.get("lines") or []).get("text")

    async def _abort_edit(self, scope: str) -> None:
        """Leave the editor without saving: cancel in scope, then discard."""
        try:
            picked = await self._pick(
                _BUTTON_PICK_JS,
                {"scope": scope, "words": CANCEL_WORDS, "tag": "cancel"},
            )
            if picked.get("count") == 1:
                await self._tagged("cancel").click()
            else:
                await self._escape()
            await self._wait(0.6, 1.0)
            discard = await self._pick(
                _BUTTON_PICK_JS,
                {"scope": "dialog", "words": DISCARD_WORDS, "tag": "discard"},
            )
            if discard.get("count") == 1:
                await self._tagged("discard").click()
        except Exception:
            logger.warning("leaving the editor failed", exc_info=True)
