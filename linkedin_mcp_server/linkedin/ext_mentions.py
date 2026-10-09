"""Fork extension: @-mentions in a composer, written the way a hand writes them.

A mention is only a mention when the editor holds it as a *link entity* with
the right target. Text written with ``execCommand`` produces dead characters
(``@Name``) that notify nobody. So every mention goes through the typeahead:
type ``@`` and the name key by key, wait for the suggestion list to settle,
pick the one suggestion whose **identifier** equals the requested target,
click it, and then read the entity back out of the editor and compare its
target again. Plain text before and after a mention is still inserted with
``execCommand``.

**The barrier: a wrong person is worse than none.** A suggestion is picked only
when its identifier (profile id ``ACoA...`` or numeric organisation id) equals
the target. A matching display name alone never picks anything. Several
namesakes without an identifier, no match, or a list that did not settle stop
the write with a reason code; the first suggestion is never taken.

Markup in the text handed to the tools (decided 2026-10-09)::

    [[Erika Muster|erika-muster-0a1b2c]]            person by slug
    [[Erika Muster|https://www.linkedin.com/in/erika-muster-0a1b2c/]]
    [[Erika Muster|urn:li:fsd_profile:ACoAA...]]    person by URN / id
    [[Acme Labs|company:acme-labs]]                 company by slug
    [[Acme Labs|company:1000001]]                   company by id
    [[Acme Labs|urn:li:organization:1000001]]

A bare ``@word`` outside this markup is refused (``mention_markup_required``)
instead of being typed: it would post a plain-text @.

Measured 2026-10-09 on the German interface (fixture
``tests/fixtures/mention-typeahead-de-2026-10-09.json``):

* member composer: tiptap/ProseMirror. Every suggestion carries its target as a
  React prop ``id="mentionTypeahead_display_<entityID>"`` -- not as a DOM
  attribute, so it is read in the page's main world. The inserted node is
  ``{"type": "mention", "attrs": {"entityID": ...}}`` in the editor document.
* page composer (company admin view): Quill/Ember. Suggestions carry **no**
  identifier before the click. The inserted entity is
  ``a.ql-mention[data-entity-urn]``. Here exactly one namesake may be picked,
  and only because its target is read back and compared before anything can be
  published; two or more namesakes stop with ``mention_ambiguous``.

Any other editor (comment box, edit dialog of a different build) is detected
and refused with ``mention_unverifiable`` until it is measured.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

from linkedin_mcp_server.linkedin.ext_composer_labels import words

_MARKUP_RE = re.compile(r"\[\[([^\[\]|\n]{1,100})\|([^\[\]|\s]{1,300})\]\]")
# A handle-like bare @: not part of an e-mail address (``a@b.de``).
_BARE_AT_RE = re.compile(r"(?<![\w.])@(?=\w)")
_SLUG_RE = re.compile(r"^[A-Za-z0-9%._~-]{2,100}$")
_PROFILE_ID_RE = re.compile(r"^ACoA[A-Za-z0-9_-]{10,}$")
_PERSON_URN_RE = re.compile(r"^urn:li:fsd_profile:(ACoA[A-Za-z0-9_-]{10,})$")
_COMPANY_URN_RE = re.compile(r"^urn:li:(?:fsd_company|organization|company):(\d{1,15})$")
_URL_RE = re.compile(r"linkedin\.com/(in|company)/([^/?#]+)", re.IGNORECASE)


@dataclass
class Mention:
    name: str
    kind: str  # "person" | "company"
    slug: str | None = None
    entity_id: str | None = None  # ACoA... for a person, digits for a company

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "slug": self.slug,
            "entity_id": self.entity_id,
        }


Segment = tuple[str, Any]  # ("text", str) | ("mention", Mention)


def fold(value: str | None) -> str:
    """Case-, accent- and whitespace-insensitive key for name comparison."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip().casefold()


def canon(value: str | None) -> str:
    text = str(value or "").replace(" ", " ")
    text = re.sub(r"[ \t]*\n\s*", "\n", text)
    return text.strip()


