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
import re
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Any
from urllib.parse import unquote

from fastmcp import Context, FastMCP
from pydantic import Field

from linkedin_mcp_server import ext_outreach as outreach
from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.linkedin.ext_mentions import plain_text, prepare_text
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
        mentions: list[dict[str, str]] | None = None,
        mention_check: str = "warn",
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
            mentions / mention_check: as in create_post. The page composer
                (Quill) shows no identifier in its suggestions, so exactly
                one namesake may be inserted and its target is read back
                before anything is committed; two namesakes stop with
                mention_ambiguous.

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
        page_ref, bad = parse_company_ref(page_id)
        if bad or not page_ref.isdigit():
            bad = check_page_id(page_id)
            if bad:
                return {"posted": False, **bad}
        return await run_company_post(
            ctx,
            page_ref,
            page_name,
            text,
            image_path=image_path,
            mode=mode,
            scheduled_at=scheduled_at,
            confirm=confirm,
            mentions=mentions,
            mention_check=mention_check,
        )


def parse_company_ref(value: str) -> tuple[str, dict[str, Any] | None]:
    """Numeric page id or company slug from an id, URL, URN or slug."""
    raw = unquote(str(value or "").strip())
    m = re.search(r"linkedin\.com/company/([^/?#]+)", raw, re.IGNORECASE)
    if m:
        raw = m.group(1)
    m = re.fullmatch(r"urn:li:(?:fsd_company|organization|company):(\d+)", raw)
    if m:
        raw = m.group(1)
    if raw.isdigit() or re.fullmatch(r"[A-Za-z0-9%._~-]{2,100}", raw):
        return raw.lower(), None
    return "", {
        "status": "invalid_input",
        "field": "as_company",
        "message": "A company page id, URL, URN or slug.",
    }


def _warn(info: dict[str, Any]) -> dict[str, Any]:
    return {"plaintext_names": info["plaintext_names"]} if info.get("plaintext_names") else {}


async def run_company_post(
    ctx: Context,
    page_ref: str,
    page_name: str,
    text: str,
    *,
    image_path: str | None,
    mode: str,
    scheduled_at: str | None,
    confirm: bool,
    mentions: list[dict[str, str]] | None = None,
    mention_check: str = "warn",
    tool: str = "create_company_post",
) -> dict[str, Any]:
    """The company post flow, shared by create_company_post and
    create_post(as_company=...): checks, ledger, pacer, composer."""
    bad = (
        check_mode(mode)
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
    segments, mention_info, bad = prepare_text(text, mentions, mention_check)
    if bad:
        return {"posted": False, **mention_info, **bad}
    text = plain_text(segments)
    extra: dict[str, Any] = (
        {"segments": segments} if any(k == "mention" for k, _ in segments) else {}
    )
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

    async def _page_id(composer: ExtCompanyPostComposer) -> tuple[str | None, dict[str, Any] | None]:
        if page_ref.isdigit():
            return page_ref, None
        resolved = await composer.resolve_page_id(page_ref)
        if resolved.get("status") != "ok":
            return None, {"posted": False, **resolved}
        return str(resolved["page_id"]), None

    async def _dry(ex: Any) -> dict[str, Any]:
        composer = _composer(ex)
        pid, bad = await _page_id(composer)
        if bad:
            return bad
        out = await composer.create_company_post(
            pid,
            page_name,
            text,
            image_path=image_path,
            mode=mode,
            scheduled_at=scheduled_at,
            confirm=False,
            **extra,
        )
        return {**out, **_warn(mention_info)}

    if not confirm:
        return await _run(
            ctx,
            tool,
            lambda ex: _dry(ex),
        )

    refusal = _peek("post")
    if refusal:
        return {"posted": False, **refusal}

    async def body(ex: Any) -> dict[str, Any]:
        composer = _composer(ex)
        page_id, bad = await _page_id(composer)
        if bad:
            return bad
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
            tool=tool,
            duplicate=repeat,
        )
        if refused:
            return {"posted": False, **refused}

        try:
            result = await composer.create_company_post(
                page_id,
                page_name,
                text,
                image_path=image_path,
                mode=mode,
                scheduled_at=scheduled_at,
                confirm=True,
                **extra,
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
        return {**result, "attempt": attempt, **_warn(mention_info)}

    return await _run(ctx, tool, body)
