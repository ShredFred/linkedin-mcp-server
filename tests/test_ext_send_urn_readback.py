"""send_message: URN recipient redirect and the post-send read-back (2026-10-22).

1. A recipient named by profile URN is redirected by LinkedIn to the vanity
   slug; the slug compare refused it as "not the requested one". The landed
   page's URN is the identity then.
2. A message that had landed was reported "unverified" with thread null: the
   read-back ignored the sender's confirmed thread_id.

Placeholder names only.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from linkedin_mcp_server import ext_outreach as outreach
from linkedin_mcp_server.linkedin import message_sender as ms
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession
from linkedin_mcp_server.tools import ext as ext_tools

URN = "ACoAABcdEfGhIjKlMnOpQrStUvWxYz0123456"
OTHER_URN = "ACoAAZyxWvUtSrQpOnMlKjIhGfEdCb9876543"


def _target(path: str, urn: str) -> ms._ProfileMessageTargetResolution:
    return ms._ProfileMessageTargetResolution(
        "resolved",
        ms._ProfileMessageTarget(
            path,
            urn,
            f"https://www.linkedin.com/messaging/compose/?recipient={urn}",
            "Max Muster",
        ),
    )


async def _send(mock_page, requested: str, resolution) -> dict[str, Any]:
    session = PageSession(mock_page)
    sender = ms.MessageSender(session, PageNavigator(session))
    with (
        patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
        patch.object(
            ms.MessageSender,
            "_read_profile_message_target",
            new_callable=AsyncMock,
            return_value=resolution,
        ),
    ):
        return await sender.send_message(requested, "Hallo", confirm_send=True)


def test_requested_profile_urn_shape() -> None:
    assert ms._requested_profile_urn(URN) == URN
    assert ms._requested_profile_urn(f"urn:li:fsd_profile:{URN}") == URN
    assert ms._requested_profile_urn("max-muster") is None
    assert ms._requested_profile_urn("acoustic-max") is None


async def test_urn_redirect_to_slug_is_the_same_profile(mock_page) -> None:
    result = await _send(mock_page, URN, _target("/in/max-muster/", URN))
    assert "not the requested one" not in str(result.get("message", ""))


async def test_urn_redirect_to_another_urn_is_refused(mock_page) -> None:
    result = await _send(mock_page, URN, _target("/in/max-muster/", OTHER_URN))
    assert result["status"] == "recipient_resolution_failed"
    assert "not the requested one" in result["message"]


async def test_slug_redirect_to_another_slug_still_refused(mock_page) -> None:
    result = await _send(mock_page, "max-muster", _target("/in/erika-muster/", URN))
    assert result["status"] == "recipient_resolution_failed"


class _Ledger:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def append(self, row: dict[str, Any]) -> None:
        self.rows.append(row)


class _Page:
    url = "https://www.linkedin.com/messaging/compose/"


class _Session:
    page = _Page()


class _Extractor:
    def __init__(self, threads: dict[str, dict], inbox: list[str]) -> None:
        self.threads = threads
        self.inbox = inbox
        self.ext_session = _Session()
        self.calls: list[dict] = []

    async def get_conversation(self, **lookup: str) -> dict:
        self.calls.append(lookup)
        if "thread_id" in lookup:
            return self.threads[lookup["thread_id"]]
        raise RuntimeError("no username lookup in this fixture")

    async def get_inbox(self, limit: int) -> dict:
        return {
            "references": {
                "inbox": [
                    {
                        "kind": "conversation",
                        "url": f"https://www.linkedin.com/messaging/thread/{t}/",
                    }
                    for t in self.inbox
                ]
            }
        }


def _conv(text: str, slug: str = "max-muster") -> dict:
    return {
        "sections": {"conversation": text},
        "references": {
            "conversation": [{"url": f"https://www.linkedin.com/in/{slug}/"}]
        },
    }


MESSAGE = "Hallo Max,\n\nschöne Grüße  aus Köln."


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(ext_tools.asyncio, "sleep", AsyncMock())


async def test_read_back_uses_the_senders_thread_id() -> None:
    rendered = "Du\nHallo Max, schöne Grüße aus Köln.​"
    ex = _Extractor({"2-abc": _conv(rendered)}, inbox=[])
    ledger = _Ledger()
    sent = {"sent": True, "thread_id": "2-abc", "url": _Page.url}
    out = await ext_tools._read_back(ex, ledger, "a1", "max-muster", MESSAGE, sent)
    assert out["status"] == "verified"
    assert out["thread_id"] == "2-abc"
    assert ledger.rows[-1]["thread"] == "2-abc"


async def test_read_back_decomposed_umlauts_match() -> None:
    rendered = "Hallo Max, schöne Grüße aus Köln."
    ex = _Extractor({"2-abc": _conv(rendered)}, inbox=[])
    out = await ext_tools._read_back(
        ex, _Ledger(), "a1", "max-muster", MESSAGE, {"sent": True, "thread_id": "2-abc"}
    )
    assert out["verified"] is True


async def test_read_back_finds_the_thread_in_the_inbox() -> None:
    ex = _Extractor(
        {
            "2-new": _conv("Frage von jemand anderem", slug="erika-muster"),
            "2-ours": _conv("Hallo Max, schöne Grüße aus Köln."),
        },
        inbox=["2-new", "2-ours"],
    )
    out = await ext_tools._read_back(
        ex, _Ledger(), "a1", "max-muster", MESSAGE, {"sent": True, "url": _Page.url}
    )
    assert out["status"] == "verified"
    assert out["thread_id"] == "2-ours"


async def test_inbox_thread_of_another_person_does_not_verify() -> None:
    ex = _Extractor(
        {"2-x": _conv("Hallo Max, schöne Grüße aus Köln.", slug="erika-muster")},
        inbox=["2-x"],
    )
    out = await ext_tools._read_back(
        ex, _Ledger(), "a1", "max-muster", MESSAGE, {"sent": True}
    )
    assert out["status"] == "unverified"
    assert out["thread_id"] is None


async def test_inbox_partner_matched_by_urn_or_encoded_slug() -> None:
    ex = _Extractor(
        {"2-u": _conv("Hallo", slug="max-%C3%BCber")},
        inbox=["2-u"],
    )
    out = await ext_tools._read_back(
        ex, _Ledger(), "a1", "max-über", "Hallo", {"sent": True}
    )
    assert out["verified"] is True
    assert ext_tools._ref_names(f"https://www.linkedin.com/in/{URN}/", URN.lower())


def test_ledger_hash_unchanged_by_readback_normalisation() -> None:
    assert outreach.text_sha("a​b") != outreach.text_sha("ab")
