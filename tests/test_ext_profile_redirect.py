"""Fork extension: LinkedIn's 2026-09-30 profile redirect to ?isSelfProfile=false.

send_message returned recipient_resolution_failed for every
recipient because the top-card page URL now carries this query.
"""

import pytest

from linkedin_mcp_server.linkedin import message_sender as ms


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://www.linkedin.com/in/johnsmith/?isSelfProfile=false",
            "/in/johnsmith/",
        ),
        ("https://www.linkedin.com/in/johnsmith/", "/in/johnsmith/"),
        ("https://www.linkedin.com/in/johnsmith/?isSelfProfile=true", None),
        (
            "https://www.linkedin.com/in/johnsmith/?isSelfProfile=false&trk=x",
            "/in/johnsmith/",
        ),
        (
            "https://www.linkedin.com/in/johnsmith/?trk=profile",
            "/in/johnsmith/",
        ),
        ("https://www.linkedin.com/in/johnsmith/?miniProfileUrn=x", None),
        ("https://www.linkedin.com/in/johnsmith/edit/?isSelfProfile=false", None),
    ],
)
def test_profile_path_accepts_the_redirect_marker_and_benign_tracking(url, expected):
    assert ms._profile_path_from_url(url) == expected


def test_current_compose_href_resolves_one_recipient():
    # Top card href as observed on 2026-09-30 (identifier replaced).
    href = (
        "/messaging/compose/?profileUrn=urn%3Ali%3Afsd_profile%3AACoAAtest123"
        "&recipient=ACoAAtest123&screenContext=NON_SELF_PROFILE_VIEW&interop=msgOverlay"
    )
    assert (
        ms._profile_urn_from_compose_url(
            href,
            base="https://www.linkedin.com/in/johnsmith/?isSelfProfile=false",
        )
        == "ACoAAtest123"
    )
