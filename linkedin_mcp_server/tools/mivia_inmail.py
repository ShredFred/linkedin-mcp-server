"""MiViA fork: send_inmail and edit_sent_message (2026-09-30).

Registered from :func:`linkedin_mcp_server.tools.mivia.register_mivia_tools`,
tagged ``mivia``. Both are dry runs unless ``confirm`` is true, and both pass
the same chain before anything leaves: control-character contract, content
checks (links, placeholders, salutation), ledger duplicate check, pacer.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastmcp import Context, FastMCP

from linkedin_mcp_server import mivia_outreach as outreach
from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.mivia_message_checks import check_message
from linkedin_mcp_server.scraping.contracts import refuse_an_invalid_message
from linkedin_mcp_server.scraping.mivia_inmail import (
    MiviaInmail,
    canon,
    pick_own_message,
    thread_url,
)

TAG = "mivia"
SUBJECT_MAX = 200
INMAIL_BODY_MAX = 1900


def _reader(extractor: Any) -> MiviaInmail:
    return MiviaInmail(extractor._mivia_session, extractor._mivia_navigator)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def precheck_inmail(username: str, subject: str, body: str) -> dict[str, Any] | None:
    """Browser-free refusals for an InMail; None when it may proceed."""
    refusal = refuse_an_invalid_message(username, body)
    if refusal is not None:
        return {"status": "invalid_message", "detail": refusal}
    if not subject or not subject.strip():
        return {"status": "subject_required"}
    if any(ord(c) < 32 or ord(c) == 127 for c in subject):
        return {
            "status": "invalid_subject",
            "detail": "no control characters or line breaks",
        }
    if len(subject) > SUBJECT_MAX:
        return {"status": "subject_too_long", "max": SUBJECT_MAX}
    if len(body) > INMAIL_BODY_MAX:
        return {"status": "body_too_long", "max": INMAIL_BODY_MAX}
    findings = [
        f
        for f in check_message(subject, None) + check_message(body, None)
        if f["code"] != "salutation_unverifiable"
    ]
    if findings:
        return {"status": "content_check_failed", "findings": findings}
    return None


def register_mivia_inmail_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    from linkedin_mcp_server.tools.mivia import _pace, _peek, _recipient, _run

    @mcp.tool(
        timeout=max(tool_timeout, 180.0),
        title="Send InMail",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={TAG, "messaging", "actions"},
    )
    async def send_inmail(
        subject: str,
        body: str,
        ctx: Context,
        linkedin_username: str | None = None,
        profile_url: str | None = None,
        confirm: bool = False,
        allow_repeat: bool = False,
    ) -> dict[str, Any]:
        """
        Send one InMail through Sales Navigator (spends one credit) to a
        2nd/3rd-degree member. Dry run unless confirm: opens the composer,
        reads the credit line, fills subject and body, verifies, discards.

        Chain before anything leaves: control characters, subject required,
        links (https, Calendly account), placeholders, salutation against the
        profile name, ledger (one InMail per person unless allow_repeat), pacer.

        Status codes: dry_run, verified, unverified, unknown, first_degree
        (use send_message_verified), open_profile, no_inmail_credits,
        inmail_not_allowed, no_sales_navigator_route, not_an_inmail_composer,
        composer_mismatch, content_check_failed, subject_required, duplicate,
        pace_budget_spent. Never falls back to a connection request.
        """
        ident = linkedin_username or profile_url
        if not ident:
            return {"status": "recipient_required"}
        username, bad = _recipient(ident)
        if bad:
            return bad
        refusal = precheck_inmail(username, subject, body)
        if refusal:
            return {"recipient": username, **refusal}
        ledger = outreach.Ledger.default()
        previous = ledger.already_contacted("inmail", username, None)
        if previous and not allow_repeat:
            return {"recipient": username, "status": "duplicate", "previous": previous}
        if confirm:
            # Peek only: the browser step below may still end without sending
            # (first_degree, open_profile, no credits), and that must not use
            # up one of five InMails a day. Booked with the attempt row.
            spent = _peek("inmail")
            if spent:
                return {"recipient": username, **spent}

        async def body_fn(ex: Any) -> dict[str, Any]:
            spent = _pace("profile_view", tool="send_inmail")
            if spent:
                return {"recipient": username, **spent}
            reader = _reader(ex)
            target = await reader.inmail_target(username)
            if target["status"] == "first_degree":
                return {
                    "recipient": username,
                    "status": "first_degree",
                    "hint": "1st-degree connection: use send_message_verified",
                    "target": target,
                }
            if target["status"] != "ok":
                return {"recipient": username, **target}
            # Subject and body both: "Hallo Herr Maier" in the subject line
            # greets the previous recipient just as visibly as in the body.
            findings = [
                f
                for text in (subject, body)
                for f in check_message(text, target.get("name"))
                if f["code"].startswith("salutation")
            ]
            if findings:
                return {
                    "recipient": username,
                    "status": "content_check_failed",
                    "findings": findings,
                    "target_name": target.get("name"),
                }
            if not confirm:
                result = await reader.inmail(target, subject, body, confirm=False)
                return {"recipient": username, "target": target, **result}
            attempt = uuid.uuid4().hex
            sha = outreach.text_sha(subject + "\n" + body)
            row = {
                "attempt": attempt,
                "kind": "inmail",
                "recipient": outreach.recipient_key(username),
                "text_sha": sha,
                "text_head": outreach.text_head(subject),
                "status": "attempted",
                "started_at": _now(),
            }
            try:
                outreach.Pacer(ledger).take("inmail", tool="send_inmail", row=row)
            except outreach.PaceExceeded as over:
                return {
                    "recipient": username,
                    "status": "pace_budget_spent",
                    "pace": over.state,
                }
            try:
                result = await reader.inmail(target, subject, body, confirm=True)
            except BaseException:
                ledger.append(
                    {
                        "attempt": attempt,
                        "status": "unknown",
                        "detail": "exception during send",
                    }
                )
                raise
            status = result["status"] if result.get("sent") else "not_sent"
            ledger.append(
                {
                    "attempt": attempt,
                    "status": status,
                    "detail": result["status"],
                    "credits_before": (result.get("credits") or {}).get("remaining"),
                    "credits_after": (result.get("credits_after") or {}).get(
                        "remaining"
                    ),
                    "url": result.get("url"),
                }
            )
            return {
                "recipient": username,
                "target": target,
                "attempt": attempt,
                **result,
            }

        return await _run(ctx, "send_inmail", body_fn)

    @mcp.tool(
        timeout=tool_timeout,
        title="InMail Credits",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={TAG, "messaging"},
    )
    async def inmail_credits(ctx: Context) -> dict[str, Any]:
        """Remaining Sales Navigator InMail credits (one page read)."""
        spent = _pace("page_read", tool="inmail_credits")
        if spent:
            return spent
        return await _run(
            ctx, "inmail_credits", lambda ex: _reader(ex).inmail_credits()
        )

    @mcp.tool(
        timeout=max(tool_timeout, 120.0),
        title="Edit Sent Message",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={TAG, "messaging", "actions"},
    )
    async def edit_sent_message(
        thread: str,
        new_text: str,
        ctx: Context,
        message_match: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """
        Edit one of your own sent messages in a thread (URL or thread id).
        The target is the own message containing message_match, else the last
        own message; an ambiguous match is refused. Dry run unless confirm:
        opens the edit form, verifies old and new text, cancels.

        LinkedIn offers "Bearbeiten" for about 60 minutes after sending
        (measured 2026-09-30); when the
        menu item is missing the status is edit_window_closed. Other codes:
        dry_run, verified, unverified, no_own_message, message_not_found,
        ambiguous_match, unchanged, content_check_failed, edit_form_mismatch,
        editor_mismatch, pace_budget_spent. The ledger keeps old and new hash.
        """
        try:
            url = thread_url(thread)
        except ValueError as bad:
            return {"status": "invalid_thread", "detail": str(bad)}
        refusal = refuse_an_invalid_message("thread", new_text)
        if refusal is not None:
            return {"status": "invalid_message", "detail": refusal}
        blocking = [
            f
            for f in check_message(new_text, None)
            if f["code"] != "salutation_unverifiable"
        ]
        if blocking:
            return {"status": "content_check_failed", "findings": blocking}
        if confirm:
            spent = _peek("message_edit")
            if spent:
                return spent
        ledger = outreach.Ledger.default()

        async def body_fn(ex: Any) -> dict[str, Any]:
            spent = _pace("page_read", tool="edit_sent_message")
            if spent:
                return spent
            reader = _reader(ex)
            listed = await reader.thread_messages(url)
            picked = pick_own_message(listed.get("messages", []), message_match)
            if picked["status"] != "ok":
                return {"thread": url, **picked}
            message = picked["message"]
            if canon(message["text"]) == canon(new_text):
                return {"thread": url, "status": "unchanged"}
            warnings = check_message(new_text, listed.get("partner"))
            salutation = [f for f in warnings if f["code"] == "salutation_mismatch"]
            if salutation:
                return {
                    "thread": url,
                    "status": "content_check_failed",
                    "findings": salutation,
                }
            old_sha = outreach.text_sha(message["text"])
            new_sha = outreach.text_sha(new_text)
            base = {
                "thread": url,
                "partner": listed.get("partner"),
                "old_text_head": outreach.text_head(message["text"]),
                "old_sha": old_sha,
                "new_sha": new_sha,
                "warnings": [
                    f for f in warnings if f["code"] == "salutation_unverifiable"
                ],
            }
            if not confirm:
                result = await reader.edit(url, message, new_text, confirm=False)
                return {**base, **result}
            attempt = uuid.uuid4().hex
            row = {
                "attempt": attempt,
                "kind": "message_edit",
                "recipient": outreach.recipient_key(listed.get("partner") or "unknown"),
                "thread": url,
                "old_sha": old_sha,
                "text_sha": new_sha,
                "text_head": outreach.text_head(new_text),
                "status": "attempted",
                "started_at": _now(),
            }
            try:
                outreach.Pacer(ledger).take(
                    "message_edit", tool="edit_sent_message", row=row
                )
            except outreach.PaceExceeded as over:
                return {**base, "status": "pace_budget_spent", "pace": over.state}
            try:
                result = await reader.edit(url, message, new_text, confirm=True)
            except BaseException:
                ledger.append(
                    {
                        "attempt": attempt,
                        "status": "unknown",
                        "detail": "exception during edit",
                    }
                )
                raise
            status = result["status"] if result.get("edited") else "not_sent"
            ledger.append(
                {"attempt": attempt, "status": status, "detail": result["status"]}
            )
            return {**base, "attempt": attempt, **result}

        return await _run(ctx, "edit_sent_message", body_fn)
