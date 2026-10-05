"""Fork extension, stage 2 pages: profile viewers, groups, invitation withdrawal,
event invitations and post comments.

Measured 2026-09-29 (headless, de locale):

* ``/analytics/profile-views/`` lists viewers as cards with a ``/in/`` link;
  the generic card walk of :mod:`ext_network` reads them.
* ``/groups/`` lists the member's groups with member counts;
  ``/groups/<id>/members/`` renders members as ``li`` cards.
* The sent-invitations page renders ``a[aria-label="Einladung an <Name>
  zurückziehen"]`` per card. Whether a confirmation dialog follows could not be
  measured without withdrawing a real invitation; :meth:`withdraw` handles both
  and verifies by re-reading the list.
* Event invitations exist only for organisers / page admins of the event. The
  congress event belongs to its organiser and renders no "Einladen" button for this account;
  the invite dialog is therefore not measured and not automated -- the tool
  reports the capability instead of clicking blind.
* The comment editor is ``[role=textbox][aria-label="Texteditor zum Erstellen
  von Kommentaren"]`` (tiptap); its submit is the button whose text is
  "Kommentieren" and which carries *no* aria-label -- the social-bar button of
  the same text carries one and a count.
"""

from __future__ import annotations

import logging
import random
import re
from typing import Any
from urllib.parse import quote

from linkedin_mcp_server.linkedin.ext_network import (
    SENT_INVITATIONS_URL,
    ExtNetworkReader,
    _profile_url,
    split_person_lines,
)

logger = logging.getLogger(__name__)

PROFILE_VIEWS_URL = "https://www.linkedin.com/analytics/profile-views/"
GROUPS_URL = "https://www.linkedin.com/groups/"

_GROUP_ID_RE = re.compile(r"(?:/groups/)?(\d{3,12})/?")

# Relative "sent" wording (de/en) -> age in days. LinkedIn rounds, so the value
# is a lower bound of the real age.
_AGE_PATTERNS = [
    (re.compile(r"(\d+)\s*(Minute|Minuten|minute|minutes)"), 0),
    (re.compile(r"(\d+)\s*(Stunde|Stunden|hour|hours)"), 0),
    (re.compile(r"(\d+)\s*(Tag|Tagen|day|days)"), 1),
    (re.compile(r"(\d+)\s*(Woche|Wochen|week|weeks)"), 7),
    (re.compile(r"(\d+)\s*(Monat|Monaten|month|months)"), 30),
    (re.compile(r"(\d+)\s*(Jahr|Jahren|year|years)"), 365),
]


def sent_age_days(text: str | None) -> int | None:
    """ "Vor 3 Wochen gesendet" -> 21, "Gestern gesendet" -> 1, unknown -> None."""
    if not text or not isinstance(text, str):
        return None
    low = text.lower()
    if "gestern" in low or "yesterday" in low:
        return 1
    if "heute" in low or "today" in low or low.strip() in {"gesendet", "sent"}:
        return 0
    for pattern, factor in _AGE_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.group(1)) * factor
    return None


_DEGREE_LABEL_RE = re.compile(
    r"^(Kontakt\s+\d\.\s+Grades|\d(st|nd|rd|th)\s+degree connection)$", re.IGNORECASE
)


def group_member_lines(lines: list[str]) -> list[str]:
    """Drop the spelled-out degree label and normalise '· 3.' to '• 3.'."""
    out = []
    for line in lines if isinstance(lines, list) else []:
        if not isinstance(line, str):
            continue
        line = line.replace(" ", " ").strip()
        if _DEGREE_LABEL_RE.match(line):
            continue
        if line.startswith("·"):
            line = "•" + line[1:]
        out.append(line)
    return out


def parse_group_id(group: str) -> str:
    match = _GROUP_ID_RE.search(group.strip())
    if not match:
        raise ValueError("group must be a group URL or numeric group id")
    return match.group(1)


