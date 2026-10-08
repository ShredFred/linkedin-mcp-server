"""Browser-DOM tests for the InMail route: saved leads and the message button.

Two failure classes reproduced on synthetic pages (2026-10-08):

* A lead already saved in Sales Navigator has no "view in Sales Navigator"
  link in the "More" menu, only "Unsave" / "Gespeichert". The route was
  reported as missing although the lead page is reachable by the profile URN.
* A lead page whose message control did not match the exact button label was
  reported as ``inmail_not_allowed`` without any refusal on the page.

Every case runs in German and English UI text. All names, slugs and URNs are
invented placeholders. Skipped when chromium is not installed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.linkedin import ext_inmail
from linkedin_mcp_server.linkedin.ext_inmail import ExtInmail

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

SLUG = "max-platzhalter-0a1b2c"
URN = "ACoAAAPLATZHALTER0001"
OTHER = "ACoAAAPLATZHALTER0002"

DE = {
    "more": "Mehr",
    "view": "In Sales Navigator anzeigen",
    "unsave": "Nicht mehr in Sales Navigator speichern",
    "saved": "Gespeichert",
    "message": "Nachricht",
    "msg_aria": "Nachricht an Max Platzhalter senden",
    "block": "Max Platzhalter nimmt keine InMails an.",
    "upsell": "Premium testen",
    "degree": "· 2.",
}
EN = {
    "more": "More",
    "view": "View in Sales Navigator",
    "unsave": "Unsave from Sales Navigator",
    "saved": "Saved",
    "message": "Message",
    "msg_aria": "Message Max Platzhalter",
    "block": "Max Platzhalter does not accept InMails.",
    "upsell": "Try Premium",
    "degree": "· 2nd",
}
LOCALES = [pytest.param(DE, id="de"), pytest.param(EN, id="en")]


@pytest.fixture
async def page():
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            pg = await browser.new_page()
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            await pg.route(
                "https://www.linkedin.com/**",
                lambda route: route.fulfill(content_type="text/html", body=""),
            )
            await pg.goto(f"https://www.linkedin.com/in/{SLUG}/")
            yield pg
        finally:
            await browser.close()


def _reader(pg: Any, pages: dict[str, str] | None = None) -> ExtInmail:
    reader = ExtInmail.__new__(ExtInmail)

    async def delay(_s: float) -> None:
        return None

    reader._session = SimpleNamespace(page=pg, delay=delay)  # type: ignore[attr-defined]

    async def goto(url: str) -> None:
        if pages and url in pages:
            await pg.set_content(pages[url])

    reader._goto = goto  # type: ignore[method-assign]
    return reader


def _profile(t: dict[str, str], menu: list[str], *, top_extra: str = "",
             data: str = "") -> str:
    items = "".join(menu)
    return f"""<html><head><title>Max Platzhalter | LinkedIn</title></head><body>
<main><section>
  <h1>Max Platzhalter</h1><span>{t['degree']}</span>
  <div><button>{t['more']}</button>{top_extra}</div>
  <div role="menu" style="display:block">{items}</div>
