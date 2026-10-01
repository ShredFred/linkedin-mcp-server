"""MiViA fork: InMail through Sales Navigator, and editing an own sent message.

Measured 2026-09-30, headless, de locale, Jessica's account (Premium plus
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

import logging
import random
import re
from typing import Any

from linkedin_mcp_server.scraping.mivia_actions import MiviaActions

logger = logging.getLogger(__name__)

SN_INBOX_URL = "https://www.linkedin.com/sales/inbox/"
_CREDITS_USE_RE = re.compile(
    r"(\d+)\s+von\s+([\d.]+)\s+InMail-Guthaben|Use\s+(\d+)\s+of\s+([\d,]+)\s+InMail credits",
    re.IGNORECASE,
)
_CREDITS_LEFT_RE = re.compile(
    r"InMail-Guthaben:\s*([\d.]+)\s*verbleibend|([\d.]+)\s+InMail-Guthaben-Einheiten"
    r"|InMail credits:\s*([\d,]+)\s*(?:remaining|left)",
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
    return re.sub(r"\s+", " ", (text or "").replace(" ", " ")).strip()


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
    own = [m for m in messages if m.get("own")]
    if not own:
        return {"status": "no_own_message"}
    if not match:
        return {"status": "ok", "message": own[-1]}
    want = canon(match)
    hits = [m for m in own if want in canon(m.get("text", ""))]
    if not hits:
        return {"status": "message_not_found", "own_count": len(own)}
    if len(hits) > 1:
        return {"status": "ambiguous_match", "candidates": [h["index"] for h in hits]}
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
    hit = next((m for m in messages if m.get("index") == target.get("index")), None)
    if hit is None or not hit.get("own"):
        return False
    got = canon(hit.get("text", ""))
    # LinkedIn may append an "(bearbeitet)"/"Edited" marker after the text.
    return got == want or (got.startswith(want) and len(got) - len(want) <= 20)


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

_SN_DIALOG = 'section[role="dialog"][aria-label^="Unterhaltung mit"], section[role="dialog"][aria-label^="Conversation with"]'

_MESSAGES_JS = r"""() => {
  document.querySelectorAll('[data-mivia-msg]').forEach(e => e.removeAttribute('data-mivia-msg'));
  const out = [];
  const items = [...document.querySelectorAll('li.msg-s-message-list__event')];
  items.forEach((li, i) => {
    const bubbles = [...li.querySelectorAll('.msg-s-event-listitem')];
    const bub = bubbles[bubbles.length - 1];
    if (!bub) return;
    bub.setAttribute('data-mivia-msg', String(i));
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
_EDIT_MENU_ITEM: tuple[Any, ...] = (
    '.artdeco-dropdown__content :text-is("Bearbeiten")',
    '.artdeco-dropdown__content :text-is("Edit")',
    ('[role="menuitem"]', r"^\s*(Bearbeiten|Edit)\s*$"),
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


class MiviaInmail(MiviaActions):
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
        if not sn:
            return {**result, "status": "no_sales_navigator_route"}
        return {**result, "status": "ok", "sales_url": sn}

    async def inmail(
        self,
        target: dict[str, Any],
        subject: str,
        body: str,
        *,
        confirm: bool,
    ) -> dict[str, Any]:
        """Open the Sales Navigator composer, fill it, then send or discard."""
        await self._goto(target["sales_url"])
        await self._wait(3.0, 5.0)
        button = (
            self._page.locator("button:visible")
            .filter(has_text=re.compile(r"^\s*(Nachricht|Message)\s*$"))
            .first
        )
        if await button.count() == 0:
            return {"status": "inmail_not_allowed", "sent": False}
        lead_text = await self._page.evaluate(
            "() => document.body.innerText.slice(0, 2500)"
        )
        if parse_degree(lead_text) == 1:
            return {"status": "first_degree", "sent": False}
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
        header = re.split(r"Empfängerinformationen|Recipient information", text)[0]
        credits = parse_credits(header)
        subject_field = await first_match(dialog, _SN_SUBJECT)
        body_field = dialog.locator('textarea[name="message"]').first
        base = {"credits": credits, "composer": "sales_navigator"}

        async def discard(status: str, **extra: Any) -> dict[str, Any]:
            await self._close_sn_dialog(dialog)
            return {**base, "status": status, "sent": False, **extra}

        if credits["free"]:
            return await discard("open_profile")
        if credits["none_left"]:
            return await discard("no_inmail_credits")
        if credits["cost"] is None:
            # No credit line: an existing conversation or a connection. Never
            # send something whose kind we cannot name.
            return await discard("not_an_inmail_composer")
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
            credits_after = await self.inmail_credits()
        except Exception as exc:
            logger.warning("InMail read-back after send failed", exc_info=True)
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
        return {
            **base,
            "status": "verified" if (delivered or spent) else "unverified",
            "sent": True,
            "delivered_in_dialog": delivered,
            "credits_after": credits_after,
            "url": sent_url,
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

    async def inmail_credits(self) -> dict[str, Any]:
        """Remaining InMail credits from the Sales Navigator inbox header."""
        await self._goto(SN_INBOX_URL)
        await self._wait(3.0, 5.0)
        text = await self._page.evaluate("() => document.body.innerText")
        line = next(
            (ln.strip() for ln in text.split("\n") if _CREDITS_LEFT_RE.search(ln)), None
        )
        facts = parse_credits(line or "")
        return {"remaining": facts["remaining"], "line": line}

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
        return await self._page.evaluate(_MESSAGES_JS)

    async def _open_menu(self, index: int) -> list[str]:
        bubble = self._page.locator(f'[data-mivia-msg="{index}"]').first
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
        prefilled = canon(await editor.inner_text())
        if prefilled != canon(message["text"]):
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
