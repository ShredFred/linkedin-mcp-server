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

CALENDLY_ACCOUNT = "calendly.com/mivia_jessica-schneider"
_URL_RE = re.compile(r"\b((?:https?://|www\.)[^\s<>()\"']+)", re.IGNORECASE)
_SHORTENERS = ("bit.ly", "tinyurl.com", "t.co/", "goo.gl", "ow.ly", "lnkd.in")
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


def check_links(text: str) -> list[dict[str, Any]]:
    found = []
    for match in _URL_RE.finditer(text or ""):
        url = match.group(1).rstrip(".,;:!?")
        low = url.lower()
        if not low.startswith("https://"):
            found.append({"code": "link_not_https", "url": url})
        elif any(s in low for s in _SHORTENERS):
            found.append({"code": "link_shortener", "url": url})
        elif "calendly.com" in low and CALENDLY_ACCOUNT not in low:
            found.append({"code": "calendly_wrong_account", "url": url})
    return found


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
