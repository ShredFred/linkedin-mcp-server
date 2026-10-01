"""MiViA fork: find LinkedIn events -- by keyword and by organiser page.

Measured 2026-09-29 (headless, de locale):

* ``/search/results/events/?keywords=<k>`` lists upcoming events. Cards are not
  reliably ``li``; each carries one link ``/events/<id>/``. Lines: title, date,
  "<place> • Von <organiser>", description, attendee line ("1 Teilnehmer:in").
* ``/company/<slug>/events/?viewAsMember=true`` lists "Anstehende Events" and
  "Vergangene Events" with links ``/events/<title-slug><19-digit id>/`` and an
  attendee line ("Matthias Steinbacher und 307 weitere Personen nehmen teil").
  Without viewAsMember an admin lands on the admin dashboard.

The event id (trailing 16-22 digits) is the key. Reads only; nothing is clicked.
"""

from __future__ import annotations

import random
import re
from typing import Any
from urllib.parse import quote

from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

# LinkedIn event ids are 19 digits. A title slug may end in a year
# ("h-rtereikongress2025" + id), so take the LAST 19 digits, not the longest run.
_EVENT_ID_RE = re.compile(r"/events/[^/]*?(\d{19})/?(?:[?#].*)?$")

# Cards: the largest ancestor of an event link that holds no other event id.
_EVENT_CARDS_JS = r"""() => {
  const main = document.querySelector('main') || document.body;
  const idOf = h => { const m = /\/events\/[^/]*?(\d{19})\/?(?:[?#].*)?$/.exec(h || ''); return m ? m[1] : null; };
  const idsIn = el => new Set([...el.querySelectorAll('a[href*="/events/"]')]
    .map(a => idOf(a.getAttribute('href'))).filter(Boolean));
  const text = (main.innerText || '');
  const pastAt = text.search(/Vergangene Events|Past events/i);
  const out = []; const seen = new Set();
  for (const a of main.querySelectorAll('a[href*="/events/"]')) {
    const id = idOf(a.getAttribute('href'));
    if (!id || seen.has(id)) continue;
    seen.add(id);
    let card = a;
    while (card.parentElement && card.parentElement !== main && idsIn(card.parentElement).size === 1) card = card.parentElement;
    // Position of the card text in the page text tells upcoming from past.
    const t = (card.innerText || '').trim();
    const pos = t ? text.indexOf(t.split('\n')[0]) : -1;
    out.push({id, lines: t.split('\n').map(s => s.trim()).filter(Boolean).slice(0, 12),
              past: pastAt >= 0 && pos > pastAt});
  }
  return {items: out, empty: /Keine Ergebnisse|No results/i.test(text.slice(0, 600))};
}"""

# A number must start with a digit: "([\d.,]+)" also matched a lone "." in
# "… Stahl. Teilnehmer" (live, 29.09.2026) and crashed the parse.
_ATTEND_RES = [
    (re.compile(r"und\s+(\d[\d.]*)\s+weitere\s+(?:Person|Kontakt)", re.I), 1),
    (re.compile(r"and\s+(\d[\d,]*)\s+other", re.I), 1),
    (
        re.compile(r"(\d[\d.,]*)\s+(?:Teilnehmer|attendee|Personen nehmen teil)", re.I),
        0,
    ),
]
_DATE_RE = re.compile(
    r"^(?:(?:Mo|Di|Mi|Do|Fr|Sa|So)\.?,?\s|(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),?\s|Heute|Morgen|Today|Tomorrow)",
    re.I,
)


def event_id(href: str) -> str | None:
    m = _EVENT_ID_RE.search(href or "")
    return m.group(1) if m else None


def attendee_count(lines: list[str]) -> int | None:
    for line in lines:
        for rx, plus in _ATTEND_RES:
            m = rx.search(line)
            if m:
                return int(re.sub(r"[.,]", "", m.group(1))) + plus
    return None


_SECTION_RE = re.compile(
    r"^(Anstehende Events|Vergangene Events|Upcoming events|Past events|Events)$", re.I
)


# ... and on a single-hit search the result counter ("1 Ergebnis", live 29.09.2026).
_RESULTS_RE = re.compile(
    r"^(?:Etwa\s+|About\s+)?\d[\d.,]*\s+(?:Ergebnis(?:se)?|results?)$", re.I
)


