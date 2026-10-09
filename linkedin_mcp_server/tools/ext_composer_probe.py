"""Fork tool: composer_probe -- read-only look at a LinkedIn page's controls.

A development instrument, not an outreach tool. It exists because the write
tools refuse to guess: when one of them reports "control not uniquely
identifiable", this is how the actual wording gets measured without a release
and a client restart per attempt.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.linkedin.ext_composer_probe import (
    ExtComposerProbe,
    check_click_label,
    check_url,
)
from linkedin_mcp_server.tools.ext import TAG, _pace, _run

logger = logging.getLogger(__name__)


def _probe(extractor: Any) -> ExtComposerProbe:
    return ExtComposerProbe(extractor.ext_session, extractor.ext_navigator)


def register_ext_composer_probe_tools(
    mcp: FastMCP,
    *,
    tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
) -> None:
    @mcp.tool(
        timeout=tool_timeout,
        title="Composer Probe",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={TAG, "diagnostics"},
    )
    async def composer_probe(
        url: str,
        ctx: Context,
        click_label: str | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 60,
    ) -> dict[str, Any]:
        """
        Report the visible interactive controls of a LinkedIn page: tag, role,
        aria-label, text, componentkey, disabled state and whether the element
        sits inside a dialog. Read-only; nothing is typed anywhere.

        Optionally opens one thing first: click_label is matched as the exact
        aria-label or exact text of one visible control, and the click happens
        only when exactly one matches. A label that could publish, send,
        schedule, delete, follow, connect or apply is refused -- checked
        against the caller's label and against the element's own wording, so a
        renamed button cannot slip through.

        Args:
            url: A linkedin.com URL. Anything else is refused.
            click_label: Exact aria-label or text of one control to open first.
            limit: Maximum elements reported (default 60).

        Statuses: probed, click_target_not_unique (nothing clicked; the report
        is still returned so the right wording can be read off it),
        refused_click, invalid_input, pace_budget_spent, pace_lock_busy.
        """
        bad = check_url(url) or check_click_label(click_label)
        if bad:
            return bad
        refusal = _pace("page_read", tool="composer_probe")
        if refusal:
            return refusal
        return await _run(
            ctx,
            "composer_probe",
            lambda ex: _probe(ex).probe(url, click_label=click_label, limit=limit),
        )