def parse_target(name: str, raw: str) -> Mention:
    """One mention target; ValueError when it is not a LinkedIn person/company."""
    raw = unquote(raw.strip())
    if m := _PERSON_URN_RE.match(raw):
        return Mention(name, "person", entity_id=m.group(1))
    if _PROFILE_ID_RE.match(raw):
        return Mention(name, "person", entity_id=raw)
    if m := _COMPANY_URN_RE.match(raw):
        return Mention(name, "company", entity_id=m.group(1))
    if m := _URL_RE.search(raw):
        value = m.group(2)
        if m.group(1).lower() == "in":
            if _PROFILE_ID_RE.match(value):
                return Mention(name, "person", entity_id=value)
            return Mention(name, "person", slug=value.lower())
        if value.isdigit():
            return Mention(name, "company", entity_id=value)
        return Mention(name, "company", slug=value.lower())
    if raw.lower().startswith("company:"):
        value = raw.split(":", 1)[1]
        if value.isdigit():
            return Mention(name, "company", entity_id=value)
        if _SLUG_RE.match(value):
            return Mention(name, "company", slug=value.lower())
    elif ":" not in raw and "/" not in raw and _SLUG_RE.match(raw):
        return Mention(name, "person", slug=raw.lower())
    raise ValueError(f"not a LinkedIn person or company reference: {raw!r}")


def parse_mentions(text: str) -> tuple[list[Segment], dict[str, Any] | None]:
    """Split ``text`` into text and mention segments; (segments, refusal)."""
    segments: list[Segment] = []
    pos = 0
    for match in _MARKUP_RE.finditer(text):
        before = text[pos : match.start()]
        bad = _plain_refusal(before)
        if bad:
            return [], bad
        if before:
            segments.append(("text", before))
        name = match.group(1).strip()
        try:
            if not name:
                raise ValueError("empty display name")
            segments.append(("mention", parse_target(name, match.group(2))))
        except ValueError as exc:
            return [], {"status": "mention_syntax_invalid", "detail": str(exc)}
        pos = match.end()
    rest = text[pos:]
    bad = _plain_refusal(rest)
    if bad:
        return [], bad
    if rest:
        segments.append(("text", rest))
    return segments, None


def _plain_refusal(part: str) -> dict[str, Any] | None:
    if "[[" in part or "]]" in part:
        return {
            "status": "mention_syntax_invalid",
            "detail": part[max(0, part.find("[[")) :][:40],
            "message": "A mention reads [[Name|slug]] or [[Name|company:slug]].",
        }
    at = _BARE_AT_RE.search(part)
    if at:
        return {
            "status": "mention_markup_required",
            "detail": part[at.start() : at.start() + 30],
            "message": "A bare @ would post as plain text. Write a mention as "
            "[[Name|slug]] or [[Name|company:slug]].",
        }
    return None


def apply_mention_list(
    text: str, mentions: list[dict[str, str]]
) -> tuple[str, dict[str, Any] | None]:
    """Turn the first plain occurrence of each listed name into markup.

    ``mentions``: ``[{"name": ..., "target": slug | URL | URN}]``. A name that
    does not occur in the plain text is a refusal, not a silent skip.
    """
    out = text
    for item in mentions:
        name = str(item.get("name") or "").strip()
        target = str(item.get("target") or "").strip()
        if not name or not target:
            return text, {"status": "mention_syntax_invalid", "detail": item}
        try:
            parse_target(name, target)
        except ValueError as exc:
            return text, {"status": "mention_syntax_invalid", "detail": str(exc)}
        spans = [m.span() for m in _MARKUP_RE.finditer(out)]
        start = 0
        while True:
            at = out.find(name, start)
            if at < 0:
                return text, {"status": "mention_name_not_in_text", "mention": name}
            if not any(a <= at < b for a, b in spans):
                break
            start = at + 1
        out = out[:at] + f"[[{name}|{target}]]" + out[at + len(name) :]
    return out, None


def plaintext_names(segments: list[Segment], names: list[str]) -> list[str]:
    """Names (from the mentions or a list) that also stand as plain text."""
    plain = fold(" ".join(v for k, v in segments if k == "text"))
    return [n for n in dict.fromkeys(names) if n and fold(n) in plain]


def mentions_of(segments: list[Segment]) -> list[Mention]:
    return [value for kind, value in segments if kind == "mention"]