# Place text -> online flag and ISO-2 country. Only explicit evidence counts:
# a trailing two-letter code (LinkedIn renders "Ort, Stadt, DE") or a country
# name as the last comma segment. A city alone is never mapped -- no guessing.
_ONLINE_RE = re.compile(
    r"^(?:online(?:[- ]?(?:event|veranstaltung))?|virtuell|virtual)$", re.I
)
_COUNTRY_NAMES = {
    "deutschland": "DE", "germany": "DE", "österreich": "AT", "austria": "AT",
    "schweiz": "CH", "switzerland": "CH", "suisse": "CH", "frankreich": "FR",
    "france": "FR", "italien": "IT", "italy": "IT", "italia": "IT",
    "spanien": "ES", "spain": "ES", "españa": "ES", "niederlande": "NL",
    "netherlands": "NL", "belgien": "BE", "belgium": "BE", "polen": "PL",
    "poland": "PL", "tschechien": "CZ", "czechia": "CZ", "czech republic": "CZ",
    "schweden": "SE", "sweden": "SE", "dänemark": "DK", "denmark": "DK",
    "vereinigtes königreich": "GB", "united kingdom": "GB", "großbritannien": "GB",
    "usa": "US", "vereinigte staaten": "US", "united states": "US",
}  # fmt: skip
_ISO2_RE = re.compile(r"^[A-Z]{2}$")


def place_facts(place: str | None) -> dict[str, Any]:
    """``is_online`` and ``country`` (ISO-2) from a raw place text; unknown -> None."""
    if not place or not place.strip():
        return {"is_online": None, "country": None}
    p = place.strip()
    if _ONLINE_RE.match(p):
        return {"is_online": True, "country": None}
    last = p.rsplit(",", 1)[-1].strip()
    if "," in p and _ISO2_RE.match(last):
        country = "GB" if last == "UK" else last
    else:
        country = _COUNTRY_NAMES.get(last.lower())
    return {"is_online": False, "country": country}


def parse_event_card(lines: list[str]) -> dict[str, Any]:
    """Title, date, place, organiser, description and attendees from card lines."""
    # A card walk can swallow the section heading above the first card.
    lines = [
        ln for ln in lines if not _SECTION_RE.match(ln) and not _RESULTS_RE.match(ln)
    ]
    title = lines[0] if lines else None
    date_line = next((ln for ln in lines[1:4] if _DATE_RE.match(ln)), None)
    place, organiser = None, None
    for ln in lines[1:6]:
        m = re.search(r"(?:^|•\s*)(?:Von|By)\s+(.+)$", ln)
        if m:
            organiser = m.group(1).strip()
            place = ln[: m.start()].rstrip(" •") or None
            break
    if place is None:
        # Organiser pages may render the place alone ("Online").
        place = next((ln for ln in lines[1:6] if _ONLINE_RE.match(ln)), None)
    skip = {title, date_line}
    desc = next(
        (
            ln
            for ln in lines[1:]
            if ln not in skip
            and len(ln) > 40
            and "•" not in ln
            and attendee_count([ln]) is None
        ),
        None,
    )
    return {
        "title": title,
        "date_text": date_line,
        "place": place,
        **place_facts(place),
        "organiser": organiser,
        "description": (desc or "")[:300] or None,
        "attendees": attendee_count(lines),
    }


def keyword_url(keyword: str, page: int = 1) -> str:
    base = f"https://www.linkedin.com/search/results/events/?keywords={quote(keyword)}"
    return base if page <= 1 else f"{base}&page={page}"


def organiser_url(slug: str) -> str:
    return f"https://www.linkedin.com/company/{slug}/events/?viewAsMember=true"


# Followers of an administered page (measured 2026-09-29 on MiViA, 1.143
# followers): /company/<numeric id>/admin/analytics/followers/ -> button "Alle
# Follower:innen anzeigen" opens a dialog, newest first, one
# .org-view-page-followers-modal__follower-list-item per person with an /in/
# link, degree, headline and the month followed; "Weitere Ergebnisse anzeigen"
# loads more.
_FOLLOWERS_JS = r"""() => {
  const d = document.querySelector('[role="dialog"]');
  if (!d) return {dialog: false, items: []};
  const items = [...d.querySelectorAll('.org-view-page-followers-modal__follower-list-item, li')]
    .filter(i => i.querySelector('a[href*="/in/"]'));
  const seen = new Set(); const out = [];
  for (const it of items) {
    const href = it.querySelector('a[href*="/in/"]').getAttribute('href');
    if (seen.has(href)) continue; seen.add(href);
    out.push({href, lines: (it.innerText || '').split('\n').map(s => s.trim()).filter(Boolean).slice(0, 6)});
  }
  return {dialog: true, items: out};
}"""

_MONTHS = {
    "januar": 1,
    "februar": 2,
    "märz": 3,
    "april": 4,
    "mai": 5,
    "juni": 6,
    "juli": 7,
    "august": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "dezember": 12,
    "january": 1,
    "february": 2,
    "march": 3,
    "may": 5,
    "june": 6,
    "july": 7,
    "october": 10,
    "december": 12,
}
_MONTH_RE = re.compile(r"^([A-Za-zäöüÄÖÜ]+)\s+(\d{4})$")


