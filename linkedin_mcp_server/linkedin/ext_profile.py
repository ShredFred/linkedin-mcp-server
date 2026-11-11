"""Fork extension: edit the signed-in member's own profile (2026-11-11).

Two surfaces, both measured on a live German profile before this was written:

* **Intro dialog** ``/in/<slug>/edit/intro/``: the custom profile button sits
  behind a control whose componentkey ends in ``_customButton_edit``; the two
  unlabelled text inputs that follow it in document order are the link URL and
  the link text. The headline is the ``role=textbox`` labelled
  "Profil-Slogan"/"Headline". The dialog's one "Speichern" saves everything.
* **Featured** ``/in/<slug>/details/featured/``: the overflow menu "Überlauf-Menü
  für Elemente im Fokus" offers "Link hinzufügen" (URL input, "Hinzufügen",
  then a form). Each item carries a button labelled "<title> löschen". A
  featured link's URL cannot be edited in place, so replacing one is
  add-new-then-delete-old: the section is never left without the link.

Order of checks, each a status instead of a click into nothing:

1. The signed-in member's slug (``/in/me/`` redirect) must equal the
   ``expected_slug`` the caller names -- the identity gate before any write.
2. The pre-image is read and returned (the tool layer stores it locally).
3. Unchanged values answer ``unchanged`` without touching anything.
4. Dry run fills nothing that is saved: the intro dialog is filled, compared
   and discarded; the featured form is reached and its fields reported, then
   discarded.
5. ``clicked`` is set right before the final click; the result is confirmed by
   reading the surface again (read-back), never by the click alone.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import quote

from linkedin_mcp_server.linkedin.ext_composer_labels import words
from linkedin_mcp_server.linkedin.ext_own_content import ExtOwnContent

logger = logging.getLogger(__name__)

PROFILE_URL = "https://www.linkedin.com/in/{slug}/"
INTRO_URL = "https://www.linkedin.com/in/{slug}/edit/intro/"
FEATURED_URL = "https://www.linkedin.com/in/{slug}/details/featured/"

SAVE_WORDS = words("save")
DISCARD_WORDS = words("discard")
CONFIRM_DELETE_WORDS = words("confirm_delete")
FEATURED_MENU_WORDS = [
    "überlauf-menü für elemente im fokus",
    "overflow menu for featured items",
]
ADD_LINK_WORDS = ["link hinzufügen", "add a link", "add link"]
ADD_WORDS = ["hinzufügen", "add"]
DELETE_SUFFIXES = (" löschen", " delete")
HEADLINE_LABELS = ["profil-slogan", "headline", "überschrift"]
TITLE_LABELS = ["überschrift", "titel", "title"]  # measured de: "Überschrift*"
DESCRIPTION_LABELS = ["beschreibung", "description"]

_VIS = r"""
  const vis = el => !!(el && el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden');
  const clearTag = tag => document.querySelectorAll('[data-ext-prof="' + tag + '"]')
      .forEach(e => e.removeAttribute('data-ext-prof'));
  const topDialog = () => {
    const d = [...document.querySelectorAll('[role="alertdialog"], [role="dialog"], dialog[open]')].filter(vis);
    return d[d.length - 1] || null;
  };
  const labelOf = el => {
    const own = (el.getAttribute('aria-label') || '').trim();
    if (own) return own;
    const by = el.getAttribute('aria-labelledby');
    if (by) { const t = by.split(/\s+/).map(i => document.getElementById(i)).filter(Boolean)
        .map(n => (n.innerText || '').trim()).join(' ').trim(); if (t) return t; }
    if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (l) return (l.innerText || '').trim(); }
    const wrap = el.closest('label'); return wrap ? (wrap.innerText || '').trim() : '';
  };
