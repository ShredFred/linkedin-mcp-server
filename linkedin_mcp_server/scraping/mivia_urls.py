"""MiViA fork: one place that decides what a LinkedIn profile URL means.

LinkedIn changes its profile URLs without notice. On 2026-09-30 every
profile opened by another member started redirecting to
``/in/<slug>/?isSelfProfile=false`` and the strict path check in the sender
refused all of them, so every send failed with recipient_resolution_failed.
The narrow fix accepted exactly that marker. This module generalises it
without loosening identity:

* A query is accepted only when every key is on an explicit allowlist of
  keys measured to carry no identity (tracking and presentation). Any other
  key, a repeated key, or a disallowed value fails closed.
* A fragment fails closed. It carries nothing we need and is the easiest
  place to smuggle a second path.
* The slug is compared percent-decoded and case-folded. LinkedIn emits
  ``/in/dieter-k%C3%B6nig-.../`` (measured 2026-09-30) while our identifiers
  hold ``dieter-könig-...``; a raw string compare made every umlaut slug a
  mismatch, and the vanityName selector in the connect path silently never
  matched them (two of the eight connect_unavailable results of 2026-09-29).
* An optional two-letter locale segment (``/in/<slug>/de/``) is the same
  profile in another UI language and is folded away.

Identity itself is never proven by the URL: the sender still pins the
recipient by its profile URN, and this module only decides whether a page
URL is *a* profile page for one unambiguous slug.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, quote, unquote, urlparse

_HOST_RE = re.compile(r"^(?:[a-z0-9-]+\.)*linkedin\.com$")
_PROFILE_RE = re.compile(r"^/in/([^/]+)(?:/([a-z]{2}))?/?$")

#: Query keys that carry no identity. ``None`` allows any single value;
#: a set restricts the value. Everything else fails closed.
BENIGN_PROFILE_QUERY: dict[str, frozenset[str] | None] = {
    # 2026-09-30 redirect marker. "true" would mean our own profile.
    "isSelfProfile": frozenset({"false"}),
    "trk": None,
    "trackingId": None,
    "lipi": None,
    "lici": None,
    "originalSubdomain": None,
    "locale": None,
}


def profile_key(slug: str) -> str | None:
    """Decoded, case-folded slug used for every identity comparison."""
    if not isinstance(slug, str) or not slug:
        return None
    try:
        decoded = unquote(slug, errors="strict")
    except UnicodeDecodeError:
        return None
    if (
        not decoded
        or "/" in decoded
        or "?" in decoded
        or "#" in decoded
        or "%" in decoded
        or any(ord(c) < 32 or ord(c) == 127 or c.isspace() for c in decoded)
    ):
        return None
    return decoded.lower()


def benign_query(query: str) -> bool:
    """True when every query key is allowlisted, once, with an allowed value."""
    if not query:
        return True
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return False
    seen: set[str] = set()
    for key, value in pairs:
        if key in seen or key not in BENIGN_PROFILE_QUERY:
            return False
        seen.add(key)
        allowed = BENIGN_PROFILE_QUERY[key]
        if allowed is not None and value not in allowed:
            return False
    return True


def profile_slug_from_url(value: str) -> str | None:
    """The decoded slug of a LinkedIn profile page URL, or None when unsafe."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\\" in value
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        return None
    try:
        parsed = urlparse(value.strip())
        port = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().removesuffix(".")
    if (
        parsed.scheme != "https"
        or not _HOST_RE.fullmatch(host)
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
        or not benign_query(parsed.query)
    ):
        return None
    match = _PROFILE_RE.fullmatch(parsed.path)
    if match is None:
        return None
    raw = match.group(1)
    if profile_key(raw) is None:
        return None
    return unquote(raw)


def canonical_profile_path(slug: str) -> str:
    """``/in/<slug>/`` with the slug escaped as one segment (upper-case hex)."""
    return f"/in/{quote(slug, safe='')}/"


def identity_path(slug: str) -> str | None:
    """``/in/<key>/`` -- the form the in-page scripts compare against."""
    key = profile_key(slug)
    return f"/in/{key}/" if key else None
