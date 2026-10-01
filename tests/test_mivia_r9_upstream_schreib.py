"""MiViA fork: unguarded upstream write tools are refused in MiViA operation."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from linkedin_mcp_server.mivia_upstream_write_guard import (
    ALLOW_ENV,
    GUARDED_REPLACEMENT,
)
from linkedin_mcp_server.server import create_mcp_server


async def test_every_non_mivia_write_tool_is_guarded():
    """A new upstream write tool must fail here until it is classified."""
    tools = {t.name: t for t in await create_mcp_server().list_tools()}
    unguarded_writes = {
        name
        for name, t in tools.items()
        if t.annotations
        and t.annotations.destructive_hint
        and "mivia" not in (t.tags or set())
        and name != "close_session"
    }
    assert unguarded_writes == set(GUARDED_REPLACEMENT)
    for replacement in GUARDED_REPLACEMENT.values():
        assert replacement in tools


@pytest.mark.parametrize(
    ("name", "args"),
    [
        (
            "send_message",
            {"linkedin_username": "x", "message": "hi", "confirm_send": True},
        ),
        ("connect_with_person", {"linkedin_username": "x", "note": "hi"}),
    ],
)
async def test_upstream_write_refused_before_browser(name, args, monkeypatch):
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    with patch(
        f"linkedin_mcp_server.tools.{'messaging' if name == 'send_message' else 'person'}.get_ready_extractor",
        new=AsyncMock(),
    ) as ready:
        async with Client(create_mcp_server()) as client:
            with pytest.raises(ToolError, match=GUARDED_REPLACEMENT[name]):
                await client.call_tool(name, args)
        ready.assert_not_awaited()


async def test_opt_out_lets_the_call_through(monkeypatch):
    monkeypatch.setenv(ALLOW_ENV, "1")
    with patch(
        "linkedin_mcp_server.tools.person.get_ready_extractor",
        new=AsyncMock(side_effect=RuntimeError("reached")),
    ) as ready:
        async with Client(create_mcp_server()) as client:
            with pytest.raises(ToolError):
                await client.call_tool(
                    "connect_with_person", {"linkedin_username": "x"}
                )
        ready.assert_awaited()


async def test_guarded_tools_are_not_blocked(monkeypatch):
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    from linkedin_mcp_server.mivia_upstream_write_guard import (
        UpstreamWriteGuardMiddleware,
    )

    for name in GUARDED_REPLACEMENT.values():
        ctx = type("C", (), {"message": type("M", (), {"name": name})()})()
        nxt = AsyncMock(return_value="ok")
        assert await UpstreamWriteGuardMiddleware().on_call_tool(ctx, nxt) == "ok"
