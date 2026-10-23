"""Fork extension: bug-hunt regressions for mentions and media (2026-10-11)."""

from __future__ import annotations

from typing import cast, Any

from value_assertions import require_value

import contextlib
import os

import pytest

from linkedin_mcp_server.linkedin import ext_media, ext_post
from linkedin_mcp_server.linkedin.ext_media import MediaAttacher, check_media
from linkedin_mcp_server.linkedin.ext_mentions import apply_mention_list, parse_target
from test_ext_post import FakePage, composer


def test_mention_matches_whole_words_only() -> None:
    out, bad = apply_mention_list(
        "Annabelle und Anna", [{"name": "Anna", "target": "anna-x"}]
    )
    assert bad is None
    assert out == "Annabelle und [[Anna|anna-x]]"
    _, bad = apply_mention_list("Annabelle", [{"name": "Anna", "target": "anna-x"}])
    assert require_value(bad)["status"] == "mention_name_not_in_text"


def test_unicode_slug_short_form_and_url_agree() -> None:
    short = parse_target("X", "jörg-m")
    url = parse_target("X", "https://www.linkedin.com/in/jörg-m/")
    assert short.slug == url.slug == "jörg-m"
    assert parse_target("X", "company:müller-gmbh").slug == "müller-gmbh"
    with pytest.raises(ValueError):
        parse_target("X", "a b")


def test_duplicate_file_by_other_spelling(tmp_path, monkeypatch) -> None:
    img = tmp_path / "a.png"
    img.write_bytes(b"x")
    monkeypatch.chdir(tmp_path)
    variants = [{"path": str(img)}, {"path": "a.png"}]
    assert require_value(check_media(variants)[1])["status"] == "media_invalid"
    if os.path.normcase("A") == os.path.normcase("a"):
        assert (
            require_value(
                check_media([{"path": str(img)}, {"path": str(img).upper()}])[1]
            )["status"]
            == "media_invalid"
        )


def test_alt_text_control_chars_and_input_shape(tmp_path) -> None:
    img = tmp_path / "a.png"
    img.write_bytes(b"x")
    assert (
        require_value(check_media([{"path": str(img), "alt_text": "a\rb"}])[1])[
            "status"
        ]
        == "alt_text_invalid"
    )
    assert (
        require_value(check_media([{"path": str(img), "alt_text": "a\tb"}])[1])[
            "status"
        ]
        == "alt_text_invalid"
    )
    assert (
        require_value(check_media(cast("list[dict[str, Any]]", {"path": str(img)}))[1])[
            "status"
        ]
        == "media_invalid"
    )  # type: ignore[arg-type]
    txt = tmp_path / "a.txt"
    txt.write_bytes(b"x")
    assert (
        require_value(check_media([{"path": str(txt)}])[1])["status"]
        == "media_unsupported_type"
    )


@pytest.mark.asyncio
async def test_attach_exception_discards_composer(tmp_path, monkeypatch) -> None:
    img = tmp_path / "a.png"
    img.write_bytes(b"x")
    items, _ = check_media([{"path": str(img)}])
    discarded: list[Any] = []

    class Boom:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def attach(self, *_: Any, **__: Any) -> dict[str, Any]:
            raise RuntimeError("page crashed")

    async def fake_discard(self, result) -> None:
        discarded.append(result)

    monkeypatch.setattr(ext_post, "MediaAttacher", Boom)
    monkeypatch.setattr(ext_post.ExtPostComposer, "_discard", fake_discard)
    with pytest.raises(RuntimeError):
        await composer(FakePage(urn=None)).create_post(
            "Zeile", image_path=None, confirm_post=False, media=items
        )
    assert len(discarded) == 1


class _Chooser:
    async def set_files(self, _: Any) -> None:
        return None


class _VideoPage:
    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if script is ext_media._VIDEO_EDITOR_JS:
            return {"editor_videos": 1, "composer_videos": 0}
        return {"count": 1}

    async def click(self, _: str) -> None:
        return None

    @contextlib.asynccontextmanager
    async def expect_file_chooser(self, **_: Any):
        class Info:
            @property
            async def value(self) -> _Chooser:
                return _Chooser()

        yield Info()


@pytest.mark.asyncio
async def test_video_next_disabled_waits_then_reports_incomplete(tmp_path) -> None:
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"x")
    att = MediaAttacher(
        _VideoPage(),
        editor_selector="#ed",
        media_words=["m"],
        allow_tags=False,
        kinds=("video",),
        poll=0.01,
        timeout=0.1,
    )
    calls: list[int] = []

    async def disabled(*_: Any, **__: Any) -> dict[str, Any]:
        calls.append(1)
        return {"count": 1, "disabled": True}

    setattr(att, "_click", disabled)  # type: ignore[method-assign]
    got = await ext_media._video(att, {"path": vid})
    assert got == {"status": "media_upload_incomplete", "kind": "video"}
    assert len(calls) > 1
