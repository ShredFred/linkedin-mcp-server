"""Fork extension: several LinkedIn accounts behind one server.

The register lives outside the repository (``~/.linkedin-mcp-accounts.json`` or
``$LINKEDIN_MCP_ACCOUNTS_FILE``). Without a register nothing here is active and
the server behaves exactly as before.

Everything in this module is pure: parsing, validation, rights and identity
comparison, the audit line. The browser-facing half lives in
:mod:`linkedin_mcp_server.account_middleware`.

Why the directory rules are strict: a profile reset deletes the profile
directory *and* artifacts in its parent (cookies, source state, runtime
profiles). Two accounts whose parents overlap would therefore share a session
or lose each other's on a reset. Overlap is refused when the register is read,
not discovered when a reset runs.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REGISTRY_ENV = "LINKEDIN_MCP_ACCOUNTS_FILE"
STATE_ROOT_ENV = "LINKEDIN_MCP_ACCOUNTS_STATE_DIR"
DEFAULT_REGISTRY = "~/.linkedin-mcp-accounts.json"
DEFAULT_STATE_ROOT = "~/.linkedin-mcp/accounts"
SCHEMA_VERSION = 1

ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
#: Rights tokens besides explicit tool names.
READ_TOKEN = "read"
ALL_TOKEN = "*"
DEFAULT_ALLOWED: tuple[str, ...] = (READ_TOKEN,)
#: Tools that work on the register itself and never touch a profile.
ACCOUNT_FREE_TOOLS = frozenset({"list_accounts"})

_ACCOUNT_KEYS = {"alias", "user_data_dir", "expected_identity", "allowed_tools"}
_TOP_KEYS = {"version", "default", "accounts"}
_URN_RE = re.compile(r"^urn:li:(fsd_profile|member|person):[A-Za-z0-9_-]+$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9\-_%]{3,100}$")


class AccountError(Exception):
    """A refusal with a stable machine-readable code. Never carries tool data."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class Account:
    alias: str
    user_data_dir: Path
    expected_identity: str | None
    allowed_tools: tuple[str, ...] = DEFAULT_ALLOWED

    @property
    def auth_root(self) -> Path:
        """The directory a reset may empty: the profile's parent."""
        return self.user_data_dir.parent


@dataclass(frozen=True)
class Registry:
    path: Path
    default: str
    accounts: dict[str, Account] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def resolve(self, alias: object) -> Account:
        """The named account, or the default when *alias* is None.

        Fail-closed: a named alias that is missing is an error, never the
        default. Only an *omitted* alias selects the default.
        """
        if alias is None:
            return self.accounts[self.default]
        if not isinstance(alias, str) or alias not in self.accounts:
            raise AccountError(
                "account_unknown",
                "the requested account alias is not in the register; "
                "no other account was used",
            )
        return self.accounts[alias]


def registry_path() -> Path:
    return Path(os.environ.get(REGISTRY_ENV) or DEFAULT_REGISTRY).expanduser()


def state_root() -> Path:
    return Path(os.environ.get(STATE_ROOT_ENV) or DEFAULT_STATE_ROOT).expanduser()


def account_state_dir(alias: str) -> Path:
    """Per-account fork state (ledgers, audit). Outside the auth root on
    purpose: a reset must not erase the double-send ledger."""
    if not ALIAS_RE.match(alias):
        raise AccountError("account_invalid", "invalid alias")
    return state_root() / alias


def _canon(path: Path) -> Path:
    return Path(os.path.normcase(os.path.abspath(path.expanduser())))


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _dangerous_root(parent: Path) -> bool:
    """A parent a reset must never be allowed to empty."""
    home = _canon(Path.home())
    if parent == home or home.is_relative_to(parent):
        return True
    return parent.parent == parent  # filesystem root / drive root


def normalize_identity(value: str) -> str:
    """URN as-is, slug lowercased and stripped of a profile URL around it."""
    value = value.strip()
    if value.startswith("urn:li:"):
        if not _URN_RE.match(value):
            raise AccountError("registry_invalid", "expected_identity is no valid URN")
        return value
    match = re.search(r"/in/([^/?#]+)", value)
    slug = (match.group(1) if match else value).strip("/").lower()
    if not _SLUG_RE.match(slug) or slug == "me":
        raise AccountError(
            "registry_invalid", "expected_identity is neither a URN nor a slug"
        )
    return slug


def identity_matches(expected: str, observed: set[str]) -> bool:
    """Exact comparison against what the signed-in page revealed."""
    norm = {o.lower() if not o.startswith("urn:li:") else o for o in observed}
    return normalize_identity(expected) in norm


def permission_warnings(path: Path) -> list[str]:
    """Warn (not refuse) when the register is readable or writable by others."""
    if sys.platform == "win32":
        return []
    try:
        mode = path.stat().st_mode
    except OSError:
        return []
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        return [
            f"account register {path} is accessible by group/others "
            f"(mode {oct(mode & 0o777)}); chmod 600 recommended"
        ]
    return []