def plain_text(segments: list[Segment]) -> str:
    """The text as LinkedIn shows it: a mention reads as its name, no @."""
    return "".join(v if k == "text" else v.name for k, v in segments)


def prepare_text(
    text: str,
    mentions: list[dict[str, str]] | None = None,
    mention_check: str = "warn",
) -> tuple[list[Segment], dict[str, Any], dict[str, Any] | None]:
    """Parse markup and an optional mention list; (segments, info, refusal)."""
    if mention_check not in ("off", "warn", "strict"):
        return [], {}, {"status": "invalid_input", "field": "mention_check"}
    if mentions:
        text, bad = apply_mention_list(text, mentions)
        if bad:
            return [], {}, bad
    segments, bad = parse_mentions(text)
    if bad:
        return [], {}, bad
    found = mentions_of(segments)
    names = [m.name for m in found] + [str(i.get("name") or "") for i in mentions or []]
    loose = plaintext_names(segments, names) if mention_check != "off" else []
    info: dict[str, Any] = {"mentions": [m.describe() for m in found]}
    if loose:
        info["plaintext_names"] = loose
        if mention_check == "strict":
            return [], info, {
                "status": "mention_plaintext_name",
                "names": loose,
                "message": "A name to be mentioned also stands as plain text.",
            }
    return segments, info, None


# -- target resolution (before the composer opens) -----------------------------

# Profile id of the profile page we are on, bound to its own slug: the same
# lookup the InMail route uses (ext_inmail._OWN_URN_JS).
_PERSON_ID_JS = r"""(slug) => {
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
  return [...out].sort();
}"""

# Numeric id of the company page we are on, bound to its universalName.
# Not measured live (2026-10-09); a slug that does not resolve to exactly one
# id stops the write. Passing company:<id> skips this lookup.
_COMPANY_ID_JS = r"""(slug) => {
  const out = new Set();
  const re = /urn(?::|%3A)li(?::|%3A)(?:fsd_company|organization|company)(?::|%3A)(\d+)/gi;
  const key = ('"universalName":"' + slug + '"').toLowerCase();
  for (const c of document.querySelectorAll('code, script[type="application/json"]')) {
    const t = (c.textContent || '').replace(/\s*:\s*/g, ':');
    const low = t.toLowerCase();
    let i = low.indexOf(key);
    while (i >= 0) {
      const win = t.slice(Math.max(0, i - 600), i + 600);
      for (const u of win.matchAll(re)) out.add(u[1]);
      i = low.indexOf(key, i + 1);
    }
  }
  return [...out].sort();
}"""


async def resolve_targets(page: Any, navigate: Any, mentions: list[Mention]) -> dict[str, Any] | None:
    """Fill ``entity_id`` from the slug; a refusal unless exactly one id."""
    for m in mentions:
        if m.entity_id:
            continue
        if m.kind == "person":
            await navigate(f"https://www.linkedin.com/in/{m.slug}/")
            ids = await page.evaluate(_PERSON_ID_JS, m.slug)
        else:
            await navigate(f"https://www.linkedin.com/company/{m.slug}/")
            ids = await page.evaluate(_COMPANY_ID_JS, m.slug)
        if not isinstance(ids, list) or len(ids) != 1:
            return {
                "status": "mention_target_unresolved",
                "mention": m.name,
                "slug": m.slug,
                "found": len(ids) if isinstance(ids, list) else None,
                "message": "Pass the id instead (urn:li:fsd_profile:ACoA... or company:<id>).",
            }
        m.entity_id = str(ids[0])
    return None


# -- the editor ---------------------------------------------------------------

# Everything below runs in the page's main world (isolated_context=False): the
# tiptap instance and React props are JS properties, invisible to an isolated
# world.
_ENGINE_JS = r"""(sel) => {
  const el = document.querySelector(sel);
  if (!el) return 'missing';
  if (el.editor && typeof el.editor.getJSON === 'function') return 'tiptap';
  if (el.classList.contains('ql-editor')) return 'quill';
  return 'unknown';
}"""

