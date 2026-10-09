"""Company actions against attrappes of the 2026-10-09 measurement (round 3).

``tests/fixtures/company-actions-de-2026-10-09.json`` holds the measurement.
The admin post card copies what makes the job hard: the submit button only
exists once text is in the box, the identity is visible in the comment box's
placeholder *and* in a modal whose radio decides, and the modal is shared by
comment and reaction. All names and ids are invented.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.linkedin import ext_company_actions as ca
from linkedin_mcp_server.linkedin.ext_mentions import parse_mentions

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "company-actions-de-2026-10-09.json").read_text(
        encoding="utf-8"
    )
)
ACT = "7000000000000000001"
PAGE = "Acme Labs"


def _admin_html(*, checked: str = "page", placeholder: str = f"Kommentieren als {PAGE} …",
                liked: bool = False) -> str:
    like_label = "Mit „Gefällt mir“ reagieren" if not liked else "Reaktion „Gefällt mir“ entfernen"
    return f"""<html><body>
<div role="article" data-urn="urn:li:activity:{ACT}">
  <p>Beitragstext</p>
  <button aria-label="Bei Reaktionen auf diesen Beitrag Menü für den Identitätswechsel öffnen" id="sw"></button>
  <button aria-label="{like_label}" id="like">Gefällt mir</button>
  <button aria-label="Kommentieren">Kommentieren</button>
  <form><div class="ql-editor" contenteditable="true" role="textbox"
       aria-placeholder="{placeholder}" aria-label="Texteditor zum Erstellen von Inhalten"></div>
  <span id="slot"></span></form>
  <div class="comments"></div>
</div>
<div id="modal"></div>
<script>(() => {{
  const ed = document.querySelector('.ql-editor');
  ed.addEventListener('input', () => {{
    const slot = document.getElementById('slot');
    if (ed.innerText.trim() && !slot.firstChild) {{
      const b = document.createElement('button'); b.type = 'button'; b.innerHTML = '<span> Kommentieren </span>';
      b.addEventListener('click', () => {{
        const c = document.createElement('article');
        c.innerText = '{PAGE}\\n' + ed.innerText; document.querySelector('.comments').appendChild(c);
      }});
      setTimeout(() => slot.appendChild(b), 300);
    }}
  }});
  document.getElementById('like').addEventListener('click', e => {{
    e.target.setAttribute('aria-label', 'Reaktion „Gefällt mir“ entfernen');
  }});
  document.getElementById('sw').addEventListener('click', () => {{
    const self = '{checked}' === 'self';
    document.getElementById('modal').innerHTML = `<div role="dialog">
      <button aria-label="Verwerfen" onclick="document.getElementById('modal').innerHTML=''"></button>
      <h2>Kommentieren, reagieren und teilen Sie im Namen von</h2>
      <ul><li><div class="artdeco-entity-lockup__title">Max Platzhalter</div>
        <input id="select-self" name="actorSelector" type="radio" aria-checked="${{self}}"></li>
      <li><div class="artdeco-entity-lockup__title">{PAGE}</div>
        <input id="select-acme-labs" name="actorSelector" type="radio" aria-checked="${{!self}}"></li></ul>
      <button aria-label="Auswahl speichern" disabled>Speichern</button></div>`;
  }});
}})();</script></body></html>"""


def _member_html(state: str = "Keine Reaktion") -> str:
    return f"""<html><body>
<button id="st" aria-label="Status des Reaktionsbuttons: {state}">46</button>
<div id="pal" style="display:none">
  <button aria-label="Gefällt mir"></button><button aria-label="Applaus"></button>
  <button aria-label="Unterstütze ich"></button><button aria-label="Wunderbar"></button>
  <button aria-label="Inspirierend"></button><button aria-label="Lustig"></button>
