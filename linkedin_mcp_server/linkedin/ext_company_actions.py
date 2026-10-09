"""Fork extension: act as a company page -- role, identity, comment, react.

Measured 2026-10-09 (German interface, round 3; fixture
``tests/fixtures/company-actions-de-2026-10-09.json``):

* **Role.** ``/company/<id>/admin/settings/manage-admins/`` lists every page
  admin with a role ("Super-Admin", "Content-Admin"); the signed-in member's
  row carries the marker "Sie". The measured session was a Super-Admin. Pages
  the member may not manage are not readable there; then the role is reported
  as unreadable and only what the admin view itself offers decides.
* **Identity.** On the admin post list (``/company/<id>/admin/page-posts/
  published/``) every post card has a button "Bei Reaktionen auf diesen Beitrag
  Menü für den Identitätswechsel öffnen". It opens the modal "Kommentieren,
  reagieren und teilen Sie im Namen von" with one radio per identity
  (``#select-self`` and one for the page); the checked one is active. The
  modal is only read and closed, never saved -- switching would change a
  setting.
* **Comment as the page.** The card's own comment box there is a Quill editor
  whose placeholder reads "Kommentieren als <page> …"; its submit button reads
  "Kommentieren". The member-view post page has no such switch, which is why
  round 2 measured "no 'Kommentieren als'" -- that finding holds for the
  member view only.
* **React.** Member view: the reaction button's aria-label reads "Status des
  Reaktionsbuttons: <state>" ("Keine Reaktion"); hovering it shows the palette
  "Gefällt mir", "Applaus", "Unterstütze ich", "Wunderbar", "Inspirierend",
  "Lustig". Admin view: "Mit „Gefällt mir“ reagieren" on the card. Clicking a
  reaction was not measured (no reaction was set), so the read-back after the
  click compares the label before and after and reports ``unverified`` when it
  did not change.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from linkedin_mcp_server.linkedin.ext_composer_labels import words
from linkedin_mcp_server.linkedin.ext_mentions import MentionWriter, Segment, plain_text

MANAGE_ADMINS_URL = "https://www.linkedin.com/company/{page_id}/admin/settings/manage-admins/"
ADMIN_POSTS_URL = "https://www.linkedin.com/company/{page_id}/admin/page-posts/published/"
POST_URL = "https://www.linkedin.com/feed/update/urn:li:activity:{activity}/"

# Roles as the admin table writes them (measured: de). The canonical key is
# what the tools report and check.
ROLE_WORDS = {
    "super_admin": ["super-admin", "super admin", "superadmin"],
    "content_admin": ["content-admin", "content admin"],
    "analyst": ["analyst", "analyst:in", "analystin"],
    "curator": ["kurator", "kurator:in", "curator"],
}
# Who may post, comment and react as the page (LinkedIn's role model).
ACTING_ROLES = {"super_admin", "content_admin"}
SELF_MARKERS = {"sie", "you"}

# Reaction names (measured de palette; en is LinkedIn's wording, unmeasured).
REACTIONS = {
    "like": ["gefällt mir", "like"],
    "celebrate": ["applaus", "celebrate"],
    "support": ["unterstütze ich", "support"],
    "love": ["wunderbar", "love"],
    "insightful": ["inspirierend", "insightful"],
    "funny": ["lustig", "funny"],
}
_STATUS_PREFIX = re.compile(r"^(status des reaktionsbuttons|reaction button state)\s*:\s*", re.I)
_NO_REACTION = {"keine reaktion", "no reaction"}


def parse_own_role(text: str) -> str | None:
    """The signed-in member's role from the admin table's text; None if absent.

    The table repeats, per admin: name, a marker line ("Sie" for the member),
    the headline, then the role. The member's role is the first role word
    after the "Sie" marker and before the next admin's marker.
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        if line.strip("​   ").lower() not in SELF_MARKERS:
            continue
        for nxt in lines[i + 1 : i + 8]:
            low = nxt.lower()
            if low in {"direkter kontakt", "1st", "1."}:
                break
            for key, names in ROLE_WORDS.items():
                if low in names:
                    return key
    return None


