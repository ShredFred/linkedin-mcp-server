"""ext_saved: card normalisation and the query filter (no browser)."""

from linkedin_mcp_server.linkedin.ext_saved import (
    activity_id,
    matches,
    normalise_cards,
    own_slug,
    split_feed_text,
)


def test_activity_id_accepts_activity_ugc_and_share():
    assert activity_id("urn:li:activity:7123") == "7123"
    assert activity_id("/feed/update/urn:li:ugcPost:99/") == "99"
    assert activity_id("nothing") is None


def test_normalise_dedupes_keeps_order_and_builds_permalink():
    raw = [
        {
            "urn": "urn:li:activity:2",
            "text": "B\n\n\n\nbody",
            "author_url": "/in/x?trk=1",
        },
        {"urn": "urn:li:activity:1", "text": "A"},
        {"urn": "urn:li:activity:2", "text": "dup"},
        {"urn": "junk", "text": "skip"},
    ]
    posts = normalise_cards(raw, 10)
    assert [p["activity_id"] for p in posts] == ["2", "1"]
    assert (
        posts[0]["post_url"]
        == "https://www.linkedin.com/feed/update/urn:li:activity:2/"
    )
    assert posts[0]["author_url"] == "https://www.linkedin.com/in/x"
    assert posts[0]["text"] == "B\n\nbody"
    assert normalise_cards(raw, 1)[0]["activity_id"] == "2"


def test_matches_requires_every_term_case_insensitive():
    post = {"text": "Personalisierte Webseite je Prospect"}
    assert matches(post, None)
    assert matches(post, "webseite PROSPECT")
    assert not matches(post, "webseite voice")


def test_own_slug_from_redirect_target():
    assert (
        own_slug("https://www.linkedin.com/in/some-member-1a2b/") == "some-member-1a2b"
    )
    assert own_slug("https://www.linkedin.com/in/me/") is None
    assert own_slug("https://www.linkedin.com/feed/") is None


def test_split_feed_text_at_card_headings_both_locales():
    text = "Header\nFeed-Beitrag\n\nA likes this\nBody one\nFeed post\nBody two\nFeed-Beitrag\n"
    posts = split_feed_text(text, 10)
    assert [p["text"] for p in posts] == ["A likes this\nBody one", "Body two"]
    assert posts[0]["post_url"] is None
    assert len(split_feed_text(text, 1)) == 1


def test_ugcpost_permalink_keeps_its_urn_type():
    posts = normalise_cards([{"urn": "urn:li:ugcPost:5", "text": "x"}], 5)
    assert posts[0]["post_url"].endswith("/urn:li:ugcPost:5/")
