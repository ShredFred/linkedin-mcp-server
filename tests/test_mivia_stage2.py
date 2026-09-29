"""MiViA fork stage 2: parsing, pacer, reply detection and local stores.

Card lines and page texts below are copied from the live pages measured on
2026-09-29 (de locale), shortened.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from linkedin_mcp_server import mivia_outreach as outreach
from linkedin_mcp_server.scraping.mivia_actions import (
    group_member_lines,
    parse_group_id,
    sent_age_days,
)
from linkedin_mcp_server.scraping.mivia_engagement import (
    SeenStore,
    parse_activity_id,
    parse_person_ref,
    parse_post_summary,
    reaction_kind,
    split_comment_lines,
    split_engager_lines,
)
from linkedin_mcp_server.scraping.mivia_network import split_person_lines


class TestEngagementParsing:
    def test_activity_id_from_every_form(self):
        aid = "7462869755113254912"
        assert parse_activity_id(aid) == aid
        assert parse_activity_id(f"urn:li:activity:{aid}") == aid
        assert (
            parse_activity_id(
                f"https://www.linkedin.com/feed/update/urn:li:activity:{aid}/"
            )
            == aid
        )
        assert (
            parse_activity_id(
                f"https://www.linkedin.com/posts/mivia_control-activity-{aid}-AbCd"
            )
            == aid
        )
        with pytest.raises(ValueError):
            parse_activity_id("https://www.linkedin.com/in/someone/")

    def test_reaction_kind_icon_before_text(self):
        assert (
            reaction_kind(["Frederik Stadler", "like-consumption-ring-medium"], [])
            == "like"
        )
        assert reaction_kind(["praise-consumption-ring-medium"], []) == "celebrate"
        assert reaction_kind(["interest-small"], []) == "insightful"
        assert reaction_kind([], ["X", "Mit „Gefällt mir“ reagiert"]) == "like"
        assert reaction_kind(["Some Name"], ["X"]) == "unknown"

    def test_engager_lines(self):
        assert split_engager_lines(
            [
                "Ralf Winkels",
                "· 1.",
                "Technischer Vertrieb bei B & C Service GmbH",
                "Mit „Gefällt mir“ reagiert",
            ]
        ) == {
            "name": "Ralf Winkels",
            "degree": 1,
            "headline": "Technischer Vertrieb bei B & C Service GmbH",
        }

    def test_person_ref(self):
        m = parse_person_ref("https://www.linkedin.com/in/ACoAABByTeoBDpbv4Csk")
        assert (m["kind"], m["id_type"]) == ("member", "member_id")
        v = parse_person_ref("/in/konstantinpoeschl/?x=1")
        assert (v["id"], v["id_type"]) == ("konstantinpoeschl", "vanity")
        c = parse_person_ref("https://www.linkedin.com/company/awt-x/")
        assert c["kind"] == "company"

    def test_comment_lines(self):
        lines = [
            "Konstantin Poeschl Verifiziert Profil 1.",
            "Konstantin Poeschl",
            "• 1.",
            "Marketing Ops @ gridX",
            "3 Woche(n)",
            "Kommentar für mehr Reichweite! :-)",
            "Status des Reaktionsbuttons: Keine Reaktion",
            "Gefällt mir",
            "Antworten",
        ]
        c = split_comment_lines(lines)
        assert c == {
            "name": "Konstantin Poeschl",
            "degree": 1,
            "headline": "Marketing Ops @ gridX",
            "age": "3 Woche(n)",
            "text": "Kommentar für mehr Reichweite! :-)",
        }

    def test_post_summary(self):
        text = "\n".join(
            [
                "Auffindbarkeit",
                "1.333",
                "Impressions",
                "Im Netzwerk (Follower:innen und Kontakte)",
                "76 %",
                "Außerhalb des Netzwerks",
                "24 %",
                "782",
                "Erreichte Mitglieder",
                "Profilaktivitäten",
                "7",
                "Mit diesem Beitrag generierte Profilansichten",
                "0",
                "Mit diesem Beitrag gewonnene Follower:innen",
                "Engagement",
                "23",
                "Soziale Interaktionen",
                "Reaktionen",
                "21",
                "Kommentare",
                "1",
                "Reposts",
                "0",
                "Gespeicherte Beiträge",
                "0",
                "Auf LinkedIn gesendet",
                "1",
            ]
        )
        assert parse_post_summary(text) == {
            "impressions": 1333,
            "members_reached": 782,
            "profile_views": 7,
            "followers_gained": 0,
            "social_engagements": 23,
            "reactions": 21,
            "comments": 1,
            "reposts": 0,
            "saves": 0,
            "sends": 1,
            "in_network_pct": 76,
            "outside_network_pct": 24,
        }

    def test_seen_store_only_new(self, tmp_path):
        store = SeenStore(tmp_path / "seen.json")
        assert store.keys("1") == set()
        store.remember("1", {"reaction:a:like"})
        store.remember("1", {"comment:b:9"})
        assert store.keys("1") == {"reaction:a:like", "comment:b:9"}
        assert store.keys("2") == set()


class TestActionParsing:
    @pytest.mark.parametrize(
        "text,days",
        [
            ("Vor 21 Stunden gesendet", 0),
            ("Gestern gesendet", 1),
            ("Vor 4 Tagen gesendet", 4),
            ("Vor 3 Wochen gesendet", 21),
            ("Vor 2 Monaten gesendet", 60),
            ("Sent 1 week ago", 7),
            ("Gesendet", 0),
            (None, None),
            ("irgendwas", None),
        ],
    )
    def test_sent_age(self, text, days):
        assert sent_age_days(text) == days

    def test_group_id(self):
        assert parse_group_id("https://www.linkedin.com/groups/1725997/") == "1725997"
        assert parse_group_id("1725997") == "1725997"
        with pytest.raises(ValueError):
            parse_group_id("metallography")

    def test_group_member_lines(self):
        lines = [
            "Madhuri G.",
            "Kontakt 3. Grades",
            "· 3.",
            "Msc in Chemistry",
            "Nachricht",
        ]
        person = split_person_lines(group_member_lines(lines))
        assert (person["name"], person["degree"], person["rest"][0]) == (
            "Madhuri G.",
            3,
            "Msc in Chemistry",
        )


class TestPacer:
    def test_booked_kinds_count_and_refuse(self, tmp_path):
        pacer = outreach.Pacer(outreach.Ledger(tmp_path / "l.jsonl"))
        budget = outreach.PACE_BUDGETS["comment"]["day"]
        for _ in range(budget):
            pacer.take("comment", tool="t")
        with pytest.raises(outreach.PaceExceeded) as spent:
            pacer.take("comment", tool="t")
        assert spent.value.state["left"] == 0

    def test_message_budget_reads_attempt_rows_and_skips_canary(self, tmp_path):
        ledger = outreach.Ledger(tmp_path / "l.jsonl")
        now = datetime.now().astimezone().isoformat()
        for i, who in enumerate(["a", "b", "frederikstadler"]):
            ledger.append(
                {
                    "attempt": str(i),
                    "kind": "message",
                    "recipient": who,
                    "status": "verified",
                    "started_at": now,
                }
            )
        pacer = outreach.Pacer(ledger)
        assert pacer.state("message")["today"] == 2
        # Checking a ledger kind books nothing extra.
        pacer.take("message", tool="t")
        assert pacer.state("message")["today"] == 2

    def test_write_total_binds(self, tmp_path, monkeypatch):
        monkeypatch.setattr(outreach, "PACE_WRITE_TOTAL_PER_DAY", 3)
        pacer = outreach.Pacer(outreach.Ledger(tmp_path / "l.jsonl"))
        pacer.take("like", 2, tool="t")
        pacer.take("comment", tool="t")
        assert pacer.state("event_invite")["left"] == 0
        # Reads are not visible actions and stay open.
        assert pacer.state("page_read")["left"] > 0

    def test_old_rows_leave_the_week(self, tmp_path):
        ledger = outreach.Ledger(tmp_path / "l.jsonl")
        old = (datetime.now().astimezone() - timedelta(days=8)).isoformat(
            timespec="seconds"
        )
        ledger.path.write_text(
            f'{{"at": "{old}", "kind": "pace", "action": "like", "count": 30}}\n',
            encoding="utf-8",
        )
        assert outreach.Pacer(ledger).state("like")["last_7_days"] == 0

    def test_unknown_action(self, tmp_path):
        with pytest.raises(ValueError):
            outreach.Pacer(outreach.Ledger(tmp_path / "l.jsonl")).state("poke")


THREAD = """Nachrichten
Frederik Stadler
12:07
Sie: Test 4/4 MCP-Fork (Calendly mit UTM) Termin buchen
Liste der Optionen in Ihrer Unterhaltung mit Frederik Stadler und Jessica Schneider öffnen
HEUTE
Profil von Jessica Schneider anzeigen
Jessica Schneider  (she/her)  12:00

