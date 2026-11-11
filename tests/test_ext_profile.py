"""Browser-free checks of the profile editing tools (identity, input, pacing)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from linkedin_mcp_server import ext_outreach as outreach
from linkedin_mcp_server.linkedin.ext_profile import ExtProfileEditor
from linkedin_mcp_server.tools import ext_profile as tp


def test_profile_edit_is_a_paced_visible_write() -> None:
    assert outreach.PACE_BUDGETS["profile_edit"] == {"day": 6, "week": 20}
    assert "profile_edit" in outreach.PACE_WRITE_KINDS


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/x",
        "https://localhost/",
        "ftp://example.org",
        "https://exa mple.org",
        "javascript:alert(1)",
        "",
    ],
)
def test_url_refused(url: str) -> None:
    assert tp.check_url(url, "u")["status"] == "invalid_input"


def test_url_accepted() -> None:
    assert tp.check_url("https://example.org/workshop/", "u") is None


@pytest.mark.parametrize("text", ["", " x", "x ", "a\nb", "a​b", "x" * 51])
def test_button_text_refused(text: str) -> None:
    assert tp.check_line(text, "t", tp.BUTTON_TEXT_MAX) is not None


def test_button_text_accepted() -> None:
    assert tp.check_line("Zum Workshop anmelden", "t", tp.BUTTON_TEXT_MAX) is None


def test_slug_refused() -> None:
    assert tp.check_slug("") is not None
    assert tp.check_slug("a/b") is not None
    assert tp.check_slug("member-123") is None


def test_change_key_stable_and_distinct() -> None:
    a = tp.change_key(op="intro", button_url="https://a.org", button_text=None)
    assert a == tp.change_key(op="intro", button_text=None, button_url="https://a.org")
    assert a != tp.change_key(op="intro", button_url="https://b.org")


class _FakeEditor(ExtProfileEditor):
    def __init__(self, signed_in: str | None) -> None:  # no browser
        self._signed_in = signed_in

    async def own_slug(self) -> str | None:  # type: ignore[override]
        return self._signed_in


def test_identity_gate_refuses_other_account() -> None:
    got = asyncio.run(_FakeEditor("someone-else").check_identity("member-123"))
    assert got == {"status": "wrong_account", "signed_in": "someone-else"}


def test_identity_gate_refuses_unknown() -> None:
    got = asyncio.run(_FakeEditor(None).check_identity("member-123"))
    assert got == {"status": "own_identity_unknown"}


def test_identity_gate_passes_case_insensitive() -> None:
    assert asyncio.run(_FakeEditor("member-123").check_identity("Member-123 ")) is None


class _FeaturedEditor(ExtProfileEditor):
    def __init__(self, titles: list[str]) -> None:
        self._titles = titles
        self.calls: list[Any] = []
        self.clicked = False

    async def featured_titles(self, slug: str) -> list[str]:  # type: ignore[override]
        return list(self._titles)

    async def _add_link(self, slug, url, title, confirm):  # type: ignore[override]
        self.calls.append(("add", confirm))
        return {"status": "dry_run"}

    async def _delete_item(self, slug, title, confirm):  # type: ignore[override]
        self.calls.append(("delete", confirm))
        return {"status": "dry_run"}


def test_featured_replace_unchanged_when_already_done() -> None:
    ed = _FeaturedEditor(["New"])
    got = asyncio.run(
        ed.replace_featured_link(
            "m-1",
            old_title="Old",
            new_url="https://a.org",
            new_title="New",
            confirm=True,
        )
    )
    assert got["status"] == "unchanged" and ed.calls == []


def test_featured_replace_missing_old() -> None:
    ed = _FeaturedEditor(["Other"])
    got = asyncio.run(
        ed.replace_featured_link(
            "m-1",
            old_title="Old",
            new_url="https://a.org",
            new_title="New",
            confirm=True,
        )
    )
    assert got["status"] == "featured_item_missing" and ed.calls == []


def test_featured_replace_adds_before_delete_and_dry_run_never_confirms() -> None:
    ed = _FeaturedEditor(["Old", "Keep"])
    got = asyncio.run(
        ed.replace_featured_link(
            "m-1",
            old_title="Old",
            new_url="https://a.org",
            new_title="New",
            confirm=False,
        )
    )
    assert got["status"] == "dry_run"
    assert got["pre_image"] == {"titles": ["Old", "Keep"]}
    assert ed.calls == [("add", False), ("delete", False)]


def test_featured_replace_refuses_duplicate_titles() -> None:
    ed = _FeaturedEditor(["Old", "Old"])
    got = asyncio.run(
        ed.replace_featured_link(
            "m-1",
            old_title="Old",
            new_url="https://a.org",
            new_title="New",
            confirm=True,
        )
    )
    assert got["status"] == "featured_item_not_unique"
