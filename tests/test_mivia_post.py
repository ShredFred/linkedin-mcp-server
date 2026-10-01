"""MiViA fork: the composer hardening around create_post.

Two things the productive HK post on Jessica's profile depends on and that the
composer did not do before 2026-10-01:

* a restored share draft with an attached image must stop the run instead of
  silently publishing two images,
* a published post must return its activity id, because the first comment
  (the booking link) is posted through comment_on_post and needs it.
"""

from __future__ import annotations

from typing import Any

import pytest

from linkedin_mcp_server.scraping import mivia_post
from linkedin_mcp_server.scraping.mivia_post import MiviaPostComposer


class FakePage:
    def __init__(self, *, leftover: int = 0, urn: str | None, body: str = "") -> None:
        self.leftover = leftover
        self.urn = urn
        self.body = body
        self.text = ""
        self.clicked: list[str] = []
        self.goto_urls: list[str] = []

    async def goto(self, url: str, **_: Any) -> None:
        self.goto_urls.append(url)

    async def wait_for_selector(self, *_: Any, **__: Any) -> None:
        return None

    async def click(self, selector: str) -> None:
        self.clicked.append(selector)

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if script is mivia_post._MEDIA_PRESENT_JS:
            return self.leftover
        if script is mivia_post._ACTIVITY_URN_JS:
            return None if self.urn is None else {"urn": self.urn, "matched": True}
        if script is mivia_post._WRITE_JS:
            self.text = arg["text"]
            return "written"
        if script is mivia_post._CLEAR_JS:
            self.text = ""
            return True
        if script.startswith("(arg) => {"):  # _FIND_BUTTON_JS
            return {"count": 1, "disabled": False}
        return self.body


class FakeSession:
    def __init__(self, page: FakePage) -> None:
        self.page = page

    async def check_rate_limit(self) -> None:
        return None

    async def delay(self, _: float) -> None:
        return None


class FakeNavigator:
    def __init__(self, page: FakePage) -> None:
        self._page = page

    async def _navigate_to_page(self, url: str) -> None:
        self._page.goto_urls.append(url)


def composer(page: FakePage) -> MiviaPostComposer:
    return MiviaPostComposer(FakeSession(page), FakeNavigator(page))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_restored_draft_with_media_stops_before_writing() -> None:
    page = FakePage(leftover=1, urn=None)
    result = await composer(page).create_post(
        "Erste Zeile\n\nzweite", image_path=None, confirm_post=True
    )
    assert result["status"] == "editor_has_media"
    assert result["posted"] is False
    assert result["attached"] == 1
    assert page.text == ""
    assert not page.clicked


@pytest.mark.asyncio
async def test_published_post_returns_the_activity_id_for_the_first_comment() -> None:
    page = FakePage(urn="urn:li:activity:7123456789012345678", body="Erste Zeile")
    result = await composer(page).create_post(
        "Erste Zeile\n\nzweite", image_path=None, confirm_post=True
    )
    assert result["status"] == "posted_verified"
    assert result["activity_id"] == "7123456789012345678"
    assert result["post_url"] == (
        "https://www.linkedin.com/feed/update/urn:li:activity:7123456789012345678/"
    )
    assert result["post_url_matched_text"] is True


@pytest.mark.asyncio
async def test_missing_urn_is_reported_as_none_not_guessed() -> None:
    page = FakePage(urn=None, body="Erste Zeile")
    result = await composer(page).create_post(
        "Erste Zeile", image_path=None, confirm_post=True
    )
    assert result["posted"] is True
    assert result["activity_id"] is None
    assert result["post_url"] is None


@pytest.mark.asyncio
async def test_dry_run_publishes_nothing_and_clears_the_editor() -> None:
    page = FakePage(urn=None)
    result = await composer(page).create_post(
        "Erste Zeile", image_path=None, confirm_post=False
    )
    assert result["status"] == "dry_run"
    assert result["posted"] is False
    assert page.text == ""
    assert not page.clicked
