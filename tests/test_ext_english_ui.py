"""Fork: reads on an English-language account (measured live 2026-10-10).

The fork's word lists were written from German measurements; their English
entries were LinkedIn's documented wording, never seen. These cases are the
English page shapes actually read off a member account switched to English,
next to the German ones already covered in ``test_ext_stage2.py``.
"""

from __future__ import annotations

from linkedin_mcp_server.linkedin.ext_composer_labels import LABELS, words
from linkedin_mcp_server.linkedin.ext_engagement import split_comment_lines
from linkedin_mcp_server.linkedin.ext_events import parse_event_card


def _norm(value: str) -> str:
    return " ".join(value.split()).lower()


class TestEnglishCommentFooter:
    def test_bare_count_line_is_not_text(self):
        c = split_comment_lines(
            [
                "Sam Sample Verified Profile 1st",
                "Sam Sample",
                "• 1st",
                "AI Engineer & Community Builder",
                "2d",
                "Thanks for being part of it!",
                "0",
                "Like",
                "Reply",
            ]
        )
        assert c["text"] == "Thanks for being part of it!"
        assert c["degree"] == 1
        assert c["age"] == "2d"

    def test_reaction_count_line_ends_the_text(self):
        c = split_comment_lines(
            [
                "Sam Sample Verified Profile 1st",
                "Sam Sample",
                "• 1st",
                "Full Stack Engineer",
                "1d",
                "It was lovely to meet you!",
                "Hope we see each other again soon.",
                "1 reaction",
                "1",
                "Like",
                "Reply",
            ]
        )
        assert (
            c["text"]
            == "It was lovely to meet you!\nHope we see each other again soon."
        )

    def test_german_count_line_too(self):
        c = split_comment_lines(
            [
                "X",
                "Sam Sample",
                "• 2.",
                "Leiter Labor",
                "3 Tag(e)",
                "Gern!",
                "12 Reaktionen",
                "Gefällt mir",
                "Antworten",
            ]
        )
        assert c["text"] == "Gern!"

    def test_a_number_inside_the_text_stays(self):
        c = split_comment_lines(
            [
                "X",
                "Sam Sample",
                "• 2.",
                "Leiter Labor",
                "3d",
                "See you in",
                "2027",
                "at the congress.",
                "Like",
                "Reply",
            ]
        )
        assert c["text"] == "See you in\n2027\nat the congress."


class TestGermanCounterRun:
    """German UI, measured 2026-10-11 on the same account."""

    def test_translation_offer_ends_the_comment(self):
        c = split_comment_lines(
            [
                "X",
                "Ridvan Sibic",
                "• 1.",
                "AI Engineer",
                "2 Tag(e)",
                "Thanks for being part of it!",
                "Übersetzung anzeigen",
                "Gefällt mir",
            ]
        )
        assert c["text"] == "Thanks for being part of it!"

    def test_organiser_badge_is_not_the_title(self):
        card = parse_event_card(
            [
                "Organisiert",
                "Do, 7. Aug. 2025, 14:00",
                "Webinar KI im Labor",
                "44 Teilnehmende",
            ]
        )
        assert card["title"] != "Organisiert"


class TestEnglishAdminViewLabels:
    """Admin view of a page post, aria-labels as rendered on 2026-10-10."""

    def test_page_like_matches_the_measured_label(self):
        assert _norm("React Like") in words("page_like")
        # Once reacted the label flips; it must not count as a like target.
        assert _norm("Unreact Like") not in words("page_like")

    def test_identity_switch_matches_the_measured_label(self):
        label = _norm(
            "Open menu for switching identity when interacting with this post"
        )
        assert any(w in label for w in words("switch_identity"))
        assert any(w in _norm("Identitätswechsel") for w in words("switch_identity"))

    def test_new_keys_carry_both_languages(self):
        for key in ("page_like", "switch_identity"):
            assert LABELS[key]["de"] and LABELS[key]["en"]
