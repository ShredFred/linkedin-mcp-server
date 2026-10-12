"""Fork extension: what happened after the publish click, and is the post live?

Incident 2026-10-12: ``create_post`` clicked "Posten", the composer did not
close within one 20 s wait, the tool answered ``post_unconfirmed``, and the
calling agent could not find out whether the post was live. This module turns
the click into one of a closed set of outcomes:

* ``posted``      -- the composer closed (or a success toast named the post);
  the post is read back from the member's (or page's) recent activity by a
  normalised text prefix, with backoff for the activity feed's lag.
* ``not_posted``  -- the composer is still open with text, or LinkedIn showed an
  error; the composer is then discarded so no draft is left.
* ``draft_saved`` -- LinkedIn put up its "save as draft?" prompt instead of
  publishing; the prompt is answered with *discard*, never with save.
* ``unknown``     -- only when nothing above could be established (fail closed:
  the caller's 30-day block keeps the text).

The same matcher answers ``verify_post`` (read-only) and the pre-publish
duplicate check (an identical post within 24 h refuses the publish).

Text matching is deliberately lossy: LinkedIn strips a URL that became a link
preview, truncates with "… mehr" / "…see more", renders hashtags as
"hashtag#x", and normalises quotes, dashes and emoji. Both sides are therefore
reduced to lower-case letters and digits separated by single spaces, and the
first ``PREFIX_CHARS`` of that form are compared.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Awaitable, Callable
from typing import Any

PREFIX_CHARS = 120
# A truncated card ("… mehr") still matches when at least this much agrees.
MIN_TRUNCATED_CHARS = 30
# Read-back schedule (seconds to wait before each attempt): ~63 s in total.
READBACK_WAITS: tuple[float, ...] = (3.0, 5.0, 8.0, 12.0, 15.0, 20.0)
# After a not-posted verdict only a short confirmation read is spent.
SHORT_READBACK_WAITS: tuple[float, ...] = (3.0, 6.0)
SETTLE_TIMEOUT = 30.0
SETTLE_POLL = 1.0
UPLOAD_TIMEOUT = 60.0
ERROR_POLLS = 3
DUPLICATE_WINDOW_SECONDS = 24 * 3600
# A read-back hit counts as the post just made when it is at most this old
# (or, age unreadable, the newest card on the page).
FRESH_SECONDS = 3600

# The success toast and the draft prompt are recognised by text only where no
# attribute exists: per-locale table, compared lower-cased (documented
# exception to the locale rule, see AGENTS.md).
DRAFT_PROMPT_WORDS = ["als entwurf speichern", "entwurf speichern", "save as draft", "save draft"]
DISCARD_WORDS = ["verwerfen", "änderungen verwerfen", "discard", "beitrag verwerfen", "discard post"]

_URL_RE = re.compile(r"(https?://\S+|www\.\S+|lnkd\.in/\S+)", re.IGNORECASE)
_HASHTAG_RE = re.compile(r"\bhashtag\s*#", re.IGNORECASE)
_TRUNC_RE = re.compile(
    r"(…|\.\.\.)\s*(mehr anzeigen|mehr|see more|more)\s*$", re.IGNORECASE
)
_AGE_RE = re.compile(
    r"(?<![\w])(\d{1,3})\s*(sek\.?|s|min\.?|m|std\.?|h|tg\.?|tag(?:e)?|t|d|wo\.?|w|mon\.?|mo\.?|j|y|yr)(?![\w])",
    re.IGNORECASE,
)
_NOW_RE = re.compile(r"(?<![\w])(gerade eben|jetzt|now|just now)(?![\w])", re.IGNORECASE)
_UNIT_SECONDS = {
    "s": 1, "sek": 1, "m": 60, "min": 60, "h": 3600, "std": 3600,
    "d": 86400, "t": 86400, "tg": 86400, "tag": 86400, "tage": 86400,
    "w": 604800, "wo": 604800, "mo": 2592000, "mon": 2592000,
    "j": 31536000, "y": 31536000, "yr": 31536000,
}


def normalize(text: str) -> str:
    """Letters and digits only, lower-case, single spaces (see module doc)."""
    text = unicodedata.normalize("NFKC", text or "")
    text = _URL_RE.sub(" ", text)
    text = _HASHTAG_RE.sub("#", text)
    out = []
    for ch in text.casefold():
        cat = unicodedata.category(ch)
        out.append(ch if cat[0] in "LN" else " ")
    return " ".join("".join(out).split())


def prefix(text: str) -> str:
    return normalize(text)[:PREFIX_CHARS]


def card_age_seconds(card_text: str) -> int | None:
    """Relative age from the card header ("2 Std.", "3d", "Gerade eben")."""
    head = (card_text or "")[:400]
    if _NOW_RE.search(head):
        return 0
    m = _AGE_RE.search(head)
    if not m:
        return None
    unit = m.group(2).lower().rstrip(".")
    seconds = _UNIT_SECONDS.get(unit)
    return int(m.group(1)) * seconds if seconds else None


def card_body(card_text: str, limit: int = 160) -> str:
    """The post text of a card, without the header chrome before the age.

    Live cards start with "Feed-Beitrag", the boost banner, author and
    headline; a 160-character head of that identifies nothing. Everything
    after the first age token is the post itself."""
    raw = card_text or ""
    m = _NOW_RE.search(raw[:600]) or _AGE_RE.search(raw[:600])
    body = raw[m.end():] if m else raw
    return " ".join(body.split())[:limit]

def card_matches(card_text: str, text: str) -> bool:
    """Does an activity card carry this post text (lossy, see module doc)?"""
    want = prefix(text)
    if not want:
        return False
    raw = (card_text or "").rstrip()
    truncated = bool(_TRUNC_RE.search(raw))
    card = normalize(_TRUNC_RE.sub("", raw))
    if want in card:
        return True
    anchor = want[:MIN_TRUNCATED_CHARS]
    if not truncated or len(anchor) < MIN_TRUNCATED_CHARS:
        return False
    pos = card.find(anchor)
    if pos < 0:
        return False
    tail = card[pos:]
    # The visible part must be a clean prefix of the post up to the cut.
    return want.startswith(tail) or tail.startswith(want)


def post_url(urn: str | None) -> str | None:
    return f"https://www.linkedin.com/feed/update/{urn}/" if urn else None


def find_matches(cards: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    """Matching cards in page order, each with index, urn, url and age."""
    hits = []
    for i, c in enumerate(cards or []):
        if card_matches(str(c.get("text") or ""), text):
            hits.append(
                {
                    "index": i,
                    "urn": c.get("urn"),
                    "post_url": post_url(c.get("urn")),
                    "age_seconds": card_age_seconds(str(c.get("text") or "")),
                }
            )
    return hits


def fresh_hit(hits: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The hit that is the post just made: young enough, or newest if ageless."""
    for h in hits:
        age = h.get("age_seconds")
        if age is not None and age <= FRESH_SECONDS:
            return h
        if age is None and h["index"] == 0:
            return h
    return None