def reaction_state(label: str | None) -> str | None:
    """The state part of "Status des Reaktionsbuttons: <state>"."""
    if not label:
        return None
    m = _STATUS_PREFIX.match(label.strip())
    return label.strip()[m.end():].strip().lower() if m else None


_TEXT_JS = "() => ((document.querySelector('main') || document.body).innerText || '')"

# The post card on the admin list by its activity urn, its identity button
# and its comment box. Exactly one of each or a count.
_ADMIN_CARD_JS = r"""(arg) => {
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const norm = v => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
  document.querySelectorAll('[data-ext-company-act]').forEach(e => e.removeAttribute('data-ext-company-act'));
  const cards = [...document.querySelectorAll('[data-urn="urn:li:activity:' + arg.activity + '"]')];
  if (cards.length !== 1) return {cards: cards.length};
  const card = cards[0];
  const pick = (els, tag) => { if (els.length === 1) els[0].setAttribute('data-ext-company-act', tag); return els.length; };
  const buttons = [...card.querySelectorAll('button')].filter(vis);
  const switches = buttons.filter(b => arg.switch_words.some(w => norm(b.getAttribute('aria-label')).includes(w)));
  const editors = [...card.querySelectorAll('.ql-editor[contenteditable="true"], [role="textbox"][contenteditable="true"]')].filter(vis);
  const like = buttons.filter(b => arg.like_labels.includes(norm(b.getAttribute('aria-label'))));
  const submit = buttons.filter(b => arg.submit_words.includes(norm(b.innerText)) && b.closest('form'));
  const placeholder = editors.length === 1 ? (editors[0].getAttribute('aria-placeholder') || editors[0].getAttribute('data-placeholder') || '') : null;
  return {cards: 1,
          switches: pick(switches, 'switch'), editors: pick(editors, 'editor'),
          likes: pick(like, 'like'), submits: pick(submit, 'submit'),
          like_label: like.length === 1 ? like[0].getAttribute('aria-label') : null,
          submit_disabled: submit.length === 1 ? (submit[0].disabled === true) : null,
          placeholder};
}"""

# The identity modal: which radio is checked, with its name.
_IDENTITY_JS = r"""() => {
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const dialogs = [...document.querySelectorAll('[role="dialog"], dialog')].filter(vis);
  const d = dialogs.find(x => x.querySelector('input[type="radio"][name="actorSelector"]'));
  if (!d) return {modal: false};
  const radios = [...d.querySelectorAll('input[type="radio"][name="actorSelector"]')];
  const rows = radios.map(r => {
    const row = r.closest('li') || r.parentElement;
    const title = row ? row.querySelector('.artdeco-entity-lockup__title') : null;
    return {id: r.id, self: r.id === 'select-self',
            checked: r.checked || r.getAttribute('aria-checked') === 'true',
            name: title ? title.innerText.trim() : ''};
  });
  return {modal: true, rows};
}"""

_CLOSE_MODAL_JS = r"""(arg) => {
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const norm = v => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const d = [...document.querySelectorAll('[role="dialog"], dialog')].filter(vis)
      .find(x => x.querySelector('input[type="radio"][name="actorSelector"]'));
  if (!d) return 'gone';
  const b = [...d.querySelectorAll('button')].filter(vis)
      .find(x => arg.close.includes(norm(x.getAttribute('aria-label'))));
  if (!b) return 'no_close';
  b.click();
  return 'closed';
}"""

