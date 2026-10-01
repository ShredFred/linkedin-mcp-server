"""MiViA fork: Haertungsrunde 3 an den Lese- und Suchwerkzeugen.

Two findings, each pinned here:

1. A profile or company URL that LinkedIn redirects to another slug was
   returned under the requested name with no trace of the redirect.
2. Blank keywords for search_companies / search_posts / search_conversations
   navigated to an unfiltered page whose content read as a search answer,
   after spending the readiness (browser/pacer) step.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from linkedin_mcp_server.linkedin.contracts import ExtractedSection
from linkedin_mcp_server.linkedin.identifiers import landed_identity_mismatch
from linkedin_mcp_server.linkedin.search_urls import (
    build_company_search_url,
    build_content_search_url,
    build_job_search_url,
    build_people_search_url,
)


# --- redirect detection -----------------------------------------------------


@pytest.mark.parametrize(
    ("landed", "kind", "requested"),
    [
        ("https://www.linkedin.com/in/max-mueller/", "in", "max-mueller"),
        (
            "https://www.linkedin.com/in/Max-Mueller/?isSelfProfile=false",
            "in",
            "max-mueller",
        ),
        ("https://www.linkedin.com/in/j%C3%BCrgen-m/", "in", "jürgen-m"),
        ("https://www.linkedin.com/in/j%C3%BCrgen-m/", "in", "j%C3%BCrgen-m"),
        ("https://www.linkedin.com/in/max_m/details/experience/", "in", "max_m"),
        ("https://www.linkedin.com/company/sap/about/", "company", "sap"),
        ("https://www.linkedin.com/feed/", "in", "abc"),  # not a subject page
        (None, "in", "abc"),
        ("", "in", "abc"),
    ],
)
def test_same_subject_is_no_redirect(landed, kind, requested):
    assert landed_identity_mismatch(landed, kind, requested) is None


@pytest.mark.parametrize(
    ("landed", "kind", "requested", "expected"),
    [
        ("https://www.linkedin.com/in/someone-else/", "in", "max", "someone-else"),
        ("https://www.linkedin.com/company/sap-se/", "company", "sap", "sap-se"),
        ("https://www.linkedin.com/school/tum/", "company", "tum", "school/tum"),
    ],
)
def test_other_subject_is_reported(landed, kind, requested, expected):
    note = landed_identity_mismatch(landed, kind, requested)
    assert note is not None
    assert note["landed"] == expected
    assert note["requested"] == requested


def _company_reader(url: str):
    from linkedin_mcp_server.linkedin.capture import SectionCapture
    from linkedin_mcp_server.linkedin.company import CompanyReader
    from linkedin_mcp_server.linkedin.content import PageContentReader
    from linkedin_mcp_server.linkedin.navigation import PageNavigator
    from linkedin_mcp_server.linkedin.session import PageSession

    page = MagicMock()
    page.url = url
    session = PageSession(page)
    navigator = PageNavigator(session)
    return CompanyReader(
        session, SectionCapture(session, navigator, PageContentReader(session))
    )


async def test_read_company_reports_a_redirect_to_another_page():
    reader = _company_reader("https://www.linkedin.com/company/other-co/")
    with patch.object(
        reader._capture,
        "capture",
        AsyncMock(return_value=ExtractedSection(text="About text", references=[])),
    ):
        result = await reader.read_company("sap", {"about"})
    assert result["sections"]["about"] == "About text"
    assert result["redirected_to"]["landed"] == "other-co"


async def test_read_company_without_redirect_has_no_note():
    reader = _company_reader("https://www.linkedin.com/company/sap/about/")
    with patch.object(
        reader._capture,
        "capture",
        AsyncMock(return_value=ExtractedSection(text="About text", references=[])),
    ):
        result = await reader.read_company("sap", {"about"})
    assert "redirected_to" not in result


# --- URL encoding of search terms ------------------------------------------


@pytest.mark.parametrize(
    "builder",
    [
        build_people_search_url,
        build_company_search_url,
        build_content_search_url,
        build_job_search_url,
    ],
)
def test_search_terms_cannot_inject_parameters(builder):
    url = builder("Härterei & Co #1 ?x=y&network=F")
    query = url.split("?", 1)[1]
    assert "#" not in url
    assert "&network=" not in query
    assert "&x=" not in query
    assert "H%C3%A4rterei+%26+Co+%231+%3Fx%3Dy%26network%3DF" in query


# --- blank keywords are refused before the readiness step -------------------


@pytest.mark.parametrize(
    ("module", "register", "tool"),
    [
        ("company", "register_company_tools", "search_companies"),
        ("post", "register_post_tools", "search_posts"),
        ("messaging", "register_messaging_tools", "search_conversations"),
    ],
)
@pytest.mark.parametrize("keywords", ["", "   ", "\t\n"])
async def test_blank_keywords_are_refused_without_a_page_load(
    monkeypatch, module, register, tool, keywords
):
    import importlib

    mod = importlib.import_module(f"linkedin_mcp_server.tools.{module}")
    ready = AsyncMock()
    monkeypatch.setattr(mod, "get_ready_extractor", ready)
    mcp = FastMCP("t")
    getattr(mod, register)(mcp)
    fn = (await mcp.get_tool(tool)).fn
    ctx = MagicMock()
    ctx.report_progress = AsyncMock()
    with pytest.raises(ToolError, match="keywords must not be empty"):
        await fn(keywords=keywords, ctx=ctx)
    ready.assert_not_called()
