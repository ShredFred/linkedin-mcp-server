"""MiViA fork: read-only network views (connections, sent invitations, event attendees).

Kept in its own module so the fork merges upstream with as few touched hunks as
possible. Everything here reads rendered pages; nothing clicks an action.

Card extraction is structural: a card is a ``[role="listitem"]`` holding a
profile link, or -- where LinkedIn renders no list roles (the connections page,
measured 2026-09-29) -- the largest ancestor of a profile link that contains no
other profile. Action state is classified from locale-independent signals first
(``componentkey`` suffix ``_pending``, ``custom-invite`` and ``/messaging/compose``
hrefs); only the remaining states fall back to the explicit per-locale table
below, which is the documented exception the upstream scraping rules allow.
"""

from __future__ import annotations

import logging
import random
import re
from datetime import date
from typing import Any
from urllib.parse import quote

from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

logger = logging.getLogger(__name__)

CONNECTIONS_URL = "https://www.linkedin.com/mynetwork/invite-connect/connections/"
SENT_INVITATIONS_URL = "https://www.linkedin.com/mynetwork/invitation-manager/sent/"

_EVENT_ID_RE = re.compile(r"^\d{10,25}$")

# Attendee total as the event page renders it ("X und 41 weitere Personen",
# "42 Personen nehmen teil"). One page view, no search; the harvest compares it
# with the last read before spending search pages.
EVENT_COUNT_JS = r"""() => {
  const t = (document.querySelector('main') || document.body).innerText || '';
  let m = /und\s+(\d[\d.]*)\s+weitere\s+(?:Person|Kontakt)/i.exec(t);
  if (m) return parseInt(m[1].replace(/\./g, ''), 10) + 1;
  m = /and\s+([\d,]+)\s+other/i.exec(t);
  if (m) return parseInt(m[1].replace(/,/g, ''), 10) + 1;
  m = /([\d.,]+)\s+(Personen nehmen teil|attendees)/i.exec(t);
  return m ? parseInt(m[1].replace(/[.,]/g, ''), 10) : null;
}"""

# Human pace. Reading is not what LinkedIn restricts, but a burst of page loads
# is what a detector sees first.
# LinkedIn's commercial use limit for people searches (monthly, resets on the
# 1st at midnight PST; help article a524372). When it is reached, the results
# are hidden -- which looks exactly like an empty last page. Wording as used
# on the German and English UI; matched on the page text only when no card
# came back.
_SEARCH_LIMIT_RE = re.compile(
    r"commercial use limit|monthly limit for (?:profile )?search|reached the (?:monthly )?limit"
    r"|kommerzielle[nr]? Nutzung|monatliche[ns]? (?:Such)?[Ll]imit|Limit für (?:Profil)?[Ss]uchen",
    re.IGNORECASE,
)


class SearchLimitReached(RuntimeError):
    """LinkedIn hides people-search results for the rest of the month."""


def is_search_limit_text(text: str | None) -> bool:
    return bool(_SEARCH_LIMIT_RE.search(text or ""))


_SCROLL_PAUSE = (1.8, 3.2)
_PAGE_PAUSE = (3.0, 6.0)