</section>
<section><a href="/in/jemand-anders/" data-urn="urn:li:fsd_profile:{OTHER}">Andere Person</a></section>
</main>{data}</body></html>"""


async def _target(pg: Any, html: str) -> dict[str, Any]:
    await pg.set_content(html)
    reader = _reader(pg)
    return await reader.inmail_target(SLUG)


@pytest.mark.parametrize("t", LOCALES)
async def test_view_link_still_wins(page, t):
    html = _profile(t, [f'<a role="menuitem" href="/sales/people/{URN},name,x/">{t["view"]}</a>'])
    got = await _target(page, html)
    assert got["status"] == "ok" and got["route"] == "view_link"


@pytest.mark.parametrize("t", LOCALES)
async def test_saved_lead_with_compose_link_is_a_route(page, t):
    html = _profile(t, [
        f'<a role="menuitem" href="#">{t["unsave"]}</a>',
        f'<a role="menuitem" href="/messaging/compose/?profileUrn=urn%3Ali%3Afsd_profile%3A{URN}">'
        f'{t["message"]}</a>',
    ])
    got = await _target(page, html)
    assert got["status"] == "ok", got
    assert got["saved_lead"] is True
    assert got["sales_url"] == f"https://www.linkedin.com/sales/people/{URN},name"


@pytest.mark.parametrize("t", LOCALES)
async def test_saved_button_and_slug_bound_urn(page, t):
    # "Gespeichert"/"Saved" in the top card; the URN comes from the page data
    # next to the own slug, never from the other person's card below.
    data = (
        '<code style="display:none">{"publicIdentifier":"' + SLUG
        + '","entityUrn":"urn:li:fsd_profile:' + URN + '"}</code>'
    )
    html = _profile(t, [], top_extra=f"<button>{t['saved']}</button>", data=data)
    got = await _target(page, html)
    assert got["status"] == "ok", (got.get("reason"), got.get("saved_lead"))
    assert got["route"] == "profile_urn" and URN in got["sales_url"]


@pytest.mark.parametrize("t", LOCALES)
async def test_saved_lead_with_two_urns_is_not_guessed(page, t):
    html = _profile(
        t, [f'<a role="menuitem" href="#">{t["unsave"]}</a>'],
        top_extra=(f'<img data-a="urn:li:fsd_profile:{URN}">'
                   f'<img data-b="urn:li:fsd_profile:{OTHER}">'),
    )
    got = await _target(page, html)
    assert got["status"] == "no_sales_navigator_route"
    assert got["reason"] == "ambiguous_profile_urn" and got["saved_lead"] is True


@pytest.mark.parametrize("t", LOCALES)
async def test_profile_message_button_is_reported(page, t):
    html = _profile(t, [], top_extra=f"<button>{t['message']}</button>")
    got = await _target(page, html)
    assert got["profile_message_button"] is True
    assert got["reason"] == "no_view_link"


# -- lead page ---------------------------------------------------------------

LEAD_URL = f"https://www.linkedin.com/sales/people/{URN},name"


def _lead(body: str) -> str:
    return f"<html><body><main><h1>Max Platzhalter</h1><p>2nd</p>{body}</main></body></html>"


async def _inmail(pg: Any, html: str, monkeypatch, **target: Any) -> dict[str, Any]:
    monkeypatch.setattr(ext_inmail, "_MESSAGE_BUTTON_POLL_SECONDS", 0.0)
    reader = _reader(pg, {LEAD_URL: html})
    tgt = {"sales_url": LEAD_URL, "name": "Max Platzhalter", **target}
    return await reader.inmail(tgt, "Betreff", "Text", confirm=False)


@pytest.mark.parametrize("t", LOCALES)
async def test_aria_only_message_button_is_found(page, t, monkeypatch):
    # Icon button: no text, the label only in aria-label. Must reach the
    # composer step (here: no dialog in the mock -> composer_not_opened),
    # not inmail_not_allowed.
    monkeypatch.setattr(ext_inmail, "_SN_DIALOG", "section.never")
    html = _lead(f'<button aria-label="{t["msg_aria"]}"><svg></svg></button>')

    got = await _inmail(page, html, monkeypatch)
    assert got["status"] in ("composer_not_opened",), got


@pytest.mark.parametrize("t", LOCALES)
async def test_no_button_without_evidence_is_not_a_refusal(page, t, monkeypatch):
    html = _lead(f"<button>{t['upsell']}</button><button>{t['more']}</button>")
    got = await _inmail(page, html, monkeypatch, profile_message_button=True)
    assert got["status"] == "message_button_not_found", got
    assert got["detail"] == {"profile_message_button": True, "upsell_visible": True}


@pytest.mark.parametrize("t", LOCALES)
async def test_refusal_text_is_inmail_not_allowed(page, t, monkeypatch):
    html = _lead(f"<p>{t['block']}</p><button>{t['more']}</button>")
    got = await _inmail(page, html, monkeypatch)
    assert got["status"] == "inmail_not_allowed" and got["evidence"]


@pytest.mark.parametrize("t", LOCALES)
async def test_disabled_message_button_is_inmail_not_allowed(page, t, monkeypatch):
    html = _lead(f"<button disabled>{t['message']}</button>")
    got = await _inmail(page, html, monkeypatch)
    assert got["status"] == "inmail_not_allowed"
    assert got["evidence"] == "message_button_disabled"


@pytest.mark.parametrize("t", LOCALES)
async def test_inbox_link_is_not_a_message_button(page, t, monkeypatch):
    inbox = "Nachrichten" if t is DE else "Messaging"
    html = _lead(f'<button aria-label="{inbox}">{inbox}</button>')
    got = await _inmail(page, html, monkeypatch)
    assert got["status"] == "message_button_not_found"
