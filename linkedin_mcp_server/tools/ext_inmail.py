"""Fork extension: send_inmail and edit_sent_message (2026-09-30).

Registered from :func:`linkedin_mcp_server.tools.ext.register_ext_tools`,
tagged ``ext``. Both are dry runs unless ``confirm`` is true, and both pass
the same chain before anything leaves: control-character contract, content
checks (links, placeholders, salutation), ledger duplicate check, pacer.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from fastmcp import Context, FastMCP

from linkedin_mcp_server import ext_outreach as outreach
from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.ext_message_checks import (
    MESSAGE_MAX_UTF16,
    check_message,
    check_outgoing,
    hidden_format_char,
)
from linkedin_mcp_server.ext_message_checks import utf16_len as utf16_len
from linkedin_mcp_server.linkedin.contracts import (
    is_invisible_control,
    refuse_an_invalid_message,
)
from linkedin_mcp_server.linkedin.ext_inmail import (
    ExtInmail,
    canon,
    pick_own_message,
    strip_edit_marker,
    thread_url,
)

TAG = "ext"
SUBJECT_MAX = 200
INMAIL_BODY_MAX = 1900


def _reader(extractor: Any) -> ExtInmail:
    return ExtInmail(extractor.ext_session, extractor.ext_navigator)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _edit_lands_on_message(
    ledger: outreach.Ledger, url: str, partner: str | None, old_sha: str
) -> dict[str, Any] | None:
    """The newest message row the edited text was sent as, in this thread.

    Thread id first (what the send stored); the partner key only for a row
    without a thread. None when nothing matches -- no guess.
    """
    match = re.search(r"/messaging/thread/([^/]+)/", url)
    tid = match.group(1) if match else None
    key = outreach.recipient_key(partner) if partner else None
    rows = [
        r
        for r in ledger.latest_by_attempt().values()
        if r.get("kind") == "message"
        and old_sha in outreach.row_text_shas(r)
        and (
            (tid and r.get("thread") == tid)
            or (not r.get("thread") and key and r.get("recipient") == key)
        )
    ]
    rows.sort(key=lambda r: str(r.get("started_at") or ""))
    return rows[-1] if rows else None


def precheck_inmail(username: str, subject: str, body: str) -> dict[str, Any] | None:
    """Browser-free refusals for an InMail; None when it may proceed."""
    refusal = refuse_an_invalid_message(username, body)
    if refusal is not None:
        return {"status": "invalid_message", "detail": refusal}
    if not subject or not subject.strip():
        return {"status": "subject_required"}
    if any(
        ord(c) < 32 or is_invisible_control(c) or hidden_format_char(c) for c in subject
    ):
        return {
            "status": "invalid_subject",
            "detail": "no control characters or line breaks",
        }
    if utf16_len(subject) > SUBJECT_MAX:
        return {"status": "subject_too_long", "max": SUBJECT_MAX}
    if utf16_len(body) > INMAIL_BODY_MAX:
        return {"status": "body_too_long", "max": INMAIL_BODY_MAX}
    # Strict links (host-bound Calendly and shorteners, bare hosts too),
    # placeholders and Cf characters for both fields.
    for text in (subject, body):
        refusal = check_outgoing(text, max_utf16=INMAIL_BODY_MAX)
        if refusal is not None:
            return refusal
    return None


def repeat_refusal(username: str, allow_repeat: bool) -> dict[str, Any] | None:
    """allow_repeat is for test sends to the canary only: a repeat to a real
    person is a second InMail (and a second credit) in their inbox."""
    if allow_repeat and outreach.recipient_key(username) != outreach.recipient_key(
        outreach.DEFAULT_CANARY
    ):
        return {
            "status": "repeat_not_allowed",
            "detail": "allow_repeat is for the canary only",
        }
    return None


def precheck_edit(new_text: str) -> dict[str, Any] | None:
    """Browser-free refusals for an edited message; None when it may proceed."""
    refusal = refuse_an_invalid_message("thread", new_text)
    if refusal is not None:
        return {"status": "invalid_message", "detail": refusal}
    return check_outgoing(new_text, max_utf16=MESSAGE_MAX_UTF16)


def register_ext_inmail_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    from linkedin_mcp_server.tools.ext import (
        _book_attempt,
        _GuardedMcp,
        _pace,
        _peek,
        _recipient,
        _run,
    )

    mcp = _GuardedMcp(mcp)  # type: ignore[assignment]

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
        (use send_message), open_profile, no_inmail_credits,
        inmail_not_allowed, no_sales_navigator_route, not_an_inmail_composer,
        composer_mismatch, content_check_failed, subject_required, duplicate,
        pace_budget_spent, repeat_not_allowed (allow_repeat to a non-canary),
        unexpected_inmail_cost, invalid_message, message_too_long. Never
        falls back to a connection request.

        Further refusals before anything is booked: recipient_required,
        invalid_recipient, invalid_subject (control characters),
        subject_too_long / body_too_long (max holds the limit),
        pace_lock_busy (retry shortly). Page-side stops before the send
        (safe to retry after a look): composer_not_opened, editor_mismatch.
        unknown / unverified: the InMail may have left and a credit may be
        spent -- never resend, check Sales Navigator's sent folder.
        """
        ident = linkedin_username or profile_url
        if not ident:
            return {"status": "recipient_required"}
        username, bad = _recipient(ident)
        if bad:
            return bad
        refusal = precheck_inmail(username, subject, body) or repeat_refusal(
            username, allow_repeat
        )
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
                    "hint": "1st-degree connection: use send_message",
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
                "text_anchor": outreach.text_anchor(body),
                "status": "attempted",
                "started_at": _now(),
            }
            refused = _book_attempt(
                ledger,
                "inmail",
                row,
                tool="send_inmail",
                duplicate=None
                if allow_repeat
                else (lambda: ledger.already_contacted("inmail", username, None)),
            )
            if refused:
                return {"recipient": username, **refused}
            try:
                result = await reader.inmail(target, subject, body, confirm=True)
            except BaseException:
                # Before the send click nothing left: not_sent neither counts
                # nor blocks. After it the InMail may be out: unknown.
                clicked = getattr(reader, "clicked", True)
                ledger.append(
                    {
                        "attempt": attempt,
                        "status": "unknown" if clicked else "not_sent",
                        "detail": "exception during send"
                        if clicked
                        else "exception before send",
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
        """Remaining Sales Navigator InMail credits (one page read).

        Refusals: pace_budget_spent (wait, see pace_status), pace_lock_busy
        (nothing booked, retry shortly).
        """
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
        editor_mismatch, invalid_message, message_too_long,
        pace_budget_spent. The ledger keeps old and new hash.

        Also: invalid_thread (not a thread URL/id), not_own_message,
        edit_form_not_opened, no_more_menu, save_button_unavailable (nothing
        saved, safe to retry), pace_lock_busy (retry shortly). unverified =
        the edit may be saved: re-read the thread before editing again.
        """
        try:
            url = thread_url(thread)
        except ValueError as bad:
            return {"status": "invalid_thread", "detail": str(bad)}
        refusal = precheck_edit(new_text)
        if refusal is not None:
            return refusal
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
            if canon(strip_edit_marker(message["text"])[0]) == canon(new_text):
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
                "text_anchor": outreach.text_anchor(new_text),
                "status": "attempted",
                "started_at": _now(),
            }
            refused = _book_attempt(
                ledger, "message_edit", row, tool="edit_sent_message"
            )
            if refused:
                return {**base, **refused}
            try:
                result = await reader.edit(url, message, new_text, confirm=True)
            except BaseException:
                clicked = getattr(reader, "clicked", True)
                ledger.append(
                    {
                        "attempt": attempt,
                        "status": "unknown" if clicked else "not_sent",
                        "detail": "exception during edit"
                        if clicked
                        else "exception before send",
                    }
                )
                raise
            status = result["status"] if result.get("edited") else "not_sent"
            ledger.append(
                {"attempt": attempt, "status": status, "detail": result["status"]}
            )
            if result.get("edited"):
                # The thread now shows the new text: the original message row
                # must carry its first line, or follow_up_list/reply_after
                # search for a text that is no longer there.
                original = _edit_lands_on_message(
                    ledger, url, listed.get("partner"), old_sha
                )
                if original is not None:
                    ledger.append(
                        {
                            "attempt": original["attempt"],
                            "text_head": outreach.text_head(new_text),
                            # Replaces the old text's anchor; a row from
                            # before anchors gains one here.
                            "text_anchor": outreach.text_anchor(new_text),
                            "edited_by": attempt,
                            "edited_at": _now(),
                            # The edited text is now what this person got:
                            # the message duplicate check must block it too.
                            **outreach.edit_note_shas(original, new_sha),
                        }
                    )
                result = {
                    **result,
                    "message_row": original["attempt"] if original else None,
                }
            return {**base, "attempt": attempt, **result}

        return await _run(ctx, "edit_sent_message", body_fn)
