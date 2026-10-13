"""Whether this account's real daemon state root can host a test owner.

Tests that start a real owner run against the account's own state root,
because the owner reads the account home rather than the environment. On
Windows production refuses every election while the account still holds
a pre-v2 ``.mcp-server-linkedin`` that is not the tombstone it writes
itself; only the account's owner may remove it. Such a host cannot run
these tests, and the skip says why.
"""

from __future__ import annotations

import os

import pytest


def skip_if_the_real_daemon_root_is_refused() -> None:
    if os.name != "nt":
        return
    from linkedin_mcp_server import daemon_descriptor
    from linkedin_mcp_server.private_state import PrivateStateError

    try:
        daemon_descriptor._legacy_windows_tombstone_exists(
            daemon_descriptor._account_home()
        )
    except PrivateStateError as exc:
        pytest.skip(f"the account's real daemon state root is refused: {exc}")
