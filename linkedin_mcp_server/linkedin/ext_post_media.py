"""Fork extension: read the media of one post (images, document pages, video poster).

Read only, rendered page only. Media is told apart by the path of its
``media.licdn.com/dms/...`` URL, never by class names or visible text: the
asset kind (``feedshare-shrink_...``, ``profile-displayphoto-...``,
``company-logo_...``, ``videocover-...``, ``feedshare-document-images...``) is
part of the URL and does not change with the UI language.

Downloads go through ``page.context.request``, which shares the browser
context's cookies and identity; no header or user agent is set here.
"""

from __future__ import annotations

import random
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from linkedin_mcp_server.linkedin.ext_engagement import parse_activity_id

MAX_FILE_BYTES = 15 * 1024 * 1024
_ALLOWED_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "application/pdf": "pdf",
}

# Asset kinds that are never post media (people and page pictures).
_EXCLUDED = ("profile-", "company-logo", "-logo_", "background", "ghost")
_SIZE_RE = re.compile(r"_(\d{2,5})(?:_(\d{2,5}))?(?:/|$)")
_ASSET_RE = re.compile(r"/dms/(?:image|document)/(?:v2/)?([^/]+)/")

# Every media URL inside the post container, from src, srcset, poster and any
# attribute carrying one (lazy-load and document viewer configs). Generic
# selectors only; the container is the element carrying the activity URN.
_MEDIA_JS = r"""(aid) => {
  const root = (aid && document.querySelector('[data-urn*="' + aid + '"]'))
    || document.querySelector('main') || document.body;
  const urls = [];
  const take = (v) => {
    for (const m of String(v).matchAll(/https:\/\/media\.licdn\.com\/dms\/[^\s"'<>,]+/g)) {
      urls.push(m[0].replace(/&amp;/g, '&'));
    }
  };
  for (const el of root.querySelectorAll('*')) {
    for (const a of el.attributes) {
      if (a.value && a.value.includes('media.licdn.com/dms/')) take(a.value);
    }
  }
  return {urls, scoped: root !== document.body && root.tagName !== 'MAIN'};
}"""


