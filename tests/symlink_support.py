"""Whether this host lets the test process create symbolic links.

Windows grants ``SeCreateSymbolicLinkPrivilege`` only to elevated processes
or, with developer mode on, to every account. Without it ``os.symlink``
fails with WinError 1314, which says nothing about the code under test.
The answer is probed once, by creating a link, rather than inferred from
the platform: a Windows host with developer mode can run these tests, and
a POSIX host whose temporary directory refuses links cannot.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

import pytest


def _probe() -> bool:
    with tempfile.TemporaryDirectory(prefix="symlink-probe-") as directory:
        link = Path(directory) / "link"
        try:
            os.symlink("missing-target", link)
        except (OSError, NotImplementedError):
            return False
        return link.is_symlink()


CAN_SYMLINK = _probe()

requires_symlinks = pytest.mark.skipif(
    not CAN_SYMLINK,
    reason=(
        "this process may not create symbolic links (on Windows: "
        "SeCreateSymbolicLinkPrivilege, granted by developer mode or elevation)"
    ),
)
