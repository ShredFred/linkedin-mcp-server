"""ext_post_media: URL classification, selection, download guards (no browser)."""

import asyncio

import pytest

from linkedin_mcp_server.linkedin.ext_post_media import (
    MAX_FILE_BYTES,
    ExtPostMediaReader,
    check_download,
    classify,
    post_kind,
    post_target,
    select_media,
)

M = "https://media.licdn.com/dms/image/v2/"
FEED_800 = M + "D4E22AQA1/feedshare-shrink_800/B4EZ/0/1?e=1&v=beta&t=x"
FEED_2048 = M + "D4E22AQA1/feedshare-shrink_2048_1536/B4EZ/0/1?e=1&v=beta&t=x"
FEED_B = M + "D4E22AQB2/feedshare-shrink_1280/B4EZ/0/1?e=1"
AVATAR = M + "C4D03AQX/profile-displayphoto-shrink_100_100/0/1?e=1"
FRAMED = M + "C4D03AQY/profile-framedphoto-shrink_200_200/0/1?e=1"
LOGO = M + "C4E0BAQZ/company-logo_100_100/0/1?e=1"
POSTER = M + "D4E05AQV/videocover-high/B4EZ/0/1?e=1"
DOC_P1 = M + "D4E1FAQP1/feedshare-document-images_800/1/1?e=1"
DOC_P2 = M + "D4E1FAQP2/feedshare-document-images_800/2/1?e=1"
DOC_FILE = "https://media.licdn.com/dms/document/media/v2/D4E1FAQD/feedshare-document-pdf-analyzed/0/1?e=1"


def test_classify_feedshare_vs_avatars_and_logos():
    assert classify(FEED_800) == "image"
    assert classify(AVATAR) is None
    assert classify(FRAMED) is None
    assert classify(LOGO) is None
    assert classify(POSTER) == "video_poster"
    assert classify(DOC_P1) == "document_page"
    assert classify(DOC_FILE) == "document"
    assert classify("https://evil.example/dms/image/v2/X/feedshare-shrink_800/") is None
    assert classify("https://static.licdn.com/aero-v1/sc/h/abc") is None


def test_select_keeps_largest_rendition_per_asset_in_page_order():
    items = select_media([FEED_800, AVATAR, FEED_B, FEED_2048, LOGO], 20)
    assert [i["url"] for i in items] == [FEED_2048, FEED_B]
    assert post_kind(items) == "multi_image"
    assert select_media([FEED_800, FEED_B], 1) == [{"type": "image", "url": FEED_800}]


def test_document_pages_numbered_and_file_url_kept():
    items = select_media([AVATAR, DOC_P1, DOC_P2, DOC_P1, DOC_FILE], 20)
    pages = [i for i in items if i["type"] == "document_page"]
    assert [p["page_index"] for p in pages] == [0, 1]
    assert any(i["type"] == "document" for i in items)
    assert post_kind(items) == "document"


def test_video_poster_only_and_empty_post():
    assert post_kind(select_media([POSTER, AVATAR], 20)) == "video"
    assert post_kind(select_media([AVATAR], 20)) == "none"


def test_check_download_rejects_type_and_size():
    assert check_download("image/jpeg", 1000) is None
    assert check_download("application/pdf; charset=binary", 10) is None
    html = check_download("text/html; charset=utf-8", 10)
    assert html == "content_type_rejected:text/html"
    assert check_download(None, 10) == "content_type_rejected:missing"
    big = MAX_FILE_BYTES + 1
    assert check_download("image/png", big) == f"too_large:{big}"


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://www.linkedin.com/feed/update/urn:li:activity:7390000000000000001/?x=1",
            "https://www.linkedin.com/feed/update/urn:li:activity:7390000000000000001/",
        ),
        (
            "https://www.linkedin.com/posts/someone_topic-activity-7390000000000000001-AbCd",
            "https://www.linkedin.com/posts/someone_topic-activity-7390000000000000001-AbCd",
        ),
        (
            "urn:li:activity:7390000000000000001",
            "https://www.linkedin.com/feed/update/urn:li:activity:7390000000000000001/",
        ),
    ],
)
def test_post_target_accepts_post_urls_and_ids(url, expected):
    assert post_target(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/feed/update/urn:li:activity:7390000000000000001/",
        "https://www.linkedin.com/in/someone/",
        "not a post",
    ],
)
def test_post_target_refuses_other_urls(url):
    with pytest.raises(ValueError):
        post_target(url)


class _Response:
    def __init__(self, ctype, body, length=None, status=200):
        self.status = status
        self.ok = status < 400
        self.headers = {"content-type": ctype}
        if length is not None:
            self.headers["content-length"] = str(length)
        self._body = body

    async def body(self):
        return self._body


class _Request:
    def __init__(self, responses):
        self._responses = responses

    async def get(self, url):
        return self._responses[url]


class _Session:
    def __init__(self, responses):
        self.page = type(
            "P",
            (),
            {"context": type("C", (), {"request": _Request(responses)})()},
        )()


def test_download_writes_images_and_refuses_html_and_oversize(tmp_path):
    items = [
        {"type": "image", "url": "a"},
        {"type": "image", "url": "b"},
        {"type": "image", "url": "c"},
        {"type": "image", "url": "d"},
    ]
    responses = {
        "a": _Response("image/jpeg", b"\xff\xd8jpeg"),
        "b": _Response("text/html", b"<html>"),
        "c": _Response("image/png", b"x", length=MAX_FILE_BYTES + 1),
        "d": _Response("image/png", b"", status=403),
    }
    notes: list[str] = []
    reader = ExtPostMediaReader(_Session(responses), None)
    asyncio.run(reader.download(items, str(tmp_path / "out"), notes))
    assert items[0]["bytes"] == 6
    assert items[0]["content_type"] == "image/jpeg"
    assert (tmp_path / "out" / "00-image.jpg").read_bytes() == b"\xff\xd8jpeg"
    assert "path" not in items[1] and "path" not in items[2] and "path" not in items[3]
    assert any("content_type_rejected:text/html" in n for n in notes)
    assert any("too_large" in n for n in notes)
    assert any("HTTP 403" in n for n in notes)