_GROUPS_JS = r"""() => {
  const main = document.querySelector('main') || document.body;
  const out = new Map();
  for (const a of main.querySelectorAll('a[href*="/groups/"]')) {
    const m = /\/groups\/(\d+)/.exec(a.getAttribute('href') || '');
    const name = (a.innerText || '').trim();
    if (!m || !name || out.has(m[1])) continue;
    let card = a;
    for (let i = 0; i < 4 && card.parentElement; i++) card = card.parentElement;
    const members = /([\d.,]+)\s*(Mitglieder|members)/i.exec(card.innerText || '');
    out.set(m[1], {id: m[1], name, members: members ? members[1] : null});
  }
  return [...out.values()];
}"""

_WITHDRAW_LINKS_JS = r"""() => [...document.querySelectorAll('main a[aria-label], main button[aria-label]')]
  .map(e => e.getAttribute('aria-label'))
  .filter(a => /zurückziehen$|^Withdraw invitation/i.test(a || ''))"""

_EVENT_CAPS_JS = r"""() => {
  const main = document.querySelector('main') || document.body;
  const btns = [...main.querySelectorAll('button, a')];
  const invite = btns.find(b => /^(Einladen|Invite)$/i.test((b.innerText || '').trim()) ||
                               /^(Einladen|Invite)/i.test(b.getAttribute('aria-label') || ''));
  const title = (main.querySelector('h1') || {}).innerText || null;
  const organiser = /Event von (.+)|Event by (.+)/.exec(main.innerText || '');
  return {
    can_invite: !!invite,
    title,
    organiser: organiser ? (organiser[1] || organiser[2]).split('\n')[0].trim() : null,
  };
}"""

_COMMENT_EDITOR = (
    '[componentkey^="update-card-focus"] [role="textbox"]'
    '[aria-label*="Kommentar"], [componentkey^="update-card-focus"] '
    '[role="textbox"][aria-label*="comment"]'
)

# The submit belongs to the editor: walk up from the editor to the smallest
# ancestor holding a "Kommentieren"/"Comment" button without aria-label, and
# never into a comment card (whose "Antworten" buttons must not be clicked).
_COMMENT_SUBMIT_JS = r"""(editor) => {
  document.querySelectorAll('[data-ext-submit]').forEach(e => e.removeAttribute('data-ext-submit'));
  const isSubmit = b => /^(Kommentieren|Comment)$/i.test((b.innerText || '').trim()) &&
                        !b.getAttribute('aria-label');
  let box = editor;
  for (let i = 0; i < 8 && box; i++) {
    box = box.parentElement;
    if (!box || box.matches('[componentkey^="update-card-focus"]')) break;
    const b = [...box.querySelectorAll('button')].find(b =>
      isSubmit(b) && !b.closest('[componentkey^="replaceableComment_urn:li:comment:"]'));
    if (b) {
      b.setAttribute('data-ext-submit', '1');
      return {disabled: b.disabled, text: (b.innerText || '').trim(), depth: i + 1};
    }
  }
  return null;
}"""

# Mark the withdraw control inside the card that links exactly to /in/<slug>/.
_MARK_WITHDRAW_JS = r"""(slug) => {
  document.querySelectorAll('[data-ext-withdraw]').forEach(e => e.removeAttribute('data-ext-withdraw'));
  const main = document.querySelector('main') || document.body;
  const want = '/in/' + slug.toLowerCase();
  const own = a => {
    try {
      const p = new URL(a.getAttribute('href'), location.href).pathname.replace(/\/+$/, '');
      return decodeURIComponent(p).toLowerCase() === want;
    } catch { return false; }
  };
  const isWithdraw = e => /zurückziehen$|^Withdraw invitation/i.test(e.getAttribute('aria-label') || '');
  for (const a of main.querySelectorAll('a[href*="/in/"]')) {
    if (!own(a)) continue;
    let card = a;
    for (let i = 0; i < 10 && card; i++) {
      card = card.parentElement;
      if (!card) break;
      const controls = [...card.querySelectorAll('a[aria-label], button[aria-label]')].filter(isWithdraw);
      if (controls.length > 1) break;  // left the card: more than one invitation inside
      if (controls.length === 1) { controls[0].setAttribute('data-ext-withdraw', '1'); return true; }
    }
  }
  return false;
}"""

