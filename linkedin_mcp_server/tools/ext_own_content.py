"""Fork extension: delete_own_post, edit_own_post, delete_own_comment, edit_own_comment.

The way back for ``create_post`` and ``comment_on_post`` (2026-10-01).
Registered from :func:`linkedin_mcp_server.tools.ext.register_ext_tools`,
tagged ``ext``. Every tool is a dry run unless ``dry_run=false``: the author
is checked against the signed-in member, the menu entry is located, the menu is
closed again -- nothing is clicked that changes anything.

Ledger and pacer follow ``edit_sent_message``: a peek before the browser, the
attempted row as booking under the pacer lock (with a repeat check), an outcome
row afterwards -- ``not_done`` when nothing was clicked (neither counts nor
blocks), ``unknown`` when an exception came after the final click.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastmcp import Context, FastMCP

from linkedin_mcp_server import ext_outreach as outreach
from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.ext_message_checks import hidden_format_char
from linkedin_mcp_server.linkedin.ext_engagement import parse_activity_id
from linkedin_mcp_server.linkedin.ext_own_content import (
    ExtOwnContent,
    parse_comment_ref,
)

TAG = "ext"
POST_MAX = 3000
COMMENT_MAX = 1250
# A row in one of these states may have changed LinkedIn: the same operation
# on the same target is refused again (deleting twice, or the same edit twice).
_REPEAT_BLOCKING = {"attempted", "unknown", "verified", "unverified"}


def _reader(extractor: Any) -> ExtOwnContent:
    return ExtOwnContent(extractor.ext_session, extractor.ext_navigator)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def check_text(text: str, limit: int) -> dict[str, Any] | None:
    """Browser-free refusal for a new text; None when it may proceed."""
    try:
        units = len(text.encode("utf-16-le")) // 2
    except UnicodeEncodeError:
        # A lone surrogate (valid in JSON "\ud800") cannot be typed: refuse
        # instead of raising through the tool.
        units = limit + 1
    if (
        not text
        or not text.strip()
        or units > limit
        or any((ord(c) < 32 and c != "\n") or hidden_format_char(c) for c in text)
    ):
        return {
            "status": "invalid_text",
            "message": f"1-{limit} UTF-16 units, LF allowed, no control or invisible characters.",
        }
    return None


def resolve_target(
    post_url: str, comment_id: str | None
) -> tuple[str | None, str | None, dict[str, Any] | None]:
    """(activity id, comment id, refusal). A comment URN must name the same post."""
    try:
        activity = parse_activity_id(post_url)
    except ValueError as bad:
        return None, None, {"status": "invalid_post", "detail": str(bad)}
    if comment_id is None:
        return activity, None, None
    try:
        in_urn, cid = parse_comment_ref(comment_id)
    except ValueError as bad:
        return None, None, {"status": "invalid_comment", "detail": str(bad)}
    if in_urn and in_urn != activity:
        return (
            None,
            None,
            {
                "status": "invalid_comment",
                "detail": "comment urn belongs to a different post",
            },
        )
    return activity, cid, None


def _original_comment_row(
    ledger: outreach.Ledger, activity: str, old_text: str | None
) -> dict[str, Any] | None:
    """The comment_on_post row that wrote this text on this post; None, no guess."""
    if not old_text:
        return None
    sha = outreach.text_sha(old_text)
    rows = [
        r
        for r in ledger.latest_by_attempt().values()
        if r.get("kind") == "comment"
        and r.get("activity") == activity
        and sha in outreach.row_text_shas(r)
    ]
    # Real aware datetimes: text order breaks across UTC offsets.
    rows.sort(
        key=lambda r: outreach.row_time(r) or datetime.min.replace(tzinfo=timezone.utc)
    )
    return rows[-1] if rows else None


async def _operate(
    ctx: Context,
    *,
    kind: str,
    tool: str,
    activity: str,
    comment: str | None,
    new_text: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    from linkedin_mcp_server.tools.ext import _book_attempt, _pace, _peek, _run

    ledger = outreach.Ledger.default()
    new_sha = outreach.text_sha(new_text) if new_text is not None else None

    def repeat() -> dict[str, Any] | None:
        return next(
            (
                r
                for r in ledger.latest_by_attempt().values()
                if r.get("kind") == kind
                and r.get("activity") == activity
                and r.get("comment") == comment
                and (new_sha is None or r.get("text_sha") == new_sha)
                and r.get("status") in _REPEAT_BLOCKING
            ),
            None,
        )

    target = {"activity_id": activity, "comment_id": comment}
    if not dry_run:
        previous = repeat()
        if previous:
            return {**target, "status": "already_attempted", "previous": previous}
        spent = _peek(kind)
        if spent:
            return {**target, **spent}

    async def body(ex: Any) -> dict[str, Any]:
        spent = _pace("page_read", 2, tool=tool)
        if spent:
            return {**target, **spent}
        reader = _reader(ex)

        async def act(confirm: bool) -> dict[str, Any]:
            if new_text is None:
                return await reader.delete(activity, comment, confirm=confirm)
            return await reader.edit(activity, comment, new_text, confirm=confirm)

        if dry_run:
            return {**target, "dry_run": True, **(await act(False))}
        attempt = uuid.uuid4().hex
        row = {
            "attempt": attempt,
            "kind": kind,
            "activity": activity,
            "comment": comment,
            "status": "attempted",
            "started_at": _now(),
        }
        if new_text is not None:
            row["text_sha"] = new_sha
            row["text_head"] = outreach.text_head(new_text)
        refused = _book_attempt(ledger, kind, row, tool=tool, duplicate=repeat)
        if refused:
            if refused["status"] == "duplicate":
                refused = {**refused, "status": "already_attempted"}
            return {**target, **refused}
        try:
            result = await act(True)
        except BaseException:
            clicked = getattr(reader, "clicked", True)
            ledger.append(
                {
                    "attempt": attempt,
                    "status": "unknown" if clicked else "not_done",
                    "detail": "exception after the final click"
                    if clicked
                    else "exception before the final click",
                }
            )
            raise
        status = result["status"] if result.get("done") else "not_done"
        outcome: dict[str, Any] = {
            "attempt": attempt,
            "status": status,
            "detail": result["status"],
        }
        if result.get("old_text"):
            outcome["old_sha"] = outreach.text_sha(result["old_text"])
        ledger.append(outcome)
        extra: dict[str, Any] = {}
        if result.get("done") and comment is not None:
            # The comment_on_post row keeps its text_sha (it still blocks the
            # same text, conservatively); it learns what became of it. An edit
            # adds edited_text_sha(s), which comment_on_post blocks as well.
            # A deletion (deleted_by) deliberately does NOT release the block:
            # posting the same text again after taking it back is exactly the
            # repeated-text pattern LinkedIn flags, and the row stays counted.
            original = _original_comment_row(ledger, activity, result.get("old_text"))
            if original is not None:
                note: dict[str, Any] = {"attempt": original["attempt"]}
                if new_text is None:
                    note.update({"deleted_by": attempt, "deleted_at": _now()})
                else:
                    note.update(
                        {
                            "edited_by": attempt,
                            "edited_at": _now(),
                            **outreach.edit_note_shas(original, new_sha),
                            "text_head": outreach.text_head(new_text),
                        }
                    )
                ledger.append(note)
            extra["comment_row"] = original["attempt"] if original else None
        result = {k: v for k, v in result.items() if k != "old_text"}
        return {**target, "attempt": attempt, **result, **extra}

    return await _run(ctx, tool, body)


def register_ext_own_content_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    from linkedin_mcp_server.tools.ext import _GuardedMcp

    mcp = _GuardedMcp(mcp)  # type: ignore[assignment]
    timeout = max(tool_timeout, 150.0)

    @mcp.tool(
        timeout=timeout,
        title="Delete Own Post",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={TAG, "post", "actions"},
    )
    async def delete_own_post(
        post_url: str, ctx: Context, dry_run: bool = True
    ) -> dict[str, Any]:
        """
        Delete one of your own posts (post URL, activity URN or id; create_post
        returns activity_id). Dry run unless dry_run=false: checks that the
        signed-in member is the author, opens the post menu, finds "Beitrag
        löschen", closes the menu. Live: confirms the dialog and reloads the
        post -- verified only when the post is gone.

        Status codes: dry_run, verified, unverified, not_own_post,
        author_unknown, author_ambiguous, own_identity_unknown, post_not_found, menu_unavailable,
        menu_item_missing, menu_item_ambiguous, confirm_dialog_missing,
        already_attempted, pace_budget_spent (post_delete: 3/day).
        Also: invalid_post (bad URL/URN, nothing read), own_identity_unknown
        (own profile not resolved: re-check login), pace_lock_busy (retry
        shortly). unverified = the action may have happened: re-read the
        post, never repeat it (the ledger answers already_attempted).
        """
        activity, _, bad = resolve_target(post_url, None)
        if bad:
            return bad
        return await _operate(
            ctx,
            kind="post_delete",
            tool="delete_own_post",
            activity=activity,  # type: ignore[arg-type]
            comment=None,
            new_text=None,
            dry_run=dry_run,
        )

    @mcp.tool(
        timeout=timeout,
        title="Edit Own Post",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={TAG, "post", "actions"},
    )
    async def edit_own_post(
        post_url: str, new_text: str, ctx: Context, dry_run: bool = True
    ) -> dict[str, Any]:
        """
        Replace the text of one of your own posts. Dry run unless
        dry_run=false: author check, post menu, "Bearbeiten" located, menu
        closed. Live: the editor's prefill must equal the shown text, the new
        text is inserted and compared, saved, and the post is reloaded --
        verified only when the text reads back exactly.

        Status codes: dry_run, verified, unverified, unchanged, invalid_text,
        not_own_post, author_unknown, post_not_found, menu_unavailable,
        menu_item_missing, editor_missing, editor_prefill_mismatch,
        editor_mismatch, save_button_unavailable, already_attempted,
        pace_budget_spent (post_edit: 5/day).
        Also: author_ambiguous, menu_item_ambiguous, invalid_post (bad
        URL/URN, nothing read), own_identity_unknown (own profile not
        resolved: re-check login), pace_lock_busy (retry shortly). unverified = the action may have happened: re-read the
        post, never repeat it (the ledger answers already_attempted).
        """
        bad = check_text(new_text, POST_MAX)
        if bad:
            return bad
        activity, _, bad = resolve_target(post_url, None)
        if bad:
            return bad
        return await _operate(
            ctx,
            kind="post_edit",
            tool="edit_own_post",
            activity=activity,  # type: ignore[arg-type]
            comment=None,
            new_text=new_text,
            dry_run=dry_run,
        )

    @mcp.tool(
        timeout=timeout,
        title="Delete Own Comment",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={TAG, "post", "actions"},
    )
    async def delete_own_comment(
        post_url: str, comment_id: str, ctx: Context, dry_run: bool = True
    ) -> dict[str, Any]:
        """
        Delete one of your own comments under a post (comment_id: the numeric
        id from get_post_engagers, or urn:li:comment:(activity:A,C)). Dry run
        unless dry_run=false: the comment's author must be the signed-in member,
        its menu must offer "Löschen". Live: confirms and reloads -- verified
        only when the comment card is gone while the post itself loaded.

        Status codes: dry_run, verified, unverified, not_own_comment,
        comment_not_found, comment_ambiguous, author_unknown, menu_unavailable,
        menu_item_missing, confirm_dialog_missing, already_attempted,
        pace_budget_spent (comment_delete: 5/day).
        Also: invalid_post / invalid_comment (bad URL or comment id, or a
        comment URN of another post), own_identity_unknown (own profile not
        resolved: re-check login), pace_lock_busy (retry shortly). unverified = the action may have happened: re-read the
        comment, never repeat it (the ledger answers already_attempted).
        """
        activity, cid, bad = resolve_target(post_url, comment_id)
        if bad:
            return bad
        return await _operate(
            ctx,
            kind="comment_delete",
            tool="delete_own_comment",
            activity=activity,  # type: ignore[arg-type]
            comment=cid,
            new_text=None,
            dry_run=dry_run,
        )

    @mcp.tool(
        timeout=timeout,
        title="Edit Own Comment",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={TAG, "post", "actions"},
    )
    async def edit_own_comment(
        post_url: str,
        comment_id: str,
        new_text: str,
        ctx: Context,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """
        Replace the text of one of your own comments. Dry run unless
        dry_run=false: author check, comment menu, "Bearbeiten" located, menu
        closed. Live: prefill must equal the shown comment, new text inserted
        and compared, saved, reloaded -- verified only on an exact read-back.

        Status codes: dry_run, verified, unverified, unchanged, invalid_text,
        not_own_comment, comment_not_found, author_unknown, menu_unavailable,
        menu_item_missing, editor_missing, editor_prefill_mismatch,
        editor_mismatch, save_button_unavailable, already_attempted,
        pace_budget_spent (comment_edit: 5/day).
        Also: comment_ambiguous, menu_item_ambiguous, invalid_post /
        invalid_comment (bad URL or comment id, or a comment URN of another
        post), own_identity_unknown (own profile not resolved: re-check
        login), pace_lock_busy (retry shortly). unverified = the action may have happened: re-read the
        comment, never repeat it (the ledger answers already_attempted).
        """
        bad = check_text(new_text, COMMENT_MAX)
        if bad:
            return bad
        activity, cid, bad = resolve_target(post_url, comment_id)
        if bad:
            return bad
        return await _operate(
            ctx,
            kind="comment_edit",
            tool="edit_own_comment",
            activity=activity,  # type: ignore[arg-type]
            comment=cid,
            new_text=new_text,
            dry_run=dry_run,
        )
