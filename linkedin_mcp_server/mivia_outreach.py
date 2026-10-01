"""MiViA fork: outreach ledger -- idempotency, canary gate and pacing caps.

One append-only JSONL file per machine (the LinkedIn session is per machine
too). Every send attempt is recorded *before* its outcome is known and closed
afterwards, so a crash between the two leaves an ``attempted`` row that blocks a
second send to the same person: an unknown outcome is treated as sent. A
duplicate message costs more than a missing one.

Caps (Frederik, 2026-09-29, raised the same day): messages default 30, hard
max 40/day, 200/week; invites default 20, hard max 25/day, 100 per rolling 7
days. The hard maxima cannot be raised by a caller.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

LEDGER_ENV = "MIVIA_LINKEDIN_LEDGER"

MESSAGES_PER_DAY_DEFAULT = 30
MESSAGES_PER_DAY_MAX = 40
INVITES_PER_DAY_DEFAULT = 20
INVITES_PER_DAY_MAX = 25
INVITES_PER_WEEK_MAX = 100
MESSAGES_PER_WEEK_MAX = 200

DEFAULT_CANARY = "frederikstadler"

# Rows in these states block another send of the same text to the same person.
_BLOCKING = {"attempted", "sent", "verified", "unverified", "unknown"}
# Rows in these states count against the caps (anything that may have left).
# "posted" is a comment's success state; not_sent/not_posted never count.
_COUNTED = _BLOCKING | {"posted"}
# create_post (2026-10-01) closes with posted_verified; a withdrawal with
# withdrawn or still_pending (the click happened). not_found never counts.
_COUNTED = _COUNTED | {"posted_verified", "withdrawn", "still_pending"}


def ledger_path() -> Path:
    configured = os.environ.get(LEDGER_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".linkedin-mcp" / "mivia-outreach-ledger.jsonl"


def canonical_text(text: str) -> str:
    """Whitespace- and newline-insensitive form used for hashing and read-back."""
    text = unicodedata.normalize("NFC", text).replace(" ", " ")
    return re.sub(r"\s+", " ", text).strip()


def text_sha(text: str) -> str:
    return hashlib.sha256(canonical_text(text).encode("utf-8")).hexdigest()


def recipient_key(username: str) -> str:
    return unicodedata.normalize("NFC", username).strip().strip("/").lower()


class LedgerCorrupt(ValueError):
    """The outreach ledger has an unreadable row before its last line."""

    def __init__(self, path: Path, line: int):
        super().__init__(f"outreach ledger {path} is unreadable at line {line}")
        self.path = path
        self.line = line


def _ends_torn(path: Path) -> bool:
    """True when *path* is non-empty and its last byte is not a newline."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                return False
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) != b"\n"
    except FileNotFoundError:
        return False


