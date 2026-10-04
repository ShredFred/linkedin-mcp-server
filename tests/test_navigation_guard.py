"""Navigation destination guard (#786) and rate-limit coupling (#957)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from linkedin_mcp_server import ext_outreach as outreach
from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    OffSiteNavigationError,
    RateLimitError,
)
from linkedin_mcp_server.linkedin import navigation as nav
from linkedin_mcp_server.linkedin.navigation import (
    PageNavigator,
    assert_linkedin_destination,
    is_linkedin_host,
    raise_if_rate_limited_response,
    set_rate_limit_hooks,
)

REQ = "https://www.linkedin.com/in/example-person/"


@pytest.mark.parametrize(
    "host",
    [
        "linkedin.com",
        "www.linkedin.com",
        "de.linkedin.com",
        "WWW.LinkedIn.COM",
        "linkedin.com.",
    ],
)
def test_linkedin_hosts_accepted(host):
    assert is_linkedin_host(host)


@pytest.mark.parametrize(
    "host",
    [
        None,
        "",
        "linkedin.com.evil.example",
        "evil-linkedin.com",
        "evillinkedin.com",
        "linkedin.co",
        "example.com",
        "linkedin.com-evil.example",
    ],
)
def test_lookalike_hosts_rejected(host):
    assert not is_linkedin_host(host)


@pytest.mark.parametrize(
    "url",
    [
        "https://linkedin.com.evil.example/in/x",
        "https://evil-linkedin.com/feed/",
        "https://www.linkedin.com@evil.example/feed/",
        "https://user:pw@www.linkedin.com/feed/",
        "https://example.com/?next=https://www.linkedin.com/feed/",
        "http://example.com/linkedin.com/feed",
        "ftp://www.linkedin.com/feed/",
    ],
)
def test_offsite_final_url_raises(url):
    with pytest.raises(OffSiteNavigationError):
        assert_linkedin_destination(REQ, url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/checkpoint/challenge/abc",
        "https://www.linkedin.com/authwall?trk=x",
        "https://www.linkedin.com/login",
        "https://www.linkedin.com/login/de",
        "https://www.linkedin.com/uas/login?session_redirect=x",
    ],
)
def test_auth_paths_are_flagged_not_raised_by_host_check(url):
    # The host check leaves auth paths to the remember-me attempt (#786).
    assert_linkedin_destination(REQ, url)
    assert nav.is_linkedin_auth_path(url)


@pytest.mark.parametrize(
    "url",
    [REQ, "https://www.linkedin.com/login-help-page/", None, "", "about:blank",
     "https://evil.example/login"],
)
def test_non_auth_urls_are_not_flagged(url):
    assert not nav.is_linkedin_auth_path(url)


@pytest.mark.parametrize(
    "url",
    [
        REQ,
        "https://www.linkedin.com/feed/",
        "https://www.linkedin.com/login-help-page/",
        None,
        "",
        "about:blank",
        MagicMock(),
    ],
)
def test_healthy_or_unjudgeable_url_passes(url):
    assert_linkedin_destination(REQ, url)


def test_429_raises_with_retry_after_clamped():
    resp = SimpleNamespace(status=429, headers={"retry-after": "7200"})
    with pytest.raises(RateLimitError) as e:
        raise_if_rate_limited_response(REQ, resp)
    assert e.value.suggested_wait_time == nav.RATE_LIMIT_MAX_WAIT
    resp = SimpleNamespace(status=429, headers={"retry-after": "garbage"})
    with pytest.raises(RateLimitError) as e:
        raise_if_rate_limited_response(REQ, resp)
    assert e.value.suggested_wait_time == nav.RATE_LIMIT_DEFAULT_WAIT


@pytest.mark.parametrize("resp", [None, SimpleNamespace(status=200), MagicMock()])
def test_non_429_passes(resp):
    raise_if_rate_limited_response(REQ, resp)


def _navigator(final_url: str, status: int = 200):
    page = MagicMock()
    page.url = final_url
    page.goto = AsyncMock(return_value=SimpleNamespace(status=status, headers={}))
    session = SimpleNamespace(page=page)
    return PageNavigator(session), page


async def _run(navigator, url=REQ):
    with (
        patch.object(nav, "record_page_trace", AsyncMock()),
        patch.object(nav, "stabilize_navigation", AsyncMock()),
        patch.object(nav, "detect_auth_barrier_quick", AsyncMock(return_value=None)),
    ):
        await navigator._navigate_to_page(url)


async def test_navigation_redirected_offsite_fails():
    navigator, _ = _navigator("https://linkedin.com.evil.example/in/x")
    with pytest.raises(OffSiteNavigationError):
        await _run(navigator)


async def test_navigation_redirected_to_checkpoint_fails():
    navigator, _ = _navigator("https://www.linkedin.com/checkpoint/lg/login")
    with pytest.raises(AuthenticationError):
        await _run(navigator)


@pytest.mark.parametrize(
    "final",
    [
        "https://www.linkedin.com/login",
        "https://www.linkedin.com/uas/login?session_redirect=x",
        "https://www.linkedin.com/checkpoint/lg/login",
        "https://www.linkedin.com/authwall?trk=x",
    ],
)
async def test_auth_redirect_without_prompt_raises(final):
    navigator, page = _navigator(final)
    with (
        patch.object(nav, "resolve_remember_me_prompt", AsyncMock(return_value=False)),
        pytest.raises(AuthenticationError),
    ):
        await _run(navigator)
    assert page.goto.await_count == 1


@pytest.mark.parametrize(
    "final", ["https://www.linkedin.com/login", "https://www.linkedin.com/checkpoint/x"]
)
async def test_auth_redirect_with_remember_me_recovers_and_retries(final):
    navigator, page = _navigator(final)
    answers = iter([final, REQ])

    async def goto(url, **_):
        page.url = next(answers)
        return SimpleNamespace(status=200, headers={})

    page.goto = AsyncMock(side_effect=goto)
    resolve = AsyncMock(return_value=True)
    with patch.object(nav, "resolve_remember_me_prompt", resolve):
        await _run(navigator)
    resolve.assert_awaited_once()
    assert page.goto.await_count == 2


async def test_auth_redirect_retry_still_on_login_raises():
    navigator, page = _navigator("https://www.linkedin.com/login")
    with (
        patch.object(nav, "resolve_remember_me_prompt", AsyncMock(return_value=True)),
        pytest.raises(AuthenticationError),
    ):
        await _run(navigator)
    assert page.goto.await_count == 2


async def test_navigation_on_linkedin_passes():
    navigator, _ = _navigator(REQ)
    await _run(navigator)


# --- pacer coupling --------------------------------------------------------


@pytest.fixture
def ledger(tmp_path):
    return outreach.Ledger(tmp_path / "ledger.jsonl")


@pytest.fixture
def coupled(ledger):
    set_rate_limit_hooks(
        lambda: outreach.rate_limit_gate(ledger),
        lambda exc: outreach.rate_limit_recorder(exc, ledger),
    )
    yield ledger
    set_rate_limit_hooks(None, None)


async def test_429_on_first_page_records_cooldown_and_blocks_next(coupled):
    navigator, page = _navigator(REQ, status=429)
    with pytest.raises(RateLimitError):
        await _run(navigator)
    assert page.goto.await_count == 1  # no retry storm
    rows = coupled.rows()
    assert [r["kind"] for r in rows] == ["rate_limited"]
    assert rows[0]["cooldown_seconds"] == nav.RATE_LIMIT_DEFAULT_WAIT
    state = outreach.Pacer(coupled).state("page_read")
    assert state["left"] == 0 and "rate_limited_until" in state
    navigator2, page2 = _navigator(REQ)
    with pytest.raises(RateLimitError):
        await _run(navigator2)
    page2.goto.assert_not_awaited()
    assert len(coupled.rows()) == 1  # the gate refusal is not booked again


async def test_429_after_redirect_is_rate_limit_not_offsite(coupled):
    navigator, _ = _navigator("https://www.linkedin.com/feed/", status=429)
    with pytest.raises(RateLimitError):
        await _run(navigator, "https://www.linkedin.com/in/example-person")
    assert coupled.rows()[0]["kind"] == "rate_limited"


async def test_429_offsite_redirect_still_rate_limit(coupled):
    # The status decides first: a 429 is a stop signal wherever it landed.
    navigator, _ = _navigator("https://example.com/", status=429)
    with pytest.raises(RateLimitError):
        await _run(navigator)


def test_cooldown_expires(coupled):
    past = (datetime.now().astimezone() - timedelta(seconds=400)).isoformat(
        timespec="seconds"
    )
    coupled.path.write_text(
        json.dumps({"at": past, "kind": "rate_limited", "cooldown_seconds": 300})
        + "\n",
        encoding="utf-8",
    )
    outreach.rate_limit_gate(coupled)
    assert outreach.Pacer(coupled).rate_limited_until() is None
    assert "rate_limited_until" not in outreach.Pacer(coupled).state("page_read")


def test_cooldown_active_reports_remaining(coupled):
    coupled.append({"kind": "rate_limited", "cooldown_seconds": 600})
    with pytest.raises(RateLimitError) as e:
        outreach.rate_limit_gate(coupled)
    assert 500 < e.value.suggested_wait_time <= 601


def test_unreadable_cooldown_value_blocks_with_default(coupled):
    coupled.append({"kind": "rate_limited", "cooldown_seconds": "x"})
    with pytest.raises(RateLimitError):
        outreach.rate_limit_gate(coupled)


def test_ledger_unavailable_gate_fails_closed(tmp_path):
    broken = outreach.Ledger(tmp_path / "ledger.jsonl")
    with patch.object(outreach.Ledger, "rows", side_effect=OSError("locked")):
        with pytest.raises(RateLimitError, match="ledger unavailable"):
            outreach.rate_limit_gate(broken)


async def test_ledger_unavailable_recorder_still_raises():
    def boom(exc):
        raise OSError("disk gone")

    set_rate_limit_hooks(None, boom)
    try:
        navigator, page = _navigator(REQ, status=429)
        with pytest.raises(RateLimitError):
            await _run(navigator)
        assert page.goto.await_count == 1
    finally:
        set_rate_limit_hooks(None, None)


async def test_body_rate_limit_via_session_check_is_recorded(coupled):
    from linkedin_mcp_server.linkedin import session as session_mod

    fake = SimpleNamespace(page=MagicMock())
    with patch.object(
        session_mod,
        "detect_rate_limit",
        AsyncMock(side_effect=RateLimitError("Rate limit message detected.", 30)),
    ):
        with pytest.raises(RateLimitError):
            await session_mod.PageSession.check_rate_limit(fake)
    assert coupled.rows()[0]["cooldown_seconds"] == outreach.RATE_LIMIT_MIN_COOLDOWN


def test_same_error_recorded_once(coupled):
    exc = RateLimitError("x", 120)
    nav.report_rate_limit(exc)
    nav.report_rate_limit(exc)
    assert len(coupled.rows()) == 1


def test_no_hooks_is_a_no_op():
    set_rate_limit_hooks(None, None)
    nav.check_rate_limit_gate()
    nav.report_rate_limit(RateLimitError("x", 30))
