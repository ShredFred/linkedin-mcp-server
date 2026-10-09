"""Fork tools: company_access and react_to_post (2026-10-09, round 3).

``company_access`` reads which role the signed-in session holds on a company
page; every ``as_company`` path asks the same question first. Which LinkedIn
account that is follows from the browser profile the server was started with
(``--user-data-dir`` / ``USER_DATA_DIR``); nothing here names an account.
"""

from __future__ import annotations

from typing import Any

from fastmcp import Context, FastMCP

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.linkedin.ext_company_actions import REACTIONS, ExtCompanyActions
from linkedin_mcp_server.linkedin.ext_engagement import parse_activity_id
from linkedin_mcp_server.tools.ext import TAG, _pace, _peek, _run


def _company(extractor: Any) -> ExtCompanyActions:
    return ExtCompanyActions(extractor.ext_session, extractor.ext_navigator)


async def require_company_role(ex: Any, page_id: str) -> dict[str, Any] | None:
    """None when the session may act as the page; else company_role_insufficient."""
    access = await _company(ex).read_access(page_id)
    if access.get("can_act_as_page"):
        return None
    return {
        "status": "company_role_insufficient",
        "role": access.get("role"),
        "role_source": access.get("role_source"),
        "message": "This session's role on the page does not allow acting as the page.",
    }


def register_ext_company_actions_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    from linkedin_mcp_server.tools.ext import _GuardedMcp

    mcp = _GuardedMcp(mcp)  # type: ignore[assignment]
    timeout = max(tool_timeout, 150.0)

    @mcp.tool(
        timeout=timeout,
        title="Company Access",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={TAG, "company", "diagnostics"},
    )
    async def company_access(page_id: str, ctx: Context) -> dict[str, Any]:
        """
        Read the signed-in session's role on a company page (numeric page id):
        super_admin, content_admin, analyst, curator, admin_unreadable (the
        admin list is not readable but the admin view offers posting), or
        none. can_act_as_page says whether as_company paths may run. Read
        from the page's "Admins verwalten" table; nothing is changed.
        Statuses: ok, invalid_input, pace_budget_spent, pace_lock_busy.
        """
        if not str(page_id).strip().isdigit():
            return {"status": "invalid_input", "field": "page_id"}
        refusal = _pace("page_read", 2, tool="company_access")
        if refusal:
            return refusal
        return await _run(
            ctx, "company_access", lambda ex: _company(ex).read_access(str(page_id).strip())
        )

    @mcp.tool(
        timeout=timeout,
        title="React To Post",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={TAG, "post", "actions"},
    )
    async def react_to_post(
        post_url: str,
        ctx: Context,
        reaction: str = "like",
        as_company: str | None = None,
        company_name: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """
        React to a post as the signed-in member, or as a company page
        (as_company = numeric page id, company_name required; only posts on
        the page's own admin list, only "like" -- the only reaction measured
        there). Dry run by default: the reaction control is located and the
        palette opened by hovering, nothing is clicked. With confirm=true the
        reaction is clicked once and the button state read back.

        reaction: like, celebrate, support, love, insightful, funny.

        Status: verified, unverified (clicked, state not read back: look at
        the post, never repeat), dry_run; stops before any click:
        already_reacted, reaction_control_unavailable, reaction_unavailable,
        reaction_unmeasured_as_page, post_not_in_admin_view,
        admin_card_controls_unavailable, already_reacted_or_unknown,
        identity_not_page, identity_unreadable, identity_modal_stuck,
        company_role_insufficient, invalid_input, invalid_post,
        pace_budget_spent (like: 40/day), pace_lock_busy.
        """
        if reaction not in REACTIONS:
            return {"status": "invalid_input", "field": "reaction",
                    "allowed": sorted(REACTIONS)}
        try:
            activity = parse_activity_id(post_url)
        except ValueError as bad:
            return {"status": "invalid_post", "detail": str(bad)}
        page_id = None
        if as_company is not None:
            page_id = str(as_company).strip()
            if not page_id.isdigit() or not (company_name or "").strip():
                return {"status": "invalid_input", "field": "as_company",
                        "message": "as_company is the numeric page id; company_name is required."}
        if confirm:
            refusal = _peek("like")
            if refusal:
                return refusal
        else:
            refusal = _pace("page_read", tool="react_to_post")
            if refusal:
                return refusal

        async def body(ex: Any) -> dict[str, Any]:
            actions = _company(ex)
            if page_id:
                denied = await require_company_role(ex, page_id)
                if denied:
                    return {"activity_id": activity, "reacted": False, **denied}
            if confirm:
                spent = _pace("like", tool="react_to_post")
                if spent:
                    return spent
            return await actions.react(
                activity, reaction, confirm=confirm, page_id=page_id,
                page_name=company_name,
            )

        return await _run(ctx, "react_to_post", body)
