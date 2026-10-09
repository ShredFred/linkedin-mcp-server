"""Video (member composer) and document (page composer) attach, from the
round-3 measurement in tests/fixtures/company-actions-de-2026-10-09.json.

Unfriendly parts copied: the document's "Fertig" stays disabled while the
upload is processed, the title field appears only after the upload, and the
read-back is the preview iframe's title. All names are invented.
"""

from __future__ import annotations

import json

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.linkedin.ext_composer_labels import words
from linkedin_mcp_server.linkedin.ext_media import MediaAttacher, check_media

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

RUNTIME = r"""
<script>(() => {
  const cfg = window.FAKE;
  const comp = document.getElementById('composer');
  const root = document.getElementById('root');
  const input = document.createElement('input'); input.type = 'file'; input.style.display = 'none';
  document.body.appendChild(input);
  document.getElementById('media').addEventListener('click', () => input.click());
  input.addEventListener('change', () => setTimeout(() => {
    root.innerHTML = '<div role="dialog"><button aria-label="Verwerfen"></button>'
      + '<video src="blob:x" style="width:300px;height:200px"></video>'
      + '<button>Zurück</button><button id="next">Weiter</button></div>';
    document.getElementById('next').addEventListener('click', () => {
      root.innerHTML = '';
      const v = document.createElement('video'); v.style.width = '300px'; v.style.height = '200px';
      comp.appendChild(v);
    });
  }, 400));
  document.getElementById('more').addEventListener('click', () => {
    const b = document.createElement('button'); b.setAttribute('aria-label', 'Dokument hinzufügen');
    comp.appendChild(b);
    b.addEventListener('click', () => {
      root.innerHTML = '<div role="dialog"><h2>Dokumente teilen</h2><button aria-label="Verwerfen"></button>'
        + '<input type="file" id="doc"><button>Zurück</button><button id="done" disabled>Fertig</button></div>';
      document.getElementById('doc').addEventListener('change', () => setTimeout(() => {
        const t = document.createElement('input'); t.type = 'text';
        t.placeholder = 'Fügen Sie einen aussagekräftigen Titel hinzu.';
        document.querySelector('#root [role=dialog]').appendChild(t);
        setTimeout(() => { document.getElementById('done').disabled = false; }, 800);
        document.getElementById('done').addEventListener('click', () => {
          const f = document.createElement('iframe');
          f.title = 'Dokument-Wiedergabe: ' + (cfg.lose_title ? 'Unbenannt' : t.value);
          comp.appendChild(f); root.innerHTML = '';
        });
      }, 500));
    });
  });
})();</script>
"""


def _html(**cfg) -> str:
    return (
        '<html><body><div role="dialog" id="composer">'
        '<div id="ed" role="textbox" contenteditable="true"></div>'
        '<button id="media" aria-label="Medieninhalte"></button>'
        '<button id="more" aria-label="Mehr"></button><button>Posten</button></div>'
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


def _attacher(pg, kinds):
    return MediaAttacher(pg, editor_selector="#ed", media_words=words("media"),
                         allow_tags=False, kinds=kinds, poll=0.2, timeout=6.0)


async def _no_nav(url):
    raise AssertionError("no navigation expected")


@pytest.mark.parametrize("kinds", [("image", "video"), ("image", "video", "document")],
                         ids=["member", "page"])
async def test_video_member_composer(page, tmp_path, kinds) -> None:
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"\x00")
    items, bad = check_media([{"path": str(f)}])
    assert bad is None and items[0]["kind"] == "video"
    await page.set_content(_html())
    got = await _attacher(page, kinds).attach(items, navigate=_no_nav)
    assert got["status"] == "attached", got


async def test_video_refused_where_unmeasured(page, tmp_path) -> None:
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"\x00")
    items, _ = check_media([{"path": str(f)}])
    await page.set_content(_html())
    got = await _attacher(page, ("image", "document")).attach(items, navigate=_no_nav)
    assert got["status"] == "media_kind_unmeasured", got


async def test_document_with_title_read_back(page, tmp_path) -> None:
    f = tmp_path / "deck.pdf"
    f.write_bytes(b"%PDF-1.4")
    items, bad = check_media([{"path": str(f), "title": "Messbericht"}])
    assert bad is None and items[0]["kind"] == "document"
    await page.set_content(_html())
    got = await _attacher(page, ("image", "document")).attach(items, navigate=_no_nav)
    assert got["status"] == "attached", got


async def test_document_title_lost_is_caught(page, tmp_path) -> None:
    f = tmp_path / "deck.pdf"
    f.write_bytes(b"%PDF-1.4")
    items, _ = check_media([{"path": str(f), "title": "Messbericht"}])
    await page.set_content(_html(lose_title=True))
    got = await _attacher(page, ("image", "document")).attach(items, navigate=_no_nav)
    assert got["status"] == "document_not_attached", got


def test_document_needs_a_title(tmp_path) -> None:
    f = tmp_path / "deck.pdf"
    f.write_bytes(b"%PDF-1.4")
    assert check_media([{"path": str(f)}])[1]["status"] == "document_title_required"
    img = tmp_path / "a.png"
    img.write_bytes(b"x")
    assert check_media([{"path": str(f), "title": "x"}, {"path": str(img)}])[1]["status"] == "media_mix_unsupported"
