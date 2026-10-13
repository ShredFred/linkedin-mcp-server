"""Fork extension: send_inmail and edit_sent_message (2026-09-30).

Registered from :func:`linkedin_mcp_server.tools.ext.register_ext_tools`,
tagged ``ext``. Both are dry runs unless ``confirm`` is true, and both pass
the same chain before anything leaves: control-character contract, content
checks (links, placeholders, salutation), ledger duplicate check, pacer.
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime
from typing import Any

import anyio
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


# Open Profile sends: the inbox balance is read outside the send deadline and
# kept briefly per process; the comparison after a free send is deferred to the
# next balance read. Live 2026-10-12: with two inbox page reads inside the
# send deadline (one before, one after the click) it ran out three times
# before the click and once after it.
BALANCE_CACHE_SECONDS = 300.0
BALANCE_READ_SECONDS = 30.0
_balance: dict[str, Any] = {"remaining": None, "at": None}
# attempt id -> balance before the free send, waiting for the next read.
_pending_free_checks: dict[str, int] = {}


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _remember_balance(remaining: Any) -> None:
    if _is_count(remaining):
        _balance.update(remaining=remaining, at=time.monotonic())


def _cached_balance() -> int | None:
    at = _balance.get("at")
    if at is None or time.monotonic() - at > BALANCE_CACHE_SECONDS:
        return None
    return _balance.get("remaining")


def _resolve_free_checks(ledger: Any, remaining: Any) -> None:
    """Close the deferred free-send checks with a fresh inbox balance."""
    if not _is_count(remaining):
        return
    for attempt, before in list(_pending_free_checks.items()):
        _pending_free_checks.pop(attempt, None)
        ledger.append(
            {
                "attempt": attempt,
                "credit_check": "measured",
                "credits_after": remaining,
                "credit_spent_despite_free": remaining < before,
            }
        )


def _void_free_checks(ledger: Any, why: str) -> None:
    """Another InMail is about to be clicked: the comparison would mix sends."""
    for attempt in list(_pending_free_checks):
        _pending_free_checks.pop(attempt, None)
        ledger.append(
            {
                "attempt": attempt,
                "credit_check": "unavailable",
                "credit_spent_despite_free": None,
                "credit_check_detail": why,
            }
        )


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
        _before_deadline,
        _book_attempt,
        _book_deadline,
        _DeadlineHit,
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
        allow_open_profile: bool = False,
        credits_before: int | None = None,
    ) -> dict[str, Any]:
        """
        Send one InMail through Sales Navigator (spends one credit) to a
        2nd/3rd-degree member. Dry run unless confirm: opens the composer,
        reads the credit line, fills subject and body, verifies, discards.

        Chain before anything leaves: control characters, subject required,
        links (https, Calendly account), placeholders, salutation against the
        profile name, ledger (one InMail per person unless allow_repeat), pacer.

        allow_open_profile: send to an Open Profile (free message) instead of
        stopping with open_profile. Only when the credit line says free and
        names no cost; every other check stays, and the ledger marks the row
        open_profile=true. Off by default.

        credits_before: optional InMail balance the caller read just before
        (inmail_credits); only used with allow_open_profile. Saves the inbox
        read before the send. Otherwise a balance read within the last 5
        minutes is reused, else the inbox is read once before anything is
        booked. The balance after a free send is compared at the next balance
        read (inmail_credits or the next Open Profile send): the ledger row
        carries credit_check="deferred" until then, and "unavailable" if
        another InMail is sent first. invalid_credits_before: not an int >= 0.

        Status codes: dry_run, verified, unverified, unknown, first_degree
        (use send_message), open_profile, no_inmail_credits,
        inmail_not_allowed (only on a refusal text or a disabled message
        button; evidence names it), message_button_not_found (no refusal seen;
        detail.profile_message_button / upsell_visible), no_sales_navigator_route
        (reason: no_view_link, saved_lead_without_urn, ambiguous_profile_urn;
        a lead already saved in Sales Navigator is routed by its URN),
        not_an_inmail_composer,
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

        Tool deadline: answered before it with deadline_reached=true:
        not_sent (retry_safe=true, deadline before the send click, nothing
        left, no credit spent) or unknown (retry_safe=false, as above).
        """
        ident = linkedin_username or profile_url
        if not ident:
            return {"status": "recipient_required"}
        if credits_before is not None and not _is_count(credits_before):
            return {"status": "invalid_credits_before"}
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
            balance_before: int | None = None
            if allow_open_profile and confirm:
                # Free lines name no balance: it is known before the composer
                # opens, never read inside the send deadline.
                balance_before = credits_before
                if balance_before is None:
                    balance_before = _cached_balance()
                if balance_before is None:
                    # A page read like inmail_credits, paced as one.
                    spent = _pace("page_read", tool="send_inmail")
                    if spent:
                        return {"recipient": username, **spent}
                    read: Any = None
                    with anyio.move_on_after(BALANCE_READ_SECONDS):
                        try:
                            read = (await reader.inmail_credits()).get("remaining")
                        except Exception:
                            read = None
                    balance_before = read if _is_count(read) else None
                    _remember_balance(balance_before)
                    _resolve_free_checks(ledger, balance_before)
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
                result = await reader.inmail(
                    target,
                    subject,
                    body,
                    confirm=False,
                    allow_open_profile=allow_open_profile,
                )
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
            # From the click on, a pending free-send comparison would count
            # this InMail's credit too.
            _void_free_checks(ledger, "another InMail sent before the check")
            # The balance may change with this click: a cached value must not
            # stand in for the inbox at the next Open Profile send (its
            # balance_before == 0 stop). A paid send re-reads it afterwards.
            _balance.update(remaining=None, at=None)
            extra = (
                {"balance_before": balance_before, "defer_credit_check": True}
                if allow_open_profile
                else {}
            )
            try:
                result = await _before_deadline(
                    lambda: reader.inmail(
                        target,
                        subject,
                        body,
                        confirm=True,
                        allow_open_profile=allow_open_profile,
                        **extra,
                    ),
                    lambda: getattr(reader, "clicked", True),
                )
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
            if isinstance(result, _DeadlineHit):
                return {
                    "recipient": username,
                    **_book_deadline(ledger, attempt, result, "not_sent"),
                }
            status = result["status"] if result.get("sent") else "not_sent"
            # Only an Open Profile send carries the free marks: every other
            # row keeps exactly its earlier shape.
            free_marks = (
                {
                    "open_profile": True,
                    "free": True,
                    "credit_check": result.get("credit_check"),
                    # None: the balance could not be compared (unknown).
                    "credit_spent_despite_free": result.get(
                        "credit_spent_despite_free"
                    ),
                }
                if result.get("open_profile")
                else {}
            )
            if result.get("credit_check") == "deferred":
                if balance_before is None:
                    # Nothing to compare against later: say so now.
                    result = {**result, "credit_check": "unavailable"}
                    free_marks["credit_check"] = "unavailable"
                else:
                    _pending_free_checks[attempt] = balance_before
            _remember_balance((result.get("credits_after") or {}).get("remaining"))
            ledger.append(
                {
                    **free_marks,
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

        Waits up to 15 s for the inbox header's credits line. Returns
        remaining and line; without a line remaining is null and the answer
        says why: reason (ambiguous_credits_line, login_page,
        sales_nav_upsell, not_sales_inbox,
        inbox_not_rendered, no_credits_line), url after navigation, title,
        credit_mentions (short lines naming InMail, for a new wording).

        Refusals: pace_budget_spent (wait, see pace_status), pace_lock_busy
        (nothing booked, retry shortly).
        """
        spent = _pace("page_read", tool="inmail_credits")
        if spent:
            return spent
        out = await _run(ctx, "inmail_credits", lambda ex: _reader(ex).inmail_credits())
        if isinstance(out, dict) and _is_count(out.get("remaining")):
            _remember_balance(out["remaining"])
            if _pending_free_checks:
                _resolve_free_checks(outreach.Ledger.default(), out["remaining"])
        return out

    @mcp.tool(
        timeout=max(tool_timeout, 120.0),
        title="Sales Navigator Sent InMails",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={TAG, "messaging"},
    )
    async def sales_nav_sent_inmails(ctx: Context, limit: int = 30) -> dict[str, Any]:
        """Rows of the Sales Navigator inbox's sent view (read only, one page read).

        Purpose: proof whether an InMail with status unknown actually went
        out. Opens the inbox filter and picks "Gesendet"/"Sent"; nothing is
        typed or sent. Per row: recipient, when (LinkedIn's time text),
        subject plus subject_source (labelled, or line_before_snippet = a
        position guess to check in the thread; null if none), text_start (snippet,
        at most 200 characters), own_last (snippet starts with "Sie:"/"You:",
        so the last message is ours), thread_url. complete is true when the
        list ended before limit (1-200); boundary names limit or end_of_list.

        Statuses: ok, filter_not_found, inbox_list_not_loaded, sent_option_not_found,
        view_not_confirmed (filter label did not read the sent view, or
        reason list_unchanged: the list still equals the inbox list; no rows,
        so received threads are never passed off as sent).
        Refusals: invalid_limit, pace_budget_spent, pace_lock_busy.
        """
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 200
        ):
            return {"status": "invalid_limit", "detail": "limit must be 1-200"}
        spent = _pace("page_read", tool="sales_nav_sent_inmails")
        if spent:
            return spent
        return await _run(
            ctx, "sales_nav_sent_inmails", lambda ex: _reader(ex).sent_inmails(limit)
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

        Tool deadline: answered before it with deadline_reached=true:
        not_sent (retry_safe=true, deadline before the save click, nothing
        saved) or unknown (retry_safe=false: re-read the thread first).
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
                result = await _before_deadline(
                    lambda: reader.edit(url, message, new_text, confirm=True),
                    lambda: getattr(reader, "clicked", True),
                )
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
            if isinstance(result, _DeadlineHit):
                return {**base, **_book_deadline(ledger, attempt, result, "not_sent")}
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
