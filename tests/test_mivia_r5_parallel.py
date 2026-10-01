"""R5: two simultaneous tool calls never drive the shared page at once.

The concern: Claude Code issues ``send_message_verified`` and ``get_inbox`` (or
two sends, or ``delete_own_post`` beside ``get_feed``) in one turn. If both
bodies could run interleaved, the second call's navigation would land in the
middle of the first call's typing -- a message to the wrong person or half an
input. These tests pin that the production server serializes every tool body,
MiViA ones included, and that no MiViA module hands page work to a task that
would outlive the per-call lock.
"""

from __future__ import annotations

import asyncio
import re

from pathlib import Path

import pytest

from linkedin_mcp_server.sequential_tool_middleware import (
    SequentialToolExecutionMiddleware,
)
from linkedin_mcp_server.server import create_mcp_server

_PKG = Path(__file__).resolve().parents[1] / "linkedin_mcp_server"


class _FakePage:
    """Records every navigation and keystroke with the call that issued it."""

    def __init__(self) -> None:
        self.url = "about:blank"
        self.log: list[tuple[str, str]] = []

    async def goto(self, owner: str, url: str) -> None:
        await asyncio.sleep(0)
        self.url = url
        self.log.append((owner, f"goto {url}"))

    async def type(self, owner: str, text: str) -> None:
        for ch in text:
            await asyncio.sleep(0)  # a yield per key, like real typing
            self.log.append((owner, f"key {ch} @ {self.url}"))


async def test_production_server_serializes_parallel_page_work():
    mcp = create_mcp_server()
    assert any(isinstance(m, SequentialToolExecutionMiddleware) for m in mcp.middleware)
    page = _FakePage()

    @mcp.tool(name="r5_fake_send")
    async def fake_send(person: str, text: str) -> str:
        await page.goto(person, f"/messaging/{person}")
        await page.type(person, text)
        return page.url

    @mcp.tool(name="r5_fake_inbox")
    async def fake_inbox() -> str:
        await page.goto("inbox", "/messaging/")
        await asyncio.sleep(0)
        return page.url

    results = await asyncio.gather(
        mcp.call_tool("r5_fake_send", {"person": "anna", "text": "hallo"}),
        mcp.call_tool("r5_fake_inbox", {}),
        mcp.call_tool("r5_fake_send", {"person": "bert", "text": "moin"}),
    )

    # Each call's entries are contiguous: no foreign navigation inside a send.
    owners = [o for o, _ in page.log]
    runs = [o for i, o in enumerate(owners) if i == 0 or owners[i - 1] != o]
    assert sorted(runs) == ["anna", "bert", "inbox"], runs
    # Every keystroke landed on the page of the person it was meant for.
    for owner, entry in page.log:
        if entry.startswith("key"):
            assert entry.endswith(f"/messaging/{owner}"), entry
    assert results[0].structured_content == {"result": "/messaging/anna"}


async def test_without_the_lock_the_interleaving_is_real():
    """Control: the fake page does expose the hazard when nothing serializes."""
    page = _FakePage()

    async def send(person: str) -> None:
        await page.goto(person, f"/messaging/{person}")
        await page.type(person, "hallo")

    await asyncio.gather(send("anna"), send("bert"))
    wrong = [e for o, e in page.log if e.startswith("key") and not e.endswith(f"/{o}")]
    assert wrong, "the control must show keystrokes on the wrong thread"


def test_every_mivia_tool_is_served_behind_the_lock():
    """The middleware has no allow-list: every registered tool passes it."""
    mcp = create_mcp_server()
    served = set(asyncio.run(_list(mcp)))
    assert {
        "send_message_verified",
        "send_message",
        "get_inbox",
        "delete_own_post",
        "get_feed",
    } <= served
    src = (_PKG / "sequential_tool_middleware.py").read_text("utf-8")
    assert "tool_name in" not in src and "if tool_name" not in src


async def _list(mcp) -> list[str]:
    return [t.name for t in await mcp.list_tools()]


@pytest.mark.parametrize(
    "path",
    sorted(
        [
            *_PKG.glob("tools/mivia*.py"),
            *_PKG.glob("mivia*.py"),
            *_PKG.glob("linkedin/mivia_*.py"),
        ]
    ),
    ids=lambda p: p.name,
)
def test_no_mivia_module_detaches_page_work_from_the_call(path: Path):
    """A task spawned inside a tool would keep driving the page after the
    middleware released the lock. None exists today; this keeps it that way."""
    src = path.read_text("utf-8")
    for pattern in (
        r"\bcreate_task\(",
        r"\bensure_future\(",
        r"\bshield\(",
        r"\bThread\(",
        r"\brun_coroutine_threadsafe\(",
    ):
        assert not re.search(pattern, src), f"{path.name}: {pattern}"
