"""Fork extension: event finder and page followers -- parsing (lines from live pages, 29.09.2026)."""

from __future__ import annotations

import pytest

from linkedin_mcp_server.linkedin.ext_events import (
    _DATE_RE,
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
        ("/events/fachkongress20257286622235937701888/", "7286622235937701888"),
        ("/events/7457346711301214208/?trk=x", "7457346711301214208"),
        ("/events/", None),
        ("/company/example-assoc/", None),
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
            "Example Materials GmbH, Musterstraße 1, Musterstadt, DE • Von Beispiel Werkstoff GmbH",
            "Sie waren schon einmal bei uns? Nutzen Sie die Chance auf 50% Rabatt !!! Update Ihres Wissens",
            "1 Teilnehmer:in",
        ]
    )
    assert card["title"] == "Der Werkstoff Stahl und seine Wärmebehandlung"
    assert card["date_text"].startswith("Di., 22. Sep.")
    assert card["organiser"] == "Beispiel Werkstoff GmbH"
    assert card["place"].startswith("Example Materials GmbH")
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
    from linkedin_mcp_server.linkedin.ext_events import parse_current_position

    single = {
        "lines": [
            "Laborleiterin",
            "Beispiel Härtetechnik GmbH · Vollzeit",
            "Jan. 2024 – Heute · 1 J. 9 Mon.",
            "Hagen",
        ],
        "outer": None,
    }
    assert parse_current_position(single) == {
        "employer": "Beispiel Härtetechnik GmbH",
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
    # Live layout 2026-09-29 (entity-collection-item): no spaces around the dash.
    live = {
        "lines": [
            "Student Assistant",
            "Beispiel GmbH · Werkstudium",
            "Apr. 2024–Heute · 2 Jahre 6 Monate",
            "Musterstadt, Deutschland · Vor Ort",
        ],
        "outer": None,
    }
    assert parse_current_position(live) == {
        "employer": "Beispiel GmbH",
        "role": "Student Assistant",
    }
    no_company = {"lines": ["Freelancer", "Jan. 2020 – Heute"], "outer": None}
    assert parse_current_position(no_company) is None
    assert parse_current_position(None) is None


def test_follower_lines():
    f = parse_follower_lines(
        [
            "Alex Example",
            "Kontakt 2. Grades",
            "· 2.",
            "Artificial Intelligence | Python",
            "September 2026",
        ]
    )
    assert f == {
        "name": "Alex Example",
        "degree": 2,
        "headline": "Artificial Intelligence | Python",
        "followed_month": "2026-09",
    }
    assert parse_follower_lines(["X", "März 2025"])["followed_month"] == "2025-03"


def test_event_summary_drops_person_names_and_extra_fields():
    from linkedin_mcp_server.linkedin.ext_events import event_summary

    lines = [
        "HK Heat Treatment Congress",
        "Di., 20. Okt. 2026",
        "Köln • Von Beispielverband",
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
    assert s["organiser"] == "Beispielverband"
    assert "Steinbacher" not in repr(s)
    assert set(s) == {
        "event_id",
        "url",
        "title",
        "date_text",
        "place",
        "is_online",
        "country",
        "organiser",
        "attendees",
        "attendees_text",
        "past",
    }


def test_parse_event_card_skips_result_counter():
    card = parse_event_card(
        ["1 Ergebnis", "Fachkongress 2026", "Di., 13. Okt. bis Do., 15. Okt.", "Köln • Von Beispielverband"]
    )
    assert card["title"] == "Fachkongress 2026"
    assert card["date_text"].startswith("Di.")


def test_place_facts_from_cards():
    from linkedin_mcp_server.linkedin.ext_events import event_summary, place_facts

    card = parse_event_card(
        [
            "Der Werkstoff Stahl",
            "Di., 22. Sep., 08:30 (Ihre Ortszeit)",
            "Example Materials GmbH, Musterstraße 1, Musterstadt, DE • Von Beispiel",
        ]
    )
    assert card["country"] == "DE" and card["is_online"] is False
    s = event_summary({**card, "event_id": "1"})
    assert s["place"].endswith("Musterstadt, DE") and s["country"] == "DE"
    online = parse_event_card(["Webinar Härten", "Mi., 4. Nov.", "Online • Von Beispielverband"])
    assert online["place"] == "Online" and online["is_online"] is True
    assert online["country"] is None
    alone = parse_event_card(["Webinar", "Mi., 4. Nov.", "Online-Event"])
    assert alone["is_online"] is True
    # A city alone is not mapped; no place stays unknown.
    assert place_facts("Köln") == {"is_online": False, "country": None}
    assert place_facts("Messe Wien, Österreich")["country"] == "AT"
    assert place_facts(None) == {"is_online": None, "country": None}
    none = parse_event_card(["Fachkongress 2026", "Matthias und 3 weitere Personen nehmen teil"])
    assert none["place"] is None and none["is_online"] is None


def _card_fixtures():
    import json
    from pathlib import Path

    p = Path(__file__).parent / "fixtures" / "linkedin" / "ext_event_cards.json"
    return json.loads(p.read_text(encoding="utf-8"))


@pytest.mark.parametrize("fx", _card_fixtures())
def test_place_from_stored_cards(fx):
    lines = fx["lines"]
    card = parse_event_card(lines)
    if _DATE_RE.match(card["title"] or ""):  # organiser-page order, as by_organiser
        card = parse_event_card(lines[1:])
    assert (card["place"], card["is_online"], card["country"]) == (
        fx["place"],
        fx["is_online"],
        fx["country"],
    )
