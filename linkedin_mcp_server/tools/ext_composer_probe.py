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
    check_type_mention,
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
        click_labels: Annotated[list[str] | None, Field(max_length=4)] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 60,
        type_mention: str | None = None,
        pick_option: Annotated[int | None, Field(ge=0, le=11)] = None,
    ) -> dict[str, Any]:
        """
        Report the visible interactive controls of a LinkedIn page: tag, role,
        aria-label, text, componentkey, disabled state and whether the element
        sits inside a dialog. Read-only except for one optional mention probe.

        Optionally opens things first: each entry of click_labels is matched
        as the exact aria-label or exact text of one visible control and
        clicked in order, each only when exactly one matches. A sequence is
        needed because a dialog is only reachable through the control that
        opens it; at most four. A label that could publish, send,
        schedule, delete, follow, connect or apply is refused -- checked
        against the caller's label and against the element's own wording, so a
        renamed button cannot slip through.

        Args:
            url: A linkedin.com URL. Anything else is refused.
            click_labels: Exact aria-labels or texts to click, in order.
            limit: Maximum elements reported (default 60).
            type_mention: Optional '@' plus 1-30 name characters, typed key
                by key into the one visible composer editor after the clicks;
                the typeahead list is reported in three snapshots (0.3/1.3/
                3.8 s), then the editor is cleared and the composer
                discarded. Nothing is ever published.
            pick_option: With type_mention: click the suggestion at this
                position (0-based) and report the editor's entity markup.
                Measurement only; the composer is cleared and discarded.

        Statuses: probed, click_target_not_unique (nothing clicked; the report
        is still returned so the right wording can be read off it),
        refused_click, invalid_input, pace_budget_spent, pace_lock_busy.
        """
        labels = list(click_labels or [])
        bad = check_url(url)
        for label in labels:
            bad = bad or check_click_label(label)
        bad = bad or check_type_mention(type_mention)
        if bad:
            return bad
        refusal = _pace("page_read", tool="composer_probe")
        if refusal:
            return refusal
        return await _run(
            ctx,
            "composer_probe",
            lambda ex: _probe(ex).probe(
                url, click_labels=labels, limit=limit, type_mention=type_mention,
                pick_option=pick_option,
            ),
        )
