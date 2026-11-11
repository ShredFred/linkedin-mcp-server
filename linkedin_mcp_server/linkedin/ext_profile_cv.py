"""Fork extension: the signed-in member's CV sections as forms (2026-11-11).

Builds on ``ExtProfileEditor`` (identity gate, discard, pick) and adds one
generic form engine for every list section LinkedIn edits in a dialog:
experience, education, certifications, languages, projects, volunteering,
honors, publications -- plus the singleton "about".

Measured on a German profile on 2026-11-11 with ``composer_probe`` (dry, every
dialog discarded):

* Every add form opens directly at ``/in/<slug>/edit/forms/<kind>/new/`` for
  certification, education, language, project, volunteer-experience, honor,
  publication and summary ("Info bearbeiten"). The position form is reached
  from ``/details/experience/`` through "Position oder berufliche Auszeit
  hinzufügen" -> "Position hinzufügen"; its sibling link is
  ``/details/experience/edit/forms/career-break/new/``.
* ``/edit/forms/skill/new/`` and ``/edit/contact-info/`` open no dialog: the
  skills section and contact info are **not measured** and are read-only here.
* Field labels come from ``aria-label``, ``aria-labelledby`` or ``label[for]``
  (position: "Jobbezeichnung*", "Unternehmen", "Standort", selects
  "Standorttyp", "Beschäftigungsverhältnis", "Startmonat", "Startjahr*",
  checkbox "Zurzeit arbeite ich hier", tiptap textbox "Hervorgehoben, max.
  2.000 Zeichen"). Typeahead inputs carry ``data-testid="typeahead-input"``.
* The position form carries a "Netzwerk informieren" switch (``role=switch``,
  default off) and a checkbox "Aktuelle Position mit sofortiger Wirkung
  abschließen – <title>". The engine keeps the switch off and never touches
  that checkbox: it ends another position.

What is matched is the label *prefix* (lower case, ``*`` stripped) from a
per-language table. A field that is not exactly one match is a status with
the form's inventory, never a guess -- that inventory is the measurement for
the next locale or layout.

Read-back: an existing item is re-read from its edit form; a new or deleted
item from the section's details page text.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from urllib.parse import quote

from linkedin_mcp_server.linkedin.ext_profile import (
    _PICK_JS,
    _VIS,
    CONFIRM_DELETE_WORDS,
    INTRO_URL,
    SAVE_WORDS,
    ExtProfileEditor,
)

logger = logging.getLogger(__name__)

BASE = "https://www.linkedin.com/in/{slug}/"
FORM_NEW = BASE + "edit/forms/{kind}/new/"
DETAILS = BASE + "details/{details}/"

# Field kinds: text (input), area (textarea or contenteditable), ahead
# (typeahead input), select, check (checkbox), month_year (two selects),
# date_text (one text input with a date placeholder).
SECTIONS: dict[str, dict[str, Any]] = {
    "experience": {
        "kind": "position",
        "details": "experience",
        "open": ["position oder berufliche auszeit hinzufügen", "position hinzufügen"],
        "open_en": ["add position or career break", "add position"],
        "key": ("title", "company"),
        "fields": {
            "title": ("ahead", ["jobbezeichnung", "titel", "title"]),
            "company": (
                "ahead",
                ["unternehmen", "organisation", "company", "organization"],
            ),
            "location": ("ahead", ["standort", "location"]),
            "location_type": ("select", ["standorttyp", "location type"]),
            "employment_type": (
                "select",
                ["beschäftigungsverhältnis", "employment type"],
            ),
            "current": (
                "check",
                ["zurzeit arbeite ich hier", "i am currently working in this role"],
            ),
            "start": ("month_year", ["start", "beginn"]),
            "end": ("month_year", ["end"]),
            "description": (
                "area",
                ["hervorgehoben", "beschreibung", "description", "highlights"],
            ),
        },
    },
    "education": {
        "kind": "education",
        "details": "education",
        "key": ("school", "degree"),
        "fields": {
            "school": (
                "ahead",
                ["bildungseinrichtung", "hochschule", "schule", "school"],
            ),
            "degree": ("text", ["abschluss", "degree"]),
            "field": ("text", ["studienfach", "field of study"]),
            "grade": ("text", ["note", "grade"]),
            "activities": ("area", ["aktivitäten", "activities"]),
            "start": ("month_year", ["start", "beginn"]),
            "end": ("month_year", ["end", "abschluss (oder erwartet)"]),
            "description": ("area", ["beschreibung", "description"]),
        },
    },
    "certifications": {
        "kind": "certification",
        "details": "certifications",
        "key": ("name", "issuer"),
        "fields": {
            "name": ("ahead", ["name"]),
            "issuer": ("ahead", ["ausgestellt von", "issuing organization"]),
            "issued": ("month_year", ["ausstellungsdatum", "ausgestellt am", "issue"]),
            "expires": ("month_year", ["ablaufdatum", "gültig bis", "expiration"]),
            "credential_id": (
                "text",
                ["nachweis-id", "credential-id", "zertifikats-id", "credential id"],
            ),
            "credential_url": (
                "text",
                ["nachweis-url", "credential-url", "zertifikats-url", "credential url"],
            ),
        },
    },
    "languages": {
        "kind": "language",
        "details": "languages",
        "key": ("name",),
        "fields": {
            "name": ("ahead", ["sprache", "language"]),
            "proficiency": (
                "select",
                ["sprachkenntnisse", "kenntnisstand", "proficiency"],
            ),
        },
    },
    "projects": {
        "kind": "project",
        "details": "projects",
        "key": ("title",),
        "fields": {
            "title": ("text", ["projektname", "name", "titel", "project name"]),
            "description": ("area", ["beschreibung", "description"]),
            "current": (
                "check",
                [
                    "ich arbeite derzeit an diesem projekt",
                    "i am currently working on this project",
                ],
            ),
            "start": ("month_year", ["start", "beginn"]),
            "end": ("month_year", ["end"]),
            "url": ("text", ["projekt-url", "url"]),
        },
    },
    "volunteering": {
        "kind": "volunteer-experience",
        "details": "volunteering-experiences",
        "key": ("organization", "role"),
        "fields": {
            "organization": ("ahead", ["organisation", "organization"]),
            "role": ("text", ["funktion", "rolle", "role"]),
            "cause": ("select", ["anliegen", "bereich", "cause"]),
            "current": (
                "check",
                ["ich bin derzeit ehrenamtlich", "i am currently volunteering"],
            ),
            "start": ("month_year", ["start", "beginn"]),
            "end": ("month_year", ["end"]),
            "description": ("area", ["beschreibung", "description"]),
        },
    },
    "honors": {
        "kind": "honor",
        "details": "honors",
        "key": ("title",),
        "fields": {
            "title": ("text", ["titel", "title"]),
            "issuer": ("text", ["verliehen von", "aussteller", "issuer"]),
            "date": ("month_year", ["verleihungsdatum", "ausstellungsdatum", "issue"]),
            "description": ("area", ["beschreibung", "description"]),
        },
    },
    "publications": {
        "kind": "publication",
        "details": "publications",
        "key": ("title",),
        "fields": {
            "title": ("text", ["titel", "title"]),
            "publisher": (
                "text",
                ["verlag", "publikation", "publisher", "publication"],
            ),
            "date": (
                "date_text",
                ["veröffentlichungsdatum", "datum", "publication date"],
            ),
            "url": ("text", ["publikations-url", "url"]),
            "description": ("area", ["beschreibung", "description"]),
        },
    },
}

SINGLETONS: dict[str, dict[str, Any]] = {
    "about": {
        "url": FORM_NEW,
        "kind": "summary",
        "fields": {"about": ("area", ["info", "about"])},
    },
    # Location sits in the intro dialog (labels not yet measured).
    "location": {
        "url": INTRO_URL,
        "fields": {
            "country": ("ahead", ["land/region", "country/region"]),
            "city": ("ahead", ["ort", "stadt", "city"]),
        },
    },
}

# Never touched: ending another position in passing (measured de).
FORBIDDEN_CHECK = ["aktuelle position mit sofortiger wirkung", "end current position"]
NOTIFY_SWITCH = [
    "netzwerk über wichtige änderungen",
    "notify network",
    "share profile updates",
]
DELETE_WORDS = ["löschen", "delete"]
_MONTH_WORDS = ["monat", "month"]
_YEAR_WORDS = ["jahr", "year"]
_DATE_RE = re.compile(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?$")

# Inventory + tagging of the topmost dialog's fields by a spec.
_FORM_JS = (
    "(arg) => {"
    + _VIS
    + r"""
  document.querySelectorAll('[data-ext-cv]').forEach(e => e.removeAttribute('data-ext-cv'));
  const dlg = topDialog();
  if (!dlg) return {dialog: false};
  const norm = s => String(s || '').replace(/\s+/g, ' ').replace(/\*/g, '').trim().toLowerCase();
  const els = [...dlg.querySelectorAll('input, textarea, select, [contenteditable="true"]')].filter(e =>
      vis(e) || (e.type === 'checkbox' && vis(e.parentElement)));
  const inv = els.map(e => {
    const tag = e.tagName.toLowerCase();
    const kind = tag === 'select' ? 'select' : (e.type === 'checkbox' ? 'check'
        : (tag === 'textarea' || e.isContentEditable) ? 'area'
        : (e.getAttribute('data-testid') === 'typeahead-input' ? 'ahead' : 'text'));
    let value = tag === 'select' ? e.value : (e.isContentEditable ? (e.innerText || '').trim() : e.value);
    if (kind === 'check') value = !!e.checked;
    const optionText = tag === 'select' && e.selectedIndex > 0 ? (e.options[e.selectedIndex].text || '').trim() : '';
    return {el: e, kind, label: norm(labelOf(e)), placeholder: norm(e.getAttribute('placeholder')),
            role: e.getAttribute('role') || '', value, optionText};
  });
  const out = {dialog: true, heading: ((dlg.querySelector('h1, h2, h3') || {}).innerText || '').trim(),
               fields: {}, inventory: inv.map(f => ({kind: f.kind, label: f.label.slice(0, 80),
                   placeholder: f.placeholder.slice(0, 60), role: f.role}))};
  const starts = (f, labels) => labels.some(l => f.label.startsWith(l) || (!f.label && f.placeholder.startsWith(l)));
  for (const [name, spec] of Object.entries(arg.fields)) {
    const [kind, labels] = spec;
    if (kind === 'month_year') {
      const sel = inv.filter(f => f.kind === 'select' && starts(f, labels));
      const month = sel.filter(f => arg.month.some(w => f.label.includes(w)));
      const year = sel.filter(f => arg.year.some(w => f.label.includes(w)));
      out.fields[name] = {count: Math.min(month.length, 1) + Math.min(year.length, 1),
                          months: month.length, years: year.length};
      if (month.length === 1) { month[0].el.setAttribute('data-ext-cv', name + '-month');
                                out.fields[name].month = month[0].value; }
      if (year.length === 1) { year[0].el.setAttribute('data-ext-cv', name + '-year');
                               out.fields[name].year = year[0].value; }
      continue;
    }
    const want = kind === 'ahead' ? ['ahead', 'text'] : kind === 'date_text' ? ['text'] : [kind];
    const hits = inv.filter(f => want.includes(f.kind) && starts(f, labels));
    out.fields[name] = {count: hits.length};
    if (hits.length === 1) {
      hits[0].el.setAttribute('data-ext-cv', name);
      out.fields[name].value = hits[0].kind === 'select' ? hits[0].optionText : hits[0].value;
      out.fields[name].raw = hits[0].value;
      out.fields[name].placeholder = hits[0].placeholder;
    }
  }
  const forbidden = inv.filter(f => f.kind === 'check' && arg.forbidden.some(w => f.label.startsWith(w)));
  out.forbidden_checked = forbidden.some(f => f.value === true);
  const notify = inv.filter(f => f.role === 'switch' && arg.notify.some(w => f.label.includes(w)));
  if (notify.length === 1) { notify[0].el.setAttribute('data-ext-cv', '__notify'); out.notify = notify[0].value; }
  return out;
}"""
)

# Options of one tagged select, for choosing by value, text or number.
_OPTIONS_JS = r"""(tag) => {
  const s = document.querySelector('[data-ext-cv="' + tag + '"]');
  return s ? [...s.options].map(o => ({value: o.value, text: (o.text || '').trim()})) : null;
}"""

# Visible suggestion options of a typeahead (topmost dialog or page).
_AHEAD_OPTIONS_JS = r"""(text) => {
  document.querySelectorAll('[data-ext-cv-opt]').forEach(e => e.removeAttribute('data-ext-cv-opt'));
  const vis = el => !!(el && el.getClientRects().length);
  const norm = s => String(s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const opts = [...document.querySelectorAll('[role="option"]')].filter(vis);
  const exact = opts.filter(o => norm((o.innerText || '').split('\n')[0]) === norm(text));
  if (exact.length === 1) exact[0].setAttribute('data-ext-cv-opt', '1');
  return {options: opts.length, exact: exact.length};
}"""

# Items of a details page: edit anchors ``/edit/forms/<kind>/<id>/`` with text.
_ITEMS_JS = r"""(kind) => {
  const vis = el => !!(el && el.getClientRects().length);
  const seen = new Set(); const out = [];
  const re = new RegExp('/edit/forms/' + kind + '/([^/?#]+)');
  for (const a of document.querySelectorAll('a[href]')) {
    const m = (a.getAttribute('href') || '').match(re);
    if (!m || m[1] === 'new' || seen.has(m[1]) || !vis(a)) continue;
    seen.add(m[1]);
    const box = a.closest('li') || a.parentElement;
    out.push({id: m[1], href: a.getAttribute('href').split('?')[0],
              lines: (box ? box.innerText : '').split('\n').map(s => s.trim()).filter(Boolean).slice(0, 12)});
  }
  const main = document.querySelector('main') || document.body;
  return {items: out, text: (main.innerText || '').replace(/\s+/g, ' ').slice(0, 20000)};
}"""


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def parse_date(value: str | None) -> tuple[str, str, str] | None:
    """``YYYY``, ``YYYY-MM`` or ``YYYY-MM-DD`` -> (year, month, day), '' when absent."""
    if value is None:
        return None
    m = _DATE_RE.match(str(value).strip())
    if not m:
        raise ValueError(f"date {value!r}: YYYY, YYYY-MM or YYYY-MM-DD")
    year, month, day = m.group(1), m.group(2) or "", m.group(3) or ""
    if month and not 1 <= int(month) <= 12:
        raise ValueError(f"date {value!r}: month 01-12")
    return year, str(int(month)) if month else "", str(int(day)) if day else ""


def date_text(value: str, placeholder: str) -> str:
    """A ``YYYY-MM-DD`` value in the form the input's placeholder asks for."""
    year, month, day = parse_date(value) or ("", "", "")
    if not (month and day):
        raise ValueError("a date input needs YYYY-MM-DD")
    if "tt" in placeholder.lower():  # de: TT.MM.JJJJ
        return f"{int(day):02d}.{int(month):02d}.{year}"
    return f"{int(month):02d}/{int(day):02d}/{year}"


def item_key(section: str, item: dict[str, Any]) -> tuple[str, ...]:
    return tuple(norm(item.get(k)) for k in SECTIONS[section]["key"])


def compare(
    spec: dict[str, Any], want: dict[str, Any], form: dict[str, Any]
) -> dict[str, Any]:
    """Fields whose form value differs from the wanted one: {name: (is, want)}."""
    diff: dict[str, Any] = {}
    fields = form.get("fields", {})
    for name, value in want.items():
        if name not in spec or value is None:
            continue
        kind = spec[name][0]
        got = fields.get(name, {})
        if kind == "month_year":
            year, month, _ = parse_date(value) or ("", "", "")
            is_ = (got.get("year") or "", got.get("month") or "")
            if is_ != (year, month):
                diff[name] = (is_, (year, month))
        elif kind == "check":
            if bool(got.get("value")) != bool(value):
                diff[name] = (got.get("value"), bool(value))
        elif kind == "date_text":
            if norm(got.get("value")) != norm(
                date_text(value, got.get("placeholder") or "")
            ):
                diff[name] = (got.get("value"), value)
        elif norm(got.get("value")) != norm(value):
            diff[name] = (got.get("value"), value)
    return diff


def form_values(spec: dict[str, Any], form: dict[str, Any]) -> dict[str, Any]:
    """The form as CV values (the pre-image of an item)."""
    out: dict[str, Any] = {}
    for name, (kind, _labels) in spec.items():
        got = form.get("fields", {}).get(name) or {}
        if kind == "month_year":
            if got.get("year"):
                out[name] = got["year"] + (
                    f"-{int(got['month']):02d}" if got.get("month") else ""
                )
        elif kind == "check":
            if "value" in got:
                out[name] = bool(got["value"])
        elif got.get("value"):
            out[name] = got["value"]
    return out


class ExtProfileCv(ExtProfileEditor):
    """Read and write CV sections through LinkedIn's own edit dialogs."""

    async def _form(self, spec: dict[str, Any]) -> dict[str, Any]:
        return await self._page.evaluate(
            _FORM_JS,
            {
                "fields": {k: [v[0], v[1]] for k, v in spec.items()},
                "month": _MONTH_WORDS,
                "year": _YEAR_WORDS,
                "forbidden": FORBIDDEN_CHECK,
                "notify": NOTIFY_SWITCH,
            },
        )

    async def _wait_form(self, spec: dict[str, Any]) -> dict[str, Any]:
        form: dict[str, Any] = {}
        for _ in range(8):
            form = await self._form(spec)
            if form.get("dialog") and form.get("inventory"):
                return form
            await asyncio.sleep(1.0)
        return form

    async def open_form(
        self, slug: str, section: str, href: str | None
    ) -> dict[str, Any]:
        """Open an item's edit form (``href``) or the section's add form."""
        if section in SINGLETONS:
            single = SINGLETONS[section]
            await self._goto(
                single["url"].format(slug=quote(slug), kind=single.get("kind", ""))
            )
            return await self._wait_form(single["fields"])
        spec = SECTIONS[section]
        if href:
            url = href if href.startswith("http") else "https://www.linkedin.com" + href
            await self._goto(url)
        elif spec.get("open"):
            await self._goto(DETAILS.format(slug=quote(slug), details=spec["details"]))
            for de, en in zip(spec["open"], spec["open_en"]):
                got = await self._click("page", [de, en], "cv-open")
                if not got["ok"]:
                    got = await self._click("menu", [de, en], "cv-open")
                if not got["ok"]:
                    return {
                        "dialog": False,
                        "status": "add_control_missing",
                        "control": de,
                    }
        else:
            await self._goto(FORM_NEW.format(slug=quote(slug), kind=spec["kind"]))
        return await self._wait_form(spec["fields"])

    async def list_items(self, slug: str, section: str) -> dict[str, Any]:
        spec = SECTIONS[section]
        await self._goto(DETAILS.format(slug=quote(slug), details=spec["details"]))
        got: dict[str, Any] = {}
        for _ in range(4):
            got = await self._page.evaluate(_ITEMS_JS, spec["kind"])
            if got.get("items"):
                break
            await asyncio.sleep(1.0)
        return got

    async def read_item(
        self, slug: str, section: str, href: str | None
    ) -> dict[str, Any]:
        fields = (
            SINGLETONS[section]["fields"]
            if section in SINGLETONS
            else SECTIONS[section]["fields"]
        )
        form = await self.open_form(slug, section, href)
        values = form_values(fields, form) if form.get("dialog") else None
        await self._discard()
        return {"values": values, "form": form}

    async def _set_select(self, tag: str, wanted: str, by_number: bool) -> bool:
        options = await self._page.evaluate(_OPTIONS_JS, tag) or []
        hit = None
        for opt in options:
            if by_number and opt["value"] == wanted:
                hit = opt
            elif not by_number and (
                norm(opt["text"]) == norm(wanted) or opt["value"] == wanted
            ):
                hit = opt
        if hit is None or hit["value"] == "":
            return False
        await self._page.locator(f'[data-ext-cv="{tag}"]').first.select_option(
            value=hit["value"]
        )
        return True

    async def _fill(
        self, name: str, kind: str, value: Any, form: dict[str, Any]
    ) -> str | None:
        """Fill one tagged field; a reason string when it could not be done."""
        loc = self._page.locator(f'[data-ext-cv="{name}"]').first
        if kind == "month_year":
            year, month, _ = parse_date(value) or ("", "", "")
            if not await self._set_select(f"{name}-year", year, True):
                return f"{name}: year {year} not offered"
            if month and not await self._set_select(f"{name}-month", month, True):
                return f"{name}: month {month} not offered"
            return None
        if kind == "select":
            return (
                None
                if await self._set_select(name, str(value), False)
                else f"{name}: option {value!r} not offered"
            )
        if kind == "check":
            checked = bool(form["fields"][name].get("value"))
            if checked != bool(value):
                await loc.click()
            return None
        text = (
            date_text(value, form["fields"][name].get("placeholder") or "")
            if kind == "date_text"
            else str(value)
        )
        await loc.click()
        if kind == "area":
            await self._page.keyboard.press("Control+A")
            await self._page.keyboard.press("Delete")
            await self._page.keyboard.insert_text(text)
        else:
            await loc.fill("")
            await self._page.keyboard.insert_text(text)
        if kind == "ahead":
            await asyncio.sleep(1.5)
            pick = await self._page.evaluate(_AHEAD_OPTIONS_JS, text)
            if pick.get("exact") == 1:
                await self._page.locator('[data-ext-cv-opt="1"]').first.click()
            else:
                await self._page.keyboard.press("Tab")  # keep free text, close the list
        await self._wait(0.3, 0.6)
        return None

    async def write_item(
        self,
        slug: str,
        section: str,
        *,
        href: str | None,
        values: dict[str, Any] | None,
        delete: bool,
        confirm: bool,
    ) -> dict[str, Any]:
        """Create (href None), update or delete one item; dry run unless confirm."""
        single = section in SINGLETONS
        spec = SINGLETONS[section]["fields"] if single else SECTIONS[section]["fields"]
        form = await self.open_form(slug, section, href)
        if not form.get("dialog"):
            return {"status": form.get("status") or "form_missing", "form": form}
        pre = form_values(spec, form) if (href or single) else None
        if delete:
            return await self._delete_open(slug, section, pre, confirm)
        values = {
            k: v for k, v in (values or {}).items() if k in spec and v is not None
        }
        if values.get("current") and "end" in values:
            values.pop("end")
        missing = {
            k: form["fields"].get(k, {}).get("count")
            for k in values
            if spec[k][0] != "month_year"
            and form["fields"].get(k, {}).get("count") != 1
        }
        missing.update(
            {
                k: form["fields"].get(k)
                for k in values
                if spec[k][0] == "month_year"
                and form["fields"].get(k, {}).get("years") != 1
            }
        )
        if missing:
            await self._discard()
            return {
                "status": "field_not_unique",
                "fields": missing,
                "pre_image": pre,
                "inventory": form.get("inventory"),
            }
        diff = compare(spec, values, form)
        if not diff:
            await self._discard()
            return {"status": "unchanged", "pre_image": pre, "done": False}
        if form.get("notify") is True:  # keep the network out of it
            await self._page.locator('[data-ext-cv="__notify"]').first.click()
        order = sorted(diff, key=lambda k: (k != "current", list(spec).index(k)))
        for name in order:
            if name == "current":
                await self._fill(name, "check", values[name], form)
                form = await self._form(spec)  # end-date selects appear/disappear
                continue
            reason = await self._fill(name, spec[name][0], values[name], form)
            if reason:
                await self._discard()
                return {
                    "status": "value_not_offered",
                    "detail": reason,
                    "pre_image": pre,
                }
        now = await self._form(spec)
        left = compare(spec, values, now)
        if now.get("forbidden_checked") or now.get("notify") is True:
            await self._discard()
            return {
                "status": "guard_tripped",
                "detail": "end-position or notify switch on",
                "pre_image": pre,
            }
        if left:
            await self._discard()
            return {
                "status": "editor_mismatch",
                "left": {k: list(v) for k, v in left.items()},
                "pre_image": pre,
            }
        if not confirm:
            await self._discard()
            return {
                "status": "dry_run",
                "would_set": {k: list(v) for k, v in diff.items()},
                "pre_image": pre,
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
        back = await self.read_back(slug, section, href, values)
        return {
            "status": "verified" if back["ok"] else "unverified",
            "pre_image": pre,
            "after": back,
            "done": True,
        }

    async def read_back(
        self, slug: str, section: str, href: str | None, values: dict[str, Any]
    ) -> dict[str, Any]:
        if href or section in SINGLETONS:
            got = await self.read_item(slug, section, href)
            spec = (
                SINGLETONS[section]["fields"]
                if section in SINGLETONS
                else SECTIONS[section]["fields"]
            )
            left = (
                compare(spec, values, got["form"])
                if got["form"].get("dialog")
                else {"form": "missing"}
            )
            return {
                "ok": not left,
                "values": got["values"],
                "left": {k: list(v) for k, v in left.items()}
                if isinstance(left, dict)
                else left,
            }
        page = await self.list_items(slug, section)
        text = norm(page.get("text"))
        keys = [norm(values.get(k)) for k in SECTIONS[section]["key"] if values.get(k)]
        return {
            "ok": all(k in text for k in keys),
            "keys": keys,
            "items": page.get("items"),
        }

    async def _delete_open(
        self, slug: str, section: str, pre: dict[str, Any] | None, confirm: bool
    ) -> dict[str, Any]:
        got = await self._page.evaluate(
            "(w) => {"
            + _VIS
            + r"""
  document.querySelectorAll('[data-ext-prof="cv-del"]').forEach(e => e.removeAttribute('data-ext-prof'));
  const dlg = topDialog(); if (!dlg) return {count: 0};
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const hits = [...dlg.querySelectorAll('button, [role="button"]')].filter(vis).filter(b =>
      w.some(x => norm(b.innerText).split(' ').includes(x) || norm(b.getAttribute('aria-label')).split(' ').includes(x)));
  if (hits.length === 1) hits[0].setAttribute('data-ext-prof', 'cv-del');
  return {count: hits.length};
}""",
            DELETE_WORDS,
        )
        if got.get("count") != 1:
            await self._discard()
            return {"status": "delete_control_not_unique", "pre_image": pre, **got}
        if not confirm:
            await self._discard()
            return {
                "status": "dry_run",
                "would_delete": pre,
                "pre_image": pre,
                "done": False,
            }
        self.clicked = True
        await self._prof("cv-del").click()
        await self._wait(1.0, 1.6)
        conf = await self._pick(
            _PICK_JS,
            {"scope": "dialog", "words": CONFIRM_DELETE_WORDS, "tag": "confirm"},
        )
        if conf.get("count") == 1:
            await self._prof("confirm").click()
        await self._dialog_gone(10.0)
        await self._wait(1.5, 2.5)
        page = await self.list_items(slug, section)
        keys = [
            norm((pre or {}).get(k))
            for k in SECTIONS[section]["key"]
            if (pre or {}).get(k)
        ]
        text = norm(page.get("text"))
        gone = bool(keys) and not all(k in text for k in keys)
        return {
            "status": "verified" if gone else "unverified",
            "pre_image": pre,
            "done": True,
        }
