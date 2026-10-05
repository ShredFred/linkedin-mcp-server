"""Bug hunt round 3 (2026-10-05): regressions for the fixes of that round.

1. comment_on_post and edit_own_post/edit_own_comment checked only
   contracts.is_invisible_control, which misses Unicode format characters
   such as U+061C (ARABIC LETTER MARK, a bidi control) and U+2060 (WORD
   JOINER). Every other write path refuses them through hidden_format_char.
2. parse_comment_ref accepted non-ASCII digits (Arabic-Indic) as a comment id.
3. A reply whose before-read saw no thread root counted a pre-existing reply
   with the same text as new, i.e. posted/verified.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from linkedin_mcp_server.linkedin import ext_actions as ea
from linkedin_mcp_server.linkedin.ext_own_content import parse_comment_ref
from linkedin_mcp_server.tools.ext_own_content import check_text
from test_ext_stage2_haertung import (  # noqa: F401  (autouse fixture)
    POST,
    _call,
    _isolated,
)

FORMAT_CHARS = ["؜", "⁠", "⁢", "᠎", "\U000e0001", "­"]


@pytest.mark.parametrize("char", FORMAT_CHARS)
def test_own_content_text_refuses_format_chars(char):
    assert check_text(f"a{char}b", 100)["status"] == "invalid_text"


class _NeverCalled:
    async def comment(self, *_a, **_k):  # pragma: no cover - must not run
        raise AssertionError("comment must not be reached")

    async def reply(self, *_a, **_k):  # pragma: no cover - must not run
        raise AssertionError("reply must not be reached")


@pytest.mark.parametrize("char", FORMAT_CHARS)
def test_comment_refuses_format_chars(monkeypatch, char):
    out = _call(
        "comment_on_post",
        {"post_url": POST, "text": f"Danke{char} dafuer", "confirm": True},
        monkeypatch,
        actions=_NeverCalled(),
    )
    assert out["status"] == "invalid_text" and out["posted"] is False


@pytest.mark.parametrize(
    "ref",
    [
        "١" * 12,
        "urn:li:comment:(activity:" + "١" * 19 + ",7123456789012340001)",
        "urn:li:comment:(activity:7123456789012345678," + "١" * 19 + ")",
    ],
)
def test_comment_ref_requires_ascii_digits(ref):
    with pytest.raises(ValueError):
        parse_comment_ref(ref)


class _Clickable:
    async def click(self):
        return None


class _Locator:
    first = _Clickable()


class _Page:
    def __init__(self, reads: list[Any]):
        self.reads = reads

    async def evaluate(self, _js, _arg=None):
        return self.reads.pop(0)

    def locator(self, _sel):
        return _Locator()


class _Editor:
    def __init__(self, text: str):
        self.text = text

    async def inner_text(self):
        return self.text

    async def evaluate(self, _js):
        return {"count": 1, "disabled": False}


async def _nothing(*_a, **_k):
    return None


def test_reply_without_before_root_is_not_verified():
    text = "Danke, sehr hilfreich"
    old = {"roots": 1, "replies": [{"key": "old", "text": text}]}
    actions = object.__new__(ea.ExtActions)
    actions._session = type(
        "S", (), {"page": _Page([None, old]), "delay": staticmethod(_nothing)}
    )()
    actions._insert_text = _nothing
    out = asyncio.run(actions._reply_typed(_Editor(text), text, True, "root"))
    assert out["posted"] is True
    assert out["status"] == "unverified" and out["verified"] is False