# Comment key (urn) with its text: the read-back compares the keys before and
# after the click, so an older comment with the same opening never counts.
_COMMENT_TEXTS_JS = r"""() => [...document.querySelectorAll('[componentkey^="replaceableComment_urn:li:comment:"]')]
  .map(c => ({key: c.getAttribute('componentkey') || '', text: c.innerText || ''}))"""


# -- replies to a comment (2026-10-05) -----------------------------------------
#
# Measured (2026-09-29, ext_engagement): a comment card carries
# componentkey="replaceableComment_urn:li:comment:(activity:A,C)", and a reply
# is a comment card nested inside its parent card. LinkedIn nests one level: a
# reply to a reply lands in the parent's thread, so the read-back looks under
# the outermost card (the thread root), not under the replied-to reply.
#
# NOT measured -- assumptions, each failing closed (never a post-level comment):
# * data-id / data-urn="urn:li:comment:(...)" as further card markers, and the
#   key of a nested reply: matched only by its ",<id>)" suffix plus the
#   activity id; anything else is reply_target_not_found.
# * the comment's own reply control: a button whose text is "Antworten"/"Reply"
#   or whose aria-label starts with it, owned by the target card itself (not
#   by a nested reply); none -> reply_button_missing, two ->
#   reply_button_ambiguous.
# * the reply editor: a visible contenteditable inside the thread root card,
#   owned by the root or the target; not exactly one -> reply_editor_missing.
#   A prefilled text (an @-mention, say) is cleared; if it stays,
#   reply_editor_not_empty.
# * the reply submit: the nearest button walking up from the editor (never
#   past the root card) with text Antworten/Reply/Kommentieren/Comment, no
#   aria-label, owned by the editor's card, not the reply control; not exactly
#   one -> no_submit.
# * the expanders of a collapsed thread ("Weitere Kommentare laden",
#   "Vorherige Antworten anzeigen", "Load more comments", "previous replies",
#   "N Antworten"): at most REPLY_MAX_EXPANSIONS clicks, then
#   reply_thread_collapsed.
# The permalink with commentUrn pins the target at the top (as for
# edit/delete_own_comment), so the expanders are a second line only.

REPLY_MAX_EXPANSIONS = 5
_REPLY_ATTR = "data-ext-reply"

_REPLY_COMMON_JS = r"""
  const CARD = '[componentkey^="replaceableComment_urn:li:comment:"], [data-id^="urn:li:comment:"], [data-urn^="urn:li:comment:"]';
  const vis = el => !!(el && el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden');
  const keyOf = e => e.getAttribute('componentkey') || e.getAttribute('data-id') || e.getAttribute('data-urn') || '';
  const idOf = k => { const m = /,(\d+)\)$/.exec(k || ''); return m ? m[1] : null; };
  const ownerId = el => { const c = el.closest(CARD); return c ? idOf(keyOf(c)) : null; };
  const clearTag = tag => document.querySelectorAll('[data-ext-reply="' + tag + '"]')
      .forEach(e => e.removeAttribute('data-ext-reply'));
  const outermost = list => list.filter(e => !list.some(o => o !== e && o.contains(e)));
  // The thread root: the outermost comment card around el with another id.
  const rootOf = (el, id) => {
    let root = el;
    for (let p = el.parentElement; p; p = p.parentElement) {
      if (p.matches(CARD) && idOf(keyOf(p)) && idOf(keyOf(p)) !== id) root = p;
    }
    return root;
  };
"""

