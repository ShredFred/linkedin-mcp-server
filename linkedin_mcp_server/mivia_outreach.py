"""MiViA fork: outreach ledger -- idempotency, canary gate and pacing caps.

One append-only JSONL file per machine (the LinkedIn session is per machine
too). Every send attempt is recorded *before* its outcome is known and closed
afterwards, so a crash between the two leaves an ``attempted`` row that blocks a
second send to the same person: an unknown outcome is treated as sent. A
duplicate message costs more than a missing one.

Caps (Frederik, 2026-09-29): 10-15 messages/day, 10-15 invites/day, 60
invites/week. The defaults sit inside those bands; the hard maxima are the band
tops and cannot be raised by a caller.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

LEDGER_ENV = "MIVIA_LINKEDIN_LEDGER"

MESSAGES_PER_DAY_DEFAULT = 12
MESSAGES_PER_DAY_MAX = 15
INVITES_PER_DAY_DEFAULT = 12
INVITES_PER_DAY_MAX = 15
INVITES_PER_WEEK_MAX = 60

DEFAULT_CANARY = "frederikstadler"

# Rows in these states block another send of the same text to the same person.
_BLOCKING = {"attempted", "sent", "verified", "unverified", "unknown"}
# Rows in these states count against the caps (anything that may have left).
_COUNTED = _BLOCKING


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
            for line in handle:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows

    def append(self, row: dict[str, Any]) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), **row}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
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
                and (sha is None or row.get("text_sha") == sha)
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
            started = datetime.fromisoformat(row.get("started_at") or row["at"])
            if started >= since:
                total += 1
        return total


def day_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now().astimezone()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


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


def delivered_in_conversation(message: str, conversation: dict[str, Any]) -> bool:
    """True when the read-back conversation text contains the whole message."""
    sections = conversation.get("sections") or {}
    haystack = canonical_text(" ".join(str(v) for v in sections.values()))
    return canonical_text(message) in haystack
