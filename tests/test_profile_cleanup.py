"""Leftover ``*.CHROME_DELETE`` directories are removed before a launch."""

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from linkedin_mcp_server import profile_cleanup as pc
from linkedin_mcp_server.core.browser import BrowserManager
from linkedin_mcp_server.core.exceptions import NetworkError
from linkedin_mcp_server.exceptions import ProfileRootRefusedError
from linkedin_mcp_server.profile_claim import claim_path, ensure_profile_claim
from linkedin_mcp_server.session_state import runtime_profile_dir


@pytest.fixture
def owned_profile(tmp_path: Path) -> Path:
    profile = tmp_path / "auth" / "profile"
    ensure_profile_claim(profile)
    profile.mkdir()
    return profile


def test_removes_only_chrome_delete_entries(owned_profile: Path) -> None:
    tmp_path = owned_profile
    (tmp_path / "profile.CHROME_DELETE" / "GPUPersistentCache").mkdir(parents=True)
    (tmp_path / "Snapshots.CHROME_DELETE" / "1.2.3").mkdir(parents=True)
    (tmp_path / "Default").mkdir()
    (tmp_path / "Local State").write_text("{}")
    result = pc.remove_orphaned_cache_deletions(tmp_path)
    assert sorted(result["removed"]) == [
        "Snapshots.CHROME_DELETE",
        "profile.CHROME_DELETE",
    ]
    assert result["kept"] == []
    names = {p.name for p in tmp_path.iterdir()}
    assert {"Default", "Local State"} <= names
    assert not any(n.endswith(pc.SUFFIX) for n in names)


def test_locked_entry_is_kept_and_reported(owned_profile: Path, monkeypatch) -> None:
    tmp_path = owned_profile
    (tmp_path / "x.CHROME_DELETE").mkdir()

    def locked(_path):
        raise PermissionError("in use")

    monkeypatch.setattr(pc.shutil, "rmtree", locked)
    result = pc.remove_orphaned_cache_deletions(tmp_path)
    assert result == {"removed": [], "kept": ["x.CHROME_DELETE"]}


def test_missing_owned_profile_is_not_an_error(owned_profile: Path) -> None:
    owned_profile.rmdir()
    assert pc.remove_orphaned_cache_deletions(owned_profile) == {
        "removed": [],
        "kept": [],
    }


def test_unclaimed_cache_suffix_does_not_authorize_deletion(tmp_path: Path) -> None:
    profile = tmp_path / "foreign" / "profile"
    sentinel = profile / "notes.CHROME_DELETE" / "sentinel.txt"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("synthetic foreign content", encoding="utf-8")
    with pytest.raises(ProfileRootRefusedError):
        pc.remove_orphaned_cache_deletions(profile)
    assert sentinel.read_text(encoding="utf-8") == "synthetic foreign content"
    assert not claim_path(profile).exists()


def test_missing_foreign_root_is_refused_before_an_empty_return(tmp_path: Path) -> None:
    profile = tmp_path / "foreign" / "profile"
    with pytest.raises(ProfileRootRefusedError):
        pc.remove_orphaned_cache_deletions(profile)
    assert not profile.parent.exists()


def test_claimed_source_cannot_authorize_a_foreign_target(
    owned_profile: Path, tmp_path: Path
) -> None:
    foreign = tmp_path / "foreign" / "profile"
    sentinel = foreign / "notes.CHROME_DELETE"
    foreign.mkdir(parents=True)
    sentinel.write_text("foreign", encoding="utf-8")
    with pytest.raises(ProfileRootRefusedError):
        pc.remove_orphaned_cache_deletions(foreign, source_profile_dir=owned_profile)
    assert sentinel.read_text(encoding="utf-8") == "foreign"