# Tags the target card, its thread root and its own reply control.
_REPLY_LOCATE_JS = (
    "(arg) => {"
    + _REPLY_COMMON_JS
    + r"""
  ['target', 'root', 'reply-button', 'reply-editor', 'reply-submit'].forEach(clearTag);
  const isTarget = e => { const k = keyOf(e);
      return k.endsWith(',' + arg.id + ')') && k.includes('activity:' + arg.activity); };
  const hits = outermost([...document.querySelectorAll(CARD)].filter(isTarget));
  if (hits.length !== 1) return {count: hits.length};
  const target = hits[0];
  const root = rootOf(target, arg.id);
  target.setAttribute('data-ext-reply', 'target');
  if (root !== target) root.setAttribute('data-ext-reply', 'root');
  const buttons = [...target.querySelectorAll('button, [role="button"]')].filter(b => vis(b) &&
      ownerId(b) === arg.id &&
      (/^(antworten|reply)$/i.test((b.innerText || '').trim()) ||
       /^(antworten|reply)\b/i.test((b.getAttribute('aria-label') || '').trim())));
  if (buttons.length === 1) buttons[0].setAttribute('data-ext-reply', 'reply-button');
  return {count: 1, buttons: buttons.length, root_id: idOf(keyOf(root)), root_key: keyOf(root)};
}"""
)

# Tags one visible expander of a collapsed comment list or reply thread.
_REPLY_EXPAND_JS = (
    "() => {"
    + _REPLY_COMMON_JS
    + r"""
  clearTag('expand');
  const words = /(weitere kommentare|mehr kommentare|vorherige kommentare|vorherige antworten|weitere antworten|antworten anzeigen|^\d+ antworten?$|load more comments|more comments|previous comments|previous repl|more repl|view \d* ?repl|^\d+ repl(y|ies)$)/i;
  const main = document.querySelector('main') || document.body;
  const hits = [...main.querySelectorAll('button, [role="button"]')].filter(b => vis(b) &&
      words.test((b.innerText || '').trim() || (b.getAttribute('aria-label') || '').trim()));
  if (hits.length) hits[0].setAttribute('data-ext-reply', 'expand');
  return hits.length;
}"""
)

# The reply editor: inside the thread root, owned by the root or the target.
_REPLY_EDITOR_JS = (
    "(arg) => {"
    + _REPLY_COMMON_JS
    + r"""
  clearTag('reply-editor');
  const target = document.querySelector('[data-ext-reply="target"]');
  if (!target) return {count: 0};
  const root = document.querySelector('[data-ext-reply="root"]') || target;
  const rootId = idOf(keyOf(root));
  const hits = outermost([...root.querySelectorAll('[contenteditable="true"], [role="textbox"][contenteditable]')]
      .filter(e => vis(e) && [rootId, arg.id].includes(ownerId(e))));
  if (hits.length === 1) hits[0].setAttribute('data-ext-reply', 'reply-editor');
  return {count: hits.length, text: hits.length === 1 ? (hits[0].innerText || '') : null};
}"""
)

# The reply submit: nearest matching button walking up from the editor, never
# past the thread root, never the reply control itself.
_REPLY_SUBMIT_JS = (
    "(editor) => {"
    + _REPLY_COMMON_JS
    + r"""
  clearTag('reply-submit');
  const root = document.querySelector('[data-ext-reply="root"]') ||
      document.querySelector('[data-ext-reply="target"]');
  if (!root || !root.contains(editor)) return {count: 0};
  const isSubmit = b => vis(b) && /^(antworten|reply|kommentieren|comment)$/i.test((b.innerText || '').trim()) &&
      !b.getAttribute('aria-label') && b.getAttribute('data-ext-reply') !== 'reply-button' &&
      ownerId(b) === ownerId(editor);
  let box = editor;
  for (let i = 0; i < 8 && box !== root; i++) {
    box = box.parentElement;
    if (!box || !root.contains(box)) break;
    const hits = [...box.querySelectorAll('button')].filter(isSubmit);
    if (hits.length === 1) {
      hits[0].setAttribute('data-ext-reply', 'reply-submit');
      return {count: 1, disabled: hits[0].disabled, depth: i + 1};
    }
    if (hits.length > 1) return {count: hits.length};
  }
  return {count: 0};
}"""
)

