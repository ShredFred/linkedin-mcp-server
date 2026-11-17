"""A failed search gives its pace booking back (operator 2026-11-17).

On 2026-11-12 seven searches were booked and only three answered. No browser,
no live action."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from linkedin_mcp_server import ext_outreach as outreach


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(outreach.LEDGER_ENV, str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv(outreach.NOTES_ENV, str(tmp_path / "notes.json"))


def _searches() -> int:
    return outreach.Pacer(outreach.Ledger.default()).state("search")["today"]


def _call(name, args, run, monkeypatch):
    import linkedin_mcp_server.tools.ext as m
    import linkedin_mcp_server.tools.ext_stage2 as s2
    from fastmcp import Client, FastMCP

    monkeypatch.setattr(m, "_run", run)
    monkeypatch.setattr(s2, "_run", run)

    async def no_sleep(*a, **k):
        return None

    monkeypatch.setattr(s2.asyncio, "sleep", no_sleep)
    mcp = FastMCP("t")
    m.register_ext_tools(mcp)

    async def go():
        async with Client(mcp) as c:
            res = await c.call_tool(name, args, raise_on_error=False)
            return res.structured_content, res.is_error

    return asyncio.run(go())


def test_refund_offsets_booking_and_ignores_writes():
    pacer = outreach.Pacer(outreach.Ledger.default())
    pacer.take("search", 3, tool="t")
    pacer.refund("search", 2, tool="t", reason="x")
    assert _searches() == 1
    pacer.take("like", 1, tool="t")
    pacer.refund("like", 1, tool="t")  # a write that may have happened keeps counting
    assert pacer.state("like")["today"] == 1


def test_search_events_error_answer_is_refunded(monkeypatch):
    async def run(ctx, tool, body):
        return {"error": "unconfirmed_empty: ...", "keywords": "x"}

    out, is_error = _call("search_events", {"keywords": "foundry"}, run, monkeypatch)
    assert out["error"].startswith("unconfirmed_empty")
    assert _searches() == 0


def test_search_events_exception_is_refunded(monkeypatch):
    async def run(ctx, tool, body):
        raise RuntimeError("Failed to start browser (profile_busy)")

    _, is_error = _call("search_events", {"keywords": "foundry"}, run, monkeypatch)
    assert is_error
    assert _searches() == 0


def test_search_events_answer_keeps_booking(monkeypatch):
    async def run(ctx, tool, body):
        return {"keywords": "x", "count": 0, "events": []}

    _call("search_events", {"keywords": "foundry"}, run, monkeypatch)
    assert _searches() == 1  # a confirmed empty result was a real search


def test_find_events_refunds_only_failed_queries(monkeypatch):
    async def run(ctx, tool, body):
        return {"count": 0, "events": [], "errors": [{"query": "b", "error": "x"}]}

    _call("find_events", {"keywords": ["a", "b", "c"]}, run, monkeypatch)
    assert _searches() == 2


def test_backfill_raise_doubles_until_2026_11_24_then_falls_back(monkeypatch):
    # conftest blanks PACE_TEMP_RAISES; read the shipped value from the source.
    import ast
    import inspect

    src = inspect.getsource(outreach)
    node = next(
        n
        for n in ast.parse(src).body
        if isinstance(n, ast.AnnAssign)
        and getattr(n.target, "id", "") == "PACE_TEMP_RAISES"
    )
    monkeypatch.setattr(outreach, "PACE_TEMP_RAISES", ast.literal_eval(node.value))
    assert outreach.pace_budget("search", datetime(2026, 11, 24, 23).astimezone()) == {
        "day": 2 * outreach.PACE_BUDGETS["search"]["day"],
        "week": 2 * outreach.PACE_BUDGETS["search"]["week"],
    }
    assert (
        outreach.pace_budget("search", datetime(2026, 11, 25, 0, 5).astimezone())
        == outreach.PACE_BUDGETS["search"]
    )
