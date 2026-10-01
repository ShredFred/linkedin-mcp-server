"""MiViA fork, round 7 leftovers (2026-10-01). No browser, no live action."""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from linkedin_mcp_server import mivia_outreach as outreach
from linkedin_mcp_server.linkedin import mivia_engagement as eng
from linkedin_mcp_server.mivia_message_checks import shortener_findings

A = "7123456789012345678"
POST = f"https://www.linkedin.com/feed/update/urn:li:activity:{A}/"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(outreach.LEDGER_ENV, str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv(outreach.NOTES_ENV, str(tmp_path / "notes.json"))
    monkeypatch.setenv(eng.SEEN_ENV, str(tmp_path / "seen.json"))


def _stored(tmp_path, key):
    data = json.loads((tmp_path / "seen.json").read_text(encoding="utf-8"))
    return data[key]["keys"]


# -- (1) anonymous engager keys are reported but never remembered -------------


class _Reader:
    async def read_reactors(self, activity_id, limit):
        return {
            "available": True,
            "complete": True,
            "reactors": [
                {"id": "u1", "name": "Anna", "reaction": "LIKE"},
                {"id": None, "name": "", "reaction": "LIKE"},
            ],
        }

    async def read_post_page(self, activity_id):
        return {
            "reaction_count": 2,
            "comments": [{"id": None, "name": None, "comment_id": "c1"}],
        }


def test_post_engagers_do_not_remember_anonymous(tmp_path, monkeypatch):
    from test_mivia_stage2_haertung import _call

    for _ in range(2):
        out = _call(
            "get_post_engagers",
            {"post_url": POST, "include_comments": True, "since_last_run": True},
            monkeypatch,
            engagement=_Reader(),
        )
    keys = _stored(tmp_path, A)
    assert keys and not any(":anon=" in k for k in keys)
    # Second run: the identified reactor is known, the anonymous ones stay new.
    assert out["reactor_count"] == 1
    assert out["comment_count"] == 1


def test_daily_viewers_do_not_remember_anonymous(tmp_path):
    from linkedin_mcp_server.mivia_daily import Collector

    class _Actions:
        async def profile_viewers(self, limit):
            return {
                "total_viewers": 2,
                "viewers": [
                    {"slug": "anna", "name": "Anna"},
                    {"slug": None, "name": ""},
                ],
            }

    c = Collector.__new__(Collector)
    c.cfg = {}
    c.seen = eng.SeenStore()
    c.actions = _Actions()
    c._take = lambda *a, **k: None
    asyncio.run(c.viewers())
    asyncio.run(c.viewers())
    keys = _stored(tmp_path, "profile_viewers")
    assert keys == ["viewer:anna:"]


# -- (2) test doubles use the public property ---------------------------------


def test_doubles_use_property_not_private_field():
    import pathlib

    here = pathlib.Path(__file__).parent
    for name in ("test_mivia_r5_ablauf.py", "test_mivia_send_haertung.py"):
        assert "_mivia_session" not in (here / name).read_text(encoding="utf-8")


# -- (3) _file_lock: failed token write leaves no fd and no lock file ---------


def test_file_lock_cleans_up_on_failed_write(tmp_path, monkeypatch):
    lock = tmp_path / "x.lock"
    closed = []
    real_close = os.close

    def bad_write(fd, data):
        raise OSError("disk full")

    monkeypatch.setattr(outreach.os, "write", bad_write)
    monkeypatch.setattr(
        outreach.os, "close", lambda fd: (closed.append(fd), real_close(fd))
    )
    with pytest.raises(OSError, match="disk full"):
        with outreach._file_lock(lock, timeout=0.5):
            pass
    assert closed
    assert not lock.exists()


# -- (4) shortener name as a path segment of another host ---------------------


@pytest.mark.parametrize(
    "text",
    [
        "https://example.com/t.co/abc",
        "https://mivia.ai/de/ow.ly/x",
        "mivia.ai/de/bit.ly/",
    ],
)
def test_shortener_in_path_is_not_a_shortener(text):
    assert shortener_findings(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "https://bit.ly/x",
        "xhttps://t.co/a",
        "Siehe_bit.ly/x",
        "?r=ow.ly/x",
        "a lnkd.in/x",
    ],
)
def test_real_shorteners_still_found(text):
    assert shortener_findings(text)
