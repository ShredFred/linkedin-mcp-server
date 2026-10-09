"""Fork extension: the read-only composer probe.

A diagnostic tool that drives a logged-in LinkedIn session is only harmless
while it cannot click the wrong thing. Two barriers carry that, and both are
tested: the caller's label is checked, and the element's own wording is checked
again in the page -- because a control can be named differently from the label
that found it.
"""

from __future__ import annotations

from typing import Any

import pytest

from linkedin_mcp_server.linkedin import ext_composer_probe as mod
from linkedin_mcp_server.linkedin.ext_composer_probe import (
    ExtComposerProbe,
    check_click_label,
    check_url,
)


class FakePage:
    def __init__(self, *, mark: dict[str, Any] | None = None) -> None:
        self.mark = mark if mark is not None else {"count": 1}
        self.clicked: list[str] = []
        self.goto_urls: list[str] = []
        self.url = "https://www.linkedin.com/feed/"

    async def click(self, selector: str) -> None:
        self.clicked.append(selector)

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if script is mod._MARK_BY_LABEL_JS:
            return self.mark
        if script is mod._REPORT_JS:
            return [{"tag": "button", "label": "Verwerfen", "text": ""}]
        return []


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


def probe(page: FakePage) -> ExtComposerProbe:
    return ExtComposerProbe(FakeSession(page), FakeNavigator(page))  # type: ignore[arg-type]


# -- only LinkedIn, and only harmless labels ----------------------------------


def test_only_linkedin_is_probed() -> None:
    assert check_url("https://www.linkedin.com/company/mivia/") is None
    assert check_url("https://evil.example/linkedin.com")["field"] == "url"
    assert check_url("https://notlinkedin.com/x")["field"] == "url"


def test_a_label_that_could_publish_is_refused() -> None:
    for label in ["Posten", "veröffentlichen", "Schedule post", "Beitrag planen",
                  "Senden", "Löschen", "Vernetzen", "Folgen"]:
        assert check_click_label(label)["status"] == "refused_click", label


def test_a_harmless_label_passes() -> None:
    assert check_click_label("Beitrag erstellen") is None
    assert check_click_label(None) is None


# -- the second barrier lives in the page -------------------------------------


@pytest.mark.asyncio
async def test_an_element_whose_own_wording_could_publish_is_not_clicked() -> None:
    page = FakePage(mark={"count": 1, "refused": "posten"})
    result = await probe(page).probe(
        "https://www.linkedin.com/company/mivia/", click_label="Beitrag erstellen", limit=10
    )
    assert result["status"] == "refused_click"
    assert not page.clicked
    # The report is still returned: that is the whole point of the tool.
    assert result["elements"]


@pytest.mark.asyncio
async def test_an_ambiguous_click_target_is_not_guessed_but_still_reported() -> None:
    page = FakePage(mark={"count": 3})
    result = await probe(page).probe(
        "https://www.linkedin.com/company/mivia/", click_label="Beitrag erstellen", limit=10
    )
    assert result["status"] == "click_target_not_unique"
    assert result["matches"] == 3
    assert not page.clicked
    assert result["elements"]


@pytest.mark.asyncio
async def test_a_plain_probe_navigates_reports_and_clicks_nothing() -> None:
    page = FakePage()
    result = await probe(page).probe(
        "https://www.linkedin.com/company/mivia/", click_label=None, limit=10
    )
    assert result["status"] == "probed"
    assert result["clicked"] is None
    assert not page.clicked
    assert page.goto_urls == ["https://www.linkedin.com/company/mivia/"]


@pytest.mark.asyncio
async def test_one_click_at_most_and_only_the_marked_element() -> None:
    page = FakePage()
    await probe(page).probe(
        "https://www.linkedin.com/company/mivia/", click_label="Beitrag erstellen", limit=10
    )
    assert page.clicked == ["[data-ext-probe]"]
