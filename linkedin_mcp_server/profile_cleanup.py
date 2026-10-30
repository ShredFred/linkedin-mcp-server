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
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

SUFFIX = ".CHROME_DELETE"


def remove_orphaned_cache_deletions(profile_dir: Path) -> dict[str, list[str]]:
    """Delete ``*.CHROME_DELETE`` entries directly inside ``profile_dir``.

    Returns the names removed and the names that could not be removed. Never
    raises for a filesystem error and never touches anything without the suffix.
    """
    removed: list[str] = []
    kept: list[str] = []
    try:
        entries = list(Path(profile_dir).iterdir())
    except OSError:
        return {"removed": removed, "kept": kept}
    for entry in entries:
        if not entry.name.endswith(SUFFIX) or entry.is_symlink():
            continue
        try:
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