_CARDS_JS = r"""() => {
  const main = document.querySelector('main') || document.body;
  const slugOf = h => {
    try {
      const u = new URL(h, location.href);
      const m = /^\/in\/([^/]+)/.exec(u.pathname);
      return m ? decodeURIComponent(m[1]) : null;
    } catch { return null; }
  };
  const slugsIn = el => new Set(
    [...el.querySelectorAll('a[href*="/in/"]')]
      .map(a => slugOf(a.getAttribute('href'))).filter(Boolean)
  );
  let items = [...main.querySelectorAll('[role="listitem"]')]
    .filter(it => it.querySelector('a[href*="/in/"]'));
  if (!items.length) {
    const seen = new Set();
    for (const a of main.querySelectorAll('a[href*="/in/"]')) {
      const slug = slugOf(a.getAttribute('href'));
      if (!slug || seen.has(slug)) continue;
      seen.add(slug);
      let card = a;
      while (
        card.parentElement && card.parentElement !== main &&
        slugsIn(card.parentElement).size === 1
      ) card = card.parentElement;
      items.push(card);
    }
  }
  const urnOf = it => {
    for (const el of it.querySelectorAll('[href*="profileUrn="], [id^="SearchResults"]')) {
      const href = el.getAttribute('href');
      if (href) {
        try {
          const v = new URL(href, location.href).searchParams.get('profileUrn');
          if (v) return v;
        } catch {}
      }
      const m = /^SearchResults(ACoA[A-Za-z0-9_-]+)$/.exec(el.id || '');
      if (m) return 'urn:li:fsd_profile:' + m[1];
    }
    return null;
  };
  return items.map(it => {
    const a = it.querySelector('a[href*="/in/"]');
    return {
      slug: slugOf(a.getAttribute('href')),
      profile_urn: urnOf(it),
      lines: (it.innerText || '').split('\n').map(s => s.trim()).filter(Boolean),
      actions: [...it.querySelectorAll('a[href], button, [role="button"]')]
        .filter(e => (e.innerText || '').trim() &&
                     !(e.getAttribute('href') || '').includes('/in/'))
        .map(e => ({
          text: (e.innerText || '').trim(),
          href: e.getAttribute('href') || '',
          key: e.getAttribute('componentkey') || '',
        })),
    };
  });
}"""

# Scroll LinkedIn's own scroll container (``main#workspace`` on the measured
# layout), falling back to the document. Returns the number of profile cards.
_SCROLL_JS = r"""() => {
  const main = document.querySelector('main');
  const target = (main && main.scrollHeight > main.clientHeight + 20)
    ? main : (document.scrollingElement || document.body);
  target.scrollTop = target.scrollHeight;
  window.scrollTo(0, document.body.scrollHeight);
  return true;
}"""

_COUNT_JS = r"""() => {
  const main = document.querySelector('main') || document.body;
  return new Set([...main.querySelectorAll('a[href*="/in/"]')]
    .map(a => a.getAttribute('href').split('?')[0])).size;
}"""

# Search result cards render their action button after the text. A card is
# settled once it holds any non-profile action link or button.
_ACTIONS_SETTLED_JS = r"""() => {
  const main = document.querySelector('main') || document.body;
  const items = [...main.querySelectorAll('[role="listitem"]')]
    .filter(it => it.querySelector('a[href*="/in/"]'));
  if (!items.length) return {items: 0, settled: 0};
  const settled = items.filter(it => [...it.querySelectorAll('a[href], button')]
    .some(e => (e.getAttribute('componentkey') || '').startsWith('ConnectButton') ||
               /custom-invite|messaging\/compose/.test(e.getAttribute('href') || '') ||
               (e.tagName === 'BUTTON' && (e.innerText || '').trim()))).length;
  return {items: items.length, settled};
}"""

# --- locale tables (documented exception to locale independence) -----------

_MONTHS = {
    # de
    "januar": 1,
    "februar": 2,
    "märz": 3,
    "maerz": 3,
    "april": 4,
    "mai": 5,
    "juni": 6,
    "juli": 7,
    "august": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "dezember": 12,
    # en
    "january": 1,
    "february": 2,
    "march": 3,
    "may": 5,
    "june": 6,
    "july": 7,
    "october": 10,
    "december": 12,
}
_CONNECTED_DE = re.compile(r"^Am (\d{1,2})\. (\w+) (\d{4}) vernetzt$", re.UNICODE)
_CONNECTED_EN = re.compile(r"^Connected on (\w+) (\d{1,2}), (\d{4})$", re.UNICODE)

_ACTION_TEXT = {
    "message": {"nachricht", "message"},
    "connect": {"vernetzen", "connect"},
    "pending": {"ausstehend", "pending"},
    "follow": {"folgen", "follow"},
}

_DEGREE_RE = re.compile(r"•\s*(\d)")


