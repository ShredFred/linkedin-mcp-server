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
  // Options anywhere (the tag picker has no listbox), plus the chips of an
  // already chosen person: unlabelled dialog buttons with a picture.
  const chips = [...document.querySelectorAll('[role="dialog"] button')].filter(visible)
      .filter(b => !b.closest('[role="option"]') && !(b.getAttribute('aria-label') || '').trim()
                   && (b.innerText || '').trim() && b.querySelector('img, svg, figure'));
  return [...document.querySelectorAll('[role="option"]'), ...chips].filter(visible)
      .slice(0, 16).map(o => ({title: (o.innerText || '').split('\n')[0],
                               role: o.getAttribute('role') || 'chip', ids: mcpIds(o)}));
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


# -- step mode (2026-10-09): media, alt text, tags, identity switch -----------

STEP_ACTIONS = ("click", "upload", "type", "wait", "report", "page", "text", "html", "focushtml", "clear")
_MAX_STEPS = 12


def check_steps(steps: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """Validate a measurement script; refusal or None."""
    if steps is None:
        return None
    if not isinstance(steps, list) or not 1 <= len(steps) <= _MAX_STEPS:
        return {"status": "invalid_input", "field": "steps",
                "message": f"1-{_MAX_STEPS} steps."}
    for i, step in enumerate(steps):
        action = step.get("action") if isinstance(step, dict) else None
        if action not in STEP_ACTIONS:
            return {"status": "invalid_input", "field": f"steps[{i}].action"}
        if action in ("click", "upload"):
            bad = check_click_label(str(step.get("label") or ""))
            if bad or not step.get("label"):
                return bad or {"status": "invalid_input", "field": f"steps[{i}].label"}
        if action == "upload":
            files = step.get("files")
            if not isinstance(files, list) or not 1 <= len(files) <= 5:
                return {"status": "invalid_input", "field": f"steps[{i}].files"}
        if action == "type":
            text = str(step.get("text") or "")
            if not 1 <= len(text) <= 60 or "\n" in text:
                return {"status": "invalid_input", "field": f"steps[{i}].text",
                        "message": "1-60 characters, no line break (no Enter)."}
        if action == "wait" and not 0 < float(step.get("seconds") or 0) <= 30:
            return {"status": "invalid_input", "field": f"steps[{i}].seconds"}
    return None


# Mark the nth visible control with this exact aria-label or text; the
# forbidden wording is checked against the element's own words.
_MARK_NTH_JS = r"""(arg) => {
  document.querySelectorAll('[data-ext-probe]').forEach(e => e.removeAttribute('data-ext-probe'));
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const norm = v => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const want = norm(arg.label);
  const sel = 'button, [role="button"], [role="menuitem"], [role="combobox"], [role="option"], [role="radio"], [role="menuitemradio"], [role="textbox"], a, img, li';
  const same = v => arg.prefix ? norm(v).startsWith(want) : norm(v) === want;
  const dlg = arg.in_dialog ? [...document.querySelectorAll('[role="dialog"], dialog')].filter(visible)
      .filter(d => d.querySelector('[contenteditable="true"]')).shift() : null;
  const scope = arg.in_dialog ? (dlg || document.createElement('div')) : document;
  const hits = [...scope.querySelectorAll(sel)].filter(visible).filter(el =>
    same(el.getAttribute('aria-label')) || same(el.innerText) || same(el.getAttribute('alt')))
    .filter((e, _, all) => !all.some(o => o !== e && e.contains(o)));
  const el = hits[arg.nth || 0];
  if (!el) return {count: hits.length};
  const own = norm(el.getAttribute('aria-label')) + ' ' + norm(el.innerText);
  const bad = arg.forbidden.find(w => own.split(/[^a-zäöüß]+/).includes(w));
  if (bad) return {count: hits.length, refused: bad};
  el.setAttribute('data-ext-probe', '1');
  return {count: hits.length};
}"""

# Everything a media/identity measurement needs, per visible dialog and for
# any open menu: buttons, inputs, images, progress, live regions.
_DIALOG_REPORT_JS = r"""() => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const t = v => String(v || '').replace(/\s+/g, ' ').trim().slice(0, 100);
  const roots = [...document.querySelectorAll('[role="dialog"], [role="alertdialog"], dialog, [role="menu"], [role="listbox"]')].filter(visible);
  return roots.map(d => ({
    role: d.getAttribute('role') || d.tagName.toLowerCase(),
    label: t(d.getAttribute('aria-label')),
    heading: t((d.querySelector('h1, h2, h3') || {}).innerText),
    buttons: [...d.querySelectorAll('button, [role="button"], [role="menuitem"], [role="option"], [role="radio"], [role="menuitemradio"]')].filter(visible).slice(0, 40)
      .map(b => ({label: t(b.getAttribute('aria-label')), text: t(b.innerText),
                  role: b.getAttribute('role') || '', pressed: b.getAttribute('aria-pressed') || b.getAttribute('aria-checked') || '',
                  disabled: b.disabled === true || b.getAttribute('aria-disabled') === 'true'})),
    inputs: [...d.querySelectorAll('input, textarea, [contenteditable="true"]')].filter(visible).slice(0, 12)
      .map(i => ({tag: i.tagName.toLowerCase(), type: i.type || '', label: t(i.getAttribute('aria-label')),
                  placeholder: t(i.getAttribute('placeholder')), value: t(i.value || i.innerText),
                  maxlength: i.getAttribute('maxlength') || '', role: i.getAttribute('role') || ''})),
    images: [...d.querySelectorAll('img, video')].filter(visible).slice(0, 12)
      .map(i => ({tag: i.tagName.toLowerCase(), src: String(i.src || '').slice(0, 40), alt: t(i.alt),
                  w: i.width || i.videoWidth || 0, h: i.height || i.videoHeight || 0,
                  cls: String(i.className || '').slice(0, 80)})),
    progress: [...d.querySelectorAll('[role="progressbar"], progress')].filter(visible)
      .map(p => ({now: p.getAttribute('aria-valuenow') || '', label: t(p.getAttribute('aria-label'))})),
    status: [...d.querySelectorAll('[role="status"], [aria-live]')]
      .map(s => t(s.getAttribute('aria-label') || s.innerText)).filter(Boolean),
  }));
}"""


# Focus the one visible input/textarea whose placeholder or aria-label is this.
_FOCUS_INPUT_JS = r"""(want) => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const norm = v => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const hits = [...document.querySelectorAll('input, textarea, [contenteditable="true"]')].filter(visible)
      .filter(i => [i.getAttribute('placeholder'), i.getAttribute('aria-label')].some(v => norm(v) === norm(want)));
  if (hits.length !== 1) return hits.length;
  hits[0].focus();
  return 1;
}"""


_DIALOG_HTML_JS = r"""() => {
  const v = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const d = [...document.querySelectorAll('[role="dialog"], dialog')].filter(v).pop();
  return d ? d.outerHTML.replace(/<svg[\s\S]*?<\/svg>/g, '<svg/>').slice(0, 30000) : '';
}"""


_FOCUS_CARD_HTML_JS = r"""() => {
  let el = document.activeElement;
  for (let i = 0; el && i < 30; i++, el = el.parentElement) {
    if (el.getAttribute && (el.getAttribute('data-urn') || el.getAttribute('data-id'))) break;
  }
  const card = el || document.activeElement;
  return card ? card.outerHTML.replace(/<svg[\s\S]*?<\/svg>/g, '<svg/>')
      .replace(/src="data:[^"]*"/g, 'src="data:"').slice(-30000) : '';
}"""


async def _report(page: Any) -> Any:
    roots = await page.evaluate(_DIALOG_REPORT_JS)
    try:
        ids = await page.evaluate(_OPTION_IDS_JS, isolated_context=False)
    except Exception:  # noqa: BLE001 - measurement aid only
        ids = None
    if ids:
        roots.append({"role": "option_ids", "label": "", "heading": "", "buttons": [],
                      "inputs": [], "images": [], "progress": [], "status": [],
                      "ids": ids})
    return roots


async def run_probe_steps(probe: "ExtComposerProbe", url: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Run a bounded measurement script; always discards at the end."""
    page = probe._page
    out: dict[str, Any] = {"url": url, "steps": []}
    await probe._navigator._navigate_to_page(url)
    await probe._session.check_rate_limit()
    await probe._session.delay(3.0)
    try:
        for step in steps:
            action = step["action"]
            rec: dict[str, Any] = {"action": action, "label": step.get("label")}
            if action == "upload" and step.get("input"):
                # A dialog that shows its file input instead of a button.
                inputs = page.locator('[role="dialog"] input[type="file"], dialog input[type="file"]')
                rec["matches"] = await inputs.count()
                if rec["matches"] != 1:
                    rec["stopped"] = "file_input_not_unique"
                    out["steps"].append(rec)
                    out["status"] = "stopped"
                    return out
                await inputs.first.set_input_files([str(f) for f in step["files"]])
                await asyncio.sleep(float(step.get("settle") or 2.0))
                rec["report"] = await _report(page)
                out["steps"].append(rec)
                continue
            if action in ("click", "upload"):
                marked = await page.evaluate(
                    _MARK_NTH_JS,
                    {"label": step["label"], "nth": int(step.get("nth") or 0),
                     "prefix": bool(step.get("prefix")),
                     "in_dialog": bool(step.get("in_dialog")),
                     "forbidden": list(FORBIDDEN_CLICK)},
                )
                rec["matches"] = marked.get("count")
                if marked.get("refused") or marked.get("count", 0) <= int(step.get("nth") or 0):
                    rec["stopped"] = marked.get("refused") or "not_found"
                    rec["report"] = await _report(page)
                    out["steps"].append(rec)
                    out["status"] = "stopped"
                    return out
                if action == "upload":
                    async with page.expect_file_chooser(timeout=10_000) as info:
                        await page.click("[data-ext-probe]")
                    chooser = await info.value
                    await chooser.set_files([str(f) for f in step["files"]])
                    rec["files"] = len(step["files"])
                elif step.get("hover") or step.get("force"):
                    # Controls revealed on hover (reaction palette): hover
                    # only, never a click on the reaction button itself.
                    try:
                        await page.locator("[data-ext-probe]").first.scroll_into_view_if_needed()
                    except Exception:  # noqa: BLE001 - measurement aid
                        pass
                    if step.get("force"):
                        await page.click("[data-ext-probe]", force=True)
                    else:
                        await page.hover("[data-ext-probe]", force=True)
                else:
                    try:
                        await page.click("[data-ext-probe]")
                    except Exception as exc:  # noqa: BLE001 - a measurement records it
                        rec["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                        rec["report"] = await _report(page)
                        out["steps"].append(rec)
                        out["status"] = "stopped"
                        return out
                await asyncio.sleep(float(step.get("settle") or 1.5))
            elif action == "type":
                if step.get("into"):
                    rec["focused"] = await page.evaluate(_FOCUS_INPUT_JS, step["into"])
                await page.keyboard.type(step["text"], delay=80)
                await asyncio.sleep(1.5)
            elif action == "wait":
                await asyncio.sleep(float(step["seconds"]))
            elif action == "page":
                rec["elements"] = await page.evaluate(_REPORT_JS, 200)
            elif action == "html":
                # The last visible dialog's markup, attributes included: the
                # one place an identifier can hide that the reports skip.
                rec["html"] = await page.evaluate(_DIALOG_HTML_JS)
            elif action == "focushtml":
                # Markup of the card around the focused element (attributes
                # only matter: urn, actor, submit control).
                rec["html"] = await page.evaluate(_FOCUS_CARD_HTML_JS)
            elif action == "clear":
                # Empty the focused editor again (select all + delete): a typed
                # measurement must not stay behind as a saved draft.
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Delete")
                await asyncio.sleep(1.0)
                rec["left"] = await page.evaluate(
                    "() => { const a = document.activeElement; return a ? (a.innerText || a.value || '').trim() : null; }"
                )
            elif action == "text":
                rec["text"] = await page.evaluate(
                    "() => ((document.querySelector('main') || document.body).innerText || '').slice(0, 4000)"
                )
            rec["report"] = await _report(page)
            out["steps"].append(rec)
        out["status"] = "probed"
        return out
    finally:
        out["cleanup"] = await _discard_everything(page)


async def _discard_everything(page: Any) -> list[str]:
    """Close dialogs and confirm the discard prompt; never anything else."""
    arg = {
        "close": ["schließen", "close", "dismiss", "verwerfen", "discard", "abbrechen", "cancel"],
        "discard": ["verwerfen", "discard"],
        "forbidden": ["posten", "post", "senden", "send", "publish", "veröffentlichen",
                      "planen", "schedule", "speichern", "save"],
    }
    done = []
    for _ in range(6):
        try:
            got = await page.evaluate(_PROBE_DISCARD_JS, arg)
        except Exception as exc:  # noqa: BLE001
            done.append(f"error:{type(exc).__name__}")
            break
        done.append(got)
        if got == "nothing":
            break
        await asyncio.sleep(1.2)
    return done
