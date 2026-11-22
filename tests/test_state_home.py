"""LINKEDIN_MCP_HOME moves every default state path, and only when set."""

from __future__ import annotations

from pathlib import Path

import pytest

from linkedin_mcp_server import state_home as sh
from linkedin_mcp_server.daemon import descriptor


def test_default_is_home_dot_linkedin_mcp(monkeypatch):
    monkeypatch.delenv(sh.STATE_HOME_ENV, raising=False)
    assert sh.configured_state_home() is None
    assert sh.state_home() == Path.home() / ".linkedin-mcp"
    assert sh.default_profile_dir() == Path.home() / ".linkedin-mcp" / "profile"


def test_configured_home_relocates_state_and_profile(monkeypatch, tmp_path):
    target = tmp_path / "state"
    monkeypatch.setenv(sh.STATE_HOME_ENV, str(target))
    assert sh.state_home() == target
    assert sh.default_profile_dir() == target / "profile"
    assert sh.is_state_root(target)
    assert not sh.is_state_root(tmp_path)


def test_relative_home_is_refused(monkeypatch):
    monkeypatch.setenv(sh.STATE_HOME_ENV, "relative/dir")
    with pytest.raises(ValueError):
        sh.state_home()


def test_outreach_state_file_follows_home(monkeypatch, tmp_path):
    from linkedin_mcp_server.ext_outreach import state_file

    monkeypatch.setenv(sh.STATE_HOME_ENV, str(tmp_path / "state"))
    assert state_file("x.json") == tmp_path / "state" / "x.json"


def test_daemon_anchor_is_parent_of_configured_home(monkeypatch, tmp_path):
    monkeypatch.delenv(descriptor.TEST_ACCOUNT_HOME_ENV, raising=False)
    monkeypatch.setenv(sh.STATE_HOME_ENV, str(tmp_path / ".linkedin-mcp"))
    assert descriptor._daemon_anchor() == (
        tmp_path if descriptor.os.name == "nt" else tmp_path.resolve()
    )


def test_test_account_home_wins_over_configured_home(monkeypatch, tmp_path):
    home = tmp_path / "acct"
    home.mkdir()
    monkeypatch.setenv(descriptor.TEST_ACCOUNT_HOME_ENV, str(home))
    monkeypatch.setenv(sh.STATE_HOME_ENV, str(tmp_path / "elsewhere" / "s"))
    assert descriptor._daemon_anchor().name == "acct"


def test_daemon_anchor_requires_existing_parent(monkeypatch, tmp_path):
    monkeypatch.delenv(descriptor.TEST_ACCOUNT_HOME_ENV, raising=False)
    monkeypatch.setenv(sh.STATE_HOME_ENV, str(tmp_path / "missing" / "state"))
    with pytest.raises(descriptor.DescriptorError):
        descriptor._daemon_anchor()
