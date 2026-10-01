"""MiViA fork R6: fuzzing of the projections from raw page data.

Every function here receives what a ``page.evaluate`` call, an ``innerText``
or an ``href`` produced. Page data is not typed: a key may be missing, a value
may be null, a list may hold a null. The properties:

* no exception other than the documented one,
* never ``complete: true`` when an entry was unusable,
* never a person without an id handed on as known or matched,
* one broken entry is counted, the rest of the list survives.

hypothesis is not installed: cases come from ``random.Random`` with a fixed
seed. Defects in files owned by other lanes are pinned as
``xfail(strict=True)``.
"""

from __future__ import annotations

import asyncio
import random

from linkedin_mcp_server import mivia_daily
from linkedin_mcp_server.linkedin import mivia_actions as A
from linkedin_mcp_server.linkedin import mivia_engagement as E
from linkedin_mcp_server.linkedin import mivia_inmail as I
from linkedin_mcp_server.linkedin import mivia_network as N
from linkedin_mcp_server.linkedin import mivia_own_content as O

CASES = 2000
SEED = 20261002

_STRINGS = [
    "",
    "x",
    "‮​",
    "😀😀😀",
    "• 2.",
    "• Sie",
    "Ben Kahle • 1.",
    "Am 3. Mai 2026 vernetzt",
    "Connected on May 3, 2026",
    "Vor 3 Wochen gesendet",
    "custom-invite",
    "/messaging/compose",
    "Nachricht",
    "a" * 5000,
]


def _value(rng: random.Random):
    """One JSON value as page JS may return it."""
    roll = rng.random()
    if roll < 0.5:
        return rng.choice(_STRINGS)
    return rng.choice([None, 0, 7, -1, 2.5, True, False, [], {}, ["x", None]])


def _record(rng: random.Random, keys: list[str]) -> dict:
    return {k: _value(rng) for k in keys if rng.random() < 0.8}


def _items(rng: random.Random, keys: list[str], n: int = 8) -> list:
    return [
        rng.choice([_record(rng, keys), _record(rng, keys), None, "x", 3])
        for _ in range(rng.randrange(n))
    ]


def _cases(name: str):
    rng = random.Random(f"{SEED}:{name}")
    for _ in range(CASES):
        yield rng


# -- pure projections ------------------------------------------------------------


def test_scalar_parsers_never_raise_on_page_values():
    for rng in _cases("scalar"):
        v = _value(rng)
        assert N.parse_connected_date(v) is None or hasattr(
            N.parse_connected_date(v), "year"
        )
        assert A.sent_age_days(v) is None or isinstance(A.sent_age_days(v), int)
        assert isinstance(mivia_daily.post_head(v), str)
        assert O.slug_of(v) is None or isinstance(O.slug_of(v), str)
        assert O.text_matches(v, "x") in (True, False)
        assert O.prefill_matches("x", v) in (True, False)


def test_classify_action_tolerates_null_fields_and_entries():
    for rng in _cases("actions"):
        actions = rng.choice([None, "x", _items(rng, ["key", "href", "text"])])
        assert N.classify_action(actions) in {
            "message",
            "connect",
            "pending",
            "follow",
            "unknown",
        }
    # one broken entry does not hide the recognisable one behind it
    assert (
        N.classify_action([None, {"href": None}, {"href": "/messaging/compose"}])
        == "message"
    )


def test_split_person_lines_keeps_positions_and_never_names_nothing():
    for rng in _cases("lines"):
        lines = rng.choice([None, [_value(rng) for _ in range(rng.randrange(5))]])
        out = N.split_person_lines(lines)
        assert out["name"] is None or (isinstance(out["name"], str) and out["name"])
        assert all(isinstance(ln, str) for ln in out["rest"])
    out = N.split_person_lines(["Ben • 2.", None, "Berlin"])
    assert out["name"] == "Ben" and out["rest"] == ["", "Berlin"]
    assert N.split_person_lines([None, "Headline"])["name"] is None


def _attendee_result(rng):
    return {
        **_record(
            rng,
            [
                "event_id",
                "start_page",
                "pages_read",
                "next_page",
                "complete",
                "warnings",
            ],
        ),
        "attendees": rng.choice(
            [
                None,
                "x",
                _items(
                    rng, ["slug", "name", "headline", "degree", "action", "page"], 12
                ),
            ]
        ),
    }


def test_project_attendees_properties():
    for rng in _cases("attendees"):
        result = _attendee_result(rng)
        out = N.project_attendees(
            result, rng.choice([None, 1, 3, 50]), rng.choice(["minimal", "card"])
        )
        raw = result["attendees"] if isinstance(result["attendees"], list) else []
        # every projected person has a slug
        assert all(
            isinstance(a["slug"], str) and a["slug"].strip() for a in out["attendees"]
        )
        assert out["count"] == len(out["attendees"])
        broken = [
            a
            for a in raw
            if not isinstance(a, dict)
            or (
                a.get("action") != "self"
                and not (isinstance(a.get("slug"), str) and a["slug"].strip())
            )
        ]
        if broken:
            assert out["complete"] is False
            assert sum(out["skipped"].values()) == len(broken)
        # complete only when the source said so explicitly
        if out["complete"] is True:
            assert result.get("complete") is True