_ENTITIES_JS = r"""(sel) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  if (el.editor && typeof el.editor.getJSON === 'function') {
    const out = [];
    const walk = n => {
      if (!n) return;
      if (n.type === 'mention') {
        const text = (n.content || []).map(c => c.text || '').join('');
        out.push({label: text, id: String((n.attrs || {}).entityID || '')});
      }
      (n.content || []).forEach(walk);
    };
    walk(el.editor.getJSON());
    return out;
  }
  return [...el.querySelectorAll('a.ql-mention, [data-test-ql-mention]')].map(a => {
    const urn = a.getAttribute('data-entity-urn') || '';
    const m = urn.match(/:(ACoA[A-Za-z0-9_-]+|\d+)$/);
    return {label: (a.innerText || a.textContent || '').trim(), id: m ? m[1] : ''};
  });
}"""

_STATE_JS = r"""(sel) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  return {text: el.innerText || '',
          pending: !!el.querySelector('.suggestion, [data-decoration-id]')};
}"""

_PREP_JS = r"""(arg) => {
  const canon = v => String(v || '').replace(/[ \t ]*\n[\s ]*/g, '\n').trim();
  const el = document.querySelector(arg.selector);
  if (!el) return 'missing';
  if (arg.clear) {
    el.focus();
    document.execCommand('selectAll', false);
    document.execCommand('delete', false);
  }
  if (arg.require_empty && canon(el.innerText)) return 'occupied';
  el.focus();
  if (document.activeElement !== el && !el.contains(document.activeElement)) return 'unfocused';
  const sel = window.getSelection();
  const range = document.createRange();
  range.selectNodeContents(el);
  range.collapse(false);
  sel.removeAllRanges(); sel.addRange(range);
  return 'ok';
}"""

_INSERT_JS = r"""(arg) => {
  const el = document.querySelector(arg.selector);
  if (!el) return 'missing';
  let text = arg.text;
  const tail = (el.innerText || '').replace(/\n+$/, '');
  if (/[  ]$/.test(tail) && text.startsWith(' ')) text = text.slice(1);
  let ok = true;
  text.split('\n').forEach((line, index) => {
    if (index > 0) ok = document.execCommand('insertParagraph', false) === true && ok;
    if (line) ok = document.execCommand('insertText', false, line) === true && ok;
  });
  return ok ? 'ok' : 'unsupported';
}"""

# Suggestions with title, subtitle, kind and -- where the build exposes it --
# the target id (React prop id="mentionTypeahead_display_<id>", measured).
_OPTIONS_JS = r"""(arg) => {
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const norm = v => String(v || '').replace(/\s+/g, ' ').trim();
  const idOf = root => {
    const want = /^mentionTypeahead_display_(.+)$/;
    for (const el of [root, ...root.querySelectorAll('*')].slice(0, 60)) {
      const urn = el.getAttribute && el.getAttribute('data-entity-urn');
      if (urn) { const m = urn.match(/:(ACoA[A-Za-z0-9_-]+|\d+)$/); if (m) return m[1]; }
      for (const k of Object.keys(el)) {
        if (k.startsWith('__reactProps')) {
          const id = el[k] && el[k].id;
          if (typeof id === 'string' && want.test(id)) return id.match(want)[1];
        }
        if (k.startsWith('__reactFiber')) {
          let f = el[k];
          for (let i = 0; f && i < 25; i++, f = f.return) {
            const id = f.memoizedProps && f.memoizedProps.id;
            if (typeof id === 'string' && want.test(id)) return id.match(want)[1];
          }
        }
      }
    }
    return null;
  };
  const status = [...document.querySelectorAll('[role="status"]')]
      .map(s => norm(s.getAttribute('aria-label') || s.innerText)).filter(Boolean);
  const boxes = [...document.querySelectorAll('[role="listbox"]')].filter(vis);
  const options = boxes.flatMap(b => [...b.querySelectorAll('[role="option"]')]).filter(vis)
    .map((o, index) => {
      const title = o.querySelector('.search-typeahead-v2__hit-text');
      const sub = o.querySelector('.search-typeahead-v2__hit-subtext');
      const lines = (o.innerText || '').split('\n').map(norm).filter(Boolean);
      const subtitle = sub ? norm(sub.innerText) : (lines[1] || '');
      let kind = null;
      if (o.querySelector('svg[id^="company-"]')
          || arg.company_words.some(w => subtitle.toLowerCase().startsWith(w))) kind = 'company';
      else if (o.querySelector('svg[id^="person-"]') || /^\d\.\+?\s*·/.test(subtitle)) kind = 'person';
      return {index, title: title ? norm(title.innerText) : (lines[0] || ''),
              subtitle, kind, entity: idOf(o)};
    });
  return {listbox: boxes.length, live: status, options};
}"""

