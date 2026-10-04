"""Haertungsrunde 2: InMail and message edit (2026-10-01)."""

from __future__ import annotations

import pytest

from linkedin_mcp_server import ext_outreach as outreach
from linkedin_mcp_server.linkedin.ext_inmail import credit_refusal
from linkedin_mcp_server.ext_message_checks import check_links, check_outgoing
from linkedin_mcp_server.tools.ext_inmail import (
    precheck_edit,
    precheck_inmail,
    repeat_refusal,
)

GOOD_CAL = "https://calendly.com/acme_jane-doe/30min"


def codes(findings):
    return [f["code"] for f in findings]


# (1) allow_repeat only for the canary
def test_allow_repeat_refused_for_real_person():
    assert repeat_refusal("max-mustermann", True)["status"] == "repeat_not_allowed"


def test_allow_repeat_allowed_for_canary_and_irrelevant_without_flag():
    assert repeat_refusal(outreach.DEFAULT_CANARY, True) is None
    assert repeat_refusal("max-mustermann", False) is None


# (2) edit: length, Cf characters, strict Calendly
def test_edit_too_long_utf16():
    # 4001 emojis = 8002 UTF-16 units, only 4001 code points.
    assert precheck_edit("\U0001f600" * 4001)["status"] == "message_too_long"
    assert precheck_edit("\U0001f600" * 4000) is None


@pytest.mark.parametrize("cf", ["\u2060", "\u00ad", "\u061c", "\u2062", "\u0085"])
def test_edit_hidden_format_char(cf):
    assert precheck_edit(f"Guten Tag{cf} Herr Maier")["status"] == "invalid_message"


def test_edit_keeps_emoji_joiners():
    assert precheck_edit("Team \U0001f468\u200d\U0001f4bb ok") is None


@pytest.mark.parametrize(
    "url",
    [
        "https://calendly.com.evil.example/ext_jane-doe",
        "https://evil.example/?r=calendly.com/acme_jane-doe",
        "https://calendly.com/acme_jane-doe-x/30min",
        "https://calendly.com/acme",
        "https://calendly.com/x/ext_jane-doe",
    ],
)
def test_edit_strict_calendly(url):
    refusal = precheck_edit(f"Termin: {url}")
    assert refusal["status"] == "content_check_failed"
    assert "calendly_wrong_account" in codes(refusal["findings"])


def test_edit_good_calendly_and_bare_www():
    assert precheck_edit(f"Termin: {GOOD_CAL}. Mehr auf www.example.com") is None
    assert (
        precheck_edit("Termin: https://www.calendly.com/acme_jane-doe")
        is None
    )


def test_edit_bare_shortener_and_bare_calendly():
    assert precheck_edit("siehe bit.ly/abc")["status"] == "content_check_failed"
    assert precheck_edit("siehe calendly.com/acme")["status"] == "content_check_failed"


# (3) check_links on host boundaries
@pytest.mark.parametrize(
    "url", ["https://robot.co/x", "https://www.orbit.ly/a", "https://abit.ly.example/a"]
)
def test_shortener_not_on_substring(url):
    assert check_links(url) == []


@pytest.mark.parametrize(
    "url",
    ["https://t.co/x", "https://bit.ly/a", "https://www.bit.ly/a", "https://lnkd.in/b"],
)
def test_shortener_on_host(url):
    assert codes(check_links(url)) == ["link_shortener"]


def test_check_links_calendly_strict():
    assert check_links(GOOD_CAL) == []
    assert codes(check_links("https://x.example/?u=" + GOOD_CAL[8:])) == [
        "calendly_wrong_account"
    ]


# InMail precheck: subject and body
def test_inmail_subject_cf_char():
    assert precheck_inmail("max", "Hallo\u2060", "Text")["status"] == "invalid_subject"


def test_inmail_body_cf_and_bare_shortener():
    assert (
        precheck_inmail("max", "Betreff", "Text\u2060")["status"] == "invalid_message"
    )
    assert (
        precheck_inmail("max", "Betreff", "siehe t.co/x")["status"]
        == "content_check_failed"
    )


def test_inmail_subject_link_and_placeholder():
    assert (
        precheck_inmail("max", "Termin calendly.com/acme", "Text")["status"]
        == "content_check_failed"
    )
    assert precheck_inmail("max", "Hallo {vorname}", "Text")["status"] == (
        "content_check_failed"
    )


def test_inmail_ok():
    assert precheck_inmail("max", "Gefügeanalyse", f"Termin: {GOOD_CAL}") is None


# Credits
@pytest.mark.parametrize(
    "credits,status",
    [
        ({"free": True, "cost": None}, "open_profile"),
        ({"none_left": True, "cost": 1, "remaining": 0}, "no_inmail_credits"),
        ({"cost": None, "remaining": 5}, "not_an_inmail_composer"),
        ({"cost": 2, "remaining": 150}, "unexpected_inmail_cost"),
        ({"cost": 1, "remaining": 0}, "no_inmail_credits"),
        ({"cost": 1, "remaining": 150}, None),
        ({"cost": 1, "remaining": None}, None),
    ],
)
def test_credit_refusal(credits, status):
    assert credit_refusal(credits) == status


def test_check_outgoing_http_refused():
    assert check_outgoing("http://example.com", max_utf16=100)["status"] == (
        "content_check_failed"
    )
