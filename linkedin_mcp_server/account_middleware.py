"""Fork extension: route every tool call to exactly one registered account.

Design, and why it is shaped this way:

* **One active browser per process.** The driver keeps one browser, one lease
  and one profile per process. Each account has its own profile directory,
  lease and browser; switching accounts closes the previous browser first.
  Calls for two accounts therefore run one after the other, never interleaved
  on one browser. That is the "no mixing" guarantee, bought with throughput.
* **Binding per call.** The account is resolved once, before the call, and the
  binding lock is held until the result is back. A second call cannot switch
  the profile underneath a running one. After the call the binding is checked
  again; a changed binding drops the result.
* **Identity, fail-closed.** Before the first call of an account, before every
  non-read tool, and whenever the browser object or the login generation has
  changed (restart, cookie refresh), the signed-in member is compared with the
  register's ``expected_identity``. A mismatch closes the browser and returns
  ``account_identity_mismatch`` with no tool data.
* **Rights.** ``allowed_tools`` decides; a tool without ``readOnlyHint=True``
  counts as write. Existing write guards (ledger, pacer, canary) stay inside
  the tools and run as before.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

import mcp.types as mt
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import Tool, ToolResult

from linkedin_mcp_server import account_context
from linkedin_mcp_server.accounts import (
    ACCOUNT_FREE_TOOLS,
    Account,
    AccountError,
    Registry,
    account_state_dir,
    audit,
    identity_matches,
    tool_allowed,
)

logger = logging.getLogger(__name__)

ACCOUNT_PARAM = "account"
AUDIT_CODES = frozenset(
    {
        "account_unknown",
        "account_identity_unconfigured",
        "account_tool_not_allowed",
        "profile_busy",
        "account_not_signed_in",
        "account_identity_mismatch",
        "account_identity_unverified",
        "account_switched_during_call",
        "account_switch_unconfirmed",
        "account_switch_failed",
    }
)


class ProfileSwitcher(Protocol):
    def current_dir(self) -> Path: ...
    async def activate(self, account: Account) -> None: ...
    async def deactivate(self) -> None: ...
    def fingerprint(self) -> object | None: ...
    def signed_in(self, account: Account) -> bool: ...


IdentityProbe = Callable[[Account], Awaitable[set[str]]]
ReadOnlyLookup = Callable[[MiddlewareContext[Any], str], Awaitable[bool]]


def _fail(code: str, message: str) -> ToolError:
    return ToolError(f"{code}: {message}")


class AccountMiddleware(Middleware):
    def __init__(
        self,
        registry: Registry,
        *,
        switcher: ProfileSwitcher,
        identify: IdentityProbe,
        read_only: ReadOnlyLookup | None = None,
        busy_timeout: float = 60.0,
    ) -> None:
        self.registry = registry
        self.switcher = switcher
        self.identify = identify
        self.read_only = read_only or _annotation_read_only
        self.busy_timeout = busy_timeout
        self._binding = asyncio.Lock()
        self._account_locks = {a: asyncio.Lock() for a in registry.accounts}
        self._bound: str | None = None
        # alias -> fingerprint at the last successful identity check
        self._verified: dict[str, object] = {}
        self._expired: set[str] = set()

    # -- tool list --------------------------------------------------------
    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Any],
    ) -> Any:
        tools = await call_next(context)
        return [_with_account_param(t) for t in tools]

    # -- calls ------------------------------------------------------------
    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        name = context.message.name
        args = dict(context.message.arguments or {})
        requested = args.pop(ACCOUNT_PARAM, None)
        context = context.copy(
            message=context.message.model_copy(update={"arguments": args})
        )
        if name in ACCOUNT_FREE_TOOLS:
            return await call_next(context)
        try:
            account = self.registry.resolve(requested)
        except AccountError as exc:
            raise _fail(exc.code, str(exc).split(": ", 1)[1]) from None
        status = "error"
        try:
            result = await self._run(context, call_next, account, name)
            status = "ok"
            return result
        except ToolError as exc:
            # Only our own codes reach the audit; a tool's message may carry
            # content (URLs, names) and must not.
            code = str(exc).split(":", 1)[0]
            status = code if code in AUDIT_CODES else "tool_error"
            raise
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            audit(account.alias, name, status)

    async def _run(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
        account: Account,
        name: str,
    ) -> ToolResult:
        alias = account.alias
        if account.expected_identity is None:
            raise _fail(
                "account_identity_unconfigured",
                f"account '{alias}' has no expected_identity; nothing was read",
            )
        read_only = await self.read_only(context, name)
        if not tool_allowed(account, name, read_only=read_only):
            raise _fail(
                "account_tool_not_allowed",
                f"account '{alias}' may not run '{name}'; nothing was done",
            )
        lock = self._account_locks[alias]
        try:
            await asyncio.wait_for(lock.acquire(), self.busy_timeout)
        except TimeoutError:
            raise _fail(
                "profile_busy", f"account '{alias}' is busy with another call"
            ) from None
        try:
            try:
                await asyncio.wait_for(self._binding.acquire(), self.busy_timeout)
            except TimeoutError:
                raise _fail(
                    "profile_busy",
                    f"another account holds the browser; '{alias}' was not used",
                ) from None
            try:
                if not self.switcher.signed_in(account):
                    raise _fail(
                        "account_not_signed_in",
                        f"account '{alias}' has no session; run --login "
                        f"--account {alias}. No other account was used.",
                    )
                if self._bound != alias:
                    self._bound = None
                    self._verified.pop(alias, None)
                    await self.switcher.activate(account)
                    self._bound = alias
                fp = self.switcher.fingerprint()
                if (
                    not read_only
                    or fp is None
                    or self._verified.get(alias) is None
                    or self._verified[alias] != fp
                ):
                    await self._verify(account)
                tokens = account_context.bind(alias, account_state_dir(alias))
                try:
                    result = await call_next(context)
                finally:
                    account_context.unbind(tokens)
                # Binding still intact? Anything else means the call may have
                # run on another profile, and its result is not returned.
                if (
                    self._bound != alias
                    or self.switcher.current_dir() != account.user_data_dir
                ):
                    self._bound = None
                    raise _fail(
                        "account_switched_during_call",
                        "the profile changed during the call; result withheld",
                    )
                if self.switcher.fingerprint() != self._verified.get(alias):
                    # Restarted or re-logged in mid-call: re-check before any
                    # data leaves.
                    await self._verify(account)
                return _tag(result, alias)
            finally:
                self._binding.release()
        finally:
            lock.release()

    async def _verify(self, account: Account) -> None:
        alias = account.alias
        self._verified.pop(alias, None)
        try:
            observed = await self.identify(account)
        except ToolError as exc:
            self._bound = None
            if "auth" in str(exc).lower() or "login" in str(exc).lower():
                self._expired.add(alias)
            raise _fail(
                "account_identity_unverified",
                f"identity of '{alias}' could not be checked; nothing was read",
            ) from exc
        except Exception as exc:
            self._bound = None
            raise _fail(
                "account_identity_unverified",
                f"identity of '{alias}' could not be checked "
                f"({type(exc).__name__}); nothing was read",
            ) from None
        assert account.expected_identity is not None
        if not identity_matches(account.expected_identity, observed):
            logger.warning("account %s: signed-in identity does not match", alias)
            self._bound = None
            try:
                await self.switcher.deactivate()
            finally:
                raise _fail(
                    "account_identity_mismatch",
                    f"the browser for '{alias}' is signed in as a different "
                    "member; no data was returned",
                )
        self._expired.discard(alias)
        self._verified[alias] = self.switcher.fingerprint()

    # -- list_accounts ----------------------------------------------------
    def describe(self) -> list[dict[str, Any]]:
        """Alias, session status, identity checked. Nothing personal."""
        out = []
        for alias, account in self.registry.accounts.items():
            if alias in self._expired:
                state = "expired"
            elif self.switcher.signed_in(account):
                state = "signed_in"
            else:
                state = "not_signed_in"
            out.append(
                {
                    "alias": alias,
                    "default": alias == self.registry.default,
                    "status": state,
                    "identity_verified": alias in self._verified,
                }
            )
        return out


def _with_account_param(tool: Tool) -> Tool:
    if tool.name in ACCOUNT_FREE_TOOLS:
        return tool
    params = dict(tool.parameters or {})
    props = dict(params.get("properties") or {})
    if ACCOUNT_PARAM in props:
        return tool
    props[ACCOUNT_PARAM] = {
        "anyOf": [{"type": "string"}, {"type": "null"}],
        "default": None,
        "description": "Account alias from list_accounts; omitted = default account.",
    }
    params["properties"] = props
    return tool.model_copy(update={"parameters": params})


def _tag(result: ToolResult, alias: str) -> ToolResult:
    sc = result.structured_content
    if isinstance(sc, dict):
        result.structured_content = {**sc, ACCOUNT_PARAM: alias}
    content = list(result.content or [])
    if len(content) == 1 and isinstance(content[0], mt.TextContent):
        try:
            data = json.loads(content[0].text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            data[ACCOUNT_PARAM] = alias
            content[0] = content[0].model_copy(update={"text": json.dumps(data)})
            result.content = content
    meta = dict(getattr(result, "meta", None) or {})
    meta[ACCOUNT_PARAM] = alias
    try:
        result.meta = meta
    except (AttributeError, ValueError):
        pass
    return result


async def _annotation_read_only(context: MiddlewareContext[Any], name: str) -> bool:
    """True only for a tool explicitly annotated readOnlyHint=True."""
    server = context.fastmcp_context.fastmcp if context.fastmcp_context else None
    if server is None:
        return False
    try:
        tool = await server.get_tool(name)
    except Exception:
        return False
    ann = getattr(tool, "annotations", None)
    return bool(ann is not None and getattr(ann, "readOnlyHint", None) is True)


# -- real browser bindings --------------------------------------------------


class DriverProfileSwitcher:
    """Binds the process-wide driver to one account's profile directory."""

    def current_dir(self) -> Path:
        from linkedin_mcp_server.accounts import _canon
        from linkedin_mcp_server.config import get_config

        return _canon(Path(get_config().browser.user_data_dir))

    async def activate(self, account: Account) -> None:
        from linkedin_mcp_server.config import get_config
        from linkedin_mcp_server.drivers.browser import close_browser
        from linkedin_mcp_server.profile_claim import ensure_profile_claim

        from linkedin_mcp_server.drivers import browser as drv

        try:
            await close_browser()
            if drv._browser is not None or drv._browser_lease is not None:
                raise _fail(
                    "account_switch_unconfirmed",
                    "the previous browser did not close; no account switch",
                )
            ensure_profile_claim(account.user_data_dir)
        except ToolError:
            raise
        except Exception as exc:
            raise _fail(
                "account_switch_failed",
                f"could not bind account '{account.alias}' ({type(exc).__name__})",
            ) from None
        get_config().browser.user_data_dir = str(account.user_data_dir)

    async def deactivate(self) -> None:
        from linkedin_mcp_server.drivers.browser import close_browser

        await close_browser()

    def fingerprint(self) -> object | None:
        from linkedin_mcp_server.bootstrap import current_login_generation
        from linkedin_mcp_server.drivers import browser as drv

        current = drv._browser
        if current is None:
            return None
        return (id(current), current_login_generation())

    def signed_in(self, account: Account) -> bool:
        from linkedin_mcp_server.session_state import profile_exists

        try:
            return profile_exists(account.user_data_dir)
        except OSError:
            return False


