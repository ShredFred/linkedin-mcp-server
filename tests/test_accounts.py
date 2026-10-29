"""Multi-account support: register, identity, locks, rights, reset isolation.

No LinkedIn access: the browser side is replaced by fakes. Aliases, slugs and
paths are placeholders.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import mcp.types as mt
import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools.base import ToolResult

from linkedin_mcp_server import account_context, accounts
from linkedin_mcp_server.account_middleware import AccountMiddleware
from linkedin_mcp_server.accounts import AccountError, parse_registry


def _reg(tmp_path: Path, **over) -> dict:
    data = {
        "version": 1,
        "default": "alpha",
        "accounts": [
            {
                "alias": "alpha",
                "user_data_dir": str(tmp_path / "acc-alpha" / "profile"),
                "expected_identity": "example-alpha",
                "allowed_tools": ["read"],
            },
            {
                "alias": "beta",
                "user_data_dir": str(tmp_path / "acc-beta" / "profile"),
                "expected_identity": "https://www.linkedin.com/in/Example-Beta/",
                "allowed_tools": ["*"],
            },
        ],
    }
    data.update(over)
    return data


def _write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# -- register -----------------------------------------------------------------


def test_no_register_means_legacy_mode(tmp_path):
    assert accounts.load_registry(tmp_path / "missing.json") is None


def test_valid_register(tmp_path):
    reg = accounts.load_registry(_write(tmp_path, _reg(tmp_path)))
    assert reg is not None
    assert set(reg.accounts) == {"alpha", "beta"}
    assert reg.accounts["beta"].expected_identity == "example-beta"
    assert reg.resolve(None).alias == "alpha"


def test_unknown_alias_never_falls_back_to_default(tmp_path):
    reg = parse_registry(_reg(tmp_path), tmp_path / "r.json")
    with pytest.raises(AccountError) as e:
        reg.resolve("gamma")
    assert e.value.code == "account_unknown"
    with pytest.raises(AccountError):
        reg.resolve(123)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(version=2),
        lambda d: d.update(extra=1),
        lambda d: d.update(default="nobody"),
        lambda d: d.update(accounts=[]),
        lambda d: d["accounts"][0].update(alias="Bad Alias"),
        lambda d: d["accounts"][0].update(unknown=True),
        lambda d: d["accounts"][1].update(alias="alpha"),
        lambda d: d["accounts"][0].update(allowed_tools="read"),
        lambda d: d["accounts"][0].update(expected_identity="me"),
        lambda d: d["accounts"][0].update(expected_identity="urn:li:x:1"),
        lambda d: d["accounts"][0].update(user_data_dir=""),
    ],
)
def test_schema_defects_refuse_the_whole_file(tmp_path, mutate):
    data = _reg(tmp_path)
    mutate(data)
    with pytest.raises(AccountError) as e:
        parse_registry(data, tmp_path / "r.json")
    assert e.value.code == "registry_invalid"


def test_same_user_data_dir_refused(tmp_path):
    data = _reg(tmp_path)
    data["accounts"][1]["user_data_dir"] = data["accounts"][0]["user_data_dir"]
    with pytest.raises(AccountError, match="share or nest"):
        parse_registry(data, tmp_path / "r.json")


def test_same_parent_refused(tmp_path):
    data = _reg(tmp_path)
    data["accounts"][1]["user_data_dir"] = str(tmp_path / "acc-alpha" / "other")
    with pytest.raises(AccountError, match="share or nest"):
        parse_registry(data, tmp_path / "r.json")


def test_nested_paths_refused(tmp_path):
    data = _reg(tmp_path)
    data["accounts"][1]["user_data_dir"] = str(
        tmp_path / "acc-alpha" / "profile" / "x" / "profile"
    )
    with pytest.raises(AccountError, match="share or nest"):
        parse_registry(data, tmp_path / "r.json")


def test_parent_of_other_auth_root_refused(tmp_path):
    data = _reg(tmp_path)
    # beta's parent is tmp_path, which contains alpha's auth root
    data["accounts"][1]["user_data_dir"] = str(tmp_path / "profile-b")
    with pytest.raises(AccountError, match="share or nest"):
        parse_registry(data, tmp_path / "r.json")


def test_home_as_parent_refused(tmp_path):
    data = _reg(tmp_path)
    data["accounts"][0]["user_data_dir"] = str(Path.home() / "profile")
    with pytest.raises(AccountError, match="home or a root"):
        parse_registry(data, tmp_path / "r.json")


def test_invalid_json_is_an_error_not_legacy_mode(tmp_path):
    path = tmp_path / "r.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(AccountError):
        accounts.load_registry(path)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_permission_warning(tmp_path):
    path = _write(tmp_path, _reg(tmp_path))
    path.chmod(0o644)
    assert accounts.load_registry(path).warnings
    path.chmod(0o600)
    assert not accounts.load_registry(path).warnings


def test_upsert_creates_and_refuses_overlap(tmp_path):
    path = tmp_path / "r.json"
    a = accounts.upsert_account(
        path,
        "alpha",
        user_data_dir=tmp_path / "a" / "profile",
        expected_identity="example-alpha",
    )
    assert a.allowed_tools == ("read",)
    with pytest.raises(AccountError):
        accounts.upsert_account(path, "beta", user_data_dir=tmp_path / "a" / "p2")
    reg = accounts.load_registry(path)
    assert set(reg.accounts) == {"alpha"}  # file untouched by the refusal


def test_rights():
    acc = accounts.Account("a", Path("/x/p"), "s", ("read", "send_message"))
    assert accounts.tool_allowed(acc, "get_feed", read_only=True)
    assert not accounts.tool_allowed(acc, "connect_with_person", read_only=False)
    assert accounts.tool_allowed(acc, "send_message", read_only=False)
    star = accounts.Account("a", Path("/x/p"), "s", ("*",))
    assert accounts.tool_allowed(star, "anything", read_only=False)


def test_identity_match():
    assert accounts.identity_matches("example-alpha", {"Example-Alpha"})
    assert not accounts.identity_matches("example-alpha", {"example-beta"})
    assert not accounts.identity_matches("example-alpha", set())
    urn = "urn:li:fsd_profile:ABC123"
    assert accounts.identity_matches(urn, {urn})
    assert not accounts.identity_matches(urn, {"urn:li:fsd_profile:abc123"})


# -- middleware with fakes -------------------------------------------------------


class FakeSwitcher:
    def __init__(self) -> None:
        self.dir: Path | None = None
        self.browser = 0
        self.generation = 0
        self.activations: list[str] = []
        self.deactivations = 0
        self.signed = {"alpha": True, "beta": True}

    def current_dir(self) -> Path:
        return self.dir  # type: ignore[return-value]

    async def activate(self, account) -> None:
        self.activations.append(account.alias)
        self.dir = account.user_data_dir
        self.browser += 1

    async def deactivate(self) -> None:
        self.deactivations += 1
        self.browser += 1

    def fingerprint(self):
        return (self.browser, self.generation)

    def signed_in(self, account) -> bool:
        return self.signed.get(account.alias, False)


class Harness:
    def __init__(self, tmp_path: Path, **kw) -> None:
        self.registry = parse_registry(_reg(tmp_path), tmp_path / "r.json")
        self.switcher = FakeSwitcher()
        # who is "signed in" in the fake browser, per profile directory
        self.signed_in_as = {
            a.user_data_dir: a.expected_identity
            for a in self.registry.accounts.values()
        }
        self.identify_calls: list[str] = []
        self.writes = {"connect_with_person", "send_message"}
        self.mw = AccountMiddleware(
            self.registry,
            switcher=self.switcher,
            identify=self._identify,
            read_only=self._read_only,
            busy_timeout=kw.get("busy_timeout", 5.0),
        )

    async def _identify(self, account) -> set[str]:
        self.identify_calls.append(account.alias)
        who = self.signed_in_as.get(self.switcher.dir)
        return {who} if who else set()

    async def _read_only(self, ctx, name) -> bool:
        return name not in self.writes

    async def call(self, name, account=None, body=None):
        args = {} if account is None else {"account": account}
        message = mt.CallToolRequestParams(name=name, arguments=args)
        ctx = MiddlewareContext(message=message)

        async def call_next(c):
            assert "account" not in (c.message.arguments or {})
            if body is not None:
                return await body()
            return ToolResult(
                content=[mt.TextContent(type="text", text=json.dumps({"x": 1}))],
                structured_content={"x": 1},
            )

        return await self.mw.on_call_tool(ctx, call_next)


@pytest.fixture
def state_root(tmp_path, monkeypatch):
    root = tmp_path / "state"
    monkeypatch.setenv(accounts.STATE_ROOT_ENV, str(root))
    return root


async def test_default_account_and_result_tag(tmp_path, state_root):
    h = Harness(tmp_path)
    res = await h.call("get_feed")
    assert res.structured_content == {"x": 1, "account": "alpha"}
    assert json.loads(res.content[0].text)["account"] == "alpha"
    assert h.switcher.activations == ["alpha"]


async def test_wrong_account_signed_in_returns_no_data(tmp_path, state_root):
    h = Harness(tmp_path)
    alpha = h.registry.accounts["alpha"].user_data_dir
    h.signed_in_as[alpha] = "someone-else"
    ran = []

    async def body():
        ran.append(1)
        return ToolResult(structured_content={"secret": 1})

    with pytest.raises(ToolError, match="account_identity_mismatch"):
        await h.call("get_feed", body=body)
    assert ran == []
    assert h.switcher.deactivations == 1
    # And the next call re-binds and checks again, still refusing.
    with pytest.raises(ToolError, match="account_identity_mismatch"):
        await h.call("get_feed", body=body)
    assert ran == []


async def test_identity_checked_once_for_reads_and_before_every_write(
    tmp_path, state_root
):
    h = Harness(tmp_path)
    await h.call("get_feed", "beta")
    await h.call("get_feed", "beta")
    assert h.identify_calls == ["beta"]
    await h.call("send_message", "beta")
    await h.call("send_message", "beta")
    assert h.identify_calls == ["beta", "beta", "beta"]


async def test_browser_restart_or_cookie_refresh_triggers_recheck(tmp_path, state_root):
    h = Harness(tmp_path)
    await h.call("get_feed")
    h.switcher.browser += 1  # restart
    await h.call("get_feed")
    h.switcher.generation += 1  # cookie refresh / new login generation
    await h.call("get_feed")
    assert h.identify_calls == ["alpha", "alpha", "alpha"]


async def test_restart_mid_call_into_other_identity_withholds_result(
    tmp_path, state_root
):
    h = Harness(tmp_path)
    alpha = h.registry.accounts["alpha"].user_data_dir

    async def body():
        h.switcher.browser += 1
        h.signed_in_as[alpha] = "someone-else"
        return ToolResult(structured_content={"secret": 1})

    with pytest.raises(ToolError, match="account_identity_mismatch"):
        await h.call("get_feed", body=body)


async def test_profile_switch_during_call_withholds_result(tmp_path, state_root):
    h = Harness(tmp_path)

    async def body():
        h.switcher.dir = h.registry.accounts["beta"].user_data_dir
        return ToolResult(structured_content={"secret": 1})

    with pytest.raises(ToolError, match="account_switched_during_call"):
        await h.call("get_feed", body=body)


async def test_write_tool_refused_without_right(tmp_path, state_root):
    h = Harness(tmp_path)
    ran = []

    async def body():
        ran.append(1)

    with pytest.raises(ToolError, match="account_tool_not_allowed"):
        await h.call("connect_with_person", "alpha", body=body)
    assert ran == [] and h.switcher.activations == []


async def test_unknown_alias_fails_closed_without_switch(tmp_path, state_root):
    h = Harness(tmp_path)
    with pytest.raises(ToolError, match="account_unknown"):
        await h.call("get_feed", "gamma")
    assert h.switcher.activations == []


async def test_expired_account_fails_closed(tmp_path, state_root):
    h = Harness(tmp_path)
    h.switcher.signed["beta"] = False
    with pytest.raises(ToolError, match="account_not_signed_in"):
        await h.call("get_feed", "beta")
    assert h.switcher.activations == []


async def test_unconfigured_identity_refused(tmp_path, state_root):
    data = _reg(tmp_path)
    data["accounts"][0]["expected_identity"] = None
    h = Harness(tmp_path)
    h.mw.registry = parse_registry(data, tmp_path / "r.json")
    with pytest.raises(ToolError, match="account_identity_unconfigured"):
        await h.call("get_feed")


async def test_parallel_calls_of_two_accounts_never_interleave(tmp_path, state_root):
    h = Harness(tmp_path)
    active: list[str] = []
    seen: list[tuple[str, Path, Path, Path | None]] = []

    def make_body(alias):
        async def body():
            active.append(alias)
            assert len(active) == 1, "two accounts inside the browser at once"
            start = h.switcher.dir
            state = account_context.current_state_dir()
            await asyncio.sleep(0.01)
            seen.append((alias, start, h.switcher.dir, state))
            active.remove(alias)
            return ToolResult(structured_content={"v": alias})

        return body

    results = await asyncio.gather(
        *[
            h.call("get_feed", alias, body=make_body(alias))
            for alias in ["alpha", "beta"] * 3
        ]
    )
    for res in results:
        assert res.structured_content["v"] == res.structured_content["account"]
    for alias, start, end, state in seen:
        assert start == end == h.registry.accounts[alias].user_data_dir
        assert state == state_root / alias


async def test_profile_busy_is_per_account(tmp_path, state_root):
    h = Harness(tmp_path, busy_timeout=0.05)
    gate = asyncio.Event()

    async def slow():
        await gate.wait()
        return ToolResult(structured_content={})

    first = asyncio.create_task(h.call("get_feed", "alpha", body=slow))
    await asyncio.sleep(0.01)
    with pytest.raises(ToolError, match="profile_busy: account 'alpha'"):
        await h.call("get_feed", "alpha")
    gate.set()
    await first


async def test_audit_line_per_call_without_content(tmp_path, state_root):
    h = Harness(tmp_path)
    await h.call("get_feed", "beta")
    with pytest.raises(ToolError):
        await h.call("connect_with_person", "alpha")
    beta = (state_root / "beta" / "audit.jsonl").read_text().splitlines()
    alpha = (state_root / "alpha" / "audit.jsonl").read_text().splitlines()
    assert [json.loads(x)["status"] for x in beta] == ["ok"]
    assert [json.loads(x)["status"] for x in alpha] == ["account_tool_not_allowed"]
    for line in beta + alpha:
        assert set(json.loads(line)) == {"ts", "alias", "tool", "status"}


async def test_ledgers_are_per_account(tmp_path, state_root):
    from linkedin_mcp_server import ext_outreach

    h = Harness(tmp_path)
    paths = {}

    async def body():
        paths[account_context.current_alias()] = ext_outreach.ledger_path()
        return ToolResult(structured_content={})

    await h.call("get_feed", "alpha", body=body)
    await h.call("get_feed", "beta", body=body)
    assert paths["alpha"].parent == state_root / "alpha"
    assert paths["beta"].parent == state_root / "beta"
    # outside a call: historical location
    assert ext_outreach.ledger_path().parent == Path.home() / ".linkedin-mcp"


async def test_list_accounts_has_no_personal_data(tmp_path, state_root):
    h = Harness(tmp_path)
    await h.call("get_feed", "beta")
    rows = h.mw.describe()
    assert {r["alias"] for r in rows} == {"alpha", "beta"}
    for r in rows:
        assert set(r) == {"alias", "default", "status", "identity_verified"}
    assert next(r for r in rows if r["alias"] == "beta")["identity_verified"]
    text = json.dumps(rows)
    assert "example" not in text and "profile" not in text


# -- server wiring ---------------------------------------------------------------


async def test_server_without_register_is_unchanged():
    from linkedin_mcp_server.server import create_mcp_server

    tools = await create_mcp_server().list_tools()
    names = {t.name for t in tools}
    assert "list_accounts" not in names
    assert all(
        "account" not in (t.parameters or {}).get("properties", {}) for t in tools
    )


async def test_server_with_register_advertises_account(tmp_path, monkeypatch):
    from linkedin_mcp_server.server import create_mcp_server

    monkeypatch.setenv(accounts.REGISTRY_ENV, str(_write(tmp_path, _reg(tmp_path))))
    tools = await create_mcp_server().list_tools()
    by = {t.name: t for t in tools}
    assert "list_accounts" in by
    assert "account" in by["get_feed"].parameters["properties"]
    assert "account" not in (by["list_accounts"].parameters or {}).get("properties", {})


def test_server_refuses_invalid_register(tmp_path, monkeypatch):
    from linkedin_mcp_server.server import create_mcp_server

    path = tmp_path / "r.json"
    path.write_text("[]", encoding="utf-8")
    monkeypatch.setenv(accounts.REGISTRY_ENV, str(path))
    with pytest.raises(AccountError):
        create_mcp_server()


# -- reset isolation ------------------------------------------------------------


def test_reset_of_one_account_leaves_the_neighbour(tmp_path):
    from linkedin_mcp_server.profile_claim import ensure_profile_claim
    from linkedin_mcp_server.session_state import clear_auth_state

    reg = parse_registry(_reg(tmp_path), tmp_path / "r.json")
    for acc in reg.accounts.values():
        acc.user_data_dir.parent.mkdir(parents=True)
        ensure_profile_claim(acc.user_data_dir)
        acc.user_data_dir.mkdir()
        (acc.user_data_dir / "Cookies").write_text("x")
        (acc.auth_root / "cookies.json").write_text("[]")
    alpha, beta = reg.accounts["alpha"], reg.accounts["beta"]
    assert clear_auth_state(alpha.user_data_dir)
    assert not alpha.user_data_dir.exists()
    assert not (alpha.auth_root / "cookies.json").exists()
    assert (beta.user_data_dir / "Cookies").read_text() == "x"
    assert (beta.auth_root / "cookies.json").exists()


# -- CLI --------------------------------------------------------------------------


def _cfg(**server):
    return SimpleNamespace(
        server=SimpleNamespace(
            account=None, login=False, expected_identity=None, **server
        ),
        browser=SimpleNamespace(user_data_dir="unchanged"),
    )


def test_cli_login_creates_account(tmp_path, monkeypatch):
    from linkedin_mcp_server.cli_main import _bind_cli_account

    path = tmp_path / "r.json"
    monkeypatch.setenv(accounts.REGISTRY_ENV, str(path))
    cfg = _cfg()
    cfg.server.account, cfg.server.login = "alpha", True
    cfg.server.expected_identity = "example-alpha"
    _bind_cli_account(cfg)
    reg = accounts.load_registry(path)
    assert reg.accounts["alpha"].expected_identity == "example-alpha"
    assert Path(cfg.browser.user_data_dir) == reg.accounts["alpha"].user_data_dir


def test_cli_logout_unknown_account_exits_without_fallback(tmp_path, monkeypatch):
    from linkedin_mcp_server.cli_main import _bind_cli_account

    monkeypatch.setenv(accounts.REGISTRY_ENV, str(_write(tmp_path, _reg(tmp_path))))
    cfg = _cfg(logout=True)
    cfg.server.account = "gamma"
    with pytest.raises(SystemExit) as e:
        _bind_cli_account(cfg)
    assert e.value.code == 1
    assert cfg.browser.user_data_dir == "unchanged"


async def test_other_account_holding_the_browser_times_out_as_busy(
    tmp_path, state_root
):
    h = Harness(tmp_path, busy_timeout=0.05)
    gate = asyncio.Event()

    async def slow():
        await gate.wait()
        return ToolResult(structured_content={})

    first = asyncio.create_task(h.call("get_feed", "alpha", body=slow))
    await asyncio.sleep(0.01)
    with pytest.raises(ToolError, match="profile_busy: another account"):
        await h.call("get_feed", "beta")
    assert h.switcher.activations == ["alpha"]
    gate.set()
    await first


async def test_audit_never_carries_tool_error_text(tmp_path, state_root):
    h = Harness(tmp_path)

    async def body():
        raise ToolError("https://example.invalid/in/someone: secret text")

    with pytest.raises(ToolError):
        await h.call("get_feed", "beta", body=body)
    line = (state_root / "beta" / "audit.jsonl").read_text()
    assert "example.invalid" not in line and '"tool_error"' in line
