"""MiViA fork: parsing, ledger and registration tests for the fork tools."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

import pytest
from fastmcp import FastMCP

from linkedin_mcp_server import mivia_outreach as outreach
from linkedin_mcp_server.scraping.contracts import refuse_an_invalid_message
from linkedin_mcp_server.scraping.mivia_network import (
    classify_action,
    event_attendees_url,
    parse_connected_date,
    split_person_lines,
)
from linkedin_mcp_server.tools.mivia import register_mivia_tools


class TestParsing:
    def test_connected_dates(self):
        assert parse_connected_date("Am 28. September 2026 vernetzt") == date(
            2026, 9, 28
        )
        assert parse_connected_date("Am 3. März 2026 vernetzt") == date(2026, 3, 3)
        assert parse_connected_date("Connected on September 28, 2026") == date(
            2026, 9, 28
        )
        assert parse_connected_date("Nachricht") is None

    def test_actions_structural_before_text(self):
        assert (
            classify_action(
                [
                    {
                        "text": "Ausstehend",
                        "key": "ConnectButtonstate:invitation:urn:li:member:1_pending",
                    }
                ]
            )
            == "pending"
        )
        assert (
            classify_action(
                [{"text": "x", "href": "/preload/search-custom-invite/?vanityName=a"}]
            )
            == "connect"
        )
        assert (
            classify_action([{"text": "x", "href": "/messaging/compose/?profileUrn=u"}])
            == "message"
        )
        assert classify_action([{"text": "Folgen"}]) == "follow"
        assert classify_action([]) == "unknown"

    def test_degree_inline_and_own_line(self):
        assert split_person_lines(["Ben Kahle • 2.", "Leiter", "Ort"]) == {
            "name": "Ben Kahle",
            "degree": 2,
            "is_self": False,
            "rest": ["Leiter", "Ort"],
        }
        first = split_person_lines(["Hannelore Reisinger", "• 1.", "Leitung", "Ort"])
        assert (first["degree"], first["rest"]) == (1, ["Leitung", "Ort"])
        assert (
            split_person_lines(["Jessica Schneider", "• Sie", "x"])["is_self"] is True
        )

    def test_event_url(self):
        assert "eventAttending=%5B%227457346711301214208%22%5D" in event_attendees_url(
            "7457346711301214208", 1
        )
        assert event_attendees_url("7457346711301214208", 3).endswith("&page=3")
        with pytest.raises(ValueError):
            event_attendees_url("abc", 1)


class TestMultiLineContract:
    def test_lf_is_allowed(self):
        assert refuse_an_invalid_message("alice", "Hallo\n\nzweite Zeile") is None

    @pytest.mark.parametrize("message", ["a\r\nb", "a\rb", "a\tb", "a\x7fb"])
    def test_other_controls_still_refused(self, message):
        assert (
            refuse_an_invalid_message("alice", message)["status"] == "invalid_message"
        )


class TestLedger:
    def _ledger(self, tmp_path):
        return outreach.Ledger(tmp_path / "ledger.jsonl")

    def test_attempt_without_outcome_blocks_a_resend(self, tmp_path):
        ledger = self._ledger(tmp_path)
        sha = outreach.text_sha("Hallo")
        ledger.append(
            {
                "attempt": "a1",
                "kind": "message",
                "recipient": "bob",
                "text_sha": sha,
                "status": "attempted",
            }
        )
        assert ledger.already_contacted("message", "Bob", sha)
        assert not ledger.already_contacted(
            "message", "bob", outreach.text_sha("Anders")
        )

    def test_not_sent_does_not_block(self, tmp_path):
        ledger = self._ledger(tmp_path)
        sha = outreach.text_sha("Hallo")
        ledger.append(
            {
                "attempt": "a1",
                "kind": "message",
                "recipient": "bob",
                "text_sha": sha,
                "status": "attempted",
            }
        )
        ledger.append({"attempt": "a1", "status": "not_sent"})
        assert ledger.already_contacted("message", "bob", sha) is None

    def test_canary_needs_verified_same_text(self, tmp_path):
        ledger = self._ledger(tmp_path)
        sha = outreach.text_sha("Hallo\nWelt")
        ledger.append(
            {
                "attempt": "c",
                "kind": "message",
                "recipient": "frederikstadler",
                "text_sha": sha,
                "status": "attempted",
            }
        )
        assert not ledger.canary_verified(sha, "frederikstadler")
        ledger.append({"attempt": "c", "status": "verified"})
        assert ledger.canary_verified(sha, "FrederikStadler")
        # Whitespace/newline layout does not change the campaign identity.
        assert outreach.text_sha("Hallo  Welt") == sha

    def test_caps(self, tmp_path):
        ledger = self._ledger(tmp_path)
        now = datetime.now().astimezone()
        for i in range(12):
            ledger.append(
                {
                    "attempt": f"m{i}",
                    "kind": "message",
                    "recipient": f"p{i}",
                    "text_sha": "x",
                    "status": "verified",
                    "started_at": now.isoformat(),
                }
            )
        ledger.append(
            {
                "attempt": "canary",
                "kind": "message",
                "recipient": "frederikstadler",
                "text_sha": "x",
                "status": "verified",
                "started_at": now.isoformat(),
            }
        )
        for i in range(58):
            at = (now - timedelta(days=1 + i % 6)).isoformat()
            ledger.append(
                {
                    "attempt": f"i{i}",
                    "kind": "invite",
                    "recipient": f"q{i}",
                    "status": "sent",
                    "started_at": at,
                }
            )
        q = outreach.quota(
            ledger, messages_per_day=99, invites_per_day=99, canary="frederikstadler"
        )
        assert q["messages_per_day"] == outreach.MESSAGES_PER_DAY_MAX
        assert q["messages_today"] == 12
        assert q["messages_left_today"] == 3
        assert q["invites_left_today"] == 2  # weekly cap 60 binds before the daily 15

    def test_read_back(self):
        conv = {
            "sections": {
                "conversation": "Jessica Schneider\n10:02\nHallo Frederik,\n\nhier der Link:\nhttps://x.y"
            }
        }
        assert outreach.delivered_in_conversation(
            "Hallo Frederik,\nhier der Link:\nhttps://x.y", conv
        )
        assert not outreach.delivered_in_conversation(
            "Hallo Frederik, anderer Text", conv
        )


def test_fork_tools_are_registered_and_tagged():
    mcp = FastMCP("t")
    register_mivia_tools(mcp)
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert names == {
        "list_connections",
        "get_event_attendees",
        "list_sent_invitations",
        "create_post",
        "send_message_verified",
        "send_campaign_batch",
        "connect_guarded",
        "outreach_quota",
    }
    assert all("mivia" in t.tags for t in tools)


def test_readme_fork_block_is_current():
    """A new fork tool must show up in the README summary (scripts/mivia_readme.py)."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    run = subprocess.run(
        [sys.executable, str(root / "scripts" / "mivia_readme.py"), "--check"],
        cwd=root,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert run.returncode == 0, run.stderr
