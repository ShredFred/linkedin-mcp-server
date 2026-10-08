"""fork R5: property fuzzing of the browser-free checks and parsers.

hypothesis is not installed in this project, so every property draws a fixed
number of cases from ``random.Random`` with a fixed seed: reproducible, and a
failure names the seed and the case. Each property states what may be raised
(the documented error kind) and what must hold for every input.

Known defects are pinned as ``xfail(strict=True)`` with the reason: they turn
red (XPASS) the moment a fix lands, so the marker has to be removed then.
"""

from __future__ import annotations

import json
import random
import string

import pytest

from linkedin_mcp_server import ext_outreach as outreach
from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.linkedin.contracts import refuse_an_invalid_message
from linkedin_mcp_server.linkedin.identifiers import (
    landed_identity_mismatch,
    normalize_person_identifier,
)
from linkedin_mcp_server.linkedin.ext_engagement import parse_activity_id
from linkedin_mcp_server.linkedin.ext_own_content import parse_comment_ref
from linkedin_mcp_server.ext_message_checks import (
    MESSAGE_MAX_UTF16,
    bare_links_as_https,
    check_links,
    check_outgoing,
    hidden_format_char,
    utf16_len,
)

CASES = 5000
SEED = 20261001

# Characters a pasted text realistically carries, plus the dangerous ones.
_PLAIN = string.ascii_letters + string.digits + " .,;:!?-_/()[]{}<>%@#'\"\n\t"
_UNICODE = "äöüÄÖÜßéèçñ€–—…“”„‚ ‍‌😀👍🏽🇩🇪"
_FORBIDDEN = [
    "‮",  # RLO
    "‪",  # LRE
    "⁦",  # LRI
    "​",  # ZWSP (Cf)
    "⁠",  # word joiner (Cf)
    "﻿",  # BOM / ZWNBSP (Cf)
    "­",  # soft hyphen (Cf)
    "\x7f",
    "\x85",
    "\x9b",
    " ",
    " ",
]
_FRAGMENTS = [
    "https://",
    "http://",
    "www.",
    "calendly.com/",
    "ext_jane-doe",
    "bit.ly/",
    "t.co/",
    "lnkd.in/",
    "{name}",
    "[Vorname]",
    "TODO",
    "XXX",
    ".com/",
    "linkedin.com/in/",
]


def _text(rng: random.Random, max_len: int = 60) -> str:
    out = []
    for _ in range(rng.randint(0, max_len)):
        pick = rng.random()
        if pick < 0.70:
            out.append(rng.choice(_PLAIN))
        elif pick < 0.88:
            out.append(rng.choice(_UNICODE))
        else:
            out.append(rng.choice(_FRAGMENTS))
    return "".join(out)


def _insert(rng: random.Random, text: str, piece: str) -> str:
    at = rng.randint(0, len(text))
    return text[:at] + piece + text[at:]


# --- check_outgoing / check_links / hidden_format_char / utf16_len ----------


def test_check_outgoing_never_raises_and_returns_known_status():
    rng = random.Random(SEED)
    for case in range(CASES):
        text = _text(rng)
        if rng.random() < 0.2:
            text = _insert(rng, text, rng.choice(_FORBIDDEN))
        result = check_outgoing(text, max_utf16=MESSAGE_MAX_UTF16)
        assert result is None or result["status"] in {
            "invalid_message",
            "message_too_long",
            "content_check_failed",
        }, (case, text, result)


def test_forbidden_character_is_refused_wherever_it_stands():
    """No loosening: a hidden/bidi character is refused at any position and
    whatever else the text holds (links, placeholders, an overlong body)."""
    rng = random.Random(SEED + 1)
    for case in range(CASES):
        bad = rng.choice(_FORBIDDEN)
        text = _insert(rng, _text(rng), bad)
        if rng.random() < 0.05:
            text = text + "x" * MESSAGE_MAX_UTF16  # length must not mask it
        assert hidden_format_char(bad), bad
        result = check_outgoing(text, max_utf16=MESSAGE_MAX_UTF16)
        assert result is not None and result["status"] == "invalid_message", (
            case,
            repr(text),
            result,
        )


def test_allowed_joiners_alone_never_trip_the_hidden_check():
    rng = random.Random(SEED + 2)
    for _ in range(CASES):
        text = "".join(rng.choice("abc ‍‌👍🏽") for _ in range(rng.randint(0, 20)))
        assert not any(hidden_format_char(c) for c in text), repr(text)


