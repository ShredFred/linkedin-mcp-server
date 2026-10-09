"""MediaAttacher against image editors built from the 2026-10-09 measurement.

``tests/fixtures/media-editor-de-2026-10-09.json`` records both measured
editors. The attrappes copy the parts that make the job hard:

* member editor: thumbnails only say ``image 0``/``image 1``; the alt-text
  button gives no sign that a text is saved (``aria-pressed`` stays false), so
  only reopening shows it; the tag button changes its label to
  "Tag, 1 Person getaggt"; tag suggestions carry their id nested in a React
  fiber, and the list's own fiber holds every id.
* page editor: thumbnails carry the file name, "Auswählen <file>" buttons,
  and tag suggestions with no identifier at all.

All names and ids are invented. Skipped when chromium is not installed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.linkedin.ext_composer_labels import words
from linkedin_mcp_server.linkedin.ext_media import MediaAttacher, check_media

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "media-editor-de-2026-10-09.json").read_text(
        encoding="utf-8"
    )
)
MAX_A = "ACoAAPLATZHALTER000000000000000000000001"
MAX_B = "ACoAAPLATZHALTER000000000000000000000002"

RUNTIME = r"""
<script>
(() => {
  const cfg = window.FAKE;
  const root = document.getElementById('root');
  const files = [];
  const alts = {};
  const tags = {};
  let sel = 0;
  const h = (tag, attrs, text) => { const e = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => e.setAttribute(k, v));
    if (text) e.textContent = text; return e; };
  const png = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==';
  const composer = document.getElementById('composer');
  const input = h('input', {type: 'file', multiple: '', style: 'display:none'});
  document.body.appendChild(input);
  document.getElementById('media').addEventListener('click', () => input.click());
  input.addEventListener('change', () => {
    [...input.files].forEach(f => files.push(f.name));
    if (cfg.reverse) files.reverse();
    setTimeout(main, 400);   // the editor mounts after a moment
  });
  const dialog = () => { root.innerHTML = ''; const d = h('div', {role: 'dialog'});
    root.appendChild(d); return d; };
  const button = (d, label, text, fn, disabled) => { const b = h('button', label ? {'aria-label': label} : {}, text);
    if (disabled) b.disabled = true; b.addEventListener('click', fn); d.appendChild(b); return b; };
  function main() {
    const d = dialog();
    button(d, cfg.t.discard, '', () => { root.innerHTML = ''; });
    button(d, cfg.t.edit, '', () => {});
    const n = (tags[sel] || []).length;
    button(d, n ? cfg.t.tagged.replace('{n}', n) : cfg.t.tag, '', tagView);
    const alt = button(d, cfg.t.alt, '', altView);
    alt.setAttribute('aria-pressed', 'false');
    files.forEach((f, i) => {
      const img = h('img', {src: png, alt: cfg.engine === 'page' ? f : 'image ' + i, width: '154', height: '192'});
      img.style.width = '154px'; img.style.height = '192px';
      if (cfg.engine === 'page') {
        const b = button(d, cfg.t.select + ' ' + f, '', () => { sel = i; main(); });
        b.appendChild(img);
      } else {
        const b = h('button', {}); b.appendChild(img); b.addEventListener('click', () => { sel = i; main(); }); d.appendChild(b);
      }
    });
    button(d, '', cfg.t.back, () => { root.innerHTML = ''; });
    button(d, '', cfg.t.next, () => {
      root.innerHTML = '';
      files.forEach(() => { const p = h('img', {src: png, alt: 'Bildvorschau'}); p.style.width = '300px'; p.style.height = '200px'; composer.appendChild(p); });
    });
  }
  function altView() {
    const d = dialog();
    button(d, cfg.t.discard, '', () => {});
    button(d, cfg.t.back, cfg.t.back, main);
    const area = h('textarea', {placeholder: cfg.t.placeholder, maxlength: '1000'});
    area.value = alts[sel] || '';
    d.appendChild(area);
    const ok = button(d, '', alts[sel] ? cfg.t.update : cfg.t.add, () => {
      if (!cfg.drop_alt) alts[sel] = area.value;
      main();
    }, !area.value);
    area.addEventListener('input', () => { ok.disabled = !area.value; });
  }
  function tagView() {
    const d = dialog();
    button(d, cfg.t.discard, '', () => {});
    button(d, cfg.t.back, cfg.t.back, main);
    const box = h('input', {type: 'text', placeholder: cfg.t.name_input});
    d.appendChild(box);
    const list = h('div', {}); d.appendChild(list);
    const chosen = [];
    const add = button(d, '', cfg.t.add, () => { tags[sel] = (tags[sel] || []).concat(chosen); main(); }, true);
    box.addEventListener('input', () => {
      setTimeout(() => {
        list.innerHTML = '';
        const hits = cfg.people.filter(p => p.name.toLowerCase().startsWith(box.value.toLowerCase().split(' ')[0]));
        list['__reactFiber$fake'] = {stateNode: list, memoizedProps: {all: hits.map(p => 'mentionTypeahead_display_' + p.id)}};
        hits.forEach(p => {
          const o = h('div', {role: 'option'}); o.innerText = p.name + '\n2. · ' + p.headline;
          if (cfg.engine !== 'page') o['__reactFiber$fake'] = {stateNode: o, memoizedProps: {},
              return: {stateNode: null, memoizedProps: {item: {meta: {id: 'mentionTypeahead_display_' + p.id}}},
                       return: list['__reactFiber$fake']}};
          o.addEventListener('click', () => { chosen.push(p.id); o.remove(); add.disabled = false; });
          list.appendChild(o);
        });
      }, 300);
    });
  }
})();
</script>
"""

DE = {"discard": "Verwerfen", "edit": "Bearbeiten", "tag": "Tag", "tagged": "Tag, {n} Person getaggt",
      "alt": "Alternativer Text", "select": "Auswählen", "back": "Zurück", "next": "Weiter",
      "add": "Hinzufügen", "update": "Aktualisieren",
      "placeholder": "Wie würden Sie dieses Bild beschreiben?", "name_input": "Namen eingeben"}
EN = {"discard": "Discard", "edit": "Edit", "tag": "Tag", "tagged": "Tag, {n} person tagged",
      "alt": "Alternative text", "select": "Select", "back": "Back", "next": "Next",
      "add": "Add", "update": "Update",
      "placeholder": "How would you describe this image?", "name_input": "Enter a name"}
LOCALES = [pytest.param(DE, id="de"), pytest.param(EN, id="en")]

PEOPLE = [
    {"name": "Max Platzhalter", "id": MAX_A, "headline": "Einkauf"},
    {"name": "Max Platzhalter", "id": MAX_B, "headline": "Labor"},
]


def _html(engine: str, t: dict, **extra) -> str:
    cfg = {"engine": engine, "t": t, "people": PEOPLE, **extra}
    media_label = "Medieninhalte" if t is DE else "Add media"
    return (
        '<html><body><div role="dialog" id="composer">'
        '<div id="ed" role="textbox" contenteditable="true"></div>'
        f'<button id="media" aria-label="{media_label}"></button><button>Posten</button></div>'
        '<div id="root"></div>'
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


@pytest.fixture
def images(tmp_path):
    out = []
    for name in ("grau.png", "blau.png"):
        f = tmp_path / name
        f.write_bytes(b"\x89PNG\r\n\x1a\n")
        out.append(f)
    return out


def _items(images, **per):
    raw = [{"path": str(images[0]), "alt_text": "Graue Fläche"},
           {"path": str(images[1]), "alt_text": "Blaue Fläche", **per}]
    items, bad = check_media(raw)
    assert bad is None, bad
    return items


def _attacher(pg, *, tags: bool) -> MediaAttacher:
    return MediaAttacher(pg, editor_selector="#ed", media_words=words("media"),
                         allow_tags=tags, poll=0.2, timeout=5.0)


async def _no_nav(url):
    raise AssertionError("no navigation expected: targets carry ids")


def test_fixture_is_the_measurement_the_dom_copies() -> None:
    assert FIXTURE["member_editor"]["thumbnail_alt"] == "image <n>"
    assert "no identifier" in FIXTURE["page_editor"]["tag_identity"]


@pytest.mark.parametrize("t", LOCALES)
async def test_member_alt_text_and_tag_by_identifier(page, images, t) -> None:
    await page.set_content(_html("member", t))
    items = _items(images, tags=[{"name": "Max Platzhalter", "target": MAX_B}])
    got = await _attacher(page, tags=True).attach(items, navigate=_no_nav)
    assert got["status"] == "attached", got
    assert got["media"][1] == {"file": "blau.png", "alt_text": "verified", "tags": [MAX_B]}


@pytest.mark.parametrize("t", LOCALES)
async def test_member_alt_text_that_is_not_kept_is_caught_on_reopen(page, images, t) -> None:
    await page.set_content(_html("member", t, drop_alt=True))
    got = await _attacher(page, tags=True).attach(_items(images), navigate=_no_nav)
    assert got["status"] == "alt_text_not_saved", got


@pytest.mark.parametrize("t", LOCALES)
async def test_member_tag_target_absent_is_not_resolved(page, images, t) -> None:
    await page.set_content(_html("member", t))
    other = "ACoAAPLATZHALTER000000000000000000000009"
    items = _items(images, tags=[{"name": "Max Platzhalter", "target": other}])
    got = await _attacher(page, tags=True).attach(items, navigate=_no_nav)
    assert got["status"] == "mention_not_resolved", got


@pytest.mark.parametrize("t", LOCALES)
async def test_page_editor_alt_text_by_file_name(page, images, t) -> None:
    await page.set_content(_html("page", t))
    got = await _attacher(page, tags=False).attach(_items(images), navigate=_no_nav)
    assert got["status"] == "attached", got


@pytest.mark.parametrize("t", LOCALES)
async def test_page_editor_order_is_checked(page, images, t) -> None:
    await page.set_content(_html("page", t, reverse=True))
    got = await _attacher(page, tags=False).attach(_items(images), navigate=_no_nav)
    assert got["status"] == "media_order_mismatch", got


async def test_page_editor_refuses_tags(page, images) -> None:
    await page.set_content(_html("page", DE))
    items = _items(images, tags=[{"name": "Max Platzhalter", "target": MAX_B}])
    got = await _attacher(page, tags=False).attach(items, navigate=_no_nav)
    assert got["status"] == "media_tag_unverifiable", got


async def test_blind_tag_namesake_is_refused_even_alone(page, images) -> None:
    # Barrier: tag suggestions without identifier, exactly one namesake.
    # Unlike a mention there is no entity to read back, so this must stop.
    await page.set_content(_html("page", DE, people=[PEOPLE[1]]))
    items = _items(images, tags=[{"name": "Max Platzhalter", "target": MAX_B}])
    got = await _attacher(page, tags=True).attach(items, navigate=_no_nav)
    assert got["status"] == "media_tag_unverifiable", got
