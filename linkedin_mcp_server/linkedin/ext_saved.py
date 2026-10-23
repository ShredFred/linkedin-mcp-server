"""Fork extension: read the member's saved posts and own reactions.

* ``/my-items/saved-posts/`` -- posts the signed-in member bookmarked;
* ``/in/<own slug>/recent-activity/reactions/`` -- posts the member reacted to.

Read only. Both pages render one card per post, but not with the same DOM:
the activity feed carries ``data-urn="urn:li:activity:..."``, the saved list
links each card to ``/feed/update/urn:li:activity:.../``. The collector
therefore takes the URN from either signal and the card text from the nearest
list item, without class names (house rule: minimal DOM dependence).
"""

from __future__ import annotations

import random
import re
from typing import Any

SAVED_POSTS_URL = "https://www.linkedin.com/my-items/saved-posts/"
# ``/in/me/recent-activity/reactions/`` renders an empty shell (measured
# 2026-10-21); the tab only fills under the member's own vanity slug.
REACTIONS_URL = "https://www.linkedin.com/in/{slug}/recent-activity/reactions/"
_SLUG_RE = re.compile(r"linkedin\.com/in/([^/?#]+)")

_ACTIVITY_RE = re.compile(r"urn:li:(activity|ugcPost|share):(\d+)")

_MAIN_TEXT_JS = "() => (document.querySelector('main') || document.body).innerText"

# The SDUI activity tab (2026-10) has neither ``data-urn`` nor a per-card
# permalink. Its only per-card signal is the screen-reader heading above each
# card, which is text and therefore locale-bound: one entry per UI language.
CARD_HEADINGS = {"Feed-Beitrag", "Feed post"}

_CARDS_JS = r"""(max) => {
  const out = [];
  const seen = new Set();
  const push = (urn, card) => {
    if (!urn || seen.has(urn) || !card) return;
    seen.add(urn);
    const author = card.querySelector('a[href*="/in/"], a[href*="/company/"]');
    out.push({
      urn,
      text: (card.innerText || '').slice(0, 3000),
      author_url: author ? author.getAttribute('href') : null,
    });
  };
  for (const el of document.querySelectorAll('[data-urn*="urn:li:activity:"]')) {
    push(el.getAttribute('data-urn'), el);
    if (out.length >= max) return out;
  }
  const main = document.querySelector('main') || document.body;
  for (const a of main.querySelectorAll('a[href*="urn:li:"], a[href*="urn%3Ali%3A"]')) {
    const href = decodeURIComponent(a.getAttribute('href') || '');
    const m = /urn:li:(activity|ugcPost):\d+/.exec(href);
    if (!m) continue;
    push(m[0], a.closest('li') || a.closest('article') || a.parentElement);
    if (out.length >= max) return out;
  }
  return out;
}"""


def activity_id(urn: str) -> str | None:
    m = _ACTIVITY_RE.search(urn or "")
    return m.group(2) if m else None


def split_feed_text(text: str, limit: int) -> list[dict[str, Any]]:
    """Fallback cards from the page text, split at the per-card heading."""
    chunks: list[list[str]] = []
    for line in (text or "").splitlines():
        if line.strip() in CARD_HEADINGS:
            chunks.append([])
        elif chunks:
            chunks[-1].append(line)
    posts = []
    for chunk in chunks[:limit]:
        body = re.sub(r"\n{3,}", "\n\n", "\n".join(chunk)).strip()
        if body:
            posts.append(
                {
                    "activity_id": None,
                    "post_url": None,
                    "author_url": None,
                    "text": body,
                }
            )
    return posts


def normalise_cards(raw: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Dedupe by activity id, build the permalink, keep order (newest first)."""
    posts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for card in raw:
        m = _ACTIVITY_RE.search(str(card.get("urn") or ""))
        aid = m.group(2) if m else None
        if not aid or aid in seen:
            continue
        assert m is not None
        seen.add(aid)
        text = re.sub(r"\n{3,}", "\n\n", str(card.get("text") or "")).strip()
        author = card.get("author_url")
        if author and author.startswith("/"):
            author = "https://www.linkedin.com" + author
        posts.append(
            {
                "activity_id": aid,
                "post_url": f"https://www.linkedin.com/feed/update/urn:li:{m.group(1)}:{aid}/",
                "author_url": author.split("?")[0] if author else None,
                "text": text,
            }
        )
        if len(posts) >= limit:
            break
    return posts


def own_slug(url: str) -> str | None:
    """The vanity slug from the URL ``/in/me/`` redirected to; None for 'me'."""
    m = _SLUG_RE.search(url or "")
    return m.group(1) if m and m.group(1) != "me" else None


def matches(post: dict[str, Any], query: str | None) -> bool:
    if not query:
        return True
    hay = post.get("text", "").casefold()
    return all(term in hay for term in query.casefold().split())


class ExtSavedReader:
    def __init__(self, session: Any, navigator: Any) -> None:
        self._session = session
        self._navigator = navigator

    @property
    def _page(self) -> Any:
        return self._session.page

    async def _collect(self, url: str, limit: int, max_rounds: int) -> list[dict]:
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        await self._session.delay(random.uniform(2.5, 4.0))
        raw: list[dict] = []
        stale = 0
        for _ in range(max_rounds):
            raw = await self._page.evaluate(_CARDS_JS, limit * 2)
            if len(normalise_cards(raw, limit)) >= limit:
                break
            before = len(raw)
            await self._page.evaluate(
                "() => window.scrollTo(0, document.body.scrollHeight)"
            )
            await self._session.delay(random.uniform(1.8, 3.0))
            raw = await self._page.evaluate(_CARDS_JS, limit * 2)
            stale = stale + 1 if len(raw) <= before else 0
            if stale >= 2:
                break
        return raw

    async def read(
        self, kind: str, limit: int, query: str | None = None
    ) -> dict[str, Any]:
        if kind == "saved":
            url = SAVED_POSTS_URL
        else:
            await self._navigator._navigate_to_page("https://www.linkedin.com/in/me/")
            await self._session.delay(random.uniform(1.5, 2.5))
            slug = own_slug(str(getattr(self._page, "url", "")))
            if not slug:
                return {"kind": kind, "status": "own_profile_unresolved", "posts": []}
            url = REACTIONS_URL.format(slug=slug)
        raw = await self._collect(url, limit if not query else limit * 3, 12)
        posts = normalise_cards(raw, limit if not query else limit * 3)
        source = "cards"
        # On the SDUI tab the only URN links belong to reshared posts embedded
        # in a card, so a short card list is not the page: the text split wins
        # whenever it sees more cards.
        text = await self._page.evaluate(_MAIN_TEXT_JS)
        by_text = split_feed_text(text, limit if not query else limit * 3)
        if len(by_text) > len(posts):
            posts, source = by_text, "page_text"
        posts = [p for p in posts if matches(p, query)][:limit]
        return {
            "url": url,
            "kind": kind,
            "landed": str(getattr(self._page, "url", "")),
            "source": source,
            "count": len(posts),
            "posts": posts,
        }
