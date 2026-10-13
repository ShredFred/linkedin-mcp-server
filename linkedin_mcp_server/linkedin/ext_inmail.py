"""Fork extension: InMail through Sales Navigator, and editing an own sent message.

Measured 2026-09-30, headless, de locale, the member's account (Premium plus
Sales Navigator):

InMail
------
* A 2nd/3rd-degree profile renders no top-card "Nachricht" button. Its "Mehr"
  menu holds ``In Sales Navigator anzeigen`` with
  ``href=/sales/people/<ACoA…>,name,<x>/`` and, for Premium, a
  ``/messaging/compose/?profileUrn=…`` InMail link. The Sales Navigator route is
  used: its composer names the credit cost and has a required subject field.
* ``/sales/people/…`` redirects to ``/sales/lead/…``. The lead header has one
  *visible* ``button`` "Nachricht" (a hidden duplicate sits before it).
* Clicking it opens ``section[role=dialog][aria-label="Unterhaltung mit
  <Name>"]`` with the text "Neue Nachricht an <Name>", "1 von 150
  InMail-Guthaben verwenden", ``input[aria-label="Betreff (erforderlich)"]``,
  ``textarea[name=message]`` and a ``button`` "Senden" (disabled while empty).
* The Sales Navigator inbox header reads "InMail-Guthaben: 150 verbleibend".

Edit
----
* A thread renders each message as ``li.msg-s-message-list__event`` holding
  ``.msg-s-event-listitem``; the other side's carry ``--other``.
* Hovering the bubble reveals ``button.msg-s-event-listitem__options-trigger``;
  its dropdown lists "Weiterleiten", "Per E-Mail teilen" and, for an own
  message still inside the edit window, "Löschen" and "Bearbeiten".
* **Edit window: about 60 minutes**, for editing and deleting alike. Measured
  on a test message sent 16:09: both items present at 16:19 … 17:00 and the
  edit itself at 16:17, both gone at 17:10. The tool does not compute the
  window; it reads the menu, so a change by LinkedIn surfaces as
  ``edit_window_closed`` rather than a wrong click.
* "Bearbeiten" swaps the thread composer for ``form.msg-edit-form__base-form``
  ("Nachricht bearbeiten"), prefilled with the old text in
  ``.msg-form__contenteditable``, with ``button.msg-edit-form__dismiss-button``
  "Abbrechen" and ``button.msg-edit-form__save-button`` "Speichern".
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
import unicodedata
from typing import Any

from linkedin_mcp_server.linkedin.ext_actions import ExtActions

logger = logging.getLogger(__name__)

SN_INBOX_URL = "https://www.linkedin.com/sales/inbox/"
_CREDITS_USE_RE = re.compile(
    r"(\d+)\s+von\s+([\d.]+)\s+InMail-Guthaben|Use\s+(\d+)\s+of\s+([\d,]+)\s+InMail credits",
    re.IGNORECASE,
)
_CREDITS_LEFT_RE = re.compile(
    r"InMail-Guthaben:\s*([\d.]+)\s*verbleibend|([\d.]+)\s+InMail-Guthaben-Einheiten"
    r"|InMail credits:\s*([\d,]+)\s*(?:remaining|left)"
    # 2026-10-13: further wordings, so a relabelled header is still read.
    r"|Verbleibende(?:s)?\s+InMail-Guthaben:?\s*([\d.]+)"
    r"|([\d.]+)\s+InMail-Guthaben\s+(?:verbleibend|verfügbar|übrig)"
    r"|InMail-Guthaben\s+verbleibend:?\s*([\d.]+)"
    r"|([\d,]+)\s+InMail credits?\s+(?:remaining|left|available)"
    r"|(?:Remaining\s+InMail credits|InMail credits remaining|InMail credits left):?\s*([\d,]+)",
    re.IGNORECASE,
)
_FREE_RE = re.compile(
    r"kostenlos|kostenfrei|Open Profile|Offenes Profil|free message|for free",
    re.IGNORECASE,
)
_NO_CREDITS_RE = re.compile(
    r"kein(?:e)?\s+InMail-Guthaben|(?<![\d.])0\s+InMail-Guthaben|no InMail credits|out of InMail",
    re.IGNORECASE,
)
_DEGREE_RE = re.compile(r"[·•]\s*(1|2|3)\s*\.")
_THREAD_ID_RE = re.compile(r"/messaging/thread/([A-Za-z0-9_=-]+)")


def canon(text: str) -> str:
    if not isinstance(text, str):
        text = ""
    return re.sub(
        r"\s+", " ", unicodedata.normalize("NFC", text).replace(" ", " ")
    ).strip()


# Only LinkedIn's own edit markers, at the very end of a message. Anything
# else after the text is a different text, never "new text plus a marker".
# Either bracketed, or on its own last line: a sentence that merely ends in
# "... bearbeitet" is text, and stripping it would make a truncated old text
# look like the new one.
_EDIT_MARKER_RE = re.compile(
    r"(?:\s*[(\[]\s*(?:bearbeitet|edited)\s*[)\]]|\s*\n\s*(?:bearbeitet|edited))\s*$",
    re.IGNORECASE,
)


def strip_edit_marker(text: str) -> tuple[str, bool]:
    """Text without a trailing "(bearbeitet)"/"Edited" marker, and whether one was there."""
    raw = text if isinstance(text, str) else ""
    stripped = _EDIT_MARKER_RE.sub("", raw)
    return stripped, stripped != raw


def mark_edited(listed: dict[str, Any]) -> dict[str, Any]:
    """Strip the edit marker from every read message and record it as ``edited``.

    The marker is LinkedIn's decoration, not message text: kept, it made the
    prefill check fail and "X (bearbeitet)" -> "X" look like a change.
    """
    messages = listed.get("messages")
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict):
            continue
        text, edited = strip_edit_marker(message.get("text", ""))
        message["text"] = text.strip()
        message["edited"] = edited
    return listed


def _num(value: str | None) -> int | None:
    return int(re.sub(r"[.,]", "", value)) if value else None


def parse_credits(text: str) -> dict[str, Any]:
    """Credit facts from composer or inbox text; None where not shown."""
    use = _CREDITS_USE_RE.search(text or "")
    left = _CREDITS_LEFT_RE.search(text or "")
    cost = remaining = None
    if use:
        cost = _num(use.group(1) or use.group(3))
        remaining = _num(use.group(2) or use.group(4))
    if left and remaining is None:
        remaining = _num(next(g for g in left.groups() if g))
    return {
        "cost": cost,
        "remaining": remaining,
        "free": bool(_FREE_RE.search(text or "")) and cost is None,
        "none_left": bool(_NO_CREDITS_RE.search(text or "")) or remaining == 0,
    }


def credit_refusal(
    credits: dict[str, Any], *, allow_open_profile: bool = False
) -> str | None:
    """Status that stops the send on the composer's credit line; None to go.

    Measured cost is 1. A different cost, or fewer credits left than it
    costs, is not a send we have priced: stop rather than spend.

    An Open Profile stops the send unless the caller asked for it with
    *allow_open_profile*, and even then only when the line says "free" and
    names no cost and no exhausted balance. Every other case is unchanged.
    """
    if credits.get("free"):
        if (
            allow_open_profile
            and credits.get("cost") is None
            and not credits.get("none_left")
        ):
            return None
        return "open_profile"
    if credits.get("none_left"):
        return "no_inmail_credits"
    cost = credits.get("cost")
    if cost is None:
        # No credit line: an existing conversation or a connection. Never
        # send something whose kind we cannot name.
        return "not_an_inmail_composer"
    if cost != 1:
        return "unexpected_inmail_cost"
    remaining = credits.get("remaining")
    if remaining is not None and remaining < cost:
        return "no_inmail_credits"
    return None


_LOGIN_URL_RE = re.compile(r"/(?:login|checkpoint|uas/|authwall|signup)", re.IGNORECASE)
_LOGIN_TEXT_RE = re.compile(
    r"(?:Anmelden|Einloggen|Sign in)\b[\s\S]{0,400}(?:Passwort|Password)", re.IGNORECASE
)
_UPSELL_RE = re.compile(
    r"Sales Navigator\s+(?:kostenlos\s+)?testen|Try Sales Navigator"
    r"|Upgrade to Sales Navigator|kostenlosen Testzeitraum|free trial"
    r"|(?:Abo|Abonnement|Vertrag)\s+ist\s+abgelaufen|subscription (?:has )?expired"
    r"|Reaktivieren|Reactivate",
    re.IGNORECASE,
)
_INMAIL_WORD_RE = re.compile(r"InMail", re.IGNORECASE)


CREDITS_LINE_MAX = 80


def credits_lines(text: str) -> list[str]:
    """Short lines naming the remaining credits.

    Only lines up to 80 characters: a message preview that happens to say
    "50 InMail credits left" is longer than the header line (review 2026-10-13).
    """
    return [
        ln.strip()
        for ln in (text or "").split("\n")
        if len(ln.strip()) <= CREDITS_LINE_MAX and _CREDITS_LEFT_RE.search(ln)
    ]


def credits_line(text: str) -> str | None:
    """The one credits line of ``text``; None if none or if lines disagree."""
    lines = credits_lines(text)
    values = {parse_credits(ln)["remaining"] for ln in lines}
    return lines[0] if lines and len(values) == 1 else None


def diagnose_credits_page(url: str, title: str, text: str) -> str:
    """Why a credits line is missing: one reason code, never a guessed number.

    login_page (session lost), sales_nav_upsell (no or an expired Sales
    Navigator seat), not_sales_inbox (redirected elsewhere),
    inbox_not_rendered (almost no text after the wait), no_credits_line
    (inbox there, header wording unknown -- see credit_mentions).
    """
    url, title, text = url or "", title or "", text or ""
    if len({parse_credits(ln)["remaining"] for ln in credits_lines(text)}) > 1:
        return "ambiguous_credits_line"
    if _LOGIN_URL_RE.search(url) or _LOGIN_TEXT_RE.search(text[:3000]):
        return "login_page"
    if _UPSELL_RE.search(text[:5000]) or _UPSELL_RE.search(title):
        return "sales_nav_upsell"
    if "/sales/" not in url:
        return "not_sales_inbox"
    if len(text.strip()) < 200:
        return "inbox_not_rendered"
    return "no_credits_line"


def credit_mentions(text: str, cap: int = 3) -> list[str]:
    """Short lines mentioning InMail, to name a new header wording.

    Only lines up to 80 characters: a message preview is longer and is not
    what is looked for.
    """
    out: list[str] = []
    for ln in (text or "").split("\n"):
        ln = ln.strip()
        if ln and len(ln) <= 80 and _INMAIL_WORD_RE.search(ln) and ln not in out:
            out.append(ln)
            if len(out) >= cap:
                break
    return out


# -- sent view of the Sales Navigator inbox (read only) ----------------------

_SENT_LABEL_RE = re.compile(
    r"^\s*(Gesendet|Gesendete Nachrichten|Sent|Sent messages)\b", re.IGNORECASE
)
_SENT_LABEL_EXACT_RE = re.compile(
    r"^\s*(Gesendet|Gesendete Nachrichten|Sent|Sent messages)\s*$", re.IGNORECASE
)
_ACTIVITY_SUFFIX_RE = re.compile(
    r"\s+(?:war zuletzt\b.*\baktiv|ist (?:gerade )?aktiv|was (?:last )?active\b.*|is active(?: now)?)\.?\s*$",
    re.IGNORECASE,
)
_OWN_PREFIX_RE = re.compile(r"^\s*(Sie|You)\s*:\s*", re.IGNORECASE)
_TIME_RE = re.compile(
    r"^\s*(?:\d{1,2}[:.]\d{2}(?:\s*(?:AM|PM|Uhr))?"
    r"|\d{1,2}\.\s*\d{1,2}\.(?:\s*\d{2,4})?"
    r"|\d{1,2}\.?\s+(?:Jan|Feb|Mär|Mar|Apr|Mai|May|Jun|Jul|Aug|Sep|Okt|Oct|Nov|Dez|Dec)"
    r"[a-zä]*\.?(?:\s+\d{4})?"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:,\s*\d{4})?"
    r"|\d{1,2}/\d{1,2}/\d{2,4}"
    r"|Gestern|Heute|Yesterday|Today|Jetzt|Now"
    r"|Mo|Di|Mi|Do|Fr|Sa|So|Mon|Tue|Wed|Thu|Fri|Sat|Sun"
    r"|Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag"
    r"|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"
    r"|vor\s+\d+\s+\w+|\d+\s*(?:min|Min\.|h|Std\.|d|T\.|w|Wo\.)(?:\s+ago)?)\s*$",
    re.IGNORECASE,
)
_NOISE_LINES = {
    "inmail",
    "neu",
    "new",
    "ungelesen",
    "unread",
    "markiert",
    "starred",
    "angenommen",
    "accepted",
    "abgelehnt",
    "declined",
    "ausstehend",
    "pending",
}
SENT_SNIPPET_MAX = 200


def parse_sent_item(item: Any) -> dict[str, Any] | None:
    """One row of the sent view: recipient, time text, subject, text start.

    ``item`` is {"href", "lines"} from the list DOM. Fields the row does not
    show stay None. All time-like lines form ``when`` together ("Heute" plus
    "12:30"), so a second one never becomes the subject; the recipient is the
    first line that is not time-like. A subject comes from a line labelled as
    one (``subject_source`` "labelled") or, failing that, the line right
    before the ``Sie:``/``You:`` snippet ("line_before_snippet" -- a
    position guess, to be checked against the thread). ``own_last`` is true
    only when the snippet carries the own-message prefix.
    """
    if not isinstance(item, dict):
        return None
    lines = [canon(x) for x in item.get("lines") or [] if isinstance(x, str)]
    lines = [x for x in lines if x and x.lower() not in _NOISE_LINES]
    times = [x for x in lines if _TIME_RE.match(x)]
    other = [x for x in lines if not _TIME_RE.match(x)]
    if not other:
        return None
    name, body = other[0], other[1:]
    # 13.10. live: "Max Muster war zuletzt Vor 3 Tagen aktiv." -- der Aktivitaetshinweis haengt am Namen.
    name = _ACTIVITY_SUFFIX_RE.sub("", name).strip() or name
    when = " ".join(times) or None
    snippet_idx = next((i for i, x in enumerate(body) if _OWN_PREFIX_RE.match(x)), None)
    subject = subject_source = None
    for x in body:
        m = re.match(r"^\s*(?:Betreff|Subject)\s*:\s*(.+)$", x, re.IGNORECASE)
        if m:
            subject, subject_source = m.group(1).strip(), "labelled"
            break
    if subject is None and snippet_idx is not None and snippet_idx >= 1:
        subject, subject_source = body[snippet_idx - 1], "line_before_snippet"
    if snippet_idx is not None:
        snippet: str | None = body[snippet_idx]
    else:
        snippet = body[-1] if body else None
    own_last = bool(snippet and _OWN_PREFIX_RE.match(snippet))
    if snippet:
        snippet = _OWN_PREFIX_RE.sub("", snippet)[:SENT_SNIPPET_MAX]
    href = item.get("href") if isinstance(item.get("href"), str) else None
    if href and href.startswith("/"):
        href = "https://www.linkedin.com" + href
    return {
        "recipient": name,
        "when": when,
        "subject": subject,
        "subject_source": subject_source,
        "text_start": snippet,
        "own_last": own_last,
        "thread_url": href or None,
    }


_SENT_ITEMS_JS = r"""() => {
  const seen = new Set(); const out = [];
  const rows = document.querySelectorAll('ul li a[href*="/sales/inbox/"]');
  for (const a of rows) {
    const li = a.closest('li') || a;
    const href = a.getAttribute('href') || '';
    const key = href || li.innerText;
    if (seen.has(key)) continue; seen.add(key);
    const lines = (li.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
    if (lines.length) out.push({href, lines});
  }
  return out;
}"""

_SCROLL_LIST_JS = r"""() => {
  const a = document.querySelector('ul li a[href*="/sales/inbox/"]');
  let el = a ? a.closest('ul') : null;
  while (el && el !== document.body) {
    if (el.scrollHeight > el.clientHeight + 4) { el.scrollTop = el.scrollHeight; return true; }
    el = el.parentElement;
  }
  window.scrollTo(0, document.body.scrollHeight); return false;
}"""

# The filter dropdown above the conversation list; its label names the view.
_FILTER_TRIGGER: tuple[Any, ...] = (
    (
        "button[aria-haspopup], button[aria-expanded]",
        r"^\s*(Posteingang|Inbox|Alle Nachrichten|All messages|Gesendet|Sent)\b",
    ),
    ("button", r"^\s*(Posteingang|Inbox|Gesendet|Sent)\s*$"),
)
_MENU_LABELS_JS = """() => Array.from(document.querySelectorAll(
  '[role="menuitem"], [role="option"], [role="menuitemradio"], .artdeco-dropdown__content li'))
  .filter(e => e.offsetParent !== null)
  .map(e => (e.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 60))
  .filter(t => t)
  .concat(Array.from(document.querySelectorAll('select option')).map(o => 'select:' + (o.textContent || '').trim()).slice(0, 20))"""
_TRIGGER_HTML_JS = (
    """(el) => (el.outerHTML || '').replace(/\\s+/g, ' ').slice(0, 600)"""
)
_SENT_OPTION: tuple[Any, ...] = (
    (
        '[role="menuitem"], [role="option"], [role="menuitemradio"]',
        r"^\s*(Gesendet|Gesendete Nachrichten|Sent|Sent messages)\s*$",
    ),
    (
        ".artdeco-dropdown__content li, .artdeco-dropdown__content button",
        r"^\s*(Gesendet|Gesendete Nachrichten|Sent|Sent messages)\s*$",
    ),
)


def _row_keys(raw: Any) -> list[str]:
    """Identity of a list snapshot: the first rows' links (or text)."""
    keys = []
    for r in (raw if isinstance(raw, list) else [])[:10]:
        if isinstance(r, dict):
            lines = [x for x in r.get("lines") or [] if isinstance(x, str)]
            keys.append(f"{r.get('href') or ''}|{'|'.join(lines)}")
    return keys


def parse_degree(text: str) -> int | None:
    match = _DEGREE_RE.search(text or "")
    return int(match.group(1)) if match else None


def thread_url(thread: str) -> str:
    """Thread id or any thread URL -> canonical thread URL."""
    thread = (thread or "").strip()
    match = _THREAD_ID_RE.search(thread)
    tid = match.group(1) if match else thread
    if not re.fullmatch(r"[A-Za-z0-9_=-]{8,}", tid):
        raise ValueError("thread must be a LinkedIn thread URL or thread id")
    return f"https://www.linkedin.com/messaging/thread/{tid}/"


def pick_own_message(
    messages: list[dict[str, Any]], match: str | None
) -> dict[str, Any]:
    """Choose the target among own messages: the one containing *match*, else the last.

    Returns {"status": "ok", "message": …} or a refusal status. Ambiguity is
    refused, never resolved to the first candidate.
    """
    own = [m for m in messages or [] if isinstance(m, dict) and m.get("own") is True]
    if not own:
        return {"status": "no_own_message"}
    if not match:
        if own[-1].get("index") is None:
            # Without an index the edit cannot be bound to this message.
            return {"status": "message_without_index"}
        return {"status": "ok", "message": own[-1]}
    want = canon(strip_edit_marker(match)[0])
    hits = [m for m in own if want in canon(strip_edit_marker(m.get("text", ""))[0])]
    if not hits:
        return {"status": "message_not_found", "own_count": len(own)}
    if len(hits) > 1:
        return {
            "status": "ambiguous_match",
            "candidates": [h.get("index") for h in hits],
        }
    if hits[0].get("index") is None:
        return {"status": "message_without_index"}
    return {"status": "ok", "message": hits[0]}


def edit_landed(
    messages: list[dict[str, Any]], target: dict[str, Any], new_text: str
) -> bool:
    """True when the edited message itself now carries *new_text*.

    Only the message at the target's index counts. Matching any own message
    reported a short new text ("Danke") as landed because an older message
    already contained it.
    """
    want = canon(new_text)
    index = target.get("index") if isinstance(target, dict) else None
    if index is None:
        return False
    hit = next(
        (m for m in messages or [] if isinstance(m, dict) and m.get("index") == index),
        None,
    )
    if hit is None or hit.get("own") is not True:
        return False
    # LinkedIn may append an "(bearbeitet)"/"Edited" marker: only that marker
    # is removed, then the comparison is exact. The former prefix match let
    # the unedited old text pass whenever the new text was its beginning.
    got = canon(strip_edit_marker(hit.get("text", ""))[0])
    return got == want


_TOP_CARD_JS = r"""() => {
  const main = document.querySelector('main') || document.body;
  const sec = main.querySelector('section') || main;
  // The 2026 profile has no h1; the document title is "Name | LinkedIn".
  const fromTitle = (document.title || '').replace(/^\(\d+\)\s*/, '').split(' | ')[0].trim();
  const h1 = main.querySelector('h1');
  const anchor = [...sec.querySelectorAll('a[href*="/in/"]')].map(a => (a.innerText || '').trim()).find(Boolean);
  const name = (h1 && (h1.innerText || '').trim()) || (fromTitle !== 'LinkedIn' ? fromTitle : '') || anchor || '';
  return {name, text: (sec.innerText || '').slice(0, 1500), url: location.href};
}"""

_MENU_JS = r"""() => [...document.querySelectorAll('[role=menu] a, [role=menu] [role=menuitem], .artdeco-dropdown__content li, .artdeco-dropdown__content a')]
  .filter(e => e.getClientRects().length)
  .map(e => ({t: (e.innerText || '').trim(), h: e.getAttribute('href')}))
  .filter(x => x.t || x.h)"""

_SN_MESSAGE_LABEL = re.compile(
    r"^\s*(Nachricht( senden)?|InMail( senden)?|Message|Send (InMail|message))\s*$",
    re.I,
)
# The same button by aria-label: icon buttons and labels with the name in them
# ("Nachricht an <Name> senden", "Message <Name>") carry no exact text.
# "Nachrichten" and "Messaging" (the inbox) are cut off by the word boundary.
_SN_MESSAGE_ARIA = re.compile(
    r"^\s*(Nachricht|InMail|Message|Send (InMail|message))\b", re.I
)
# Positive evidence that this member cannot receive an InMail. Only this (or a
# disabled message button) turns a missing route into inmail_not_allowed
# (2026-10-08: a profile with a visible "Nachricht" button was refused merely
# because no button on the lead page matched the exact label).
_INMAIL_BLOCK_RE = re.compile(
    r"(keine InMails?\b[^\n]{0,40}(erhalten|empfangen|annehmen)"
    r"|InMail[^\n]{0,30}nicht (verfügbar|möglich)"
    r"|nimmt keine InMails"
    r"|(can(no|')t|cannot|does not|doesn't) (receive|accept) InMails?"
    r"|InMail[^\n]{0,30}(not available|unavailable))",
    re.I,
)
_SAVED_LEAD_RE = re.compile(
    r"^\s*(Nicht mehr in Sales Navigator speichern|Unsave( from Sales Navigator)?|Gespeichert|Saved)\s*$",
    re.I,
)
_URN_RE = re.compile(r"urn(?::|%3A)li(?::|%3A)fsd_profile(?::|%3A)(ACoA[A-Za-z0-9_-]+)")

# URNs bound to this profile: next to its own publicIdentifier in the page
# data, else in the top card. Sorted and distinct; the caller takes only a
# single one, never the first of several.
_OWN_URN_JS = r"""(slug) => {
  const out = new Set();
  const re = /urn(?::|%3A)li(?::|%3A)fsd_profile(?::|%3A)(ACoA[A-Za-z0-9_-]+)/gi;
  const key = ('"publicIdentifier":"' + slug + '"').toLowerCase();
  for (const c of document.querySelectorAll('code, script[type="application/json"]')) {
    const t = (c.textContent || '').replace(/\s*:\s*/g, ':');
    const low = t.toLowerCase();
    let i = low.indexOf(key);
    while (i >= 0) {
      const win = t.slice(Math.max(0, i - 1500), i + 1500);
      for (const u of win.matchAll(re)) out.add(u[1]);
      i = low.indexOf(key, i + 1);
    }
  }
  if (!out.size) {
    const main = document.querySelector('main') || document.body;
    const sec = main.querySelector('section') || main;
    for (const u of (sec.innerHTML || '').matchAll(re)) out.add(u[1]);
  }
  return [...out].sort();
}"""

_BUTTON_LABELS_JS = (
    "() => [...document.querySelectorAll('button, [role=button]')]"
    ".filter(b => b.getClientRects().length)"
    ".map(b => ({t: (b.innerText || '').trim(), a: (b.getAttribute('aria-label') || '').trim(),"
    " d: b.disabled === true || b.getAttribute('aria-disabled') === 'true'}))"
    ".filter(x => x.t || x.a).slice(0, 60)"
)

_TOP_BUTTONS_JS = (
    "() => { const main = document.querySelector('main') || document.body;"
    " const sec = main.querySelector('section') || main;"
    " return [...sec.querySelectorAll('button, [role=button]')]"
    ".filter(b => b.getClientRects().length)"
    ".map(b => ({t: (b.innerText || '').trim(), a: (b.getAttribute('aria-label') || '').trim()}))"
    ".filter(x => x.t || x.a); }"
)
_SN_DIALOG = 'section[role="dialog"][aria-label^="Unterhaltung mit"], section[role="dialog"][aria-label^="Conversation with"]'

_MESSAGES_JS = r"""() => {
  document.querySelectorAll('[data-ext-msg]').forEach(e => e.removeAttribute('data-ext-msg'));
  const out = [];
  const items = [...document.querySelectorAll('li.msg-s-message-list__event')];
  items.forEach((li, i) => {
    const bubbles = [...li.querySelectorAll('.msg-s-event-listitem')];
    const bub = bubbles[bubbles.length - 1];
    if (!bub) return;
    bub.setAttribute('data-ext-msg', String(i));
    const body = bub.querySelector('.msg-s-event-listitem__body, .msg-s-event__content');
    out.push({index: i, own: !bub.classList.contains('msg-s-event-listitem--other'),
              text: (body ? body.innerText : bub.innerText || '').trim()});
  });
  const title = document.querySelector('.msg-entity-lockup__entity-title, .msg-thread__link-to-profile, h2.msg-entity-lockup__entity-title');
  return {messages: out, partner: title ? ((title.innerText || '').trim().split(/\n/)[0].trim() || null) : null};
}"""

_EDIT_FORM = "form.msg-edit-form__base-form"

_REPLACE_EDITOR_JS = r"""(el, text) => {
  el.focus();
  const sel = window.getSelection();
  const range = document.createRange();
  range.selectNodeContents(el);
  sel.removeAllRanges(); sel.addRange(range);
  document.execCommand('delete', false);
  let ok = true;
  text.split('\n').forEach((line, i) => {
    if (i) ok = document.execCommand('insertParagraph', false) && ok;
    if (line) ok = document.execCommand('insertText', false, line) && ok;
  });
  return ok;
}"""

# Ordered fallback chains. Every entry is evaluated inside a scope already
# bound to our own message bubble, the open edit form or the InMail dialog, so
# a fallback can never reach a button belonging to somebody else's message.
# An entry is a CSS selector or ``(css, text_regex)``.
_OPTIONS_TRIGGER: tuple[Any, ...] = (
    "button.msg-s-event-listitem__options-trigger",
    'button[aria-label*="Optionen"]',
    'button[aria-label*="options" i]',
)
# Visible hits only: a hidden dropdown of another bubble may still hold a
# "Bearbeiten" entry, and clicking that would edit the wrong message.
_EDIT_MENU_ITEM: tuple[Any, ...] = (
    '.artdeco-dropdown__content :text-is("Bearbeiten"):visible',
    '.artdeco-dropdown__content :text-is("Edit"):visible',
    ('[role="menuitem"]:visible', r"^\s*(Bearbeiten|Edit)\s*$"),
)
_EDIT_CANCEL: tuple[Any, ...] = (
    "button.msg-edit-form__dismiss-button",
    'button[aria-label^="Abbrechen"]',
    'button[aria-label^="Cancel"]',
    ('button, [role="button"]', r"^\s*(Abbrechen|Cancel)\s*$"),
)
_EDIT_SAVE: tuple[Any, ...] = (
    "button.msg-edit-form__save-button",
    'button[aria-label^="Speichern"]',
    'button[aria-label^="Save"]',
    ('button, [role="button"]', r"^\s*(Speichern|Save)\s*$"),
)
_SN_SUBJECT: tuple[Any, ...] = (
    'input[aria-label^="Betreff"]',
    'input[aria-label^="Subject"]',
    'input[placeholder^="Betreff"]',
    'input[placeholder^="Subject"]',
    'input[name="subject"]',
)
_SN_SEND: tuple[Any, ...] = (
    ("button", r"^\s*(Senden|Send)\s*$"),
    'button[aria-label^="Senden"]',
    'button[aria-label^="Send "]',
    ('[role="button"]', r"^\s*(Senden|Send)\s*$"),
)


async def first_match(scope: Any, chain: tuple[Any, ...], *, last: bool = False) -> Any:
    """Locator of the first chain entry with a hit in ``scope``; else None.

    None instead of a dangling locator lets the caller answer with a status
    rather than run into a click timeout.
    """
    for entry in chain:
        if isinstance(entry, tuple):
            css, pattern = entry
            loc = scope.locator(css).filter(has_text=re.compile(pattern))
        else:
            loc = scope.locator(entry)
        try:
            if await loc.count():
                return loc.last if last else loc.first
        except Exception:
            logger.debug("selector %r failed", entry, exc_info=True)
    return None


_MESSAGE_BUTTON_POLL_SECONDS = 1.0
_UNSET: Any = object()  # balance_before not supplied: read it here


class ExtInmail(ExtActions):
    """InMail send and message edit. Every write sits behind ``confirm``."""

    async def _wait(self, low: float, high: float) -> None:
        await self._session.delay(random.uniform(low, high))

    # -- InMail ---------------------------------------------------------------

    async def inmail_target(self, username: str) -> dict[str, Any]:
        """Profile facts: name, degree and the Sales Navigator lead link."""
        await self._goto(f"https://www.linkedin.com/in/{username}/")
        card = await self._page.evaluate(_TOP_CARD_JS)
        degree = parse_degree(card.get("text", ""))
        result: dict[str, Any] = {
            "name": card.get("name") or None,
            "degree": degree,
            "profile_url": card.get("url"),
        }
        if degree == 1:
            return {**result, "status": "first_degree"}
        more = (
            self._page.locator("main section")
            .first.locator("button")
            .filter(has_text=re.compile(r"^\s*(Mehr|More)\s*$"))
            .first
        )
        if await more.count() == 0:
            return {**result, "status": "no_more_menu"}
        await more.click()
        await self._wait(1.0, 1.8)
        items = await self._page.evaluate(_MENU_JS)
        await self._page.keyboard.press("Escape")
        sn = next(
            (i["h"] for i in items if i.get("h") and "/sales/people/" in i["h"]), None
        )
        result["menu"] = [i["t"] for i in items if i.get("t")]
        top = await self._page.evaluate(_TOP_BUTTONS_JS)
        # A visible "Nachricht" in the top card of a 2nd/3rd-degree profile:
        # Open Profile or Premium InMail, a message without a connection.
        result["profile_message_button"] = any(
            _SN_MESSAGE_LABEL.match(b.get("t") or "") for b in top
        )
        if sn:
            return {**result, "status": "ok", "sales_url": sn, "route": "view_link"}
        # 2026-10-08: a lead already saved in Sales Navigator shows "Nicht mehr
        # in Sales Navigator speichern" / "Gespeichert" and no "view" link. It
        # is still a valid route: the lead page is addressed by the profile URN,
        # taken from the compose link in the menu or bound to the own slug.
        saved = any(_SAVED_LEAD_RE.match(t) for t in result["menu"]) or any(
            _SAVED_LEAD_RE.match(b.get("t") or "") for b in top
        )
        urns = sorted(
            {m.group(1) for i in items for m in [_URN_RE.search(i.get("h") or "")] if m}
        )
        route = "compose_link"
        if not urns and saved:
            urns = await self._page.evaluate(_OWN_URN_JS, username)
            route = "profile_urn"
        if saved:
            result["saved_lead"] = True
        if len(urns) == 1:
            # The surname check on the lead page in inmail() still guards
            # against a wrong recipient.
            return {
                **result,
                "status": "ok",
                "sales_url": f"https://www.linkedin.com/sales/people/{urns[0]},name",
                "route": route,
            }
        reason = (
            "ambiguous_profile_urn"
            if urns
            else "saved_lead_without_urn"
            if saved
            else "no_view_link"
        )
        return {**result, "status": "no_sales_navigator_route", "reason": reason}

    async def inmail(
        self,
        target: dict[str, Any],
        subject: str,
        body: str,
        *,
        confirm: bool,
        allow_open_profile: bool = False,
        balance_before: Any = _UNSET,
        defer_credit_check: bool = False,
    ) -> dict[str, Any]:
        """Open the Sales Navigator composer, fill it, then send or discard.

        *balance_before*: the inbox balance the caller already read (None =
        unknown). When given, the composer does not read the inbox itself --
        that page read used to sit inside the send deadline and ate it.
        *defer_credit_check*: for an Open Profile send, skip the inbox read
        after the click; the caller compares the balance later
        (credit_check="deferred"). Paid sends are not affected by either.
        """
        # True from the moment the send button is clicked: an exception before
        # it means nothing left, one after it means it may have.
        self.clicked = False
        try:
            return await self._inmail(
                target,
                subject,
                body,
                confirm=confirm,
                allow_open_profile=allow_open_profile,
                balance_before=balance_before,
                defer_credit_check=defer_credit_check,
            )
        except BaseException:
            # A filled composer must not survive an exception before the send
            # click: it would sit open on the shared page as a ready draft.
            if not self.clicked:
                await self._close_sn_dialog(self._page.locator(_SN_DIALOG).last)
            raise

    async def _inmail(
        self,
        target: dict[str, Any],
        subject: str,
        body: str,
        *,
        confirm: bool,
        allow_open_profile: bool = False,
        balance_before: Any = _UNSET,
        defer_credit_check: bool = False,
    ) -> dict[str, Any]:
        # A free composer line names no balance, so a free send could not be
        # checked against one. Read it from the inbox first (one page read,
        # only for a confirmed send that may go to an Open Profile) -- unless
        # the caller already read it outside the send deadline.
        read_here = balance_before is _UNSET
        if read_here or not (allow_open_profile and confirm):
            balance_before = None
        if allow_open_profile and confirm and read_here:
            try:
                # Inside the send deadline: a short poll, not the tool's 15 s.
                balance_before = (await self._credits_in_send()).get("remaining")
            except Exception:
                logger.warning("InMail balance before send unreadable", exc_info=True)
        await self._goto(target["sales_url"])
        await self._wait(3.0, 5.0)
        # The lead page renders its action bar late, and the label varies
        # ("Nachricht", "Nachricht senden", "Message", "Send InMail"): one
        # look after 3-5 s with an exact label refused every InMail of the
        # first live run as inmail_not_allowed. Poll up to 15 s, and when
        # nothing matches say which buttons were visible.
        by_text = (
            self._page.locator("button:visible")
            .filter(has_text=_SN_MESSAGE_LABEL)
            .first
        )
        button = None
        for _ in range(12):
            if await by_text.count() > 0:
                button = by_text
                break
            button = await self._message_button_by_aria()
            if button is not None:
                break
            await asyncio.sleep(_MESSAGE_BUTTON_POLL_SECONDS)
        if button is None or await button.is_disabled():
            return await self._no_message_button(target, disabled=button is not None)
        lead_text = await self._page.evaluate(
            "() => document.body.innerText.slice(0, 2500)"
        )
        if parse_degree(lead_text) == 1:
            return {"status": "first_degree", "sent": False}
        name = (target.get("name") or "").strip()
        last = name.split()[-1] if name else ""
        if last and last.casefold() not in lead_text.casefold():
            # The lead page must show the profile's surname; the URN fallback
            # route above must never reach another person.
            return {
                "status": "lead_mismatch",
                "sent": False,
                "expected": name,
                "page": self._page.url,
            }
        await button.click()
        dialog = self._page.locator(_SN_DIALOG).last
        try:
            await dialog.wait_for(state="visible", timeout=15000)
        except Exception:
            return {"status": "composer_not_opened", "sent": False}
        await self._wait(1.0, 2.0)
        text = await dialog.inner_text()
        # Only the header: the recipient panel below quotes their posts, and a
        # post saying "for free" must not read as an Open Profile.
        parts = re.split(r"Empfängerinformationen|Recipient information", text)
        header = parts[0]
        credits = parse_credits(header)
        # Without the panel marker the "header" is the whole dialog, and a
        # quoted post can say "for free": an Open Profile is then not proven,
        # so the opt-in does not apply and the send stops as before.
        if len(parts) < 2:
            allow_open_profile = False
        # An inbox that already shows no credits left counts as none_left:
        # the opt-in never sends against an exhausted balance.
        if balance_before == 0:
            allow_open_profile = False
        subject_field = await first_match(dialog, _SN_SUBJECT)
        body_field = dialog.locator('textarea[name="message"]').first
        base: dict[str, Any] = {"credits": credits, "composer": "sales_navigator"}

        async def discard(status: str, **extra: Any) -> dict[str, Any]:
            await self._close_sn_dialog(dialog)
            return {**base, "status": status, "sent": False, **extra}

        stop = credit_refusal(credits, allow_open_profile=allow_open_profile)
        if stop is not None:
            return await discard(stop)
        if credits.get("free"):
            # Only reachable with allow_open_profile and a "free" line without
            # cost: marked so the ledger and the caller can tell it apart.
            base["open_profile"] = True
        if subject_field is None or await body_field.count() == 0:
            return await discard("composer_fields_missing")
        await subject_field.fill(subject)
        await body_field.fill(body)
        await self._wait(0.8, 1.5)
        typed_subject = await subject_field.input_value()
        typed_body = await body_field.input_value()
        if canon(typed_subject) != canon(subject) or canon(typed_body) != canon(body):
            await subject_field.fill("")
            await body_field.fill("")
            return await discard("composer_mismatch")
        send = await first_match(dialog, _SN_SEND, last=True)
        if send is None or await send.is_disabled():
            await subject_field.fill("")
            await body_field.fill("")
            return await discard("send_button_unavailable")
        if not confirm:
            await subject_field.fill("")
            await body_field.fill("")
            return await discard("dry_run", composer_verified=True)
        self.clicked = True
        await send.click()
        # From here the InMail may have left: a failing read-back is
        # "unverified", never an exception that would leave the ledger at
        # "unknown" for a send that most likely happened.
        delivered = False
        sent_url = None
        credits_after: dict[str, Any] = {}
        try:
            await self._wait(4.0, 6.0)
            after = canon(await dialog.inner_text()) if await dialog.count() else ""
            delivered = canon(body)[:150] in after
            sent_url = self._page.url
            if base.get("open_profile") and defer_credit_check:
                # The caller compares the balance after its answer: an inbox
                # read here ran past the deadline and turned a sent message
                # into "unknown".
                base["credit_check"] = "deferred"
                base["credit_spent_despite_free"] = None
                return {
                    **base,
                    "status": "verified" if delivered else "unverified",
                    "sent": True,
                    "delivered_in_dialog": delivered,
                    "credits_after": {},
                    "url": sent_url,
                }
            credits_after = await self._credits_in_send()
        except Exception as exc:
            logger.warning("InMail read-back after send failed", exc_info=True)
            if base.get("open_profile"):
                # No balance after the send: whether a credit went is unknown.
                base["credit_check"] = "unavailable"
                base["credit_spent_despite_free"] = None
            return {
                **base,
                "status": "verified" if delivered else "unverified",
                "sent": True,
                "delivered_in_dialog": delivered,
                "credits_after": credits_after,
                "url": sent_url,
                "verify_error": f"{type(exc).__name__}: {exc}",
            }
        spent = (
            credits["remaining"] is not None
            and credits_after.get("remaining") is not None
            and credits_after["remaining"] < credits["remaining"]
        )
        if base.get("open_profile"):
            # Announced as free: compare the balance anyway and report a drop,
            # never hide it. Unknown on either side is said, not assumed.
            before = credits["remaining"]
            if before is None:
                before = balance_before
            after_left = credits_after.get("remaining")
            known = before is not None and after_left is not None
            base["credit_check"] = "measured" if known else "unavailable"
            base["credit_spent_despite_free"] = (after_left < before) if known else None
        return {
            **base,
            "status": "verified" if (delivered or spent) else "unverified",
            "sent": True,
            "delivered_in_dialog": delivered,
            "credits_after": credits_after,
            "url": sent_url,
        }

    async def _message_button_by_aria(self) -> Any:
        """Visible message button found by its aria-label, else None."""
        loc = self._page.locator(
            "button[aria-label]:visible, [role=button][aria-label]:visible"
        )
        try:
            n = await loc.count()
            for i in range(min(n, 60)):
                el = loc.nth(i)
                if _SN_MESSAGE_ARIA.match(await el.get_attribute("aria-label") or ""):
                    return el
        except Exception:
            logger.debug("aria message button lookup failed", exc_info=True)
        return None

    async def _no_message_button(
        self, target: dict[str, Any], *, disabled: bool
    ) -> dict[str, Any]:
        """inmail_not_allowed only on positive evidence; else a reason code."""
        buttons = await self._page.evaluate(_BUTTON_LABELS_JS)
        labels = [b.get("t") or b.get("a") for b in buttons]
        text = await self._page.evaluate("() => document.body.innerText.slice(0, 6000)")
        block = _INMAIL_BLOCK_RE.search(text or "")
        base = {"sent": False, "visible_buttons": labels[:40], "page": self._page.url}
        if block:
            return {**base, "status": "inmail_not_allowed", "evidence": block.group(0)}
        if disabled:
            return {
                **base,
                "status": "inmail_not_allowed",
                "evidence": "message_button_disabled",
            }
        upsell = any(
            re.search(
                r"Premium|Upgrade|Sales Navigator (testen|kostenlos)|Try Sales Navigator",
                x or "",
                re.I,
            )
            for x in labels
        )
        return {
            **base,
            "status": "message_button_not_found",
            "detail": {
                "profile_message_button": bool(target.get("profile_message_button")),
                "upsell_visible": upsell,
            },
        }

    async def _close_sn_dialog(self, dialog: Any) -> None:
        close = dialog.locator("button").filter(
            has_text=re.compile(r"schließen|Close conversation", re.IGNORECASE)
        )
        try:
            if await close.count():
                await close.first.click()
                await self._wait(0.8, 1.4)
                discard = self._page.locator(
                    '[role="alertdialog"] button:visible, [role="dialog"] button:visible'
                ).filter(has_text=re.compile(r"^\s*(Verwerfen|Discard)\s*$"))
                if await discard.count():
                    await discard.first.click()
        except Exception:
            logger.warning("closing the Sales Navigator composer failed", exc_info=True)

    async def _page_text(self) -> str:
        text = await self._page.evaluate(
            "() => document.body ? document.body.innerText : ''"
        )
        return text if isinstance(text, str) else ""

    async def _credits_in_send(self) -> dict[str, Any]:
        """inmail_credits inside the send deadline: a 5 s poll, not 15 s."""
        self._credits_poll_s = 5.0
        try:
            return await self.inmail_credits()
        finally:
            self._credits_poll_s = None

    async def inmail_credits(self, timeout_s: float | None = None) -> dict[str, Any]:
        """Remaining InMail credits from the Sales Navigator inbox header.

        Polls up to ``timeout_s`` for the credits line instead of one look
        after a fixed 3-5 s pause: from 2026-10-13 ~00:10 that look returned
        null repeatedly. Without a line the answer carries ``reason`` (see
        :func:`diagnose_credits_page`), the URL after navigation, the page
        title and short InMail lines -- never a silent null.
        """
        await self._goto(SN_INBOX_URL)
        if timeout_s is None:
            timeout_s = getattr(self, "_credits_poll_s", None) or 15.0
        deadline = time.monotonic() + max(timeout_s, 0.0)
        text = await self._page_text()
        line = credits_line(text)
        while line is None and time.monotonic() < deadline:
            await self._wait(0.8, 1.2)
            text = await self._page_text()
            line = credits_line(text)
        if line is not None:
            return {"remaining": parse_credits(line)["remaining"], "line": line}
        url = getattr(self._page, "url", "") or ""
        try:
            title = await self._page.title()
        except Exception:
            title = ""
        return {
            "remaining": None,
            "line": None,
            "reason": diagnose_credits_page(url, title, text),
            "url": url,
            "title": title,
            "credit_mentions": credit_mentions(text),
        }

    async def sent_inmails(self, limit: int = 30) -> dict[str, Any]:
        """Rows of the Sales Navigator inbox's sent view. Read only.

        Opens the inbox filter menu and picks "Gesendet"/"Sent" -- clicks that
        only change the view; nothing is typed, nothing is sent. Rows are
        returned only once the filter label reads the sent view; otherwise
        received threads could pass for sent ones, and the answer is a status
        without rows.
        """
        limit = max(1, min(int(limit), 200))
        await self._goto(SN_INBOX_URL)
        await self._wait(2.0, 3.0)
        url = lambda: getattr(self._page, "url", "") or ""  # noqa: E731

        def empty(status: str, **extra: Any) -> dict[str, Any]:
            return {
                "status": status,
                "items": [],
                "count": 0,
                "complete": False,
                "url": url(),
                **extra,
            }

        trigger = await first_match(self._page, _FILTER_TRIGGER)
        if trigger is None:
            return empty("filter_not_found")
        before = canon(await trigger.inner_text())
        switched = not _SENT_LABEL_RE.match(before)
        inbox_keys: list[str] = []
        if switched:
            # The inbox list must be loaded first, or the comparison below has
            # nothing to tell the sent list from (review 2026-10-13, round 2).
            for _ in range(6):
                inbox_keys = _row_keys(await self._page.evaluate(_SENT_ITEMS_JS) or [])
                if inbox_keys:
                    break
                await self._wait(1.0, 1.5)
            if not inbox_keys:
                return empty("inbox_list_not_loaded", filter_label=before)
            await trigger.click()
            await self._wait(0.8, 1.4)
            option = await first_match(self._page, _SENT_OPTION)
            menu_text = ""
            if option is None:
                # 13.10. live: das Sales-Navigator-Menue ("hue-menu") traegt keine Standardrollen. Gesucht wird
                # im Container, den der Ausloeser per aria-controls benennt, nach genau der Beschriftung.
                menu_id = await trigger.get_attribute("aria-controls")
                if menu_id and re.fullmatch(r"[A-Za-z0-9_-]+", menu_id):
                    menu = self._page.locator(f"#{menu_id}")
                    try:
                        menu_text = (await menu.inner_text(timeout=3000)).strip()
                    except Exception:
                        menu_text = ""
                    kandidat = menu.get_by_text(_SENT_LABEL_EXACT_RE)
                    if await kandidat.count() == 1:
                        option = kandidat.first
            if option is None:
                # 13.10. live: "sent_option_not_found" ohne jeden Hinweis, wie die Option wirklich heisst.
                # Die sichtbaren Menueeintraege (nur Beschriftungen, hoechstens 20) machen den Befund pruefbar.
                try:
                    angeboten = await self._page.evaluate(_MENU_LABELS_JS) or []
                except Exception:
                    angeboten = []
                try:
                    await self._page.keyboard.press("Escape")
                except Exception:
                    logger.debug("closing the filter menu failed", exc_info=True)
                try:
                    ausloeser = await trigger.evaluate(_TRIGGER_HTML_JS)
                except Exception:
                    ausloeser = ""
                return empty(
                    "sent_option_not_found",
                    filter_label=before,
                    offered=angeboten[:40],
                    trigger_html=ausloeser,
                    menu_text=menu_text[:600],
                )
            await option.click()
            await self._wait(2.0, 3.0)
        label_loc = await first_match(self._page, _FILTER_TRIGGER)
        label = canon(await label_loc.inner_text()) if label_loc is not None else ""
        if not _SENT_LABEL_RE.match(label):
            return empty("view_not_confirmed", filter_label=label)
        raw = await self._page.evaluate(_SENT_ITEMS_JS) or []
        # The label can switch before the list does (review 2026-10-13): rows
        # still identical to the inbox list are not taken as the sent view.
        rounds = 0
        while switched and inbox_keys and _row_keys(raw) == inbox_keys and rounds < 5:
            await self._wait(1.0, 1.5)
            raw = await self._page.evaluate(_SENT_ITEMS_JS) or []
            rounds += 1
        if switched and inbox_keys and _row_keys(raw) == inbox_keys:
            return empty(
                "view_not_confirmed", filter_label=label, reason="list_unchanged"
            )
        stalled = 0
        while len(raw) <= limit and stalled < 2:
            await self._page.evaluate(_SCROLL_LIST_JS)
            await self._wait(1.2, 2.0)
            more = await self._page.evaluate(_SENT_ITEMS_JS) or []
            if len(more) > len(raw):
                raw, stalled = more, 0
            else:
                stalled += 1
        items = [p for p in (parse_sent_item(r) for r in raw[:limit]) if p]
        hit_limit = len(raw) > limit
        return {
            "status": "ok",
            "view": "sent",
            "filter_label": label,
            "url": url(),
            "count": len(items),
            "items": items,
            # complete: the list stopped growing before the limit was reached.
            "complete": not hit_limit,
            "boundary": "limit" if hit_limit else "end_of_list",
        }

    # -- edit -----------------------------------------------------------------

    async def thread_messages(self, url: str) -> dict[str, Any]:
        await self._goto(url)
        await self._wait(2.0, 3.5)
        try:
            await self._page.locator("li.msg-s-message-list__event").first.wait_for(
                timeout=15000
            )
        except Exception:
            return {"messages": [], "partner": None}
        return mark_edited(await self._page.evaluate(_MESSAGES_JS))

    async def _open_menu(self, index: int) -> list[str]:
        bubble = self._page.locator(f'[data-ext-msg="{index}"]').first
        await bubble.scroll_into_view_if_needed()
        await bubble.hover()
        await self._wait(0.8, 1.4)
        trigger = await first_match(bubble, _OPTIONS_TRIGGER)
        if trigger is None:
            return []
        await trigger.click()
        await self._wait(0.8, 1.4)
        return [i["t"] for i in await self._page.evaluate(_MENU_JS) if i.get("t")]

    async def edit(
        self, url: str, message: dict[str, Any], new_text: str, *, confirm: bool
    ) -> dict[str, Any]:
        self.clicked = False
        try:
            return await self._edit(url, message, new_text, confirm=confirm)
        except BaseException:
            # An open menu or a half-replaced edit form must not stay on the
            # page: the next send would land in the edit form instead.
            if not self.clicked:
                await self._leave_edit_form()
            raise

    async def _leave_edit_form(self) -> None:
        try:
            form = self._page.locator(_EDIT_FORM).first
            cancel = (
                await first_match(form, _EDIT_CANCEL) if await form.count() else None
            )
            if cancel is not None:
                await cancel.click()
            else:
                await self._page.keyboard.press("Escape")
        except Exception:
            logger.warning("leaving the message edit form failed", exc_info=True)

    async def _edit(
        self, url: str, message: dict[str, Any], new_text: str, *, confirm: bool
    ) -> dict[str, Any]:
        if message.get("own") is False:
            return {"status": "not_own_message", "edited": False}
        menu = await self._open_menu(message["index"])
        if not menu:
            await self._page.keyboard.press("Escape")
            return {
                "status": "editor_mismatch",
                "edited": False,
                "reason": "no_options_trigger",
            }
        if not any(m in ("Bearbeiten", "Edit") for m in menu):
            await self._page.keyboard.press("Escape")
            return {"status": "edit_window_closed", "edited": False, "menu": menu}
        item = await first_match(self._page, _EDIT_MENU_ITEM)
        if item is None:
            await self._page.keyboard.press("Escape")
            return {
                "status": "editor_mismatch",
                "edited": False,
                "reason": "no_edit_menu_item",
            }
        await item.click()
        form = self._page.locator(_EDIT_FORM).first
        try:
            await form.wait_for(state="visible", timeout=10000)
        except Exception:
            return {"status": "edit_form_not_opened", "edited": False}
        editor = form.locator('[contenteditable="true"]').first
        cancel = await first_match(form, _EDIT_CANCEL)
        save = await first_match(form, _EDIT_SAVE)
        if cancel is None or await editor.count() == 0:
            # Without a way back the editor must not be touched at all.
            await self._page.keyboard.press("Escape")
            return {
                "status": "edit_form_mismatch",
                "edited": False,
                "reason": "no_cancel_or_editor",
            }
        prefilled = canon(strip_edit_marker(await editor.inner_text())[0])
        if prefilled != canon(strip_edit_marker(message["text"])[0]):
            await cancel.click()
            return {
                "status": "edit_form_mismatch",
                "edited": False,
                "prefilled": prefilled[:200],
            }
        inserted = await editor.evaluate(_REPLACE_EDITOR_JS, new_text)
        await self._wait(0.8, 1.4)
        typed = canon(await editor.inner_text())
        if not inserted or typed != canon(new_text):
            await cancel.click()
            return {"status": "editor_mismatch", "edited": False, "typed": typed[:200]}
        if save is None or await save.is_disabled():
            await cancel.click()
            return {"status": "save_button_unavailable", "edited": False}
        if not confirm:
            await cancel.click()
            await self._wait(0.8, 1.4)
            return {"status": "dry_run", "edited": False, "editor_verified": True}
        self.clicked = True
        await save.click()
        # The edit may have landed: a failing read-back is "unverified".
        try:
            await self._wait(3.0, 5.0)
            reread = await self.thread_messages(url)
            found = edit_landed(reread.get("messages", []), message, new_text)
        except Exception as exc:
            logger.warning("edit read-back after save failed", exc_info=True)
            return {
                "status": "unverified",
                "edited": True,
                "verified": False,
                "verify_error": f"{type(exc).__name__}: {exc}",
            }
        return {
            "status": "verified" if found else "unverified",
            "edited": True,
            "verified": found,
        }