def parse_connected_date(line: str) -> date | None:
    """Parse "Am 28. September 2026 vernetzt" / "Connected on September 28, 2026"."""
    match = _CONNECTED_DE.match(line.strip())
    if match:
        day, month_name, year = match.groups()
    else:
        match = _CONNECTED_EN.match(line.strip())
        if not match:
            return None
        month_name, day, year = match.groups()
    month = _MONTHS.get(month_name.lower())
    if month is None:
        return None
    try:
        return date(int(year), month, int(day))
    except ValueError:
        return None


def classify_action(actions: list[dict[str, str]]) -> str:
    """Return message/connect/pending/follow/unknown for one card's actions."""
    for action in actions:
        if action.get("key", "").endswith("_pending"):
            return "pending"
        href = action.get("href", "")
        if "custom-invite" in href:
            return "connect"
        if "/messaging/compose" in href:
            return "message"
    for action in actions:
        text = action.get("text", "").strip().lower()
        for state, words in _ACTION_TEXT.items():
            if text in words:
                return state
    return "unknown"


def parse_degree(first_line: str) -> int | None:
    match = _DEGREE_RE.search(first_line)
    return int(match.group(1)) if match else None


def _clean_name(first_line: str) -> str:
    return first_line.split("•")[0].strip()


_SELF_MARKERS = {"sie", "you"}


def split_person_lines(lines: list[str]) -> dict[str, Any]:
    """Split a search card's lines into name, degree and the remaining text.

    LinkedIn renders the degree either on the name line ("Ben Kahle • 2.") or,
    for 1st-degree and self cards, as its own line ("• 1.", "• Sie").
    """
    if not lines:
        return {"name": None, "degree": None, "is_self": False, "rest": []}
    name_line, rest = lines[0], list(lines[1:])
    marker = name_line.partition("•")[2].strip()
    if not marker and rest and rest[0].startswith("•"):
        marker = rest.pop(0).lstrip("•").strip()
    degree = parse_degree("• " + marker) if marker else None
    return {
        "name": _clean_name(name_line),
        "degree": degree,
        "is_self": marker.lower() in _SELF_MARKERS,
        "rest": rest,
    }


def _profile_url(slug: str) -> str:
    return f"https://www.linkedin.com/in/{quote(slug, safe='-_.')}/"



ATTENDEE_FIELDS_MINIMAL = ("slug", "name", "headline", "degree")
ATTENDEE_FIELDS_CARD = ATTENDEE_FIELDS_MINIMAL + ("location", "action", "page")


def project_attendees(
    result: dict[str, Any], limit: int | None = None, fields: str = "minimal"
) -> dict[str, Any]:
    """Reduce an attendee read to what one search card shows -- no profile calls.

    ``minimal`` keeps slug, name, headline and degree; ``card`` adds location,
    button state and page (still the same card). The account itself is dropped.
    ``readable`` is false when the first page shows no card at all: LinkedIn
    lists attendees only to an account that has RSVP'd, and an empty first page
    is indistinguishable from that, so it is reported instead of read as "0".
    """
    keep = ATTENDEE_FIELDS_CARD if fields == "card" else ATTENDEE_FIELDS_MINIMAL
    people = [a for a in result.get("attendees") or [] if a.get("action") != "self"]
    cut = limit is not None and len(people) > limit
    if limit is not None:
        people = people[:limit]
    out = {
        k: result[k]
        for k in ("event_id", "start_page", "pages_read", "next_page", "complete")
        if k in result
    }
    if cut:
        out["complete"] = False
    first_empty = result.get("start_page") == 1 and not result.get("attendees")
    out.update(
        readable=not (first_empty and result.get("pages_read", 0) >= 1),
        fields=fields if fields == "card" else "minimal",
        count=len(people),
        attendees=[{k: a.get(k) for k in keep} for a in people],
    )
    if not out["readable"]:
        out["reason"] = "empty_first_page_rsvp_likely_required"
    if result.get("warnings"):
        out["warnings"] = result["warnings"]
    return out

