"""Fork extension: read-only probe of a LinkedIn page's interactive controls.

Why this exists. ``create_company_post`` guesses nothing -- it demands exactly
one matching control and stops otherwise. That is right, but it made the first
productive attempt useless as a *measurement*: the composer at
``/feed/?shareActive=true`` turned out to carry no author control and no
schedule clock at all, and finding the entry point that does would have meant a
code change, a release and a client restart per attempt.

So the measurement gets its own tool. It navigates, optionally opens something
by an explicit label the caller names, and reports what is actually on screen.
It never publishes:

* only ``linkedin.com`` is navigated to;
* a click is performed only for a label the caller passes **and** that does not
  look like a publish, send, schedule-confirm or delete control -- the refusal
  list is checked against the caller's label *and* against the element's own
  text, so a renamed button cannot slip through;
* clicks happen only in the order the caller names, at most four per call.
  A sequence is needed because a dialog can only be reached through the
  control that opens it.
* typing is limited to one mention probe (``type_mention``, 2026-10-09): an
  ``@`` plus at most 30 name characters, typed key by key into the one
  visible composer editor, so the typeahead list can be measured. The editor
  is cleared and the composer discarded afterwards; no publish control is
  ever clicked (the discard only clicks close/discard wording).

The report is deliberately verbose: tag, role, aria-label, text, enabled state
and whether the element sits inside a dialog. That is what distinguishes "the
control is missing" from "the control is there under a name we did not try".
"""

from __future__ import annotations

import logging
import asyncio
import re
from typing import Any
from urllib.parse import urlparse

from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession

logger = logging.getLogger(__name__)

# Never clicked, whatever the caller asks for. Substring match, lowercase.
FORBIDDEN_CLICK = (
    "posten",
    "post",
    "veröffentlichen",
    "veroeffentlichen",
    "publish",
    "senden",
    "send",
    "planen",
    "schedule",
    "löschen",
    "loeschen",
    "delete",
    "entfernen",
    "remove",
    "bewerben",
    "apply",
    "folgen",
    "follow",
    "vernetzen",
    "connect",
)


def check_url(url: str) -> dict[str, Any] | None:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return {"status": "invalid_input", "field": "url"}
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return {
            "status": "invalid_input",
            "field": "url",
            "message": "Only linkedin.com is probed.",
        }
    return None


def check_click_label(label: str | None) -> dict[str, Any] | None:
    if label is None:
        return None
    low = label.strip().lower()
    if not low:
        return {"status": "invalid_input", "field": "click_label"}
    hit = next((w for w in FORBIDDEN_CLICK if w in low), None)
    if hit:
        return {
            "status": "refused_click",
            "message": f"The label contains {hit!r}; this probe never clicks a "
            "control that could publish, send, schedule or delete.",
        }
    return None


_MENTION_PROBE_RE = re.compile(r"^@[^\W\d_][\w .'-]{0,29}$")


