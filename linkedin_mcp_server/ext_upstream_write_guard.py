"""Fork extension: the guarded write tools own the upstream names.

Upstream registers ``send_message`` and ``connect_with_person``. Both reach
LinkedIn without the fork protections: no ledger double-send lock, no pacer
budget, no ``check_outgoing`` content check (placeholders, short links,
Calendly account, Cf/Bidi, length), and ``connect_with_person`` not even a
confirm/dry run. An agent that picks the plain name must get the guarded tool.

So the fork claims both names before its own tools register:
:func:`claim_upstream_write_names` removes the upstream registrations, and the
guarded fork tools then register under the freed names. With
``LINKEDIN_MCP_ALLOW_UPSTREAM_WRITES=1`` the upstream originals are kept but
renamed to ``<name>_unguarded`` -- for upstream debugging, never for outreach.
:func:`assert_write_tool_ownership` runs after every registration and fails
loudly if an upstream merge re-registers a plain name or if FastMCP replaced one
registration with another.

Historical note: until 2026-10-05 the guarded tools were called
``send_message_verified`` and ``connect_guarded``. Ledger rows written under
those names still count; the pacer and the duplicate check key on the action
kind (``message`` / ``invite``), never on the tool name.
"""

from __future__ import annotations

import os

import mcp.types as mt
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import Tool, ToolResult

ALLOW_ENV = "LINKEDIN_MCP_ALLOW_UPSTREAM_WRITES"

#: Upstream write tool name -> module that defines the upstream original.
UPSTREAM_WRITE_TOOLS: dict[str, str] = {
    "send_message": "linkedin_mcp_server.tools.messaging",
    "connect_with_person": "linkedin_mcp_server.tools.person",
}
#: Module that defines the guarded tools now holding the plain names.
GUARDED_MODULE = "linkedin_mcp_server.tools.ext"
UNGUARDED_SUFFIX = "_unguarded"

#: Unguarded name exposed under the opt-out -> the guarded tool to use instead.
GUARDED_REPLACEMENT: dict[str, str] = {
    f"{name}{UNGUARDED_SUFFIX}": name for name in UPSTREAM_WRITE_TOOLS
}


def upstream_writes_allowed() -> bool:
    return os.environ.get(ALLOW_ENV, "").strip() == "1"


def _local_tools(mcp: FastMCP) -> dict[str, list]:
    by_name: dict[str, list] = {}
    for component in mcp.local_provider._components.values():
        if isinstance(component, Tool):
            by_name.setdefault(component.name, []).append(component)
    return by_name


def _module_of(tool) -> str:
    fn = getattr(tool, "fn", None)
    return getattr(fn, "__module__", "") or ""


def claim_upstream_write_names(mcp: FastMCP) -> None:
    """Free the plain upstream write names before the fork tools register.

    Raises if an upstream original is missing or comes from an unexpected
    module: the guard must never silently claim the wrong registration.
    """
    tools = _local_tools(mcp)
    allowed = upstream_writes_allowed()
    for name, module in UPSTREAM_WRITE_TOOLS.items():
        found = tools.get(name, [])
        if len(found) != 1 or _module_of(found[0]) != module:
            raise RuntimeError(
                f"fork write guard: expected exactly one upstream '{name}' from "
                f"{module}, found {[_module_of(t) for t in found]}"
            )
        mcp.local_provider.remove_tool(name)
        if allowed:
            renamed = found[0].model_copy(
                update={"name": f"{name}{UNGUARDED_SUFFIX}"}
            )
            mcp.add_tool(renamed)


def write_tool_owners(mcp: FastMCP) -> dict[str, str]:
    """Write tool name -> defining module, for every name the guard governs."""
    tools = _local_tools(mcp)
    names = list(UPSTREAM_WRITE_TOOLS) + list(GUARDED_REPLACEMENT)
    owners: dict[str, str] = {}
    for name in names:
        found = tools.get(name, [])
        if len(found) > 1:
            raise RuntimeError(f"fork write guard: '{name}' registered twice")
        if found:
            owners[name] = _module_of(found[0])
    return owners


def assert_write_tool_ownership(mcp: FastMCP) -> None:
    """Fail loudly unless the plain names are the guarded fork tools."""
    owners = write_tool_owners(mcp)
    expected = {name: GUARDED_MODULE for name in UPSTREAM_WRITE_TOOLS}
    if upstream_writes_allowed():
        expected |= {
            f"{name}{UNGUARDED_SUFFIX}": module
            for name, module in UPSTREAM_WRITE_TOOLS.items()
        }
    if owners != expected:
        raise RuntimeError(
            f"fork write guard: write tool ownership {owners} != {expected}"
        )


async def upstream_tool_list():
    """The tool list with the upstream originals under their upstream names.

    For the upstream contract tests only: builds a server with the opt-out set
    and maps ``<name>_unguarded`` back to ``<name>``.
    """
    from linkedin_mcp_server.server import create_mcp_server

    previous = os.environ.get(ALLOW_ENV)
    os.environ[ALLOW_ENV] = "1"
    try:
        tools = await create_mcp_server().list_tools()
    finally:
        if previous is None:
            os.environ.pop(ALLOW_ENV, None)
        else:
            os.environ[ALLOW_ENV] = previous
    out = []
    for tool in tools:
        if tool.name in GUARDED_REPLACEMENT:
            tool = tool.model_copy(update={"name": GUARDED_REPLACEMENT[tool.name]})
        out.append(tool)
    return out


class UpstreamWriteGuardMiddleware(Middleware):
    """Defence in depth: refuse an unguarded name unless the opt-out is set.

    Normally the unguarded tools are not registered at all; this catches a
    registration that slipped past :func:`claim_upstream_write_names`.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        name = context.message.name
        replacement = GUARDED_REPLACEMENT.get(name)
        if replacement is not None and not upstream_writes_allowed():
            raise ToolError(
                f"'{name}' ist im Fork-Betrieb gesperrt: es umgeht Ledger-"
                "Doppelversandsperre, Pacer-Budget und Inhaltspruefung. "
                f"Nimm '{replacement}'. Nichts wurde gesendet."
            )
        return await call_next(context)
