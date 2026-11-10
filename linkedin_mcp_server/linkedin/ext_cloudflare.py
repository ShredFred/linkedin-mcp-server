"""Cloudflare's block page in front of LinkedIn (fork extension).

Measured 2026-11-09: in the headless mode the browser announces itself as
``HeadlessChrome`` and some LinkedIn paths (company pages and their admin
views, the feed) answer with Cloudflare's "Sorry, you have been blocked" page,
while the headed browser on the same profile gets the real page. Nothing here
changes the user agent, client hints or fingerprint: the page is recognised,
reported as ``cloudflare_headless_block``, and the one tool call that hit it may
be repeated once with the headed, off-screen window (``--hidden-window``).
"""

from __future__ import annotations

from typing import Any

REASON = "cloudflare_headless_block"

_TITLE_MARKERS = ("attention required! | cloudflare",)
_BODY_MARKERS = (
    "sorry, you have been blocked",
    "you are unable to access linkedin.com",
)

_READ_JS = (
    "() => [document.title || '', "
    "((document.body && document.body.innerText) || '').slice(0, 2000)]"
)


class CloudflareBlock(Exception):
    """A navigation landed on Cloudflare's block page instead of LinkedIn."""

    def __init__(self, url: str, *, headless: bool = True) -> None:
        self.url = url
        self.headless = headless
        self.reason = REASON if headless else "cloudflare_block"
        super().__init__(f"{self.reason}: {url}")


def is_block(title: str | None, body: str | None) -> bool:
    """True for Cloudflare's block page (title or its fixed body wording)."""
    t = (title or "").strip().lower()
    b = (body or "").lower()
    if any(m in t for m in _TITLE_MARKERS):
        return True
    return "cloudflare" in b and any(m in b for m in _BODY_MARKERS)


async def raise_if_blocked(page: Any, url: str) -> None:
    """Read title and body once; raise :class:`CloudflareBlock` on the block page."""
    try:
        title, body = await page.evaluate(_READ_JS)
    except Exception:  # noqa: BLE001 - an unreadable page is not a block
        return
    if is_block(title, body):
        # The browser layer decides the mode; the tool runner re-reads it.
        raise CloudflareBlock(url)
