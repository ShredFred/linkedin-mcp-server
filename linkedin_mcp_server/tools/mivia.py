"""MiViA fork tools.

Read tools: list_connections, get_event_attendees, list_sent_invitations.
Write tools: create_post (dry run unless confirm_post), send_message_verified
(send + read-back), send_campaign_batch (canary -> small staggered batches under
daily caps with an idempotency ledger), connect_guarded (invite caps), and
outreach_quota (read-only ledger state).

Everything is tagged ``mivia`` so the upstream tool-contract test can ignore
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

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from linkedin_mcp_server import mivia_outreach as outreach
from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    InvalidReferenceError,
)
from linkedin_mcp_server.mivia_message_checks import (
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
from linkedin_mcp_server.linkedin.mivia_network import (
    MiviaNetworkReader,
    project_attendees,
)
from linkedin_mcp_server.linkedin.mivia_post import MiviaPostComposer

logger = logging.getLogger(__name__)

TAG = "mivia"
# Randomised gap between two sends inside one batch (seconds).
SEND_GAP = (25.0, 70.0)
BATCH_MAX = 3
BATCH_TIMEOUT_SECONDS = 600.0
_THREAD_RE = re.compile(r"/messaging/thread/([A-Za-z0-9_=-]+)/")


def _network(extractor: Any) -> MiviaNetworkReader:
    return MiviaNetworkReader(extractor.mivia_session, extractor.mivia_navigator)


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
# invite budget was booked. Raise it via MIVIA_INVITE_NOTE_MAX (max 300).
INVITE_NOTE_MAX_PREMIUM = 300
INVITE_NOTE_MAX_DEFAULT = 200
INVITE_NOTE_MAX_ENV = "MIVIA_INVITE_NOTE_MAX"


def invite_note_max() -> int:
    raw = os.environ.get(INVITE_NOTE_MAX_ENV, "").strip()
    try:
        value = int(raw) if raw else INVITE_NOTE_MAX_DEFAULT
    except ValueError:
        return INVITE_NOTE_MAX_DEFAULT
    return max(1, min(value, INVITE_NOTE_MAX_PREMIUM))


def ledger_corrupt_status(exc: "outreach.LedgerCorrupt") -> dict[str, Any]:
    """One status for a corrupt outreach ledger in every mivia tool."""
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

    guarded._mivia_ledger_guarded = True  # type: ignore[attr-defined]
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


# One source for the content rules: mivia_message_checks. The underscore
# names stay as aliases because mivia_stage2/inmail and tests import them.
_utf16_len = utf16_len
_hidden_format_char = hidden_format_char


_CLICK_DECIDES = frozenset(
    {"follow_only", "unavailable", "connect_unavailable", "custom_note_limit_reached"}
)


def _event_id(value: str) -> tuple[str | None, dict[str, Any] | None]:
    """Normalised event id, or a refusal before anything is booked: the
    reader rejects a non-numeric id only after _pace had charged the budget."""
    from linkedin_mcp_server.linkedin.mivia_network import _EVENT_ID_RE

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

    Same rules as the invite note and the InMail (mivia_message_checks.
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


def _composer(extractor: Any) -> MiviaPostComposer:
    return MiviaPostComposer(extractor.mivia_session, extractor.mivia_navigator)


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
        tool="send_message_verified",
        duplicate=duplicate,
    )
    if refused:
        return {"recipient": username, "verified": False, **refused}
    try:
        sent = await extractor.send_message(username, message, confirm_send=True)
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
        exc.mivia_send_status = "not_sent"  # type: ignore[attr-defined]
        raise
    except BaseException:
        # Cancellation (tool timeout): the click may have happened.
        ledger.append(
            {"attempt": attempt, "status": "unknown", "detail": "exception during send"}
        )
        raise
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
    await asyncio.sleep(random.uniform(3.0, 6.0))
    verified = False
    # Read back through the thread the send landed in. Looking the conversation
    # up by username failed on the first live test (2026-09-29): the inbox
    # lookup did not find a thread it had just written to.
    # The send result carries the compose URL; the page itself has moved on to
    # the thread by now. Failing both, the newest inbox thread is the one just
    # written to.
    thread = _THREAD_RE.search(str(sent.get("url", ""))) or _THREAD_RE.search(
        str(getattr(extractor.mivia_session.page, "url", ""))
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


def register_mivia_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    raw_mcp = mcp
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
            from linkedin_mcp_server.linkedin.mivia_network import SearchLimitReached

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
    ) -> dict[str, Any]:
        """
        Compose a post on the signed-in member's personal profile. Multi-line
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
            as_company: Posting as a company page is not implemented; the member
                must be a page admin and that is posted from the browser.
        """
        if as_company:
            return {
                "status": "not_supported",
                "posted": False,
                "message": "Company-page posting is not implemented; it requires page admin rights and stays manual.",
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
        if not confirm_post:
            # Dry run: nothing is published, nothing is booked.
            return await _run(
                ctx,
                "create_post",
                lambda ex: _composer(ex).create_post(
                    text, image_path=image_path, confirm_post=False
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
                result = await composer.create_post(
                    text, image_path=image_path, confirm_post=True
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
            status = _POST_LEDGER_STATUS.get(result.get("status"), "not_posted")
            outcome: dict[str, Any] = {
                "attempt": attempt,
                "status": status,
                "detail": result.get("status"),
            }
            if result.get("activity_id"):
                outcome["activity"] = result["activity_id"]
            ledger.append(outcome)
            return result

        return await _run(ctx, "create_post", body)

    @mcp.tool(
        timeout=tool_timeout,
        title="Send Message Verified",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={TAG, "messaging", "actions"},
    )
    async def send_message_verified(
        linkedin_username: str,
        message: str,
        confirm_send: bool,
        ctx: Context,
        allow_repeat: bool = False,
    ) -> dict[str, Any]:
        """
        Send one message (multi-line allowed via LF) and read the conversation
        back to confirm the whole text arrived. Every attempt is recorded in the
        outreach ledger; the same text is never sent twice to the same person
        unless allow_repeat is true (intended for test sends to the canary only).

        Returns status verified / unverified / not_sent / unknown / duplicate;
        dry_run without confirm_send; refusals before any send:
        content_check_failed, repeat_not_allowed (allow_repeat to a
        non-canary), pace_budget_spent.
        """
        username, bad = _recipient(linkedin_username)
        if bad:
            return bad
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
        refusal = _pace("message", tool="send_message_verified")
        if refusal:
            return {"recipient": username, **refusal}
        return await _run(
            ctx,
            "send_message_verified",
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
        """
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
            for index, username in enumerate(targets[:take]):
                if index:
                    await asyncio.sleep(random.uniform(*SEND_GAP))
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
                    status = getattr(exc, "mivia_send_status", "unknown")
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
        title="Connect Guarded",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={TAG, "network", "actions"},
    )
    async def connect_guarded(
        linkedin_username: str,
        confirm_send: bool,
        ctx: Context,
        note: str | None = None,
        invites_per_day: Annotated[
            int, Field(ge=1, le=outreach.INVITES_PER_DAY_MAX)
        ] = outreach.INVITES_PER_DAY_DEFAULT,
    ) -> dict[str, Any]:
        """
        connect_with_person behind the ledger: refuses once today's invite cap
        (default 20, max 25) or the rolling 7-day cap (100) is reached, and never
        invites the same person twice. The attempted row is the booking: it is
        written under the pacer lock, after the duplicate check is repeated
        there, and before connect_with_person runs -- an attempt that dies
        mid-dialog may still have sent, so it must count. An unrecognised result
        is recorded as unknown, which blocks a retry. The note is limited to 200
        characters (free account) unless MIVIA_INVITE_NOTE_MAX raises it to at
        most 300 (Premium).
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
        refusal = _pace("invite", tool="connect_guarded")
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
                tool="connect_guarded",
                duplicate=lambda: ledger.already_contacted("invite", username, None),
            )
            if refused:
                return {"recipient": username, **refused}
            try:
                res = await ex.connect_with_person(username, note=note)
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

        return await _run(ctx, "connect_guarded", body)

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
        Loads the canary profile (frederikstadler), resolves its Message action
        and profile URN and its connection state; optionally classifies one
        not-connected profile (may open its More menu, never clicks an item).
        Sends nothing, invites nobody. ok=false lists the problems.
        """
        if connect_probe_username is not None:
            connect_probe_username, bad = _recipient(connect_probe_username)
            if bad:
                return {**bad, "field": "connect_probe_username"}
        pages = 2 if connect_probe_username else 1
        refusal = _pace("page_read", pages, tool="outreach_selftest")
        if refusal:
            return refusal
        from linkedin_mcp_server.linkedin.mivia_selftest import (
            outreach_selftest as run_selftest,
        )

        return await _run(
            ctx,
            "outreach_selftest",
            lambda ex: run_selftest(
                ex.mivia_session,
                ex.mivia_navigator,
                canary=outreach.DEFAULT_CANARY,
                connect_probe=connect_probe_username,
            ),
        )

    from linkedin_mcp_server.tools.mivia_stage2 import register_mivia_stage2_tools

    register_mivia_stage2_tools(raw_mcp, tool_timeout=tool_timeout)

    from linkedin_mcp_server.tools.mivia_inmail import register_mivia_inmail_tools

    register_mivia_inmail_tools(raw_mcp, tool_timeout=tool_timeout)

    from linkedin_mcp_server.tools.mivia_own_content import (
        register_mivia_own_content_tools,
    )

    register_mivia_own_content_tools(raw_mcp, tool_timeout=tool_timeout)
