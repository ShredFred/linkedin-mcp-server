"""Keep every child process of the server off the desktop on Windows.

The server is started by MCP clients that often have no console of their own
(background sessions, GUI hosts, ``pythonw``). Windows then gives every console
child -- the Playwright node driver, ``tasklist``/PowerShell snapshots, helper
interpreters -- a fresh, visible console window. One client start could leave
several windows open.

``install()`` makes ``CREATE_NO_WINDOW`` the default for ``subprocess.Popen``
(and therefore ``subprocess.run`` and ``asyncio.create_subprocess_exec``) unless
the caller already chose a console disposition (``DETACHED_PROCESS`` or
``CREATE_NEW_CONSOLE``). GUI programs such as the browser are unaffected: the
flag only concerns console allocation. On other platforms it does nothing.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

_CONSOLE_CHOICES = 0x00000008 | 0x00000010  # DETACHED_PROCESS | CREATE_NEW_CONSOLE
_CREATE_NO_WINDOW = 0x08000000
_MARK = "_linkedin_mcp_no_console"


def effective_flags(flags: int) -> int:
    """Return ``flags`` with ``CREATE_NO_WINDOW`` added where it is allowed."""
    if flags & _CONSOLE_CHOICES:
        return flags
    return flags | _CREATE_NO_WINDOW


def install() -> bool:
    """Patch ``subprocess.Popen`` once; return whether a patch is active."""
    if sys.platform != "win32":
        return False
    popen = subprocess.Popen
    if getattr(popen.__init__, _MARK, False):
        return True
    original = popen.__init__

    def __init__(self: Any, *args: Any, **kwargs: Any) -> None:
        # creationflags is the 14th positional parameter; nobody here passes it
        # positionally, so only the keyword form is adjusted.
        kwargs["creationflags"] = effective_flags(int(kwargs.get("creationflags") or 0))
        original(self, *args, **kwargs)

    setattr(__init__, _MARK, True)
    popen.__init__ = __init__  # type: ignore[method-assign]
    return True