def event_attendees_url(event_id: str, page: int) -> str:
    if not _EVENT_ID_RE.match(event_id):
        raise ValueError("event_id must be the numeric LinkedIn event id")
    base = (
        "https://www.linkedin.com/search/results/people/"
        f"?eventAttending=%5B%22{event_id}%22%5D&origin=EVENT_PAGE_CANONICAL"
    )
    return base if page <= 1 else f"{base}&page={page}"


class MiviaNetworkReader:
    """Read-only walks over network pages for the MiViA fork tools."""

    def __init__(self, session: ScrapingSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator

    @property
    def _page(self) -> Any:
        return self._session.page

    async def _pause(self, bounds: tuple[float, float]) -> None:
        await self._session.delay(random.uniform(*bounds))

    async def _cards(self) -> list[dict[str, Any]]:
        cards = await self._page.evaluate(_CARDS_JS)
        return [card for card in cards or [] if card.get("slug")]

    async def _wait_for_cards(self, timeout: float = 12.0) -> None:
        deadline = self._session.monotonic() + timeout
        while self._session.monotonic() < deadline:
            if await self._page.evaluate(_COUNT_JS):
                return
            await self._session.delay(0.5)

    async def _scroll_until(
        self, *, limit: int, stop: Any = None, max_rounds: int = 200
    ) -> list[dict[str, Any]]:
        """Scroll the list until it stops growing, reaches *limit*, or *stop* says so."""
        stale = 0
        previous = -1
        cards: list[dict[str, Any]] = []
        for _ in range(max_rounds):
            cards = await self._cards()
            if len(cards) >= limit or (stop is not None and stop(cards)):
                break
            if len(cards) == previous:
                stale += 1
                if stale >= 3:
                    break
            else:
                stale = 0
            previous = len(cards)
            await self._page.evaluate(_SCROLL_JS)
            await self._pause(_SCROLL_PAUSE)
        return cards

    async def list_connections(self, since: date | None, limit: int) -> dict[str, Any]:
        await self._navigator._navigate_to_page(CONNECTIONS_URL)
        await self._session.check_rate_limit()
        await self._wait_for_cards()

        def older_than_since(cards: list[dict[str, Any]]) -> bool:
            if since is None or not cards:
                return False
            for line in reversed(cards[-1]["lines"]):
                connected = parse_connected_date(line)
                if connected is not None:
                    return connected < since
            return False

        cards = await self._scroll_until(limit=limit, stop=older_than_since)
        connections = []
        undated = 0
        for card in cards:
            connected = next(
                (d for d in map(parse_connected_date, card["lines"]) if d), None
            )
            if connected is None:
                undated += 1
            if since is not None and connected is not None and connected < since:
                continue
            lines = card["lines"]
            connections.append(
                {
                    "name": _clean_name(lines[0]) if lines else None,
                    "slug": card["slug"],
                    "profile_url": _profile_url(card["slug"]),
                    "profile_urn": card.get("profile_urn"),
                    "headline": lines[1] if len(lines) > 1 else None,
                    "connected_on": connected.isoformat() if connected else None,
                }
            )
            if len(connections) >= limit:
                break
        result: dict[str, Any] = {
            "url": CONNECTIONS_URL,
            "sort": "recently_added",
            "since": since.isoformat() if since else None,
            "count": len(connections),
            "connections": connections,
        }
        if undated:
            result["warnings"] = [
                f"{undated} card(s) without a parseable connection date"
            ]
        return result

    async def list_sent_invitations(self, limit: int) -> dict[str, Any]:
        await self._navigator._navigate_to_page(SENT_INVITATIONS_URL)
        await self._session.check_rate_limit()
        await self._wait_for_cards()
        cards = await self._scroll_until(limit=limit)
        invitations = []
        for card in cards[:limit]:
            lines = card["lines"]
            invitations.append(
                {
                    "name": _clean_name(lines[0]) if lines else None,
                    "slug": card["slug"],
                    "profile_url": _profile_url(card["slug"]),
                    "headline": lines[1] if len(lines) > 2 else None,
                    # Relative text as rendered ("Vor 18 Stunden gesendet"),
                    # deliberately not converted: LinkedIn rounds it.
                    "sent_text": lines[-2] if len(lines) >= 3 else None,
                }
            )
        header_total = await self._page.evaluate(
            r"""() => { const m = /\((\d[\d.,]*)\)/.exec((document.querySelector('main')||document.body).innerText.slice(0, 400)); return m ? m[1] : null; }"""
        )
        result: dict[str, Any] = {
            "url": SENT_INVITATIONS_URL,
            "count": len(invitations),
            "header_total": header_total,
            "invitations": invitations,
        }
        if header_total and header_total.replace(".", "").replace(",", "").isdigit():
            total = int(header_total.replace(".", "").replace(",", ""))
            result["complete"] = len(invitations) >= min(total, limit)
        return result

    async def _wait_for_actions(self, timeout: float = 10.0) -> dict[str, int]:
        deadline = self._session.monotonic() + timeout
        state = {"items": 0, "settled": 0}
        while self._session.monotonic() < deadline:
            state = await self._page.evaluate(_ACTIONS_SETTLED_JS)
            if state["items"] and state["settled"] >= state["items"]:
                break
            await self._session.delay(0.5)
        return state

    async def event_attendee_count(self, event_id: str) -> dict[str, Any]:
        """Read the attendee total from the event page itself (read-only)."""
        if not _EVENT_ID_RE.match(event_id):
            raise ValueError("event_id must be the numeric LinkedIn event id")
        await self._navigator._navigate_to_page(f"https://www.linkedin.com/events/{event_id}/")
        await self._session.check_rate_limit()
        # The attendee line renders late: right after navigation it was missing on
        # both events read on 30.09.2026 and present six seconds later.
        count = None
        deadline = self._session.monotonic() + 12.0
        while True:
            count = await self._page.evaluate(EVENT_COUNT_JS)
            if count is not None or self._session.monotonic() >= deadline:
                break
            await self._session.delay(1.0)
        return {"event_id": event_id, "attendee_count": count}

    async def get_event_attendees(
        self, event_id: str, start_page: int, max_pages: int
    ) -> dict[str, Any]:
        attendees: list[dict[str, Any]] = []
        seen: set[str] = set()
        warnings: list[str] = []
        page = start_page
        exhausted = False
        pages_read = 0
        for page in range(start_page, start_page + max_pages):
            if pages_read:
                await self._pause(_PAGE_PAUSE)
            await self._navigator._navigate_to_page(event_attendees_url(event_id, page))
            await self._session.check_rate_limit()
            await self._wait_for_cards()
            await self._wait_for_actions()
            cards = await self._cards()
            pages_read += 1
            if not cards:
                body = await self._page.evaluate(
                    "() => (document.querySelector('main') || document.body).innerText.slice(0, 4000)"
                )
                if is_search_limit_text(body):
                    raise SearchLimitReached(
                        "LinkedIn commercial use limit for people searches reached"
                    )
                exhausted = True
                break
            new = 0
            for card in cards:
                if card["slug"] in seen:
                    continue
                seen.add(card["slug"])
                new += 1
                person = split_person_lines(card["lines"])
                rest = person["rest"]
                attendees.append(
                    {
                        "name": person["name"],
                        "slug": card["slug"],
                        "profile_url": _profile_url(card["slug"]),
                        "profile_urn": card.get("profile_urn"),
                        "degree": 0 if person["is_self"] else person["degree"],
                        "headline": rest[0] if rest else None,
                        "location": rest[1] if len(rest) > 1 else None,
                        "action": "self"
                        if person["is_self"]
                        else classify_action(card["actions"]),
                        "page": page,
                    }
                )
            unknown = sum(
                1 for a in attendees if a["page"] == page and a["action"] == "unknown"
            )
            if unknown:
                warnings.append(
                    f"page {page}: {unknown} card(s) without a recognised action"
                )
            if new == 0 or len(cards) < 10:
                exhausted = True
                break
        return {
            "event_id": event_id,
            "start_page": start_page,
            "pages_read": pages_read,
            "next_page": None if exhausted else page + 1,
            "complete": exhausted,
            "count": len(attendees),
            "attendees": attendees,
            **({"warnings": warnings} if warnings else {}),
        }
