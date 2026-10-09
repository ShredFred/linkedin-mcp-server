"""Fork tool: create_company_post -- post as a company page (draft / schedule / live).

Separate module so the upstream tool-contract test keeps ignoring the fork, and
so the company path cannot be reached by accident from ``create_post``, which
still refuses ``as_company`` and points here.

Budget and duplicate protection are shared with ``create_post``: the same
``post`` pacer and the same 30-day text block. A company post and a personal
post with the same text are the same text as far as the reader is concerned,
and the page is the louder of the two.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import Field

from linkedin_mcp_server import ext_outreach as outreach
from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.linkedin.ext_company_post import (
    ExtCompanyPostComposer,
    check_mode,
    check_page_id,
    check_schedule,
)
from linkedin_mcp_server.tools.ext import (
    POST_REPEAT_DAYS,
    TAG,
    _POST_REPEAT_BLOCKING,
    _book_attempt,
    _hidden_format_char,
    _peek,
    _run,
    _utf16_len,
)

logger = logging.getLogger(__name__)

# Composer result -> ledger status. A scheduled post will go live without any
# further click, so it blocks the text exactly like a published one; an
# unconfirmed schedule may have been accepted and blocks it too.
_LEDGER_STATUS = {
    "scheduled": "posted",
    "schedule_unconfirmed": "unknown",
    "posted_unverified": "posted",
    "post_unconfirmed": "unknown",
    "draft_saved": "posted",
}


def _composer(extractor: Any) -> ExtCompanyPostComposer:
    return ExtCompanyPostComposer(extractor.ext_session, extractor.ext_navigator)


def register_ext_company_post_tools(
    mcp: FastMCP,
    *,
    tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
) -> None:
    @mcp.tool(
        timeout=tool_timeout,
        title="Create Company Post",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={TAG, "post", "actions"},
    )
    async def create_company_post(
        page_id: str,
        page_name: str,
        text: str,
        ctx: Context,
        mode: Annotated[str, Field(description="draft, schedule or publish")] = "draft",
        confirm: bool = False,
        image_path: str | None = None,
        scheduled_at: str | None = None,
    ) -> dict[str, Any]:
        """
        Compose a post authored by a company page the signed-in member
        administers. The composer is opened from the page's own admin view,
        so the page is the author by construction -- this tool verifies that
        author, it does not switch one. Without confirm=true this is a dry
        run: the composer is opened, the author verified, the text written
        and verified, then everything is discarded -- nothing is published,
        no image is uploaded and the schedule dialog is only located.

        Three modes. ``draft`` saves it to the page's drafts and publishes
        nothing. ``schedule`` hands LinkedIn a time, after which **it publishes
        by itself, with no further click**. ``publish`` posts immediately.

        Args:
            page_id: Numeric page id, e.g. 12345678. It addresses
                the admin view the composer is opened from.
            page_name: The page name as the composer writes it, e.g. "Acme Labs".
                Verified as a case-insensitive substring of the author button.
            text: Post text; LF separates paragraphs.
            mode: draft (default), schedule or publish.
            confirm: Must be true to actually draft, schedule or publish.
            image_path: Optional local image file to attach.
            scheduled_at: "YYYY-MM-DD HH:MM" local time; only with
                mode=schedule, at least 5 minutes ahead and at most 90 days.

        Returns the composer result. Statuses that did something:
        draft_saved, scheduled (time confirmed in the composer),
        posted_unverified (published, composer closed),
        schedule_unconfirmed / post_unconfirmed (may be live or scheduled: do
        NOT retry, check the page). dry_run did nothing. Stops before any
        commit, safe to fix and retry: author_control_unavailable,
        composer_opener_unavailable, author_not_confirmed, author_lost,
        text_not_written, media_button_unavailable,
        schedule_control_unavailable, schedule_not_filled,
        schedule_confirm_unavailable, schedule_not_confirmed,
        post_button_unavailable, post_button_disabled, close_unavailable,
        draft_prompt_unavailable, composer_unavailable, editor_has_media,
        invalid_image. Refusals before anything is booked: invalid_input,
        invalid_text, schedule_too_soon, schedule_too_far, duplicate_text,
        pace_budget_spent, pace_lock_busy.
        """
        bad = (
            check_page_id(page_id)
            or check_mode(mode)
            or check_schedule(mode, scheduled_at)
        )
        if bad:
            return {"posted": False, **bad}
        if not page_name.strip():
            return {
                "status": "invalid_input",
                "field": "page_name",
                "posted": False,
            }
        if not text.strip() or any(
            (ord(c) < 32 and c != "\n") or _hidden_format_char(c) for c in text
        ):
            return {
                "status": "invalid_text",
                "posted": False,
                "message": "Text must be non-empty and contain no control characters other than LF.",
            }
        if _utf16_len(text) > 3000:
            return {
                "status": "invalid_text",
                "posted": False,
                "message": "LinkedIn posts are limited to 3000 characters.",
            }

        ledger = outreach.Ledger.default()
        sha = outreach.text_sha(text)

        def repeat() -> dict[str, Any] | None:
            since = datetime.now().astimezone() - timedelta(days=POST_REPEAT_DAYS)
            return next(
                (
                    r
                    for r in ledger.latest_by_attempt().values()
                    if r.get("kind") == "post"
                    and sha in outreach.row_text_shas(r)
                    and r.get("status") in _POST_REPEAT_BLOCKING
                    and outreach.counted_time(r) >= since
                ),
                None,
            )

        previous = repeat()
        if previous:
            return {"status": "duplicate_text", "posted": False, "previous": previous}

        if not confirm:
            return await _run(
                ctx,
                "create_company_post",
                lambda ex: _composer(ex).create_company_post(
                    page_id,
                    page_name,
                    text,
                    image_path=image_path,
                    mode=mode,
                    scheduled_at=scheduled_at,
                    confirm=False,
                ),
            )

        refusal = _peek("post")
        if refusal:
            return {"posted": False, **refusal}

        async def body(ex: Any) -> dict[str, Any]:
            attempt = uuid.uuid4().hex
            refused = _book_attempt(
                ledger,
                "post",
                {
                    "attempt": attempt,
                    "kind": "post",
                    "text_sha": sha,
                    "text_head": outreach.text_head(text),
                    "status": "attempted",
                    "page": page_name,
                    "page_id": page_id,
                    "mode": mode,
                    "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                },
                tool="create_company_post",
                duplicate=repeat,
            )
            if refused:
                return {"posted": False, **refused}

            composer = _composer(ex)
            try:
                result = await composer.create_company_post(
                    page_id,
                    page_name,
                    text,
                    image_path=image_path,
                    mode=mode,
                    scheduled_at=scheduled_at,
                    confirm=True,
                )
            except Exception:
                # The click marker is the only honest witness: set means the
                # commit may have happened, so the row must block the text.
                ledger.append(
                    {
                        "attempt": attempt,
                        "status": "unknown" if composer.clicked else "not_posted",
                    }
                )
                raise

            ledger.append(
                {
                    "attempt": attempt,
                    "status": _LEDGER_STATUS.get(result.get("status", ""), "not_posted"),
                    "detail": result.get("status"),
                }
            )
            return {**result, "attempt": attempt}

        return await _run(ctx, "create_company_post", body)
