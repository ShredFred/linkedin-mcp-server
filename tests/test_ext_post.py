"""Fork extension: the composer hardening around create_post.

Two things the productive HK post on the member's profile depends on and that the
composer did not do before 2026-10-01:

* a restored share draft with an attached image must stop the run instead of
  silently publishing two images,
* a published post must return its activity id, because the first comment
  (the booking link) is posted through comment_on_post and needs it.
"""

from __future__ import annotations

from typing import Any

import pytest

from linkedin_mcp_server.linkedin import ext_post
from linkedin_mcp_server.linkedin.ext_post import ExtPostComposer


class FakePage:
    def __init__(
        self,
        *,
        leftover: int = 0,
        urn: str | None,
        body: str = "",
        newest: bool = True,
    ) -> None:
        self.newest = newest
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

    async def evaluate(self, script: str, arg: Any = None, **_: Any) -> Any:
        if script is ext_post._MEDIA_PRESENT_JS:
            return self.leftover
        if script is ext_post._ACTIVITY_URN_JS:
            if self.urn is None:
                return None
            return {"urn": self.urn, "matched": True, "newest": self.newest}
        handled, value = mention_writer_script(self, script, arg)
        if handled:
            return value
        if script is ext_post._CLEAR_JS:
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


def composer(page: FakePage) -> ExtPostComposer:
    return ExtPostComposer(FakeSession(page), FakeNavigator(page))  # type: ignore[arg-type]


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


@pytest.mark.asyncio
async def test_older_post_with_same_first_line_does_not_verify() -> None:
    # The activity page also lists last week's post that opened with the same
    # line; the newest card does not carry it, so the publish is unverified.
    page = FakePage(
        urn="urn:li:activity:7000000000000000001", body="Erste Zeile", newest=False
    )
    result = await composer(page).create_post(
        "Erste Zeile\n\nzweite", image_path=None, confirm_post=True
    )
    assert result["posted"] is True
    assert result["verified"] is False
    assert result["status"] == "posted_unverified"


class ImagePage(FakePage):
    """Tracks uploads and discard calls; configurable media button and editor."""

    def __init__(
        self,
        *,
        media_count: int = 1,
        lose_editor: bool = False,
        discard_raises: bool = False,
    ) -> None:
        super().__init__(urn=None)
        self.media_count = media_count
        self.lose_editor = lose_editor
        self.discard_raises = discard_raises
        self.uploads = 0
        self.discards = 0
        self.waits = 0

    async def wait_for_selector(self, *_: Any, **__: Any) -> None:
        self.waits += 1
        if self.lose_editor and self.waits > 1:
            raise TimeoutError("editor gone")

    def expect_file_chooser(self, **_: Any) -> Any:
        page = self

        class _Chooser:
            async def set_files(self, _path: str) -> None:
                page.uploads += 1

        async def _value() -> Any:
            return _Chooser()

        class _Ctx:
            async def __aenter__(self) -> Any:
                self.value = _value()
                return self

            async def __aexit__(self, *_: Any) -> None:
                return None

        return _Ctx()

    async def evaluate(self, script: str, arg: Any = None, **_: Any) -> Any:
        if script is ext_post._DISCARD_JS:
            assert "posten" in arg["post"]
            if self.discard_raises:
                self.discards += 1
                raise RuntimeError("dialog gone")
            # Like the real page: the discard prompt only exists after the
            # close click, so only the second pass can confirm it.
            if arg["discard_only"]:
                self.discards += 1
                return "discarded"
            return "closed"
        if script is ext_post._FIND_BUTTON_JS and arg["tag"] == "media":
            return {"count": self.media_count, "disabled": False}
        if script.startswith("(words) =>"):
            return 1
        return await super().evaluate(script, arg)


@pytest.fixture
def image_file(tmp_path: Any) -> str:
    path = tmp_path / "bild.png"
    path.write_bytes(b"\x89PNG")
    return str(path)


@pytest.mark.asyncio
async def test_dry_run_with_image_never_uploads(image_file: str) -> None:
    page = ImagePage()
    result = await composer(page).create_post(
        "Erste Zeile", image_path=image_file, confirm_post=False
    )
    assert result["status"] == "dry_run"
    assert result["image_step"] == "not_uploaded_dry_run"
    assert page.uploads == 0
    assert '[data-ext-target="media"]' not in page.clicked
    assert result["posted"] is False


@pytest.mark.asyncio
async def test_media_button_unavailable_discards_editor(image_file: str) -> None:
    page = ImagePage(media_count=0)
    result = await composer(page).create_post(
        "Erste Zeile", image_path=image_file, confirm_post=False
    )
    assert result["status"] == "media_button_unavailable"
    assert page.discards == 1
    assert result["cleanup"] == "closed+discarded"
    assert page.discards == 1
    assert not page.clicked


@pytest.mark.asyncio
async def test_composer_lost_after_image_discards_and_tolerates_cleanup_error(
    image_file: str,
) -> None:
    page = ImagePage(lose_editor=True, discard_raises=True)
    result = await composer(page).create_post(
        "Erste Zeile", image_path=image_file, confirm_post=True
    )
    assert result["status"] == "composer_lost_after_image"
    assert page.uploads == 1
    assert page.discards == 1
    assert "dialog gone" in result["cleanup_error"]
    assert result["posted"] is False
    assert '[data-ext-target="post"]' not in page.clicked


def mention_writer_script(page: Any, script: str, arg: Any) -> tuple[bool, Any]:
    """Answer the shared writer's scripts on a fake page (plain text only)."""
    from linkedin_mcp_server.linkedin import ext_mentions as em

    if script is em._PREP_JS:
        if arg.get("clear"):
            page.text = ""
        if arg.get("require_empty") and page.text:
            return True, "occupied"
        return True, "ok"
    if script is em._ENGINE_JS:
        return True, "tiptap"
    if script is em._INSERT_JS:
        outcome = getattr(page, "write", "written")
        if outcome != "written":
            return True, outcome
        page.text += arg["text"]
        return True, "ok"
    if script is em._STATE_JS:
        return True, {"text": page.text, "pending": False}
    if script is em._ENTITIES_JS:
        return True, []
    return False, None