def recent_duplicate(hits: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A hit within 24 h; an unreadable age counts as recent (fail closed)."""
    for h in hits:
        age = h.get("age_seconds")
        if age is None or age <= DUPLICATE_WINDOW_SECONDS:
            return h
    return None


# Activity cards: the urn and the card's text. Generic attributes only.
CARDS_JS = r"""() => {
  // Live 2026-10-12: own activity cards no longer carry data-urn; the urn sits
  // in other attributes (componentkey, tracking JSON, hrefs, often %3A-encoded).
  // Scan every attribute, normalise the urn, and take the text of the nearest
  // card-like ancestor.
  const RX = /urn(?::|%3A)li(?::|%3A)(activity|share|ugcPost)(?::|%3A)(\d{6,})/gi;
  const CARD = 'article, [role="article"], .feed-shared-update-v2, [data-finite-scroll-hotkey-item], li';
  const out = new Map();
  // The card is the largest ancestor that still holds at most one post: a
  // share urn sits on a small "boost" banner, the activity urn on the card,
  // and both must resolve to the same card text. A list of cards holds
  // several activity urns, so the climb stops below it.
  const acts = s => new Set([...String(s).matchAll(/activity(?::|%3A)(\d{6,})/gi)].map(m => m[1]));
  const container = el => {
    let best = el.closest(CARD) || el;
    let n = el;
    for (let i = 0; i < 25 && n.parentElement && n.parentElement !== document.body; i++) {
      const p = n.parentElement;
      if (acts(p.outerHTML).size > 1) break;
      n = p;
      best = n;
    }
    return best;
  };
  for (const el of document.querySelectorAll('*')) {
    for (const a of el.attributes) {
      if (!/urn/i.test(a.value)) continue;
      RX.lastIndex = 0;
      let m;
      while ((m = RX.exec(a.value)) !== null) {
        const type = m[1].toLowerCase() === 'ugcpost' ? 'ugcPost' : m[1].toLowerCase();
        const urn = `urn:li:${type}:${m[2]}`;
        if (out.has(urn)) continue;
        out.set(urn, { urn, text: (container(el).innerText || '').slice(0, 4000) });
      }
    }
  }
  // Activity urns first (stable): they are what comment/analytics tools take.
  const isAct = c => c.urn.includes(':activity:') ? 0 : 1;
  return [...out.values()].sort((x, y) => isAct(x) - isAct(y));
}"""

# The composer's state after the publish click. Read in one pass so every poll
# sees a consistent picture.
STATE_JS = r"""(arg) => {
  const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  const norm = v => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const editor = [...document.querySelectorAll(arg.editor)].find(visible) || null;
  const dialogs = [...document.querySelectorAll('[role="dialog"], [role="alertdialog"], dialog')].filter(visible);
  const btnText = d => [...d.querySelectorAll('button, [role="button"]')].filter(visible).map(b => norm(b.innerText));
  const draft = dialogs.some(d => btnText(d).some(t => arg.draft.includes(t)));
  const discard = !draft && dialogs.some(d => !d.querySelector(arg.editor)
      && btnText(d).some(t => arg.discard.includes(t)));
  const box = editor ? editor.closest('[role="dialog"], dialog') : null;
  const busy = !!box && [...box.querySelectorAll('[role="progressbar"], progress, [aria-busy="true"]')].some(visible);
  const toasts = [...document.querySelectorAll('[role="alert"], [aria-live="assertive"], [aria-live="polite"]')].filter(visible);
  let toastUrn = null;
  for (const t of toasts) {
    const a = t.querySelector('a[href*="urn:li:activity:"], a[href*="urn:li:share:"], a[href*="urn:li:ugcPost:"]');
    const m = a && a.getAttribute('href').match(/urn:li:(activity|share|ugcPost):\d+/);
    if (m) { toastUrn = m[0]; break; }
  }
  const alerts = [...document.querySelectorAll('[role="alert"], [aria-live="assertive"]')].filter(visible);
  const alertText = alerts.map(t => norm(t.innerText)).filter(Boolean).join(' | ').slice(0, 300);
  return {
    editor: !!editor,
    editor_chars: editor ? norm(editor.innerText).length : 0,
    draft_prompt: draft,
    discard_prompt: discard,
    busy,
    toast_urn: toastUrn,
    alert: toastUrn ? null : (alertText || null),
  };
}"""

# Answer an open draft / discard prompt with "discard"; never "save", never post.
DISCARD_PROMPT_JS = r"""(arg) => {
  const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  const norm = v => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const buttons = [...document.querySelectorAll('[role="dialog"] button, [role="alertdialog"] button, dialog button')]
    .filter(b => visible(b) && arg.discard.includes(norm(b.innerText)) && !arg.post.includes(norm(b.innerText)));
  if (buttons.length !== 1) return {clicked: false, count: buttons.length};
  buttons[0].click();
  return {clicked: true, count: 1};
}"""

Sleep = Callable[[float], Awaitable[Any]]
Navigate = Callable[[str], Awaitable[Any]]


def state_arg(editor: str) -> dict[str, Any]:
    return {"editor": editor, "draft": DRAFT_PROMPT_WORDS, "discard": DISCARD_WORDS}


async def read_state(page: Any, editor: str) -> dict[str, Any]:
    return await page.evaluate(STATE_JS, state_arg(editor)) or {}


async def wait_for_upload(
    page: Any, editor: str, sleep: Sleep, *, timeout: float = UPLOAD_TIMEOUT
) -> bool:
    """True once the composer shows no progress indicator (polled)."""
    waited = 0.0
    while True:
        state = await read_state(page, editor)
        if not state.get("busy"):
            return True
        if waited >= timeout:
            return False
        await sleep(SETTLE_POLL)
        waited += SETTLE_POLL


def classify(state: dict[str, Any]) -> str | None:
    """Terminal state, or None while the composer is still settling."""
    if state.get("draft_prompt"):
        return "draft_prompt"
    if state.get("discard_prompt"):
        return "discard_prompt"
    if state.get("toast_urn"):
        return "closed"
    if not state.get("editor"):
        return "closed"
    if state.get("alert") and not state.get("busy"):
        return "error"
    return None


async def settle(
    page: Any,
    editor: str,
    sleep: Sleep,
    *,
    timeout: float = SETTLE_TIMEOUT,
    poll: float = SETTLE_POLL,
) -> dict[str, Any]:
    """Poll the composer after the click until it reaches a terminal state."""
    waited = 0.0
    toast_urn = None
    errors = 0
    while True:
        state = await read_state(page, editor)
        toast_urn = toast_urn or state.get("toast_urn")
        verdict = classify(state)
        # An error message must persist with the composer open: a transient
        # announcement while LinkedIn is still publishing is not a failure.
        errors = errors + 1 if verdict == "error" else 0
        if verdict == "error" and errors < ERROR_POLLS:
            verdict = None
        if verdict:
            return {**state, "toast_urn": toast_urn, "verdict": verdict, "waited": waited}
        if waited >= timeout:
            return {**state, "toast_urn": toast_urn, "verdict": "open", "waited": waited}
        await sleep(poll)
        waited += poll


# Live 2026-10-12: the activity page is a SPA; right after navigation the feed
# is often not rendered yet and one evaluate returns no card at all, which
# read as "not_found" for a post that was live. Poll until cards show up.
CARD_RENDER_POLLS = 12
CARD_RENDER_INTERVAL = 1.0


async def _render_cards(page: Any, sleep: Sleep) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for i in range(CARD_RENDER_POLLS):
        cards = await page.evaluate(CARDS_JS) or []
        if cards:
            return cards
        if i < CARD_RENDER_POLLS - 1:
            await sleep(CARD_RENDER_INTERVAL)
    return cards

ME_ALIAS = "/in/me/"
_SLUG_RE = re.compile(r"/in/([^/?#]+)")


async def resolve_me(page: Any, navigate: Navigate, url: str) -> str:
    """Replace the /in/me/ alias with the member's real slug.

    Live 2026-10-12: /in/me/recent-activity/all/ is not redirected; the page
    stays empty (0 cards) and every lookup read as not_found. /in/me/ itself
    does redirect, so resolve the slug there first. Unresolvable -> url as is.
    """
    if ME_ALIAS not in url:
        return url
    await navigate("https://www.linkedin.com/in/me/")
    m = _SLUG_RE.search(str(getattr(page, "url", "") or ""))
    slug = m.group(1) if m else "me"
    return url if slug == "me" else url.replace(ME_ALIAS, f"/in/{slug}/", 1)

async def read_activity(
    page: Any,
    navigate: Navigate,
    sleep: Sleep,
    text: str,
    url: str,
    *,
    waits: tuple[float, ...] = READBACK_WAITS,
    want_fresh: bool = True,
) -> dict[str, Any]:
    """Read the activity page until the text appears (backoff over ``waits``)."""
    attempts = 0
    hits: list[dict[str, Any]] = []
    hit = None
    cards_seen = 0
    landed = ""
    url = await resolve_me(page, navigate, url)
    for wait in waits:
        await sleep(wait)
        attempts += 1
        await navigate(url)
        cards = await _render_cards(page, sleep)
        cards_seen = len(cards)
        landed = str(getattr(page, "url", "") or "")
        hits = find_matches(cards, text)
        hit = fresh_hit(hits) if want_fresh else (hits[0] if hits else None)
        if hit:
            break
    return {
        "found": bool(hit),
        "urn": (hit or {}).get("urn"),
        "post_url": (hit or {}).get("post_url"),
        "age_seconds": (hit or {}).get("age_seconds"),
        "matches": hits,
        "attempts": attempts,
        "activity_url": url,
        # Diagnosis for not_found: 0 cards = page not rendered or wrong page.
        "cards_seen": cards_seen,
        "landed_url": landed,
    }


async def decide(
    *,
    page: Any,
    editor: str,
    sleep: Sleep,
    navigate: Navigate,
    discard: Callable[[dict[str, Any]], Awaitable[Any]],
    post_words: list[str],
    text: str,
    activity_url: str,
    settle_timeout: float = SETTLE_TIMEOUT,
) -> dict[str, Any]:
    """Everything after the publish click; returns the result fields.

    ``status`` stays in the composers' vocabulary (posted_verified,
    posted_unverified, post_unconfirmed) plus not_posted and draft_discarded;
    ``outcome`` is the closed set posted / not_posted / draft_saved / unknown.
    """
    st = await settle(page, editor, sleep, timeout=settle_timeout)
    out: dict[str, Any] = {
        "settle": {k: st.get(k) for k in ("verdict", "waited", "editor_chars", "alert", "toast_urn")},
        "retry_safe": False,
    }
    verdict = st["verdict"]

    if verdict in ("draft_prompt", "discard_prompt"):
        clicked = await page.evaluate(
            DISCARD_PROMPT_JS, {"discard": DISCARD_WORDS, "post": post_words}
        )
        out["prompt_discarded"] = bool((clicked or {}).get("clicked"))
        if not out["prompt_discarded"]:
            await discard(out)
        rb = await read_activity(
            page, navigate, sleep, text, activity_url, waits=SHORT_READBACK_WAITS
        )
        out["readback"] = rb
        if rb["found"]:
            return {**out, **_posted(rb, verified=True)}
        return {
            **out,
            "posted": False,
            "outcome": "draft_saved" if verdict == "draft_prompt" else "not_posted",
            "status": "draft_discarded" if verdict == "draft_prompt" else "not_posted",
            "retry_safe": out["prompt_discarded"],
            "message": "LinkedIn asked to save a draft instead of publishing; the draft was discarded."
            if verdict == "draft_prompt"
            else "LinkedIn asked to discard the post; it was discarded, nothing published.",
        }

    if verdict == "closed":
        rb = await read_activity(page, navigate, sleep, text, activity_url)
        out["readback"] = rb
        if rb["found"]:
            return {**out, **_posted(rb, verified=True)}
        toast = st.get("toast_urn")
        return {
            **out,
            "posted": True,
            "verified": False,
            "outcome": "posted",
            "status": "posted_unverified",
            "activity_id": toast.rsplit(":", 1)[1] if toast else None,
            "post_url": post_url(toast),
            "message": "The composer closed (LinkedIn published), but the post "
            "was not found in recent activity within ~60 s. Do not repost; "
            "call verify_post later.",
        }

    # error or still open: the composer holds the text. Discard it first (no
    # draft may stay), then make sure nothing went live after all.
    await discard(out)
    rb = await read_activity(
        page, navigate, sleep, text, activity_url, waits=SHORT_READBACK_WAITS
    )
    out["readback"] = rb
    if rb["found"]:
        return {**out, **_posted(rb, verified=True)}
    if verdict == "error" or st.get("editor_chars", 0) > 0:
        return {
            **out,
            "posted": False,
            "outcome": "not_posted",
            "status": "not_posted",
            "retry_safe": True,
            "error": st.get("alert"),
            "message": "LinkedIn did not publish (composer stayed open"
            + (f", message: {st.get('alert')}" if st.get("alert") else "")
            + "); the composer was discarded and the post is not in recent activity.",
        }
    return {
        **out,
        "posted": False,
        "outcome": "unknown",
        "status": "post_unconfirmed",
        "message": "The composer neither closed nor kept its text; the post "
        "was not found. Do not repost; call verify_post later.",
    }


def _posted(rb: dict[str, Any], *, verified: bool) -> dict[str, Any]:
    urn = rb.get("urn")
    return {
        "posted": True,
        "verified": verified,
        "outcome": "posted",
        "status": "posted_verified",
        "activity_id": urn.rsplit(":", 1)[1] if urn else None,
        "post_url": rb.get("post_url"),
        "post_url_matched_text": True,
    }
