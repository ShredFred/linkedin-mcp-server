"""MiViA fork: block the unguarded upstream write tools in MiViA operation.

Upstream registers ``send_message`` and ``connect_with_person``. Both reach
LinkedIn without the MiViA protections that ``send_message_verified`` and
``connect_guarded`` carry: no ledger double-send lock, no pacer budget, no
``check_outgoing`` content check (placeholders, short links, Calendly account,
Cf/Bidi, length), and ``connect_with_person`` not even a confirm/dry run. An
agent that picks the shorter name would bypass all of it.

The tools stay registered (the upstream tool contract and the policy-trace
tests depend on their schema); a call is refused before it reaches the browser
and names the guarded tool. ``MIVIA_ALLOW_UPSTREAM_WRITES=1`` lifts the block
for one process -- for upstream debugging, never for outreach.
"""

from __future__ import annotations

import os

import mcp.types as mt
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

ALLOW_ENV = "MIVIA_ALLOW_UPSTREAM_WRITES"

#: Upstream write tool -> the MiViA tool that carries the protections.
GUARDED_REPLACEMENT: dict[str, str] = {
    "send_message": "send_message_verified",
    "connect_with_person": "connect_guarded",
}


def upstream_writes_allowed() -> bool:
    return os.environ.get(ALLOW_ENV, "").strip() == "1"


class UpstreamWriteGuardMiddleware(Middleware):
    """Refuse calls to unguarded upstream write tools."""

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        name = context.message.name
        replacement = GUARDED_REPLACEMENT.get(name)
        if replacement is not None and not upstream_writes_allowed():
            raise ToolError(
                f"'{name}' ist im MiViA-Betrieb gesperrt: es umgeht Ledger-"
                "Doppelversandsperre, Pacer-Budget und Inhaltspruefung. "
                f"Nimm '{replacement}'. Nichts wurde gesendet."
            )
        return await call_next(context)
