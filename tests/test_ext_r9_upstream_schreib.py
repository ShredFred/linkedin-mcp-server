"""Fork extension: the guarded tools own the upstream write names.

Without LINKEDIN_MCP_ALLOW_UPSTREAM_WRITES=1 the unguarded upstream originals are
not registered at all; with it they appear as ``<name>_unguarded``. In both modes
every tool name is unique and the plain names belong to the fork module.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from linkedin_mcp_server.ext_upstream_write_guard import (
    ALLOW_ENV,
    GUARDED_MODULE,
    GUARDED_REPLACEMENT,
    UPSTREAM_WRITE_TOOLS,
    UpstreamWriteGuardMiddleware,
    assert_write_tool_ownership,
    claim_upstream_write_names,
    write_tool_owners,
)
from linkedin_mcp_server.server import create_mcp_server


@pytest.fixture(params=["", "1"], ids=["blocked", "allowed"])
def mode(request, monkeypatch):
    monkeypatch.setenv(ALLOW_ENV, request.param)
    return request.param == "1"


async def test_tool_names_are_unique_and_owned(mode):
    mcp = create_mcp_server()
    names = [t.name for t in await mcp.list_tools()]
    assert len(names) == len(set(names))
    owners = write_tool_owners(mcp)
    expected = {name: GUARDED_MODULE for name in UPSTREAM_WRITE_TOOLS}
    if mode:
        expected |= {f"{n}_unguarded": m for n, m in UPSTREAM_WRITE_TOOLS.items()}
    assert owners == expected
    for name in ("send_message_unguarded", "connect_with_person_unguarded"):
        assert (name in names) is mode


async def test_every_non_ext_write_tool_is_guarded(mode):
    """A new upstream write tool must fail here until it is classified."""
    tools = {t.name: t for t in await create_mcp_server().list_tools()}
    unguarded_writes = {
        name
        for name, t in tools.items()
        if t.annotations
        and t.annotations.destructive_hint
        and "ext" not in (t.tags or set())
        and name != "close_session"
    }
    assert unguarded_writes == (set(GUARDED_REPLACEMENT) if mode else set())


async def test_plain_names_describe_a_dry_run_first(monkeypatch):
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    tools = {t.name: t for t in await create_mcp_server().list_tools()}
    for name in UPSTREAM_WRITE_TOOLS:
        assert tools[name].description.strip().startswith(
            "Without confirm_send=true this is a dry run only: nothing is sent."
        )
        assert "ext" in tools[name].tags


def test_upstream_reregistration_fails_loudly(monkeypatch):
    """An upstream merge that registers a plain name again must not pass."""
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    mcp = create_mcp_server()
    with pytest.raises(Exception):
        # FastMCP is configured with on_duplicate="error".
        @mcp.tool(name="send_message")
        async def send_message(x: str) -> str:  # pragma: no cover
            return x

    mcp.local_provider.remove_tool("connect_with_person")

    @mcp.tool(name="connect_with_person")
    async def connect_with_person(x: str) -> str:  # pragma: no cover
        return x

    with pytest.raises(RuntimeError, match="ownership"):
        assert_write_tool_ownership(mcp)


def test_claim_refuses_a_missing_upstream_original(monkeypatch):
    from fastmcp import FastMCP

    monkeypatch.delenv(ALLOW_ENV, raising=False)
    with pytest.raises(RuntimeError, match="expected exactly one upstream"):
        claim_upstream_write_names(FastMCP("t"))


def test_upstream_sources_still_register_the_originals():
    """If upstream renames its tools, the claim above must be revisited."""
    from linkedin_mcp_server.tools import messaging, person

    assert "async def send_message(" in inspect.getsource(messaging)
    assert "async def connect_with_person(" in inspect.getsource(person)


async def test_opt_out_lets_the_unguarded_call_through(monkeypatch):
    monkeypatch.setenv(ALLOW_ENV, "1")
    with patch(
        "linkedin_mcp_server.tools.person.get_ready_extractor",
        new=AsyncMock(side_effect=RuntimeError("reached")),
    ) as ready:
        async with Client(create_mcp_server()) as client:
            with pytest.raises(ToolError):
                await client.call_tool(
                    "connect_with_person_unguarded", {"linkedin_username": "x"}
                )
        ready.assert_awaited()


async def test_middleware_refuses_unguarded_names_without_opt_out(monkeypatch):
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    for name, replacement in GUARDED_REPLACEMENT.items():
        ctx = type("C", (), {"message": type("M", (), {"name": name})()})()
        with pytest.raises(ToolError, match=replacement):
            await UpstreamWriteGuardMiddleware().on_call_tool(ctx, AsyncMock())
    for name in UPSTREAM_WRITE_TOOLS:
        ctx = type("C", (), {"message": type("M", (), {"name": name})()})()
        nxt = AsyncMock(return_value="ok")
        assert await UpstreamWriteGuardMiddleware().on_call_tool(ctx, nxt) == "ok"
