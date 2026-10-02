"""MiViA fork, stage 2: engagement, event invitations, follow-ups, pacing.

Registered from :func:`linkedin_mcp_server.tools.mivia.register_mivia_tools`,
tagged ``mivia`` like the rest of the fork.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import Field

from linkedin_mcp_server import mivia_outreach as outreach
from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.linkedin.contracts import is_invisible_control
from linkedin_mcp_server.linkedin.mivia_actions import MiviaActions, parse_group_id
from linkedin_mcp_server.linkedin.mivia_network import (
    _EVENT_ID_RE as _NETWORK_EVENT_ID_RE,
)
from linkedin_mcp_server.linkedin.mivia_events import MiviaEventFinder, event_summary
from linkedin_mcp_server.linkedin.mivia_repost import MiviaReposter
from linkedin_mcp_server.linkedin.mivia_engagement import (
    MiviaEngagementReader,
    SeenStore,
    engager_key,
    parse_activity_id,
)
from linkedin_mcp_server.tools.mivia import (
    BATCH_TIMEOUT_SECONDS,
    TAG,
    _book_attempt,
    _GuardedMcp,
    _hidden_format_char,
    _pace,
    _peek,
    _recipient,
    _run,
    _utf16_len,
    pace_lock_busy,
)


# A repost row in one of these states may be live and blocks a second repost.
_REPOST_OPEN = {"attempted", "unknown", "reposted", "unverified"}
_REPOST_DONE = {"reposted", "unverified"}


def _engagement(extractor: Any) -> MiviaEngagementReader:
    return MiviaEngagementReader(extractor.mivia_session, extractor.mivia_navigator)


def _actions(extractor: Any) -> MiviaActions:
    return MiviaActions(extractor.mivia_session, extractor.mivia_navigator)


def _activity(post_url: str) -> tuple[str | None, dict[str, Any] | None]:
    """The activity id, or a clear refusal instead of an exception."""
    try:
        return parse_activity_id(post_url), None
    except (ValueError, AttributeError) as bad:
        return None, {"status": "invalid_post_url", "message": str(bad)[:300]}


def register_mivia_stage2_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    mcp = _GuardedMcp(mcp)  # type: ignore[assignment]

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Post Engagers",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={TAG, "post"},
    )
    async def get_post_engagers(
        post_url: str,
        ctx: Context,
        since_last_run: bool = False,
        include_reactors: bool = True,
        include_comments: bool = True,
        limit: Annotated[int, Field(ge=1, le=500)] = 200,
    ) -> dict[str, Any]:
        """
        Who reacted to a post (with reaction kind) and who commented.

        Reactors come from the post-analytics list, which LinkedIn renders for
        the account's own posts and for posts of pages it administers. For
        anyone else's post (AWT, exhibitors) only the reaction count and the
        commenters are available: the reactor list sits behind the member's own
        reaction toggle, and this tool never clicks it.

        Args:
            post_url: Post URL (feed/update/urn:li:activity:..., /posts/...-activity-<id>-...)
                or the activity URN / id.
            since_last_run: Return only engagers not reported for this post
                before (local memory ~/.linkedin-mcp/mivia-engagers-seen.json);
                every call updates the memory.
            limit: Maximum reactors to read.

        Returns:
            reactors [{name, degree, headline, kind member/company, id, id_type
            member_id/vanity, profile_url, reaction}], comments [{name, degree,
            headline, age, text, id, profile_url, comment_id, is_reply}],
            reaction_count, reactors_available, new_only.

        Leads from this list are a professional activity signal only: company
        first, suppression check and the cadence tracker decide what is used.

        reactors_complete=false: the reactor list was cut at limit or did not
        finish scrolling. reactors_available=false comes with
        reactors_unavailable_reason (not our post): use reaction_count.
        Refusals: invalid_post_url (not a post URL/URN), pace_budget_spent (wait, see pace_status),
        pace_lock_busy (nothing booked, retry shortly).
        """
        activity_id, bad = _activity(post_url)
        if bad:
            return bad
        # Worst case: the analytics list plus the post page, which is read even
        # without include_comments when the reactor list is unavailable.
        pages = int(include_reactors) + int(include_comments or include_reactors)
        refusal = _pace("page_read", max(pages, 1), tool="get_post_engagers")
        if refusal:
            return refusal

        async def body(ex: Any) -> dict[str, Any]:
            reader = _engagement(ex)
            result: dict[str, Any] = {
                "activity_id": activity_id,
                "post_url": f"https://www.linkedin.com/feed/update/urn:li:activity:{activity_id}/",
            }
            reactors: list[dict[str, Any]] = []
            if include_reactors:
                r = await reader.read_reactors(activity_id, limit)
                reactors = r["reactors"]
                result["reactors_available"] = r["available"]
                if r["available"]:
                    result["reactors_complete"] = r["complete"]
                else:
                    result["reactors_unavailable_reason"] = r["reason"]
            comments: list[dict[str, Any]] = []
            if include_comments or not result.get("reactors_available", True):
                page = await reader.read_post_page(activity_id)
                result["reaction_count"] = page["reaction_count"]
                if include_comments:
                    comments = page["comments"]
            store = SeenStore()
            known = store.keys(activity_id)

            def rkey(r: dict[str, Any]) -> str:
                return engager_key("reaction", r["id"], r["reaction"], name=r["name"])

            def ckey(c: dict[str, Any]) -> str:
                return engager_key(
                    "comment", c["id"], c["comment_id"] or "", name=c["name"]
                )

            keys = {rkey(r) for r in reactors} | {ckey(c) for c in comments}
            if since_last_run:
                reactors = [r for r in reactors if rkey(r) not in known]
                comments = [c for c in comments if ckey(c) not in known]
            # Anonymous keys ('anon=<uuid>') never match again; remembering
            # them only grows the memory file. They stay reported as new.
            store.remember(activity_id, {k for k in keys if not _is_anon_key(k)})
            return {
                **result,
                "new_only": since_last_run,
                "previous_run_known": len(known),
                "reactor_count": len(reactors),
                "comment_count": len(comments),
                "reactors": reactors,
                "comments": comments,
            }

        return await _run(ctx, "get_post_engagers", body)

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Post Analytics",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={TAG, "post"},
    )
    async def get_post_analytics(
        post_urls: list[str],
        ctx: Context,
    ) -> dict[str, Any]:
        """
        Impressions, members reached, reactions, comments, reposts, saves, sends,
        profile views and followers gained for the account's own posts. At most
        10 posts per call; posts without analytics (anyone else's, and page
        posts where LinkedIn shows none) come back available=false.

        Refusals: invalid_post_url (empty list or a bad URL), too_many_posts
        (more than 10; split the list, nothing is cut), pace_budget_spent (wait, see pace_status),
        pace_lock_busy (nothing booked, retry shortly).
        """
        if not post_urls:
            return {"status": "invalid_post_url", "message": "post_urls is empty"}
        # More than 10 is refused, not cut: a silently shortened list reads
        # as "all requested posts" to the caller.
        if len(post_urls) > 10:
            return {
                "status": "too_many_posts",
                "max": 10,
                "requested": len(post_urls),
            }
        ids: list[str] = []
        for url in post_urls:
            activity_id, bad = _activity(url)
            if bad:
                return {**bad, "post_url": url[:300]}
            if activity_id not in ids:
                ids.append(activity_id)
        refusal = _pace("page_read", max(len(ids), 1), tool="get_post_analytics")
        if refusal:
            return refusal

        async def body(ex: Any) -> dict[str, Any]:
            reader = _engagement(ex)
            posts = []
            for index, activity_id in enumerate(ids):
                if index:
                    await asyncio.sleep(random.uniform(3.0, 6.0))
                posts.append(await reader.read_post_summary(activity_id))
            return {
                "count": len(posts),
                "requested": len(post_urls),
                "posts": posts,
            }

        return await _run(ctx, "get_post_analytics", body)

    @mcp.tool(
        timeout=30,
        title="Pace Status",
        annotations={"readOnlyHint": True},
        tags={TAG, "messaging"},
    )
    async def pace_status() -> dict[str, Any]:
        """
        The pacer (Taktgeber): per action kind today's and the last seven days'
        use against its budget, and the daily total of visible actions. Every
        fork tool asks it before touching LinkedIn.
        """
        return outreach.Pacer(outreach.Ledger.default()).summary()

    # -- 2. event invitations -------------------------------------------------

    @mcp.tool(
        timeout=tool_timeout,
        title="Invite To Event",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={TAG, "network", "actions"},
    )
    async def invite_to_event(
        event_id: str,
        usernames: list[str],
        ctx: Context,
        confirm_send: bool = False,
    ) -> dict[str, Any]:
        """
        Invite 1st-degree connections to a LinkedIn event.

        LinkedIn offers event invitations only to the organiser and admins of
        the organising page. The tool first reads the event page: without an
        "Einladen"/"Invite" button it returns status not_organizer and does
        nothing. Budget: event_invite pacer (default 25/day, 150/week; the
        platform ceiling is 1,000/week per organiser).

        The invite dialog has not been measured yet -- no event of an account
        page was available on 2026-09-29 -- so even for an organiser this
        returns status dialog_not_measured instead of clicking blind.

        Statuses: not_organizer (cannot invite, nothing done), dry_run
        (confirm_send=false), dialog_not_measured (nothing clicked). Refusals:
        invalid_event_id (not a 10-25 digit id or event URL), no_recipients
        (usernames empty), invalid_recipient, pace_budget_spent (event_invite
        budget below the requested count, or page_read spent), pace_lock_busy.
        """
        event_id = event_id.strip().split("?")[0].strip("/").rsplit("/", 1)[-1]
        # One rule with the network layer (10-25 digits): a shorter id was
        # accepted here and only refused later inside the browser action.
        if not _NETWORK_EVENT_ID_RE.match(event_id):
            return {
                "status": "invalid_event_id",
                "message": "event_id must be the numeric id or the event URL",
            }
        if not usernames:
            return {"event_id": event_id, "status": "no_recipients"}
        targets: list[str] = []
        for raw in usernames:
            username, bad = _recipient(raw)
            if bad:
                return {"event_id": event_id, **bad}
            if username not in targets:
                targets.append(username)
        state = outreach.Pacer(outreach.Ledger.default()).state("event_invite")
        if state["left"] < len(targets):
            return {
                "status": "pace_budget_spent",
                "pace": state,
                "requested": len(targets),
            }
        refusal = _pace("page_read", tool="invite_to_event")
        if refusal:
            return refusal

        async def body(ex: Any) -> dict[str, Any]:
            caps = await _actions(ex).event_capabilities(event_id)
            base = {
                "event_id": event_id,
                "event": caps,
                "requested": targets,
                "pace": state,
            }
            if not caps["can_invite"]:
                return {
                    **base,
                    "status": "not_organizer",
                    "message": "This account cannot invite to this event; only the organiser or its page admins can.",
                }
            return {
                **base,
                "status": "dialog_not_measured" if confirm_send else "dry_run",
                "message": "Invite button present. The invite dialog is not automated yet; measure it on an event of an administered page first.",
            }

        return await _run(ctx, "invite_to_event", body)

    # -- 3. withdraw old invitations -------------------------------------------

    @mcp.tool(
        timeout=BATCH_TIMEOUT_SECONDS,
        title="Withdraw Invitations",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={TAG, "network", "actions"},
    )
    async def withdraw_invitations(
        ctx: Context,
        older_than_days: Annotated[int, Field(ge=7, le=365)] = 21,
        confirm_withdraw: bool = False,
        usernames: list[str] | None = None,
        max_withdrawals: Annotated[int, Field(ge=1, le=15)] = 10,
    ) -> dict[str, Any]:
        """
        Withdraw pending connection invitations older than older_than_days
        (default 21). Stale open invitations push the account towards the
        >500-pending restriction trigger and lower its acceptance rate.

        Without confirm_withdraw this lists the candidates (age from LinkedIn's
        rounded wording, a lower bound). With confirm_withdraw it withdraws at
        most max_withdrawals, and only those also named in usernames -- the
        list from a dry run, so every withdrawal is one somebody looked at.
        Each is verified by re-reading the sent list; the withdraw pacer caps
        the day (30). Note LinkedIn blocks re-inviting a withdrawn person for
        about three weeks.

        Statuses: dry_run (candidates listed), withdrawn (all picked done),
        partial (some withdrawn, then a stop), stopped (none withdrawn),
        nothing_selected (confirm without matching usernames). results[].status
        is withdrawn, still_pending, not_found (card gone), not_confirmed
        (dialog did not confirm) or unverified (read-back unclear: re-read
        the sent list before retrying), pace_budget_spent or pace_lock_busy.
        Refusals: invalid_recipient, pace_budget_spent (wait, see pace_status),
        pace_lock_busy (nothing booked, retry shortly).
        """
        chosen = set()
        for raw in usernames or []:
            username, bad = _recipient(raw)
            if bad:
                return bad
            chosen.add(username)
        if confirm_withdraw and not chosen:
            # Checked before the page read: without names nothing can be picked.
            return {
                "status": "nothing_selected",
                "message": "confirm_withdraw needs usernames from the dry-run candidates.",
            }
        refusal = _pace("page_read", tool="withdraw_invitations")
        if refusal:
            return refusal

        async def body(ex: Any) -> dict[str, Any]:
            actions = _actions(ex)
            invitations = await actions.sent_invitations_with_age(1000)
            candidates = [
                i
                for i in invitations
                if i.get("age_days") is not None and i["age_days"] >= older_than_days
            ]
            unknown_age = [i["slug"] for i in invitations if i.get("age_days") is None]
            plan = {
                "older_than_days": older_than_days,
                "pending_total": len(invitations),
                "candidates": candidates,
                "unknown_age": unknown_age,
            }
            if not confirm_withdraw:
                return {**plan, "status": "dry_run"}
            picked = [c for c in candidates if c["slug"] in chosen][:max_withdrawals]
            if not picked:
                return {
                    **plan,
                    "status": "nothing_selected",
                    "message": "confirm_withdraw needs usernames from the dry-run candidates.",
                }
            ledger = outreach.Ledger.default()
            pacer = outreach.Pacer(ledger)
            results = []
            for index, inv in enumerate(picked):
                if index:
                    await asyncio.sleep(random.uniform(8.0, 20.0))
                # The attempt row is the booking, written under the pacer lock
                # (withdraw is a ledger kind). A card that is gone (not_found)
                # ends as an uncounted status and gives the unit back.
                attempt = uuid.uuid4().hex
                row = {
                    "attempt": attempt,
                    "kind": "withdraw",
                    "recipient": outreach.recipient_key(inv["slug"]),
                    "status": "attempted",
                    "started_at": datetime.now()
                    .astimezone()
                    .isoformat(timespec="seconds"),
                }
                try:
                    pacer.take("withdraw", tool="withdraw_invitations", row=row)
                except outreach.PaceExceeded as spent:
                    results.append(
                        {
                            "slug": inv["slug"],
                            "status": "pace_budget_spent",
                            "pace": spent.state,
                        }
                    )
                    break
                except TimeoutError as busy:
                    results.append({"slug": inv["slug"], **pace_lock_busy(busy)})
                    break
                # Row before the click, closed afterwards: a withdrawal that may
                # have happened must leave a trace even if the read-back throws.
                try:
                    outcome = await actions.withdraw(inv["name"], inv["slug"])
                except BaseException:
                    ledger.append({"attempt": attempt, "status": "unknown"})
                    raise
                ledger.append({"attempt": attempt, "status": outcome["status"]})
                results.append(outcome)
                if outcome["status"] != "withdrawn":
                    break
            done = sum(1 for r in results if r.get("status") == "withdrawn")
            status = (
                "withdrawn"
                if done == len(picked)
                else ("partial" if done else "stopped")
            )
            return {**plan, "status": status, "withdrawn": done, "results": results}

        return await _run(ctx, "withdraw_invitations", body)

    # -- 4. replies and follow-ups ----------------------------------------------

    @mcp.tool(
        timeout=BATCH_TIMEOUT_SECONDS,
        title="Follow Up List",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={TAG, "messaging"},
    )
    async def follow_up_list(
        ctx: Context,
        follow_up_days: Annotated[
            int, Field(ge=1, le=60)
        ] = outreach.FOLLOW_UP_DAYS_DEFAULT,
        max_threads: Annotated[int, Field(ge=1, le=40)] = 15,
        include_canary: bool = False,
    ) -> dict[str, Any]:
        """
        Who answered which ledger message, and who is due for a follow-up.

        For every message in the outreach ledger (newest first, one per
        recipient, at most max_threads threads read at human pace) the thread
        is read back: replied=true when a block by someone other than the sender
        follows our message; unclear=true (listed separately, never due) when a
        later block's author cannot be determined or several others wrote
        after us (group chat). Recipients without a reply whose last message is at
        least follow_up_days old are due. Local contact notes (set_contact_note)
        are attached.

        complete=false / has_more=true: more ledger threads than max_threads,
        or an entry with status unreadable (thread could not be read; listed,
        never due) -- call again later. Refusals: pace_budget_spent (wait, see pace_status),
        pace_lock_busy (nothing booked, retry shortly).
        """
        ledger = outreach.Ledger.default()
        rows = outreach.sent_messages(ledger, include_canary=include_canary)
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            latest.setdefault(row["recipient"], row)
        todo = list(latest.values())[:max_threads]
        if not todo:
            # Nothing to read: no page is opened, so nothing may be booked.
            return {
                "follow_up_days": follow_up_days,
                "threads_read": 0,
                "recipients_in_ledger": 0,
                "skipped_undated": outreach.undated_messages(ledger),
                "has_more": False,
                "complete": True,
                "replied": [],
                "due": [],
                "unclear": [],
                "entries": [],
            }
        refusal = _pace("page_read", len(todo), tool="follow_up_list")
        if refusal:
            return refusal
        notes = outreach.ContactNotes()

        async def body(ex: Any) -> dict[str, Any]:
            now = datetime.now().astimezone()
            entries = []
            for index, row in enumerate(todo):
                if index:
                    await asyncio.sleep(random.uniform(3.0, 6.0))
                lookup = (
                    {"thread_id": row["thread"]}
                    if row.get("thread")
                    else {"linkedin_username": row["recipient"]}
                )
                try:
                    conv = await ex.get_conversation(**lookup)
                    sections = conv.get("sections") or {}
                    # Only the conversation section: other sections (inbox
                    # previews) list names that are not replies.
                    text = str(
                        sections.get("conversation")
                        or " \n".join(str(v) for v in sections.values())
                    )
                except Exception as exc:  # one unreadable thread must not stop the list
                    entries.append(
                        {
                            "recipient": row["recipient"],
                            "status": "unreadable",
                            "error": str(exc)[:200],
                        }
                    )
                    continue
                # Longest stored anchor first; rows from before text_anchor
                # fall back to text_head.
                state = (
                    outreach.reply_after(
                        text,
                        row.get("text_head") or row["text_anchor"],
                        anchor=row.get("text_anchor"),
                    )
                    if row.get("text_head") or row.get("text_anchor")
                    else {"found": False, "replied": None}
                )
                if not state.get("found"):
                    last = outreach.last_block_sender(text)
                    state = {"found": False, "replied": None, "last_sender": last}
                sent_at = outreach.row_time(row)
                assert sent_at is not None  # sent_messages drops undated rows
                age = (now - sent_at).days
                entries.append(
                    {
                        "recipient": row["recipient"],
                        "campaign": row.get("campaign"),
                        "sent_at": sent_at.isoformat(timespec="minutes"),
                        "age_days": age,
                        **state,
                        "due": state.get("replied") is False and age >= follow_up_days,
                        "notes": notes.get(row["recipient"]),
                    }
                )
            return {
                "follow_up_days": follow_up_days,
                "threads_read": len(entries),
                "recipients_in_ledger": len(latest),
                "skipped_undated": outreach.undated_messages(ledger),
                # max_threads cut the list: "due" is then only the due among
                # the newest threads, not everyone due. Unreadable threads
                # leave the answer incomplete as well.
                "has_more": len(latest) > len(todo),
                "complete": len(latest) == len(todo)
                and not any(e.get("status") == "unreadable" for e in entries),
                "replied": [e for e in entries if e.get("replied")],
                "due": [e for e in entries if e.get("due")],
                # Author of a later block not determinable, or a group chat:
                # neither replied nor due -- check by hand before following up.
                "unclear": [e for e in entries if e.get("unclear")],
                "entries": entries,
            }

        return await _run(ctx, "follow_up_list", body)

    @mcp.tool(
        timeout=30,
        title="Set Contact Note",
        annotations={"readOnlyHint": False, "openWorldHint": False},
        tags={TAG, "messaging"},
    )
    async def set_contact_note(
        linkedin_username: str,
        tags: list[str] | None = None,
        note: str | None = None,
        replace: bool = False,
    ) -> dict[str, Any]:
        """
        Local keywords and a short note per contact (~/.linkedin-mcp/mivia-contact-notes.json),
        shown by follow_up_list. Never leaves the machine. Professional context
        only -- no private-sphere details.

        Statuses: saved (entry holds the stored note). Refusals:
        invalid_recipient, invalid_note / invalid_tag (control or invisible
        characters), note_too_long / tag_too_long (max holds the limit),
        notes_unreadable (notes file corrupt: nothing written, repair it).
        """
        username, bad = _recipient(linkedin_username)
        if bad:
            return bad
        # UTF-16 units like every other text limit in the fork; bidi and other
        # Cf format characters hide or reorder what follow_up_list shows.
        if note is not None and _utf16_len(note) > NOTE_MAX:
            return {"recipient": username, "status": "note_too_long", "max": NOTE_MAX}
        if note is not None and any(
            (ord(c) < 32 and c != "\n") or ord(c) == 127 or _hidden_format_char(c)
            for c in note
        ):
            return {
                "recipient": username,
                "status": "invalid_note",
                "detail": "no control characters except LF",
            }
        if tags is not None and any(_utf16_len(t) > TAG_MAX for t in tags):
            return {"recipient": username, "status": "tag_too_long", "max": TAG_MAX}
        if tags is not None and any(
            any(ord(c) < 32 or ord(c) == 127 or _hidden_format_char(c) for c in t)
            for t in tags
        ):
            return {
                "recipient": username,
                "status": "invalid_tag",
                "detail": "no control or invisible characters",
            }
        try:
            entry = outreach.ContactNotes().set(
                username, tags=tags, note=note, replace=replace
            )
        except (ValueError, OSError) as broken:
            # A corrupt notes file is reported, never overwritten with {}.
            return {
                "recipient": username,
                "status": "notes_unreadable",
                "detail": str(broken)[:200],
            }
        return {"recipient": username, "status": "saved", "entry": entry}

    # -- 5. profile viewers ----------------------------------------------------

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Profile Viewers",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={TAG, "network"},
    )
    async def get_profile_viewers(
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """
        Who viewed the account's profile (last 90 days, newest first; full list
        with Premium / Sales Navigator). Each viewer: name, slug, profile_url,
        degree, headline and detail lines ("Vor 1 Tag angesehen", shared
        connections, rendered action).

        Refusals: pace_budget_spent (wait, see pace_status),
        pace_lock_busy (nothing booked, retry shortly).
        """
        refusal = _pace("page_read", tool="get_profile_viewers")
        if refusal:
            return refusal
        return await _run(
            ctx, "get_profile_viewers", lambda ex: _actions(ex).profile_viewers(limit)
        )

    # -- 6. comments -------------------------------------------------------------

    @mcp.tool(
        timeout=tool_timeout,
        title="Comment On Post",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={TAG, "post", "actions"},
    )
    async def comment_on_post(
        post_url: str,
        text: str,
        ctx: Context,
        confirm: bool = False,
        reply_to: str | None = None,
    ) -> dict[str, Any]:
        """
        Comment on a post. Dry run by default: the comment editor is filled,
        compared with the text and cleared again; nothing is posted. With
        confirm=true one comment is posted and read back. Never use in a loop:
        each comment needs its own approval, and the same text is refused for a
        second post (repeated text is a restriction trigger). Budget: comment
        pacer (8/day).

        reply_to (a comment id) is not implemented yet and is refused.

        Result status: posted (read back), unverified (posted=true but not
        read back: check the post, never comment again), no_editor /
        editor_mismatch (posted=false, nothing posted: safe to retry), dry_run.
        Refusals (posted=false): not_supported (reply_to), invalid_text,
        invalid_post_url, duplicate_text (same text attempted before),
        already_commented (an earlier comment on this post may be live),
        pace_budget_spent (comment budget spent: wait), pace_lock_busy.
        """
        if reply_to:
            return {
                "status": "not_supported",
                "posted": False,
                "message": "Replies to a comment are not implemented yet.",
            }
        # Same checks as a message: LinkedIn counts UTF-16 units (an emoji is
        # two), and an invisible control (zero-width, bidi override) makes the
        # read-back compare a text the reader never sees.
        if (
            not text.strip()
            or _utf16_len(text) > 1250
            or any(
                (ord(c) < 32 and c != "\n") or ord(c) == 127 or is_invisible_control(c)
                for c in text
            )
        ):
            return {
                "status": "invalid_text",
                "posted": False,
                "message": "1-1250 UTF-16 units, LF allowed, no other control or invisible characters.",
            }
        activity_id, bad = _activity(post_url)
        if bad:
            return {**bad, "posted": False}
        ledger = outreach.Ledger.default()
        sha = outreach.text_sha(text)

        # Any attempt that may have posted blocks the same text again: an
        # attempt row without outcome counts as posted (repeated text is a
        # restriction trigger).
        def repeat() -> dict[str, Any] | None:
            return next(
                (
                    r
                    for r in ledger.latest_by_attempt().values()
                    if r.get("kind") == "comment"
                    and sha in outreach.row_text_shas(r)
                    and r.get("status")
                    in {"attempted", "unknown", "posted", "unverified"}
                ),
                None,
            )

        # A second comment on the same post (other text) is refused as well
        # while an earlier one may have posted: two comments from one account
        # under one post read as spam and cannot be taken back here.
        # A comment removed afterwards by a verified delete_own_comment no
        # longer blocks the post: otherwise one deleted comment locked it
        # forever. The release is bound to the deleted comment itself, never
        # to the post: delete_own_comment notes deleted_by on exactly the
        # comment row whose text it removed (matched by the text it read
        # back). A row counts as gone only when that note names a delete
        # attempt on this post whose latest status is verified and which
        # started after the comment (real aware datetimes, not text order).
        # Every other open row -- in particular an unknown attempt the delete
        # could not be tied to -- keeps blocking. The same text stays refused
        # through repeat() regardless.
        def same_post() -> dict[str, Any] | None:
            latest = ledger.latest_by_attempt()

            def deleted(r: dict[str, Any]) -> bool:
                d = latest.get(str(r.get("deleted_by") or ""))
                if not d or d.get("kind") != "comment_delete":
                    return False
                if d.get("activity") != activity_id or d.get("status") != "verified":
                    return False
                posted_at, deleted_at = outreach.row_time(r), outreach.row_time(d)
                return (
                    posted_at is not None
                    and deleted_at is not None
                    and deleted_at >= posted_at
                )

            return next(
                (
                    r
                    for r in latest.values()
                    if r.get("kind") == "comment"
                    and r.get("activity") == activity_id
                    and r.get("status")
                    in {"attempted", "unknown", "posted", "unverified"}
                    and not deleted(r)
                ),
                None,
            )

        previous = repeat()
        if previous:
            return {"status": "duplicate_text", "posted": False, "previous": previous}
        earlier = same_post()
        if earlier and confirm:
            return {
                "status": "already_commented",
                "posted": False,
                "previous": earlier,
            }
        if confirm:
            # Peek only: the attempt row below is the booking, written under
            # the pacer lock together with the repeated duplicate check, so
            # two parallel calls cannot both post the same text.
            refusal = _peek("comment")
            if refusal:
                return refusal
        else:
            refusal = _pace("page_read", tool="comment_on_post")
            if refusal:
                return refusal

        async def body(ex: Any) -> dict[str, Any]:
            if not confirm:
                result = await _actions(ex).comment(activity_id, text, False)
                return {"activity_id": activity_id, **result}
            attempt = uuid.uuid4().hex
            refused = _book_attempt(
                ledger,
                "comment",
                {
                    "attempt": attempt,
                    "kind": "comment",
                    "activity": activity_id,
                    "text_sha": sha,
                    "status": "attempted",
                    "started_at": datetime.now()
                    .astimezone()
                    .isoformat(timespec="seconds"),
                },
                tool="comment_on_post",
                duplicate=lambda: repeat() or same_post(),
            )
            if refused:
                if refused["status"] == "duplicate":
                    refused = {**refused, "status": "duplicate_text"}
                return {"posted": False, **refused}
            actions = _actions(ex)
            # R7: comment() resets comment_submitted once the editor is found
            # and sets it directly before the submit click. Not reset here: an
            # exception before comment() set the marker at all (page load), or
            # a reader without it, stays fail-closed (unknown).
            try:
                result = await actions.comment(activity_id, text, True)
            except BaseException:
                clicked = bool(getattr(actions, "comment_submitted", True))
                ledger.append(
                    {
                        "attempt": attempt,
                        "status": "unknown" if clicked else "not_posted",
                        "detail": "exception after the submit click"
                        if clicked
                        else "exception before the submit click",
                    }
                )
                raise
            # Not posted (no editor, mismatch): release the text for a retry.
            # posted must be literally True; an unknown status with posted=True
            # is at most unverified, never a confirmed post.
            if result.get("posted") is True:
                status = (
                    result.get("status")
                    if result.get("status") in {"posted", "unverified"}
                    else "unverified"
                )
                result = {**result, "status": status}
            else:
                status = "not_posted"
                result = {**result, "posted": False}
            ledger.append({"attempt": attempt, "status": status})
            return {"activity_id": activity_id, **result}

        return await _run(ctx, "comment_on_post", body)

    @mcp.tool(
        timeout=tool_timeout,
        title="Repost Post",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={TAG, "post", "actions"},
    )
    async def repost_post(
        post_url: str,
        ctx: Context,
        confirm: bool = False,
        undo: bool = False,
        thoughts: str | None = None,
    ) -> dict[str, Any]:
        """
        Repost a post instantly to the signed-in member's own feed (never as a
        company page), or take that repost back with undo=true. Dry run by
        default: the repost menu is opened, its entries reported and closed
        again; nothing is shared. With confirm=true the menu entry is clicked
        and the post reloaded to read the state back. Each repost needs its own
        approval; never use in a loop. Budget: repost / repost_undo pacer
        (3/day each). After undo_unverified the post stays blocked for a new
        repost (the undo wording is not measured): check it by hand.

        thoughts (repost with own text) is not implemented and is refused.

        Result status: reposted / undone (done=true, read back), unverified /
        undo_unverified (done=true but not read back: look at the post, never
        repeat), dry_run (menu lists the entries, would_click the one a confirm
        would click). Nothing clicked (done=false, safe to retry or check by
        hand): no_repost_button, repost_button_ambiguous, menu_missing (button
        opened no recognised menu; new_lines shows what appeared), menu_unclear (no
        single matching entry), already_reposted (the page or the ledger shows
        an earlier repost), not_reposted (undo=true but nothing to take back).
        Refusals (done=false): not_supported (thoughts), invalid_post_url,
        repost_pending (an earlier undo on this post is still unresolved),
        pace_budget_spent (wait), pace_lock_busy.
        """
        if thoughts is not None:
            return {
                "status": "not_supported",
                "done": False,
                "message": "Repost with own thoughts is not implemented; use create_post.",
            }
        activity_id, bad = _activity(post_url)
        if bad:
            return {**bad, "done": False}
        ledger = outreach.Ledger.default()
        kind = "repost_undo" if undo else "repost"

        def blocker() -> dict[str, Any] | None:
            """An earlier repost that may be live, or an unresolved undo."""
            latest = ledger.latest_by_attempt()
            rows = [r for r in latest.values() if r.get("activity") == activity_id]
            if undo:
                return next(
                    (
                        r
                        for r in rows
                        if r.get("kind") == "repost_undo"
                        and r.get("status") in {"attempted", "unknown"}
                    ),
                    None,
                )
            # An undo releases a repost only when its attempt was written
            # AFTER the repost's (ledger order; times are whole seconds and
            # tie) and is not dated earlier. Undated rows release nothing.
            undone = [
                (i, outreach.row_time(r))
                for i, r in enumerate(rows)
                if r.get("kind") == "repost_undo" and r.get("status") == "undone"
            ]
            for i, r in enumerate(rows):
                if r.get("kind") != "repost" or r.get("status") not in _REPOST_OPEN:
                    continue
                posted_at = outreach.row_time(r)
                if posted_at is None or not any(
                    j > i and t is not None and t >= posted_at for j, t in undone
                ):
                    return r
            return None

        earlier = blocker()
        if earlier and confirm:
            return {
                "status": "repost_pending" if undo else "already_reposted",
                "done": False,
                "previous": earlier,
            }
        if confirm:
            refusal = _peek(kind)
        else:
            refusal = _pace("page_read", tool="repost_post")
        if refusal:
            return {**refusal, "done": False}

        async def body(ex: Any) -> dict[str, Any]:
            reposter = MiviaReposter(ex.mivia_session, ex.mivia_navigator)
            if not confirm:
                result = await reposter.repost(activity_id, undo=undo, confirm=False)
                return {"activity_id": activity_id, **result}
            attempt = uuid.uuid4().hex
            refused = _book_attempt(
                ledger,
                kind,
                {
                    "attempt": attempt,
                    "kind": kind,
                    "activity": activity_id,
                    "status": "attempted",
                    "started_at": datetime.now()
                    .astimezone()
                    .isoformat(timespec="seconds"),
                },
                tool="repost_post",
                duplicate=blocker,
            )
            if refused:
                if refused["status"] == "duplicate":
                    refused = {
                        **refused,
                        "status": "repost_pending" if undo else "already_reposted",
                    }
                return {"done": False, **refused}
            try:
                result = await reposter.repost(activity_id, undo=undo, confirm=True)
            except BaseException:
                clicked = bool(getattr(reposter, "repost_clicked", True))
                ledger.append(
                    {
                        "attempt": attempt,
                        "status": "unknown" if clicked else "not_done",
                        "detail": "exception after the menu click"
                        if clicked
                        else "exception before the menu click",
                    }
                )
                raise
            # done must be literally True. menu_missing is not_done: measured
            # 2026-10-02, the button only opens the menu (nothing is shared).
            if result.get("done") is True:
                done_ok = {"undone", "undo_unverified"} if undo else _REPOST_DONE
                status = (
                    result.get("status")
                    if result.get("status") in done_ok
                    else ("undo_unverified" if undo else "unverified")
                )
                result = {**result, "status": status}
            else:
                status = "not_done"
                result = {**result, "done": False}
            ledger.append({"attempt": attempt, "status": status})
            return {"activity_id": activity_id, **result}

        return await _run(ctx, "repost_post", body)

    # -- 7. job watch --------------------------------------------------------------

    @mcp.tool(
        timeout=BATCH_TIMEOUT_SECONDS,
        title="Job Watch",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={TAG, "job"},
    )
    async def job_watch(
        ctx: Context,
        companies: list[str] | None = None,
        searches: list[dict[str, str]] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """
        Weekly job watch: saved searches (default Metallograf, Werkstoffprüfer,
        Wärmebehandlung in Germany, Austria and Switzerland) plus the jobs of a
        company list, past week only, one results page each. Returns only job
        ids not seen in an earlier run (~/.linkedin-mcp/mivia-job-watch.json).
        Refuses to run again within 6 days unless force. Reading only -- there
        is no apply automation.

        Args:
            companies: Company names to search jobs for (keywords = name).
            searches: Override the saved searches: [{keywords, location}].

        Statuses: ran (every search answered; complete=true), partial (some
        searches failed: complete=false, failed_searches counts them, last_run
        is not advanced so the next call retries them), failed (every search
        failed, e.g. session expired; nothing recorded), not_due (last run
        under 6 days ago: pass force=true only on purpose). searches[].status
        is ok or failed (with error). Refusals: pace_budget_spent (wait, see pace_status),
        pace_lock_busy (nothing booked, retry shortly).
        """
        store = JobWatchStore()
        last = store.last_run()
        if (
            last
            and not force
            and datetime.now().astimezone() - last < timedelta(days=6)
        ):
            return {"status": "not_due", "last_run": last.isoformat(timespec="minutes")}
        plan = [dict(s) for s in (searches or DEFAULT_JOB_SEARCHES)]
        plan += [
            {"keywords": c, "location": None, "company": c} for c in companies or []
        ]
        refusal = _pace("search", len(plan), tool="job_watch")
        if refusal:
            return refusal

        async def body(ex: Any) -> dict[str, Any]:
            seen = store.seen()
            results = []
            new_ids: set[str] = set()
            for index, search in enumerate(plan):
                if index:
                    await asyncio.sleep(random.uniform(4.0, 9.0))
                try:
                    res = await ex.search_jobs(
                        search["keywords"],
                        search.get("location"),
                        max_pages=1,
                        date_posted="past_week",
                    )
                except Exception as exc:
                    results.append(
                        {**search, "status": "failed", "error": str(exc)[:200]}
                    )
                    continue
                # job_ids is upstream's scoped result list; the references also
                # carry LinkedIn's unrelated recommendations (measured
                # 2026-09-29: "Metallograf" -> Kfz-Mechatroniker, Drohnenpilot),
                # so references only supply titles, never ids.
                ids = list(dict.fromkeys(res.get("job_ids") or []))
                titles = job_titles(res.get("references"))
                fresh = [i for i in ids if i not in seen and i not in new_ids]
                new_ids.update(fresh)
                stem = keyword_stem(search.get("match") or search["keywords"])
                results.append(
                    {
                        **search,
                        "status": "ok",
                        "found": len(ids),
                        "new_jobs": [
                            {
                                "job_id": i,
                                "url": f"https://www.linkedin.com/jobs/view/{i}/",
                                "title": titles.get(i),
                                "title_matches": bool(
                                    titles.get(i) and stem in titles[i].lower()
                                )
                                if not search.get("company")
                                else None,
                            }
                            for i in fresh
                        ],
                        "url": res.get("url"),
                    }
                )
            # A run where every search failed (session expired, rate limit) is
            # not a run: keeping last_run would silence the next six days.
            # Only a run where every search answered counts as a run: a
            # partly failed one would otherwise silence the failed searches
            # for six days behind a "ran".
            failed = [r for r in results if r["status"] != "ok"]
            ok = len(failed) < len(results)
            store.record(new_ids, ran=not failed)
            if not ok:
                return {"status": "failed", "new_total": 0, "searches": results}
            return {
                "status": "partial" if failed else "ran",
                "complete": not failed,
                "failed_searches": len(failed),
                "new_total": len(new_ids),
                "searches": results,
            }

        return await _run(ctx, "job_watch", body)

    # -- 9. groups -----------------------------------------------------------------

    @mcp.tool(
        timeout=tool_timeout,
        title="List Groups",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={TAG, "network"},
    )
    async def list_groups(ctx: Context) -> dict[str, Any]:
        """The account's LinkedIn groups with id, name and member count.

        Refusals: pace_budget_spent (wait, see pace_status), pace_lock_busy
        (nothing booked, retry shortly).
        """
        refusal = _pace("page_read", tool="list_groups")
        if refusal:
            return refusal
        return await _run(ctx, "list_groups", lambda ex: _actions(ex).list_groups())

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Group Members",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={TAG, "network"},
    )
    async def get_group_members(
        group: str,
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=300)] = 100,
    ) -> dict[str, Any]:
        """
        Members of a group the account belongs to (name, slug, degree,
        headline), in LinkedIn's rendered order. A professional-context list:
        company first, then the cadence tracker with its suppression check.

        Refusals: pace_budget_spent (wait, see pace_status),
        pace_lock_busy (nothing booked, retry shortly).
        """
        group_id = parse_group_id(group)
        refusal = _pace("page_read", tool="get_group_members")
        if refusal:
            return refusal
        return await _run(
            ctx,
            "get_group_members",
            lambda ex: _actions(ex).group_members(group_id, limit),
        )

    # -- events and page followers -------------------------------------------------

    @mcp.tool(
        timeout=BATCH_TIMEOUT_SECONDS,
        title="Find Events",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={TAG, "search"},
    )
    async def find_events(
        ctx: Context,
        keywords: list[str] | None = None,
        organisers: list[str] | None = None,
        include_past: bool = False,
    ) -> dict[str, Any]:
        """
        Find LinkedIn events by keyword (event search, upcoming only) and by
        organiser page (company slug; its "Events" tab, upcoming and with
        include_past also past ones). Each event: event_id, title, date_text,
        place, organiser, description, attendees, url, found_by, past.
        Duplicates across keywords/organisers are merged. At most 12 queries.

        Refusals: pace_budget_spent (wait, see pace_status),
        pace_lock_busy (nothing booked, retry shortly).
        """
        kws = [k for k in (keywords or []) if k.strip()][:12]
        orgs = [o for o in (organisers or []) if o.strip()][: max(0, 12 - len(kws))]
        refusal = _pace("search", max(1, len(kws) + len(orgs)), tool="find_events")
        if refusal:
            return refusal

        async def body(ex: Any) -> dict[str, Any]:
            finder = MiviaEventFinder(ex.mivia_session, ex.mivia_navigator)
            found: dict[str, dict[str, Any]] = {}
            errors = []
            for index, (kind, value) in enumerate(
                [("k", k) for k in kws] + [("o", o) for o in orgs]
            ):
                if index:
                    await asyncio.sleep(random.uniform(3.0, 6.0))
                try:
                    events = (
                        (await finder.by_keyword(value))
                        if kind == "k"
                        else (
                            await finder.by_organiser(value, include_past=include_past)
                        )
                    )
                except Exception as exc:  # one query must not stop the rest
                    errors.append({"query": value, "error": str(exc)[:200]})
                    continue
                for ev in events:
                    prev = found.get(ev["event_id"])
                    if prev:
                        prev["found_by"] = sorted(
                            set(prev["found_by"]) | {ev["found_by"]}
                        )
                    else:
                        found[ev["event_id"]] = {**ev, "found_by": [ev["found_by"]]}
            return {
                "count": len(found),
                "events": list(found.values()),
                "errors": errors,
            }

        return await _run(ctx, "find_events", body)

    @mcp.tool(
        timeout=tool_timeout,
        title="Search Events",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={TAG, "search"},
    )
    async def search_events(
        ctx: Context, keywords: str, limit: int = 10
    ) -> dict[str, Any]:
        """
        One LinkedIn event search (/search/results/events/?keywords=...).
        Per hit only event master data: event_id, url, title, date_text,
        organiser, attendees (count), attendees_text, past. No person data.
        limit 1-25. has_more=true when more hits exist than were returned
        (cut at limit or further result pages). Reads only; nothing is clicked.

        Refusals: pace_budget_spent (wait, see pace_status),
        pace_lock_busy (nothing booked, retry shortly).
        """
        kw = (keywords or "").strip()
        if not kw:
            return {"error": "keywords required"}
        limit = max(1, min(int(limit), 25))
        refusal = _pace("search", 1, tool="search_events")
        if refusal:
            return refusal

        async def body(ex: Any) -> dict[str, Any]:
            finder = MiviaEventFinder(ex.mivia_session, ex.mivia_navigator)
            events = await finder.by_keyword(kw, max_pages=1 if limit <= 10 else 3)
            out = [event_summary(e) for e in events][:limit]
            truncated = len(events) > limit
            return {
                "keywords": kw,
                "count": len(out),
                "events": out,
                "truncated": truncated,
                "has_more": truncated or bool(getattr(finder, "last_has_more", False)),
            }

        return await _run(ctx, "search_events", body)

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Company Events",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={TAG, "company"},
    )
    async def get_company_events(
        ctx: Context, company_slug: str, include_past: bool = False
    ) -> dict[str, Any]:
        """
        Events tab of one organiser page (/company/<slug>/events/). Same
        fields as search_events. Upcoming only unless include_past.

        Refusals: pace_budget_spent (wait, see pace_status),
        pace_lock_busy (nothing booked, retry shortly).
        """
        slug = (company_slug or "").strip().strip("/")
        if not slug or "/" in slug:
            return {"error": "company_slug must be a bare slug"}
        refusal = _pace("search", 1, tool="get_company_events")
        if refusal:
            return refusal

        async def body(ex: Any) -> dict[str, Any]:
            finder = MiviaEventFinder(ex.mivia_session, ex.mivia_navigator)
            events = await finder.by_organiser(slug, include_past=include_past)
            out = [event_summary(e) for e in events]
            return {"company_slug": slug, "count": len(out), "events": out}

        return await _run(ctx, "get_company_events", body)

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Page Followers",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={TAG, "network"},
    )
    async def get_page_followers(
        page_id: str,
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=300)] = 50,
    ) -> dict[str, Any]:
        """
        Newest followers of a company page the account administers (numeric
        page id, e.g. MiViA 81728804), with name, degree, headline and the
        month followed. Needs page admin rights; otherwise available=false.

        Refusals: pace_budget_spent (wait, see pace_status),
        pace_lock_busy (nothing booked, retry shortly).
        """
        refusal = _pace("page_read", tool="get_page_followers")
        if refusal:
            return refusal
        return await _run(
            ctx,
            "get_page_followers",
            lambda ex: MiviaEventFinder(
                ex.mivia_session, ex.mivia_navigator
            ).page_followers(page_id.strip(), known=set(), limit=limit),
        )


# set_contact_note: a note is a keyword aid, not a dossier.
NOTE_MAX = 1000
TAG_MAX = 60

DEFAULT_JOB_SEARCHES = [
    {"keywords": keywords, "location": location}
    for keywords in ("Metallograf", "Werkstoffprüfer", "Wärmebehandlung")
    for location in ("Deutschland", "Österreich", "Schweiz")
]


_JOB_VIEW_RE = re.compile(r"/jobs/view/(\d+)")


def job_titles(references: Any) -> dict[str, str]:
    """job id -> title from upstream's reference lists (any section)."""
    out: dict[str, str] = {}
    for refs in (references or {}).values():
        for ref in refs or []:
            match = _JOB_VIEW_RE.search(str(ref.get("url", "")))
            if ref.get("kind") == "job" and match and ref.get("text"):
                out.setdefault(match.group(1), ref["text"].strip())
    return out


def keyword_stem(keywords: str) -> str:
    """Lower-case stem that survives German inflection: 'Metallograf' -> 'metallogra'."""
    word = keywords.strip().split()[0].lower() if keywords.strip() else ""
    for suffix in ("ung", "er", "in", "f", "ph"):
        if len(word) > 7 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def job_watch_path() -> Path:
    configured = os.environ.get("MIVIA_LINKEDIN_JOB_WATCH")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".linkedin-mcp" / "mivia-job-watch.json"


class JobWatchStore:
    def __init__(self, path: Path | None = None):
        self.path = path or job_watch_path()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def seen(self) -> set[str]:
        return set(self._load().get("seen", []))

    def last_run(self) -> datetime | None:
        value = self._load().get("last_run")
        return datetime.fromisoformat(value) if value else None

    def record(self, new_ids: set[str], *, ran: bool = True) -> None:
        data = self._load()
        data["seen"] = sorted(set(data.get("seen", [])) | new_ids)
        if ran:
            data["last_run"] = datetime.now().astimezone().isoformat(timespec="seconds")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)


def _is_anon_key(key: str) -> bool:
    """engager_key for an engager without id and name ('kind:anon=<uuid>:x')."""
    return ":anon=" in key
