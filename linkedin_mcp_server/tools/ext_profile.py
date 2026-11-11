"""Fork extension: edit_profile_intro, replace_featured_link (2026-11-11).

Edits of the signed-in member's own profile. Every tool is a dry run unless
``dry_run=false`` and refuses unless the signed-in member is exactly
``expected_slug`` -- the identity gate sits in the call path, before any
field is touched. The pre-image (the values before the change) is stored in
the local ledger row and returned; the result is read back from LinkedIn.

Ledger and pacer follow ``edit_own_post``: a peek before the browser, the
attempted row as booking under the pacer lock (with a repeat check), an
outcome row afterwards -- ``not_done`` when nothing was clicked.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from fastmcp import Context

from linkedin_mcp_server import ext_outreach as outreach
from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.ext_message_checks import hidden_format_char
from linkedin_mcp_server.tools.ext import ToolRegistrar

TAG = "ext"
KIND = "profile_edit"
BUTTON_TEXT_MAX = 50
HEADLINE_MAX = 220
FEATURED_TITLE_MAX = 200
URL_MAX = 2000
_SLUG_RE = re.compile(r"^[a-z0-9%\-_.]{3,100}$")
_REPEAT_BLOCKING = {"attempted", "unknown", "unverified"}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def check_slug(slug: str) -> dict[str, Any] | None:
    if not isinstance(slug, str) or not _SLUG_RE.match(slug.strip().lower()):
        return {"status": "invalid_input", "field": "expected_slug"}
    return None


def check_url(url: str, field: str) -> dict[str, Any] | None:
    """https only, a host with a dot, no whitespace or control characters."""
    try:
        parsed = urlparse(url)
    except (ValueError, TypeError):
        return {"status": "invalid_input", "field": field}
    if (
        not isinstance(url, str)
        or len(url) > URL_MAX
        or parsed.scheme != "https"
        or "." not in (parsed.hostname or "")
        or any(c.isspace() or ord(c) < 32 for c in url)
    ):
        return {
            "status": "invalid_input",
            "field": field,
            "message": "https URL required",
        }
    return None


def check_line(text: str, field: str, limit: int) -> dict[str, Any] | None:
    """One line, 1..limit characters, no control or invisible characters."""
    if (
        not isinstance(text, str)
        or not text.strip()
        or text != text.strip()
        or len(text) > limit
        or any(ord(c) < 32 or hidden_format_char(c) for c in text)
    ):
        return {
            "status": "invalid_input",
            "field": field,
            "message": f"1-{limit} characters, one line",
        }
    return None


def change_key(**values: Any) -> str:
    """A stable fingerprint of the requested change, for the repeat check."""
    return outreach.text_sha(
        repr(sorted((k, v) for k, v in values.items() if v is not None))
    )


async def _operate(
    ctx: Context,
    *,
    tool: str,
    target: dict[str, Any],
    dry_run: bool,
    action: Any,
) -> dict[str, Any]:
    from linkedin_mcp_server.linkedin.ext_profile import ExtProfileEditor
    from linkedin_mcp_server.tools.ext import (
        _before_deadline,
        _book_attempt,
        _book_deadline,
        _DeadlineHit,
        _pace,
        _peek,
        _run,
    )

    ledger = outreach.Ledger.default()
    key = target["change"]

    def repeat() -> dict[str, Any] | None:
        return next(
            (
                r
                for r in ledger.latest_by_attempt().values()
                if r.get("kind") == KIND
                and r.get("change") == key
                and r.get("status") in _REPEAT_BLOCKING
            ),
            None,
        )

    if not dry_run:
        previous = repeat()
        if previous:
            return {**target, "status": "already_attempted", "previous": previous}
        spent = _peek(KIND)
        if spent:
            return {**target, **spent}

    async def body(ex: Any) -> dict[str, Any]:
        spent = _pace("page_read", 3, tool=tool)
        if spent:
            return {**target, **spent}
        editor = ExtProfileEditor(ex.ext_session, ex.ext_navigator)
        denied = await editor.check_identity(target["expected_slug"])
        if denied:
            return {**target, **denied}
        if dry_run:
            return {**target, "dry_run": True, **(await action(editor, False))}
        attempt = uuid.uuid4().hex
        row = {
            "attempt": attempt,
            "kind": KIND,
            "tool": tool,
            "change": key,
            "status": "attempted",
            "started_at": _now(),
        }
        refused = _book_attempt(ledger, KIND, row, tool=tool, duplicate=repeat)
        if refused:
            if refused["status"] == "duplicate":
                refused = {**refused, "status": "already_attempted"}
            return {**target, **refused}
        try:
            result = await _before_deadline(
                lambda: action(editor, True), lambda: getattr(editor, "clicked", True)
            )
        except BaseException:
            clicked = getattr(editor, "clicked", True)
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
        if isinstance(result, _DeadlineHit):
            return {
                **target,
                "attempt": attempt,
                **_book_deadline(ledger, attempt, result, "not_done"),
            }
        status = result["status"] if result.get("done") else "not_done"
        # The pre-image goes into the local ledger: the way back is known.
        ledger.append(
            {
                "attempt": attempt,
                "status": status,
                "detail": result["status"],
                "pre_image": result.get("pre_image"),
                "after": result.get("after") or result.get("steps"),
            }
        )
        return {**target, "attempt": attempt, **result}

    return await _run(ctx, tool, body)


def register_ext_profile_tools(
    mcp: ToolRegistrar, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    from linkedin_mcp_server.tools.ext import _GuardedMcp

    mcp = _GuardedMcp(mcp)  # type: ignore[assignment]
    timeout = max(tool_timeout, 240.0)

    @mcp.tool(
        timeout=timeout,
        title="Edit Profile Intro",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={TAG, "profile", "actions"},
    )
    async def edit_profile_intro(
        expected_slug: str,
        ctx: Context,
        button_url: str | None = None,
        button_text: str | None = None,
        headline: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """
        Edit the signed-in member's intro: the custom profile button (link URL
        and link text) and/or the headline. Refused unless the signed-in
        member's profile slug equals expected_slug (identity gate before any
        field is touched). Dry run unless dry_run=false: the intro dialog is
        opened, the fields are filled and compared, then discarded. Live: the
        dialog is saved and the intro is read again -- verified only when every
        requested field reads back exactly. The previous values (pre_image) are
        returned and stored in the local ledger.

        Status codes: dry_run, verified, unverified, unchanged, wrong_account,
        own_identity_unknown, intro_dialog_missing, custom_button_missing,
        custom_button_inputs_missing, headline_editor_missing, editor_mismatch,
        save_button_unavailable, already_attempted, pace_budget_spent
        (profile_edit: 6/day), invalid_input, nothing_to_change.
        """
        bad = check_slug(expected_slug)
        if bad:
            return bad
        if button_url is None and button_text is None and headline is None:
            return {"status": "nothing_to_change"}
        if button_url is not None and (bad := check_url(button_url, "button_url")):
            return bad
        if button_text is not None and (
            bad := check_line(button_text, "button_text", BUTTON_TEXT_MAX)
        ):
            return bad
        if headline is not None and (
            bad := check_line(headline, "headline", HEADLINE_MAX)
        ):
            return bad
        slug = expected_slug.strip().lower()
        target = {
            "expected_slug": slug,
            "change": change_key(
                op="intro",
                button_url=button_url,
                button_text=button_text,
                headline=headline,
            ),
        }

        async def action(editor: Any, confirm: bool) -> dict[str, Any]:
            return await editor.edit_intro(
                slug,
                button_url=button_url,
                button_text=button_text,
                headline=headline,
                confirm=confirm,
            )

        return await _operate(
            ctx,
            tool="edit_profile_intro",
            target=target,
            dry_run=dry_run,
            action=action,
        )

    @mcp.tool(
        timeout=timeout,
        title="Replace Featured Link",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={TAG, "profile", "actions"},
    )
    async def replace_featured_link(
        expected_slug: str,
        old_title: str,
        new_url: str,
        new_title: str,
        ctx: Context,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """
        Replace a link in the signed-in member's Featured section. LinkedIn does
        not edit a featured link's URL in place, so this adds the new link first
        and then deletes the old item (identified by its exact title) -- the
        section is never left without the link. Identity gate as in
        edit_profile_intro. Dry run unless dry_run=false: the new link's form is
        reached and filled (fields reported), then discarded; the old item's
        delete control is located. Live: saved, deleted, and the Featured list is
        read again. pre_image holds the titles before the change.

        Status codes: dry_run, verified, partial (added but old item not
        removed, or the reverse: read titles_after), unverified, unchanged,
        wrong_account, own_identity_unknown, featured_item_missing,
        featured_item_not_unique, add_<reason> (featured_menu_missing,
        add_link_missing, link_input_missing, title_field_missing,
        editor_mismatch, save_button_unavailable), already_attempted,
        pace_budget_spent (profile_edit: 6/day), invalid_input.
        """
        bad = (
            check_slug(expected_slug)
            or check_line(old_title, "old_title", FEATURED_TITLE_MAX)
            or check_url(new_url, "new_url")
            or check_line(new_title, "new_title", FEATURED_TITLE_MAX)
        )
        if bad:
            return bad
        slug = expected_slug.strip().lower()
        target = {
            "expected_slug": slug,
            "change": change_key(
                op="featured", old_title=old_title, new_url=new_url, new_title=new_title
            ),
        }

        async def action(editor: Any, confirm: bool) -> dict[str, Any]:
            return await editor.replace_featured_link(
                slug,
                old_title=old_title,
                new_url=new_url,
                new_title=new_title,
                confirm=confirm,
            )

        return await _operate(
            ctx,
            tool="replace_featured_link",
            target=target,
            dry_run=dry_run,
            action=action,
        )