@dataclass
class Ledger:
    path: Path

    @classmethod
    def default(cls) -> "Ledger":
        return cls(ledger_path())

    def rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows = []
        with self.path.open(encoding="utf-8") as handle:
            lines = handle.readlines()
        for number, raw in enumerate(lines, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as bad:
                # A torn final line without its newline is a write that never
                # finished: append() writes the row before the action, so the
                # action behind it did not start. Anything else is corruption,
                # and a pacer that skipped it would under-count -- refuse.
                if number == len(lines) and not raw.endswith("\n"):
                    continue
                # append() seals such a fragment with a newline before the
                # next row, so it can also sit in the middle: a line that
                # opens an object and never closes it is the same torn write.
                # A cut can also fall right after a nested object's "}" (e.g.
                # '{"a": {"b": 1}'): the parser then fails at the very end of
                # the line -- a truncated document, not a corrupt one.
                if line.startswith("{") and (
                    not line.endswith("}") or bad.pos >= len(line)
                ):
                    continue
                raise LedgerCorrupt(self.path, number) from bad
            if not isinstance(rows[-1], dict):
                # Valid JSON but not a row ("null", a list): every reader
                # would die on .get(); it is corruption like any other line.
                raise LedgerCorrupt(self.path, number)
        return rows

    def append(self, row: dict[str, Any]) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), **row}
        # Two MCP instances append to the same file. On Windows "a" mode is a
        # seek-to-end plus write, not an atomic append: without a lock two
        # rows can interleave, or one process seals the other's half-written
        # line as torn. Own lock file, because take() already holds the pacer
        # lock while it appends (the lock is not re-entrant).
        with _file_lock(self.path.with_suffix(".append.lock")):
            torn = _ends_torn(self.path)
            # A crash left a last line without its newline; without the
            # leading newline the new row would be glued onto the fragment.
            payload = ("\n" if torn else "") + json.dumps(row, ensure_ascii=False)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(payload + "\n")  # one write call
                handle.flush()
                os.fsync(handle.fileno())
        return row

    # -- derived state -------------------------------------------------------

    def latest_by_attempt(self) -> dict[str, dict[str, Any]]:
        """Collapse rows to the latest state per attempt id."""
        latest: dict[str, dict[str, Any]] = {}
        for row in self.rows():
            attempt = row.get("attempt")
            if attempt:
                latest[attempt] = {**latest.get(attempt, {}), **row}
        return latest

    def already_contacted(
        self, kind: str, recipient: str, sha: str | None
    ) -> dict[str, Any] | None:
        key = recipient_key(recipient)
        for row in self.latest_by_attempt().values():
            if (
                row.get("kind") == kind
                and row.get("recipient") == key
                and (sha is None or sha in row_text_shas(row))
                and row.get("status") in _BLOCKING
            ):
                return row
        return None

    def canary_verified(self, sha: str, canary: str) -> bool:
        key = recipient_key(canary)
        return any(
            row.get("kind") == "message"
            and row.get("recipient") == key
            and row.get("text_sha") == sha
            and row.get("status") == "verified"
            for row in self.latest_by_attempt().values()
        )

    def count_since(
        self, kind: str, since: datetime, *, exclude: Iterable[str] = ()
    ) -> int:
        skip = {recipient_key(r) for r in exclude}
        total = 0
        for row in self.latest_by_attempt().values():
            if row.get("kind") != kind or row.get("status") not in _COUNTED:
                continue
            if row.get("recipient") in skip:
                continue
            started = counted_time(row)
            if started >= since:
                total += 1
        return total


def _count(row: dict[str, Any]) -> int:
    """A pace row's units; an unreadable count books 1 instead of crashing."""
    try:
        return max(1, int(row.get("count", 1)))
    except (TypeError, ValueError):
        return 1


def row_text_shas(row: dict[str, Any]) -> set[str]:
    """Every text this attempt has stood for: the sent one and each edit.

    An edit (edit_sent_message, edit_own_comment) appends edited_text_sha(s)
    to the original row; the edited text is then also 'already sent' to that
    recipient/post, or the duplicate check would let it go out a second time.
    """
    shas = {row.get("text_sha"), row.get("edited_text_sha")}
    shas.update(row.get("edited_text_shas") or ())
    return {s for s in shas if isinstance(s, str) and s}


def edit_note_shas(row: dict[str, Any], new_sha: str) -> dict[str, Any]:
    """Fields an edit note adds to the original row; keeps earlier edits."""
    earlier = [s for s in row.get("edited_text_shas") or () if s != new_sha]
    if row.get("edited_text_sha") and row["edited_text_sha"] not in earlier:
        earlier.append(row["edited_text_sha"])
    return {"edited_text_sha": new_sha, "edited_text_shas": [*earlier, new_sha]}


def day_start(now: datetime | None = None) -> datetime:
    now = (now or datetime.now()).astimezone()
    # Midnight through the local zone rules, not with *now*'s offset: on a
    # DST change day midnight carried the other offset, and replace() would
    # shift the day window by an hour (25.10.: 00:00-01:00 not counted).
    midnight = now.replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone()


def month_start_pst(now: datetime | None = None) -> datetime:
    """LinkedIn's commercial use limit resets 'at midnight PST on the 1st of each
    calendar month' (help article a524372).

    A fixed UTC-8 on purpose: Windows has no tz database without the `tzdata`
    package, and ZoneInfo would then raise in every pacer call of every tool.
    Daylight saving shifts the reset by one hour at most, which does not matter
    for a monthly limit.
    """
    pst = timezone(timedelta(hours=-8))
    now = (now or datetime.now().astimezone()).astimezone(pst)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def week_window_start(now: datetime | None = None) -> datetime:
    """Rolling seven days, so a Monday reset cannot double a week's volume."""
    now = now or datetime.now().astimezone()
    return now - timedelta(days=7)


