"""Which account the current tool call runs for (fork extension).

A ContextVar, so concurrent tasks never see each other's account. ``None``
means single-account mode, and every consumer then falls back to its
historical default.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from pathlib import Path

_current_alias: ContextVar[str | None] = ContextVar("linkedin_account", default=None)
_current_state_dir: ContextVar[Path | None] = ContextVar(
    "linkedin_account_state_dir", default=None
)


def current_alias() -> str | None:
    return _current_alias.get()


def current_state_dir() -> Path | None:
    return _current_state_dir.get()


def bind(alias: str, state_dir: Path) -> tuple[Token[str | None], Token[Path | None]]:
    return _current_alias.set(alias), _current_state_dir.set(state_dir)


def unbind(tokens: tuple[Token[str | None], Token[Path | None]]) -> None:
    _current_alias.reset(tokens[0])
    _current_state_dir.reset(tokens[1])