def post_target(post_url: str) -> str:
    """The URL to open: a linkedin.com post URL as given, else built from the id."""
    raw = (post_url or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme in ("http", "https"):
        host = (parsed.hostname or "").lower()
        if host != "linkedin.com" and not host.endswith(".linkedin.com"):
            raise ValueError("post_url must be a linkedin.com URL")
        if not (
            parsed.path.startswith("/feed/update/") or parsed.path.startswith("/posts/")
        ):
            raise ValueError("post_url must be a /feed/update/ or /posts/ URL")
        return f"https://www.linkedin.com{parsed.path}"
    activity = parse_activity_id(raw)
    return f"https://www.linkedin.com/feed/update/urn:li:activity:{activity}/"


def classify(url: str) -> str | None:
    """Media type from the URL path; None for avatars, logos and non-media."""
    parsed = urlparse(url)
    if parsed.hostname != "media.licdn.com" or not parsed.path.startswith("/dms/"):
        return None
    path = parsed.path.lower()
    if path.startswith("/dms/document/"):
        return "document"
    if any(x in path for x in _EXCLUDED):
        return None
    if "feedshare-document" in path:
        return "document_page"
    if "videocover" in path or "video-cover" in path:
        return "video_poster"
    if "feedshare" in path or "articleshare" in path or "image-shrink" in path:
        return "image"
    return None


def _size(url: str) -> int:
    m = _SIZE_RE.search(urlparse(url).path)
    if not m:
        return 0
    return int(m.group(1)) * int(m.group(2) or m.group(1))


def _asset(url: str) -> str:
    m = _ASSET_RE.search(urlparse(url).path)
    return m.group(1) if m else url.split("?")[0]


def select_media(urls: list[str], max_items: int) -> list[dict[str, Any]]:
    """One item per asset (largest rendition), page order; document pages numbered."""
    order: list[str] = []
    best: dict[str, tuple[str, str]] = {}
    for url in urls:
        kind = classify(url)
        if kind is None:
            continue
        key = _asset(url)
        if key not in best:
            order.append(key)
            best[key] = (kind, url)
        elif _size(url) > _size(best[key][1]):
            best[key] = (kind, url)
    items: list[dict[str, Any]] = []
    page = 0
    for key in order:
        kind, url = best[key]
        item: dict[str, Any] = {"type": kind, "url": url}
        if kind == "document_page":
            item["page_index"] = page
            page += 1
        items.append(item)
        if len(items) >= max_items:
            break
    return items


def post_kind(items: list[dict[str, Any]]) -> str:
    types = {i["type"] for i in items}
    if types & {"document", "document_page"}:
        return "document"
    if "video_poster" in types:
        return "video"
    images = sum(1 for i in items if i["type"] == "image")
    if images > 1:
        return "multi_image"
    return "image" if images == 1 else "none"


def check_download(content_type: str | None, size: int) -> str | None:
    """None when the file may be written, else the refusal reason."""
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype not in _ALLOWED_TYPES:
        return f"content_type_rejected:{ctype or 'missing'}"
    if size > MAX_FILE_BYTES:
        return f"too_large:{size}"
    return None


def file_name(index: int, item: dict[str, Any], content_type: str) -> str:
    ext = _ALLOWED_TYPES[content_type.split(";")[0].strip().lower()]
    return f"{index:02d}-{item['type']}.{ext}"


class ExtPostMediaReader:
    def __init__(self, session: Any, navigator: Any) -> None:
        self._session = session
        self._navigator = navigator

    @property
    def _page(self) -> Any:
        return self._session.page

    async def _urls(self, aid: str | None) -> tuple[list[str], bool]:
        urls: list[str] = []
        scoped = False
        # The document viewer renders in a same-origin frame; read every frame.
        for frame in list(getattr(self._page, "frames", None) or [self._page]):
            try:
                found = await frame.evaluate(_MEDIA_JS, aid)
            except Exception:  # noqa: BLE001 -- a detached frame has no media
                continue
            urls.extend(found.get("urls") or [])
            scoped = scoped or bool(found.get("scoped"))
        return urls, scoped

    async def download(
        self, items: list[dict[str, Any]], save_dir: str, notes: list[str]
    ) -> None:
        target = Path(save_dir).expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)
        request = self._page.context.request
        for index, item in enumerate(items):
            try:
                response = await request.get(item["url"])
            except Exception as broken:  # noqa: BLE001
                notes.append(f"item {index}: download failed ({type(broken).__name__})")
                continue
            if not response.ok:
                notes.append(f"item {index}: HTTP {response.status}")
                continue
            ctype = response.headers.get("content-type", "")
            try:
                declared = int(response.headers.get("content-length") or 0)
            except ValueError:
                declared = 0
            reason = check_download(ctype, declared)
            body = b""
            if reason is None:
                body = await response.body()
                reason = check_download(ctype, len(body))
            if reason is not None:
                notes.append(f"item {index}: not saved ({reason})")
                continue
            path = target / file_name(index, item, ctype)
            path.write_bytes(body)
            item.update(
                path=str(path),
                bytes=len(body),
                content_type=ctype.split(";")[0].strip().lower(),
            )

    async def read(
        self, post_url: str, save_dir: str | None, max_items: int
    ) -> dict[str, Any]:
        url = post_target(post_url)
        try:
            aid: str | None = parse_activity_id(url)
        except ValueError:
            aid = None
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        await self._session.delay(random.uniform(2.5, 4.0))
        urls, scoped = await self._urls(aid)
        items = select_media(urls, max_items)
        notes: list[str] = []
        if not scoped:
            notes.append("post container not found by activity URN; read main area")
        if any(i["type"] == "document_page" for i in items) and not any(
            i["type"] == "document" for i in items
        ):
            notes.append("document file URL not exposed; page images only")
        if any(i["type"] == "video_poster" for i in items):
            notes.append("video: poster image only, the stream is not read")
        if len(items) >= max_items:
            notes.append(f"cut at max_items={max_items}")
        if save_dir:
            await self.download(items, save_dir, notes)
        return {
            "url": url,
            "kind": post_kind(items),
            "items": items,
            "count": len(items),
            "notes": notes,
        }