def quota(
    ledger: Ledger,
    *,
    messages_per_day: int,
    invites_per_day: int,
    canary: str | None = None,
) -> dict[str, Any]:
    messages_per_day = max(0, min(messages_per_day, MESSAGES_PER_DAY_MAX))
    invites_per_day = max(0, min(invites_per_day, INVITES_PER_DAY_MAX))
    exclude = [canary] if canary else []
    msgs_today = ledger.count_since("message", day_start(), exclude=exclude)
    inv_today = ledger.count_since("invite", day_start())
    inv_week = ledger.count_since("invite", week_window_start())
    return {
        "messages_today": msgs_today,
        "messages_per_day": messages_per_day,
        "messages_left_today": max(0, messages_per_day - msgs_today),
        "invites_today": inv_today,
        "invites_per_day": invites_per_day,
        "invites_last_7_days": inv_week,
        "invites_per_week": INVITES_PER_WEEK_MAX,
        "invites_left_today": max(
            0, min(invites_per_day - inv_today, INVITES_PER_WEEK_MAX - inv_week)
        ),
        "ledger": str(ledger.path),
    }


# --- pacer (Taktgeber) ------------------------------------------------------
#
# One budget per action kind, per day and per rolling week. Every fork tool,
# read or write, asks the pacer before it touches LinkedIn and records what it
# used. Sources: provider consensus (LinkedIn publishes none) -- invites 20-40,
# profile views <=150, likes+comments 50-150, ~150 actions/day in total;
# Frederik's tighter message/invite caps above win where they are lower.
# Messages and invites keep their own attempt rows; the pacer counts those
# directly so nothing is double-booked.

PACE_BUDGETS: dict[str, dict[str, int]] = {
    # Jessica has Sales Navigator (Frederik, 2026-09-30): vendor values for SN are
    # 250-400 profile views and 80-100 search pages a day (Dripify, salesrobot;
    # LinkedIn publishes none). Set at the lower end; the monthly-limit lock
    # (record_limit_hit) stays the backstop if LinkedIn disagrees.
    "profile_view": {"day": 250, "week": 1200},
    "like": {"day": 40, "week": 200},
    "comment": {"day": 8, "week": 30},
    "invite": {"day": INVITES_PER_DAY_MAX, "week": INVITES_PER_WEEK_MAX},
    "event_invite": {"day": 25, "week": 150},
    "withdraw": {"day": 30, "week": 150},
    "message": {"day": MESSAGES_PER_DAY_MAX, "week": MESSAGES_PER_WEEK_MAX},
    "search": {"day": 80, "week": 400},
    "page_read": {"day": 300, "week": 1500},
    # InMail spends a paid credit and reaches a stranger (2026-09-30): low caps.
    "inmail": {"day": 5, "week": 20},
    "message_edit": {"day": 10, "week": 40},
    # Taking back an own post or comment (2026-10-01): rare by design, and a
    # burst of deletions looks like account clean-up to LinkedIn.
    "post_delete": {"day": 3, "week": 10},
    # Publishing an own post (2026-10-01): public and broadcast to the whole
    # network, so the tightest cap of all writes.
    "post": {"day": 3, "week": 10},
    "post_edit": {"day": 5, "week": 20},
    "comment_delete": {"day": 5, "week": 20},
    "comment_edit": {"day": 5, "week": 20},
}
# Everything that is visible to another member counts against one total.
PACE_WRITE_KINDS = {
    "like",
    "comment",
    "invite",
    "event_invite",
    "message",
    "withdraw",
    "inmail",
    "message_edit",
    "post_delete",
    "post_edit",
    "comment_delete",
    "comment_edit",
    "post",
}
PACE_WRITE_TOTAL_PER_DAY = 150
# LinkedIn's own event-invitation ceiling per organiser account and week.
EVENT_INVITES_PLATFORM_PER_WEEK = 1000

# Kinds whose attempt row is the pacer booking (no separate pace row).
_LEDGER_KINDS = {
    "message",
    "invite",
    "inmail",
    "message_edit",
    "comment",
    "post_delete",
    "post_edit",
    "comment_delete",
    "comment_edit",
    "post",
    "withdraw",
}