# Member-view reaction button and the palette that hover opens.
_REACTION_JS = r"""(arg) => {
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const norm = v => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
  document.querySelectorAll('[data-ext-react]').forEach(e => e.removeAttribute('data-ext-react'));
  const status = [...document.querySelectorAll('button')].filter(vis)
      .filter(b => /^(status des reaktionsbuttons|reaction button state)\s*:/i.test(b.getAttribute('aria-label') || ''));
  if (status.length) status[0].setAttribute('data-ext-react', 'status');
  const palette = [...document.querySelectorAll('button')].filter(vis)
      .filter(b => arg.names.includes(norm(b.getAttribute('aria-label') || b.innerText))
                   && !/status/i.test(b.getAttribute('aria-label') || ''));
  if (palette.length === 1) palette[0].setAttribute('data-ext-react', 'pick');
  return {status: status.length, label: status.length ? status[0].getAttribute('aria-label') : null,
          palette: palette.length};
}"""


class ExtCompanyActions:
    def __init__(self, session: Any, navigator: Any, *, poll: float = 0.5):
        self._session = session
        self._navigator = navigator
        self._poll = poll
        self.clicked = False

    @property
    def _page(self) -> Any:
        return self._session.page

    @property
    def comment_submitted(self) -> bool:
        """The comment tool's click marker (set right before the submit)."""
        return self.clicked

    async def _goto(self, url: str) -> None:
        await self._navigator._navigate_to_page(url)
        await asyncio.sleep(self._poll * 4)

    # -- role and identity ------------------------------------------------

    async def read_access(self, page_id: str) -> dict[str, Any]:
        """Role of the signed-in member on the page, and what that allows."""
        await self._goto(MANAGE_ADMINS_URL.format(page_id=page_id))
        role = parse_own_role(await self._page.evaluate(_TEXT_JS))
        source = "manage_admins"
        if role is None:
            # Not readable (no right to manage admins, or not an admin): the
            # admin post list decides whether posting is offered at all.
            await self._goto(ADMIN_POSTS_URL.format(page_id=page_id))
            text = (await self._page.evaluate(_TEXT_JS)).lower()
            offered = any(w in text for w in words("start_post"))
            role = "admin_unreadable" if offered else "none"
            source = "admin_view"
        acting = role in ACTING_ROLES or role == "admin_unreadable"
        return {"status": "ok", "page_id": page_id, "role": role,
                "role_source": source, "can_act_as_page": acting}

    async def _admin_card(self, page_id: str, activity: str) -> dict[str, Any]:
        await self._goto(ADMIN_POSTS_URL.format(page_id=page_id))
        # The list renders its cards after the page: poll for the card.
        loop = asyncio.get_running_loop()
        end = loop.time() + 12.0
        while True:
            got = await self._admin_card_again(activity)
            if got.get("cards") == 1 or loop.time() >= end:
                return got
            await asyncio.sleep(self._poll * 2)

    async def _admin_card_first(self, activity: str) -> dict[str, Any]:
        return await self._page.evaluate(
            _ADMIN_CARD_JS,
            {"activity": activity,
             "switch_words": ["identitätswechsel", "switch identity", "identity"],
             "like_labels": ["mit „gefällt mir“ reagieren", "react with like", "like"],
             "submit_words": words("comment_submit")},
        ) or {}

    async def read_identity(self) -> dict[str, Any]:
        """Open the card's identity modal, read the checked identity, close."""
        await self._page.click('[data-ext-company-act="switch"]')
        await asyncio.sleep(self._poll * 3)
        got = await self._page.evaluate(_IDENTITY_JS) or {}
        closed = await self._page.evaluate(_CLOSE_MODAL_JS, {"close": words("close")})
        await asyncio.sleep(self._poll * 2)
        checked = [r for r in got.get("rows") or [] if r.get("checked")]
        return {"modal": got.get("modal", False), "closed": closed,
                "active": checked[0] if len(checked) == 1 else None,
                "rows": got.get("rows") or []}

    async def _identity_is_page(self, page_name: str) -> dict[str, Any] | None:
        ident = await self.read_identity()
        active = ident.get("active")
        if not ident.get("modal") or active is None:
            return {"status": "identity_unreadable", "identity": ident}
        if active.get("self") or active.get("name", "").strip().lower() != page_name.strip().lower():
            return {"status": "identity_not_page", "active": active.get("name"),
                    "message": "The page's identity switch is not on the page; it is "
                    "only read here, never changed."}
        if ident.get("closed") != "closed":
            return {"status": "identity_modal_stuck"}
        return None

    # -- comment as the page --------------------------------------------------

    async def comment_as_page(
        self, page_id: str, page_name: str, activity: str,
        segments: list[Segment], *, confirm: bool,
    ) -> dict[str, Any]:
        base: dict[str, Any] = {"as_company": page_id, "page": page_name, "posted": False}
        card = await self._admin_card(page_id, activity)
        if card.get("cards") != 1:
            return {**base, "status": "post_not_in_admin_view", "cards": card.get("cards"),
                    "message": "Only posts on the page's own admin list can be "
                    "commented as the page."}
        # The submit button only appears once text is in the box (measured).
        if card.get("switches") != 1 or card.get("editors") != 1:
            return {**base, "status": "admin_card_controls_unavailable", "found": card}
        if page_name.strip().lower() not in str(card.get("placeholder") or "").lower():
            return {**base, "status": "identity_not_confirmed",
                    "placeholder": card.get("placeholder")}
        wrong = await self._identity_is_page(page_name)
        if wrong:
            return {**base, **wrong}
        card = await self._page.evaluate(
            _ADMIN_CARD_JS,
            {"activity": activity,
             "switch_words": ["identitätswechsel", "switch identity", "identity"],
             "like_labels": [], "submit_words": words("comment_submit")},
        ) or {}
        await self._page.click('[data-ext-company-act="editor"]')
        wrote = await MentionWriter(self._page, '[data-ext-company-act="editor"]').write(
            segments
        )
        if wrote["status"] != "written":
            await self._clear()
            return {**base, **{k: v for k, v in wrote.items() if k != "mentions"}}
        text = plain_text(segments)
        loop = asyncio.get_running_loop()
        end = loop.time() + 6.0
        while True:
            card = await self._page.evaluate(
                _ADMIN_CARD_JS,
                {"activity": activity, "switch_words": [], "like_labels": [],
                 "submit_words": words("comment_submit")},
            ) or {}
            if card.get("submits") == 1 or loop.time() >= end:
                break
            await asyncio.sleep(self._poll)
        if card.get("submits") != 1 or card.get("submit_disabled"):
            await self._clear()
            return {**base, "status": "no_submit", "submits": card.get("submits")}
        if not confirm:
            await self._clear()
            return {**base, "status": "dry_run", "would_comment": text,
                    "identity": page_name, "mentions": wrote.get("mentions")}
        before = await self._comment_hits(activity, text, page_name)
        self.clicked = True
        await self._page.click('[data-ext-company-act="submit"]')
        await asyncio.sleep(self._poll * 8)
        after = await self._comment_hits(activity, text, page_name)
        verified = after > before
        return {**base, "posted": True, "verified": verified,
                "status": "posted" if verified else "unverified"}

    async def _comment_hits(self, activity: str, text: str, page_name: str) -> int:
        return await self._page.evaluate(
            r"""(arg) => {
              const card = document.querySelector('[data-urn="urn:li:activity:' + arg.activity + '"]');
              if (!card) return 0;
              const norm = v => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
              const want = norm(arg.text).slice(0, 80);
              return [...card.querySelectorAll('article, .comments-comment-entity, [data-id^="urn:li:comment"]')]
                .filter(c => norm(c.innerText).includes(want) && norm(c.innerText).includes(norm(arg.page)))
                .length;
            }""",
            {"activity": activity, "text": text, "page": page_name},
        )

    async def _clear(self) -> None:
        try:
            await self._page.click('[data-ext-company-act="editor"]')
            await self._page.keyboard.press("Control+A")
            await self._page.keyboard.press("Delete")
        except Exception:  # noqa: BLE001 - cleanup must not raise
            pass

    # -- reactions ------------------------------------------------------------

    async def react(
        self, activity: str, reaction: str, *, confirm: bool,
        page_id: str | None = None, page_name: str | None = None,
    ) -> dict[str, Any]:
        base: dict[str, Any] = {"activity_id": activity, "reaction": reaction, "reacted": False}
        if page_id:
            return await self._react_as_page(base, page_id, page_name or "", activity,
                                             reaction, confirm)
        await self._goto(POST_URL.format(activity=activity))
        names = REACTIONS[reaction]
        state = await self._page.evaluate(_REACTION_JS, {"names": names}) or {}
        if state.get("status") != 1:
            return {**base, "status": "reaction_control_unavailable", "found": state}
        before = reaction_state(state.get("label"))
        if before not in _NO_REACTION:
            return {**base, "status": "already_reacted", "state": before}
        # Hover opens the palette only on entry: start from outside.
        await self._page.mouse.move(1, 700)
        await self._page.hover('[data-ext-react="status"]')
        await asyncio.sleep(self._poll * 4)
        state = await self._page.evaluate(_REACTION_JS, {"names": names}) or {}
        if state.get("palette") != 1:
            await self._page.mouse.move(1, 700)
            return {**base, "status": "reaction_unavailable", "palette": state.get("palette")}
        if not confirm:
            await self._page.mouse.move(1, 700)
            return {**base, "status": "dry_run", "as": "member"}
        self.clicked = True
        await self._page.click('[data-ext-react="pick"]')
        await asyncio.sleep(self._poll * 6)
        after = await self._page.evaluate(_REACTION_JS, {"names": names}) or {}
        now = reaction_state(after.get("label"))
        verified = bool(now) and now not in _NO_REACTION and any(now == n for n in names)
        return {**base, "reacted": True, "state": now,
                "status": "verified" if verified else "unverified"}

    async def _react_as_page(
        self, base: dict[str, Any], page_id: str, page_name: str, activity: str,
        reaction: str, confirm: bool,
    ) -> dict[str, Any]:
        base = {**base, "as_company": page_id}
        if reaction != "like":
            return {**base, "status": "reaction_unmeasured_as_page",
                    "message": "Only 'like' was measured on the admin view."}
        card = await self._admin_card(page_id, activity)
        if card.get("cards") != 1:
            return {**base, "status": "post_not_in_admin_view", "cards": card.get("cards")}
        if card.get("switches") != 1:
            return {**base, "status": "admin_card_controls_unavailable", "found": card}
        if card.get("likes") != 1:
            return {**base, "status": "already_reacted_or_unknown", "found": card}
        wrong = await self._identity_is_page(page_name)
        if wrong:
            return {**base, **wrong}
        card = await self._admin_card_again(activity)
        if card.get("likes") != 1:
            return {**base, "status": "already_reacted_or_unknown"}
        if not confirm:
            return {**base, "status": "dry_run", "as": page_name}
        before = card.get("like_label")
        self.clicked = True
        await self._page.click('[data-ext-company-act="like"]')
        await asyncio.sleep(self._poll * 6)
        after = await self._admin_card_again(activity)
        changed = after.get("like_label") != before
        return {**base, "reacted": True,
                "status": "verified" if changed else "unverified",
                "label_after": after.get("like_label")}

    async def _admin_card_again(self, activity: str) -> dict[str, Any]:
        return await self._page.evaluate(
            _ADMIN_CARD_JS,
            {"activity": activity,
             "switch_words": ["identitätswechsel", "switch identity", "identity"],
             "like_labels": ["mit „gefällt mir“ reagieren", "react with like", "like"],
             "submit_words": words("comment_submit")},
        ) or {}
