"""Codex sandbox read access, owner decision 2026-10-13."""

from __future__ import annotations

import pytest

from linkedin_mcp_server import windows_acl
from linkedin_mcp_server.windows_acl import (
    ACCESS_ALLOWED_ACE_TYPE,
    ACCESS_DENIED_ACE_TYPE,
    CONTAINER_INHERITANCE,
    AccessEntry,
)

_SID = "S-1-5-21-1-2-3-1001"
_READ_EXECUTE = 0x001200A9


@pytest.fixture
def lookup(monkeypatch):
    accounts: dict[str, tuple[str, str, int]] = {}
    monkeypatch.setattr(windows_acl, "_lookup_account_sid", accounts.get)
    monkeypatch.setattr(windows_acl, "_local_computer_name", lambda: "HOSTPC")
    return accounts


def _entry(mask: int = _READ_EXECUTE, ace_type: int = ACCESS_ALLOWED_ACE_TYPE):
    return AccessEntry(sid=_SID, type=ace_type, flags=CONTAINER_INHERITANCE, mask=mask)


def test_read_only_codex_group_is_accepted(lookup):
    lookup[_SID] = ("CodexSandboxUsers", "HOSTPC", 4)
    assert windows_acl._is_codex_sandbox_read(_entry())


@pytest.mark.parametrize(
    "mask",
    [
        _READ_EXECUTE | windows_acl.FILE_WRITE_DATA,
        _READ_EXECUTE | windows_acl.DELETE,
        _READ_EXECUTE | windows_acl.WRITE_DAC,
        _READ_EXECUTE | windows_acl.WRITE_OWNER,
        windows_acl.GENERIC_WRITE,
        windows_acl.GENERIC_ALL,
        windows_acl.FILE_ALL_ACCESS,
    ],
)
def test_codex_group_with_write_access_is_refused(lookup, mask):
    lookup[_SID] = ("CodexSandboxUsers", "HOSTPC", 4)
    assert not windows_acl._is_codex_sandbox_read(_entry(mask))


def test_deny_entry_is_not_the_allowance(lookup):
    lookup[_SID] = ("CodexSandboxUsers", "HOSTPC", 4)
    assert not windows_acl._is_codex_sandbox_read(
        _entry(ace_type=ACCESS_DENIED_ACE_TYPE)
    )


def test_other_group_with_read_access_is_refused(lookup):
    lookup[_SID] = ("OtherSandboxUsers", "HOSTPC", 4)
    assert not windows_acl._is_codex_sandbox_read(_entry())


def test_domain_group_with_same_name_is_refused(lookup):
    lookup[_SID] = ("CodexSandboxUsers", "CORPDOMAIN", 4)
    assert not windows_acl._is_codex_sandbox_read(_entry())


def test_unresolvable_sid_is_refused(lookup):
    assert not windows_acl._is_codex_sandbox_read(_entry())
