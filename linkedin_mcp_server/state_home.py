"""Where this server keeps its local state, in one place.

By default everything lives under ``~/.linkedin-mcp`` (browser profile, cookies,
pacer and outreach state, update cache) and the daemon election under
``~/.linkedin-mcp-daemon``. Setting ``LINKEDIN_MCP_HOME`` to an absolute path
moves the whole tree: that directory takes the role of ``~/.linkedin-mcp`` and
the daemon namespace is created beside it, as ``<parent>/.linkedin-mcp-daemon``.
The parent therefore anchors daemon state and must be a directory only the
account (and system administrators) can change, exactly like the account home
it replaces.

``USER_DATA_DIR`` still overrides the browser profile alone; this variable
moves the defaults every other path derives from.
"""

from __future__ import annotations

import os
from pathlib import Path

STATE_HOME_ENV = "LINKEDIN_MCP_HOME"
DEFAULT_STATE_DIR_NAME = ".linkedin-mcp"


def configured_state_home() -> Path | None:
    """Return the relocated state directory, or None when the default applies."""
    raw = os.environ.get(STATE_HOME_ENV, "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{STATE_HOME_ENV} must be an absolute path, got {raw!r}")
    return path


def state_home() -> Path:
    """Return the directory that plays the role of ``~/.linkedin-mcp``."""
    return configured_state_home() or Path.home() / DEFAULT_STATE_DIR_NAME


def default_profile_dir() -> Path:
    """Return the default persistent browser profile directory."""
    return state_home() / "profile"


def is_state_root(path: Path) -> bool:
    """Whether *path* is the top of a state tree (default name or relocated)."""
    if path.name == DEFAULT_STATE_DIR_NAME:
        return True
    configured = configured_state_home()
    return configured is not None and os.path.normcase(
        str(path.absolute())
    ) == os.path.normcase(str(configured.absolute()))