_TAG_OPTION_JS = r"""(index) => {
  document.querySelectorAll('[data-ext-mention]').forEach(e => e.removeAttribute('data-ext-mention'));
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const opts = [...document.querySelectorAll('[role="listbox"]')].filter(vis)
      .flatMap(b => [...b.querySelectorAll('[role="option"]')]).filter(vis);
  if (!opts[index]) return false;
  opts[index].setAttribute('data-ext-mention', 'pick');
  return true;
}"""


def choose_option(
    options: list[dict[str, Any]], mention: Mention
) -> dict[str, Any]:
    """The suggestion for ``mention``, decided by identifier, never by position.

    Returns ``{"status": "ok", "index", "verify_only"}`` or a refusal.
    ``verify_only`` means the build shows no identifiers at all and exactly one
    namesake is offered: it may be inserted, and the inserted entity's target
    is what decides (read back before anything can be published).
    """
    want = fold(mention.name)
    kind_ok = [o for o in options if o.get("kind") in (mention.kind, None)]
    hits = [o for o in kind_ok if o.get("entity") and o["entity"] == mention.entity_id]
    if len(hits) > 1:
        return {"status": "mention_ambiguous", "mention": mention.name, "count": len(hits)}
    if hits:
        if fold(hits[0].get("title")) != want:
            return {
                "status": "mention_name_mismatch",
                "mention": mention.name,
                "shown": hits[0].get("title"),
            }
        return {"status": "ok", "index": hits[0]["index"], "verify_only": False}
    namesakes = [o for o in kind_ok if fold(o.get("title")) == want]
    blind = [o for o in namesakes if not o.get("entity")]
    if any(o.get("entity") for o in options) or not blind:
        # The list does show identifiers and none is the target, or no namesake.
        return {
            "status": "mention_not_resolved",
            "mention": mention.name,
            "seen": [o.get("title") for o in options][:8],
        }
    if len(blind) > 1:
        return {"status": "mention_ambiguous", "mention": mention.name, "count": len(blind)}
    return {"status": "ok", "index": blind[0]["index"], "verify_only": True}


