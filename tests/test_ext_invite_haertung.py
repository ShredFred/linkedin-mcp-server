"""Hardening round 2026-10-01: click markers of the invite probe and withdraw."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.linkedin.connection import ActionSignals
from linkedin_mcp_server.linkedin.connection_actions import ConnectionActions
from linkedin_mcp_server.linkedin.ext_actions import ExtActions
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession


def _probe_page(*, click_raises: bool, dialog_left: int) -> MagicMock:
    page = MagicMock()
    buttons = MagicMock()
    buttons.count = AsyncMock(return_value=3)
    button = MagicMock()
    button.click = AsyncMock(side_effect=RuntimeError("detached") if click_raises else None)
    buttons.nth = MagicMock(return_value=button)
    textarea = MagicMock()
    textarea.count = AsyncMock(return_value=0)
    visible = MagicMock()
    visible.count = AsyncMock(return_value=dialog_left)

    def locator_for(selector: str):
        if "visible=true" in selector:
            return visible
        return textarea if "textarea" in selector else buttons

    page.locator.side_effect = locator_for
    page.wait_for_selector = AsyncMock(side_effect=PlaywrightTimeoutError("no"))
    return page


def _actions(page) -> ConnectionActions:
    session = PageSession(page)
    return ConnectionActions(session, PageNavigator(session), AsyncMock())


async def _probe(page) -> ConnectionActions:
    actions = _actions(page)
    with (
        patch.object(actions, "_dialog_is_open", new_callable=AsyncMock, return_value=True),
        patch.object(
            actions, "_get_premium_upsell_message", new_callable=AsyncMock, return_value=None
        ),
        patch.object(actions, "_dismiss_dialog", new_callable=AsyncMock),
    ):
        await actions._probe_invite_note_limit()
    return actions


async def test_probe_click_that_closes_the_dialog_counts_as_send():
    actions = await _probe(_probe_page(click_raises=False, dialog_left=0))
    assert actions.send_clicked is True


async def test_probe_click_that_raises_counts_as_send():
    actions = await _probe(_probe_page(click_raises=True, dialog_left=1))
    assert actions.send_clicked is True


async def test_probe_click_with_dialog_still_open_is_no_send():
    actions = await _probe(_probe_page(click_raises=False, dialog_left=1))
    assert actions.send_clicked is False


async def test_follow_only_after_a_probe_that_may_have_sent_is_send_failed():
    """follow_only is booked not_sent unconditionally; a possible send must
    surface as send_failed so connect_guarded books unknown."""
    actions = _actions(MagicMock())
    actions._read_main_profile = AsyncMock(
        return_value={"sections": {"main_profile": "X\nFollow"}}
    )
    no_invite = ActionSignals(
        has_invite_anchor=False,
        has_compose_anchor_in_action_root=True,
        has_edit_intro_anchor=False,
        has_labeled_action_button=True,
        has_labeled_action_anchor=False,
        has_incoming_action_row=False,
    )

    async def probe():
        actions.send_clicked = True
        return None

    with (
        patch.object(actions, "_read_action_signals", new_callable=AsyncMock, return_value=no_invite),
        patch.object(actions, "_open_more_menu", new_callable=AsyncMock, return_value=True),
        patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
        patch.object(actions, "_probe_invite_note_limit", side_effect=probe),
    ):
        result = await actions.connect_with_person("someone", note="Hallo")
    assert result["status"] == "send_failed"


def _withdraw_page(*, dialog: bool, confirm: bool) -> MagicMock:
    page = MagicMock()
    page.evaluate = AsyncMock(return_value=True)
    page.keyboard.press = AsyncMock()
    link = MagicMock()
    link.scroll_into_view_if_needed = AsyncMock()
    link.click = AsyncMock()
    confirm_btn = MagicMock()
    confirm_btn.count = AsyncMock(return_value=1 if confirm else 0)
    confirm_btn.click = AsyncMock()
    dlg = MagicMock()
    dlg.count = AsyncMock(return_value=1 if dialog else 0)
    dlg.locator.return_value.filter.return_value.first = confirm_btn

    def locator_for(selector: str):
        holder = MagicMock()
        holder.first = link if "data-ext-withdraw" in selector else dlg
        return holder

    page.locator.side_effect = locator_for
    page._confirm = confirm_btn
    return page


async def _withdraw(page, present):
    session = SimpleNamespace(page=page, delay=AsyncMock())
    actions = ExtActions(session, MagicMock())
    with patch.object(actions, "_slug_present", new_callable=AsyncMock, return_value=present) as sp:
        out = await actions.withdraw("Anna", "anna")
    return out, sp


async def test_withdraw_dialog_without_confirm_button_is_not_counted():
    page = _withdraw_page(dialog=True, confirm=False)
    out, sp = await _withdraw(page, True)
    assert out["status"] == "not_confirmed"
    page.keyboard.press.assert_awaited_with("Escape")
    sp.assert_not_awaited()
    from linkedin_mcp_server import ext_outreach

    assert "not_confirmed" not in ext_outreach._COUNTED


async def test_withdraw_confirmed_click_is_verified():
    page = _withdraw_page(dialog=True, confirm=True)
    out, _ = await _withdraw(page, False)
    assert out["status"] == "withdrawn"
    page._confirm.click.assert_awaited_once()
