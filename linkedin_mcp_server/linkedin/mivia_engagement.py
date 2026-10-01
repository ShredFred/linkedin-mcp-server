"""MiViA fork: who engaged with a post, and how a post performed.

Measured 2026-09-29 on the rendered pages (headless, de locale):

* ``/analytics/post/<activity urn>/?resultType=REACTIONS`` renders "Personen, die
  reagiert haben" as ``li`` cards for posts of the account *and* of pages it
  administers; scrolling loads the rest ("Weitere Ergebnisse anzeigen" beyond).
  The reaction kind is an icon token (``like-consumption-ring-medium``).
  For anyone else's post the page renders only "Fehler beim Laden".
* On a post's own page the reaction count sits *inside* the member's reaction
  toggle. Clicking anywhere in that container reacts or un-reacts -- measured the
  hard way: a container click removed a like, which was restored at once. This
  module therefore never clicks on a post page; for foreign posts it reports the
  count and the commenters, not the reactors.
* Comments carry ``componentkey="replaceableComment_urn:li:comment:(activity:A,C)"``;
  a reply is a comment card nested inside another one.
* ``/analytics/post-summary/<urn>/`` renders impressions, reach and engagement
  counts for the account's own posts.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession

logger = logging.getLogger(__name__)

SEEN_ENV = "MIVIA_LINKEDIN_ENGAGERS_SEEN"

_ACTIVITY_RE = re.compile(r"(?:activity[:-]|urn:li:activity:)(\d{16,22})")
_SCROLL_PAUSE = (1.8, 3.2)

# Icon token prefix -> reaction kind. Structural first; the text table below is
# the documented per-locale fallback.
REACTION_ICONS = {
    "like": "like",
    "praise": "celebrate",
    "celebrate": "celebrate",
    "empathy": "love",
    "love": "love",
    "interest": "insightful",
    "insight": "insightful",
    "appreciation": "support",
    "support": "support",
    "entertainment": "funny",
    "funny": "funny",
}
REACTION_TEXT = {
    "gefällt mir": "like",
    "like": "like",
    "applaus": "celebrate",
    "celebrate": "celebrate",
    "unterstützung": "support",
    "support": "support",
    "liebe": "love",
    "love": "love",
    "wissenswert": "insightful",
    "insightful": "insightful",
    "lustig": "funny",
    "funny": "funny",
}

_REACTORS_JS = r"""() => {
  const main = document.querySelector('main') || document.body;
  const text = main.innerText || '';
  const failed = /Fehler beim Laden|couldn.t be loaded|could not be loaded/i.test(text.slice(0, 400));
  const items = [...main.querySelectorAll('li')]
    .filter(li => li.querySelector('a[href*="/in/"], a[href*="/company/"]'));
  return {
    failed,
    items: items.map(li => {
      const a = li.querySelector('a[href*="/in/"], a[href*="/company/"]');
      return {
        href: a.getAttribute('href') || '',
        lines: (li.innerText || '').split('\n').map(s => s.trim()).filter(Boolean),
        icons: [...li.querySelectorAll('img, svg')]
          .map(x => x.getAttribute('data-test-icon') || x.getAttribute('alt') || '')
          .filter(Boolean),
      };
    }),
    more: [...main.querySelectorAll('button')]
      .some(b => /Weitere Ergebnisse|Show more results/i.test(b.innerText || '')),
  };
}"""

_SCROLL_ALL_JS = r"""() => {
  for (const e of document.querySelectorAll('main, main *')) {
    if (e.scrollHeight > e.clientHeight + 50 &&
        /auto|scroll/.test(getComputedStyle(e).overflowY)) e.scrollTop = e.scrollHeight;
  }
  window.scrollTo(0, document.body.scrollHeight);
  return true;
}"""

_COMMENTS_JS = r"""() => {
  const cards = [...document.querySelectorAll('[componentkey^="replaceableComment_urn:li:comment:"]')];
  return cards.map(card => {
    const key = card.getAttribute('componentkey');
    const parent = card.parentElement && card.parentElement.closest('[componentkey^="replaceableComment_urn:li:comment:"]');
    const a = card.querySelector('a[href*="/in/"], a[href*="/company/"]');
    return {
      key,
      parent: parent ? parent.getAttribute('componentkey') : null,
      href: a ? a.getAttribute('href') : '',
      lines: (card.innerText || '').split('\n').map(s => s.trim()).filter(Boolean),
    };
  });
}"""

_REACTION_COUNT_JS = r"""() => {
  const card = document.querySelector('[componentkey^="update-card-focus"]') || document.querySelector('main');
  if (!card) return null;
  const b = card.querySelector('button[aria-label^="Status des Reaktionsbuttons"], button[aria-label*="React"]');
  return b ? (b.innerText || '').trim() : null;
}"""

_SUMMARY_JS = r"""() => (document.querySelector('main') || document.body).innerText"""

# Labels on the post-summary page (de/en) -> key. The number precedes (reach
# block) or follows (engagement block) its label; both layouts were measured.
_SUMMARY_BEFORE = {
    "Impressions": "impressions",  # measured: the de page says "Impressions"
    "Impressionen": "impressions",
    "Erreichte Mitglieder": "members_reached",
    "Members reached": "members_reached",
    "Mit diesem Beitrag generierte Profilansichten": "profile_views",
    "Profile viewers from this post": "profile_views",
    "Mit diesem Beitrag gewonnene Follower:innen": "followers_gained",
    "Followers gained from this post": "followers_gained",
    "Soziale Interaktionen": "social_engagements",
    "Social engagements": "social_engagements",
}
_SUMMARY_AFTER = {
    "Reaktionen": "reactions",
    "Reactions": "reactions",
    "Kommentare": "comments",
    "Comments": "comments",
    "Reposts": "reposts",
    "Gespeicherte Beiträge": "saves",
    "Saves": "saves",
    "Auf LinkedIn gesendet": "sends",
    "Sends on LinkedIn": "sends",
}


def parse_activity_id(post: str) -> str:
    """Accept a post URL, an activity URN or the bare id; return the numeric id."""
    post = post.strip()
    if re.fullmatch(r"\d{16,22}", post):
        return post
    match = _ACTIVITY_RE.search(post)
    if not match:
        raise ValueError(
            "post_url must contain an activity id (urn:li:activity:<id> or .../posts/...-activity-<id>-...)"
        )
    return match.group(1)


def reaction_kind(icons: list[str], lines: list[str]) -> str:
    for icon in icons:
        token = icon.lower()
        for prefix, kind in REACTION_ICONS.items():
            if token.startswith(prefix + "-") or token == prefix:
                return kind
    for line in reversed(lines):
        low = line.lower()
        for word, kind in REACTION_TEXT.items():
            if word in low and ("reagiert" in low or "react" in low):
                return kind
    return "unknown"


def _number(text: str) -> int | None:
    digits = re.sub(r"[.,\s ]", "", text.strip().rstrip("%"))
    return int(digits) if digits.isdigit() else None


def parse_person_ref(href: str) -> dict[str, Any]:
    """Split a card link into kind (member/company) and identifier."""
    path = re.sub(r"^https?://[^/]+", "", href or "").split("?")[0]
    member = re.match(r"^/in/([^/]+)", path)
    if member:
        ident = member.group(1)
        return {
            "kind": "member",
            "id": ident,
            # Analytics lists link by member id, search and comments by vanity.
            "id_type": "member_id" if ident.startswith("ACoA") else "vanity",
            "profile_url": f"https://www.linkedin.com/in/{ident}/",
        }
    company = re.match(r"^/company/([^/]+)", path)
    if company:
        return {
            "kind": "company",
            "id": company.group(1),
            "id_type": "company",
            "profile_url": f"https://www.linkedin.com/company/{company.group(1)}/",
        }
    return {"kind": "unknown", "id": None, "id_type": None, "profile_url": None}


def split_engager_lines(lines: list[str]) -> dict[str, Any]:
    """Name, degree and headline from '<Name>', '· 1.', '<Headline>', '<Reaction>'."""
    if not lines:
        return {"name": None, "degree": None, "headline": None}
    name = lines[0].split("•")[0].split("·")[0].strip()
    degree = None
    rest = lines[1:]
    if rest and re.match(r"^[·•]\s*\d", rest[0]):
        degree = int(re.search(r"\d", rest[0]).group(0))
        rest = rest[1:]
    headline = next(
        (
            line
            for line in rest
            if "reagiert" not in line.lower() and "react" not in line.lower()
        ),
        None,
    )
    return {"name": name, "degree": degree, "headline": headline}


_REL_TIME_RE = re.compile(
    r"^\d+\s*(Min\.|Std\.|Tag\(e\)|Woche\(n\)|Monat\(e\)|Jahr\(e\)|m|h|d|w|mo|yr)$"
)


def split_comment_lines(lines: list[str]) -> dict[str, Any]:
    """Comment card: name (repeated), '• 1.', headline, relative time, text..., actions."""
    if not lines:
        return {
            "name": None,
            "degree": None,
            "headline": None,
            "age": None,
            "text": None,
        }
    # The first line is an accessibility label ("X Verifiziert Profil 1."); the
    # visible name follows it.
    name = lines[1] if len(lines) > 1 else lines[0]
    rest = lines[2:]
    degree = None
    if rest and re.match(r"^[·•]\s*\d", rest[0]):
        degree = int(re.search(r"\d", rest[0]).group(0))
        rest = rest[1:]
    age_index = next((i for i, ln in enumerate(rest) if _REL_TIME_RE.match(ln)), None)
    headline = rest[0] if rest and age_index not in (None, 0) else None
    body = rest[age_index + 1 :] if age_index is not None else rest[1:]
    stop = {
        "gefällt mir",
        "like",
        "antworten",
        "reply",
    }
    text_lines = []
    for line in body:
        if line.lower() in stop or line.lower().startswith(
            "status des reaktionsbuttons"
        ):
            break
        text_lines.append(line)
    return {
        "name": name.split("•")[0].strip(),
        "degree": degree,
        "headline": headline,
        "age": rest[age_index] if age_index is not None else None,
        "text": "\n".join(text_lines) or None,
    }


def parse_post_summary(text: str) -> dict[str, Any]:
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    out: dict[str, Any] = {}
    for i, line in enumerate(lines):
        if line in _SUMMARY_BEFORE and i > 0:
            value = _number(lines[i - 1])
            if value is not None:
                out.setdefault(_SUMMARY_BEFORE[line], value)
        if line in _SUMMARY_AFTER and i + 1 < len(lines):
            value = _number(lines[i + 1])
            if value is not None:
                out.setdefault(_SUMMARY_AFTER[line], value)
    for label, key in (
        ("Im Netzwerk", "in_network_pct"),
        ("Außerhalb des Netzwerks", "outside_network_pct"),
    ):
        for i, line in enumerate(lines):
            if (
                line.startswith(label)
                and i + 1 < len(lines)
                and lines[i + 1].endswith("%")
            ):
                out[key] = _percent(lines[i + 1])
    return out


def _percent(text: str) -> float | int | None:
    """'76 %' -> 76, '12,5 %' -> 12.5 (a decimal comma, not a thousands mark)."""
    raw = text.strip().rstrip("%").strip().replace(" ", "").replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        return None
    return int(value) if value.is_integer() else value


# --- "only new since last run" memory ---------------------------------------


def seen_path() -> Path:
    configured = os.environ.get(SEEN_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".linkedin-mcp" / "mivia-engagers-seen.json"


class SeenStore:
    """Per post: engager keys already reported. Plain JSON, rewritten atomically."""

    def __init__(self, path: Path | None = None):
        self.path = path or seen_path()

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def keys(self, activity_id: str) -> set[str]:
        return set((self.load().get(activity_id) or {}).get("keys", []))

    def remember(self, activity_id: str, keys: set[str]) -> None:
        data = self.load()
        entry = data.get(activity_id) or {}
        entry["keys"] = sorted(set(entry.get("keys", [])) | keys)
        entry["last_run"] = datetime.now().astimezone().isoformat(timespec="seconds")
        data[activity_id] = entry
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)


def engager_key(
    kind: str, ident: str | None, extra: str = "", *, name: str | None = None
) -> str:
    """Memory key. Without an id the name stands in, so id-less engagers are
    not all collapsed into one '?' entry."""
    who = ident or f"name={(name or '?').strip().lower()}"
    return f"{kind}:{who}:{extra}"


class MiviaEngagementReader:
    def __init__(self, session: PageSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator

    @property
    def _page(self) -> Any:
        return self._session.page

    async def _pause(self, bounds: tuple[float, float]) -> None:
        await self._session.delay(random.uniform(*bounds))

    async def _goto(self, url: str) -> None:
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        await self._session.delay(random.uniform(2.5, 4.0))

    async def read_reactors(self, activity_id: str, limit: int) -> dict[str, Any]:
        url = (
            f"https://www.linkedin.com/analytics/post/urn:li:activity:{activity_id}/"
            "?resultType=REACTIONS"
        )
        await self._goto(url)
        state = await self._page.evaluate(_REACTORS_JS)
        previous = -1
        stale = 0
        for _ in range(40):
            if len(state["items"]) >= limit:
                break
            if len(state["items"]) == previous:
                stale += 1
                if stale >= 2:
                    break
            else:
                stale = 0
            previous = len(state["items"])
            await self._page.evaluate(_SCROLL_ALL_JS)
            await self._pause(_SCROLL_PAUSE)
            state = await self._page.evaluate(_REACTORS_JS)
        if not state["items"] and state["failed"]:
            return {"available": False, "reason": "not_own_post", "reactors": []}
        reactors = []
        seen: set[str] = set()
        for item in state["items"][:limit]:
            ref = parse_person_ref(item["href"])
            if ref["id"] is not None:
                if ref["id"] in seen:
                    continue
                seen.add(ref["id"])
            reactors.append(
                {
                    **split_engager_lines(item["lines"]),
                    **ref,
                    "reaction": reaction_kind(item["icons"], item["lines"]),
                }
            )
        return {
            "available": True,
            "reactors": reactors,
            # Measured on the raw items: duplicates dropped above must not hide
            # that items beyond *limit* were cut off.
            "complete": not state["more"] and len(state["items"]) < limit,
        }

    async def read_post_page(self, activity_id: str) -> dict[str, Any]:
        await self._goto(
            f"https://www.linkedin.com/feed/update/urn:li:activity:{activity_id}/"
        )
        count_text = await self._page.evaluate(_REACTION_COUNT_JS)
        cards = await self._page.evaluate(_COMMENTS_JS)
        comments = []
        for card in cards:
            ref = parse_person_ref(card["href"])
            match = re.search(r",(\d+)\)", card["key"])
            comments.append(
                {
                    **split_comment_lines(card["lines"]),
                    **ref,
                    "comment_id": match.group(1) if match else None,
                    "is_reply": card["parent"] is not None,
                }
            )
        return {
            "reaction_count": _number(count_text) if count_text else None,
            "comments": comments,
        }

    async def read_post_summary(self, activity_id: str) -> dict[str, Any]:
        url = f"https://www.linkedin.com/analytics/post-summary/urn:li:activity:{activity_id}/"
        await self._goto(url)
        text = await self._page.evaluate(_SUMMARY_JS)
        metrics = parse_post_summary(text or "")
        return {
            "activity_id": activity_id,
            "url": url,
            "available": bool(metrics),
            "metrics": metrics,
        }
