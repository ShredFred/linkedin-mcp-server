"""Mention markup, mention list, plain-text check and the identifier barrier.

All names, slugs and ids are invented placeholders.
"""

from __future__ import annotations

import pytest

from linkedin_mcp_server.linkedin.ext_composer_labels import LABELS, words
from linkedin_mcp_server.linkedin.ext_mentions import (
    Mention,
    apply_mention_list,
    choose_option,
    parse_mentions,
    parse_target,
    plain_text,
    prepare_text,
)

A = "ACoAAPLATZHALTER000000000000000000000001"
B = "ACoAAPLATZHALTER000000000000000000000002"


@pytest.mark.parametrize(
    ("raw", "kind", "slug", "eid"),
    [
        ("max-platzhalter-0a1b2c", "person", "max-platzhalter-0a1b2c", None),
        ("https://www.linkedin.com/in/max-platzhalter-0a1b2c/", "person", "max-platzhalter-0a1b2c", None),
        (f"urn:li:fsd_profile:{A}", "person", None, A),
        (A, "person", None, A),
        ("company:acme-labs", "company", "acme-labs", None),
        ("company:1000001", "company", None, "1000001"),
        ("urn:li:organization:1000001", "company", None, "1000001"),
        ("https://www.linkedin.com/company/acme-labs/", "company", "acme-labs", None),
    ],
)
def test_targets(raw, kind, slug, eid) -> None:
    m = parse_target("X", raw)
    assert (m.kind, m.slug, m.entity_id) == (kind, slug, eid)


@pytest.mark.parametrize("raw", ["urn:li:member:123", "https://example.com/in/x", "a", "school:x"])
def test_bad_targets(raw) -> None:
    with pytest.raises(ValueError):
        parse_target("X", raw)


def test_markup_splits_text_and_mentions() -> None:
    segs, bad = parse_mentions("Danke [[Max Platzhalter|max-p]] und [[Acme Labs|company:acme-labs]]!")
    assert bad is None
    assert [k for k, _ in segs] == ["text", "mention", "text", "mention", "text"]
    assert plain_text(segs) == "Danke Max Platzhalter und Acme Labs!"


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("Hallo @Max", "mention_markup_required"),
        ("Hallo [[Max Platzhalter]]", "mention_syntax_invalid"),
        ("Hallo [[Max|urn:li:member:1]]", "mention_syntax_invalid"),
        ("Hallo [[Max|max-p] ]", "mention_syntax_invalid"),
    ],
)
def test_refusals(text, status) -> None:
    assert parse_mentions(text)[1]["status"] == status


def test_email_is_not_a_mention() -> None:
    segs, bad = parse_mentions("Schreib an team@example.org")
    assert bad is None and plain_text(segs) == "Schreib an team@example.org"


def test_mention_list_marks_the_first_plain_occurrence() -> None:
    out, bad = apply_mention_list(
        "Max Platzhalter hat geprüft. Danke, Max Platzhalter!",
        [{"name": "Max Platzhalter", "target": "max-p"}],
    )
    assert bad is None
    assert out == "[[Max Platzhalter|max-p]] hat geprüft. Danke, Max Platzhalter!"


def test_mention_list_name_missing() -> None:
    _, bad = apply_mention_list("Kein Name hier", [{"name": "Max", "target": "max-p"}])
    assert bad["status"] == "mention_name_not_in_text"


def test_plaintext_check_warn_and_strict() -> None:
    text = "Max Platzhalter hat geprüft. Danke, Max Platzhalter!"
    mentions = [{"name": "Max Platzhalter", "target": "max-p"}]
    _, info, bad = prepare_text(text, mentions, "warn")
    assert bad is None and info["plaintext_names"] == ["Max Platzhalter"]
    _, info, bad = prepare_text(text, mentions, "strict")
    assert bad["status"] == "mention_plaintext_name"
    _, info, bad = prepare_text(text, mentions, "off")
    assert bad is None and "plaintext_names" not in info


def _opt(index, title, entity, kind="person"):
    return {"index": index, "title": title, "subtitle": "", "kind": kind, "entity": entity}


def test_namesakes_pick_the_identifier_even_when_not_first() -> None:
    options = [_opt(0, "Max Platzhalter", A), _opt(1, "Max Platzhalter", B)]
    got = choose_option(options, Mention("Max Platzhalter", "person", entity_id=B))
    assert got == {"status": "ok", "index": 1, "verify_only": False}


def test_barrier_name_alone_never_picks_when_ids_are_shown() -> None:
    # Exactly one namesake, but its identifier is another person: a name
    # match must not be enough. This is the test that turns red when the
    # identifier barrier is removed.
    options = [_opt(0, "Max Platzhalter", A), _opt(1, "Maxi Beispiel", "ACoAAOTHER0000000000")]
    got = choose_option(options, Mention("Max Platzhalter", "person", entity_id=B))
    assert got["status"] == "mention_not_resolved"


def test_namesakes_without_identifier_are_ambiguous() -> None:
    options = [_opt(0, "Max Platzhalter", None), _opt(1, "Max Platzhalter", None)]
    got = choose_option(options, Mention("Max Platzhalter", "person", entity_id=B))
    assert got["status"] == "mention_ambiguous"


def test_single_blind_namesake_needs_read_back() -> None:
    options = [_opt(0, "Maxi Beispiel", None), _opt(1, "Max Platzhalter", None)]
    got = choose_option(options, Mention("Max Platzhalter", "person", entity_id=B))
    assert got == {"status": "ok", "index": 1, "verify_only": True}


def test_identifier_with_another_name_is_refused() -> None:
    options = [_opt(0, "Ganz Anders", B)]
    got = choose_option(options, Mention("Max Platzhalter", "person", entity_id=B))
    assert got["status"] == "mention_name_mismatch"


def test_company_kind_is_respected() -> None:
    options = [_opt(0, "Acme Labs", "1000001", "person")]
    got = choose_option(options, Mention("Acme Labs", "company", entity_id="1000001"))
    assert got["status"] == "mention_not_resolved"


def test_every_label_has_both_languages() -> None:
    for key, entry in LABELS.items():
        assert entry.get("de") and entry.get("en"), key
        assert all(v == v.lower().strip() for v in entry["de"] + entry["en"]), key
    assert "firma" in words("company_hint") and "company" in words("company_hint")


def test_check_media(tmp_path) -> None:
    from linkedin_mcp_server.linkedin.ext_media import check_media

    img = tmp_path / "a.png"
    img.write_bytes(b"x")
    items, bad = check_media([{"path": str(img), "alt_text": "Fläche",
                               "tags": [{"name": "Max", "target": "max-p"}]}])
    assert bad is None and items[0]["tags"][0].slug == "max-p"
    assert check_media([{"path": str(tmp_path / "v.avi")}])[1]["status"] == "media_kind_unmeasured"
    assert check_media([{"path": str(tmp_path / "v.mp4")}])[1]["status"] == "media_invalid_path"
    assert check_media([{"path": str(tmp_path / "x.png")}])[1]["status"] == "media_invalid_path"
    assert check_media([{"path": str(img), "alt_text": "a" * 1001}])[1]["status"] == "alt_text_invalid"
    assert check_media([{"path": str(img), "tags": ["max-p"]}])[1]["status"] == "media_tag_invalid"
    assert check_media([{"path": str(img)}] * 21)[1]["status"] == "media_too_many"
    assert check_media([{"path": str(img)}, {"path": str(img)}])[1]["status"] == "media_invalid"
