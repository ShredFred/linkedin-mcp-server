"""Fork extension: ledger rows written under the old tool names still count.

Until 2026-10-05 the guarded tools were ``send_message_verified`` and
``connect_guarded``. The pacer and the duplicate check key on the action kind,
so a row from that era must still block a repeat and still use up budget.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

from fastmcp import Client, FastMCP

from linkedin_mcp_server import ext_outreach as outreach


def _write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _old_rows(now):
    started = now.isoformat(timespec="seconds")
    return [
        {
            "attempt": "old-msg-1",
            "kind": "message",
            "tool": "send_message_verified",
            "recipient": outreach.recipient_key("ext-person-a"),
            "text_sha": outreach.text_sha("Guten Tag"),
            "status": "verified",
            "started_at": started,
        },
        {
            "attempt": "old-inv-1",
            "kind": "invite",
            "tool": "connect_guarded",
            "recipient": outreach.recipient_key("ext-person-b"),
            "status": "sent",
            "started_at": started,
        },
    ]


def test_old_name_rows_count_for_pacer_and_dedup(tmp_path, monkeypatch):
    path = tmp_path / "ledger.jsonl"
    monkeypatch.setenv(outreach.LEDGER_ENV, str(path))
    now = datetime.now().astimezone()
    _write(path, _old_rows(now))
    ledger = outreach.Ledger.default()
    assert ledger.already_contacted(
        "message", "ext-person-a", outreach.text_sha("Guten Tag")
    )
    assert ledger.already_contacted("invite", "ext-person-b", None)
    pacer = outreach.Pacer(ledger)
    since = now - timedelta(hours=1)
    assert pacer.used("message", since) == 1
    assert pacer.used("invite", since) == 1


def test_renamed_tools_see_old_name_rows(tmp_path, monkeypatch):
    import linkedin_mcp_server.tools.ext as m

    path = tmp_path / "ledger.jsonl"
    monkeypatch.setenv(outreach.LEDGER_ENV, str(path))
    _write(path, _old_rows(datetime.now().astimezone()))
    mcp = FastMCP("t")
    m.register_ext_tools(mcp)

    async def go():
        async with Client(mcp) as c:
            msg = await c.call_tool(
                "send_message",
                {
                    "linkedin_username": "ext-person-a",
                    "message": "Guten Tag",
                    "confirm_send": False,
                },
            )
            inv = await c.call_tool(
                "connect_with_person",
                {"linkedin_username": "ext-person-b", "confirm_send": False},
            )
            return msg.structured_content, inv.structured_content

    msg, inv = asyncio.run(go())
    assert msg["status"] == "duplicate"
    assert inv["status"] in {"duplicate", "already_invited"}, inv
