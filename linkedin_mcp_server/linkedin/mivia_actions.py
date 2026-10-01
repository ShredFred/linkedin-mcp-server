"""MiViA fork, stage 2 pages: profile viewers, groups, invitation withdrawal,
event invitations and post comments.

Measured 2026-09-29 (headless, de locale):

* ``/analytics/profile-views/`` lists viewers as cards with a ``/in/`` link;
  the generic card walk of :mod:`mivia_network` reads them.
* ``/groups/`` lists the member's groups with member counts;
  ``/groups/<id>/members/`` renders members as ``li`` cards.
* The sent-invitations page renders ``a[aria-label="Einladung an <Name>
  zurückziehen"]`` per card. Whether a confirmation dialog follows could not be
  measured without withdrawing a real invitation; :meth:`withdraw` handles both
  and verifies by re-reading the list.
* Event invitations exist only for organisers / page admins of the event. The
  HK event belongs to AWT and renders no "Einladen" button for this account;
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

from linkedin_mcp_server.linkedin.mivia_network import (
    SENT_INVITATIONS_URL,
    MiviaNetworkReader,
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
    if not text:
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
    for line in lines:
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
  document.querySelectorAll('[data-mivia-submit]').forEach(e => e.removeAttribute('data-mivia-submit'));
  const isSubmit = b => /^(Kommentieren|Comment)$/i.test((b.innerText || '').trim()) &&
                        !b.getAttribute('aria-label');
  let box = editor;
  for (let i = 0; i < 8 && box; i++) {
    box = box.parentElement;
    if (!box || box.matches('[componentkey^="update-card-focus"]')) break;
    const b = [...box.querySelectorAll('button')].find(b =>
      isSubmit(b) && !b.closest('[componentkey^="replaceableComment_urn:li:comment:"]'));
    if (b) {
      b.setAttribute('data-mivia-submit', '1');
      return {disabled: b.disabled, text: (b.innerText || '').trim(), depth: i + 1};
    }
  }
  return null;
}"""

# Mark the withdraw control inside the card that links exactly to /in/<slug>/.
_MARK_WITHDRAW_JS = r"""(slug) => {
  document.querySelectorAll('[data-mivia-withdraw]').forEach(e => e.removeAttribute('data-mivia-withdraw'));
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
      if (controls.length === 1) { controls[0].setAttribute('data-mivia-withdraw', '1'); return true; }
    }
  }
  return false;
}"""

# Comment key (urn) with its text: the read-back compares the keys before and
# after the click, so an older comment with the same opening never counts.
_COMMENT_TEXTS_JS = r"""() => [...document.querySelectorAll('[componentkey^="replaceableComment_urn:li:comment:"]')]
  .map(c => ({key: c.getAttribute('componentkey') || '', text: c.innerText || ''}))"""


def _canon(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _matching_comment_keys(comments: Any, probe: str) -> set[str]:
    """Keys of the rendered comments whose text carries *probe*.

    A comment without a key cannot be told apart from an older one and never
    counts as new.
    """
    keys: set[str] = set()
    for c in comments or []:
        if not isinstance(c, dict):
            continue
        key = str(c.get("key") or "")
        if key and probe in _canon(str(c.get("text") or "")):
            keys.add(key)
    return keys


class MiviaActions(MiviaNetworkReader):
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
        marked = await self._page.evaluate(_MARK_WITHDRAW_JS, slug)
        if not marked:
            return {"slug": slug, "name": name, "status": "not_found"}
        link = self._page.locator('[data-mivia-withdraw="1"]').first
        await link.scroll_into_view_if_needed()
        await self._session.delay(random.uniform(0.8, 1.6))
        await link.click()
        await self._session.delay(random.uniform(1.5, 2.5))
        dialog = self._page.locator('[role="dialog"], [role="alertdialog"]').first
        if await dialog.count():
            confirm = (
                dialog.locator("button")
                .filter(has_text=re.compile(r"^\s*(Zurückziehen|Withdraw)\s*$"))
                .first
            )
            if await confirm.count():
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
        await editor.click()
        await self._session.delay(random.uniform(0.6, 1.2))
        for index, paragraph in enumerate(text.split("\n")):
            if index:
                await self._page.keyboard.press("Shift+Enter")
            if paragraph:
                await self._page.keyboard.insert_text(paragraph)
        await self._session.delay(random.uniform(0.8, 1.5))
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
        await self._page.locator('[data-mivia-submit="1"]').first.click()
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

    async def _clear(self, editor: Any) -> None:
        await editor.click()
        await self._page.keyboard.press("Control+A")
        await self._page.keyboard.press("Delete")
