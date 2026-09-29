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
import logging
import random
import re
import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from linkedin_mcp_server import mivia_outreach as outreach
from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.scraping.contracts import refuse_an_invalid_message
from linkedin_mcp_server.scraping.identifiers import normalize_person_identifier
from linkedin_mcp_server.scraping.mivia_network import (
    MiviaNetworkReader,
    project_attendees,
)
from linkedin_mcp_server.scraping.mivia_post import MiviaPostComposer

logger = logging.getLogger(__name__)

TAG = "mivia"
# Randomised gap between two sends inside one batch (seconds).
SEND_GAP = (25.0, 70.0)
BATCH_MAX = 3
BATCH_TIMEOUT_SECONDS = 600.0
_THREAD_RE = re.compile(r"/messaging/thread/([A-Za-z0-9_=-]+)/")


def _network(extractor: Any) -> MiviaNetworkReader:
    return MiviaNetworkReader(extractor._mivia_session, extractor._mivia_navigator)


def _pace(action: str, count: int = 1, *, tool: str) -> dict[str, Any] | None:
    """Ask the pacer; return a refusal dict when the budget is spent."""
    try:
        outreach.Pacer(outreach.Ledger.default()).take(action, count, tool=tool)
    except outreach.PaceExceeded as spent:
        return {"status": "pace_budget_spent", "pace": spent.state}
    return None


def _composer(extractor: Any) -> MiviaPostComposer:
    return MiviaPostComposer(extractor._mivia_session, extractor._mivia_navigator)


async def _run(ctx: Context, name: str, body: Any) -> dict[str, Any]:
    try:
        extractor = await get_ready_extractor(ctx, tool_name=name)
        return await body(extractor)
    except ToolError:
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
) -> dict[str, Any]:
    """One send with a ledger row before and after, then a conversation read-back."""
    sha = outreach.text_sha(message)
    attempt = uuid.uuid4().hex
    started = datetime.now().astimezone().isoformat(timespec="seconds")
    ledger.append(
        {
            "attempt": attempt,
            "kind": "message",
            "recipient": outreach.recipient_key(username),
            "text_sha": sha,
            "text_head": outreach.text_head(message),
            "campaign": campaign,
            "status": "attempted",
            "started_at": started,
        }
    )
    try:
        sent = await extractor.send_message(username, message, confirm_send=True)
    except BaseException:
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
        str(getattr(extractor._mivia_session.page, "url", ""))
    )
    lookups: list[dict[str, str]] = []
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


def register_mivia_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    @mcp.tool(
        timeout=tool_timeout,
        title="List Connections",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={TAG, "network", "scraping"},
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
        since_date = date.fromisoformat(since) if since else None
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
        tags={TAG, "network", "search", "scraping"},
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
        event_id = event_id.strip().strip("/").rsplit("/", 1)[-1]
        if limit is not None:
            max_pages = min(max_pages, -(-limit // 10))
        refusal = _pace("search", max_pages, tool="get_event_attendees")
        if refusal:
            return refusal

        async def body(ex: Any) -> dict[str, Any]:
            from linkedin_mcp_server.scraping.mivia_network import SearchLimitReached

            try:
                raw = await _network(ex).get_event_attendees(event_id, start_page, max_pages)
            except SearchLimitReached as exc:
                # Same lock as the collector: manual calls stop hitting the wall too.
                outreach.Pacer(outreach.Ledger.default()).record_limit_hit(
                    "search", tool="get_event_attendees")
                return {"status": "monthly_search_limit", "detail": str(exc)}
            return project_attendees(raw, limit, fields)

        return await _run(ctx, "get_event_attendees", body)

    @mcp.tool(
        timeout=tool_timeout,
        title="List Sent Invitations",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={TAG, "network", "scraping"},
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
        without manual deletion) and read back from recent activity.

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
            (ord(c) < 32 and c != "\n") or ord(c) == 127 for c in text
        ):
            return {
                "status": "invalid_text",
                "posted": False,
                "message": "Text must be non-empty and contain no control characters other than LF.",
            }
        if len(text) > 3000:
            return {
                "status": "invalid_text",
                "posted": False,
                "message": "LinkedIn posts are limited to 3000 characters.",
            }
        return await _run(
            ctx,
            "create_post",
            lambda ex: _composer(ex).create_post(
                text, image_path=image_path, confirm_post=confirm_post
            ),
        )

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

        Returns status verified / unverified / not_sent / unknown / duplicate.
        """
        refusal = refuse_an_invalid_message(linkedin_username, message)
        if refusal is not None:
            return refusal
        username = normalize_person_identifier(linkedin_username)
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
            lambda ex: _send_and_verify(ex, ledger, username, message, campaign=None),
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
        confirm_send=false it only reports the plan.
        """
        refusal = refuse_an_invalid_message(canary, message)
        if refusal is not None:
            return refusal
        ledger = outreach.Ledger.default()
        sha = outreach.text_sha(message)
        canary_key = normalize_person_identifier(canary)
        targets = []
        skipped = []
        for raw in recipients:
            username = normalize_person_identifier(raw)
            if outreach.recipient_key(username) == outreach.recipient_key(canary_key):
                continue
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
        refusal = _pace("message", tool="send_campaign_batch")
        if refusal:
            return {**plan, **refusal}

        async def body(ex: Any) -> dict[str, Any]:
            if not plan["canary_verified"]:
                outcome = await _send_and_verify(
                    ex, ledger, canary_key, message, campaign=campaign
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
            results = []
            for index, username in enumerate(targets[:take]):
                if index:
                    await asyncio.sleep(random.uniform(*SEND_GAP))
                outcome = await _send_and_verify(
                    ex, ledger, username, message, campaign=campaign
                )
                results.append(outcome)
                if not outcome["verified"]:
                    return {
                        **plan,
                        "status": "stopped_on_failure",
                        "results": results,
                        "remaining": targets[index + 1 :],
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
        invites the same person twice. Records every attempt.
        """
        username = normalize_person_identifier(linkedin_username)
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
            ledger.append(
                {
                    "attempt": attempt,
                    "kind": "invite",
                    "recipient": outreach.recipient_key(username),
                    "status": "attempted",
                    "started_at": datetime.now()
                    .astimezone()
                    .isoformat(timespec="seconds"),
                }
            )
            try:
                res = await ex.connect_with_person(username, note=note)
            except BaseException:
                ledger.append({"attempt": attempt, "status": "unknown"})
                raise
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
            }.get(raw, "not_sent")
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

    from linkedin_mcp_server.tools.mivia_stage2 import register_mivia_stage2_tools

    register_mivia_stage2_tools(mcp, tool_timeout=tool_timeout)
