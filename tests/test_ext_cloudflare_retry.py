"""Cloudflare's block page: recognised, and one call repeated off-screen.

Headless stays the default. A tool call whose first page is Cloudflare's block
page is repeated once with the headed, off-screen window; afterwards the
browser is headless again. A block after a page already loaded is never
repeated, because a click may have happened.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError

from linkedin_mcp_server.linkedin import ext_cloudflare as cf
from linkedin_mcp_server.tools import ext


def test_block_page_is_recognised() -> None:
    assert cf.is_block("Attention Required! | Cloudflare", "")
    assert cf.is_block(
        "",
        "Sorry, you have been blocked\nYou are unable to access linkedin.com\nCloudflare Ray ID: x",
    )
    assert not cf.is_block("Feed | LinkedIn", "Sorry, you have been blocked")
    assert not cf.is_block("Acme Labs | LinkedIn", "Admins verwalten")
    assert not cf.is_block(None, None)


@pytest.fixture
def browser_state(monkeypatch):
    import linkedin_mcp_server.config as config_mod
    import linkedin_mcp_server.drivers.browser as drv

    state = {"headless": True, "closed": 0, "modes": []}
    cfg = SimpleNamespace(browser=SimpleNamespace(hidden_window=False))
    monkeypatch.setattr(config_mod, "get_config", lambda: cfg)
    monkeypatch.setattr(drv, "current_headless", lambda: state["headless"])
    monkeypatch.setattr(drv, "set_headless", lambda v: state.__setitem__("headless", v))

    async def close():
        state["closed"] += 1

    monkeypatch.setattr(drv, "close_browser", close)

    async def ready(ctx, tool_name):
        state["modes"].append((state["headless"], cfg.browser.hidden_window))
        return SimpleNamespace(ext_navigator=SimpleNamespace(ext_pages_loaded=0))

    monkeypatch.setattr(ext, "get_ready_extractor", ready)
    state["cfg"] = cfg
    return state


async def test_first_page_block_is_repeated_once_off_screen(browser_state) -> None:
    calls = []

    async def body(extractor):
        calls.append(browser_state["headless"])
        if browser_state["headless"]:
            raise cf.CloudflareBlock(
                "https://www.linkedin.com/company/1/admin/", headless=True
            )
        return {"status": "ok"}

    got = await ext._run(None, "company_access", body)
    assert got == {"status": "ok", "browser_retry": "cloudflare_headless_block"}
    assert calls == [True, False]
    assert browser_state["modes"] == [(True, False), (False, True)]
    # headless again afterwards, the headed browser closed
    assert browser_state["headless"] is True
    assert browser_state["cfg"].browser.hidden_window is False
    assert browser_state["closed"] == 2


async def test_block_after_a_loaded_page_is_not_repeated(browser_state) -> None:
    calls = []

    async def body(extractor):
        calls.append(1)
        extractor.ext_navigator.ext_pages_loaded = 1
        raise cf.CloudflareBlock("https://www.linkedin.com/feed/", headless=True)

    with pytest.raises(ToolError, match="cloudflare_headless_block"):
        await ext._run(None, "create_post", body)
    assert calls == [1]
    assert browser_state["closed"] == 0


async def test_block_in_the_hidden_window_is_reported(browser_state) -> None:
    async def body(extractor):
        raise cf.CloudflareBlock(
            "https://www.linkedin.com/company/1/", headless=browser_state["headless"]
        )

    with pytest.raises(ToolError, match="hidden-window retry"):
        await ext._run(None, "company_access", body)
    assert browser_state["headless"] is True