@contextmanager
def _file_lock(path: Path, timeout: float = 15.0, stale_after: float = 60.0):
    """Exclusive lock by O_EXCL lock file; a lock older than *stale_after* s
    is taken over (its holder crashed).

    The file carries an owner token, and release removes it only while it is
    still ours: a holder whose lock was taken over as stale must not delete
    the new owner's lock on its way out (two holders afterwards). A stale
    takeover moves the file aside first and re-checks the moved file, so a
    waiter cannot delete a lock another one has just created.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    token = f"{os.getpid()}-{time.monotonic_ns()}-{id(path)}".encode()
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, token)
            break
        except (FileExistsError, PermissionError):
            # PermissionError: Windows refuses O_EXCL on a file that is being
            # deleted -- the lock is still taken, wait like for an existing one.
            try:
                if time.time() - path.stat().st_mtime > stale_after:
                    _take_over_stale(path, stale_after)
                    continue
            except FileNotFoundError:
                continue
            except PermissionError:
                # Windows: another process holds or is deleting the stale
                # file. Not ours to fail on -- keep waiting until the deadline.
                pass
            if time.monotonic() > deadline:
                raise TimeoutError(f"pacer lock {path} held too long")
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(fd)
        # Windows (WinError 32): a reader or virus scanner may hold the file
        # for a moment; retry briefly instead of leaving our lock behind.
        for attempt in range(40):
            try:
                if path.read_bytes() == token:
                    path.unlink(missing_ok=True)
                break
            except FileNotFoundError:
                break
            except PermissionError:
                time.sleep(0.05)


def _take_over_stale(path: Path, stale_after: float) -> None:
    """Move a stale lock aside atomically; give back one that turned out fresh."""
    aside = path.with_name(f"{path.name}.stale-{os.getpid()}-{time.monotonic_ns()}")
    os.rename(path, aside)  # FileNotFoundError: another waiter was first
    try:
        if time.time() - aside.stat().st_mtime <= stale_after:
            # Between the caller's stat and the rename a new owner created a
            # fresh lock: put it back unless yet another lock exists by now.
            try:
                os.link(aside, path)
            except FileExistsError:
                pass
    finally:
        aside.unlink(missing_ok=True)


class AlreadyContacted(Exception):
    """Raised by :meth:`Pacer.take` when the duplicate re-check under the lock hits."""

    def __init__(self, previous: dict[str, Any]):
        super().__init__("recipient already contacted")
        self.previous = previous


class PaceExceeded(Exception):
    """Raised by :meth:`Pacer.take` when a budget is spent."""

    def __init__(self, state: dict[str, Any]):
        super().__init__(f"pace budget for {state.get('action')} is spent")
        self.state = state


@dataclass
class Pacer:
    ledger: Ledger

    def _events(self) -> list[tuple[str, datetime, int]]:
        """One ledger read: (action, started, units) for everything that counts."""
        rows = self.ledger.rows()
        latest: dict[str, dict[str, Any]] = {}
        events: list[tuple[str, datetime, int]] = []
        canary = recipient_key(DEFAULT_CANARY)
        for row in rows:
            if row.get("kind") == "pace":
                # A kind whose attempt row is its booking counts that row only.
                # Older ledgers (comment before 14b4d8d) also hold pace rows
                # for it; counting both would book one action twice.
                if row.get("action") in _LEDGER_KINDS:
                    continue
                events.append(
                    (
                        row.get("action"),
                        counted_time(row),
                        _count(row),
                    )
                )
            elif row.get("kind") == "limit_hit":
                events.append(
                    (
                        f"{row.get('action')}_limit_hit",
                        counted_time(row),
                        1,
                    )
                )
            elif row.get("attempt"):
                latest[row["attempt"]] = {**latest.get(row["attempt"], {}), **row}
        for row in latest.values():
            kind = row.get("kind")
            if kind not in _LEDGER_KINDS or row.get("status") not in _COUNTED:
                continue
            if kind == "message" and row.get("recipient") == canary:
                continue
            events.append((kind, counted_time(row), 1))
        return events

    @staticmethod
    def _sum(
        events: list[tuple[str, datetime, int]], actions: set[str], since: datetime
    ) -> int:
        return sum(n for a, at, n in events if a in actions and at >= since)

    def used(self, action: str, since: datetime) -> int:
        return self._sum(self._events(), {action}, since)

    def _writes_today(
        self, events: list[tuple[str, datetime, int]] | None = None
    ) -> int:
        return self._sum(
            events if events is not None else self._events(),
            PACE_WRITE_KINDS,
            day_start(),
        )

    def state(
        self, action: str, events: list[tuple[str, datetime, int]] | None = None
    ) -> dict[str, Any]:
        if action not in PACE_BUDGETS:
            raise ValueError(f"unknown pace action: {action}")
        events = events if events is not None else self._events()
        budget = PACE_BUDGETS[action]
        day = self._sum(events, {action}, day_start())
        week = self._sum(events, {action}, week_window_start())
        left = min(budget["day"] - day, budget["week"] - week)
        if action in PACE_WRITE_KINDS:
            left = min(left, PACE_WRITE_TOTAL_PER_DAY - self._writes_today(events))
        extra: dict[str, Any] = {}
        month = month_start_pst()
        hits = [at for a, at, _ in events if a == f"{action}_limit_hit" and at >= month]
        if hits:
            # LinkedIn itself said "no more this month": every tool stops until
            # the reset, instead of hammering a wall that is logged per account.
            left = 0
            first = min(hits)
            reset = month.replace(
                month=month.month % 12 + 1, year=month.year + (month.month == 12)
            )
            extra = {
                "month_limit_hit": first.isoformat(timespec="seconds"),
                # The measured limit of this account: what was used when it hit.
                "used_this_month_at_hit": sum(
                    n for a, at, n in events if a == action and month <= at <= first
                ),
                "resets_at": reset.isoformat(),
            }
        return {
            "action": action,
            "today": day,
            "per_day": budget["day"],
            "last_7_days": week,
            "per_week": budget["week"],
            "left": max(0, left),
            **extra,
        }

    def record_limit_hit(
        self, action: str = "search", *, tool: str | None = None
    ) -> None:
        """LinkedIn showed its monthly limit notice: remember it until the reset."""
        self.ledger.append({"kind": "limit_hit", "action": action, "tool": tool})

    def peek(self, action: str, count: int = 1) -> dict[str, Any]:
        """Check *count* units without booking; raise :class:`PaceExceeded`.

        For a gate in front of a browser step that may still abort without
        acting (first_degree, open_profile, no credits): nothing is used yet,
        so nothing may be booked. The booking happens in :meth:`take` at the
        moment the action is really attempted.
        """
        state = self.state(action)
        if state["left"] < count:
            raise PaceExceeded({**state, "requested": count})
        return state

    def take(
        self,
        action: str,
        count: int = 1,
        *,
        tool: str | None = None,
        row: dict[str, Any] | None = None,
        duplicate: Callable[[], dict[str, Any] | None] | None = None,
    ) -> dict[str, Any]:
        """Reserve *count* units or raise :class:`PaceExceeded`.

        Messages and invites are recorded by their own attempt rows; for them
        this only checks. Everything else is booked here, before the action,
        because an action that may have happened must count. *duplicate* is
        re-run under the lock before *row* is written; a row it returns raises
        :class:`AlreadyContacted` (two parallel calls, one send).
        """
        # Check and book under one lock: two tool calls in parallel must not
        # both pass the check before either books.
        with _file_lock(self.ledger.path.with_suffix(".lock")):
            if duplicate is not None:
                previous = duplicate()
                if previous:
                    raise AlreadyContacted(previous)
            state = self.state(action)
            if state["left"] < count:
                raise PaceExceeded({**state, "requested": count})
            if row is not None:
                # The attempt row is the booking for ledger kinds; writing it
                # under the same lock closes the gap between check and book.
                self.ledger.append(row)
                state = self.state(action)
            elif action not in _LEDGER_KINDS:
                self.ledger.append(
                    {"kind": "pace", "action": action, "count": count, "tool": tool}
                )
                state = self.state(action)
        return state

    def summary(self) -> dict[str, Any]:
        events = self._events()
        return {
            "actions": {a: self.state(a, events) for a in PACE_BUDGETS},
            "writes_today": self._writes_today(events),
            "writes_per_day": PACE_WRITE_TOTAL_PER_DAY,
            "ledger": str(self.ledger.path),
        }


# --- replies, follow-ups, contact notes -------------------------------------

NOTES_ENV = "MIVIA_LINKEDIN_NOTES"
FOLLOW_UP_DAYS_DEFAULT = 5

_SENDER_RE = re.compile(r"Profil von (.+?) anzeigen|View (.+?)[’']s profile")


def person_name(name: str) -> str:
    """Fold NBSP/whitespace runs and drop a trailing '(she/her)'-style suffix."""
    name = re.sub(r"\s+", " ", name or "").strip()  # \s covers NBSP too
    return re.sub(r"\s*\([^)]*\)$", "", name).strip()


def _find_own_message(
    haystack: str, message: str, anchor: str | None
) -> tuple[int, int]:
    """(start, end) of our message's last occurrence in the raw thread text.

    The longest available anchor wins: a stored text_anchor (first ~200
    canonical characters) is unique where the first line -- usually a
    salutation like "Guten Tag Frau X," -- repeats in a later message and
    would hide every reply in between. Whitespace in the anchor matches any
    whitespace run in the raw text. (-1, -1) when nothing matches.
    """
    head = text_head(message)
    candidates = {a for a in (anchor, text_anchor(message)) if a}
    for candidate in sorted(candidates, key=len, reverse=True):
        if len(candidate) <= len(head):
            continue  # not longer than the head: the head search is the same
        pattern = re.compile(r"\s+".join(re.escape(w) for w in candidate.split(" ")))
        last = None
        for last in pattern.finditer(haystack):
            pass
        if last is not None:
            return last.start(), last.end()
    pos = haystack.rfind(head) if head else -1
    if pos < 0:
        return -1, -1
    return pos, pos + len(head)


def reply_after(
    conversation_text: str, message: str, anchor: str | None = None
) -> dict[str, Any]:
    """Did anyone other than the sender write after *message* in this thread?

    The thread pane renders each message block behind "Profil von <Name>
    anzeigen". The sender is whoever owns the block holding our text; any later
    block by another name is a reply. The inbox list above the thread repeats
    names too, so only text after our message counts.

    Three outcomes, fail-safe for follow-ups: replied True, replied False, and
    replied None with unclear=True when a later block exists but its author
    cannot be told apart from ours (sender unknown) or more than one other
    person wrote after us (group chat). "unclear" is never "no reply": a
    follow-up on it may land on someone who already answered.
    """
    haystack = conversation_text or ""
    start, end = _find_own_message(haystack, message, anchor)
    if start < 0:
        return {"found": False, "replied": None}
    before = list(_SENDER_RE.finditer(haystack[:start]))
    sender = (
        person_name(next((g for g in before[-1].groups() if g), "")) if before else None
    )
    after = haystack[end:]
    later = [
        (m, person_name(next(g for g in m.groups() if g)))
        for m in _SENDER_RE.finditer(after)
    ]
    if later and not sender:
        return {
            "found": True,
            "replied": None,
            "unclear": True,
            "reason": "sender_unknown",
            "sender": None,
            "later_blocks": len(later),
        }
    others = sorted({name for _, name in later if name != sender})
    if len(others) > 1:
        return {
            "found": True,
            "replied": None,
            "unclear": True,
            "reason": "group_chat",
            "sender": sender,
            "others": others,
        }
    for match, name in later:
        if name != sender:
            excerpt = after[match.end() :].strip().split("\n")
            body = [ln.strip() for ln in excerpt[1:6] if ln.strip()]
            return {
                "found": True,
                "replied": True,
                "sender": sender,
                "by": name,
                "excerpt": " ".join(body)[:280] or None,
            }
    return {"found": True, "replied": False, "sender": sender}


def notes_path() -> Path:
    configured = os.environ.get(NOTES_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".linkedin-mcp" / "mivia-contact-notes.json"


class ContactNotes:
    """Local keywords per contact (never sent anywhere): {key: {tags, note, at}}."""

    def __init__(self, path: Path | None = None):
        self.path = path or notes_path()

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"contact notes {self.path} are not a JSON object")
        return data

    def get(self, username: str) -> dict[str, Any] | None:
        return self.load().get(recipient_key(username))

    def set(
        self, username: str, *, tags: list[str] | None, note: str | None, replace: bool
    ) -> dict[str, Any]:
        with _file_lock(self.path.with_suffix(".lock")):
            return self._set_locked(username, tags=tags, note=note, replace=replace)

    def _set_locked(
        self, username: str, *, tags: list[str] | None, note: str | None, replace: bool
    ) -> dict[str, Any]:
        data = self.load()
        key = recipient_key(username)
        entry = {} if replace else dict(data.get(key) or {})
        if tags is not None:
            merged = tags if replace else sorted(set(entry.get("tags", [])) | set(tags))
            entry["tags"] = [t.strip() for t in merged if t.strip()]
        if note is not None:
            entry["note"] = note
        entry["at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        data[key] = entry
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
        return entry


def sent_messages(
    ledger: Ledger, *, include_canary: bool = False
) -> list[dict[str, Any]]:
    """Latest state of every message attempt that may have left, newest first."""
    rows = [
        r
        for r in ledger.latest_by_attempt().values()
        if r.get("kind") == "message"
        # A row without recipient cannot be looked up or grouped per person.
        and isinstance(r.get("recipient"), str)
        and r["recipient"]
        # attempted/unknown may have left too; a follow-up list that hides them
        # hides exactly the sends nobody could confirm.
        and r.get("status")
        in {"sent", "verified", "unverified", "attempted", "unknown"}
    ]
    if not include_canary:
        rows = [r for r in rows if r.get("recipient") != recipient_key(DEFAULT_CANARY)]
    # A row without a readable timestamp cannot be aged or ordered; it is
    # skipped here (and counted by skipped_undated) instead of crashing the list.
    rows = [r for r in rows if row_time(r) is not None]
    rows.sort(key=lambda r: row_time(r), reverse=True)  # type: ignore[arg-type,return-value]
    return rows


def counted_time(row: dict[str, Any]) -> datetime:
    """row_time for budget counting: an undated row counts as now.

    Skipping it would under-count a send that may have left; dating it now
    over-counts for at most one window, which is the safe side for a pacer.
    """
    return row_time(row) or datetime.now().astimezone()


def row_time(row: dict[str, Any]) -> datetime | None:
    """started_at or at as an aware datetime; None when missing or unreadable."""
    for key in ("started_at", "at"):
        value = row.get(key)
        if not isinstance(value, str) or not value:
            continue
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed
    return None


def undated_messages(ledger: Ledger) -> int:
    """Message attempts sent_messages() had to skip for want of a timestamp."""
    return sum(
        1
        for r in ledger.latest_by_attempt().values()
        if r.get("kind") == "message" and row_time(r) is None
    )


def text_head(message: str) -> str:
    """First line, at most 80 characters: enough to find the message in a thread."""
    return next((ln for ln in message.split("\n") if ln.strip()), message).strip()[:80]


TEXT_ANCHOR_CHARS = 200


def text_anchor(message: str) -> str:
    """First TEXT_ANCHOR_CHARS of the canonical text: a longer, rarely repeated
    search key than text_head, stored in new ledger rows as text_anchor."""
    return canonical_text(message or "")[:TEXT_ANCHOR_CHARS].strip()


def last_block_sender(conversation_text: str) -> str | None:
    """Sender of the newest message block in the thread pane."""
    matches = list(_SENDER_RE.finditer(conversation_text or ""))
    if not matches:
        return None
    return person_name(next(g for g in matches[-1].groups() if g))


def delivered_in_conversation(message: str, conversation: dict[str, Any]) -> bool:
    """True when the read-back conversation text contains the whole message."""
    sections = conversation.get("sections") or {}
    haystack = canonical_text(" ".join(str(v) for v in sections.values()))
    return canonical_text(message) in haystack


def pace_report() -> dict[str, Any]:
    """Read-only pacer state for callers outside the fork (MiViA HQ).

    HQ plans its catalogue and harvest share from this instead of a copied
    number: the caps, today's and the week's use, and what is left per action.
    Reading it books nothing.
    """
    return {
        "schema": "mivia-pace-report.v1",
        "budgets": PACE_BUDGETS,
        "write_kinds": sorted(PACE_WRITE_KINDS),
        "write_total_per_day": PACE_WRITE_TOTAL_PER_DAY,
        **Pacer(Ledger.default()).summary(),
    }


if __name__ == "__main__":  # pragma: no cover - thin CLI
    import sys

    if sys.argv[1:] != ["--pace-report"]:
        print(
            "usage: python -m linkedin_mcp_server.mivia_outreach --pace-report",
            file=sys.stderr,
        )
        raise SystemExit(2)
    print(json.dumps(pace_report(), ensure_ascii=True))