def parse_registry(data: Any, path: Path) -> Registry:
    """Validate the register strictly. Any defect refuses the whole file."""

    def bad(msg: str) -> AccountError:
        return AccountError("registry_invalid", msg)

    if not isinstance(data, dict):
        raise bad("top level must be an object")
    if unknown := set(data) - _TOP_KEYS:
        raise bad(f"unknown keys: {sorted(unknown)}")
    if data.get("version") != SCHEMA_VERSION:
        raise bad(f"version must be {SCHEMA_VERSION}")
    raw = data.get("accounts")
    if not isinstance(raw, list) or not raw:
        raise bad("accounts must be a non-empty list")
    accounts: dict[str, Account] = {}
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise bad(f"accounts[{i}] must be an object")
        if unknown := set(entry) - _ACCOUNT_KEYS:
            raise bad(f"accounts[{i}] unknown keys: {sorted(unknown)}")
        alias = entry.get("alias")
        if not isinstance(alias, str) or not ALIAS_RE.match(alias):
            raise bad(f"accounts[{i}].alias must match {ALIAS_RE.pattern}")
        if alias in accounts:
            raise bad(f"duplicate alias {alias!r}")
        udd = entry.get("user_data_dir")
        if not isinstance(udd, str) or not udd.strip():
            raise bad(f"{alias}: user_data_dir must be a non-empty string")
        udd_path = _canon(Path(udd))
        if _dangerous_root(udd_path.parent):
            raise bad(
                f"{alias}: the parent of user_data_dir is the home or a root "
                "directory; a reset would empty it. Use a dedicated parent."
            )
        ident = entry.get("expected_identity")
        if ident is not None:
            if not isinstance(ident, str):
                raise bad(f"{alias}: expected_identity must be a string or null")
            ident = normalize_identity(ident)
        tools = entry.get("allowed_tools", list(DEFAULT_ALLOWED))
        if not isinstance(tools, list) or not all(
            isinstance(t, str) and t for t in tools
        ):
            raise bad(f"{alias}: allowed_tools must be a list of strings")
        accounts[alias] = Account(alias, udd_path, ident, tuple(tools))
    # Each account owns its parent exclusively: no equal, nested or crossing
    # paths, because a reset of one would delete or corrupt the other.
    items = list(accounts.values())
    for i, a in enumerate(items):
        for b in items[i + 1 :]:
            if (
                _overlaps(a.auth_root, b.auth_root)
                or _overlaps(a.auth_root, b.user_data_dir)
                or _overlaps(b.auth_root, a.user_data_dir)
            ):
                raise bad(
                    f"{a.alias} and {b.alias} share or nest their profile "
                    "directories; each account needs its own parent directory"
                )
    default = data.get("default")
    if default is None and len(accounts) == 1:
        default = next(iter(accounts))
    if not isinstance(default, str) or default not in accounts:
        raise bad("default must name a registered alias")
    return Registry(path, default, accounts, tuple(permission_warnings(path)))


def load_registry(path: Path | None = None) -> Registry | None:
    """The register, or ``None`` when there is none (legacy single account).

    A file that exists but is invalid raises: falling back to single-account
    mode would silently run every call on whatever profile is configured.
    """
    path = path or registry_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AccountError(
            "registry_invalid", f"cannot read {path}: {type(exc).__name__}"
        ) from exc
    return parse_registry(data, path)


def tool_allowed(account: Account, tool: str, *, read_only: bool) -> bool:
    """Whether *account* may run *tool*. An unknown annotation counts as write."""
    allowed = set(account.allowed_tools)
    if ALL_TOKEN in allowed or tool in allowed:
        return True
    return read_only and READ_TOKEN in allowed


def default_user_data_dir(alias: str) -> Path:
    """A dedicated parent per account: ``~/.linkedin-mcp-accounts.d/<alias>/profile``."""
    if not ALIAS_RE.match(alias):
        raise AccountError("account_invalid", "invalid alias")
    return Path("~/.linkedin-mcp-accounts.d").expanduser() / alias / "profile"


def upsert_account(
    path: Path,
    alias: str,
    *,
    user_data_dir: Path | None = None,
    expected_identity: str | None = None,
    allowed_tools: list[str] | None = None,
) -> Account:
    """Add or update one account and write the register atomically.

    The result is validated as a whole before anything is written, so an
    overlapping directory is refused without touching the file.
    """
    data: dict[str, Any]
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        parse_registry(data, path)  # refuse to extend a broken register
    else:
        data = {"version": SCHEMA_VERSION, "accounts": []}
    entries = [e for e in data["accounts"] if e.get("alias") != alias]
    old = next((e for e in data["accounts"] if e.get("alias") == alias), {})
    entry = {
        "alias": alias,
        "user_data_dir": str(
            user_data_dir or old.get("user_data_dir") or default_user_data_dir(alias)
        ),
        "expected_identity": expected_identity or old.get("expected_identity"),
        "allowed_tools": allowed_tools
        or old.get("allowed_tools")
        or list(DEFAULT_ALLOWED),
    }
    entries.append(entry)
    data["accounts"] = entries
    data.setdefault("default", alias)
    registry = parse_registry(data, path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    if sys.platform != "win32":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return registry.accounts[alias]


def audit(alias: str, tool: str, status: str) -> None:
    """One line per tool call in the account's own log. Never content."""
    try:
        target = account_state_dir(alias)
        target.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "alias": alias,
                "tool": tool,
                "status": status,
            }
        )
        with open(target / "audit.jsonl", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except (OSError, AccountError):
        # Auditing must not turn a refusal into a different error.
        pass
