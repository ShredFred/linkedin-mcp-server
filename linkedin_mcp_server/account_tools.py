"""Fork extension: wire the account register into a server."""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP

from linkedin_mcp_server.account_middleware import (
    AccountMiddleware,
    DriverProfileSwitcher,
    driver_identify,
)
from linkedin_mcp_server.accounts import Registry, load_registry

logger = logging.getLogger(__name__)


def install_accounts(
    mcp: FastMCP, registry: Registry | None = None, **middleware_kwargs: Any
) -> AccountMiddleware | None:
    """Add the account middleware and ``list_accounts`` when a register exists.

    An invalid register raises here, at startup: running on whichever profile
    happens to be configured would be the silent fallback this feature forbids.
    """
    registry = registry or load_registry()
    if registry is None:
        return None
    for warning in registry.warnings:
        logger.warning(warning)
    middleware_kwargs.setdefault("switcher", DriverProfileSwitcher())
    middleware_kwargs.setdefault("identify", driver_identify)
    if "busy_timeout" not in middleware_kwargs:
        from linkedin_mcp_server.config import get_config

        middleware_kwargs["busy_timeout"] = float(
            get_config().browser.browser_wait_seconds
        )
    middleware = AccountMiddleware(registry, **middleware_kwargs)
    mcp.add_middleware(middleware)

    @mcp.tool(
        title="List Accounts",
        annotations={"readOnlyHint": True, "openWorldHint": False},
        tags={"accounts"},
    )
    async def list_accounts() -> dict[str, Any]:
        """List the registered LinkedIn accounts.

        Returns only aliases, whether a session exists (signed_in / expired /
        not_signed_in), whether the identity was verified in this server run,
        and which alias is the default. No personal data.
        """
        return {"accounts": middleware.describe()}

    return middleware
