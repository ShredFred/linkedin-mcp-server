"""fork tools.

Read tools: list_connections, get_event_attendees, list_sent_invitations.
Write tools: create_post (dry run unless confirm_post), send_message
(dry run unless confirm_send) (send + read-back), send_campaign_batch (canary -> small staggered batches under
daily caps with an idempotency ledger), connect_with_person (invite caps), and
outreach_quota (read-only ledger state).

Everything is tagged ``ext`` so the upstream tool-contract test can ignore
this module, which keeps upstream merges free of fixture conflicts.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import random
import re
import uuid
from datetime import date, datetime, timedelta
from collections.abc import Callable
from typing import Annotated, Any, Literal

import anyio
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from linkedin_mcp_server import ext_outreach as outreach
from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    InvalidReferenceError,
)
from linkedin_mcp_server.ext_message_checks import (
    MESSAGE_MAX_UTF16,
    check_message,
    check_outgoing,
    hidden_format_char,
    utf16_len,
)
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.linkedin.contracts import (
    refuse_an_invalid_message,
)
from linkedin_mcp_server.linkedin.identifiers import normalize_person_identifier
from linkedin_mcp_server.linkedin.ext_network import (
    ExtNetworkReader,
    project_attendees,
)
from linkedin_mcp_server.linkedin.ext_mentions import has_markup, plain_text, prepare_text
from linkedin_mcp_server.linkedin.ext_post import ExtPostComposer
from linkedin_mcp_server.linkedin.message_sender import (
    _send_budget_deadline as _reply_budget_deadline,
)

logger = logging.getLogger(__name__)

TAG = "ext"
# Randomised gap between two sends inside one batch (seconds).
SEND_GAP = (25.0, 70.0)
BATCH_MAX = 3
BATCH_TIMEOUT_SECONDS = 600.0
_THREAD_RE = re.compile(r"/messaging/thread/([A-Za-z0-9_=-]+)/")


def _network(extractor: Any) -> ExtNetworkReader:
    return ExtNetworkReader(extractor.ext_session, extractor.ext_navigator)


def _pace(action: str, count: int = 1, *, tool: str) -> dict[str, Any] | None:
    """Ask the pacer; return a refusal dict when the budget is spent."""
    try:
        outreach.Pacer(outreach.Ledger.default()).take(action, count, tool=tool)
    except outreach.PaceExceeded as spent:
        return {"status": "pace_budget_spent", "pace": spent.state}
    except TimeoutError as busy:
        return pace_lock_busy(busy)
    return None


def pace_lock_busy(exc: BaseException) -> dict[str, Any]:
    """One status for a pacer lock that stayed held past its timeout."""
    return {
        "status": "pace_lock_busy",
        "detail": f"{exc}; nothing was sent or booked, try again shortly",
    }


def _recipient(value: str | None) -> tuple[str | None, dict[str, Any] | None]:
    """Normalise a person identifier; (None, refusal) instead of raising."""
    try:
        return normalize_person_identifier(value or ""), None
    except InvalidReferenceError as bad:
        return None, {"status": "invalid_recipient", "detail": str(bad), "input": value}


# LinkedIn allows 300 characters with Premium and 200 without. 200 is the safe
# default: a longer note on a free account fails in the dialog, after the
# invite budget was booked. Raise it via LINKEDIN_MCP_INVITE_NOTE_MAX (max 300).
INVITE_NOTE_MAX_PREMIUM = 300
INVITE_NOTE_MAX_DEFAULT = 200
INVITE_NOTE_MAX_ENV = "LINKEDIN_MCP_INVITE_NOTE_MAX"


def invite_note_max() -> int:
    raw = os.environ.get(INVITE_NOTE_MAX_ENV, "").strip()
    try:
        value = int(raw) if raw else INVITE_NOTE_MAX_DEFAULT
    except ValueError:
        return INVITE_NOTE_MAX_DEFAULT
    return max(1, min(value, INVITE_NOTE_MAX_PREMIUM))


def ledger_corrupt_status(exc: "outreach.LedgerCorrupt") -> dict[str, Any]:
    """One status for a corrupt outreach ledger in every ext tool."""
    return {
        "status": "ledger_corrupt",
        "line": exc.line,
        "ledger": str(exc.path),
        "detail": "repair or move the ledger row; nothing was sent or booked",
    }


def _ledger_guard(fn: Any) -> Any:
    @functools.wraps(fn)
    async def guarded(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except outreach.LedgerCorrupt as exc:
            return ledger_corrupt_status(exc)

    guarded._ext_ledger_guarded = True  # type: ignore[attr-defined]
    return guarded


class _GuardedMcp:
    """FastMCP proxy whose tool decorator maps LedgerCorrupt to a status."""

    def __init__(self, mcp: FastMCP):
        self._mcp = mcp

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        register = self._mcp.tool(*args, **kwargs)
        return lambda fn: register(_ledger_guard(fn))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._mcp, name)


# One source for the content rules: ext_message_checks. The underscore
# names stay as aliases because ext_stage2/inmail and tests import them.
_utf16_len = utf16_len
_hidden_format_char = hidden_format_char


_CLICK_DECIDES = frozenset(
    {"follow_only", "unavailable", "connect_unavailable", "custom_note_limit_reached"}
)


def _event_id(value: str) -> tuple[str | None, dict[str, Any] | None]:
    """Normalised event id, or a refusal before anything is booked: the
    reader rejects a non-numeric id only after _pace had charged the budget."""
    from linkedin_mcp_server.linkedin.ext_network import _EVENT_ID_RE

    event_id = str(value or "").strip().strip("/").rsplit("/", 1)[-1]
    if not _EVENT_ID_RE.match(event_id):
        return None, {
            "status": "invalid_input",
            "field": "event_id",
            "detail": "numeric event id from linkedin.com/events/<id>/",
        }
    return event_id, None


def _int_in(value: Any, low: int, high: int) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool) and low <= value <= high
    )


def check_invite_note(note: str | None) -> dict[str, Any] | None:
    """Browser-free refusal for a connection note; None when it may go."""
    if note is None:
        return None
    if not note.strip():
        return {
            "status": "invalid_note",
            "detail": "note is empty; pass null for no note",
        }
    if any(ord(c) < 32 or _hidden_format_char(c) for c in note):
        return {
            "status": "invalid_note",
            "detail": "no control characters or line breaks",
        }
    limit = invite_note_max()
    if _utf16_len(note) > limit:
        return {"status": "note_too_long", "max": limit}
    findings = [
        f for f in check_message(note, None) if f["code"] != "salutation_unverifiable"
    ]
    if findings:
        return {"status": "content_check_failed", "findings": findings}
    return None


def check_message_content(message: str) -> dict[str, Any] | None:
    """Browser-free content refusal for a direct message; None when it may go.

    Same rules as the invite note and the InMail (ext_message_checks.
    check_outgoing): hidden characters, length, an unfilled template, a
    non-https link, a shortener or a foreign Calendly account -- bare
    "www.x" / "bit.ly/x" included -- stop the send before anything is booked.
    The salutation is not checked here: the recipient is not known yet.
    """
    return check_outgoing(message, max_utf16=MESSAGE_MAX_UTF16)


def _peek(action: str, count: int = 1) -> dict[str, Any] | None:
    """Check the pacer without booking; refusal dict when the budget is spent."""
    try:
        outreach.Pacer(outreach.Ledger.default()).peek(action, count)
    except outreach.PaceExceeded as spent:
        return {"status": "pace_budget_spent", "pace": spent.state}
    except TimeoutError as busy:
        return pace_lock_busy(busy)
    return None


# create_post (2026-10-01): the same text is not published twice within this
# window; repeated posts are a spam signal and a post_unconfirmed may be live.
POST_REPEAT_DAYS = 30
_POST_REPEAT_BLOCKING = {"attempted", "unknown", "posted", "posted_verified"}
# Composer result -> ledger status. posted_unverified was published (the
# composer closed); post_unconfirmed may have been. Everything else did not
# reach the publish click or stopped before it and releases the text.
_POST_LEDGER_STATUS = {
    "posted_verified": "posted_verified",
    "posted_unverified": "posted",
    "post_unconfirmed": "unknown",
}


def _composer(extractor: Any) -> ExtPostComposer:
    return ExtPostComposer(extractor.ext_session, extractor.ext_navigator)


async def _run(ctx: Context, name: str, body: Any) -> dict[str, Any]:
    try:
        extractor = await get_ready_extractor(ctx, tool_name=name)
        return await body(extractor)
    except (ToolError, outreach.LedgerCorrupt):
        raise
    except AuthenticationError as e:
        try:
            await handle_auth_error(e, ctx)
        except Exception as relogin_exc:
            raise_tool_error(relogin_exc, name)
    except Exception as e:
        raise_tool_error(e, name)
    raise AssertionError("unreachable")


# -- answering before the tool deadline ---------------------------------------
#
# FastMCP runs a tool inside anyio.fail_after(); a deadline that lands there
# discards whatever the tool would have returned, and the client sees only
# "Error calling tool" -- no status, no retry_safe. Upstream #1233 fixed that
# for send_message by running dispatch and confirmation under an earlier
# budget (message_sender._send_budget_deadline: the tool deadline minus a
# reserve). Every fork write tool uses the same budget through the helpers
# below, so it answers itself while the reserve is left:
#
# - budget ran out before the click marker was set -> nothing left; the tool
#   books and answers its not-done status (not_sent / not_posted / not_done)
#   with retry_safe=true, which releases the ledger block.
# - budget ran out after the marker -> unknown, retry_safe=false, blocking.
# - budget ran out in a read-back after a confirmed send -> unverified,
#   retry_safe=false (see _send_and_verify).
#
# A cancellation the budget does not own (client cancel, server shutdown)
# still propagates; the tools' ``except BaseException`` branches book it by
# the same click marker. Without the marker set, the click provably did not
# happen (every reader sets it *before* the click call), so that, too, is a
# releasing not-done row. A reader without a marker fails closed (unknown).


class _DeadlineHit:
    """The write's budget ran out; ``clicked`` says whether a click may be out."""

    __slots__ = ("clicked",)

    def __init__(self, clicked: bool) -> None:
        self.clicked = clicked