@pytest.mark.parametrize(
    "separator", [" ", "\n", "\t", "(", ": ", ", ", "„", " ", "　"]
)
def test_shortener_after_a_separator_is_always_refused(separator):
    rng = random.Random(SEED + 3)
    for case in range(CASES // 10):
        link = rng.choice(["https://bit.ly/", "bit.ly/", "https://t.co/", "lnkd.in/"])
        link += "".join(rng.choice(string.ascii_letters) for _ in range(6))
        head = "".join(rng.choice(string.ascii_letters + " ") for _ in range(10))
        text = head + separator + link + rng.choice(["", ".", " danke", ")"])
        result = check_outgoing(text, max_utf16=MESSAGE_MAX_UTF16)
        assert result and result["status"] == "content_check_failed", (
            case,
            repr(text),
        )


@pytest.mark.parametrize("text", ["Siehexhttps://bit.ly/abc", "Siehe_bit.ly/abc"])
def test_shortener_glued_to_a_word_is_refused(text):
    result = check_outgoing(text, max_utf16=MESSAGE_MAX_UTF16)
    assert result and result["status"] == "content_check_failed"


def test_wrong_calendly_account_is_refused_wherever_it_stands():
    rng = random.Random(SEED + 4)
    for case in range(CASES):
        bad = rng.choice(
            [
                "https://calendly.com/acme/30min",
                "https://calendly.com.evil.example/ext_jane-doe",
                "calendly.com/acme/x",
                "https://evil.example/?r=calendly.com/acme_jane-doe",
            ]
        )
        sep = rng.choice([" ", "\n", "(", ": "])
        text = _text(rng).replace("‮", "") + sep + bad + rng.choice(["", " ok"])
        text = "".join(c for c in text if not hidden_format_char(c))
        result = check_outgoing(text, max_utf16=MESSAGE_MAX_UTF16)
        assert result and result["status"] == "content_check_failed", (
            case,
            repr(text),
        )


def test_link_checks_are_deterministic_and_rewrite_is_idempotent():
    rng = random.Random(SEED + 5)
    for case in range(CASES):
        text = _text(rng)
        once = bare_links_as_https(text)
        assert bare_links_as_https(once) == once, (case, repr(text))
        assert check_links(once) == check_links(once)
        assert check_outgoing(text, max_utf16=MESSAGE_MAX_UTF16) == check_outgoing(
            text, max_utf16=MESSAGE_MAX_UTF16
        )


def test_utf16_len_matches_the_encoding_and_is_additive():
    rng = random.Random(SEED + 6)
    for _ in range(CASES):
        a, b = _text(rng), _text(rng)
        assert utf16_len(a) == len(a.encode("utf-16-le")) // 2
        assert utf16_len(a) >= len(a)
        assert utf16_len(a + b) == utf16_len(a) + utf16_len(b)
    assert utf16_len(None) == 0  # type: ignore[arg-type]


def test_length_limit_is_exact_in_utf16_units():
    assert check_outgoing("😀" * 4000, max_utf16=MESSAGE_MAX_UTF16) is None
    assert check_outgoing("😀" * 4000 + "a", max_utf16=MESSAGE_MAX_UTF16) == {
        "status": "message_too_long",
        "max": MESSAGE_MAX_UTF16,
    }


def test_lone_surrogate_is_refused_not_raised():
    result = check_outgoing("Hallo \ud83d Welt", max_utf16=MESSAGE_MAX_UTF16)
    assert result is not None and result["status"] == "invalid_message"


# --- contracts.refuse_an_invalid_message ------------------------------------


def test_refuse_invalid_message_refuses_every_control_but_lf():
    rng = random.Random(SEED + 7)
    controls = [chr(c) for c in range(32) if c != 10] + ["\x7f", "\x85", "‮"]
    for case in range(CASES):
        text = _text(rng)
        if not text.strip():
            assert refuse_an_invalid_message("dieter", text) is not None
            continue
        clean = "".join(c for c in text if c not in controls and c != "\t")
        clean = "".join(c for c in clean if not (0x2066 <= ord(c) <= 0x2069 or c == "‪"))
        if clean.strip():
            got = refuse_an_invalid_message("dieter", clean)
            invisible = any(c in "  ​⁠﻿­" for c in clean)
            if got is None:
                assert "\r" not in clean and "\t" not in clean
            elif not invisible:
                pytest.fail(f"clean text refused: case {case} {clean!r} -> {got}")
        dirty = _insert(rng, clean or "x", rng.choice(controls))
        assert refuse_an_invalid_message("dieter", dirty) is not None, (
            case,
            repr(dirty),
        )


# --- Ledger.rows -------------------------------------------------------------


def _row(rng: random.Random, n: int) -> dict:
    return {
        "attempt": f"a{n}",
        "kind": "message",
        "recipient": rng.choice(["anna", "bert", "jürgen", "zoë"]),
        "text_head": rng.choice(["Guten Tag Frau Köhler,", "Hallo 👍", "Hi"]),
        "status": rng.choice(["attempted", "verified", "not_sent"]),
        "started_at": "2026-10-01T08:00:00+02:00",
    }


def test_ledger_rows_survive_line_endings_blank_lines_and_a_torn_tail(tmp_path):
    """Mixed CRLF/LF, blank lines and a final row cut at any *character*:
    every complete row is read, the torn tail is skipped, nothing raises."""
    rng = random.Random(SEED + 8)
    path = tmp_path / "ledger.jsonl"
    for case in range(CASES // 5):
        rows = [_row(rng, i) for i in range(rng.randint(1, 6))]
        parts = []
        for row in rows:
            parts.append(json.dumps(row, ensure_ascii=False))
            parts.append(rng.choice(["\n", "\r\n", "\n\n", "\r\n\r\n", "\n  \n"]))
        torn = json.dumps(_row(rng, 99), ensure_ascii=False)
        cut = torn[: rng.randint(1, len(torn) - 1)]
        path.write_bytes(("".join(parts) + cut).encode("utf-8"))
        got = outreach.Ledger(path).rows()
        assert [r["attempt"] for r in got] == [r["attempt"] for r in rows], (
            case,
            cut,
        )


def test_ledger_mid_file_torn_row_sealed_by_append_is_skipped(tmp_path):
    rng = random.Random(SEED + 9)
    path = tmp_path / "ledger.jsonl"
    for case in range(CASES // 10):
        path.unlink(missing_ok=True)
        ledger = outreach.Ledger(path)
        ledger.append({"attempt": "x0", "kind": "message"})
        torn = json.dumps(_row(rng, 1), ensure_ascii=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(torn[: rng.randint(1, len(torn) - 1)])
        ledger.append({"attempt": "x2", "kind": "message"})
        assert [r["attempt"] for r in ledger.rows()] == ["x0", "x2"], case


def test_ledger_corruption_raises_only_ledger_corrupt(tmp_path):
    rng = random.Random(SEED + 10)
    path = tmp_path / "ledger.jsonl"
    garbage = ["null", "[1,2]", '"x"', "}{", "not json", '{"a":1}}', "1"]
    for case in range(CASES // 5):
        lines = [json.dumps({"attempt": f"a{i}"}) for i in range(3)]
        lines.insert(rng.randint(0, 2), rng.choice(garbage))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with pytest.raises(outreach.LedgerCorrupt):
            outreach.Ledger(path).rows()


def test_ledger_torn_inside_a_multibyte_character_is_skipped(tmp_path):
    path = tmp_path / "ledger.jsonl"
    good = json.dumps({"attempt": "a", "text_head": "Köhler"}, ensure_ascii=False)
    torn = json.dumps({"attempt": "b", "text_head": "Köhler"}, ensure_ascii=False)
    raw = torn.encode("utf-8")
    cut = raw[: raw.index("ö".encode()) + 1]  # half of the 'ö'
    path.write_bytes(good.encode("utf-8") + b"\n" + cut)
    assert [r["attempt"] for r in outreach.Ledger(path).rows()] == ["a"]


def test_ledger_with_utf8_bom_is_readable(tmp_path):
    path = tmp_path / "ledger.jsonl"
    body = json.dumps({"attempt": "a"}) + "\n" + json.dumps({"attempt": "b"}) + "\n"
    path.write_bytes(b"\xef\xbb\xbf" + body.encode())
    assert [r["attempt"] for r in outreach.Ledger(path).rows()] == ["a", "b"]


def test_ledger_row_with_unhashable_attempt_is_ledger_corrupt(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text('{"attempt": ["x"], "kind": "message"}\n', encoding="utf-8")
    with pytest.raises(outreach.LedgerCorrupt):
        outreach.Pacer(outreach.Ledger(path)).state("message")


@pytest.mark.parametrize("stamp", ["0001-01-01T00:00:00", "1969-12-31T23:00:00"])
def test_row_time_with_extreme_naive_date_does_not_raise(stamp):
    outreach.row_time({"at": stamp})


# --- parse_comment_ref / parse_activity_id ----------------------------------


def _digits(rng: random.Random, n: int) -> str:
    return str(rng.randint(1, 9)) + "".join(
        rng.choice("0123456789") for _ in range(n - 1)
    )


def test_parse_activity_id_only_value_error_and_id_is_from_input():
    rng = random.Random(SEED + 11)
    shapes = [
        "urn:li:activity:{}",
        "https://www.linkedin.com/feed/update/urn:li:activity:{}/",
        "https://www.linkedin.com/posts/x-y_activity-{}-abcd",
        "{}",
        " {} ",
        "activity:{} activity:{}",
    ]
    for case in range(CASES):
        ident = _digits(rng, rng.randint(14, 22))
        noise = "".join(c for c in _text(rng, 10) if not c.isdigit())
        text = rng.choice(shapes + ["{}" + noise]).replace("{}", ident)
        if rng.random() < 0.3:
            # Digit-free noise: an adjacent digit is the known truncation
            # defect pinned separately below.
            text = _insert(
                rng, text, "".join(c for c in _text(rng, 8) if not c.isdigit())
            )
        try:
            got = parse_activity_id(text)
        except ValueError:
            continue
        assert got.isdigit() and 16 <= len(got) <= 22, (case, text, got)
        assert got in text, (case, text, got)
        assert parse_activity_id(got) == got  # idempotent


def test_parse_activity_id_does_not_truncate_an_overlong_id():
    with pytest.raises(ValueError):
        parse_activity_id("urn:li:activity:12345678901234567890123")


def test_parse_comment_ref_only_value_error_and_ids_from_input():
    rng = random.Random(SEED + 12)
    for case in range(CASES):
        act, cid = _digits(rng, 19), _digits(rng, rng.randint(10, 22))
        text = rng.choice(
            [
                f"urn:li:comment:(activity:{act},{cid})",
                f"urn:li:comment:(urn:li:activity:{act}, {cid})",
                f"https://www.linkedin.com/feed/update/urn:li:activity:{act}/?commentUrn=urn%3Ali%3Acomment%3A%28activity%3A{act}%2C{cid}%29",
                cid,
                _text(rng, 30),
                f"urn:li:comment:(activity:{act},{cid}) urn:li:comment:(activity:{act},{_digits(rng, 12)})",
            ]
        )
        if rng.random() < 0.2:
            text = _insert(rng, text, rng.choice(["%", "%25", "(", ")", ","]))
        try:
            activity, comment = parse_comment_ref(text)
        except ValueError:
            continue
        assert comment.isdigit() and 10 <= len(comment) <= 22, (case, text)
        assert activity is None or (activity.isdigit() and len(activity) >= 16)
        # Re-parsing the canonical form gives the same pair.
        if activity:
            again = parse_comment_ref(f"urn:li:comment:(activity:{activity},{comment})")
            assert again == (activity, comment)


# --- identifiers -------------------------------------------------------------


def test_normalize_person_identifier_raises_only_invalid_reference_and_is_idempotent():
    rng = random.Random(SEED + 13)
    slugs = ["test-u", "jürgen-müller-12a", "ACoAAB12", "me", "x", "a.b"]
    for case in range(CASES):
        base = rng.choice(slugs + [_text(rng, 15)])
        text = rng.choice(
            [
                "{}",
                "https://www.linkedin.com/in/{}/",
                "linkedin.com/in/{}",
                "/in/{}/",
                "https://de.linkedin.com/in/{}?trk=x",
                "https://www.linkedin.com/company/{}/",
                "  {}  ",
            ]
        ).format(base)
        try:
            once = normalize_person_identifier(text)
        except InvalidReferenceError:
            continue
        assert once and once == once.strip(), (case, text, once)
        assert normalize_person_identifier(once) == once, (case, text, once)
        assert (
            normalize_person_identifier(f"https://www.linkedin.com/in/{once}/") == once
        ), (case, text, once)


def test_landed_identity_mismatch_contract():
    rng = random.Random(SEED + 14)
    for case in range(CASES):
        slug = "".join(rng.choice(string.ascii_lowercase + "-") for _ in range(8))
        kind = rng.choice(["in", "company"])
        same = f"https://www.linkedin.com/{kind}/{slug.upper()}/"
        assert landed_identity_mismatch(same, kind, slug) is None, (case, slug)
        other = f"https://www.linkedin.com/{kind}/{slug}x/"
        note = landed_identity_mismatch(other, kind, slug)
        assert note and note["landed"] == slug + "x", (case, slug)
        # Without brackets: an unbalanced one is the known defect pinned below.
        junk = rng.choice(
            [None, "", 0, _text(rng, 20).replace("[", "").replace("]", "")]
        )
        result = landed_identity_mismatch(junk, kind, slug)
        assert result is None or set(result) == {"requested", "landed", "landed_url"}


def test_landed_identity_mismatch_unparsable_url_is_not_raised():
    assert landed_identity_mismatch("https://[broken/in/x/", "in", "x") is None
