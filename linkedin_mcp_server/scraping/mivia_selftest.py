"""MiViA fork: read-only outreach self-check against the canary profile.

On 2026-09-30 a LinkedIn URL change made every send fail, and it was found
only after a batch had started. ``outreach_selftest`` loads the canary (a
known 1st-degree profile) and answers, without clicking any action and
without writing anything, whether the two things every outreach run needs
still resolve:

* the Message action: one recipient-specific compose action, with a profile
  URN, on a page URL the normaliser accepts;
* the connection state: the canary must classify as ``already_connected``.

Optionally a second, not-connected profile is probed for the Connect side.
The More menu may be opened for that (opening a menu writes nothing), and it
is closed again; no menu item is ever clicked.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any
from urllib.parse import urlparse

from patchright.async_api import TimeoutError as PlaywrightTimeoutError

import linkedin_mcp_server.scraping.connection as connection
import linkedin_mcp_server.scraping.mivia_urls as mivia_urls
from linkedin_mcp_server.scraping.connection_actions import ConnectionActions
from linkedin_mcp_server.scraping.identifiers import (
    normalize_person_identifier,
    person_profile_url,
)
from linkedin_mcp_server.scraping.message_sender import (
    MessageSender,
    _profile_path_from_url,
)
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

logger = logging.getLogger(__name__)


async def _unread(_username: str) -> dict[str, Any]:
    raise AssertionError("the self-check never reads profile text")


def _query_keys(url: str) -> list[str]:
    return sorted(
        {pair.split("=", 1)[0] for pair in urlparse(url).query.split("&") if pair}
    )


async def _load(
    session: ScrapingSession, navigator: PageNavigator, username: str
) -> str:
    await navigator._navigate_to_page(person_profile_url(username, "/"))
    try:
        await session.page.wait_for_selector("main", timeout=15_000)
    except PlaywrightTimeoutError:
        logger.debug("Self-check profile did not render main for %s", username)
    return session.page.url


async def _connect_state(
    actions: ConnectionActions, session: ScrapingSession, username: str
) -> dict[str, Any]:
    signals = await actions._read_action_signals(username)
    state = connection.detect_connection_state(signals)
    out: dict[str, Any] = {"state": state, "more_menu_opened": False}
    if state in ("follow_only", "unavailable"):
        opened = await actions._open_more_menu()
        out["more_menu_opened"] = opened
        if opened:
            signals = await actions._read_action_signals(username)
            try:
                await session.page.keyboard.press("Escape")
            except Exception:
                logger.debug("Escape after self-check menu read failed", exc_info=True)
            menu_state = connection.detect_connection_state(signals)
            out["state_after_more_menu"] = menu_state
    out["signals"] = asdict(signals)
    return out


async def outreach_selftest(
    session: ScrapingSession,
    navigator: PageNavigator,
    *,
    canary: str,
    connect_probe: str | None = None,
) -> dict[str, Any]:
    canary = normalize_person_identifier(canary)
    problems: list[str] = []

    page_url = await _load(session, navigator, canary)
    sender = MessageSender(session, navigator)
    resolution = await sender._read_profile_message_target()
    page_path = _profile_path_from_url(page_url)
    message = {
        "status": resolution.status,
        "recipient_urn_resolved": bool(
            resolution.target and resolution.target.profile_urn
        ),
        "page_url_accepted": page_path is not None,
        "page_url_query_keys": _query_keys(page_url),
        "unknown_query_keys": [
            k for k in _query_keys(page_url) if k not in mivia_urls.BENIGN_PROFILE_QUERY
        ],
    }
    if page_path is None:
        problems.append(
            "canary page URL is not accepted by the profile URL normaliser "
            "(new query key or path shape?)"
        )
    elif mivia_urls.profile_key(page_path[len("/in/") : -1]) != mivia_urls.profile_key(
        canary
    ):
        problems.append("canary page URL names a different profile (redirect?)")
    if resolution.status != "resolved" or not message["recipient_urn_resolved"]:
        problems.append(
            f"Message action on the canary did not resolve (status={resolution.status})"
        )

    actions = ConnectionActions(session, navigator, _unread)
    canary_connect = await _connect_state(actions, session, canary)
    canary_state = canary_connect.get("state_after_more_menu", canary_connect["state"])
    if canary_state != "already_connected":
        problems.append(
            f"canary classified as {canary_state}, expected already_connected"
        )

    result: dict[str, Any] = {
        "canary": canary,
        "message": message,
        "connection": canary_connect,
    }
    if connect_probe:
        probe = normalize_person_identifier(connect_probe)
        probe_url = await _load(session, navigator, probe)
        probe_connect = await _connect_state(actions, session, probe)
        probe_connect["page_url_accepted"] = (
            _profile_path_from_url(probe_url) is not None
        )
        final = probe_connect.get("state_after_more_menu", probe_connect["state"])
        probe_connect["resolvable"] = final in (
            "connectable",
            "pending",
            "already_connected",
            "follow_only",
        )
        if not probe_connect["resolvable"]:
            problems.append(f"connect probe {probe} classified as {final}")
        result["connect_probe"] = {"username": probe, **probe_connect}

    result["ok"] = not problems
    result["problems"] = problems
    return result