def _marker_set(clicked: Callable[[], Any]) -> bool:
    """Read a click marker fail-closed: unreadable counts as clicked."""
    try:
        return bool(clicked())
    except Exception:
        return True


def _write_budget() -> float:
    """When a write tool's work has to stop to still answer (event-loop clock)."""
    return _reply_budget_deadline()


async def _before_deadline(
    act: Callable[[], Any],
    clicked: Callable[[], Any],
    *,
    budget: float | None = None,
) -> Any:
    """Run ``act()`` under the reply budget; a ``_DeadlineHit`` when it ran out.

    Too little time left to start at all is a hit before any click.
    """
    end = _write_budget() if budget is None else budget
    if anyio.current_time() >= end:
        return _DeadlineHit(False)
    with anyio.CancelScope(deadline=end):
        return await act()
    return _DeadlineHit(_marker_set(clicked))


def _deadline_answer(hit: _DeadlineHit, not_done: str) -> dict[str, Any]:
    """The client-facing part of a budget stop (``not_done`` before the click)."""
    if hit.clicked:
        return {
            "status": "unknown",
            "retry_safe": False,
            "deadline_reached": True,
            "detail": "The tool deadline arrived after the click; the action "
            "may have gone through. Check by hand, do not retry.",
        }
    return {
        "status": not_done,
        "retry_safe": True,
        "deadline_reached": True,
        "detail": "The tool deadline arrived before the click; nothing was "
        "done. Safe to retry.",
    }


def _book_deadline(
    ledger: outreach.Ledger, attempt: str, hit: _DeadlineHit, not_done: str
) -> dict[str, Any]:
    """Close the attempt row for a budget stop and return the client answer."""
    ledger.append(
        {
            "attempt": attempt,
            "status": "unknown" if hit.clicked else not_done,
            "detail": "tool deadline after the click"
            if hit.clicked
            else "tool deadline before the click",
        }
    )
    return _deadline_answer(hit, not_done)