def test_project_attendees_one_broken_entry_keeps_the_rest():
    result = {
        "start_page": 1,
        "pages_read": 1,
        "complete": True,
        "attendees": [
            {"slug": "a", "name": "A", "action": "connect", "page": 1},
            None,
            {"slug": None, "name": "Ghost", "action": "connect", "page": 1},
            {"slug": "b", "name": "B", "action": "message", "page": 1},
        ],
    }
    out = N.project_attendees(result)
    assert [a["slug"] for a in out["attendees"]] == ["a", "b"]
    assert out["complete"] is False
    assert out["skipped"] == {"malformed": 1, "without_slug": 1}
    assert any("without a usable slug" in w for w in out["warnings"])


def test_project_attendees_missing_complete_is_not_complete():
    out = N.project_attendees({"attendees": [{"slug": "a"}]})
    assert out["complete"] is False


def test_message_projections_tolerate_broken_messages():
    for rng in _cases("messages"):
        msgs = _items(rng, ["text", "own", "index"])
        listed = I.mark_edited({"messages": list(msgs)})
        for m in listed["messages"]:
            if isinstance(m, dict):
                assert isinstance(m["text"], str) and m["edited"] in (True, False)
        picked = I.pick_own_message(msgs, rng.choice([None, "", "x", "Nachricht"]))
        assert picked["status"] in {
            "ok",
            "no_own_message",
            "message_not_found",
            "ambiguous_match",
            "message_without_index",
        }
        if picked["status"] == "ok":
            assert picked["message"].get("index") is not None
            assert picked["message"].get("own") is True
        target = rng.choice([{}, {"index": None}, {"index": 0}, None])
        assert I.edit_landed(msgs, target, "x") in (True, False)


def test_edit_landed_without_target_index_never_matches_an_indexless_message():
    msgs = [{"own": True, "text": "neu"}]  # no index
    assert I.edit_landed(msgs, {}, "neu") is False
    assert I.pick_own_message(msgs, None)["status"] == "message_without_index"


def test_own_truthy_string_is_not_own():
    # "own" from the page must be a real boolean; a stray string is not ours.
    assert (
        I.pick_own_message([{"own": "false", "index": 1, "text": "x"}], None)["status"]
        == "no_own_message"
    )


def test_comment_keys_and_group_lines_tolerate_page_junk():
    for rng in _cases("comments"):
        comments = rng.choice([None, 5, "x", _items(rng, ["key", "text"])])
        keys = A._matching_comment_keys(comments, "x")
        assert all(isinstance(k, str) and k for k in keys)
        lines = [_value(rng) for _ in range(rng.randrange(6))]
        assert all(isinstance(ln, str) for ln in A.group_member_lines(lines))


def test_post_gone_evidence_needs_readable_state():
    aid = "7300000000000000000"
    url = f"https://www.linkedin.com/feed/update/urn:li:activity:{aid}/"
    for rng in _cases("gone"):
        state = rng.choice([None, "x", _record(rng, ["text", "cards"])])
        assert O.post_gone_evidence(aid, url, state) in (True, False)
    assert (
        O.post_gone_evidence(aid, url, {"cards": "3", "text": "nicht verfügbar"})
        is False
    )


# -- page walks with a fake page -------------------------------------------------


class _Session:
    def __init__(self, page):
        self.page = page
        self.t = 0.0

    def monotonic(self):
        self.t += 100.0
        return self.t

    async def delay(self, _s):
        return None

    async def check_rate_limit(self):
        return None


class _Nav:
    async def _navigate_to_page(self, _url):
        return None


class _Page:
    def __init__(self, cards_per_call):
        self.calls = list(cards_per_call)

    async def evaluate(self, js, *_a):
        if js is N._CARDS_JS:
            return self.calls.pop(0) if self.calls else []
        if js is N._COUNT_JS:
            return 1
        return None


def _reader(pages):
    page = _Page(pages)
    reader = N.MiviaNetworkReader(_Session(page), _Nav())

    async def no_wait(*_a, **_k):
        return {"items": 1, "settled": 1}

    reader._wait_for_actions = no_wait
    return reader


def test_event_attendees_drop_broken_card_and_are_not_complete():
    good = {
        "slug": "a",
        "lines": ["A • 2.", "Head"],
        "actions": [{"href": "custom-invite"}],
    }
    cards = [
        good,
        None,
        {"slug": 12345, "lines": ["X"]},
        {"slug": "b", "lines": None, "actions": None},
        {"slug": "c", "lines": ["C", None, "Ort"], "actions": [None, {"text": None}]},
    ]
    out = asyncio.run(_reader([cards]).get_event_attendees("1234567890123", 1, 1))
    assert [a["slug"] for a in out["attendees"]] == ["a", "b", "c"]
    assert out["complete"] is False
    assert any("without a usable slug" in w for w in out["warnings"])
    b = out["attendees"][1]
    assert b["name"] is None and b["action"] == "unknown"


