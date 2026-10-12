"""A valid shared session must survive contention and unfinished logins.

Incident 2026-10-12: a short-lived stdio client hit a 30 s feed timeout while
other clients held the profile, read it as a dead session, retired the whole
profile and exited before any login could finish. Every client was logged out.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from linkedin_mcp_server import bootstrap
from linkedin_mcp_server.bootstrap import (
    _restore_retired_before_login,
    get_bootstrap_state,
    initialize_bootstrap,
    invalidate_auth_and_trigger_relogin,
)
from linkedin_mcp_server.drivers.browser import _feed_auth_succeeds
from linkedin_mcp_server.exceptions import (
    AuthenticationStartedError,
    BrowserBusyError,
)
from linkedin_mcp_server.session_state import (
    portable_cookie_path,
    source_state_path,
)


def _make_auth_ready(profile_dir, generation="gen-1"):
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "Default").mkdir(parents=True, exist_ok=True)
    (profile_dir / "Default" / "Cookies").write_text("placeholder")
    cookie_path = portable_cookie_path(profile_dir)
    cookie_path.parent.mkdir(parents=True, exist_ok=True)
    cookie_path.write_text(json.dumps([{"name": "li_at", "domain": ".linkedin.com"}]))
    source_state_path(profile_dir).write_text(
        json.dumps(
            {
                "version": 1,
                "source_runtime_id": "windows-amd64-host",
                "login_generation": generation,
                "created_at": "2026-10-12T18:00:00Z",
                "profile_path": str(profile_dir),
                "cookies_path": str(cookie_path),
            }
        )
    )


def _session_is_in_place(profile_dir) -> bool:
    return (
        (profile_dir / "Default" / "Cookies").exists()
        and portable_cookie_path(profile_dir).exists()
        and source_state_path(profile_dir).exists()
    )


def _no_login_window(monkeypatch, result):
    """Stub the headed login so nothing launches; *result* decides its outcome."""
    monkeypatch.setattr(bootstrap, "_uses_custom_chrome", lambda: True)

    async def fake_login(*_args, **_kwargs):
        if isinstance(result, BaseException):
            raise result
        if callable(result):
            return result()
        return result

    monkeypatch.setattr(bootstrap, "interactive_login", fake_login)


class TestHeldProfileIsNotMoved:
    async def test_profile_held_by_another_process_stays_in_place(
        self, isolate_profile_dir, monkeypatch
    ):
        _make_auth_ready(isolate_profile_dir)
        # Another server process holds the cross-process lease.
        monkeypatch.setattr(
            "linkedin_mcp_server.session_state._take_within", lambda *_a: False
        )

        async def fake_login_flow():
            return None

        monkeypatch.setattr(bootstrap, "_run_login_flow", fake_login_flow)
        initialize_bootstrap("managed")

        with pytest.raises(AuthenticationStartedError):
            await invalidate_auth_and_trigger_relogin(stale_generation="gen-1")

        assert _session_is_in_place(isolate_profile_dir)
        assert get_bootstrap_state().retired_before_login is None
        assert not list(isolate_profile_dir.parent.glob("invalid-state-*"))


class TestAuthCheckUnderContention:
    async def test_feed_timeout_on_the_signed_in_feed_is_busy_not_stale(self):
        """The 2026-10-12 trace: goto timed out, title 'Feed | LinkedIn'."""
        browser = MagicMock()
        browser.page.url = "https://www.linkedin.com/feed/"
        browser.page.title = AsyncMock(return_value="Feed | LinkedIn")
        browser.page.evaluate = AsyncMock(return_value="Start Netzwerk Jobs")
        locator = MagicMock()
        locator.count = AsyncMock(return_value=0)
        browser.page.locator = MagicMock(return_value=locator)
        browser.page.goto = AsyncMock(
            side_effect=TimeoutError("Page.goto: Timeout 30000ms exceeded.")
        )

        with (
            patch(
                "linkedin_mcp_server.drivers.browser.resolve_remember_me_prompt",
                new_callable=AsyncMock,
                return_value=False,
            ),
            pytest.raises(BrowserBusyError),
        ):
            await _feed_auth_succeeds(browser)

    async def test_a_timeout_redirected_to_login_is_still_stale(self):
        browser = MagicMock()
        browser.page.url = "https://www.linkedin.com/login"
        browser.page.title = AsyncMock(return_value="LinkedIn Login")
        browser.page.evaluate = AsyncMock(return_value="")
        locator = MagicMock()
        locator.count = AsyncMock(return_value=0)
        browser.page.locator = MagicMock(return_value=locator)
        browser.page.goto = AsyncMock(
            side_effect=TimeoutError("Page.goto: Timeout 30000ms exceeded.")
        )

        with patch(
            "linkedin_mcp_server.drivers.browser.resolve_remember_me_prompt",
            new_callable=AsyncMock,
            return_value=False,
        ):
            assert await _feed_auth_succeeds(browser) is False


class TestUnfinishedLoginRestoresTheSession:
    async def test_failed_login_restores_the_retired_session(
        self, isolate_profile_dir, monkeypatch
    ):
        _make_auth_ready(isolate_profile_dir)
        _no_login_window(monkeypatch, False)
        initialize_bootstrap("managed")

        with pytest.raises(AuthenticationStartedError):
            await invalidate_auth_and_trigger_relogin(stale_generation="gen-1")
        task = get_bootstrap_state().login_task
        await asyncio.gather(task, return_exceptions=True)

        assert _session_is_in_place(isolate_profile_dir)
        assert get_bootstrap_state().retired_before_login is None

    async def test_cancelled_login_restores_the_retired_session(
        self, isolate_profile_dir, monkeypatch
    ):
        _make_auth_ready(isolate_profile_dir)
        _no_login_window(monkeypatch, asyncio.CancelledError())
        initialize_bootstrap("managed")

        with pytest.raises(AuthenticationStartedError):
            await invalidate_auth_and_trigger_relogin(stale_generation="gen-1")
        task = get_bootstrap_state().login_task
        await asyncio.gather(task, return_exceptions=True)

        assert _session_is_in_place(isolate_profile_dir)

    async def test_process_exit_before_the_login_restores_the_session(
        self, isolate_profile_dir, monkeypatch
    ):
        """The stdio client exits before its login task ever runs."""
        _make_auth_ready(isolate_profile_dir)

        async def never_runs():
            await asyncio.Event().wait()

        monkeypatch.setattr(bootstrap, "_run_login_flow", never_runs)
        initialize_bootstrap("managed")

        with pytest.raises(AuthenticationStartedError):
            await invalidate_auth_and_trigger_relogin(stale_generation="gen-1")
        assert not _session_is_in_place(isolate_profile_dir)

        # What atexit runs at interpreter shutdown.
        _restore_retired_before_login()

        assert _session_is_in_place(isolate_profile_dir)
        assert not list(isolate_profile_dir.parent.glob("invalid-state-*"))
        get_bootstrap_state().login_task.cancel()


class TestGenuinelyStaleSessionStillRotates:
    async def test_completed_login_keeps_the_old_session_retired(
        self, isolate_profile_dir, monkeypatch
    ):
        _make_auth_ready(isolate_profile_dir)

        def sign_in():
            _make_auth_ready(isolate_profile_dir, generation="gen-2")
            return True

        _no_login_window(monkeypatch, sign_in)
        initialize_bootstrap("managed")

        with pytest.raises(AuthenticationStartedError):
            await invalidate_auth_and_trigger_relogin(stale_generation="gen-1")
        await asyncio.gather(get_bootstrap_state().login_task, return_exceptions=True)

        state = json.loads(source_state_path(isolate_profile_dir).read_text())
        assert state["login_generation"] == "gen-2"
        retired = list(isolate_profile_dir.parent.glob("invalid-state-*"))
        assert len(retired) == 1
        assert get_bootstrap_state().retired_before_login is None


# -- Windows: a live Chrome holds ``lockfile`` without sharing ---------------

_WIN = __import__("os").name == "nt"


@__import__("pytest").mark.skipif(not _WIN, reason="Windows lock semantics")
def test_windows_lockfile_held_without_sharing_is_in_use(tmp_path):
    import ctypes
    from ctypes import wintypes

    from linkedin_mcp_server.session_state import profile_in_use_by

    lock = tmp_path / "lockfile"
    lock.write_bytes(b"")
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    handle = k32.CreateFileW(str(lock), 0xC0000000, 0, None, 3, 0x80, None)
    assert handle not in (None, wintypes.HANDLE(-1).value)
    try:
        assert profile_in_use_by(tmp_path) == lock
    finally:
        k32.CloseHandle(handle)
    # Released (or left behind by a crash): free again.
    assert profile_in_use_by(tmp_path) is None


@__import__("pytest").mark.skipif(not _WIN, reason="Windows lock semantics")
def test_windows_profile_without_lockfile_is_free(tmp_path):
    from linkedin_mcp_server.session_state import profile_in_use_by

    assert profile_in_use_by(tmp_path) is None
