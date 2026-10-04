"""Process-wide rate-limit hooks (#957).

The fork registers a gate (raises while a cooldown is active) and a recorder
(books a detected rate limit). Upstream code paths run unchanged when nothing
is registered. Lives in ``core`` so navigation and session share it without
an import cycle.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from linkedin_mcp_server.core.exceptions import RateLimitError

logger = logging.getLogger(__name__)

__all__ = ["check_rate_limit_gate", "report_rate_limit", "set_rate_limit_hooks"]

_rate_limit_gate: Callable[[], None] | None = None
_rate_limit_recorder: Callable[[RateLimitError], None] | None = None


def set_rate_limit_hooks(
    gate: Callable[[], None] | None,
    recorder: Callable[[RateLimitError], None] | None,
) -> None:
    global _rate_limit_gate, _rate_limit_recorder
    _rate_limit_gate = gate
    _rate_limit_recorder = recorder


def check_rate_limit_gate() -> None:
    if _rate_limit_gate is not None:
        _rate_limit_gate()


def report_rate_limit(exc: RateLimitError) -> None:
    """Hand a detected rate limit to the recorder; never swallow the error.

    A recorder failure (ledger unavailable) is logged and the original
    RateLimitError still propagates: the caller stops either way.
    """
    if getattr(exc, "_mcp_rate_limit_reported", False):
        return
    try:
        exc._mcp_rate_limit_reported = True  # type: ignore[attr-defined]
    except Exception:
        pass
    if _rate_limit_recorder is None:
        return
    try:
        _rate_limit_recorder(exc)
    except Exception as record_error:  # fail-closed: the error below still stops
        logger.error("Could not record rate limit cooldown: %s", record_error)