</div>
<script>(() => {{
  const st = document.getElementById('st'), pal = document.getElementById('pal');
  st.addEventListener('mouseenter', () => {{ pal.style.display = 'block'; }});
  pal.querySelectorAll('button').forEach(b => b.addEventListener('click', () => {{
    st.setAttribute('aria-label', 'Status des Reaktionsbuttons: ' + b.getAttribute('aria-label'));
    pal.style.display = 'none';
  }}));
}})();</script></body></html>"""


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


def _actions(pg, html: str) -> ca.ExtCompanyActions:
    async def navigate(url):
        await pg.set_content(html)

    return ca.ExtCompanyActions(
        SimpleNamespace(page=pg), SimpleNamespace(_navigate_to_page=navigate), poll=0.1
    )


def _segs(text):
    segs, bad = parse_mentions(text)
    assert bad is None
    return segs


def test_role_is_read_from_the_own_row() -> None:
    text = FIXTURE["role"]["admin_table_text"]
    assert ca.parse_own_role(text) == "super_admin"
    other = text.replace("Super-Admin\n\n\nErika", "Content-Admin\n\n\nErika", 1)
    assert ca.parse_own_role(other) == "content_admin"
    assert ca.parse_own_role("Admins verwalten\nKeine Liste") is None


def test_reaction_state() -> None:
    assert ca.reaction_state("Status des Reaktionsbuttons: Keine Reaktion") == "keine reaktion"
    assert ca.reaction_state("Status des Reaktionsbuttons: Applaus") == "applaus"
    assert ca.reaction_state("Mit „Gefällt mir“ reagieren") is None


async def test_comment_as_page_dry_run(page) -> None:
    got = await _actions(page, _admin_html()).comment_as_page(
        "1000001", PAGE, ACT, _segs("Danke fürs Teilen!"), confirm=False
    )
    assert got["status"] == "dry_run", got
    assert (await page.inner_text(".ql-editor")).strip() == ""


async def test_comment_as_page_confirm_reads_back(page) -> None:
    acts = _actions(page, _admin_html())
    got = await acts.comment_as_page("1000001", PAGE, ACT, _segs("Danke fürs Teilen!"), confirm=True)
    assert got["status"] == "posted" and got["verified"] is True, got
    assert acts.comment_submitted is True


async def test_identity_on_self_stops_before_writing(page) -> None:
    # Barrier: the modal's checked radio decides, even when the placeholder
    # names the page. Removing the identity check turns this red.
    got = await _actions(page, _admin_html(checked="self")).comment_as_page(
        "1000001", PAGE, ACT, _segs("Danke!"), confirm=True
    )
    assert got["status"] == "identity_not_page", got
    assert (await page.inner_text(".ql-editor")).strip() == ""


async def test_placeholder_without_page_stops(page) -> None:
    got = await _actions(page, _admin_html(placeholder="Kommentar hinzufügen …")).comment_as_page(
        "1000001", PAGE, ACT, _segs("Danke!"), confirm=False
    )
    assert got["status"] == "identity_not_confirmed", got


async def test_post_not_in_admin_view(page) -> None:
    got = await _actions(page, _admin_html()).comment_as_page(
        "1000001", PAGE, "7000000000000000009", _segs("Danke!"), confirm=False
    )
    assert got["status"] == "post_not_in_admin_view", got


async def test_react_as_page_like_reads_label_change(page) -> None:
    got = await _actions(page, _admin_html()).react(
        ACT, "like", confirm=True, page_id="1000001", page_name=PAGE
    )
    assert got["status"] == "verified", got


async def test_react_as_page_refuses_when_identity_is_self(page) -> None:
    got = await _actions(page, _admin_html(checked="self")).react(
        ACT, "like", confirm=True, page_id="1000001", page_name=PAGE
    )
    assert got["status"] == "identity_not_page", got
    assert await page.get_attribute("#like", "aria-label") == "Mit „Gefällt mir“ reagieren"


async def test_react_as_member_dry_and_confirm(page) -> None:
    acts = _actions(page, _member_html())
    got = await acts.react(ACT, "celebrate", confirm=False)
    assert got["status"] == "dry_run", got
    got = await acts.react(ACT, "celebrate", confirm=True)
    assert got["status"] == "verified" and got["state"] == "applaus", got


async def test_react_as_member_already_reacted(page) -> None:
    got = await _actions(page, _member_html("Gefällt mir")).react(ACT, "like", confirm=True)
    assert got["status"] == "already_reacted", got