def check_type_mention(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if not _MENTION_PROBE_RE.match(value):
        return {
            "status": "invalid_input",
            "field": "type_mention",
            "message": "type_mention must be '@' plus 1-30 name characters.",
        }
    return None


# The one visible composer editor (inside a dialog).
_MARK_EDITOR_JS = r"""() => {
  document.querySelectorAll('[data-ext-probe-editor]').forEach(e => e.removeAttribute('data-ext-probe-editor'));
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const norm = v => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
  // The composer is the dialog with a commit button; the messaging overlay is
  // a dialog with a textbox too (measured 2026-10-09 on the page admin view).
  const commit = ['posten', 'post', 'planen', 'schedule'];
  const composer = d => [...d.querySelectorAll('button')].filter(visible)
      .some(b => commit.includes(norm(b.innerText)));
  const hits = [...document.querySelectorAll('[role="dialog"] [contenteditable="true"], dialog [contenteditable="true"]')]
      .filter(visible).filter(e => !e.classList.contains('ql-clipboard'))
      .filter(e => composer(e.closest('[role="dialog"], dialog')))
      .filter((e, _, all) => !all.some(o => o !== e && o.contains(e)));
  if (!hits.length) {
    // No composer dialog: the one visible comment editor of a post page.
    const loose = [...document.querySelectorAll('[contenteditable="true"][role="textbox"]')]
        .filter(visible).filter(e => !e.closest('[role="dialog"], dialog'));
    if (loose.length === 1) hits.push(loose[0]);
  }
  if (hits.length !== 1) return {count: hits.length, seen: hits.map(e => ({
      label: e.getAttribute('aria-label') || '', componentkey: e.getAttribute('componentkey') || '',
      cls: String(e.className || '').slice(0, 60), w: e.offsetWidth, h: e.offsetHeight,
      dialog: (e.closest('[role="dialog"], dialog').innerText || '').slice(0, 80)}))};
  hits[0].setAttribute('data-ext-probe-editor', '1');
  hits[0].focus();
  return {count: 1, componentkey: hits[0].getAttribute('componentkey') || '',
          label: hits[0].getAttribute('aria-label') || ''};
}"""

# Everything that could be the suggestion list: ARIA listboxes, options,
# anything whose class/id/componentkey says typeahead or mention, plus loading
# indicators. Attributes are reported in full (bounded), because the anchor
# we need -- profile URL, URN, entity type -- may sit on any of them.
_TYPEAHEAD_REPORT_JS = r"""() => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const attrs = el => Object.fromEntries([...el.attributes]
      .filter(a => a.name !== 'style' && a.name !== 'd')
      .map(a => [a.name, String(a.value).slice(0, 160)]));
  const deep = el => [el, ...el.querySelectorAll('*')]
      .filter(e => [...e.attributes].some(a => /href|urn|entity|data-|aria-label|componentkey|src/.test(a.name)))
      .slice(0, 25).map(e => ({tag: e.tagName.toLowerCase(), attrs: attrs(e)}));
  const pick = '[role="listbox"], [role="option"], [class*="typeahead" i], [id*="typeahead" i],'
             + ' [class*="mention" i], [componentkey*="typeahead" i], [componentkey*="mention" i],'
             + ' [aria-busy="true"], [role="progressbar"], [aria-live]';
  const containers = [...document.querySelectorAll(pick)].filter(visible).slice(0, 40).map(el => ({
    tag: el.tagName.toLowerCase(), attrs: attrs(el),
    lines: (el.innerText || '').split('\n').map(s => s.trim()).filter(Boolean).slice(0, 12),
    in_dialog: !!el.closest('[role="dialog"], dialog'),
  }));
  const options = [...document.querySelectorAll('[role="option"]')].filter(visible).slice(0, 12)
      .map(o => ({lines: (o.innerText || '').split('\n').map(s => s.trim()).filter(Boolean),
                  attrs: attrs(o), parts: deep(o),
                  html: o.outerHTML.slice(0, 2500)}));
  const editor = document.querySelector('[data-ext-probe-editor]');
  return {containers, options,
          active: document.activeElement ? attrs(document.activeElement) : null,
          editor_html: editor ? editor.innerHTML.slice(0, 1500) : null};
}"""

# Tag one suggestion by position, for the measurement of the entity it
# inserts. The probe never decides anything with it.
_TAG_OPTION_JS = r"""(index) => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const opts = [...document.querySelectorAll('[role="listbox"] [role="option"]')].filter(visible);
  if (!opts[index]) return false;
  opts[index].setAttribute('data-ext-probe-option', '1');
  return true;
}"""

_IDS_JS = r"""
  const mcpIds = (root) => {
    const found = new Set();
    const seen = new WeakSet();
    const re = /urn:li:(?!digitalmediaAsset)[A-Za-z_]+:[A-Za-z0-9_-]+|\/(?:in|company)\/[A-Za-z0-9%_.-]+|ACoAA[A-Za-z0-9_-]{20,}/g;
    const walk = (v, depth) => {
      if (v == null || depth > 7 || found.size > 20) return;
      if (typeof v === 'string') { (v.match(re) || []).forEach(m => found.add(m)); return; }
      if (typeof v === 'number') return;
      if (typeof v !== 'object' || seen.has(v)) return;
      if (v instanceof Node || typeof v === 'function') return;
      seen.add(v);
      for (const k of Object.keys(v).slice(0, 60)) {
        if (k === 'children' || k === '_owner' || k.startsWith('_')) continue;
        try {
          const x = v[k];
          if (/entity|urn|^id$|companyid|organization/i.test(k) && (typeof x === 'string' || typeof x === 'number')) found.add(k + '=' + x);
          walk(x, depth + 1);
        } catch (e) {}
      }
    };
    for (const el of [root, ...root.querySelectorAll('*')].slice(0, 40)) {
      for (const k of Object.keys(el)) {
        if (k.startsWith('__reactProps')) walk(el[k], 0);
        if (k.startsWith('__reactFiber')) {
          let f = el[k];
          for (let i = 0; f && i < 25; i++, f = f.return) walk(f.memoizedProps, 0);
        }
      }
    }
    return [...found];
  };
"""

_OPTION_IDS_JS = "() => {" + _IDS_JS + r"""
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  return [...document.querySelectorAll('[role="listbox"] [role="option"]')].filter(visible)
      .slice(0, 12).map(o => ({title: (o.innerText || '').split('\n')[0], ids: mcpIds(o)}));
}"""

_EDITOR_STATE_JS = r"""() => {
  const editor = document.querySelector('[data-ext-probe-editor]');
  if (!editor) return null;
  const attrs = el => Object.fromEntries([...el.attributes]
      .map(a => [a.name, String(a.value).slice(0, 200)]));
  let doc = null;
  try { doc = editor.editor && editor.editor.getJSON ? JSON.stringify(editor.editor.getJSON()).slice(0, 3000) : null; } catch (e) { doc = 'error: ' + e; }
  let pm = null;
  try { pm = editor.pmViewDesc && editor.pmViewDesc.node ? JSON.stringify(editor.pmViewDesc.node.toJSON()).slice(0, 3000) : null; } catch (e) { pm = 'error: ' + e; }
  return {html: editor.innerHTML.slice(0, 3000), text: editor.innerText, tiptap: doc, pm: pm,
          editor_keys: Object.keys(editor).slice(0, 20),
          nodes: [...editor.querySelectorAll('*')].filter(e => e.attributes.length)
              .slice(0, 20).map(e => ({tag: e.tagName.toLowerCase(), attrs: attrs(e),
                                       text: (e.innerText || '').slice(0, 80)}))};
}"""

_DIALOG_BUTTONS_JS = r"""() => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  return [...document.querySelectorAll('[role="dialog"], [role="alertdialog"], dialog')].filter(visible)
      .map(d => [...d.querySelectorAll('button')].filter(visible)
          .map(b => ((b.getAttribute('aria-label') || '') + ' | ' + (b.innerText || '').trim()).slice(0, 80)));
}"""

_PROBE_CLEAR_JS = r"""() => {
  const editor = document.querySelector('[data-ext-probe-editor]');
  if (!editor) return 'no_editor';
  editor.focus();
  document.execCommand('selectAll', false);
  document.execCommand('delete', false);
  return (editor.innerText || '').trim() ? 'not_cleared' : 'cleared';
}"""

# Close the composer: only close/discard wording, never anything else.
_PROBE_DISCARD_JS = r"""(arg) => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const norm = v => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const buttons = [...document.querySelectorAll('[role="dialog"] button, [role="alertdialog"] button, dialog button')].filter(visible);
  const own = b => norm(b.getAttribute('aria-label')) + ' ' + norm(b.innerText);
  const safe = b => !arg.forbidden.some(w => own(b).split(/\s+/).includes(w));
  const hit = buttons.filter(b => safe(b) && (arg.close.includes(norm(b.getAttribute('aria-label')))
      || arg.discard.includes(norm(b.innerText))));
  if (!hit.length) return 'nothing';
  hit[hit.length - 1].click();
  return 'clicked';
}"""

# Report every visible interactive element. ``dialog`` tells a composer dialog
# apart from the page behind it, which is where the earlier guess went wrong.
_REPORT_JS = r"""(limit) => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const sel = 'button, [role="button"], [role="radio"], [role="option"], [role="menuitem"],'
            + ' [role="menuitemradio"], [role="combobox"], [contenteditable="true"], input, select';
  const text = el => (el.innerText || el.value || '').replace(/\s+/g, ' ').trim().slice(0, 90);
  return [...document.querySelectorAll(sel)].filter(visible).slice(0, limit).map(el => ({
    tag: el.tagName.toLowerCase(),
    role: el.getAttribute('role') || '',
    label: (el.getAttribute('aria-label') || '').slice(0, 90),
    text: text(el),
    type: (el.getAttribute('type') || '').slice(0, 20),
    componentkey: (el.getAttribute('componentkey') || '').slice(0, 60),
    disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
    dialog: !!el.closest('[role="dialog"], dialog'),
  }));
}"""

# Mark one element by exact aria-label or exact text. Exactly one or nothing.
_MARK_BY_LABEL_JS = r"""(arg) => {
  document.querySelectorAll('[data-ext-probe]').forEach(e => e.removeAttribute('data-ext-probe'));
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const norm = v => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const want = norm(arg.label);
  const sel = 'button, [role="button"], [role="menuitem"], [role="combobox"], a';
  const hits = [...document.querySelectorAll(sel)].filter(visible).filter(el =>
    norm(el.getAttribute('aria-label')) === want || norm(el.innerText) === want);
  if (hits.length !== 1) return {count: hits.length};
  const el = hits[0];
  // Second barrier: the element's own words, not only the caller's label.
  const own = norm(el.getAttribute('aria-label')) + ' ' + norm(el.innerText);
  const bad = arg.forbidden.find(w => own.includes(w));
  if (bad) return {count: 1, refused: bad};
  el.setAttribute('data-ext-probe', '1');
  return {count: 1};
}"""


class ExtComposerProbe:
    def __init__(self, session: PageSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator

    @property
    def _page(self) -> Any:
        return self._session.page

    async def probe(
        self,
        url: str,
        *,
        click_labels: list[str],
        limit: int,
        type_mention: str | None = None,
        pick_option: int | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"url": url, "clicked": []}
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        await self._session.delay(3.0)

        for label in click_labels:
            marked = await self._page.evaluate(
                _MARK_BY_LABEL_JS,
                {"label": label, "forbidden": list(FORBIDDEN_CLICK)},
            )
            if marked.get("refused"):
                result["status"] = "refused_click"
                result["stopped_at"] = label
                result["message"] = (
                    f"The element's own wording contains {marked['refused']!r}; "
                    "it was not clicked."
                )
                result["elements"] = await self._page.evaluate(_REPORT_JS, limit)
                return result
            if marked.get("count") != 1:
                result["status"] = "click_target_not_unique"
                result["stopped_at"] = label
                result["matches"] = marked.get("count")
                result["elements"] = await self._page.evaluate(_REPORT_JS, limit)
                return result
            await self._page.click("[data-ext-probe]")
            await self._session.delay(3.0)
            result["clicked"].append(label)

        if type_mention:
            result["typeahead"] = await self._measure_typeahead(
                type_mention, pick_option
            )

        result["status"] = "probed"
        result["current_url"] = self._page.url
        result["elements"] = await self._page.evaluate(_REPORT_JS, limit)
        return result

    async def _measure_typeahead(
        self, text: str, pick_option: int | None = None
    ) -> dict[str, Any]:
        out: dict[str, Any] = {"typed": text}
        editor = await self._page.evaluate(_MARK_EDITOR_JS)
        out["editor"] = editor
        if editor.get("count") != 1:
            out["status"] = "editor_not_unique"
            return out
        try:
            await self._page.keyboard.type(text, delay=120)
            # Snapshots over time: the list loads asynchronously, and a
            # snapshot taken too early is exactly the "not fully loaded"
            # state the mention writer has to recognise.
            snaps = []
            for wait in (0.3, 1.0, 2.5):
                await asyncio.sleep(wait)
                snaps.append(
                    {"after_s": wait, **await self._page.evaluate(_TYPEAHEAD_REPORT_JS)}
                )
            out["snapshots"] = snaps
            out["option_ids"] = await self._page.evaluate(
                _OPTION_IDS_JS, isolated_context=False
            )
            if pick_option is not None:
                if await self._page.evaluate(_TAG_OPTION_JS, pick_option):
                    await self._page.click("[data-ext-probe-option]")
                    await asyncio.sleep(1.0)
                    out["after_pick"] = await self._page.evaluate(
                        _EDITOR_STATE_JS, isolated_context=False
                    )
                else:
                    out["after_pick"] = "option_missing"
            out["status"] = "measured"
        finally:
            try:
                # No Escape: measured 2026-10-09, it is not needed to leave
                # the list and the clear below must still find the editor.
                out["clear"] = await self._page.evaluate(_PROBE_CLEAR_JS)
                arg = {
                    "close": ["schließen", "close", "dismiss", "verwerfen", "discard"],
                    "discard": ["verwerfen", "discard"],
                    "forbidden": ["posten", "post", "senden", "send", "publish",
                                  "veröffentlichen", "planen", "schedule"],
                }
                first = await self._page.evaluate(_PROBE_DISCARD_JS, arg)
                await asyncio.sleep(1.0)
                out["dialogs_after_close"] = await self._page.evaluate(_DIALOG_BUTTONS_JS)
                second = await self._page.evaluate(_PROBE_DISCARD_JS, arg)
                out["cleanup"] = f"{first}+{second}"
                await asyncio.sleep(1.0)
                out["dialogs_at_end"] = await self._page.evaluate(_DIALOG_BUTTONS_JS)
            except Exception as exc:  # noqa: BLE001 - cleanup must not raise
                out["cleanup_error"] = f"{type(exc).__name__}: {exc}"[:200]
        return out