"""

# Intro dialog: custom button inputs and headline editor, tagged for fill().
_INTRO_FIELDS_JS = (
    "(arg) => {"
    + _VIS
    + r"""
  ['cb-url', 'cb-text', 'headline'].forEach(clearTag);
  const dlg = topDialog();
  if (!dlg) return {dialog: false};
  const anchors = [...dlg.querySelectorAll('[componentkey$="_customButton_edit"]')].filter(vis);
  const out = {dialog: true, button_anchor: anchors.length};
  if (anchors.length === 1) {
    const a = anchors[0];
    const after = [...dlg.querySelectorAll('input[type="text"], input:not([type])')].filter(vis)
        .filter(i => a.compareDocumentPosition(i) & Node.DOCUMENT_POSITION_FOLLOWING);
    if (after.length >= 2) {
      after[0].setAttribute('data-ext-prof', 'cb-url');
      after[1].setAttribute('data-ext-prof', 'cb-text');
      out.button_url = after[0].value; out.button_text = after[1].value;
    }
    out.button_inputs = after.length;
  }
  const heads = [...dlg.querySelectorAll('[role="textbox"], textarea')].filter(vis)
      .filter(e => arg.headline.includes(labelOf(e).toLowerCase()));
  out.headline_editors = heads.length;
  if (heads.length === 1) {
    heads[0].setAttribute('data-ext-prof', 'headline');
    out.headline = (heads[0].value !== undefined && heads[0].tagName === 'TEXTAREA')
        ? heads[0].value : (heads[0].innerText || '').trim();
  }
  return out;
}"""
)

# One visible control by exact (lower-case) text or aria-label; scope dialog/page/menu.
_PICK_JS = (
    "(arg) => {"
    + _VIS
    + r"""
  clearTag(arg.tag);
  let root = document;
  if (arg.scope === 'dialog') { root = topDialog(); if (!root) return {count: 0, scope: false}; }
  const sel = arg.scope === 'menu' ? '[role="menuitem"]' : 'button, [role="button"], a[role="menuitem"], [role="menuitem"]';
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  let hits = [...root.querySelectorAll(sel)].filter(vis).filter(b =>
      arg.words.includes(norm(b.innerText)) || arg.words.includes(norm(b.getAttribute('aria-label'))));
  hits = hits.filter(h => !hits.some(o => o !== h && h.contains(o)));
  if (hits.length === 1) hits[0].setAttribute('data-ext-prof', arg.tag);
  const b = hits[0];
  return {count: hits.length, scope: true,
          disabled: !!b && (b.disabled || b.getAttribute('aria-disabled') === 'true')};
}"""
)

# Featured: titles from the per-item delete buttons ("<title> löschen").
_FEATURED_TITLES_JS = (
    "(suffixes) => {"
    + _VIS
    + r"""
  const out = [];
  document.querySelectorAll('button[aria-label]').forEach(b => {
    if (!vis(b)) return;
    const l = (b.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
    for (const s of suffixes) if (l.toLowerCase().endsWith(s)) { out.push(l.slice(0, l.length - s.length).trim()); break; }
  });
  return out;
}"""
)

# The form fields of the topmost dialog: what the dry run reports.
_DIALOG_FORM_JS = (
    "(arg) => {"
    + _VIS
    + r"""
  ['f-url', 'f-title', 'f-desc'].forEach(clearTag);
  const dlg = topDialog();
  if (!dlg) return {dialog: false};
  const heading = ((dlg.querySelector('h1, h2, h3') || {}).innerText || '').trim();
  const fields = [...dlg.querySelectorAll('input, textarea, [role="textbox"]')].filter(vis)
      .filter(e => !['checkbox', 'radio', 'hidden', 'file'].includes((e.type || '').toLowerCase()))
      .map(e => ({el: e, label: labelOf(e), value: ('value' in e && e.tagName !== 'DIV') ? e.value : (e.innerText || '').trim()}));
  const pick = (labels, tag) => {
    const h = fields.filter(f => labels.some(l => f.label.toLowerCase().replace(/\*/g, '').trim().startsWith(l)));
    if (h.length === 1) h[0].el.setAttribute('data-ext-prof', tag);
    return h.length;
  };
  return {dialog: true, heading, text: (dlg.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 600),
          fields: fields.map(f => ({label: f.label.slice(0, 80), value: (f.value || '').slice(0, 200)})),
          title_fields: pick(arg.title, 'f-title'), desc_fields: pick(arg.desc, 'f-desc'),
          url_fields: pick(arg.url, 'f-url')};
}"""
)


class ExtProfileEditor(ExtOwnContent):
    """Edit the signed-in member's intro and featured links behind ``confirm``."""

    clicked = False

    async def _pick(self, js: str, arg: Any) -> Any:  # type: ignore[override]
        return await self._page.evaluate(js, arg)

    def _prof(self, tag: str) -> Any:
        return self._page.locator(f'[data-ext-prof="{tag}"]').first

    async def _click(self, scope: str, wanted: list[str], tag: str) -> dict[str, Any]:
        got = await self._pick(_PICK_JS, {"scope": scope, "words": wanted, "tag": tag})
        if got.get("count") != 1 or got.get("disabled"):
            return {"ok": False, **got}
        await self._prof(tag).click()
        await self._wait(0.8, 1.4)
        return {"ok": True}

    async def _dialog_gone(self, timeout: float = 15.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            got = await self._page.evaluate(
                "() => { " + _VIS + " return !!topDialog(); }"
            )
            if not got:
                return True
            await asyncio.sleep(0.5)
        return False

    async def _discard(self) -> None:
        """Close the topmost dialog without saving, answering a discard prompt."""
        try:
            closed = await self._click(
                "dialog", DISCARD_WORDS + ["schließen", "close", "dismiss"], "close"
            )
            if not closed["ok"]:
                await self._escape()
            await self._wait(0.6, 1.0)
            await self._click("dialog", DISCARD_WORDS, "discard")
        except Exception:
            logger.warning("discarding the profile dialog failed", exc_info=True)

    async def check_identity(self, expected_slug: str) -> dict[str, Any] | None:
        own = await self.own_slug()
        if own is None:
            return {"status": "own_identity_unknown"}
        if own != expected_slug.strip().lower():
            return {"status": "wrong_account", "signed_in": own}
        return None

    # -- intro: custom button + headline ---------------------------------------

    async def _open_intro(self, slug: str) -> dict[str, Any]:
        await self._goto(INTRO_URL.format(slug=quote(slug)))
        fields: dict[str, Any] = {}
        for _ in range(6):
            fields = await self._pick(_INTRO_FIELDS_JS, {"headline": HEADLINE_LABELS})
            if fields.get("dialog") and fields.get("button_inputs") is not None:
                break
            await asyncio.sleep(1.0)
        return fields

    async def read_intro(self, slug: str) -> dict[str, Any]:
        fields = await self._open_intro(slug)
        await self._discard()
        return fields

    async def edit_intro(
        self,
        slug: str,
        *,
        button_url: str | None,
        button_text: str | None,
        headline: str | None,
        confirm: bool,
    ) -> dict[str, Any]:
        fields = await self._open_intro(slug)
        if not fields.get("dialog"):
            return {"status": "intro_dialog_missing"}
        pre = {k: fields.get(k) for k in ("button_url", "button_text", "headline")}
        wants_button = button_url is not None or button_text is not None
        if wants_button and fields.get("button_anchor") != 1:
            await self._discard()
            return {
                "status": "custom_button_missing",
                "pre_image": pre,
                "probe": fields,
            }
        if wants_button and "button_url" not in fields:
            await self._discard()
            return {
                "status": "custom_button_inputs_missing",
                "pre_image": pre,
                "probe": fields,
            }
        if headline is not None and fields.get("headline_editors") != 1:
            await self._discard()
            return {
                "status": "headline_editor_missing",
                "pre_image": pre,
                "probe": fields,
            }
        target = {
            "button_url": button_url if button_url is not None else pre["button_url"],
            "button_text": button_text
            if button_text is not None
            else pre["button_text"],
            "headline": headline if headline is not None else pre["headline"],
        }
        if target == pre:
            await self._discard()
            return {"status": "unchanged", "pre_image": pre, "done": False}
        if button_url is not None and button_url != pre["button_url"]:
            await self._prof("cb-url").fill(button_url)
        if button_text is not None and button_text != pre["button_text"]:
            await self._prof("cb-text").fill(button_text)
        if headline is not None and headline != pre["headline"]:
            editor = self._prof("headline")
            await editor.click()
            await self._page.keyboard.press("Control+A")
            await self._page.keyboard.press("Delete")
            await self._page.keyboard.insert_text(headline)
        await self._wait(0.6, 1.0)
        now = await self._pick(_INTRO_FIELDS_JS, {"headline": HEADLINE_LABELS})
        filled = {k: now.get(k) for k in ("button_url", "button_text", "headline")}
        if filled != target:
            await self._discard()
            return {
                "status": "editor_mismatch",
                "pre_image": pre,
                "filled": filled,
                "wanted": target,
            }
        if not confirm:
            await self._discard()
            return {
                "status": "dry_run",
                "pre_image": pre,
                "would_set": target,
                "done": False,
            }
        save = await self._pick(
            _PICK_JS, {"scope": "dialog", "words": SAVE_WORDS, "tag": "save"}
        )
        if save.get("count") != 1 or save.get("disabled"):
            await self._discard()
            return {"status": "save_button_unavailable", "pre_image": pre, **save}
        self.clicked = True
        await self._prof("save").click()
        if not await self._dialog_gone():
            return {
                "status": "unverified",
                "detail": "dialog still open after save",
                "pre_image": pre,
                "done": True,
            }
        await self._wait(1.5, 2.5)
        back = await self.read_intro(slug)
        after = {k: back.get(k) for k in ("button_url", "button_text", "headline")}
        status = "verified" if after == target else "unverified"
        return {"status": status, "pre_image": pre, "after": after, "done": True}

    # -- featured links ----------------------------------------------------------

    async def featured_titles(self, slug: str) -> list[str]:
        await self._goto(FEATURED_URL.format(slug=quote(slug)))
        titles: list[str] = []
        for _ in range(5):
            titles = await self._page.evaluate(
                _FEATURED_TITLES_JS, list(DELETE_SUFFIXES)
            )
            if titles:
                break
            await asyncio.sleep(1.0)
        return titles

    async def _add_link(
        self, slug: str, url: str, title: str, confirm: bool
    ) -> dict[str, Any]:
        opened = await self._click("page", FEATURED_MENU_WORDS, "featured-menu")
        if not opened["ok"]:
            return {"status": "featured_menu_missing", **opened}
        item = await self._click("menu", ADD_LINK_WORDS, "add-link")
        if not item["ok"]:
            await self._escape()
            return {"status": "add_link_missing", **item}
        form = await self._pick(
            _DIALOG_FORM_JS,
            {"title": TITLE_LABELS, "desc": DESCRIPTION_LABELS, "url": ["link"]},
        )
        if form.get("url_fields") != 1:
            await self._discard()
            return {"status": "link_input_missing", "form": form}
        field = self._prof("f-url")
        await field.click()
        await field.fill("")
        await self._page.keyboard.insert_text(url)
        await self._wait(0.8, 1.2)
        add = await self._click("dialog", ADD_WORDS, "add")
        if not add["ok"]:
            await self._discard()
            return {"status": "add_button_unavailable", **add}
        form = {}
        for _ in range(20):  # the preview is fetched from the target page
            await asyncio.sleep(1.0)
            form = await self._pick(
                _DIALOG_FORM_JS,
                {
                    "title": TITLE_LABELS,
                    "desc": DESCRIPTION_LABELS,
                    "url": ["link zu artikel"],
                },
            )
            if form.get("title_fields") == 1:
                break
        if form.get("title_fields") != 1:
            await self._discard()
            return {"status": "title_field_missing", "form": form}
        title_el = self._prof("f-title")
        await title_el.fill(title)
        await self._wait(0.5, 0.9)
        form = await self._pick(
            _DIALOG_FORM_JS,
            {
                "title": TITLE_LABELS,
                "desc": DESCRIPTION_LABELS,
                "url": ["link zu artikel"],
            },
        )
        got_title = next(
            (
                f["value"]
                for f in form.get("fields", [])
                if f["label"]
                .lower()
                .replace("*", "")
                .strip()
                .startswith(tuple(TITLE_LABELS))
            ),
            None,
        )
        if got_title != title:
            await self._discard()
            return {"status": "editor_mismatch", "form": form}
        if not confirm:
            await self._discard()
            return {"status": "dry_run", "form": form}
        save = await self._pick(
            _PICK_JS, {"scope": "dialog", "words": SAVE_WORDS, "tag": "save"}
        )
        if save.get("count") != 1 or save.get("disabled"):
            await self._discard()
            return {"status": "save_button_unavailable", **save}
        self.clicked = True
        await self._prof("save").click()
        if not await self._dialog_gone():
            return {"status": "unverified", "detail": "dialog still open after save"}
        await self._wait(1.5, 2.5)
        titles = await self.featured_titles(slug)
        return {
            "status": "verified" if title in titles else "unverified",
            "titles_after": titles,
        }

    async def _delete_item(
        self, slug: str, title: str, confirm: bool
    ) -> dict[str, Any]:
        labels = [f"{title}{s}".lower() for s in DELETE_SUFFIXES]
        got = await self._pick(
            _PICK_JS, {"scope": "page", "words": labels, "tag": "item-delete"}
        )
        if got.get("count") != 1:
            return {"status": "featured_item_not_unique", **got}
        if not confirm:
            return {"status": "dry_run"}
        self.clicked = True
        await self._prof("item-delete").click()
        await self._wait(1.0, 1.6)
        conf = await self._pick(
            _PICK_JS,
            {"scope": "dialog", "words": CONFIRM_DELETE_WORDS, "tag": "confirm"},
        )
        if conf.get("count") == 1:
            await self._prof("confirm").click()
            await self._dialog_gone(10.0)
        await self._wait(1.5, 2.5)
        titles = await self.featured_titles(slug)
        return {
            "status": "verified" if title not in titles else "unverified",
            "titles_after": titles,
        }

    async def replace_featured_link(
        self, slug: str, *, old_title: str, new_url: str, new_title: str, confirm: bool
    ) -> dict[str, Any]:
        titles = await self.featured_titles(slug)
        pre = {"titles": titles}
        if titles.count(old_title) > 1 or titles.count(new_title) > 1:
            return {"status": "featured_item_not_unique", "pre_image": pre}
        has_old, has_new = old_title in titles, new_title in titles
        if not has_old and has_new:
            return {"status": "unchanged", "pre_image": pre, "done": False}
        if not has_old:
            return {"status": "featured_item_missing", "pre_image": pre}
        steps: dict[str, Any] = {}
        if not has_new:
            steps["add"] = await self._add_link(slug, new_url, new_title, confirm)
            if steps["add"]["status"] not in ("dry_run", "verified"):
                return {
                    "status": f"add_{steps['add']['status']}",
                    "pre_image": pre,
                    "steps": steps,
                    "done": self.clicked,
                }
            if confirm:
                await self.featured_titles(slug)
        steps["delete"] = await self._delete_item(slug, old_title, confirm)
        if not confirm:
            return {
                "status": "dry_run",
                "pre_image": pre,
                "steps": steps,
                "done": False,
            }
        ok = (
            steps["delete"]["status"] == "verified"
            and steps.get("add", {"status": "verified"})["status"] == "verified"
        )
        return {
            "status": "verified" if ok else "partial",
            "pre_image": pre,
            "steps": steps,
            "done": True,
        }