async def driver_identify(account: Account) -> set[str]:
    """Read the signed-in member from /in/me/: slug, plus URN if expected."""
    import re

    from linkedin_mcp_server.dependencies import get_ready_extractor
    from linkedin_mcp_server.drivers.browser import get_or_create_browser

    await get_ready_extractor(None, tool_name="get_my_profile")
    browser = await get_or_create_browser()
    page = browser.page
    await page.goto("https://www.linkedin.com/in/me/", wait_until="domcontentloaded")
    found: set[str] = set()
    match = re.search(r"/in/([^/?#]+)", page.url)
    if match and match.group(1).lower() != "me":
        found.add(match.group(1).lower())
    expected = account.expected_identity or ""
    if expected.startswith("urn:li:"):
        # Other members' URNs appear on one's own profile too (sidebar,
        # activity). Only the clearly dominant profile URN counts as self, and
        # a tie counts as nothing: fail-closed. A slug is the stronger key.
        kind = expected.split(":")[2]
        counts: dict[str, int] = {}
        for urn in re.findall(rf"urn:li:{kind}:[A-Za-z0-9_-]+", await page.content()):
            counts[urn] = counts.get(urn, 0) + 1
        ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
        if ranked and (len(ranked) == 1 or ranked[0][1] > 2 * ranked[1][1]):
            found.add(ranked[0][0])
    return found
