"""Regressions found in bughunt round 2 of the fork hardening."""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from linkedin_mcp_server import ext_outreach as outreach
from linkedin_mcp_server.core.exceptions import (
    OffLinkedInLandingError,
    OffSiteNavigationError,
)
from linkedin_mcp_server.drivers.browser import _feed_auth_succeeds
from linkedin_mcp_server.linkedin.capture import (
    COMPANY_PEOPLE_MAX_ROUNDS,
    SectionCapture,
)
from linkedin_mcp_server.linkedin.link_metadata import classify_link, normalize_url
from linkedin_mcp_server.linkedin.search_urls import resolve_people_geo_ids


# -- feed auth check must not call an off-site landing "authenticated" --------


async def test_feed_auth_off_site_landing_is_not_success():
    browser = MagicMock()
    browser.page = MagicMock()
    browser.page.goto = AsyncMock()
    browser.page.url = "https://portal.example/login"
    with (
        patch(
            "linkedin_mcp_server.drivers.browser.resolve_remember_me_prompt",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.detect_auth_barrier_quick",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.stabilize_navigation",
            new_callable=AsyncMock,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.record_page_trace",
            new_callable=AsyncMock,
        ),
    ):
        # Upstream #1235 refuses the landing first; the fork check stays behind it.
        with pytest.raises((OffSiteNavigationError, OffLinkedInLandingError)):
            await _feed_auth_succeeds(browser)


# -- link classification: port and trailing dot are still LinkedIn -----------


@pytest.mark.parametrize(
    "href",
    [
        "https://www.linkedin.com:443/in/ext-person/",
        "https://www.linkedin.com./in/ext-person/",
        "https://WWW.LINKEDIN.COM/in/ext-person/",
    ],
)
def test_classify_link_host_variants(href):
    assert classify_link(href) == ("person", "/in/ext-person/")


def test_redirect_unwrap_with_port():
    href = (
        "https://www.linkedin.com:443/redir/redirect/"
        "?url=https%3A%2F%2Fext.example%2Fpage"
    )
    assert normalize_url(href) == "https://ext.example/page"


def test_userinfo_host_is_external():
    kind, _ = classify_link("https://www.linkedin.com@ext.example/in/ext-person/")
    assert kind == "external"


# -- pacer: a pathological cooldown must not crash every pacer read ----------


@pytest.mark.parametrize("raw", ["Infinity", "1e300", str(10**15)])
def test_rate_limited_row_with_huge_cooldown(tmp_path, raw):
    path = tmp_path / "ledger.jsonl"
    at = datetime.now().astimezone().isoformat()
    path.write_text(
        '{"kind": "rate_limited", "at": "%s", "cooldown_seconds": %s}\n' % (at, raw),
        encoding="utf-8",
    )
    pacer = outreach.Pacer(outreach.Ledger(path))
    # Still blocked, neither crashed nor open.
    assert pacer.rate_limited_until() is not None


def test_record_rate_limit_clamps(tmp_path):
    ledger = outreach.Ledger(tmp_path / "ledger.jsonl")
    outreach.Pacer(ledger).record_rate_limit(10**15)
    assert ledger.rows()[-1]["cooldown_seconds"] == outreach.RATE_LIMIT_MAX_COOLDOWN


def test_json_infinity_parses():
    # Premise of the ledger test above: json reads Infinity as a float.
    assert json.loads('{"x": Infinity}')["x"] == float("inf")


# -- people geo: decomposed umlaut resolves like the composed one ------------


def test_geo_name_nfd_resolves():
    assert resolve_people_geo_ids("Österreich") == resolve_people_geo_ids(
        "Österreich"
    )


# -- company people: reaching the target on the last round is "limit" -------


async def test_load_more_round_cap_reports_limit_when_target_reached():
    capture = SectionCapture.__new__(SectionCapture)
    session = MagicMock()
    session.delay = AsyncMock()
    states = iter(
        [{"rows": n * 10, "more": True} for n in range(COMPANY_PEOPLE_MAX_ROUNDS + 1)]
    )

    async def evaluate(js, *args):
        if "companyPeopleClickMore" in js:
            return True
        return next(states)

    session.page.evaluate = evaluate
    capture._session = session
    target = COMPANY_PEOPLE_MAX_ROUNDS * 10
    outcome = await capture._load_more_company_people(target)
    assert outcome["rows"] == target
    assert outcome["stop"] == "limit"
