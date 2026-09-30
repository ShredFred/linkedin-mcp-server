"""MiViA fork: regression cases for the 2026-09-30 outreach hardening.

The page structures below are the measured 2026 top card of four live
German-locale profiles (identifiers, names and component ids replaced). They
cover what broke on 2026-09-29/30:

* Pending, and Connect, sitting only in the More menu, marked by a
  locale-independent ``componentkey`` -- three of the eight
  ``connect_unavailable`` results of 2026-09-29 were pending there.
* Umlaut slugs, which LinkedIn emits percent-encoded -- the vanityName
  selector never matched them.
* The ``?isSelfProfile=false`` redirect and other benign query keys, which
  made every send fail with ``recipient_resolution_failed``.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from patchright.async_api import Page, async_playwright

from linkedin_mcp_server.scraping import message_sender as ms
from linkedin_mcp_server.scraping import mivia_urls
from linkedin_mcp_server.scraping.connection import (
    ActionSignals,
    detect_connection_state,
)
from linkedin_mcp_server.scraping.connection_actions import ConnectionActions
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

USER = "dieter-könig-1b0000000"
USER_HREF = "dieter-k%C3%B6nig-1b0000000"
PROFILE = f"https://www.linkedin.com/in/{USER_HREF}/?isSelfProfile=false"


# --------------------------------------------------------------------------
# URL normalisation (one place)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (PROFILE, f"/in/{USER_HREF}/"),
        # LinkedIn has emitted lower-case hex; same person.
        (
            "https://www.linkedin.com/in/dieter-k%c3%b6nig-1b0000000/",
            f"/in/{USER_HREF}/",
        ),
        (
            "https://www.linkedin.com/in/jane/?isSelfProfile=false&trk=x&lipi=y",
            "/in/jane/",
        ),
        ("https://www.linkedin.com/in/jane/de/", None),
        ("https://www.linkedin.com/in/jane", "/in/jane/"),
        ("https://de.linkedin.com/in/jane/?originalSubdomain=de", "/in/jane/"),
        # Fail closed:
        ("https://www.linkedin.com/in/jane/?isSelfProfile=true", None),
        ("https://www.linkedin.com/in/jane/?trk=a&trk=b", None),
        ("https://www.linkedin.com/in/jane/?miniProfileUrn=urn", None),
        ("https://www.linkedin.com/in/jane/?recipient=ACoAAx", None),
        ("https://www.linkedin.com/in/jane/#experience", None),
        ("https://www.linkedin.com/in/jane/edit/intro/", None),
        ("https://www.linkedin.com/in/jane/details/", None),
        ("https://www.linkedin.com/in/jane%2Fedit/", None),
        ("https://www.linkedin.com/in/%ZZ/", None),
        ("https://www.linkedin.com/in/%C3/", None),
        ("http://www.linkedin.com/in/jane/", None),
        ("https://www.linkedin.com.evil.example/in/jane/", None),
        ("https://www.linkedin.com/sales/lead/ACoAA,NAME_SEARCH/", None),
    ],
)
def test_profile_path_normalisation(url: str, expected: str | None) -> None:
    assert ms._profile_path_from_url(url) == expected


def test_identity_key_folds_encoding_and_case() -> None:
    assert mivia_urls.identity_path(USER) == mivia_urls.identity_path(
        "Dieter-K%C3%B6nig-1B0000000"
    )
    assert mivia_urls.identity_path("sascha-pleßer-6a") == "/in/sascha-pleßer-6a/"


def test_compose_url_urn_mismatch_fails_closed() -> None:
    href = (
        "/messaging/compose/?profileUrn=urn%3Ali%3Afsd_profile%3AACoAAone"
        "&recipient=ACoAAtwo&screenContext=NON_SELF_PROFILE_VIEW"
    )
    assert ms._profile_urn_from_compose_url(href, base=PROFILE) is None


# --------------------------------------------------------------------------
# Recorded 2026 top cards
# --------------------------------------------------------------------------

_SALES_NAV = '<button type="button" aria-label="Profil speichern">In Sales Navigator speichern</button>'
_MORE = '<button type="button" aria-expanded="false">Mehr</button>'
_STICKY_MORE = '<button type="button" aria-expanded="false" aria-label="Mehr"></button>'
_COMPOSE = (
    '<a href="/messaging/compose/?profileUrn=urn%3Ali%3Afsd_profile%3AACoAAx'
    '&recipient=ACoAAx&screenContext=NON_SELF_PROFILE_VIEW">Nachricht</a>'
)


def _top(*actions: str) -> str:
    row = "".join(actions)
    return f"""