def parse_follower_lines(lines: list[str]) -> dict[str, Any]:
    """'Name', 'Kontakt 2. Grades', '· 2.', headline, 'September 2026'."""
    name = lines[0] if lines else None
    degree = None
    month = None
    rest = []
    for ln in lines[1:]:
        m = re.match(r"^[·•]\s*(\d)", ln)
        if m:
            degree = int(m.group(1))
            continue
        if re.match(r"^(Kontakt\s+\d\.\s+Grades|\d(st|nd|rd|th) degree)", ln, re.I):
            continue
        mm = _MONTH_RE.match(ln)
        if mm and mm.group(1).lower() in _MONTHS:
            month = f"{mm.group(2)}-{_MONTHS[mm.group(1).lower()]:02d}"
            continue
        rest.append(ln)
    return {
        "name": name,
        "degree": degree,
        "headline": rest[0] if rest else None,
        "followed_month": month,
    }


# Employer lookup (AGENTS.md "Company first", exception of 2026-09-29): only the
# CURRENT position is read -- the first experience entry whose date range ends
# in "Heute"/"Present". The top card is not used: it renders the current
# employer and the school identically, and a school that is also a Close lead
# (a university customer) would be a wrong match.
_EXPERIENCE_JS = r"""() => {
  const main = document.querySelector('main') || document.body;
  // New layout (measured 2026-09-29): entries carry
  // componentkey="entity-collection-item-..."; the old list items stay as fallback.
  let items = [...main.querySelectorAll('[componentkey^="entity-collection-item"]')]
    .filter(i => /(Heute|Present|heute)/.test(i.innerText || ''));
  if (!items.length) items = [...main.querySelectorAll('li, [role="listitem"]')]
    .filter(i => /(Heute|Present|heute)/.test(i.innerText || ''));
  // Innermost matching item first: grouped positions nest a role list inside
  // the company item.
  const leaves = items.filter(i => !items.some(o => o !== i && i.contains(o)));
  const pick = leaves[0] || items[0];
  if (!pick) return null;
  const outer = items.find(o => o !== pick && o.contains(pick));
  // Three lines suffice (role, company, range); nothing else of the page is taken.
  const lines = el => (el.innerText || '').split('\n').map(s => s.trim()).filter(Boolean).slice(0, 3);
  return {lines: lines(pick), outer: outer ? lines(outer) : null};
}"""

_EMPLOYMENT_TYPES = re.compile(
    r"\s*·\s*(Vollzeit|Teilzeit|Selbstständig|Freiberuflich|Praktikum|Werkstudent|Werkstudium|Minijob|Befristet|Saisonal|Ausbildung|Duales Studium|"
    r"Full-time|Part-time|Self-employed|Freelance|Internship|Apprenticeship|Contract)\b.*$",
    re.I,
)
_RANGE_RE = re.compile(r"(Heute|Present)", re.I)


def parse_current_position(found: dict[str, Any] | None) -> dict[str, Any] | None:
    """{'lines': [title, 'Company · Vollzeit', 'Jan. 2024 – Heute · …'], 'outer': …}."""
    if not found or not found.get("lines"):
        return None
    lines = found["lines"]
    if found.get("outer") and not any(
        _EMPLOYMENT_TYPES.search(ln) for ln in lines[1:2]
    ):
        # Grouped: the outer item names the company, the inner the role.
        company = found["outer"][0]
        role = lines[0]
    else:
        role = lines[0]
        second = lines[1] if len(lines) > 1 else ""
        if _RANGE_RE.search(second):
            return None  # no company line
        company = _EMPLOYMENT_TYPES.sub("", second).split(" · ")[0].strip()
    if not company or _RANGE_RE.search(company):
        return None
    return {"employer": company[:120], "role": role[:120]}