async def _send_and_verify(
    extractor: Any,
    ledger: outreach.Ledger,
    username: str,
    message: str,
    *,
    campaign: str | None,
    allow_repeat: bool = False,
    quota_check: Callable[[], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """One send with a ledger row before and after, then a conversation read-back.

    The attempted row is written by the pacer under its lock, after the
    duplicate check is repeated there: two parallel calls for the same person
    and text both pass the check outside the lock, only one may send.
    """
    sha = outreach.text_sha(message)
    attempt = uuid.uuid4().hex
    started = datetime.now().astimezone().isoformat(timespec="seconds")
    row = {
        "attempt": attempt,
        "kind": "message",
        "recipient": outreach.recipient_key(username),
        "text_sha": sha,
        "text_head": outreach.text_head(message),
        "text_anchor": outreach.text_anchor(message),
        "campaign": campaign,
        "status": "attempted",
        "started_at": started,
    }

    def duplicate() -> dict[str, Any] | None:
        # Runs under the pacer lock, right before the attempt row is written:
        # the campaign's own day quota is re-read here, so a parallel batch
        # that booked in between is counted (BATCH_QUOTA_RACE).
        if quota_check is not None:
            over = quota_check()
            if over is not None:
                raise _CampaignQuotaReached(over)
        if allow_repeat:
            return None
        return ledger.already_contacted("message", username, sha)

    refused = _book_attempt(
        ledger,
        "message",
        row,
        tool="send_message",
        duplicate=duplicate,
    )
    if refused:
        return {"recipient": username, "verified": False, **refused}
    # One budget for send and read-back, read while the whole call is ahead.
    budget = _write_budget()
    try:
        sent = await _before_deadline(
            lambda: extractor.send_message(username, message, confirm_send=True),
            lambda: _message_dispatched(extractor),
            budget=budget,
        )
    except Exception as exc:
        # message_sender's contract: once the submit may have been dispatched
        # it returns send_unconfirmed instead of raising, so an Exception that
        # gets here was raised before the click. Booking it as unknown blocked
        # the person for good over a page that merely failed to load.
        ledger.append(
            {
                "attempt": attempt,
                "status": "not_sent",
                "detail": f"exception before send: {type(exc).__name__}",
            }
        )
        exc.ext_send_status = "not_sent"  # type: ignore[attr-defined]
        raise
    except BaseException:
        # Cancellation the budget does not own (client cancel, shutdown). The
        # sender sets its dispatch marker before the submit click, so a marker
        # still unset proves nothing left: releasing not_sent. Set, or an
        # extractor without one: the click may have happened -> unknown.
        if _message_dispatched(extractor):
            ledger.append(
                {
                    "attempt": attempt,
                    "status": "unknown",
                    "detail": "exception during send",
                }
            )
        else:
            ledger.append(
                {
                    "attempt": attempt,
                    "status": "not_sent",
                    "detail": "cancelled before the submit click",
                }
            )
        raise
    if isinstance(sent, _DeadlineHit):
        return {
            "recipient": username,
            "verified": False,
            **_book_deadline(ledger, attempt, sent, "not_sent"),
        }
    if not sent.get("sent"):
        status = "not_sent" if sent.get("retry_safe") else "unknown"
        ledger.append(
            {"attempt": attempt, "status": status, "detail": sent.get("status")}
        )
        return {
            "recipient": username,
            "status": status,
            "send": sent,
            "verified": False,
        }
    try:
        read = await _before_deadline(
            lambda: _read_back(extractor, ledger, attempt, username, message, sent),
            lambda: True,
            budget=budget,
        )
        if not isinstance(read, _DeadlineHit):
            return read
        # The send was confirmed, only the read-back was cut by the budget.
        _close_unverified(ledger, attempt, "read-back cut by the tool deadline")
        return {
            "recipient": username,
            "status": "unverified",
            "send": sent,
            "verified": False,
            "retry_safe": False,
            "deadline_reached": True,
            "detail": "Sent, but the tool deadline cut the read-back. Check "
            "the thread, do not resend.",
        }
    except BaseException:
        # R7: a cancellation (tool timeout) during the read-back left the row
        # at "attempted". The send had already answered sent=True, so the row
        # is closed as unverified -- blocking and counted, never not_sent.
        # Synchronous append: an await here would be cancelled again at once.
        # A final row already written (verified) is never downgraded.
        _close_unverified(ledger, attempt, "read-back interrupted")
        raise


def _message_dispatched(extractor: Any) -> bool:
    """The sender's dispatch marker; an extractor without one fails closed."""
    return _marker_set(lambda: getattr(extractor, "message_submit_dispatched", True))


def _close_unverified(ledger: outreach.Ledger, attempt: str, detail: str) -> None:
    """Close a sent attempt as unverified unless a final row already exists."""
    try:
        done = ledger.latest_by_attempt().get(attempt, {}).get("status")
    except Exception:
        done = None
    if done not in ("verified", "unverified"):
        ledger.append({"attempt": attempt, "status": "unverified", "detail": detail})


async def _read_back(
    extractor: Any,
    ledger: outreach.Ledger,
    attempt: str,
    username: str,
    message: str,
    sent: dict[str, Any],
) -> dict[str, Any]:
    """Conversation read-back after a send that answered sent=True."""
    await asyncio.sleep(random.uniform(3.0, 6.0))
    verified = False
    # Read back through the thread the send landed in. Looking the conversation
    # up by username failed on the first live test (2026-09-29): the inbox
    # lookup did not find a thread it had just written to.
    # The send result carries the compose URL; the page itself has moved on to
    # the thread by now. Failing both, the newest inbox thread is the one just
    # written to.
    thread = _THREAD_RE.search(str(sent.get("url", ""))) or _THREAD_RE.search(
        str(getattr(extractor.ext_session.page, "url", ""))
    )
    lookups: list[dict[str, str]] = []
    # A thread taken from the inbox is a guess: the newest thread may belong to
    # someone who just wrote to us. It only counts when checked (see below).
    guessed: set[str] = set()
    if thread:
        lookups.append({"thread_id": thread.group(1)})
    else:
        try:
            inbox = await extractor.get_inbox(3)
            for ref in (inbox.get("references") or {}).get("inbox", []):
                match = _THREAD_RE.search(str(ref.get("url", "")))
                if ref.get("kind") == "conversation" and match:
                    thread = match
                    lookups.append({"thread_id": match.group(1)})
                    guessed.add(match.group(1))
                    break
        except Exception:
            logger.warning("inbox read for read-back failed", exc_info=True)
    lookups.append({"linkedin_username": username})
    for lookup in lookups:
        try:
            conversation = await extractor.get_conversation(**lookup)
        except Exception:
            logger.warning("read-back via %s failed", lookup, exc_info=True)
            continue
        verified = outreach.delivered_in_conversation(message, conversation)
        if verified and lookup.get("thread_id") in guessed:
            verified = _thread_belongs_to(conversation, username, message)
        if verified:
            break
    status = "verified" if verified else "unverified"
    ledger.append(
        {
            "attempt": attempt,
            "status": status,
            "thread": thread.group(1) if thread else None,
        }
    )
    return {"recipient": username, "status": status, "send": sent, "verified": verified}


# _send_and_verify outcomes after which nothing reached the recipient.
_NOTHING_LEFT = {"not_sent", "pace_budget_spent", "pace_lock_busy"}


class _CampaignQuotaReached(Exception):
    """The batch's own messages_per_day is spent, read under the pacer lock."""

    def __init__(self, quota: dict[str, Any]) -> None:
        super().__init__("campaign quota reached")
        self.quota = quota


def _book_attempt(
    ledger: outreach.Ledger,
    kind: str,
    row: dict[str, Any],
    *,
    tool: str,
    duplicate: Any = None,
) -> dict[str, Any] | None:
    """Write an attempted row through the pacer lock; refusal dict or None."""
    try:
        outreach.Pacer(ledger).take(kind, tool=tool, row=row, duplicate=duplicate)
    except outreach.AlreadyContacted as dup:
        return {"status": "duplicate", "previous": dup.previous}
    except outreach.PaceExceeded as over:
        return {"status": "pace_budget_spent", "pace": over.state}
    except _CampaignQuotaReached as spent:
        return {"status": "campaign_quota_reached", "quota": spent.quota}
    except TimeoutError as busy:
        return pace_lock_busy(busy)
    return None


def _thread_belongs_to(
    conversation: dict[str, Any], username: str, message: str
) -> bool:
    """A thread guessed from the inbox counts only when it is the recipient's
    and our message is the newest block in it.

    Partner: a person reference in the thread points at the recipient's slug.
    Newest: nobody else wrote after our text (reply_after finds no later block).
    """
    key = outreach.recipient_key(username)
    partner = any(
        f"/in/{key}/" in str(ref.get("url", "")).lower() + "/"
        for refs in (conversation.get("references") or {}).values()
        for ref in (refs or [])
        if isinstance(ref, dict)
    )
    if not partner:
        return False
    text = "\n".join(str(v) for v in (conversation.get("sections") or {}).values())
    after = outreach.reply_after(text, message)
    return bool(after.get("found")) and after.get("replied") is False


def missing_deployment_values() -> list[str]:
    """Deployment values that are unset, each with what it disables.

    Both fail closed when missing; this only says so at start-up instead of at
    the first campaign send or the first message with a booking link."""
    from linkedin_mcp_server.ext_message_checks import (
        CALENDLY_ACCOUNT_ENV,
        calendly_account,
    )

    missing = []
    if not outreach.DEFAULT_CANARY:
        missing.append(
            f"{outreach.CANARY_ENV} (send_campaign_batch and outreach_selftest refuse)"
        )
    if not calendly_account():
        missing.append(f"{CALENDLY_ACCOUNT_ENV} (every Calendly link is refused)")
    return missing


def register_ext_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    for item in missing_deployment_values():
        logger.warning("fork setting not configured: %s", item)
    raw_mcp = mcp
    # A detected rate limit books a cooldown and blocks later navigations.
    outreach.install_rate_limit_coupling()
    mcp = _GuardedMcp(mcp)  # type: ignore[assignment]

    @mcp.tool(
        timeout=tool_timeout,
        title="List Connections",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={TAG, "network"},
    )
    async def list_connections(
        ctx: Context,
        since: str | None = None,
        limit: Annotated[int, Field(ge=1, le=1000)] = 200,
    ) -> dict[str, Any]:
        """
        List the account's 1st-degree connections, newest first ("Neu hinzugefügt"),
        with the date each connection was made.

        Args:
            since: Optional ISO date (YYYY-MM-DD). Only connections made on or
                after it are returned, and scrolling stops once older ones appear.
            limit: Maximum number of connections to return (default 200).

        Returns:
            Dict with count and connections [{name, slug, profile_url,
            profile_urn, headline, connected_on (ISO date or null)}].
            Refusals: invalid_input (field names the bad argument),
            pace_budget_spent (pacer budget spent: do not retry now, see
            pace_status), pace_lock_busy (pacer lock held: nothing booked,
            retry shortly).
        """
        try:
            since_date = date.fromisoformat(since) if since else None
        except (TypeError, ValueError):
            return {"status": "invalid_input", "field": "since", "detail": "YYYY-MM-DD"}
        if not _int_in(limit, 1, 1000):
            return {"status": "invalid_input", "field": "limit"}
        refusal = _pace("page_read", tool="list_connections")
        if refusal:
            return refusal
        return await _run(
            ctx,
            "list_connections",
            lambda ex: _network(ex).list_connections(since_date, limit),
        )

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Event Attendees",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={TAG, "network", "search"},
    )
    async def get_event_attendees(
        event_id: str,
        ctx: Context,
        start_page: Annotated[int, Field(ge=1, le=100)] = 1,
        max_pages: Annotated[int, Field(ge=1, le=15)] = 10,
        limit: Annotated[int | None, Field(ge=1, le=150)] = None,
        fields: Literal["minimal", "card"] = "minimal",
    ) -> dict[str, Any]:
        """
        List the attendees of a LinkedIn event via people search with
        eventAttending, 10 per page, at human pace (3-6 s between pages).

        Each attendee carries name, slug, profile_url, profile_urn, degree (1/2/3,
        0 for the account itself), headline, location and action -- the rendered
        button state: message, connect, pending, follow, self or unknown. The
        tool waits for the late-rendering buttons before reading a page.

        Args:
            event_id: Numeric event id from linkedin.com/events/<id>/.
            start_page: First results page to read (default 1).
            max_pages: Pages to read in this call (default 10). For long lists
                call again with start_page=next_page until complete is true.
            limit: Stop after this many attendees; pages are capped to
                ceil(limit / 10), and only those pages are charged to the budget.
            fields: "minimal" (slug, name, headline, degree -- the default) or
                "card" (adds location, button state, page; same card). Neither
                opens a profile. "readable": false means the first page was
                empty -- usually the account has not RSVP'd.

        Refusals: invalid_input (field names the bad argument),
        pace_budget_spent (pacer budget spent: do not retry now, see
        pace_status), pace_lock_busy (pacer lock held: nothing booked, retry
        shortly), monthly_search_limit (LinkedIn's monthly people-search wall: stop
        searching until next month, retrying burns nothing but time).
        """
        event_id, bad = _event_id(event_id)
        if bad:
            return bad
        # Validated before the booking: a call the reader rejects must not
        # spend search budget (start_page >= 1, max_pages 1-100 there).
        if not _int_in(start_page, 1, 100):
            return {"status": "invalid_input", "field": "start_page"}
        if not _int_in(max_pages, 1, 15):
            return {"status": "invalid_input", "field": "max_pages"}
        if limit is not None and not _int_in(limit, 1, 150):
            return {"status": "invalid_input", "field": "limit"}
        if fields not in ("minimal", "card"):
            return {"status": "invalid_input", "field": "fields"}
        if limit is not None:
            max_pages = min(max_pages, -(-limit // 10))
        refusal = _pace("search", max_pages, tool="get_event_attendees")
        if refusal:
            return refusal

        async def body(ex: Any) -> dict[str, Any]:
            from linkedin_mcp_server.linkedin.ext_network import SearchLimitReached

            try:
                raw = await _network(ex).get_event_attendees(
                    event_id, start_page, max_pages
                )
            except SearchLimitReached as exc:
                # Same lock as the collector: manual calls stop hitting the wall too.
                outreach.Pacer(outreach.Ledger.default()).record_limit_hit(
                    "search", tool="get_event_attendees"
                )
                return {"status": "monthly_search_limit", "detail": str(exc)}
            return project_attendees(raw, limit, fields)

        return await _run(ctx, "get_event_attendees", body)

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Event Attendee Count",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={TAG, "network"},
    )
    async def get_event_attendee_count(event_id: str, ctx: Context) -> dict[str, Any]:
        """
        Read the attendee total of a LinkedIn event from its event page -- one
        page view, no people search, no profile. Cheap check before harvesting:
        call get_event_attendees only when the total grew.

        Returns:
            Dict with event_id and attendee_count (null when the page shows no
            total, e.g. the event is gone or the layout changed).
            Refusals: invalid_input (event_id not numeric),
            pace_budget_spent (pacer budget spent: do not retry now, see
            pace_status), pace_lock_busy (pacer lock held: nothing booked,
            retry shortly).
        """
        event_id, bad = _event_id(event_id)
        if bad:
            return bad
        refusal = _pace("page_read", tool="get_event_attendee_count")
        if refusal:
            return refusal
        return await _run(
            ctx,
            "get_event_attendee_count",
            lambda ex: _network(ex).event_attendee_count(event_id),
        )

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Event Status",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={TAG, "network"},
    )
    async def get_event_status(event_id: str, ctx: Context) -> dict[str, Any]:
        """
        One event page view (page_read budget, no people search): attendee
        total and whether the account itself has RSVP'd. Nothing is clicked.

        Returns:
            event_id, attendee_count, own_rsvp (true = "Networking" tab shown,
            false = "Teilnehmen"/"Attend" button shown, null = unknown),
            attend_button {text, disabled} or null, networking_tab, and
            acting_as (text of a page-actor switch if the page offers one),
            gone (page missing or redirected away from /events/) and
            cancelled (LinkedIn's cancelled banner as its own line).
            Refusals: invalid_input (event_id not numeric),
            pace_budget_spent (pacer budget spent: do not retry now, see
            pace_status), pace_lock_busy (pacer lock held: nothing booked,
            retry shortly).
        """
        event_id, bad = _event_id(event_id)
        if bad:
            return bad
        refusal = _pace("page_read", tool="get_event_status")
        if refusal:
            return refusal
        return await _run(
            ctx,
            "get_event_status",
            lambda ex: _network(ex).event_status(event_id),
        )

    @mcp.tool(
        timeout=tool_timeout,
        title="List Sent Invitations",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={TAG, "network"},
    )
    async def list_sent_invitations(
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=1000)] = 500,
    ) -> dict[str, Any]:
        """
        List pending sent connection invitations. The page renders 10 at a time;
        the tool scrolls LinkedIn's list container until it stops growing, and
        reports complete=true when the count matches the page header total.

        Returns:
            Dict with count, header_total, complete and invitations [{name, slug,
            profile_url, headline, sent_text}]. sent_text is LinkedIn's relative
            wording ("Vor 18 Stunden gesendet"), kept as rendered.
            Refusals: invalid_input (limit out of range),
            pace_budget_spent (pacer budget spent: do not retry now, see
            pace_status), pace_lock_busy (pacer lock held: nothing booked,
            retry shortly).
        """
        if not _int_in(limit, 1, 1000):
            return {"status": "invalid_input", "field": "limit"}
        refusal = _pace("page_read", tool="list_sent_invitations")
        if refusal:
            return refusal
        return await _run(
            ctx,
            "list_sent_invitations",
            lambda ex: _network(ex).list_sent_invitations(limit),
        )

    @mcp.tool(
        timeout=tool_timeout,
        title="Create Post",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={TAG, "post", "actions"},
    )
    async def create_post(
        text: str,
        ctx: Context,
        confirm_post: bool = False,
        image_path: str | None = None,
        as_company: str | None = None,
        company_name: str | None = None,
        mentions: list[dict[str, str]] | None = None,
        mention_check: str = "warn",
        media: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """
        Compose a post on the signed-in member's personal profile (or, with
        as_company, on a company page the member administers). Multi-line
        text (LF) is supported. Without confirm_post=true this is a dry run: the
        editor is filled and verified, then cleared again; nothing is published.
        With confirm_post=true the post is published (public, irreversible
        without manual deletion) and read back from recent activity. Budget:
        post pacer (3/day, 10/week); the same text is refused for 30 days after
        any attempt that may have published (including post_unconfirmed).

        Args:
            text: Post text; LF separates paragraphs. Other control characters
                are refused.
            confirm_post: Must be true to publish.
            image_path: Optional local image file to attach.
            as_company: Post as this company page instead: numeric page id,
                company URL or slug (a slug is resolved on the page; it must
                yield exactly one id). Runs the create_company_post flow in
                mode=publish; company_name is then required, because the
                composer's author is verified against it.
            company_name: The page name as the composer shows it.
            mentions: Optional list of {"name", "target"}; the first plain
                occurrence of each name becomes a real @-mention. target is a
                profile slug/URL/URN or company:<slug|id>. Inline markup works
                too: [[Name|slug]], [[Name|company:slug]].
            media: Images, in order: [{"path", "alt_text", "tags": [{"name",
                "target"}]}] (max 20; png/jpg/gif/webp). Alt text is typed
                into LinkedIn's dialog and read back by reopening it; a tag is
                picked only by the suggestion's identifier, and the tag count
                LinkedIn shows is read back. A dry run uploads, checks
                everything, and discards (measured: no draft is left).
                Stops: media_invalid_path, media_kind_unmeasured (video,
                documents), media_too_many, alt_text_invalid,
                media_tag_invalid, media_button_unavailable,
                media_upload_incomplete, media_count_mismatch,
                media_order_mismatch, media_thumbnail_unavailable,
                alt_text_control_unavailable, alt_text_not_taken,
                alt_text_confirm_unavailable, alt_text_not_saved,
                media_tag_control_unavailable, media_tag_confirm_unavailable,
                media_tags_not_saved, media_tag_unverifiable (page composer:
                its tag suggestions carry no identifier), image_editor_stuck.
                Not together with image_path.
            mention_check: off | warn (default: plaintext_names in the result)
                | strict (mention_plaintext_name stops when a name to be
                mentioned also stands as plain text).

        Mentions are typed key by key into the typeahead and picked only when
        the suggestion's identifier equals the target -- never by name or
        position alone; the inserted entity is read back. Stops (nothing
        posted, also in a dry run): mention_ambiguous, mention_not_resolved,
        mention_list_not_loaded, mention_target_unresolved,
        mention_wrong_entity, mention_not_linked, mention_missing,
        mention_name_mismatch, mention_unverifiable; refusals before the
        browser: mention_syntax_invalid, mention_markup_required (a bare
        @word), mention_name_not_in_text, mention_plaintext_name;
        invalid_input (mention_check not off/warn/strict, or as_company
        without company_name / not a page reference).

        Returns the composer result; its status is posted_verified (live and
        read back), posted_unverified (published, read-back missed it: check
        recent activity, do not repost), post_unconfirmed (may be live: do
        not repost, check recent activity), dry_run, or a stop before the
        publish click (composer_unavailable, editor_has_media, invalid_image,
        post_button_disabled, post_button_unavailable: safe to fix and retry).
        Refusals before anything is booked: not_supported (as_company),
        invalid_text (empty, control characters or > 3000 chars),
        duplicate_text (same text attempted within 30 days; previous holds
        the row), pace_budget_spent (post budget spent: wait), pace_lock_busy
        (retry shortly). All refusals carry posted=false.

        Tool deadline: answered before it with deadline_reached=true and
        posted=false: not_posted (retry_safe=true, deadline before the
        publish click) or unknown (retry_safe=false, may be live: check
        recent activity, do not repost).
        """
        segments, mention_info, bad = prepare_text(text, mentions, mention_check)
        if bad:
            return {"posted": False, **mention_info, **bad}
        from linkedin_mcp_server.linkedin.ext_media import check_media

        media_items, bad = check_media(media)
        if bad:
            return {"posted": False, **bad}
        if media_items and image_path:
            return {
                "status": "invalid_input",
                "field": "media",
                "posted": False,
                "message": "Use media or image_path, not both.",
            }
        if as_company:
            from linkedin_mcp_server.tools.ext_company_post import (
                parse_company_ref,
                run_company_post,
            )

            page_ref, bad = parse_company_ref(as_company)
            if bad:
                return {"posted": False, **bad}
            if not (company_name or "").strip():
                return {
                    "status": "invalid_input",
                    "field": "company_name",
                    "posted": False,
                    "message": "as_company needs company_name: the composer's author is verified against it.",
                }
            return await run_company_post(
                ctx,
                page_ref,
                str(company_name),
                text,
                image_path=image_path,
                mode="publish",
                scheduled_at=None,
                confirm=confirm_post,
                mentions=mentions,
                mention_check=mention_check,
                media=media,
                tool="create_post",
            )
        text = plain_text(segments)
        # Only a text with mentions needs the segments; plain text keeps the
        # composer call it always had.
        extra: dict[str, Any] = (
            {"segments": segments} if any(k == "mention" for k, _ in segments) else {}
        )
        if media_items:
            extra["media"] = media_items
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

        # The same text again within POST_REPEAT_DAYS is refused: any attempt
        # that may have published (attempted, unknown = post_unconfirmed,
        # posted = unverified) blocks it like a verified one.
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
        warn = (
            {"plaintext_names": mention_info["plaintext_names"]}
            if mention_info.get("plaintext_names")
            else {}
        )
        if not confirm_post:
            # Dry run: nothing is published, nothing is booked.
            return warn | await _run(
                ctx,
                "create_post",
                lambda ex: _composer(ex).create_post(
                    text,
                    image_path=image_path,
                    confirm_post=False,
                    **extra,
                ),
            )
        # Peek only: the attempt row below is the booking, written under the
        # pacer lock together with the repeated duplicate check.
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
                    "started_at": datetime.now()
                    .astimezone()
                    .isoformat(timespec="seconds"),
                },
                tool="create_post",
                duplicate=repeat,
            )
            if refused:
                if refused["status"] == "duplicate":
                    refused = {**refused, "status": "duplicate_text"}
                return {"posted": False, **refused}
            composer = _composer(ex)
            try:
                result = await _before_deadline(
                    lambda: composer.create_post(
                        text,
                        image_path=image_path,
                        confirm_post=True,
                        **extra,
                    ),
                    lambda: getattr(composer, "clicked", True),
                )
            except BaseException:
                clicked = getattr(composer, "clicked", True)
                ledger.append(
                    {
                        "attempt": attempt,
                        "status": "unknown" if clicked else "not_posted",
                        "detail": "exception after the publish click"
                        if clicked
                        else "exception before the publish click",
                    }
                )
                raise
            if isinstance(result, _DeadlineHit):
                return {
                    "posted": False,
                    **_book_deadline(ledger, attempt, result, "not_posted"),
                }
            status = _POST_LEDGER_STATUS.get(result.get("status"), "not_posted")
            outcome: dict[str, Any] = {
                "attempt": attempt,
                "status": status,
                "detail": result.get("status"),
            }
            if result.get("activity_id"):
                outcome["activity"] = result["activity_id"]
            ledger.append(outcome)
            return {**result, **warn}

        return await _run(ctx, "create_post", body)

    @mcp.tool(
        timeout=tool_timeout,
        title="Send Message",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={TAG, "messaging", "actions"},
    )
    async def send_message(
        linkedin_username: str,
        message: str,
        confirm_send: bool,
        ctx: Context,
        allow_repeat: bool = False,
    ) -> dict[str, Any]:
        """
        Without confirm_send=true this is a dry run only: nothing is sent.
        Send one message (multi-line allowed via LF) and read the conversation
        back to confirm the whole text arrived. Every attempt is recorded in the
        outreach ledger; the same text is never sent twice to the same person
        unless allow_repeat is true (intended for test sends to the canary only).

        Returns status verified / unverified / not_sent / unknown / duplicate;
        dry_run without confirm_send; refusals before any send:
        content_check_failed, repeat_not_allowed (allow_repeat to a
        non-canary), pace_budget_spent.

        What to do: verified = done. unverified = sent, read-back missed it;
        check the thread, do not resend. unknown = the click may have gone
        out; never retry, check the thread by hand. not_sent = nothing left,
        safe to retry. duplicate = already in the ledger (previous holds the
        row). invalid_recipient (bad username/URL), invalid_message (empty,
        control or invisible characters), content_check_failed
        (findings list the rule), message_too_long (over the length cap),
        pace_lock_busy (nothing booked, retry shortly).
        mention_not_supported_in_messages: the text carries [[Name|slug]]
        markup; a LinkedIn message has no @-mention entity (documented limit).

        Tool deadline: the tool answers before it instead of failing.
        deadline_reached=true with not_sent (retry_safe=true: the deadline
        came before the submit click), unknown (retry_safe=false: after it)
        or unverified (retry_safe=false: sent, the read-back was cut).
        """
        username, bad = _recipient(linkedin_username)
        if bad:
            return bad
        if has_markup(message):
            # Documented limit (2026-10-09): a 1:1 message has no @-mention
            # entity; the markup would arrive as literal brackets.
            return {
                "recipient": username,
                "status": "mention_not_supported_in_messages",
                "message": "LinkedIn messages carry no @-mentions; write the name as plain text.",
            }
        refusal = refuse_an_invalid_message(username, message)
        if refusal is not None:
            return refusal
        content = check_message_content(message)
        if content:
            return {"recipient": username, **content}
        canary_key = outreach.recipient_key(outreach.DEFAULT_CANARY)
        if allow_repeat and outreach.recipient_key(username) != canary_key:
            # A repeat to a real person is a second copy of the same text in
            # their inbox -- the retry after an "unknown" is exactly that case.
            return {
                "recipient": username,
                "status": "repeat_not_allowed",
                "detail": "allow_repeat is for the canary only",
            }
        ledger = outreach.Ledger.default()
        previous = ledger.already_contacted(
            "message", username, outreach.text_sha(message)
        )
        if previous and not allow_repeat:
            return {"recipient": username, "status": "duplicate", "previous": previous}
        if not confirm_send:
            return {
                "recipient": username,
                "status": "dry_run",
                "ledger": str(ledger.path),
            }
        refusal = _pace("message", tool="send_message")
        if refusal:
            return {"recipient": username, **refusal}
        return await _run(
            ctx,
            "send_message",
            lambda ex: _send_and_verify(
                ex, ledger, username, message, campaign=None, allow_repeat=allow_repeat
            ),
        )

    @mcp.tool(
        timeout=BATCH_TIMEOUT_SECONDS,
        title="Send Campaign Batch",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={TAG, "messaging", "actions"},
    )
    async def send_campaign_batch(
        message: str,
        recipients: list[str],
        campaign: str,
        confirm_send: bool,
        ctx: Context,
        canary: str = outreach.DEFAULT_CANARY,
        batch_size: Annotated[int, Field(ge=1, le=BATCH_MAX)] = 2,
        messages_per_day: Annotated[
            int, Field(ge=1, le=outreach.MESSAGES_PER_DAY_MAX)
        ] = outreach.MESSAGES_PER_DAY_DEFAULT,
    ) -> dict[str, Any]:
        """
        Send the same text to many recipients safely, one small batch per call.

        1. Canary first: until this exact text has been sent to the canary and
           read back verified, the call sends only to the canary and stops.
        2. Afterwards each call sends to at most batch_size (<=3) recipients not
           yet in the ledger for this text, with 25-70 s random gaps, within the
           daily message cap (default 30, hard max 40; canary sends excluded).
        3. It stops at the first recipient that is not read back verified.

        Call repeatedly (spread over the day) until remaining is empty. With
        confirm_send=false it only reports the plan (status dry_run). Refusals
        before any send: content_check_failed, campaign_required,
        invalid_recipients, pace_budget_spent, campaign_quota_reached.

        Batch statuses: canary_verified (canary read back, call again to
        start), canary_failed (stopped; check the canary thread before a
        retry), batch_sent (all of this batch verified; call again while
        remaining is non-empty), done (nothing left), daily_cap_reached
        (continue tomorrow), campaign_quota_reached (campaign day quota spent
        under the lock; nothing booked for remaining), stopped_on_failure
        (a recipient was not verified; results[-1].status is verified /
        unverified / unknown / not_sent / duplicate / pace_budget_spent /
        pace_lock_busy -- unknown and unverified must not be resent, remaining
        excludes them; deadline_reached=true marks a stop by the tool deadline,
        retry_safe says whether that recipient may be sent again),
        stopped_on_deadline (the next pause would run past the tool deadline;
        nothing in flight, call again for remaining). Other refusals: canary_not_configured (no canary:
        LINKEDIN_MCP_CANARY unset and none passed), invalid_recipient (bad canary, with
        field=canary), invalid_message, message_too_long, pace_lock_busy
        (retry shortly).
        """
        if not (canary or "").strip():
            return {
                "status": "canary_not_configured",
                "detail": f"set {outreach.CANARY_ENV} or pass canary",
            }
        canary_key, bad = _recipient(canary)
        if bad:
            return {**bad, "field": "canary"}
        refusal = refuse_an_invalid_message(canary_key, message)
        if refusal is not None:
            return refusal
        content = check_message_content(message)
        if content:
            return content
        if not campaign or not campaign.strip():
            return {"status": "campaign_required"}
        invalid = []
        normalized = []
        for raw in recipients:
            username, bad = _recipient(raw)
            (invalid.append(bad) if bad else normalized.append(username))
        if invalid:
            # A typo is refused as a whole, not dropped: a silently shorter
            # list is a recipient nobody notices was never written to.
            return {"status": "invalid_recipients", "invalid": invalid}
        ledger = outreach.Ledger.default()
        sha = outreach.text_sha(message)
        targets = []
        skipped = []
        seen: set[str] = set()
        for username in normalized:
            key = outreach.recipient_key(username)
            # "Dieter" and ".../in/dieter/" twice in one list: the ledger check
            # below runs before any send, so it would pass both into one batch.
            if key == outreach.recipient_key(canary_key) or key in seen:
                continue
            seen.add(key)
            (
                skipped
                if ledger.already_contacted("message", username, sha)
                else targets
            ).append(username)
        q = outreach.quota(
            ledger,
            messages_per_day=messages_per_day,
            invites_per_day=0,
            canary=canary_key,
        )
        plan: dict[str, Any] = {
            "campaign": campaign,
            "text_sha": sha,
            "canary": canary_key,
            "canary_verified": ledger.canary_verified(sha, canary_key),
            "already_sent": skipped,
            "remaining": targets,
            "quota": q,
        }
        if not confirm_send:
            return {**plan, "status": "dry_run"}
        if plan["canary_verified"] and not targets:
            # Nothing left to send: answered without charging the pacer.
            return {**plan, "status": "done", "results": []}
        refusal = _pace("message", tool="send_campaign_batch")
        if refusal:
            return {**plan, **refusal}

        async def body(ex: Any) -> dict[str, Any]:
            if not plan["canary_verified"]:
                # The canary may be retried after a failed read-back.
                outcome = await _send_and_verify(
                    ex,
                    ledger,
                    canary_key,
                    message,
                    campaign=campaign,
                    allow_repeat=True,
                )
                status = "canary_verified" if outcome["verified"] else "canary_failed"
                return {
                    **plan,
                    "status": status,
                    "results": [outcome],
                    "next": "call again to start the batches"
                    if outcome["verified"]
                    else "stopped",
                }
            # The pacer also binds the week (200) and the daily total of visible
            # actions, which the day quota alone does not see.
            pace_left = outreach.Pacer(ledger).state("message")["left"]
            take = min(batch_size, q["messages_left_today"], pace_left, len(targets))
            if take == 0:
                return {
                    **plan,
                    "status": "done" if not targets else "daily_cap_reached",
                    "results": [],
                }

            def quota_check() -> dict[str, Any] | None:
                # q above was read before the lock; a parallel batch may have
                # booked since. Re-counted under the pacer lock per recipient.
                now = outreach.quota(
                    ledger,
                    messages_per_day=messages_per_day,
                    invites_per_day=0,
                    canary=canary_key,
                )
                return now if now["messages_left_today"] <= 0 else None

            results = []
            batch_budget = _write_budget()
            for index, username in enumerate(targets[:take]):
                if index:
                    gap = random.uniform(*SEND_GAP)
                    if anyio.current_time() + gap >= batch_budget:
                        # The pause would run into the tool deadline and the
                        # answer, verified sends included, would be lost.
                        return {
                            **plan,
                            "status": "stopped_on_deadline",
                            "results": results,
                            "remaining": targets[index:],
                            "retry_safe": True,
                        }
                    await asyncio.sleep(gap)
                try:
                    outcome = await _send_and_verify(
                        ex,
                        ledger,
                        username,
                        message,
                        campaign=campaign,
                        quota_check=quota_check,
                    )
                except outreach.LedgerCorrupt:
                    raise
                except Exception as exc:
                    # The ledger already holds this attempt (not_sent before the
                    # click, unknown otherwise). Raising here dropped the
                    # verified sends before it from the report, and the caller
                    # read the whole batch as failed.
                    status = getattr(exc, "ext_send_status", "unknown")
                    results.append(
                        {
                            "recipient": username,
                            "status": status,
                            "verified": False,
                            "error": f"{type(exc).__name__}: {exc}"[:300],
                        }
                    )
                    return {
                        **plan,
                        "status": "stopped_on_failure",
                        "results": results,
                        "remaining": targets[
                            index if status in _NOTHING_LEFT else index + 1 :
                        ],
                    }
                if outcome.get("status") == "campaign_quota_reached":
                    # Nothing was booked or sent for this recipient.
                    return {
                        **plan,
                        "status": "campaign_quota_reached",
                        "quota": outcome["quota"],
                        "results": results,
                        "remaining": targets[index:],
                    }
                results.append(outcome)
                if not outcome["verified"]:
                    # A recipient nothing left for (not_sent, a pacer refusal,
                    # a busy lock) is still open; dropping it from remaining
                    # reported a person as handled who never got the text.
                    still_open = outcome.get("status") in _NOTHING_LEFT
                    return {
                        **plan,
                        "status": "stopped_on_failure",
                        "results": results,
                        "remaining": targets[index if still_open else index + 1 :],
                    }
            return {
                **plan,
                "status": "batch_sent",
                "results": results,
                "remaining": targets[take:],
            }

        return await _run(ctx, "send_campaign_batch", body)

    @mcp.tool(
        timeout=tool_timeout,
        title="Connect With Person",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={TAG, "network", "actions"},
    )
    async def connect_with_person(
        linkedin_username: str,
        confirm_send: bool,
        ctx: Context,
        note: str | None = None,
        invites_per_day: Annotated[
            int, Field(ge=1, le=outreach.INVITES_PER_DAY_MAX)
        ] = outreach.INVITES_PER_DAY_DEFAULT,
    ) -> dict[str, Any]:
        """
        Without confirm_send=true this is a dry run only: nothing is sent.
        Connection request behind the ledger: refuses once today's invite cap
        (default 20, max 25) or the rolling 7-day cap (100) is reached, and never
        invites the same person twice. The attempted row is the booking: it is
        written under the pacer lock, after the duplicate check is repeated
        there, and before the dialog runs -- an attempt that dies
        mid-dialog may still have sent, so it must count. An unrecognised result
        is recorded as unknown, which blocks a retry. The note is limited to 200
        characters (free account) unless LINKEDIN_MCP_INVITE_NOTE_MAX raises it to at
        most 300 (Premium).

        On a send the response has no status; result.status is the dialog
        outcome: connected/accepted (invite left), pending/already_connected
        (nothing new), send_failed (may have left: do not retry), follow_only,
        unavailable, connect_unavailable, custom_note_limit_reached (no invite
        if the click did not happen; the ledger decides). Refusals:
        invalid_recipient, invalid_note (empty or control characters),
        note_too_long (max holds the limit), content_check_failed (findings),
        duplicate (person already invited), cap_reached (today's invite cap;
        continue tomorrow), dry_run (confirm_send=false, quota shown),
        pace_budget_spent (wait), pace_lock_busy (retry shortly).

        Tool deadline: answered before it with a top-level status and
        deadline_reached=true: not_sent (retry_safe=true, deadline before the
        send click, no invite booked) or unknown (retry_safe=false, may have
        left; the person stays blocked).
        """
        username, bad = _recipient(linkedin_username)
        if bad:
            return bad
        refusal = check_invite_note(note)
        if refusal:
            return {"recipient": username, **refusal}
        ledger = outreach.Ledger.default()
        previous = ledger.already_contacted("invite", username, None)
        if previous:
            return {"recipient": username, "status": "duplicate", "previous": previous}
        q = outreach.quota(ledger, messages_per_day=0, invites_per_day=invites_per_day)
        if q["invites_left_today"] <= 0:
            return {"recipient": username, "status": "cap_reached", "quota": q}
        if not confirm_send:
            return {"recipient": username, "status": "dry_run", "quota": q}
        refusal = _pace("invite", tool="connect_with_person")
        if refusal:
            return {"recipient": username, **refusal}

        async def body(ex: Any) -> dict[str, Any]:
            attempt = uuid.uuid4().hex
            row = {
                "attempt": attempt,
                "kind": "invite",
                "recipient": outreach.recipient_key(username),
                "status": "attempted",
                "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            refused = _book_attempt(
                ledger,
                "invite",
                row,
                tool="connect_with_person",
                duplicate=lambda: ledger.already_contacted("invite", username, None),
            )
            if refused:
                return {"recipient": username, **refused}
            try:
                res = await _before_deadline(
                    lambda: ex.connect_with_person(username, note=note),
                    lambda: getattr(ex, "invite_send_clicked", True),
                )
            except BaseException:
                # An extractor without the click marker is treated as clicked:
                # fail closed, the attempt blocks a retry.
                clicked = bool(getattr(ex, "invite_send_clicked", True))
                ledger.append(
                    {
                        "attempt": attempt,
                        "status": "unknown" if clicked else "not_sent",
                        "detail": "exception after the send click"
                        if clicked
                        else "exception before the send click",
                    }
                )
                raise
            if isinstance(res, _DeadlineHit):
                return {
                    "recipient": username,
                    **_book_deadline(ledger, attempt, res, "not_sent"),
                }
            clicked = bool(getattr(ex, "invite_send_clicked", True))
            raw = str(res.get("status", ""))
            # connected/accepted: an invitation left or one was accepted.
            # pending/already_connected: nothing new left -> not counted.
            # send_failed: may or may not have left -> unknown, blocks a retry.
            status = {
                "connected": "sent",
                "accepted": "sent",
                "pending": "skipped",
                "already_connected": "skipped",
                "send_failed": "unknown",
                # Anything unrecognised may have sent: blocking, not retry-safe.
            }.get(raw, "unknown")
            # follow_only, unavailable, connect_unavailable and
            # custom_note_limit_reached normally come before any send click,
            # but can follow one (Enter fallback, a dialog that closes slowly,
            # a button that turned into Follow after the click). Only the
            # click marker can tell: not clicked -> not_sent and retry-safe;
            # possibly clicked -> unknown, which blocks a second invitation.
            if raw in _CLICK_DECIDES:
                status = "unknown" if clicked else "not_sent"
            ledger.append({"attempt": attempt, "status": status, "detail": raw})
            return {
                "recipient": username,
                "result": res,
                "quota": outreach.quota(
                    ledger, messages_per_day=0, invites_per_day=invites_per_day
                ),
            }

        return await _run(ctx, "connect_with_person", body)

    @mcp.tool(
        timeout=30,
        title="Outreach Quota",
        annotations={"readOnlyHint": True},
        tags={TAG, "messaging"},
    )
    async def outreach_quota(
        messages_per_day: Annotated[
            int, Field(ge=1, le=outreach.MESSAGES_PER_DAY_MAX)
        ] = outreach.MESSAGES_PER_DAY_DEFAULT,
        invites_per_day: Annotated[
            int, Field(ge=1, le=outreach.INVITES_PER_DAY_MAX)
        ] = outreach.INVITES_PER_DAY_DEFAULT,
    ) -> dict[str, Any]:
        """Today's and this week's sends and invites from the local outreach ledger."""
        return outreach.quota(
            outreach.Ledger.default(),
            messages_per_day=messages_per_day,
            invites_per_day=invites_per_day,
            canary=outreach.DEFAULT_CANARY,
        )

    @mcp.tool(
        timeout=tool_timeout,
        title="Outreach Self-Test",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={TAG, "messaging", "network"},
    )
    async def outreach_selftest(
        ctx: Context,
        connect_probe_username: str | None = None,
    ) -> dict[str, Any]:
        """
        Read-only check that the Message and Connect actions still resolve, run
        before a batch so a LinkedIn UI change is caught before the first send.
        Loads the canary profile (LINKEDIN_MCP_CANARY), resolves its Message action
        and profile URN and its connection state; optionally classifies one
        not-connected profile (may open its More menu, never clicks an item).
        Sends nothing, invites nobody. ok=false lists the problems.
        Refusals: canary_not_configured (LINKEDIN_MCP_CANARY unset),
        invalid_recipient (bad connect_probe_username, with field),
        pace_budget_spent (wait), pace_lock_busy (retry shortly).
        """
        if connect_probe_username is not None:
            connect_probe_username, bad = _recipient(connect_probe_username)
            if bad:
                return {**bad, "field": "connect_probe_username"}
        if not outreach.DEFAULT_CANARY:
            return {
                "status": "canary_not_configured",
                "detail": f"set {outreach.CANARY_ENV} to the canary profile",
            }
        pages = 2 if connect_probe_username else 1
        refusal = _pace("page_read", pages, tool="outreach_selftest")
        if refusal:
            return refusal
        from linkedin_mcp_server.linkedin.ext_selftest import (
            outreach_selftest as run_selftest,
        )

        return await _run(
            ctx,
            "outreach_selftest",
            lambda ex: run_selftest(
                ex.ext_session,
                ex.ext_navigator,
                canary=outreach.DEFAULT_CANARY,
                connect_probe=connect_probe_username,
            ),
        )

    from linkedin_mcp_server.tools.ext_stage2 import register_ext_stage2_tools

    register_ext_stage2_tools(raw_mcp, tool_timeout=tool_timeout)

    from linkedin_mcp_server.tools.ext_inmail import register_ext_inmail_tools

    register_ext_inmail_tools(raw_mcp, tool_timeout=tool_timeout)

    from linkedin_mcp_server.tools.ext_own_content import (
        register_ext_own_content_tools,
    )

    register_ext_own_content_tools(raw_mcp, tool_timeout=tool_timeout)