<section>
  <a href="{PROFILE}" aria-label="Mitteilungen verwalten"><svg></svg></a>
  <h1>Dieter</h1>
  <div class="actions"><div>{row}</div></div>
  <div class="sticky"><div>{row.replace(_MORE, _STICKY_MORE)}</div></div>
</section>"""


def _menu(*items: str) -> str:
    return '<div role="menu">' + "".join(items) + "</div>"


_MENU_PENDING = (
    f'<a role="menuitem" href="{PROFILE}" '
    'componentkey="ConnectButtonstate:invitation:urn:li:member:1_pending">Ausstehend</a>'
)
_MENU_CONNECT = (
    f'<a role="menuitem" href="/preload/custom-invite/?vanityName={USER_HREF}" '
    'componentkey="ConnectButtonstate:invitation:urn:li:member:1_connect">Vernetzen</a>'
)
_MENU_FOLLOW = '<div role="menuitem" componentkey="auto-component-1">Folgen</div>'
_MENU_REPORT = f'<a role="menuitem" href="{PROFILE}">Melden</a>'
_TOP_CONNECT = (
    f'<a href="/preload/custom-invite/?vanityName={USER_HREF}" aria-label="Einladen" '
    'componentkey="ConnectButtonstate:invitation:urn:li:member:1_conn">Vernetzen</a>'
)
_OTHER_CONNECT = (
    '<aside><a href="/preload/custom-invite/?vanityName=someone-else" '
    'componentkey="ConnectButtonstate:invitation:urn:li:member:2_conn">Vernetzen</a>'
    '<a componentkey="ConnectButtonstate:invitation:urn:li:member:3_pending">x</a></aside>'
)


def _page(main: str, portal: str = "") -> str:
    return f"<html><body><main>{main}</main>{portal}</body></html>"


CASES = {
    # Thomas-Kern-shape: Message, Sales Navigator, More; Pending in More.
    "pending_in_more": (
        _page(
            _top(_COMPOSE, _SALES_NAV, _MORE),
            _menu(_MENU_FOLLOW, _MENU_PENDING, _MENU_REPORT),
        ),
        "pending",
    ),
    # Lukas-Meluhn-shape: Connect in the top card, Message only in More.
    "connect_top": (
        _page(_top(_TOP_CONNECT, _SALES_NAV, _MORE) + _OTHER_CONNECT),
        "connectable",
    ),
    # Connect only in the More menu.
    "connect_in_more": (
        _page(_top(_COMPOSE, _SALES_NAV, _MORE), _menu(_MENU_CONNECT, _MENU_FOLLOW)),
        "connectable",
    ),
    # Follow only: menu has neither Connect nor Pending.
    "follow_only": (
        _page(_top(_COMPOSE, _SALES_NAV, _MORE), _menu(_MENU_FOLLOW, _MENU_REPORT)),
        "follow_only",
    ),
    # First degree: Message and More, nothing labeled.
    "connected": (_page(_top(_COMPOSE, _MORE)), "already_connected"),
    # Another member's pending card elsewhere on the page is not ours.
    "sidebar_pending_is_not_ours": (
        _page(_top(_COMPOSE, _SALES_NAV, _MORE) + _OTHER_CONNECT),
        "follow_only",
    ),
    # 2026 layout: an outer wrapper section holds the top card and a later
    # section with another member's Pending card.
    "wrapper_section_other_pending": (
        _page(
            "<section>"
            + _top(_COMPOSE, _SALES_NAV, _MORE)
            + '<section><a componentkey="ConnectButtonstate:invitation:'
            'urn:li:member:9_pending">x</a></section></section>'
        ),
        "follow_only",
    ),
}


@pytest.fixture
async def dom_page():
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True, channel="chromium")
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"chromium unavailable: {exc}")
    page = await browser.new_page()
    try:
        yield page
    finally:
        await browser.close()
        await pw.stop()


def _dom_actions(page: Any) -> ConnectionActions:
    async def unreachable(_username: str) -> dict[str, Any]:
        raise AssertionError("not read")

    session = ScrapingSession(cast(Page, page))
    return ConnectionActions(session, PageNavigator(session), unreachable)


@pytest.mark.browser_dom
@pytest.mark.xdist_group("browser_runtime")
@pytest.mark.parametrize("name", sorted(CASES))
async def test_recorded_2026_top_cards(dom_page, name: str) -> None:
    html, expected = CASES[name]
    await dom_page.set_content(html)
    signals = await _dom_actions(dom_page)._read_action_signals(USER)
    assert detect_connection_state(signals) == expected, (name, signals)


@pytest.mark.browser_dom
@pytest.mark.xdist_group("browser_runtime")
async def test_more_opener_found_without_message_anchor(dom_page) -> None:
    from linkedin_mcp_server.scraping.connection_actions import OPEN_MORE_BUTTON_JS

    await dom_page.set_content(_page(_top(_TOP_CONNECT, _SALES_NAV, _MORE)) + "")
    await dom_page.evaluate(
        "() => document.querySelectorAll('button[aria-expanded]').forEach("
        "b => b.addEventListener('click', () => b.dataset.hit = b.getAttribute('aria-label') === null ? 'top' : 'sticky'))"
    )
    assert await dom_page.evaluate(OPEN_MORE_BUTTON_JS) is True
    assert await dom_page.evaluate(
        "() => Array.from(document.querySelectorAll('[data-hit]')).map(b => b.dataset.hit)"
    ) == ["top"]


# --------------------------------------------------------------------------
# connect_with_person flow with the More menu
# --------------------------------------------------------------------------


def _sig(**kw: bool) -> ActionSignals:
    base = dict(
        has_invite_anchor=False,
        has_compose_anchor_in_action_root=False,
        has_edit_intro_anchor=False,
        has_labeled_action_button=False,
        has_labeled_action_anchor=False,
        has_incoming_action_row=False,
    )
    base.update(kw)
    return ActionSignals(**base)


def _flow_actions(mock_page: Any, texts: list[str]) -> ConnectionActions:
    pages = [{"sections": {"main_profile": t}} for t in texts]
    read = (
        AsyncMock(side_effect=pages)
        if len(pages) > 1
        else AsyncMock(return_value=pages[0])
    )
    session = ScrapingSession(mock_page)
    return ConnectionActions(session, PageNavigator(session), read)


FOLLOWISH = dict(has_compose_anchor_in_action_root=True, has_labeled_action_button=True)


async def test_pending_in_more_menu_is_pending_and_writes_nothing(mock_page) -> None:
    actions = _flow_actions(mock_page, ["text"])
    with (
        patch.object(
            actions,
            "_read_action_signals",
            new_callable=AsyncMock,
            side_effect=[
                _sig(**FOLLOWISH),
                _sig(**FOLLOWISH, has_pending_invitation_key=True),
            ],
        ),
        patch.object(
            actions, "_open_more_menu", new_callable=AsyncMock, return_value=True
        ),
        patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock) as nav,
    ):
        result = await actions.connect_with_person(USER)
    assert result["status"] == "pending"
    nav.assert_not_awaited()


async def test_connect_in_more_menu_sends_via_deeplink(mock_page) -> None:
    actions = _flow_actions(mock_page, ["text", "after"])
    with (
        patch.object(
            actions,
            "_read_action_signals",
            new_callable=AsyncMock,
            side_effect=[
                _sig(**FOLLOWISH),
                _sig(**FOLLOWISH, has_invite_anchor=True),
                _sig(**FOLLOWISH, has_pending_invitation_key=True),
            ],
        ),
        patch.object(
            actions, "_open_more_menu", new_callable=AsyncMock, return_value=True
        ),
        patch.object(
            actions,
            "_submit_invite_dialog",
            new_callable=AsyncMock,
            return_value=(True, False, None),
        ),
        patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock) as nav,
    ):
        result = await actions.connect_with_person(USER)
    assert result["status"] == "connected"
    assert result["connect_via"] == "more_menu"
    (url,), _ = nav.call_args
    assert url.endswith("vanityName=dieter-k%C3%B6nig-1b0000000")


async def test_connect_only_card_without_message_opens_more(mock_page) -> None:
    actions = _flow_actions(mock_page, ["text"])
    with (
        patch.object(
            actions,
            "_read_action_signals",
            new_callable=AsyncMock,
            side_effect=[_sig(), _sig()],
        ),
        patch.object(
            actions, "_open_more_menu", new_callable=AsyncMock, return_value=True
        ) as more,
        patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock) as nav,
    ):
        result = await actions.connect_with_person(USER)
    more.assert_awaited_once()
    assert result["status"] == "connect_unavailable"
    nav.assert_not_awaited()


async def test_follow_only_needs_a_read_menu(mock_page) -> None:
    actions = _flow_actions(mock_page, ["text"])
    with (
        patch.object(
            actions,
            "_read_action_signals",
            new_callable=AsyncMock,
            side_effect=[_sig(**FOLLOWISH), _sig(**FOLLOWISH)],
        ),
        patch.object(
            actions, "_open_more_menu", new_callable=AsyncMock, return_value=True
        ),
        patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
    ):
        result = await actions.connect_with_person(USER)
    assert result["status"] == "follow_only"


def test_pending_key_beats_a_stale_invite_anchor() -> None:
    assert (
        detect_connection_state(
            _sig(has_invite_anchor=True, has_pending_invitation_key=True)
        )
        == "pending"
    )


# --------------------------------------------------------------------------
# outreach_selftest
# --------------------------------------------------------------------------


async def _selftest(
    mock_page: Any, page_url: str, resolution: Any, signals: ActionSignals
) -> dict[str, Any]:
    from linkedin_mcp_server.scraping import mivia_selftest

    mock_page.url = page_url
    session = ScrapingSession(mock_page)
    with (
        patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
        patch.object(
            ms.MessageSender,
            "_read_profile_message_target",
            new_callable=AsyncMock,
            return_value=resolution,
        ),
        patch.object(
            ConnectionActions,
            "_read_action_signals",
            new_callable=AsyncMock,
            return_value=signals,
        ),
        patch.object(
            ConnectionActions,
            "_open_more_menu",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        return await mivia_selftest.outreach_selftest(
            session, PageNavigator(session), canary="frederikstadler"
        )


def _resolved() -> Any:
    return ms._ProfileMessageTargetResolution(
        "resolved",
        ms._ProfileMessageTarget(
            "/in/frederikstadler/",
            "ACoAAx",
            "https://www.linkedin.com/messaging/compose/?recipient=ACoAAx",
            "F",
        ),
    )


async def test_selftest_ok_on_current_layout(mock_page) -> None:
    result = await _selftest(
        mock_page,
        "https://www.linkedin.com/in/frederikstadler/?isSelfProfile=false",
        _resolved(),
        _sig(has_compose_anchor_in_action_root=True),
    )
    assert result["ok"] is True, result["problems"]
    assert result["message"]["page_url_query_keys"] == ["isSelfProfile"]


async def test_selftest_flags_an_unknown_query_key(mock_page) -> None:
    result = await _selftest(
        mock_page,
        "https://www.linkedin.com/in/frederikstadler/?newThing=1",
        _resolved(),
        _sig(has_compose_anchor_in_action_root=True),
    )
    assert result["ok"] is False
    assert result["message"]["unknown_query_keys"] == ["newThing"]


async def test_selftest_flags_a_lost_message_action(mock_page) -> None:
    result = await _selftest(
        mock_page,
        "https://www.linkedin.com/in/frederikstadler/",
        ms._ProfileMessageTargetResolution("failed"),
        _sig(),
    )
    assert result["ok"] is False
    assert len(result["problems"]) == 2


# --------------------------------------------------------------------------
# Review findings (mco/cursor, 2026-09-30)
# --------------------------------------------------------------------------


async def test_stale_connect_with_pending_in_menu_sends_nothing(mock_page) -> None:
    """P0: a top-card Connect next to a Pending shown only in More."""
    actions = _flow_actions(mock_page, ["text"])
    with (
        patch.object(
            actions,
            "_read_action_signals",
            new_callable=AsyncMock,
            return_value=_sig(has_invite_anchor=True),
        ),
        patch.object(
            actions,
            "_more_menu_shows_pending",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock) as nav,
    ):
        result = await actions.connect_with_person(USER)
    assert result["status"] == "pending"
    nav.assert_not_awaited()


@pytest.mark.browser_dom
@pytest.mark.xdist_group("browser_runtime")
async def test_menu_pending_peek_reads_only_the_menu(dom_page) -> None:
    from linkedin_mcp_server.scraping.connection_actions import MENU_PENDING_JS

    await dom_page.set_content(_page(_top(_COMPOSE, _MORE) + _OTHER_CONNECT))
    assert await dom_page.evaluate(MENU_PENDING_JS) is False
    await dom_page.set_content(_page(_top(_COMPOSE, _MORE), _menu(_MENU_PENDING)))
    assert await dom_page.evaluate(MENU_PENDING_JS) is True


@pytest.mark.browser_dom
@pytest.mark.xdist_group("browser_runtime")
@pytest.mark.parametrize(
    "href",
    [
        f"https://evil.example/preload/custom-invite/?vanityName={USER_HREF}",
        f"/preload/custom-invite/extra/?vanityName={USER_HREF}",
        f"/preload/custom-invite/?vanityName={USER_HREF}&vanityName=x",
    ],
)
async def test_invite_gate_requires_linkedins_own_route(dom_page, href: str) -> None:
    await dom_page.set_content(
        _page(_top(f'<a href="{href}" aria-label="x">Vernetzen</a>', _SALES_NAV, _MORE))
    )
    signals = await _dom_actions(dom_page)._read_action_signals(USER)
    assert signals.has_invite_anchor is False


async def test_send_refuses_a_redirect_to_another_slug(mock_page) -> None:
    """P0: the landed page must be the requested person."""
    other = ms._ProfileMessageTargetResolution(
        "resolved",
        ms._ProfileMessageTarget(
            "/in/someone-else/",
            "ACoAAx",
            "https://www.linkedin.com/messaging/compose/?recipient=ACoAAx",
            "S",
        ),
    )
    session = ScrapingSession(mock_page)
    sender = ms.MessageSender(session, PageNavigator(session))
    with (
        patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
        patch.object(
            ms.MessageSender,
            "_read_profile_message_target",
            new_callable=AsyncMock,
            return_value=other,
        ),
    ):
        result = await sender.send_message(
            "frederikstadler", "Hallo", confirm_send=True
        )
    assert result["status"] == "recipient_resolution_failed"
