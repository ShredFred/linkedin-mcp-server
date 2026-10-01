"""MiViA fork: content checks a message must pass before it can leave.

Shared by ``send_inmail`` and ``edit_sent_message`` (2026-09-30). Every check
is a named function with its own reason code, so a refusal says which rule
fired and a rule that fires too broadly is visible in the count.

* **Links** -- only ``https://`` links; a Calendly link must point at the
  booking account ``calendly.com/mivia_jessica-schneider`` (the bare
  ``calendly.com/mivia`` is somebody else's page); no link shorteners, because
  the recipient cannot see where they lead.
* **Placeholders** -- ``{name}``, ``[Vorname]``, ``<Firma>``, ``XXX`` or
  ``TODO`` mean a template was sent unfilled.
* **Salutation** -- when the text opens with a salutation ("Hallo Herr
  König", "Sehr geehrte Frau …", "Hi Dieter", "Dear …"), the name after it
  must be the recipient's first or last name as LinkedIn shows it. A letter
  that greets the previous recipient is the most visible copy-paste error.

The checks are pure: they read the text and the recipient name, nothing else.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any
from urllib.parse import urlsplit

CALENDLY_ACCOUNT = "calendly.com/mivia_jessica-schneider"
_URL_RE = re.compile(r"\b((?:https?://|www\.)[^\s<>()\"']+)", re.IGNORECASE)
# Matched against the URL host (exact or as a parent domain), never as a
# substring: "t.co/" used to hit robot.co/x and "bit.ly" www.orbit.ly.
_SHORTENERS = ("bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "lnkd.in")
_CALENDLY_HOSTS = ("calendly.com", "www.calendly.com")
# Hidden in a pasted text and reordering or hiding it; same rule as
# tools/mivia.py:_hidden_format_char (ZWNJ/ZWJ and emoji tags allowed).
_ALLOWED_CF = (0x200C, 0x200D)
# LinkedIn's direct-message limit, counted in UTF-16 units like the composer.
MESSAGE_MAX_UTF16 = 8000
_BARE_WWW = re.compile(r"(?<![\w/.:@-])www\.", re.IGNORECASE)
_BARE_HOST = re.compile(
    r"(?<![\w/.:@-])(?=(?:[a-z0-9-]+\.)+(?:ly|com|co|gl|in|link|me|io|to|gd|is|cc)\b/)",
    re.IGNORECASE,
)
_PLACEHOLDER_RE = re.compile(
    r"\{[^{}\n]{1,40}\}|\[(?:ihr |dein )?(?:vorname|nachname|name|firma|firmenname"
    r"|unternehmen|anrede|position|titel|company|first ?name|last ?name)\]"
    r"|<(?:vorname|nachname|name|firma|firmenname|unternehmen|anrede|company)>"
    r"|%(?:vorname|nachname|name|firma|company|first_?name|last_?name)%"
    r"|\bXXX+\b|\bTODO\b",
    re.IGNORECASE,
)
_SALUTATION_RE = re.compile(
    r"^\s*(?:(?:Hallo|Guten Tag|Liebe[rs]?|Hi|Hey|Dear|Hello|Moin|Servus|Grüezi)"
    r"|Sehr geehrte[rs]?)"
    r"(?:\s+(?:Herr|Frau|Mr\.?|Mrs\.?|Ms\.?|Dr\.?|Prof\.?))*"
    r"\s+([^\s,!.\n]+(?:[ -][^\s,!.\n]+)?)",
    re.IGNORECASE,
)
_TITLE_WORDS = {"herr", "frau", "mr", "mrs", "ms", "dr", "prof"}


def _fold(value: str) -> str:
    value = (value or "").lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        value = value.replace(a, b)
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    return value.replace("ß", "ss").lower().strip()


def utf16_len(text: str) -> int:
    """Length as the browser counts it: an emoji is two units, not one."""
    return len((text or "").encode("utf-16-le")) // 2


def hidden_format_char(character: str) -> bool:
    """A Unicode format character (Cf) or C1/bidi control that hides or
    reorders text. ZWNJ/ZWJ and the emoji tag characters stay allowed."""
    code = ord(character)
    if 0x7F <= code <= 0x9F:
        return True
    if unicodedata.category(character) != "Cf":
        return False
    return not (code in _ALLOWED_CF or 0xE0020 <= code <= 0xE007F)


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _host_is(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def calendly_ok(url: str) -> bool:
    """Host exactly calendly.com/www.calendly.com and first path segment
    exactly the booking account."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    segment = parts.path.lstrip("/").split("/", 1)[0].lower()
    return host in _CALENDLY_HOSTS and segment == CALENDLY_ACCOUNT.split("/", 1)[1]


def check_links(text: str) -> list[dict[str, Any]]:
    found = []
    for match in _URL_RE.finditer(text or ""):
        url = match.group(1).rstrip(".,;:!?")
        low = url.lower()
        if not low.startswith("https://"):
            found.append({"code": "link_not_https", "url": url})
            continue
        host = _host(url)
        if any(_host_is(host, s) for s in _SHORTENERS):
            found.append({"code": "link_shortener", "url": url})
        elif "calendly" in low and not calendly_ok(url):
            # Any mention counts: calendly.com.evil.example/... and
            # ?r=calendly.com/... are not the booking page.
            found.append({"code": "calendly_wrong_account", "url": url})
    return found


def bare_links_as_https(text: str) -> str:
    """ "www.x" and bare "bit.ly/x" are linkified by LinkedIn: rewrite them to
    https:// so the link rules run on them too."""
    return _BARE_HOST.sub("https://", _BARE_WWW.sub("https://www.", text or ""))


def check_outgoing(text: str, *, max_utf16: int) -> dict[str, Any] | None:
    """Browser-free refusal shared by InMail body/subject and message edit:
    hidden format characters, length, links (bare ones too), placeholders.
    Salutation is left to the caller, who knows the recipient."""
    if any(hidden_format_char(c) for c in text or ""):
        return {
            "status": "invalid_message",
            "detail": "invisible or direction-changing characters are refused",
        }
    if utf16_len(text) > max_utf16:
        return {"status": "message_too_long", "max": max_utf16}
    findings = check_links(bare_links_as_https(text)) + check_placeholders(text)
    if findings:
        return {"status": "content_check_failed", "findings": findings}
    return None


def check_placeholders(text: str) -> list[dict[str, Any]]:
    return [
        {"code": "unfilled_placeholder", "match": m.group(0)}
        for m in _PLACEHOLDER_RE.finditer(text or "")
    ]


def check_salutation(text: str, recipient_name: str | None) -> list[dict[str, Any]]:
    match = _SALUTATION_RE.match(text or "")
    if not match:
        return []
    greeted = [
        w
        for w in re.split(r"[ -]", _fold(match.group(1)))
        if w and w not in _TITLE_WORDS
    ]
    if not recipient_name:
        return [{"code": "salutation_unverifiable", "greeted": match.group(1)}]
    parts = {p for p in re.split(r"[\s-]+", _fold(recipient_name)) if len(p) > 1}
    if greeted and all(w.strip(".") in parts for w in greeted):
        return []
    return [
        {
            "code": "salutation_mismatch",
            "greeted": match.group(1),
            "recipient": recipient_name,
        }
    ]


def check_message(text: str, recipient_name: str | None = None) -> list[dict[str, Any]]:
    """All findings; an empty list means the text may leave."""
    return (
        check_links(text)
        + check_placeholders(text)
        + check_salutation(text, recipient_name)
    )
