"""Remove leftover ``*.CHROME_DELETE`` directories from the profile before a launch.

When Chromium starts with a different version than the one recorded in
``Last Version``, it discards its GPU, shader and snapshot caches by renaming
them into ``<name>.CHROME_DELETE`` directories inside the profile and deleting
those afterwards. If that deletion is interrupted -- another browser process
still holds a file open, or the launch is killed -- the directories stay
behind. The next version switch then renames into a target that already
exists, the rename fails with "access denied", and the launch exits with
code 33 ("Failed to start browser"). Observed after alternating between two
installed browser revisions on one profile.

The directories carry nothing the session needs: they are caches Chromium has
already decided to throw away. Removing them is best effort -- a directory
that is still locked stays and is reported, the launch proceeds as before.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
from pathlib import Path

from linkedin_mcp_server.exceptions import ProfileRootRefusedError
from linkedin_mcp_server.session_state import (
    _owned,
    canonical,
    runtime_profiles_root,
)

logger = logging.getLogger(__name__)

SUFFIX = ".CHROME_DELETE"


def _cleanup_target(profile_dir: Path, source_profile_dir: Path | None) -> Path:
    # Check the source, not a derived runtime's disposable auth root. A suffix
    # alone never proves that a directory or file belongs to this server.
    source = _owned(source_profile_dir or profile_dir)
    requested = profile_dir.expanduser().absolute()
    target = canonical(profile_dir)
    if requested != target:
        raise ProfileRootRefusedError("Cache cleanup refuses a linked profile path")
    if target == source:
        return target
    try:
        relative = target.relative_to(runtime_profiles_root(source))
    except ValueError:
        relative = None
    if relative is None or len(relative.parts) != 2 or relative.parts[-1] != "profile":
        raise ProfileRootRefusedError(
            "Cache cleanup target is neither the owned source nor its runtime profile"
        )
    return target


def remove_orphaned_cache_deletions(
    profile_dir: Path, *, source_profile_dir: Path | None = None
) -> dict[str, list[str]]:
    """Delete ``*.CHROME_DELETE`` entries directly inside ``profile_dir``.

    Returns the names removed and the names that could not be removed. Never
    raises for a filesystem error. An unowned or escaped target is refused
    before an exists-or-empty check. Runtime callers supply their source root.
    """
    target = _cleanup_target(profile_dir, source_profile_dir)
    removed: list[str] = []
    kept: list[str] = []
    try:
        identity = target.stat()
        entries = list(target.iterdir())
    except OSError:
        return {"removed": removed, "kept": kept}
    for entry in entries:
        if not entry.name.endswith(SUFFIX):
            continue
        try:
            # Windows junctions are not is_symlink(). Refuse every reparse
            # entry before deciding whether it is a directory or a file.
            attributes = getattr(entry.lstat(), "st_file_attributes", 0)
            if entry.is_symlink() or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                kept.append(entry.name)
                continue
            if canonical(target) != target or not os.path.samestat(
                identity, target.stat()
            ):
                raise ProfileRootRefusedError(
                    "Cache cleanup profile changed during cleanup"
                )
            _owned(source_profile_dir or profile_dir)
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed.append(entry.name)
        except OSError as exc:
            kept.append(entry.name)
            logger.warning(
                "Could not remove %s before launch: %s", entry.name, type(exc).__name__
            )
    if removed:
        logger.info(
            "Removed leftover cache deletions before launch: %s",
            ", ".join(sorted(removed)),
        )
    return {"removed": removed, "kept": kept}
