"""fork stage 2, hardening round 2 (2026-10-01).

No browser: ``_run`` is replaced by a direct call with a stub extractor.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client, FastMCP

from linkedin_mcp_server import ext_outreach as outreach
from linkedin_mcp_server.linkedin.ext_engagement import parse_activity_id

A = "7123456789012345678"
B = "7123456789012345679"
POST = f"https://www.linkedin.com/feed/update/urn:li:activity:{A}/"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(outreach.LEDGER_ENV, str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv(outreach.NOTES_ENV, str(tmp_path / "notes.json"))


def _call(name, args, monkeypatch=None, actions=None, engagement=None):
    import linkedin_mcp_server.tools.ext as m
    import linkedin_mcp_server.tools.ext_stage2 as s2

    if monkeypatch is not None:

        async def fake_run(ctx, tool, body):
            return await body(object())

        monkeypatch.setattr(s2, "_run", fake_run)
        if actions is not None:
            monkeypatch.setattr(s2, "_actions", lambda ex: actions)
        if engagement is not None:
            monkeypatch.setattr(s2, "_engagement", lambda ex: engagement)
    mcp = FastMCP("t")
    m.register_ext_tools(mcp)

    async def go():
        async with Client(mcp) as c:
            return (await c.call_tool(name, args)).structured_content

    return asyncio.run(go())


# -- (1) set_contact_note -------------------------------------------------------


def _note(**args):
    return _call("set_contact_note", {"linkedin_username": "dieter", **args})


def test_note_length_counts_utf16_units():
    import linkedin_mcp_server.tools.ext_stage2 as s2

    # Emojis are two UTF-16 units: NOTE_MAX emojis exceed the limit.
    assert _note(note="\U0001f600" * s2.NOTE_MAX)["status"] == "note_too_long"
    assert _note(note="a" * s2.NOTE_MAX)["status"] == "saved"


@pytest.mark.parametrize("char", ["\u202e", "\u2066", "\u200b", "\u061c", "\u2060"])
def test_note_refuses_bidi_and_cf(char):
    assert _note(note=f"Kunde{char}xy")["status"] == "invalid_note"


@pytest.mark.parametrize("tag", ["a\u202eb", "a\nb", "a\u00adb"])
def test_tags_refuse_control_and_invisible(tag):
    assert _note(tags=[tag])["status"] == "invalid_tag"


def test_tag_length_utf16():
    import linkedin_mcp_server.tools.ext_stage2 as s2

    assert _note(tags=["\U0001f600" * s2.TAG_MAX])["status"] == "tag_too_long"


# -- (2) parse_activity_id ---------------------------------------------------------


def test_activity_id_refuses_two_different_ids():
    with pytest.raises(ValueError, match="more than one"):
        parse_activity_id(f"urn:li:activity:{A} urn:li:activity:{B}")


def test_activity_id_same_id_twice_is_fine():
    assert parse_activity_id(f"{POST}?x=urn:li:activity:{A}") == A


def test_tools_return_invalid_post_url_instead_of_raising(monkeypatch):
    bad = f"urn:li:activity:{A}/urn:li:activity:{B}"
    assert _call("get_post_engagers", {"post_url": bad})["status"] == (
        "invalid_post_url"
    )
    out = _call("comment_on_post", {"post_url": bad, "text": "x"})
    assert out["status"] == "invalid_post_url"
    assert out["posted"] is False
    assert _call("get_post_analytics", {"post_urls": [POST, bad]})["status"] == (
        "invalid_post_url"
    )


# -- comment_on_post -------------------------------------------------------------


class _Commenter:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def comment(self, activity_id, text, confirm):
        self.calls.append((activity_id, confirm))
        return self.result


def _comment(monkeypatch, actions, text="Starker Beitrag", post=POST):
    return _call(
        "comment_on_post",
        {"post_url": post, "text": text, "confirm": True},
        monkeypatch,
        actions=actions,
    )


def test_second_comment_same_post_other_text_refused(monkeypatch):
    actions = _Commenter({"status": "posted", "posted": True})
    assert _comment(monkeypatch, actions)["status"] == "posted"
    again = _comment(monkeypatch, actions, text="Noch ein Gedanke")
    assert again["status"] == "already_commented"
    assert len(actions.calls) == 1
    # Another post is still open.
    other = f"urn:li:activity:{B}"
    assert _comment(monkeypatch, actions, text="Anderer", post=other)["status"] == (
        "posted"
    )
    assert actions.calls[-1][0] == B


def test_unknown_outcome_blocks_same_post(monkeypatch):
    class Boom:
        async def comment(self, *a):
            raise RuntimeError("tab closed after click")

    with pytest.raises(Exception):
        _comment(monkeypatch, Boom())
    actions = _Commenter({"status": "posted", "posted": True})
    assert _comment(monkeypatch, actions, text="Neu")["status"] == ("already_commented")
    assert actions.calls == []


def test_not_posted_releases_same_post(monkeypatch):
    actions = _Commenter({"status": "no_editor", "posted": False})
    _comment(monkeypatch, actions)
    actions.result = {"status": "posted", "posted": True}
    assert _comment(monkeypatch, actions, text="Zweiter Versuch")["status"] == (
        "posted"
    )


@pytest.mark.parametrize(
    "result,status,posted",
    [
        ({"status": "clicked", "posted": True}, "unverified", True),
        ({"status": "posted", "posted": "yes"}, "posted", False),
        ({"status": "unverified", "posted": True}, "unverified", True),
    ],
)
def test_posted_flag_and_status_normalised(monkeypatch, result, status, posted):
    out = _comment(monkeypatch, _Commenter(result))
    assert out["status"] == status
    assert out["posted"] is posted


def test_dry_run_not_blocked_by_earlier_comment(monkeypatch):
    actions = _Commenter({"status": "posted", "posted": True})
    _comment(monkeypatch, actions)
    actions.result = {"status": "dry_run", "posted": False}
    out = _call(
        "comment_on_post",
        {"post_url": POST, "text": "Probe", "confirm": False},
        monkeypatch,
        actions=actions,
    )
    assert out["status"] == "dry_run"


# -- get_post_analytics ----------------------------------------------------------


class _Reader:
    def __init__(self):
        self.read = []

    async def read_post_summary(self, activity_id):
        self.read.append(activity_id)
        return {"activity_id": activity_id, "available": True}


def test_analytics_refuses_more_than_ten():
    urls = [f"urn:li:activity:{int(A) + i}" for i in range(11)]
    out = _call("get_post_analytics", {"post_urls": urls})
    assert out["status"] == "too_many_posts"


def test_analytics_empty_refused():
    assert _call("get_post_analytics", {"post_urls": []})["status"] == (
        "invalid_post_url"
    )


def test_analytics_dedupes_and_reports_requested(monkeypatch):
    reader = _Reader()
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    out = _call(
        "get_post_analytics",
        {"post_urls": [POST, A]},
        monkeypatch,
        engagement=reader,
    )
    assert reader.read == [A]
    assert (out["count"], out["requested"]) == (1, 2)


async def _nosleep(*_a, **_k):
    return None


# -- invite_to_event --------------------------------------------------------------


@pytest.mark.parametrize("event_id", ["", "abc", "https://www.linkedin.com/events/"])
def test_invite_refuses_bad_event_id(event_id):
    out = _call("invite_to_event", {"event_id": event_id, "usernames": ["dieter"]})
    assert out["status"] == "invalid_event_id"


def test_invite_refuses_empty_list():
    out = _call("invite_to_event", {"event_id": "7380000000000000000", "usernames": []})
    assert out["status"] == "no_recipients"


def test_invite_dedupes_and_accepts_url_with_query(monkeypatch):
    class Caps:
        async def event_capabilities(self, event_id):
            self.seen = event_id
            return {"can_invite": False}

    caps = Caps()
    out = _call(
        "invite_to_event",
        {
            "event_id": "https://www.linkedin.com/events/7380000000000000000/?x=1",
            "usernames": ["dieter", "dieter"],
        },
        monkeypatch,
        actions=caps,
    )
    assert out["status"] == "not_organizer"
    assert out["requested"] == ["dieter"]
    assert caps.seen == "7380000000000000000"


# -- job_watch ------------------------------------------------------------------


def test_partly_failed_job_watch_is_partial_and_stays_due(monkeypatch, tmp_path):
    import linkedin_mcp_server.tools.ext_stage2 as s2

    path = tmp_path / "jobs.json"
    monkeypatch.setattr(s2, "job_watch_path", lambda: path)
    monkeypatch.setattr(s2.asyncio, "sleep", _nosleep)

    class Ex:
        async def search_jobs(self, keywords, location, **k):
            if keywords == "kaputt":
                raise RuntimeError("rate limited")
            return {"job_ids": ["1"], "references": None, "url": "u"}

    async def fake_run(ctx, tool, body):
        return await body(Ex())

    monkeypatch.setattr(s2, "_run", fake_run)
    searches = [{"keywords": "ok", "location": "DE"}, {"keywords": "kaputt"}]
    out = _call("job_watch", {"searches": searches})
    assert out["status"] == "partial"
    assert out["complete"] is False
    assert out["failed_searches"] == 1
    # Not recorded as a run: the next call is due again.
    again = _call("job_watch", {"searches": searches})
    assert again["status"] != "not_due"
