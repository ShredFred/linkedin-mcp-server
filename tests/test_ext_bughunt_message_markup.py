"""Mention markup never leaves through a message path (bughunt 2026-10-11).

Only send_message refused it; campaign, InMail, message edit and the
invitation note let ``[[Name|slug]]`` through as literal brackets.
"""

from value_assertions import require_value

from linkedin_mcp_server.ext_message_checks import check_outgoing
from linkedin_mcp_server.tools.ext import check_invite_note
from linkedin_mcp_server.tools.ext_inmail import precheck_edit, precheck_inmail

TEXT = "Hallo Erika, siehe [[Acme Labs|company:acme-labs]] danke"


def test_check_outgoing_refuses_markup() -> None:
    assert (
        require_value(check_outgoing(TEXT, max_utf16=8000))["status"]
        == "mention_not_supported_in_messages"
    )


def test_broken_markup_is_refused_too() -> None:
    assert (
        require_value(check_outgoing("siehe [[Acme Labs", max_utf16=8000))["status"]
        == "mention_not_supported_in_messages"
    )


def test_message_edit_refuses_markup() -> None:
    assert (
        require_value(precheck_edit(TEXT))["status"]
        == "mention_not_supported_in_messages"
    )


def test_inmail_refuses_markup() -> None:
    got = precheck_inmail("erika-platzhalter", "Betreff", TEXT)
    assert got and got["status"] == "mention_not_supported_in_messages", got


def test_invite_note_refuses_markup() -> None:
    assert (
        require_value(check_invite_note(TEXT))["status"]
        == "mention_not_supported_in_messages"
    )


def test_plain_text_still_passes() -> None:
    assert check_outgoing("Hallo Erika, siehe Acme Labs, danke", max_utf16=8000) is None
    assert check_invite_note("Hallo Erika, gern vernetzen.") is None