def test_event_attendees_fuzz_never_raise_and_complete_only_when_clean():
    for rng in _cases("walk"):
        if rng.random() < 0.9:
            continue  # the walk is slower; a tenth of the cases is enough
        cards = rng.choice(
            [None, "x", _items(rng, ["slug", "lines", "actions", "profile_urn"], 9)]
        )
        out = asyncio.run(_reader([cards]).get_event_attendees("1234567890123", 1, 1))
        assert all(isinstance(a["slug"], str) and a["slug"] for a in out["attendees"])
        usable = [
            c
            for c in (cards if isinstance(cards, list) else [])
            if isinstance(c, dict)
            and isinstance(c.get("slug"), str)
            and c["slug"].strip()
        ]
        if isinstance(cards, list) and len(usable) < len(cards):
            assert out["complete"] is False


# -- daily engagers ---------------------------------------------------------------


class _Engagement:
    def __init__(self, reactors, comments):
        self.reactors, self.comments = reactors, comments

    async def read_reactors(self, _aid, _limit):
        return {"available": True, "reactors": self.reactors}

    async def read_post_page(self, _aid):
        return {"reaction_count": 3, "comments": self.comments}


def _collector(tmp_path, monkeypatch, reactors, comments):
    monkeypatch.setenv("MIVIA_LINKEDIN_LEDGER", str(tmp_path / "ledger.jsonl"))

    class Ex:
        mivia_session = _Session(None)
        mivia_navigator = _Nav()

    c = mivia_daily.Collector(Ex(), {}, tmp_path)
    c.seen = E.SeenStore(tmp_path / "seen.json")
    c.engagement = _Engagement(reactors, comments)
    return c


def test_engagers_broken_entry_is_counted_and_rest_kept(tmp_path, monkeypatch):
    reactors = [{"id": "p1", "name": "A", "reaction": "LIKE"}, None, {"name": "B"}]
    comments = ["x", {"id": "p2", "name": "C", "comment_id": None, "text": None}]
    c = _collector(tmp_path, monkeypatch, reactors, comments)
    out = asyncio.run(c._engagers("1", reactors=True, source="own"))
    assert len(out["new_reactors"]) == 2 and len(out["new_comments"]) == 1
    assert out["unidentified"]["broken"] == 2


def test_engager_without_id_and_name_is_never_known(tmp_path, monkeypatch):
    reactors = [{"id": None, "name": None, "reaction": "LIKE"}]
    c = _collector(tmp_path, monkeypatch, reactors, [])
    asyncio.run(c._engagers("1", reactors=True, source="own"))
    # a second, different anonymous reactor must not be hidden by the first
    c.engagement = _Engagement([{"id": None, "name": "", "reaction": "LIKE"}], [])
    out = asyncio.run(c._engagers("1", reactors=True, source="own"))
    assert len(out["new_reactors"]) == 1
    assert out["unidentified"]["without_id_or_name"] == 1


def test_engagers_fuzz_never_raise(tmp_path, monkeypatch):
    c = _collector(tmp_path, monkeypatch, [], [])
    for i, rng in enumerate(_cases("engagers")):
        if i % 20:
            continue
        reactors = rng.choice([None, "x", _items(rng, ["id", "name", "reaction"])])
        comments = rng.choice([None, _items(rng, ["id", "name", "comment_id", "text"])])
        c.engagement = _Engagement(reactors, comments)
        out = asyncio.run(c._engagers(str(i), reactors=True, source="own"))
        assert out["reactor_total"] + out["comment_total"] + out.get(
            "unidentified", {}
        ).get("broken", 0) == len(reactors if isinstance(reactors, list) else []) + len(
            comments if isinstance(comments, list) else []
        )


# -- other lanes: pinned only -----------------------------------------------------


def test_reaction_kind_tolerates_null_entries():
    assert E.reaction_kind([None], [None]) == "unknown"


def test_split_engager_lines_tolerates_null_line():
    assert E.split_engager_lines([None, "Headline"])["name"] is None
    assert E.split_engager_lines([None, "Headline"])["headline"] == "Headline"


def test_engager_key_anonymous_entries_never_collide():
    a = E.engager_key("reaction", None, "like", name=None)
    b = E.engager_key("reaction", "", "like", name="  ")
    assert a != b and "name=?" not in a
    assert E.engager_key("reaction", None, "like", name=" Anna ") == (
        "reaction:name=anna:like"
    )
    assert E.engager_key("reaction", "x", "like") == "reaction:x:like"
