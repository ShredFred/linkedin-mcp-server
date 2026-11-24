"""Pacer bookings for the upstream read tools.

The fork's own tools ask the pacer before they touch LinkedIn (``_pace`` in
``tools/ext.py``). The read tools inherited from upstream did not, so a day of
profile reads and people searches left ``pace_status`` at zero for
``profile_view`` and ``search``. Every read now books its action kind here:

- profile reads -> ``profile_view``
- searches -> ``search``
- feed, company, job, post and inbox pages -> ``page_read``

A booking that is refused returns the same ``pace_budget_spent`` /
``pace_lock_busy`` dict as the fork tools. A read that raises before LinkedIn
answered gives its units back, as the fork's reads do.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from linkedin_mcp_server import ext_outreach as outreach

T = TypeVar("T")


def take_read(action: str, count: int = 1, *, tool: str) -> dict[str, Any] | None:
    """Book *count* units of *action*; return a refusal dict when spent."""
    try:
        outreach.Pacer(outreach.Ledger.default()).take(action, count, tool=tool)
    except outreach.PaceExceeded as spent:
        return {"status": "pace_budget_spent", "pace": spent.state}
    except TimeoutError as busy:
        return {
            "status": "pace_lock_busy",
            "detail": f"{busy}; nothing was read or booked, try again shortly",
        }
    return None


def refund_read(action: str, count: int = 1, *, tool: str, reason: str) -> None:
    """Give back a booking whose read produced no answer; never raises."""
    try:
        outreach.Pacer(outreach.Ledger.default()).refund(
            action, count, tool=tool, reason=reason
        )
    except Exception:  # a refund must never mask the read's own error
        pass


_REFUSALS = {"pace_budget_spent", "pace_lock_busy"}


def is_refusal(result: Any) -> bool:
    """True for the dict :func:`paced_read` returns instead of reading."""
    return isinstance(result, dict) and result.get("status") in _REFUSALS


async def paced_read(
    action: str,
    tool: str,
    read: Callable[[], Awaitable[T]],
    count: int = 1,
) -> T | dict[str, Any]:
    """Book, run *read*, refund on failure; the refusal dict when spent."""
    refusal = take_read(action, count, tool=tool)
    if refusal is not None:
        return refusal
    try:
        return await read()
    except BaseException as exc:
        refund_read(action, count, tool=tool, reason=type(exc).__name__)
        raise
