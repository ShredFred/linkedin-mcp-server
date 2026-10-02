"""MiViA fork: repost a post as the signed-in member, and take a repost back.

Built 2026-10-02. The repost control is the social-bar button "Reposten" /
"Repost" of the focused post card; it opens a menu with an instant repost, a
repost with own thoughts and -- once reposted -- an undo entry. The menu wording
was not measured with a real repost; everything here is therefore fail-closed:

* no button, more than one button, no menu, or no single matching menu entry:
  nothing is clicked beyond opening the menu, and the menu is closed again;
* the dry run opens the menu, reports its entries and closes it (Escape) --
  opening the menu publishes nothing;
* after the confirmed click the post is reloaded and the menu read again: a
  repost counts as verified only when the undo entry is now present, an undo
  only when it is gone. Anything else is ``unverified``.

Repost with own thoughts and reposting as a company page are not automated.
"""

from __future__ import annotations

import logging
import random
import re
from typing import Any

from linkedin_mcp_server.linkedin.mivia_actions import MiviaActions

logger = logging.getLogger(__name__)

_POST_URL = "https://www.linkedin.com/feed/update/urn:li:activity:{}/"

# The repost button of the focused card: aria-label or text starting with
# "Repost"/"Reposten", never inside a comment. Marked for the click.
_MARK_REPOST_BUTTON_JS = r"""() => {
  document.querySelectorAll('[data-mivia-repost]').forEach(e => e.removeAttribute('data-mivia-repost'));
  const card = document.querySelector('[componentkey^="update-card-focus"]') || document.querySelector('main');
  if (!card) return {count: 0};
  const isRepost = b => {
    const label = (b.getAttribute('aria-label') || '').trim();
    const text = (b.innerText || '').trim();
    return /^repost/i.test(label) || /^repost(en)?$/i.test(text);
  };
  const buttons = [...card.querySelectorAll('button')].filter(b =>
    isRepost(b) && !b.closest('[componentkey^="replaceableComment_urn:li:comment:"]'));
  if (buttons.length === 1) buttons[0].setAttribute('data-mivia-repost', '1');
  return {count: buttons.length,
          label: buttons.length ? (buttons[0].getAttribute('aria-label') || buttons[0].innerText || '').trim().slice(0, 120) : null};
}"""

# Visible menu entries after the button click, each marked with its index.
_MENU_ITEMS_JS = r"""() => {
  document.querySelectorAll('[data-mivia-menu]').forEach(e => e.removeAttribute('data-mivia-menu'));
  const seen = new Set();
  const items = [...document.querySelectorAll(
      '[role="menu"] [role="menuitem"], [role="menuitem"], .artdeco-dropdown__item, [role="menu"] li')]
    .filter(e => {
      if (seen.has(e)) return false;
      seen.add(e);
      const r = e.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && (e.innerText || '').trim();
    });
  // An outer li that holds an inner menuitem is the same entry twice.
  const leaves = items.filter(e => !items.some(o => o !== e && e.contains(o)));
  return leaves.map((e, i) => {
    e.setAttribute('data-mivia-menu', String(i));
    return {index: i, text: (e.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 200)};
  });
}"""

_UNDO_RE = re.compile(r"rückgängig|undo|entfernen|remove repost|löschen", re.I)
_THOUGHTS_RE = re.compile(r"gedanken|thoughts|kommentar", re.I)
_INSTANT_RE = re.compile(r"^\s*repost(en)?\b|sofort|instantly", re.I)


def classify_menu_entry(text: Any) -> str | None:
    """'undo', 'thoughts', 'instant' or None for an unrelated entry."""
    if not isinstance(text, str) or not text.strip():
        return None
    if _UNDO_RE.search(text):
        return "undo"
    if _THOUGHTS_RE.search(text):
        return "thoughts"
    if _INSTANT_RE.search(text):
        return "instant"
    return None


def pick_entry(items: Any, wanted: str) -> tuple[dict[str, Any] | None, bool]:
    """(the single entry of class *wanted*, undo entry present)."""
    entries = (
        [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
    )
    classes = [classify_menu_entry(i.get("text")) for i in entries]
    has_undo = "undo" in classes
    matches = [e for e, c in zip(entries, classes) if c == wanted]
    return (matches[0] if len(matches) == 1 else None), has_undo


class MiviaReposter(MiviaActions):
    """Repost / undo repost behind an explicit confirm flag."""

    repost_clicked: bool = False

    async def _open_menu(self, activity_id: str) -> dict[str, Any]:
        await self._goto(_POST_URL.format(activity_id))
        mark = await self._page.evaluate(_MARK_REPOST_BUTTON_JS)
        count = mark.get("count") if isinstance(mark, dict) else 0
        if count != 1:
            return {
                "status": "no_repost_button"
                if not count
                else "repost_button_ambiguous",
                "buttons": count,
            }
        await self._page.locator('[data-mivia-repost="1"]').first.click()
        await self._session.delay(random.uniform(0.8, 1.5))
        items = await self._page.evaluate(_MENU_ITEMS_JS)
        if not isinstance(items, list) or not items:
            await self._escape_quietly()
            return {"status": "menu_missing"}
        return {"status": "menu_open", "items": items}

    async def _undo_present(self, activity_id: str) -> bool | None:
        """Reload and read the menu again: is the undo entry there? None = unreadable."""
        opened = await self._open_menu(activity_id)
        if opened["status"] != "menu_open":
            return None
        await self._escape_quietly()
        return pick_entry(opened["items"], "undo")[1]

    async def repost(
        self, activity_id: str, *, undo: bool = False, confirm: bool = False
    ) -> dict[str, Any]:
        self.repost_clicked = False
        opened = await self._open_menu(activity_id)
        if opened["status"] != "menu_open":
            return {**opened, "done": False}
        items = opened["items"]
        menu = [i.get("text") for i in items if isinstance(i, dict)]
        wanted = "undo" if undo else "instant"
        entry, has_undo = pick_entry(items, wanted)
        if not undo and has_undo:
            await self._escape_quietly()
            return {"status": "already_reposted", "done": False, "menu": menu}
        if undo and not has_undo:
            await self._escape_quietly()
            return {"status": "not_reposted", "done": False, "menu": menu}
        if entry is None:
            await self._escape_quietly()
            return {"status": "menu_unclear", "done": False, "menu": menu}
        if not confirm:
            await self._escape_quietly()
            return {
                "status": "dry_run",
                "done": False,
                "menu": menu,
                "would_click": entry.get("text"),
            }
        self.repost_clicked = True
        await self._page.locator(
            f'[data-mivia-menu="{int(entry["index"])}"]'
        ).first.click()
        await self._session.delay(random.uniform(3.0, 5.0))
        try:
            present = await self._undo_present(activity_id)
        except Exception:
            logger.warning("repost read-back failed", exc_info=True)
            present = None
        verified = present is (not undo)
        if undo:
            status = "undone" if verified else "undo_unverified"
        else:
            status = "reposted" if verified else "unverified"
        return {"status": status, "done": True, "verified": verified, "menu": menu}
