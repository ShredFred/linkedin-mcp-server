"""MiViA fork: event finder and page followers -- parsing (lines from live pages, 29.09.2026)."""

from __future__ import annotations

import pytest

from linkedin_mcp_server.scraping.mivia_events import (
    attendee_count,
    event_id,
    keyword_url,
    organiser_url,
    parse_event_card,
    parse_follower_lines,
)


@pytest.mark.parametrize(
    "href,expected",
    [
        ("https://www.linkedin.com/events/7457346711301214208/", "7457346711301214208"),
        (
            "/events/hk-heattreatmentcongress-h-rter7457346711301214208/",
            "7457346711301214208",
        ),
        # Title slug ends in a year: the id is the last 19 digits.
        ("/events/h-rtereikongress20257286622235937701888/", "7286622235937701888"),
        ("/events/7457346711301214208/?trk=x", "7457346711301214208"),
        ("/events/", None),
        ("/company/awt/", None),
    ],
)
def test_event_id(href, expected):
    assert event_id(href) == expected


def test_attendee_count_forms():
    assert (
        attendee_count(["Matthias Steinbacher und 307 weitere Personen nehmen teil"])
        == 308
    )
    assert attendee_count(["Sonja Mueller und 3 weitere Kontakte nehmen teil"]) == 4
    assert attendee_count(["1 Teilnehmer:in"]) == 1
    assert attendee_count(["Der Werkstoff Stahl. Teilnehmer willkommen"]) is None
    assert attendee_count(["Jane and 12 others are attending"]) == 13


def test_search_card():
    card = parse_event_card(
        [
            "Der Werkstoff Stahl und seine Wärmebehandlung",
            "Di., 22. Sep., 08:30 (Ihre Ortszeit)",
            "Dr. Summer Material Technology GmbH, Hellenthalstraße 2, Issum, DE • Von Dr. Sommer Werkstofftechnik GmbH",
            "Sie waren schon einmal bei uns? Nutzen Sie die Chance auf 50% Rabatt !!! Update Ihres Wissens",
            "1 Teilnehmer:in",
        ]
    )
    assert card["title"] == "Der Werkstoff Stahl und seine Wärmebehandlung"
    assert card["date_text"].startswith("Di., 22. Sep.")
    assert card["organiser"] == "Dr. Sommer Werkstofftechnik GmbH"
    assert card["place"].startswith("Dr. Summer Material Technology GmbH")
    assert card["attendees"] == 1 and card["description"].startswith("Sie waren")


def test_section_heading_is_not_the_title():
    card = parse_event_card(
        [
            "Anstehende Events",
            "HK - Heat Treatment Congress - Härtereikongress",
            "Matthias Steinbacher und 307 weitere Personen nehmen teil",
        ]
    )
    assert (
        card["title"] == "HK - Heat Treatment Congress - Härtereikongress"
        and card["attendees"] == 308
    )


def test_urls():
    assert keyword_url("heat treatment").endswith("keywords=heat%20treatment")
    assert keyword_url("x", 2).endswith("&page=2")
    assert organiser_url("ifhtse").endswith("/company/ifhtse/events/?viewAsMember=true")


def test_current_position_single_and_grouped():
    from linkedin_mcp_server.scraping.mivia_events import parse_current_position

    single = {
        "lines": [
            "Laborleiterin",
            "Härtetechnik Hagen GmbH · Vollzeit",
            "Jan. 2024 – Heute · 1 J. 9 Mon.",
            "Hagen",
        ],
        "outer": None,
    }
    assert parse_current_position(single) == {
        "employer": "Härtetechnik Hagen GmbH",
        "role": "Laborleiterin",
    }
    grouped = {
        "lines": ["Head of Quality", "Vollzeit", "März 2025 – Heute"],
        "outer": ["Robert Bosch GmbH", "5 J. 2 Mon.", "Head of Quality"],
    }
    assert parse_current_position(grouped) == {
        "employer": "Robert Bosch GmbH",
        "role": "Head of Quality",
    }
    no_company = {"lines": ["Freelancer", "Jan. 2020 – Heute"], "outer": None}
    assert parse_current_position(no_company) is None
    assert parse_current_position(None) is None


def test_follower_lines():
    f = parse_follower_lines(
        [
            "Anandu Babu",
            "Kontakt 2. Grades",
            "· 2.",
            "Artificial Intelligence | Python",
            "September 2026",
        ]
    )
    assert f == {
        "name": "Anandu Babu",
        "degree": 2,
        "headline": "Artificial Intelligence | Python",
        "followed_month": "2026-09",
    }
    assert parse_follower_lines(["X", "März 2025"])["followed_month"] == "2025-03"


def test_event_summary_drops_person_names_and_extra_fields():
    from linkedin_mcp_server.scraping.mivia_events import event_summary

    lines = [
        "HK Heat Treatment Congress",
        "Di., 20. Okt. 2026",
        "Köln • Von AWT",
        "Matthias Steinbacher und 307 weitere Personen nehmen teil",
    ]
    ev = {
        "event_id": "7457346711301214208",
        **parse_event_card(lines),
        "url": "https://www.linkedin.com/events/7457346711301214208/",
        "found_by": "keyword:HK",
        "past": False,
    }
    s = event_summary(ev)
    assert s["event_id"] == "7457346711301214208"
    assert s["attendees"] == 308
    assert s["attendees_text"] == "308 Teilnehmende"
    assert s["organiser"] == "AWT"
    assert "Steinbacher" not in repr(s)
    assert set(s) == {
        "event_id", "url", "title", "date_text", "organiser",
        "attendees", "attendees_text", "past",
    }