class MiviaEventFinder:
    def __init__(self, session: ScrapingSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator

    async def _read(self, url: str) -> dict[str, Any]:
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        await self._session.delay(random.uniform(3.0, 5.0))
        landed = str(getattr(self._session.page, "url", ""))
        if "/company/" in url and "/admin/" in landed:
            raise RuntimeError(f"redirected to the admin view ({landed})")
        await self._session.page.evaluate(
            "() => window.scrollTo(0, document.body.scrollHeight)"
        )
        await self._session.delay(random.uniform(1.5, 2.5))
        return await self._session.page.evaluate(_EVENT_CARDS_JS)

    async def by_keyword(
        self, keyword: str, max_pages: int = 1
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            state = await self._read(keyword_url(keyword, page))
            if not state["items"]:
                break
            for it in state["items"]:
                events.append(
                    {
                        "event_id": it["id"],
                        **parse_event_card(it["lines"]),
                        "url": f"https://www.linkedin.com/events/{it['id']}/",
                        "found_by": f"keyword:{keyword}",
                        "past": False,
                    }
                )
            if len(state["items"]) < 10:
                break
            await self._session.delay(random.uniform(3.0, 6.0))
        return events

    async def current_employer(self, profile_url: str) -> dict[str, Any]:
        """Current employer and role of one person, from the experience page only."""
        m = re.search(r"/in/([^/?#]+)", profile_url or "")
        if not m:
            return {"status": "invalid_url"}
        await self._navigator._navigate_to_page(
            f"https://www.linkedin.com/in/{m.group(1)}/details/experience/"
        )
        await self._session.check_rate_limit()
        await self._session.delay(random.uniform(3.0, 5.0))
        found = await self._session.page.evaluate(_EXPERIENCE_JS)
        pos = parse_current_position(found)
        if not pos:
            return {"status": "no_current_position"}
        return {"status": "ok", **pos}

    async def page_followers(
        self, page_id: str, *, known: set[str], limit: int = 100
    ) -> dict[str, Any]:
        """Newest followers of an administered page, until *known* ones appear."""
        await self._navigator._navigate_to_page(
            f"https://www.linkedin.com/company/{page_id}/admin/analytics/followers/"
        )
        await self._session.check_rate_limit()
        await self._session.delay(random.uniform(3.0, 5.0))
        page = self._session.page
        opener = (
            page.locator("button")
            .filter(
                has_text=re.compile(
                    r"^\s*(Alle Follower:innen anzeigen|Show all followers)\s*$"
                )
            )
            .first
        )
        if await opener.count() == 0:
            return {"available": False, "reason": "no_admin_view", "followers": []}
        await opener.click()
        await self._session.delay(random.uniform(2.5, 4.0))
        state = await page.evaluate(_FOLLOWERS_JS)
        rounds = 0
        while state["dialog"] and len(state["items"]) < limit and rounds < 15:
            if known and any(it["href"] in known for it in state["items"]):
                break  # reached followers seen in an earlier run
            more = (
                page.locator('[role="dialog"] button')
                .filter(
                    has_text=re.compile(
                        r"^\s*(Weitere Ergebnisse anzeigen|Show more results)\s*$"
                    )
                )
                .first
            )
            if await more.count() == 0:
                break
            await more.click()
            await self._session.delay(random.uniform(2.0, 3.5))
            state = await page.evaluate(_FOLLOWERS_JS)
            rounds += 1
        followers = []
        for it in state["items"][:limit]:
            slug = re.sub(r"^/in/|/$", "", it["href"].split("?")[0])
            followers.append(
                {
                    **parse_follower_lines(it["lines"]),
                    "slug": slug,
                    "href": it["href"],
                    "profile_url": f"https://www.linkedin.com/in/{slug}/",
                }
            )
        return {"available": state["dialog"], "followers": followers}

    async def by_organiser(
        self, slug: str, include_past: bool = False
    ) -> list[dict[str, Any]]:
        state = await self._read(organiser_url(slug))
        out = []
        for it in state["items"]:
            if it["past"] and not include_past:
                continue
            card = parse_event_card(it["lines"])
            # Organiser pages render date first, then title.
            if card["title"] and _DATE_RE.match(card["title"]) and len(it["lines"]) > 1:
                card = {
                    **parse_event_card(it["lines"][1:]),
                    "date_text": it["lines"][0],
                }
            out.append(
                {
                    "event_id": it["id"],
                    **card,
                    "organiser": card.get("organiser") or slug,
                    "url": f"https://www.linkedin.com/events/{it['id']}/",
                    "found_by": f"organiser:{slug}",
                    "past": it["past"],
                }
            )
        return out


def event_summary(ev: dict[str, Any]) -> dict[str, Any]:
    """Event master data: id, url, title, date, place facts, organiser, attendees.

    ``place`` is the raw card text, ``is_online``/``country`` come from
    ``place_facts`` and stay None when the card does not say.

    The attendee line may start with a person name ("X und 307 weitere ...");
    only the count leaves the finder, never the line itself.
    """
    n = ev.get("attendees")
    return {
        "event_id": ev.get("event_id"),
        "url": ev.get("url"),
        "title": ev.get("title"),
        "date_text": ev.get("date_text"),
        "place": ev.get("place"),
        "is_online": ev.get("is_online"),
        "country": ev.get("country"),
        "organiser": ev.get("organiser"),
        "attendees": n,
        "attendees_text": None if n is None else f"{n} Teilnehmende",
        "past": bool(ev.get("past")),
    }
