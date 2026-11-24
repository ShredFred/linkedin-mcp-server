"""
LinkedIn feed reading tool.

Fetches posts from the authenticated user's LinkedIn home feed using
innerText extraction. Scrolls until the requested number of post
permalinks have been observed in SDUI pagination responses — a
locale-independent progress signal, since the feed DOM exposes no
stable per-post container selector.
"""

import logging
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.dependencies import get_ready_extractor, tool_errors
from linkedin_mcp_server.linkedin.contracts import ExtractedSection
from linkedin_mcp_server.tools.read_pace import paced_read, take_read
from linkedin_mcp_server.linkedin.contracts import RATE_LIMITED_SECTION_TEXT
from linkedin_mcp_server.linkedin.contracts import rate_limited_section_error
from linkedin_mcp_server.linkedin.link_metadata import Reference

logger = logging.getLogger(__name__)


def register_feed_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register feed-related tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Feed",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"feed"},
    )
    @tool_errors
    async def get_feed(
        ctx: Context,
        num_posts: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> dict[str, Any]:
        """
        Get posts from the authenticated user's LinkedIn feed.

        Args:
            ctx: FastMCP context for progress reporting
            num_posts: Number of posts to fetch (1-50, default 10).
                       The feed is scrolled until this many distinct posts
                       (deduplicated by permalink) have loaded, three scrolls
                       in a row bring nothing new, or the scroll ceiling
                       (num_posts + 10, at least 12, at most 60) is reached.
                       A batch may slightly exceed the target.

        Pacing: the first page load books one ``page_read``, and every
        further scroll books one more. When the budget runs out mid-scroll
        the posts loaded so far are returned with ``pace_stopped: true``;
        when it is spent before the load, the ``pace_budget_spent`` dict is
        returned instead.

        Returns:
            Dict with url, sections (name -> raw text), and optional keys:
            - references["feed"]: list of {kind: "feed_post", url, ...}
              entries. URLs are relative paths and may carry either
              ``/feed/update/<urn>/`` (DOM-anchor-derived) or
              ``/posts/<slug>`` (SDUI-derived) shape — both are valid
              LinkedIn permalinks. An entry paired with a rendered post
              carries that post's URN as ``value`` and may carry ``text``
              (an excerpt of the post).
            - section_errors: present when the feed is rate-limited or
              extraction fails.

            Truncated posts are not auto-expanded; full text for any post
            is reachable via its permalink in references["feed"]. The LLM
            should parse sections["feed"] for post bodies.
        """
        extractor = await get_ready_extractor(ctx, tool_name="get_feed")
        logger.info("Reading feed (num_posts=%d)", num_posts)

        await ctx.report_progress(
            progress=0, total=100, message="Reading the home feed"
        )

        pace_stopped = False

        def scroll_gate() -> bool:
            nonlocal pace_stopped
            if take_read("page_read", tool="get_feed") is None:
                return True
            pace_stopped = True
            return False

        extracted = await paced_read(
            "page_read",
            "get_feed",
            lambda: extractor.extract_feed(
                num_posts=num_posts, scroll_gate=scroll_gate
            ),
        )
        if not isinstance(extracted, ExtractedSection):
            return extracted  # the pacer's refusal dict

        url = "https://www.linkedin.com/feed/"
        sections: dict[str, str] = {}
        references: dict[str, list[Reference]] = {}
        section_errors: dict[str, dict[str, Any]] = {}
        if extracted.text and extracted.text != RATE_LIMITED_SECTION_TEXT:
            sections["feed"] = extracted.text
            if extracted.references:
                references["feed"] = extracted.references
        elif extracted.text == RATE_LIMITED_SECTION_TEXT:
            section_errors["feed"] = rate_limited_section_error()
        elif extracted.error:
            section_errors["feed"] = extracted.error

        await ctx.report_progress(progress=100, total=100, message="Complete")

        result: dict[str, Any] = {"url": url, "sections": sections}
        if references:
            result["references"] = references
        if section_errors:
            result["section_errors"] = section_errors
        if pace_stopped:
            result["pace_stopped"] = True
        return result
