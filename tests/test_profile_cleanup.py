"""Leftover ``*.CHROME_DELETE`` directories are removed before a launch."""
from pathlib import Path

from linkedin_mcp_server import profile_cleanup as pc


def test_removes_only_chrome_delete_entries(tmp_path: Path) -> None:
    (tmp_path / "profile.CHROME_DELETE" / "GPUPersistentCache").mkdir(parents=True)
    (tmp_path / "Snapshots.CHROME_DELETE" / "1.2.3").mkdir(parents=True)
    (tmp_path / "Default").mkdir()
    (tmp_path / "Local State").write_text("{}")
    result = pc.remove_orphaned_cache_deletions(tmp_path)
    assert sorted(result["removed"]) == ["Snapshots.CHROME_DELETE", "profile.CHROME_DELETE"]
    assert result["kept"] == []
    names = {p.name for p in tmp_path.iterdir()}
    assert {"Default", "Local State"} <= names
    assert not any(n.endswith(pc.SUFFIX) for n in names)


def test_locked_entry_is_kept_and_reported(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "x.CHROME_DELETE").mkdir()

    def locked(_path):
        raise PermissionError("in use")
    monkeypatch.setattr(pc.shutil, "rmtree", locked)
    result = pc.remove_orphaned_cache_deletions(tmp_path)
    assert result == {"removed": [], "kept": ["x.CHROME_DELETE"]}


def test_missing_profile_is_not_an_error(tmp_path: Path) -> None:
    assert pc.remove_orphaned_cache_deletions(tmp_path / "missing") == {"removed": [], "kept": []}
