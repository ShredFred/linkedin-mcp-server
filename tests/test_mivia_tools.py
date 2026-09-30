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
        for i in range(98):
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
        assert q["messages_left_today"] == 28
        assert q["invites_left_today"] == 2  # weekly cap 100 binds before the daily 25

    def test_cap_values(self):
        # Frederik, 2026-09-29: raised caps. Changing them is a decision, not a refactor.
        assert outreach.INVITES_PER_DAY_DEFAULT == 20
        assert outreach.INVITES_PER_DAY_MAX == 25
        assert outreach.INVITES_PER_WEEK_MAX == 100
        assert outreach.MESSAGES_PER_DAY_DEFAULT == 30
        assert outreach.MESSAGES_PER_DAY_MAX == 40
        assert outreach.PACE_BUDGETS["invite"] == {"day": 25, "week": 100}
        assert outreach.PACE_BUDGETS["message"] == {"day": 40, "week": 200}
        assert outreach.PACE_WRITE_TOTAL_PER_DAY == 150

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
        "get_event_attendee_count",
        "list_sent_invitations",
        "create_post",
        "send_message_verified",
        "send_campaign_batch",
        "connect_guarded",
        "outreach_quota",
        # stage 2
        "get_post_engagers",
        "get_post_analytics",
        "pace_status",
        "invite_to_event",
        "withdraw_invitations",
        "follow_up_list",
        "set_contact_note",
        "get_profile_viewers",
        "comment_on_post",
        "job_watch",
        "list_groups",
        "get_group_members",
        "find_events",
        "search_events",
        "get_company_events",
        "get_page_followers",
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


class TestProjectAttendees:
    RAW = {
        "event_id": "7449450258214014977",
        "start_page": 1,
        "pages_read": 1,
        "next_page": 2,
        "complete": False,
        "attendees": [
            {"name": "Ich", "slug": "me", "degree": 0, "action": "self",
             "headline": "x", "location": "y", "profile_urn": "u0", "page": 1},
            {"name": "A B", "slug": "a-b", "degree": 2, "action": "connect",
             "headline": "Leiter Labor bei X", "location": "Bayern",
             "profile_url": "https://www.linkedin.com/in/a-b/", "profile_urn": "u1", "page": 1},
            {"name": "C D", "slug": "c-d", "degree": 1, "action": "message",
             "headline": "QS", "location": "Wien", "profile_urn": "u2", "page": 1},
        ],
    }

    def test_minimal_keeps_four_fields_and_drops_self(self):
        from linkedin_mcp_server.scraping.mivia_network import project_attendees

        out = project_attendees(self.RAW)
        assert out["readable"] is True and out["count"] == 2
        assert [set(a) for a in out["attendees"]] == [{"slug", "name", "headline", "degree"}] * 2
        assert "profile_urn" not in str(out)

    def test_card_and_limit(self):
        from linkedin_mcp_server.scraping.mivia_network import project_attendees

        out = project_attendees(self.RAW, limit=1, fields="card")
        assert out["count"] == 1 and out["complete"] is False
        assert set(out["attendees"][0]) == {"slug", "name", "headline", "degree", "location", "action", "page"}

    def test_empty_first_page_is_not_readable(self):
        from linkedin_mcp_server.scraping.mivia_network import project_attendees

        out = project_attendees({"event_id": "1", "start_page": 1, "pages_read": 1,
                                 "next_page": None, "complete": True, "attendees": []})
        assert out["readable"] is False and out["reason"].startswith("empty_first_page")
        later = project_attendees({"start_page": 3, "pages_read": 1, "complete": True, "attendees": []})
        assert later["readable"] is True


def test_event_attendees_tool_caps_pages_by_limit(monkeypatch):
    import linkedin_mcp_server.tools.mivia as m

    taken = []
    monkeypatch.setattr(m, "_pace", lambda action, count=1, *, tool: taken.append(count) or {"status": "stop"})
    mcp = FastMCP("t")
    register_mivia_tools(mcp)
    res = asyncio.run(mcp.call_tool("get_event_attendees", {"event_id": "7449450258214014977", "limit": 25}))
    assert taken == [3]
    assert "stop" in str(res)


def test_event_attendee_count_tool_charges_one_page_read(monkeypatch):
    import linkedin_mcp_server.tools.mivia as m

    taken = []
    monkeypatch.setattr(m, "_pace", lambda action, count=1, *, tool: taken.append((action, count)) or {"status": "stop"})
    mcp = FastMCP("t")
    register_mivia_tools(mcp)
    res = asyncio.run(mcp.call_tool("get_event_attendee_count", {"event_id": "https://www.linkedin.com/events/7449450258214014977/"}))
    assert taken == [("page_read", 1)]
    assert "stop" in str(res)


def test_event_attendee_count_waits_for_the_late_attendee_line():
    from linkedin_mcp_server.scraping.mivia_network import EVENT_COUNT_JS, MiviaNetworkReader

    answers = [None, None, 310]
    clock = [0.0]

    class Page:
        async def evaluate(self, js):
            assert js is EVENT_COUNT_JS
            return answers.pop(0)

    class Session:
        page = Page()

        def monotonic(self):
            return clock[0]

        async def delay(self, s):
            clock[0] += s

        async def check_rate_limit(self):
            pass

    class Nav:
        async def _navigate_to_page(self, url):
            assert url == "https://www.linkedin.com/events/7457346711301214208/"

    out = asyncio.run(MiviaNetworkReader(Session(), Nav()).event_attendee_count("7457346711301214208"))
    assert out == {"event_id": "7457346711301214208", "attendee_count": 310}