# Replies rendered under the thread root, found again by its key (a re-render
# drops the tag); roots says how often the root itself was found.
_REPLY_READBACK_JS = (
    "(rootKey) => {"
    + _REPLY_COMMON_JS
    + r"""
  const roots = outermost([...document.querySelectorAll(CARD)].filter(e => keyOf(e) === rootKey));
  if (roots.length !== 1) return {roots: roots.length, replies: []};
  const rootId = idOf(rootKey);
  const replies = outermost([...roots[0].querySelectorAll(CARD)]
      .filter(e => idOf(keyOf(e)) && idOf(keyOf(e)) !== rootId));
  return {roots: 1, replies: replies.map(c => ({key: keyOf(c), text: c.innerText || ''}))};
}"""
)


def reply_permalink(activity_id: str, comment_id: str) -> str:
    """Post page with the target comment pinned (same form as edit/delete)."""
    urn = quote(f"urn:li:comment:(activity:{activity_id},{comment_id})", safe="")
    return (
        f"https://www.linkedin.com/feed/update/urn:li:activity:{activity_id}/"
        f"?commentUrn={urn}"
    )


def _canon(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _matching_comment_keys(comments: Any, probe: str) -> set[str]:
    """Keys of the rendered comments whose text carries *probe*.

    A comment without a key cannot be told apart from an older one and never
    counts as new.
    """
    keys: set[str] = set()
    for c in comments if isinstance(comments, list) else []:
        if not isinstance(c, dict):
            continue
        key = str(c.get("key") or "")
        if key and probe in _canon(str(c.get("text") or "")):
            keys.add(key)
    return keys


class ExtActions(ExtNetworkReader):
    """Stage-2 page walks. Writes happen only behind an explicit confirm flag."""

    async def _goto(self, url: str) -> None:
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        await self._session.delay(random.uniform(2.5, 4.0))

    # -- reads ---------------------------------------------------------------

    async def profile_viewers(self, limit: int) -> dict[str, Any]:
        await self._goto(PROFILE_VIEWS_URL)
        await self._wait_for_cards()
        cards = await self._scroll_until(limit=limit, max_rounds=30)
        viewers = []
        for card in cards[:limit]:
            person = split_person_lines(card["lines"])
            viewers.append(
                {
                    "name": person["name"],
                    "slug": card["slug"],
                    "profile_url": _profile_url(card["slug"]),
                    "profile_urn": card.get("profile_urn"),
                    "degree": person["degree"],
                    "headline": person["rest"][0] if person["rest"] else None,
                    "detail": person["rest"][1:4],
                }
            )
        header = await self._page.evaluate(
            r"""() => { const t=(document.querySelector('main')||document.body).innerText; const m=/(\d[\d.]*)\s*\n+\s*Profilbesucher/.exec(t) || /(\d[\d.]*)\s*\n+\s*Profile viewers/.exec(t); return m ? m[1] : null; }"""
        )
        return {
            "url": PROFILE_VIEWS_URL,
            "window": "90_days",
            "total_viewers": int(header.replace(".", "")) if header else None,
            "count": len(viewers),
            "viewers": viewers,
        }

    async def list_groups(self) -> dict[str, Any]:
        await self._goto(GROUPS_URL)
        groups = await self._page.evaluate(_GROUPS_JS)
        for group in groups:
            if group.get("members"):
                group["members"] = int(re.sub(r"[.,]", "", group["members"]))
        return {"count": len(groups), "groups": groups}

    async def group_members(self, group_id: str, limit: int) -> dict[str, Any]:
        url = f"https://www.linkedin.com/groups/{group_id}/members/"
        await self._goto(url)
        await self._wait_for_cards()
        cards = await self._scroll_until(limit=limit, max_rounds=40)
        members = []
        for card in cards[:limit]:
            person = split_person_lines(group_member_lines(card["lines"]))
            rest = [
                ln for ln in person["rest"] if ln not in {"Nachricht", "Message", "--"}
            ]
            members.append(
                {
                    "name": person["name"],
                    "slug": card["slug"],
                    "profile_url": _profile_url(card["slug"]),
                    "degree": person["degree"],
                    "headline": rest[0] if rest else None,
                }
            )
        return {
            "group_id": group_id,
            "url": url,
            "count": len(members),
            "members": members,
        }

    async def event_capabilities(self, event_id: str) -> dict[str, Any]:
        await self._goto(f"https://www.linkedin.com/events/{event_id}/")
        return await self._page.evaluate(_EVENT_CAPS_JS)

    # -- invitation withdrawal ------------------------------------------------

    async def sent_invitations_with_age(self, limit: int) -> list[dict[str, Any]]:
        listed = await self.list_sent_invitations(limit)
        for inv in listed["invitations"]:
            inv["age_days"] = sent_age_days(inv.get("sent_text"))
        return listed["invitations"]

    async def _slug_present(self, slug: str) -> bool | None:
        """Is *slug* still on the sent list? None when the list did not load fully."""
        await self._goto(SENT_INVITATIONS_URL)
        await self._wait_for_cards()
        listed = await self.list_sent_invitations(1000)
        if not listed.get("complete"):
            return None
        return any(inv["slug"] == slug for inv in listed["invitations"])

    async def withdraw(self, name: str, slug: str) -> dict[str, Any]:
        """Withdraw one pending invitation, located by its card's profile slug.

        By slug, not by name: two pending invitations to namesakes would
        otherwise withdraw whichever card renders first. The sent list must be
        open and scrolled far enough to hold the card (list_sent_invitations
        leaves it so).
        """
        # Click marker (fork, 2026-10-05): False until just before the first
        # click, so a deadline or cancellation before it books a retryable
        # not_done instead of a permanent unknown.
        self.withdraw_clicked = False
        marked = await self._page.evaluate(_MARK_WITHDRAW_JS, slug)
        if not marked:
            return {"slug": slug, "name": name, "status": "not_found"}
        link = self._page.locator('[data-ext-withdraw="1"]').first
        await link.scroll_into_view_if_needed()
        await self._session.delay(random.uniform(0.8, 1.6))
        self.withdraw_clicked = True
        await link.click()
        try:
            await self._session.delay(random.uniform(1.5, 2.5))
            dialog = self._page.locator('[role="dialog"], [role="alertdialog"]').first
            has_dialog = await dialog.count()
        except BaseException:
            # The confirm dialog may be open: never leave it on the page.
            await self._escape_quietly()
            raise
        if has_dialog:
            confirm = (
                dialog.locator("button")
                .filter(has_text=re.compile(r"^\s*(Zurückziehen|Withdraw)\s*$"))
                .first
            )
            try:
                found = await confirm.count()
            except BaseException:
                await self._escape_quietly()
                raise
            if not found:
                # fork extension (2026-10-01): a confirm dialog without a
                # recognisable confirm button was left open and then read
                # back as still_pending, which the ledger counts as a click.
                # Nothing was withdrawn: close it and report uncounted.
                try:
                    await self._page.keyboard.press("Escape")
                except Exception:
                    pass
                return {"slug": slug, "name": name, "status": "not_confirmed"}
            await confirm.click()
            await self._session.delay(random.uniform(1.5, 2.5))
        # Verify on a fully loaded list: the oldest invitations, which are the
        # candidates, sit at the bottom and are not rendered without scrolling.
        present = await self._slug_present(slug)
        status = {True: "still_pending", False: "withdrawn", None: "unverified"}[
            present
        ]
        return {"slug": slug, "name": name, "status": status}

    # -- comments ------------------------------------------------------------

    async def comment(
        self, activity_id: str, text: str, confirm: bool = False
    ) -> dict[str, Any]:
        await self._goto(
            f"https://www.linkedin.com/feed/update/urn:li:activity:{activity_id}/"
        )
        editor = self._page.locator(_COMMENT_EDITOR).first
        if await editor.count() == 0:
            return {"status": "no_editor", "posted": False}
        self.comment_submitted = False
        try:
            return await self._comment_typed(editor, text, confirm)
        except BaseException:
            # Typed text left in the editor would be prepended to the next
            # comment on this post; clear it unless the submit was clicked.
            if not self.comment_submitted:
                try:
                    await self._clear(editor)
                except Exception:
                    logger.warning("clearing the comment editor failed", exc_info=True)
            raise

    async def _escape_quietly(self) -> None:
        try:
            await self._page.keyboard.press("Escape")
        except Exception:
            logger.debug("Escape failed", exc_info=True)

    async def _comment_typed(
        self, editor: Any, text: str, confirm: bool
    ) -> dict[str, Any]:
        await self._insert_text(editor, text)
        typed = _canon(await editor.inner_text())
        submit = await editor.evaluate(_COMMENT_SUBMIT_JS)
        if typed != _canon(text) or submit is None or submit["disabled"]:
            await self._clear(editor)
            return {
                "status": "editor_mismatch" if typed != _canon(text) else "no_submit",
                "posted": False,
                "typed": typed,
            }
        if not confirm:
            await self._clear(editor)
            return {"status": "dry_run", "posted": False, "verified_text": typed}
        # LinkedIn truncates long comments ("…mehr"); a prefix is enough to
        # recognise our own fresh comment -- but only a fresh one: a comment
        # with the same opening that was already on the page before the click
        # made a failed submit read as posted.
        probe = _canon(text)[:150]
        before = _matching_comment_keys(
            await self._page.evaluate(_COMMENT_TEXTS_JS), probe
        )
        self.comment_submitted = True
        await self._page.locator('[data-ext-submit="1"]').first.click()
        await self._session.delay(random.uniform(3.0, 5.0))
        after = _matching_comment_keys(
            await self._page.evaluate(_COMMENT_TEXTS_JS), probe
        )
        verified = bool(after - before)
        return {
            "status": "posted" if verified else "unverified",
            "posted": True,
            "verified": verified,
        }

    async def _insert_text(self, editor: Any, text: str) -> None:
        """The one typing path for comments and replies: LF as Shift+Enter."""
        await editor.click()
        await self._session.delay(random.uniform(0.6, 1.2))
        for index, paragraph in enumerate(text.split("\n")):
            if index:
                await self._page.keyboard.press("Shift+Enter")
            if paragraph:
                await self._page.keyboard.insert_text(paragraph)
        await self._session.delay(random.uniform(0.8, 1.5))

    # -- replies -------------------------------------------------------------

    async def reply(
        self, activity_id: str, comment_id: str, text: str, confirm: bool = False
    ) -> dict[str, Any]:
        """Reply to the comment *comment_id* under the post *activity_id*.

        Never falls back to the post-level comment box: every element is found
        inside the target's thread card, and anything not found exactly once
        ends the call before a click that could write.
        """
        self.comment_submitted = False
        await self._goto(reply_permalink(activity_id, comment_id))
        arg = {"activity": activity_id, "id": comment_id}
        located = await self._locate_reply_target(arg)
        if located["status"] != "ok":
            return {**located, "posted": False}
        info = {
            "target_comment_id": comment_id,
            "thread_root_id": located["root_id"],
            "reply_to_reply": located["root_id"] != comment_id,
        }
        await self._page.locator(f'[{_REPLY_ATTR}="reply-button"]').first.click()
        await self._session.delay(random.uniform(1.0, 2.0))
        pick = await self._page.evaluate(_REPLY_EDITOR_JS, arg) or {"count": 0}
        if pick.get("count") != 1:
            await self._escape_quietly()
            return {
                "status": "reply_editor_missing",
                "posted": False,
                "count": pick.get("count"),
                **info,
            }
        editor = self._page.locator(f'[{_REPLY_ATTR}="reply-editor"]').first
        try:
            if _canon(str(pick.get("text") or "")):
                # A prefilled text (LinkedIn may insert an @-mention of the
                # replied-to author; unmeasured) is removed: the reply carries
                # exactly the given text. Mentions are not supported.
                await self._clear(editor)
                if _canon(await editor.inner_text()):
                    await self._clear(editor)
                    await self._escape_quietly()
                    return {
                        "status": "reply_editor_not_empty",
                        "posted": False,
                        **info,
                    }
            result = await self._reply_typed(
                editor, text, confirm, str(located["root_key"])
            )
            return {**result, **info}
        except BaseException:
            if not self.comment_submitted:
                try:
                    await self._clear(editor)
                except Exception:
                    logger.warning("clearing the reply editor failed", exc_info=True)
            raise

    async def _locate_reply_target(self, arg: dict[str, Any]) -> dict[str, Any]:
        """The target card exactly once, a collapsed thread expanded at most
        REPLY_MAX_EXPANSIONS times; ``{"status": "ok", ...}`` or a refusal."""
        expansions = 0
        while True:
            found = await self._page.evaluate(_REPLY_LOCATE_JS, arg) or {"count": 0}
            count = found.get("count") or 0
            if count > 1:
                return {"status": "reply_target_ambiguous", "count": count}
            if count == 1:
                break
            if expansions >= REPLY_MAX_EXPANSIONS:
                return {"status": "reply_thread_collapsed", "expansions": expansions}
            if not await self._page.evaluate(_REPLY_EXPAND_JS):
                return {"status": "reply_target_not_found", "expansions": expansions}
            await self._page.locator(f'[{_REPLY_ATTR}="expand"]').first.click()
            expansions += 1
            await self._session.delay(random.uniform(1.5, 2.5))
        buttons = found.get("buttons")
        if buttons != 1:
            return {
                "status": "reply_button_ambiguous"
                if isinstance(buttons, int) and buttons > 1
                else "reply_button_missing",
                "count": buttons,
            }
        return {
            "status": "ok",
            "root_id": found.get("root_id"),
            "root_key": found.get("root_key"),
            "expansions": expansions,
        }

    async def _reply_typed(
        self, editor: Any, text: str, confirm: bool, root_key: str
    ) -> dict[str, Any]:
        await self._insert_text(editor, text)
        typed = _canon(await editor.inner_text())
        submit = await editor.evaluate(_REPLY_SUBMIT_JS) or {"count": 0}
        if typed != _canon(text) or submit.get("count") != 1 or submit.get("disabled"):
            await self._clear(editor)
            await self._escape_quietly()
            return {
                "status": "editor_mismatch" if typed != _canon(text) else "no_submit",
                "posted": False,
                "typed": typed,
            }
        if not confirm:
            await self._clear(editor)
            await self._escape_quietly()
            return {"status": "dry_run", "posted": False, "verified_text": typed}
        # Same rule as a comment: only a reply card that was not under the
        # thread root before the click, and carries the text, counts.
        probe = _canon(text)[:150]
        before = await self._page.evaluate(_REPLY_READBACK_JS, root_key) or {}
        self.comment_submitted = True
        await self._page.locator(f'[{_REPLY_ATTR}="reply-submit"]').first.click()
        await self._session.delay(random.uniform(3.0, 5.0))
        after = await self._page.evaluate(_REPLY_READBACK_JS, root_key) or {}
        new = _matching_comment_keys(
            after.get("replies"), probe
        ) - _matching_comment_keys(before.get("replies"), probe)
        # The before-read must have seen the thread too: an empty before (no
        # root, evaluate answered nothing) would count a pre-existing reply
        # with the same text as new.
        verified = before.get("roots") == 1 and after.get("roots") == 1 and bool(new)
        return {
            "status": "posted" if verified else "unverified",
            "posted": True,
            "verified": verified,
        }

    async def _clear(self, editor: Any) -> None:
        await editor.click()
        await self._page.keyboard.press("Control+A")
        await self._page.keyboard.press("Delete")
