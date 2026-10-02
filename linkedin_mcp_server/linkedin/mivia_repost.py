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
  // Measured 2026-10-02: the entries ("Sofort teilen", "Mit Kommentar
  // teilen") are div[role=button] inserted by the click. A candidate is any
  // visible clickable that was not visible before the click.
  const cands = [...document.querySelectorAll(
      '[role="menuitem"], [role="button"], .artdeco-dropdown__item, button')]
    .filter(e => {
      if (e.getAttribute('data-mivia-pre') === 'visible') return false;
      const r = e.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && (e.innerText || '').trim();
    });
  // Review 2026-10-02: a lazily loaded comment's "Entfernen" or a toast is
  // new as well. Only candidates inside the smallest container of the
  // share/repost entries count; without such an entry there is no menu.
  const share = cands.filter(e => /teilen|repost|share/i.test(e.innerText || ''));
  if (!share.length) return [];
  let root = share.length === 1 ? share[0].parentElement : share[0];
  while (root && !share.every(s => root.contains(s))) root = root.parentElement;
  if (!root) return [];
  const items = cands.filter(e => root.contains(e));
  // An outer element that holds an inner entry is the same entry twice.
  const leaves = items.filter(e => !items.some(o => o !== e && e.contains(o)));
  return leaves.map((e, i) => {
    e.setAttribute('data-mivia-menu', String(i));
    return {index: i, text: (e.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 200)};
  });
}"""

# Everything clickable before the repost click; the menu is what is new after it.
_MARK_PRESENT_JS = r"""() => {
  document.querySelectorAll('[role="menuitem"], [role="button"], .artdeco-dropdown__item, button')
    .forEach(e => {
      // Only what was visible before the click is "old".
      const r = e.getBoundingClientRect();
      e.setAttribute('data-mivia-pre', r.width > 0 && r.height > 0 ? 'visible' : 'hidden');
    });
  return true;
}"""

# Visible text lines of the page (with the role/tag of their element), for
# the before/after diff that reports an unrecognised menu.
_BODY_LINES_JS = r"""() => [...document.querySelectorAll('[role], li, button, a, span, div')]
  .filter(e => e.children.length <= 3)
  .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
  .map(e => {
    const t = (e.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 120);
    return t ? (e.getAttribute('role') || e.tagName.toLowerCase()) + ': ' + t : '';
  })
  .filter(Boolean)"""


def new_lines(before: Any, after: Any, limit: int = 40) -> list[str]:
    """Lines visible after but not before, in page order, at most *limit*."""
    old = set(before) if isinstance(before, list) else set()
    out: list[str] = []
    for line in after if isinstance(after, list) else []:
        if isinstance(line, str) and line not in old and line not in out:
            out.append(line)
            if len(out) >= limit:
                break
    return out


# Undo only together with the repost/share wording: "Löschen" or "Entfernen"
# alone may belong to anything on the page.
_UNDO_RE = re.compile(r"rückgängig|undo|entfernen|remove|löschen", re.I)
_UNDO_CONTEXT_RE = re.compile(r"repost|teilen|geteilt|share", re.I)
_THOUGHTS_RE = re.compile(r"gedanken|thoughts|kommentar", re.I)
_INSTANT_RE = re.compile(r"^\s*repost(en)?\b|sofort|instantly", re.I)


def classify_menu_entry(text: Any) -> str | None:
    """'undo', 'thoughts', 'instant' or None for an unrelated entry."""
    if not isinstance(text, str) or not text.strip():
        return None
    if _UNDO_RE.search(text) and _UNDO_CONTEXT_RE.search(text):
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


def is_share_menu(items: Any) -> bool:
    """The opened menu carries a recognised share entry (instant or thoughts)."""
    entries = items if isinstance(items, list) else []
    return any(
        isinstance(i, dict)
        and classify_menu_entry(i.get("text")) in {"instant", "thoughts"}
        for i in entries
    )


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
        before = await self._page.evaluate(_BODY_LINES_JS)
        await self._page.evaluate(_MARK_PRESENT_JS)
        await self._page.locator('[data-mivia-repost="1"]').first.click()
        await self._session.delay(random.uniform(0.8, 1.5))
        items = await self._page.evaluate(_MENU_ITEMS_JS)
        if not isinstance(items, list) or not items:
            # Measurement for the unmeasured menu: what appeared after the click.
            after = await self._page.evaluate(_BODY_LINES_JS)
            await self._escape_quietly()
            return {
                "status": "menu_missing",
                "button": mark.get("label"),
                "new_lines": new_lines(before, after),
            }
        return {"status": "menu_open", "items": items}

    async def _undo_present(self, activity_id: str) -> bool | None:
        """Reload and read the menu again: is the undo entry there? None = unreadable."""
        opened = await self._open_menu(activity_id)
        if opened["status"] != "menu_open":
            return None
        await self._escape_quietly()
        has_undo = pick_entry(opened["items"], "undo")[1]
        # A missing undo entry proves nothing in a menu we do not recognise.
        if not has_undo and not is_share_menu(opened["items"]):
            return None
        return has_undo

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
            if not is_share_menu(items):
                return {"status": "menu_unclear", "done": False, "menu": menu}
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
        try:
            selector = f'[data-mivia-menu="{int(entry["index"])}"]'
        except (KeyError, TypeError, ValueError):
            await self._escape_quietly()
            return {"status": "menu_unclear", "done": False, "menu": menu}
        # Set right before the click: everything above clicked nothing.
        self.repost_clicked = True
        await self._page.locator(selector).first.click()
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