Test 1/4 MCP-Fork (einzeilig): Zustelltest, bitte ignorieren.

Profil von Jessica Schneider anzeigen
Jessica Schneider  (she/her)  12:07

Test 4/4 MCP-Fork (Calendly mit UTM)
Termin buchen:
"""


class TestReplies:
    def test_no_reply(self):
        state = outreach.reply_after(
            THREAD, "Test 4/4 MCP-Fork (Calendly mit UTM)\nTermin buchen:"
        )
        assert state == {"found": True, "replied": False, "sender": "Jessica Schneider"}

    def test_reply_after_our_message(self):
        text = (
            THREAD
            + "\nProfil von Frederik Stadler anzeigen\nFrederik Stadler  12:30\n\nDanke, passt!\n"
        )
        state = outreach.reply_after(text, "Test 4/4 MCP-Fork (Calendly mit UTM)")
        assert state["replied"] is True
        assert state["by"] == "Frederik Stadler"
        assert "Danke, passt!" in state["excerpt"]

    def test_earlier_message_counts_later_ones_of_us_not_as_reply(self):
        state = outreach.reply_after(
            THREAD, "Test 1/4 MCP-Fork (einzeilig): Zustelltest"
        )
        assert state["replied"] is False

    def test_message_not_in_thread(self):
        assert outreach.reply_after(THREAD, "Ganz anderer Text")["found"] is False

    def test_last_block_sender(self):
        assert outreach.last_block_sender(THREAD) == "Jessica Schneider"

    def test_text_head(self):
        assert outreach.text_head("\n  Hallo Frau X,\nzweite Zeile") == "Hallo Frau X,"
        assert len(outreach.text_head("x" * 200)) == 80

    def test_sent_messages_latest_state_and_canary(self, tmp_path):
        ledger = outreach.Ledger(tmp_path / "l.jsonl")
        ledger.append(
            {
                "attempt": "1",
                "kind": "message",
                "recipient": "a",
                "status": "attempted",
                "started_at": "2026-09-01T10:00:00+02:00",
            }
        )
        ledger.append({"attempt": "1", "status": "verified"})
        ledger.append(
            {"attempt": "2", "kind": "message", "recipient": "b", "status": "attempted"}
        )
        ledger.append({"attempt": "2", "status": "not_sent"})
        ledger.append(
            {
                "attempt": "3",
                "kind": "message",
                "recipient": "frederikstadler",
                "status": "verified",
                "started_at": "2026-09-02T10:00:00+02:00",
            }
        )
        assert [r["recipient"] for r in outreach.sent_messages(ledger)] == ["a"]
        assert [
            r["recipient"] for r in outreach.sent_messages(ledger, include_canary=True)
        ] == ["frederikstadler", "a"]


class TestContactNotes:
    def test_merge_and_replace(self, tmp_path):
        notes = outreach.ContactNotes(tmp_path / "n.json")
        notes.set("Bob", tags=["HK26", "Härterei"], note=None, replace=False)
        notes.set("bob", tags=["Rückruf"], note="will Demo", replace=False)
        entry = notes.get("BOB")
        assert entry["tags"] == ["HK26", "Härterei", "Rückruf"]
        assert entry["note"] == "will Demo"
        notes.set("bob", tags=["neu"], note=None, replace=True)
        assert notes.get("bob")["tags"] == ["neu"]
        assert "note" not in notes.get("bob")


class TestJobWatch:
    def test_titles_from_references_never_ids(self):
        from linkedin_mcp_server.tools.mivia_stage2 import job_titles

        refs = {
            "search_results": [
                {
                    "kind": "job",
                    "url": "/jobs/view/4472429782/",
                    "text": " Metallograf (m/w/d) ",
                },
                {"kind": "job", "url": "/jobs/view/4460972089/"},
                {"kind": "company", "url": "/company/x/", "text": "Logo"},
            ]
        }
        assert job_titles(refs) == {"4472429782": "Metallograf (m/w/d)"}
        assert job_titles(None) == {}

    @pytest.mark.parametrize(
        "keywords,stem",
        [
            ("Metallograf", "metallogra"),
            ("Werkstoffprüfer", "werkstoffprüf"),
            ("Wärmebehandlung", "wärmebehandl"),
            ("Härterei Leiter", "härterei"),
        ],
    )
    def test_stem(self, keywords, stem):
        from linkedin_mcp_server.tools.mivia_stage2 import keyword_stem

        assert keyword_stem(keywords) == stem


class TestReviewFixes:
    """Regression tests for the review round of 2026-09-29."""

    def test_percent_with_decimal_comma(self):
        out = parse_post_summary("Im Netzwerk\n12,5 %\nAußerhalb des Netzwerks\n87,5 %")
        assert out == {"in_network_pct": 12.5, "outside_network_pct": 87.5}
        assert parse_post_summary("1.400\nImpressionen")["impressions"] == 1400

    def test_idless_engagers_keep_distinct_keys(self):
        from linkedin_mcp_server.scraping.mivia_engagement import engager_key

        a = engager_key("reaction", None, "like", name="Anna A")
        b = engager_key("reaction", None, "like", name="Bert B")
        assert a != b
        assert engager_key("reaction", "x", "like", name="egal") == "reaction:x:like"

    def test_person_name_folding(self):
        assert (
            outreach.person_name("Jessica  Schneider  (she/her)") == "Jessica Schneider"
        )

    def test_reply_ignores_sender_label_variants(self):
        text = (
            THREAD.replace(
                "Profil von Jessica Schneider anzeigen\nJessica Schneider  (she/her)  12:07",
                "Profil von Jessica Schneider anzeigen\nJessica Schneider  (she/her)  12:07",
            )
            + "\nProfil von Jessica Schneider (she/her) anzeigen\nJessica Schneider 12:09\n\nNachtrag\n"
        )
        state = outreach.reply_after(text, "Test 4/4 MCP-Fork (Calendly mit UTM)")
        assert state["replied"] is False

    def test_follow_up_list_keeps_unconfirmed_sends(self, tmp_path):
        ledger = outreach.Ledger(tmp_path / "l.jsonl")
        ledger.append(
            {
                "attempt": "1",
                "kind": "message",
                "recipient": "a",
                "status": "attempted",
                "started_at": "2026-09-01T10:00:00+02:00",
            }
        )
        ledger.append(
            {
                "attempt": "2",
                "kind": "message",
                "recipient": "b",
                "status": "attempted",
                "started_at": "2026-09-02T10:00:00+02:00",
            }
        )
        ledger.append({"attempt": "2", "status": "unknown"})
        assert {r["recipient"] for r in outreach.sent_messages(ledger)} == {"a", "b"}

    def test_pacer_lock_is_released_and_stale_lock_taken_over(self, tmp_path):
        ledger = outreach.Ledger(tmp_path / "l.jsonl")
        pacer = outreach.Pacer(ledger)
        pacer.take("like", tool="t")
        lock = ledger.path.with_suffix(".lock")
        assert not lock.exists()
        lock.write_text("", encoding="utf-8")
        import os
        import time

        old = time.time() - 120
        os.utime(lock, (old, old))
        pacer.take("like", tool="t")
        assert pacer.state("like")["today"] == 2

    def test_pacer_summary_matches_state(self, tmp_path):
        pacer = outreach.Pacer(outreach.Ledger(tmp_path / "l.jsonl"))
        pacer.take("search", 3, tool="t")
        summary = pacer.summary()
        assert summary["actions"]["search"] == pacer.state("search")
        assert summary["writes_today"] == 0

    def test_failed_job_watch_run_does_not_count(self, tmp_path):
        from linkedin_mcp_server.tools.mivia_stage2 import JobWatchStore

        store = JobWatchStore(tmp_path / "jw.json")
        store.record(set(), ran=False)
        assert store.last_run() is None
        store.record({"1"})
        assert store.last_run() is not None and store.seen() == {"1"}
