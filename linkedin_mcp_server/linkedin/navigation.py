"""Authentication-aware page navigation lifecycle."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

import logging
import re
from urllib.parse import urlsplit

from linkedin_mcp_server.core.auth import (
    detect_auth_barrier,
    detect_auth_barrier_quick,
    resolve_remember_me_prompt,
)
from linkedin_mcp_server.core.destination import (
    is_linkedin_landing,
    raise_if_off_linkedin,
)
from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    OffSiteNavigationError,
    RateLimitError,
)
from linkedin_mcp_server.core.rate_limit_hooks import (
    check_rate_limit_gate,
    report_rate_limit,
    set_rate_limit_hooks,
)
from linkedin_mcp_server.core.proxy_errors import (
    NAVIGATION_BUDGET_MS,
    goto_reporting_proxy_errors,
    raise_if_proxy_error,
    redact_proxy_credentials,
    redacted_copy,
)
from linkedin_mcp_server.debug_trace import record_page_trace
from linkedin_mcp_server.debug_utils import stabilize_navigation
from linkedin_mcp_server.linkedin.session import PageSession

logger = logging.getLogger(__name__)

WaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]

_LINKEDIN_DOMAIN = "linkedin.com"
# Paths on linkedin.com that mean "not signed in / challenged", not content.
_AUTH_PATH_PREFIXES = ("/checkpoint", "/authwall", "/login", "/uas/login")
# A 429 without Retry-After: wait this long. Retry-After is clamped to the band.
RATE_LIMIT_DEFAULT_WAIT = 300
RATE_LIMIT_MIN_WAIT = 60
RATE_LIMIT_MAX_WAIT = 3600


def is_linkedin_host(host: str | None) -> bool:
    """Exact domain or subdomain of linkedin.com; no substring matching."""
    if not host:
        return False
    host = host.lower().rstrip(".")
    return host == _LINKEDIN_DOMAIN or host.endswith("." + _LINKEDIN_DOMAIN)


def assert_linkedin_destination(requested_url: str, final_url: Any) -> None:
    """Fail when a navigation ended off LinkedIn (#786).

    Only the host is judged here. An auth path (/login, /checkpoint, ...) on
    linkedin.com is a barrier the caller may still clear through the
    remember-me prompt, so it is reported by :func:`is_linkedin_auth_path` and
    raised only after that attempt failed. Non-string or host-less URLs (mocks,
    ``about:blank`` before commit) are not judged here.
    """
    if not isinstance(final_url, str) or not final_url:
        return
    parts = urlsplit(final_url)
    if parts.scheme not in ("http", "https"):
        if parts.netloc:
            raise OffSiteNavigationError(requested_url, final_url)
        return
    # urlsplit().hostname drops userinfo, so "https://linkedin.com@evil.example/"
    # is judged by evil.example, which is what the browser actually loaded.
    if parts.username is not None or parts.password is not None:
        raise OffSiteNavigationError(requested_url, final_url)
    if not is_linkedin_host(parts.hostname):
        raise OffSiteNavigationError(requested_url, final_url)


def is_linkedin_auth_path(final_url: Any) -> bool:
    """True when ``final_url`` is a LinkedIn sign-in/challenge page."""
    if not isinstance(final_url, str) or not final_url:
        return False
    parts = urlsplit(final_url)
    if parts.scheme not in ("http", "https") or not is_linkedin_host(parts.hostname):
        return False
    path = parts.path.lower()
    return any(
        path == prefix or path.startswith(prefix + "/") for prefix in _AUTH_PATH_PREFIXES
    )


def _retry_after_seconds(response: Any) -> int:
    try:
        headers = response.headers
        value = headers.get("retry-after") if isinstance(headers, dict) else None
        wait = int(str(value).strip()) if value is not None else RATE_LIMIT_DEFAULT_WAIT
    except (TypeError, ValueError, AttributeError):
        wait = RATE_LIMIT_DEFAULT_WAIT
    return max(RATE_LIMIT_MIN_WAIT, min(RATE_LIMIT_MAX_WAIT, wait))


def raise_if_rate_limited_response(url: str, response: Any) -> None:
    """HTTP 429 on the main document is a rate limit, not a page (#957)."""
    status = getattr(response, "status", None)
    if isinstance(status, int) and status == 429:
        raise RateLimitError(
            f"LinkedIn answered HTTP 429 (too many requests) for {url}.",
            suggested_wait_time=_retry_after_seconds(response),
        )




class PageNavigator:
    """Own the complete lifecycle around navigation on one browser page."""

    # How long to let `page.url` catch up with a navigation the sidebar scroll
    # suppressed. The lag itself measured 6ms across ten runs, min and max alike;
    # the rest of the budget is for a redirect chain that is still hopping. Only a
    # page that already looks wrong pays it, so the ceiling costs a healthy
    # ten-page search nothing and a broken one 25s of its 180s tool timeout.
    _URL_SETTLE_TIMEOUT = 2.5
    _URL_SETTLE_POLL = 0.01
    # How long the route has to hold still before it counts as the destination. A
    # redirect chain hops through intermediate documents, and judging one of those
    # calls a checkpoint healthy, or a healthy page a checkpoint.
    _URL_SETTLE_QUIET = 0.5
    # How long a navigation has to announce itself before the page counts as
    # going nowhere. Measured across 300 evaluations destroyed by a navigation:
    # 0.37ms at worst idle, 0.81ms at the 99th percentile with twenty-four
    # workers saturating the machine. Paid in full only by a failure with no
    # navigation behind it, and by a page that only rewrote its own address.
    #
    # It is also the whole window: a redirect committing later than this after
    # the scroll returns is not seen here, and falls to the route comparison at
    # the end, which a reload leaves nothing for. Three hundred times the
    # measured announcement is the trade, the other side of it being the wait
    # every ordinary DOM failure pays before it can report itself.
    _URL_SETTLE_LAG = 0.3
    # How long the replacement document gets to reach `domcontentloaded`. It
    # renders after it commits, and an account picker was measured 200ms behind
    # its own navigation, so a page judged on arrival is judged empty.
    _DOCUMENT_READY_TIMEOUT = 5.0

    def __init__(self, session: PageSession):
        self._session = session

    @staticmethod
    def _normalize_body_marker(value: Any) -> str:
        """Compress body text into a short, single-line diagnostic marker."""
        if not isinstance(value, str):
            return ""
        return re.sub(r"\s+", " ", value).strip()[:200]

    async def _log_navigation_failure(
        self,
        target_url: str,
        wait_until: str,
        navigation_error: Exception,
        hops: list[str],
    ) -> None:
        """Emit structured diagnostics for a failed target navigation."""
        page = self._session.page
        try:
            title = await page.title()
        except Exception:
            title = ""

        try:
            auth_barrier = await detect_auth_barrier(page)
        except Exception:
            auth_barrier = None

        try:
            remember_me_visible = (await page.locator("#rememberme-div").count()) > 0
        except Exception:
            remember_me_visible = False

        try:
            body_marker = self._normalize_body_marker(
                await page.evaluate("() => document.body?.innerText || ''")
            )
        except Exception:
            body_marker = ""

        logger.warning(
            "Navigation to %s failed (wait_until=%s, error=%s). "
            "current_url=%s title=%r auth_barrier=%s remember_me=%s hops=%s body_marker=%r",
            target_url,
            wait_until,
            # Redacted like the traces above: a driver error can quote the
            # proxy URL, and this log is what users paste into issue reports.
            redact_proxy_credentials(
                f"{type(navigation_error).__name__}: {navigation_error}"
            ),
            page.url,
            title,
            auth_barrier,
            remember_me_visible,
            hops,
            body_marker,
        )

    async def _raise_if_auth_barrier(
        self,
        url: str,
        *,
        navigation_error: Exception | None = None,
    ) -> None:
        """Raise an auth error when LinkedIn shows login/account-picker UI."""
        barrier = await detect_auth_barrier(self._session.page)
        if not barrier:
            return

        logger.warning("Authentication barrier detected on %s: %s", url, barrier)
        message = (
            "LinkedIn requires interactive re-authentication. "
            "Run with --login and complete the account selection/sign-in flow."
        )
        if navigation_error is not None:
            raise AuthenticationError(message) from navigation_error
        raise AuthenticationError(message)

    async def _goto_with_auth_checks(
        self,
        url: str,
        *,
        wait_until: WaitUntil = "domcontentloaded",
        allow_remember_me: bool = True,
    ) -> None:
        """Navigate to a LinkedIn page and fail fast on auth barriers."""
        page = self._session.page
        hops: list[str] = []
        listener_registered = False

        def record_navigation(frame: Any) -> None:
            if frame != page.main_frame:
                return
            frame_url = getattr(frame, "url", "")
            if frame_url and (not hops or hops[-1] != frame_url):
                hops.append(frame_url)

        def unregister_navigation_listener() -> None:
            nonlocal listener_registered
            if not listener_registered:
                return
            page.remove_listener("framenavigated", record_navigation)
            listener_registered = False

        page.on("framenavigated", record_navigation)
        listener_registered = True
        try:
            await record_page_trace(
                page,
                "extractor-before-goto",
                extra={"target_url": url, "wait_until": wait_until},
            )
            try:
                # A scripted page records the timeout it was given and does not
                # enforce it. Only Patchright's clock starts at the call, which
                # is the clock that expires before a cold browser sends.
                if type(page).__module__.startswith("patchright."):
                    response = await goto_reporting_proxy_errors(
                        page,
                        url,
                        wait_until=wait_until,
                        timeout=NAVIGATION_BUDGET_MS,
                    )
                else:
                    response = await page.goto(
                        url,
                        wait_until=wait_until,
                        timeout=NAVIGATION_BUDGET_MS,
                    )
                raise_if_rate_limited_response(url, response)
                await stabilize_navigation(f"goto {url}", logger)
                await record_page_trace(
                    page,
                    "extractor-after-goto",
                    extra={"target_url": url, "wait_until": wait_until},
                )
            except RateLimitError:
                raise
            except Exception as exc:
                # Ahead of the traces below: they record the raw exception text,
                # which for a proxy failure can quote the proxy URL and land a
                # password in trace.jsonl. Converting here also keeps a proxy
                # outage from being reported as a LinkedIn navigation problem.
                raise_if_proxy_error(exc)
                if allow_remember_me and await resolve_remember_me_prompt(page):
                    await stabilize_navigation(
                        f"remember-me resolution for {url}", logger
                    )
                    await record_page_trace(
                        page,
                        "extractor-navigation-error-before-remember-me-retry",
                        extra={
                            "target_url": url,
                            "wait_until": wait_until,
                            "error": redact_proxy_credentials(
                                f"{type(exc).__name__}: {exc}"
                            ),
                            "hops": hops,
                        },
                    )
                    await record_page_trace(
                        page,
                        "extractor-after-remember-me",
                        extra={
                            "target_url": url,
                            "error": redact_proxy_credentials(
                                f"{type(exc).__name__}: {exc}"
                            ),
                        },
                    )
                    unregister_navigation_listener()
                    await self._goto_with_auth_checks(
                        url,
                        wait_until=wait_until,
                        allow_remember_me=False,
                    )
                    return
                await record_page_trace(
                    page,
                    "extractor-navigation-error",
                    extra={
                        "target_url": url,
                        "wait_until": wait_until,
                        "error": redact_proxy_credentials(
                            f"{type(exc).__name__}: {exc}"
                        ),
                        "hops": hops,
                    },
                )
                await self._log_navigation_failure(url, wait_until, exc, hops)
                await self._raise_if_auth_barrier(url, navigation_error=exc)
                # Re-raised as a redacted copy rather than the original: with a
                # proxy configured, a driver error can quote the proxy URL, and
                # everything downstream from here logs the exception -- the
                # catch-all in error_handler, and FastMCP's own handler above
                # that. Only the message is rewritten; the type is preserved so
                # callers that branch on it are unaffected.
                raise redacted_copy(exc) from None

            # Ahead of the barrier check and the remember-me click, which read
            # a title and an id that any page can carry. This sees where the
            # navigation settled, not a redirect that fires later; the content
            # read checks again for that.
            if not is_linkedin_landing(page.url):
                await record_page_trace(
                    page,
                    "extractor-off-linkedin",
                    extra={"target_url": url, "hops": hops},
                )
                raise_if_off_linkedin(page.url)

            assert_linkedin_destination(url, page.url)
            barrier = await detect_auth_barrier_quick(page)
            if not barrier:
                if not is_linkedin_auth_path(page.url):
                    return
                barrier = f"auth redirect: {urlsplit(page.url).path}"

            if allow_remember_me and await resolve_remember_me_prompt(page):
                await stabilize_navigation(f"remember-me retry for {url}", logger)
                await record_page_trace(
                    page,
                    "extractor-after-remember-me-retry",
                    extra={"target_url": url, "barrier": barrier},
                )
                unregister_navigation_listener()
                await self._goto_with_auth_checks(
                    url,
                    wait_until=wait_until,
                    allow_remember_me=False,
                )
                return

            # The chooser wait gives a redirect time to land, and a chooser it
            # found on another site was refused rather than clicked. Neither
            # says the session expired, which the error below would retire it
            # for.
            raise_if_off_linkedin(page.url)
            await record_page_trace(
                page,
                "extractor-auth-barrier",
                extra={"target_url": url, "barrier": barrier},
            )
            logger.warning("Authentication barrier detected on %s: %s", url, barrier)
            raise AuthenticationError(
                "LinkedIn requires interactive re-authentication. "
                "Run with --login and complete the account selection/sign-in flow."
            )
        finally:
            unregister_navigation_listener()

    async def _navigate_to_page(self, url: str) -> None:
        """Navigate to a LinkedIn page and fail fast on auth barriers."""
        logger.debug("_navigate_to_page: target=%s", url)
        check_rate_limit_gate()
        try:
            await self._goto_with_auth_checks(url)
        except RateLimitError as exc:
            report_rate_limit(exc)
            raise

    @contextmanager
    def _watching_navigations(self) -> Iterator[list[str]]:
        """Record main-frame navigations for the duration of the block.

        The address is not the signal. A reload replaces the document and
        leaves `page.url` exactly as it was, so a check that samples the URL
        calls the replacement the same page and reads whatever it renders. The
        browser says so directly, and this is the same listener
        `_goto_with_auth_checks` uses.
        """
        page = self._session.page
        hops: list[str] = []

        def record(frame: Any) -> None:
            if frame == page.main_frame:
                hops.append(page.url)

        page.on("framenavigated", record)
        try:
            yield hops
        finally:
            try:
                page.remove_listener("framenavigated", record)
            except Exception:
                logger.debug("Could not remove navigation listener", exc_info=True)

    async def _document_origin(self) -> float | None:
        """A reading that is fixed when the document is created.

        `framenavigated` fires for a same-document history change as readily
        as for a replacement, and LinkedIn appends `currentJobId` to a search
        URL that way by itself: measured locally, `pushState`, `replaceState`
        and a bare hash change each raise the event on the main frame. Acting
        on the event alone would make every healthy search page wait out a
        chain that never ran.

        `performance.timeOrigin` separates them, and separates them without
        writing anything into the page. Measured against the same four
        changes: identical across all three same-document ones, different
        after a reload and after a navigation elsewhere.

        `None` when the reading cannot be taken, which is what a context
        destroyed by a navigation in flight looks like from here.
        """
        try:
            origin = await self._session.page.evaluate("() => performance.timeOrigin")
        except Exception:
            return None
        return origin if isinstance(origin, (int, float)) else None

    async def _settle_navigation(self, hops: list[str], origin: float | None) -> bool:
        """Wait out a navigation the page is in the middle of.

        Returns whether one happened at all.

        Three waits, and each answers a question the one before it cannot.

        Whether the page navigated is answered by the listener and the
        document identity together, the listener firing for a reload as well
        as for a redirect. It is dispatched over the
        channel that also updates `page.url`, so it arrives at the same moment
        the address would have: measured across 300 destroyed evaluations,
        0.37ms at worst on an idle machine and 0.81ms with twenty-four workers
        saturating it. `_URL_SETTLE_LAG` is three hundred times that, and an
        ordinary failure with nothing behind it pays exactly that and no more.

        The listener overreports, so `_document_origin` decides which hop
        counts: a page rewriting its own address raises the same event and
        leaves nothing to settle.

        Where it stopped is answered by the hops falling quiet. Counting them
        rather than comparing addresses, or a chain that returns to the route
        it started on reads as one that never left.

        What the destination holds is answered by the document being ready. A
        replacement renders after it commits, and the account picker measured
        200ms behind its own navigation; judging that page on arrival calls a
        barrier a loading screen.
        """
        # A hop says something moved, not that the document was replaced, so
        # the wait is for a replacement and not for an event. Leaving on the
        # first same-document hop is what a page rewriting its own address
        # would buy, and it costs the redirect arriving right behind it: the
        # search page announces `currentJobId` the moment a card is selected,
        # and a checkpoint committing fifty milliseconds later would then be
        # judged by a route comparison that has not been told yet.
        #
        # Each hop is read at most once, so a healthy page pays one evaluate
        # and the wait, and only a page that keeps moving pays more.
        lag_deadline = self._session.monotonic() + self._URL_SETTLE_LAG
        judged = 0
        replaced = False
        while self._session.monotonic() < lag_deadline:
            if len(hops) > judged:
                judged = len(hops)
                if origin is None or await self._document_origin() != origin:
                    replaced = True
                    break
            await self._session.delay(self._URL_SETTLE_POLL)
        if not replaced:
            if hops:
                logger.debug("Same document after %d history change(s)", len(hops))
            return False

        deadline = self._session.monotonic() + self._URL_SETTLE_TIMEOUT
        seen = len(hops)
        quiet_since = self._session.monotonic()
        while self._session.monotonic() < deadline:
            await self._session.delay(self._URL_SETTLE_POLL)
            if len(hops) != seen:
                seen = len(hops)
                quiet_since = self._session.monotonic()
            elif self._session.monotonic() - quiet_since >= self._URL_SETTLE_QUIET:
                break

        try:
            await self._session.page.wait_for_load_state(
                "domcontentloaded", timeout=self._DOCUMENT_READY_TIMEOUT * 1000
            )
        except Exception:
            logger.debug("Replacement document was not ready in time", exc_info=True)
        return True