class MentionWriter:
    """Write segments into one contenteditable editor, mentions via typeahead."""

    def __init__(
        self,
        page: Any,
        selector: str,
        *,
        list_timeout: float = 8.0,
        poll: float = 0.5,
        settle_polls: int = 2,
    ) -> None:
        self._page = page
        self._selector = selector
        self._timeout = list_timeout
        self._poll = poll
        self._settle = settle_polls

    async def _js(self, script: str, arg: Any = None) -> Any:
        return await self._page.evaluate(script, arg, isolated_context=False)

    async def _settled_options(self, mention: Mention, engine: str) -> dict[str, Any]:
        """Poll until the list is present and unchanged for ``settle_polls``."""
        loop = asyncio.get_running_loop()
        end = loop.time() + self._timeout
        last: Any = None
        same = 0
        snap: dict[str, Any] = {}
        while True:
            await asyncio.sleep(self._poll)
            snap = await self._js(_OPTIONS_JS, {"company_words": words("company_hint")}) or {}
            sig = [
                (o.get("title"), o.get("subtitle"), o.get("entity"))
                for o in snap.get("options") or []
            ]
            same = same + 1 if sig and sig == last else 0
            last = sig
            # The page composer announces the finished list and names the
            # query in it (measured); without that announcement it is still
            # loading, however stable it looks.
            announced = engine != "quill" or any(
                fold(mention.name) in fold(s) for s in snap.get("live") or []
            )
            if snap.get("listbox") and same >= self._settle and announced:
                return {"status": "ok", **snap}
            if loop.time() >= end:
                return {
                    "status": "mention_list_not_loaded",
                    "mention": mention.name,
                    "options": len(snap.get("options") or []),
                }

    async def write_mention(self, mention: Mention, engine: str) -> dict[str, Any]:
        if not mention.entity_id:
            return {"status": "mention_target_unresolved", "mention": mention.name}
        before = await self._js(_ENTITIES_JS, self._selector) or []
        prep = await self._js(
            _PREP_JS, {"selector": self._selector, "require_empty": False, "clear": False}
        )
        if prep != "ok":
            return {"status": f"text_{prep}"}
        # Key by key like a hand: the typeahead listens to key input.
        await self._page.keyboard.type("@" + mention.name, delay=60)
        listed = await self._settled_options(mention, engine)
        if listed["status"] != "ok":
            return listed
        choice = choose_option(listed.get("options") or [], mention)
        if choice["status"] != "ok":
            return choice
        if not await self._js(_TAG_OPTION_JS, choice["index"]):
            return {"status": "mention_list_not_loaded", "mention": mention.name}
        await self._page.click('[data-ext-mention="pick"]')
        loop = asyncio.get_running_loop()
        end = loop.time() + 3.0
        after: list[dict[str, Any]] = []
        while loop.time() < end:
            await asyncio.sleep(0.3)
            after = await self._js(_ENTITIES_JS, self._selector) or []
            if len(after) > len(before):
                break
        if len(after) != len(before) + 1:
            return {"status": "mention_not_linked", "mention": mention.name}
        new = [e for e in after if e not in before] or [after[-1]]
        got = new[-1]
        if str(got.get("id")) != str(mention.entity_id):
            return {
                "status": "mention_wrong_entity",
                "mention": mention.name,
                "linked_to": got.get("id"),
            }
        state = await self._js(_STATE_JS, self._selector) or {}
        if state.get("pending"):
            return {"status": "mention_not_linked", "mention": mention.name}
        return {
            "status": "ok",
            "mention": mention.name,
            "entity_id": got.get("id"),
            "picked_by": "verified_after_insert" if choice["verify_only"] else "identifier",
        }

    async def write(self, segments: list[Segment], *, clear: bool = False) -> dict[str, Any]:
        """Write all segments into the editor (empty, or cleared first)."""
        prep = await self._js(
            _PREP_JS, {"selector": self._selector, "require_empty": True, "clear": clear}
        )
        if prep != "ok":
            return {"status": f"text_{prep}"}
        wanted = mentions_of(segments)
        engine = await self._js(_ENGINE_JS, self._selector)
        if wanted and engine not in ("tiptap", "quill"):
            return {
                "status": "mention_unverifiable",
                "engine": engine,
                "message": "This editor is not measured; a mention could not be verified.",
            }
        written: list[dict[str, Any]] = []
        for kind, value in segments:
            if kind == "text":
                prep = await self._js(
                    _PREP_JS,
                    {"selector": self._selector, "require_empty": False, "clear": False},
                )
                if prep != "ok":
                    return {"status": f"text_{prep}", "mentions": written}
                ok = await self._js(_INSERT_JS, {"selector": self._selector, "text": value})
                if ok != "ok":
                    return {"status": f"text_{ok}", "mentions": written}
            else:
                got = await self.write_mention(value, engine)
                if got["status"] != "ok":
                    return {**got, "mentions": written}
                written.append(got)
        state = await self._js(_STATE_JS, self._selector) or {}
        shown = canon(state.get("text"))
        if shown != canon(plain_text(segments)) or "[[" in shown:
            return {"status": "text_mismatch", "mentions": written, "shown": shown[:200]}
        if wanted:
            final = await self._js(_ENTITIES_JS, self._selector) or []
            if [str(e.get("id")) for e in final] != [str(m.entity_id) for m in wanted]:
                return {
                    "status": "mention_missing",
                    "mentions": written,
                    "entities": [e.get("id") for e in final],
                }
        return {"status": "written", "engine": engine, "mentions": written}


def has_markup(text: str) -> bool:
    """True when the text carries mention markup (or a broken piece of it)."""
    return bool(_MARKUP_RE.search(text or "")) or "[[" in (text or "")
