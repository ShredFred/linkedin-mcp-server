"""MentionWriter against synthetic composers built from the measured fixture.

``tests/fixtures/mention-typeahead-de-2026-10-09.json`` records what
composer_probe measured on 2026-10-09 (German interface). The two editors here
copy it, including the parts that make the job hard:

* member composer (tiptap): the suggestion's target is *not* a DOM attribute,
  only a React-style prop on an inner node; the first list shown belongs to a
  shorter prefix and is replaced about a second later; the inserted entity
  keeps its id only in the editor document (``editor.getJSON()``).
* page composer (Quill): no identifier at all before the click; the list is
  only announced as finished when the live region names the query; the
  inserted ``a.ql-mention`` carries the target URN.

All names and ids are invented. Every case runs with German and English list
wording. Skipped when chromium is not installed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.linkedin.ext_mentions import (
    Mention,
    MentionWriter,
    parse_mentions,
)

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "mention-typeahead-de-2026-10-09.json").read_text(
        encoding="utf-8"
    )
)

MAX_A = "ACoAAPLATZHALTER000000000000000000000001"
MAX_B = "ACoAAPLATZHALTER000000000000000000000002"
ERIKA = "ACoAAPLATZHALTER000000000000000000000003"
ACME = "1000001"

DE = {"company": "Firma", "degree": "2.", "status_tt": "{n} Vorschläge verfügbar",
      "status_q": "{n} Vorschläge für „{q}“ gefunden."}
EN = {"company": "Company", "degree": "2nd", "status_tt": "{n} suggestions available",
      "status_q": "{n} suggestions found for “{q}”."}
LOCALES = [pytest.param(DE, id="de"), pytest.param(EN, id="en")]

# Shared fake-typeahead runtime. ``window.FAKE`` holds the candidates; ids are
# kept in a closure map, never in the DOM, unless the engine exposes them the
# way the measured build does.
RUNTIME = r"""
<script>
(() => {
  const cfg = window.FAKE;
  const ids = new WeakMap();
  const editor = document.getElementById('ed');
  let timers = [];
  const box = document.getElementById('box');
  const status = document.getElementById('st');
  const query = () => {
    const t = editor.innerText.replace(/\n$/, '');
    const at = t.lastIndexOf('@');
    return at < 0 ? null : t.slice(at + 1);
  };
  const render = (list, announce) => {
    box.innerHTML = '';
    box.style.display = list.length ? 'block' : 'none';
    list.forEach((c, i) => {
      const o = document.createElement('div');
      o.setAttribute('role', 'option');
      o.id = '_r_' + i + '_' + Math.random().toString(36).slice(2, 6);
      const kind = c.kind === 'company' ? 'company-accent-4' : 'person-accent-4';
      const sub = c.kind === 'company' ? cfg.t.company + '· Software' : cfg.t.degree + ' · ' + c.headline;
      if (cfg.engine === 'quill') {
        o.className = 'basic-typeahead__selectable editor-typeahead__typeahead-item';
        o.innerHTML = '<div class="search-typeahead-v2__hit"><span class="search-typeahead-v2__hit-info">'
          + '<span class="search-typeahead-v2__hit-text">' + c.name + '</span>'
          + '<span class="search-typeahead-v2__hit-subtext">' + sub + '</span></span></div>';
      } else {
        o.innerHTML = '<div role="button" tabindex="-1"><figure aria-hidden="true"><svg id="' + kind
          + '"></svg></figure><div><p>' + c.name + '</p><p>' + sub + '</p></div></div>';
        if (cfg.expose_ids !== false) {
          // As measured: the id is nested in the props of a component fiber
          // between the option and the list, not a top-level prop. The list's
          // own fiber holds every id -- a walk that does not stop at the
          // option would see all of them.
          o['__reactFiber$fake'] = {stateNode: o, memoizedProps: {className: 'x'},
            return: {stateNode: null,
                     memoizedProps: {item: {data: {hit: {meta: {id: 'mentionTypeahead_display_' + c.id}}}}},
                     return: {stateNode: box, memoizedProps: {all: list.map(x => 'mentionTypeahead_display_' + x.id)}}}};
        }
      }
      o.addEventListener('mousedown', e => e.preventDefault());
      o.addEventListener('click', () => pick(c));
      box.appendChild(o);
    });
    if (cfg.engine === 'quill') {
      status.setAttribute('aria-label', announce ? cfg.t.status_q.replace('{n}', list.length).replace('{q}', query()) : '');
    } else {
      status.innerText = list.length ? cfg.t.status_tt.replace('{n}', list.length + 1) : '';
    }
  };
  const pick = c => {
    const q = query();
    // Remove "@query" at the end of the last text node, insert the entity.
    const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT);
    let last = null; while (walker.nextNode()) last = walker.currentNode;
    if (!last || !last.data.endsWith('@' + q)) return;
    last.data = last.data.slice(0, last.data.length - q.length - 1);
    let ent;
    if (cfg.engine === 'quill') {
      ent = document.createElement('a');
      ent.className = 'ql-mention'; ent.href = '#';
      const urn = c.kind === 'company' ? 'urn:li:fsd_company:' : 'urn:li:fsd_profile:';
      ent.setAttribute('data-entity-urn', urn + (cfg.link_as ? cfg.link_as : c.id));
      ent.setAttribute('data-test-ql-mention', 'true');
      ent.textContent = c.name;
    } else {
      ent = document.createElement('span');
      ent.setAttribute('data-type', 'mention'); ent.contentEditable = 'false';
      ent.innerHTML = '<strong>' + c.name + '</strong>';
      ids.set(ent, cfg.link_as ? cfg.link_as : c.id);
    }
    last.parentNode.insertBefore(ent, last.nextSibling);
    const after = document.createTextNode('');
    ent.parentNode.insertBefore(after, ent.nextSibling);
    const r = document.createRange(); r.setStart(after, 0); r.collapse(true);
    const s = getSelection(); s.removeAllRanges(); s.addRange(r);
    timers.forEach(clearTimeout); render([], false);
  };
  if (cfg.engine === 'tiptap') {
    editor.editor = {getJSON: () => {
      const para = [];
      editor.querySelectorAll('[data-type="mention"]').forEach(e => para.push(
        {type: 'mention', attrs: {id: null, label: null, entityID: ids.get(e) || null},
         content: [{type: 'text', text: e.innerText}]}));
      return {type: 'doc', content: [{type: 'paragraph', content: para}]};
    }};
  }
  editor.addEventListener('input', () => {
    timers.forEach(clearTimeout); timers = [];
    const q = query();
    if (q === null || q.length < 2) { render([], false); return; }
    const final = cfg.people.filter(c => c.name.toLowerCase().startsWith(q.toLowerCase().split(' ')[0]));
    if (cfg.never_settles) {
      let n = 0;
      const flip = () => { render(final.slice(0, 1 + (n++ % Math.max(1, final.length))), false);
                           timers.push(setTimeout(flip, 300)); };
      flip(); return;
    }
    // First the list of a shorter prefix, then the real one (measured).
    timers.push(setTimeout(() => render(cfg.stale, false), 200));
    timers.push(setTimeout(() => render(final, true), 900));
  });
})();
</script>
"""


def _html(engine: str, t: dict[str, str], people: list[dict], **extra) -> str:
    cfg = {
        "engine": engine,
        "t": t,
        "people": people,
        "stale": [{"name": "Maxima Andere", "id": "ACoAAPLATZHALTER000000000000000000000009",
                   "headline": "Vertrieb", "kind": "person"}],
        **extra,
    }
    if engine == "quill":
        ed = ('<div id="ed" class="ql-editor" contenteditable="true" '
              'aria-label="Texteditor zum Erstellen von Inhalten"></div>'
              '<div class="ql-clipboard" contenteditable="true" style="width:0;height:1px"></div>')
    else:
        ed = ('<div id="ed" class="tiptap ProseMirror" contenteditable="true" role="textbox" '
              'componentkey="ShareBox_textEditor"></div>')
    return (
        "<html><body><div role=\"dialog\">" + ed + "<button>Posten</button>"
        "<div role=\"listbox\" id=\"box\" style=\"display:none\"></div>"
        "<div role=\"status\" aria-live=\"polite\" id=\"st\"></div></div>"
        f"<script>window.FAKE = {json.dumps(cfg)};</script>" + RUNTIME + "</body></html>"
    )


@pytest.fixture
async def page():
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            pg = await browser.new_page()
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            yield pg
        finally:
            await browser.close()


def _writer(pg) -> MentionWriter:
    return MentionWriter(pg, "#ed", list_timeout=5.0, poll=0.4)


def _segments(text: str):
    segments, bad = parse_mentions(text)
    assert bad is None, bad
    return segments


PEOPLE = [
    {"name": "Max Platzhalter", "id": MAX_A, "headline": "Einkauf", "kind": "person"},
    {"name": "Max Platzhalter", "id": MAX_B, "headline": "Werkstoffprüfung", "kind": "person"},
    {"name": "Maxi Beispiel", "id": ERIKA, "headline": "Labor", "kind": "person"},
]


def test_fixture_is_the_measurement_the_dom_copies() -> None:
    member = FIXTURE["member_composer"]
    assert "mentionTypeahead_display_" in member["option_identity"]
    assert member["inserted_entity_doc"]["type"] == "mention"
    assert "none before the click" in FIXTURE["page_composer"]["option_identity"]
    assert "data-entity-urn" in FIXTURE["page_composer"]["inserted_entity_html"]


@pytest.mark.parametrize("t", LOCALES)
async def test_tiptap_picks_the_namesake_by_id_not_by_position(page, t) -> None:
    # Two "Max Platzhalter"; the wanted one is the second suggestion.
    await page.set_content(_html("tiptap", t, PEOPLE))
    got = await _writer(page).write(
        _segments(f"Danke an [[Max Platzhalter|urn:li:fsd_profile:{MAX_B}]] fürs Prüfen.")
    )
    assert got["status"] == "written", got
    assert got["mentions"][0]["entity_id"] == MAX_B
    assert got["mentions"][0]["picked_by"] == "identifier"
    text = await page.inner_text("#ed")
    assert text.replace(" ", " ").strip() == "Danke an Max Platzhalter fürs Prüfen."


@pytest.mark.parametrize("t", LOCALES)
async def test_tiptap_target_absent_is_not_resolved(page, t) -> None:
    await page.set_content(_html("tiptap", t, PEOPLE[:1]))
    got = await _writer(page).write(_segments(f"Hallo [[Max Platzhalter|{MAX_B}]]"))
    assert got["status"] == "mention_not_resolved", got
    assert await page.locator('[data-type="mention"]').count() == 0


@pytest.mark.parametrize("t", LOCALES)
async def test_quill_namesakes_without_identifier_stop(page, t) -> None:
    await page.set_content(_html("quill", t, PEOPLE))
    got = await _writer(page).write(_segments(f"Hallo [[Max Platzhalter|{MAX_B}]]"))
    assert got["status"] == "mention_ambiguous", got
    assert await page.locator("a.ql-mention").count() == 0


@pytest.mark.parametrize("t", LOCALES)
async def test_quill_single_namesake_is_verified_after_insert(page, t) -> None:
    await page.set_content(_html("quill", t, PEOPLE[2:]))
    got = await _writer(page).write(_segments(f"Gruß an [[Maxi Beispiel|{ERIKA}]]!"))
    assert got["status"] == "written", got
    assert got["mentions"][0]["picked_by"] == "verified_after_insert"


@pytest.mark.parametrize("t", LOCALES)
async def test_quill_single_namesake_with_another_target_is_refused(page, t) -> None:
    # The only "Maxi Beispiel" offered links to someone else.
    await page.set_content(_html("quill", t, PEOPLE[2:], link_as=MAX_A))
    got = await _writer(page).write(_segments(f"Gruß an [[Maxi Beispiel|{ERIKA}]]!"))
    assert got["status"] == "mention_wrong_entity", got
    assert got["linked_to"] == MAX_A


@pytest.mark.parametrize("t", LOCALES)
async def test_company_mention_by_numeric_id(page, t) -> None:
    people = [{"name": "Acme Labs", "id": ACME, "headline": "", "kind": "company"}]
    await page.set_content(_html("tiptap", t, people))
    got = await _writer(page).write(_segments(f"Mit [[Acme Labs|company:{ACME}]] gebaut."))
    assert got["status"] == "written", got


@pytest.mark.parametrize("t", LOCALES)
async def test_list_that_never_settles_is_not_loaded(page, t) -> None:
    await page.set_content(_html("tiptap", t, PEOPLE, never_settles=True))
    got = await _writer(page).write(_segments(f"Hallo [[Max Platzhalter|{MAX_B}]]"))
    assert got["status"] == "mention_list_not_loaded", got


@pytest.mark.parametrize("t", LOCALES)
async def test_quill_list_without_announcement_is_not_loaded(page, t) -> None:
    # Same stable list, but the live region never names the query.
    html = _html("quill", t, PEOPLE[2:]).replace(
        "status.setAttribute('aria-label', announce", "status.setAttribute('aria-label', false"
    )
    await page.set_content(html)
    got = await _writer(page).write(_segments(f"Gruß an [[Maxi Beispiel|{ERIKA}]]!"))
    assert got["status"] == "mention_list_not_loaded", got


async def test_unknown_editor_refuses_mentions(page) -> None:
    await page.set_content(
        '<div role="dialog"><div id="ed" contenteditable="true"></div><button>Posten</button></div>'
    )
    got = await _writer(page).write(_segments(f"Hallo [[Max Platzhalter|{MAX_B}]]"))
    assert got["status"] == "mention_unverifiable", got
    # Plain text still writes.
    got = await _writer(page).write(_segments("Nur Text."))
    assert got["status"] == "written", got


def test_mention_dataclass_describes_itself() -> None:
    assert Mention("A", "person", entity_id=MAX_A).describe()["entity_id"] == MAX_A