def test_owned_runtime_cleanup_uses_the_source_claim(owned_profile: Path) -> None:
    runtime = runtime_profile_dir("synthetic-runtime", owned_profile)
    sentinel = runtime / "cache.CHROME_DELETE" / "sentinel.txt"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("rebuildable cache", encoding="utf-8")
    result = pc.remove_orphaned_cache_deletions(
        runtime, source_profile_dir=owned_profile
    )
    assert result == {"removed": ["cache.CHROME_DELETE"], "kept": []}
    assert not sentinel.exists()
    assert claim_path(owned_profile).exists()
    assert not claim_path(runtime).exists()


def _junction_or_symlink(link: Path, target: Path) -> None:
    if os.name == "nt":
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode:
            pytest.skip("test filesystem cannot create a junction")
    else:
        link.symlink_to(target, target_is_directory=True)


def test_cache_entry_reparse_link_and_its_foreign_target_are_kept(
    owned_profile: Path, tmp_path: Path
) -> None:
    foreign = tmp_path / "foreign-data"
    foreign.mkdir()
    sentinel = foreign / "sentinel.txt"
    sentinel.write_text("foreign", encoding="utf-8")
    link = owned_profile / "cache.CHROME_DELETE"
    _junction_or_symlink(link, foreign)
    try:
        result = pc.remove_orphaned_cache_deletions(owned_profile)
        assert result == {"removed": [], "kept": ["cache.CHROME_DELETE"]}
        assert sentinel.read_text(encoding="utf-8") == "foreign"
        assert link.exists()
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


def test_runtime_root_reparse_escape_does_not_gain_source_authority(
    owned_profile: Path, tmp_path: Path
) -> None:
    foreign = tmp_path / "foreign-runtime"
    sentinel = foreign / "synthetic-runtime" / "profile" / "cache.CHROME_DELETE"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("foreign", encoding="utf-8")
    link = owned_profile.parent / "runtime-profiles"
    _junction_or_symlink(link, foreign)
    try:
        with pytest.raises(ProfileRootRefusedError):
            pc.remove_orphaned_cache_deletions(
                runtime_profile_dir("synthetic-runtime", owned_profile),
                source_profile_dir=owned_profile,
            )
        assert sentinel.read_text(encoding="utf-8") == "foreign"
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


def test_profile_replaced_during_listing_preserves_both_trees(
    owned_profile: Path, monkeypatch
) -> None:
    original_cache = owned_profile / "cache.CHROME_DELETE"
    original_cache.write_text("original", encoding="utf-8")
    moved = owned_profile.with_name("old-profile")
    original_iterdir = Path.iterdir

    def replace_after_listing(path):
        entries = list(original_iterdir(path))
        if path == owned_profile:
            path.rename(moved)
            path.mkdir()
            (path / "cache.CHROME_DELETE").write_text("replacement", encoding="utf-8")
        return iter(entries)

    monkeypatch.setattr(Path, "iterdir", replace_after_listing)
    with pytest.raises(ProfileRootRefusedError, match="changed during cleanup"):
        pc.remove_orphaned_cache_deletions(owned_profile)
    assert original_cache.read_text(encoding="utf-8") == "replacement"
    assert (moved / "cache.CHROME_DELETE").read_text(encoding="utf-8") == "original"


async def test_browser_manager_forwards_source_authority_for_runtime_cleanup(
    owned_profile: Path, monkeypatch
) -> None:
    runtime = runtime_profile_dir("synthetic-runtime", owned_profile)
    cache = runtime / "cache.CHROME_DELETE"
    cache.mkdir(parents=True)
    (cache / "sentinel.txt").write_text("rebuildable cache", encoding="utf-8")
    driver = SimpleNamespace(
        chromium=SimpleNamespace(
            launch_persistent_context=AsyncMock(
                side_effect=RuntimeError("stop after synthetic cleanup")
            )
        )
    )
    manager = BrowserManager(user_data_dir=runtime, source_profile_dir=owned_profile)
    monkeypatch.setattr(
        manager, "_start_contained_driver", AsyncMock(return_value=driver)
    )
    try:
        with pytest.raises(NetworkError, match="stop after synthetic cleanup"):
            await manager.start()
        assert not cache.exists()
        assert claim_path(owned_profile).exists()
    finally:
        await manager.close()
