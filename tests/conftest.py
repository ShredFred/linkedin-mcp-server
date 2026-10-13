import os
import sys

# FastMCP 4 bridges camelCase reads on MCP SDK models (`result.isError`) with
# deprecation shims that this switch turns off. The suite runs with them off,
# so a read the SDK v2 rename left behind fails here instead of passing on a
# shim. Set before anything imports fastmcp, which reads its settings once at
# import, and inherited by every process a test starts. CI sets it before
# Python starts as well; `test_fastmcp_compatibility.py` checks it took effect.
os.environ["FASTMCP_MCP_CAMELCASE_COMPAT"] = "false"
# Deployment values the fork reads from the environment (never built in).
# The canary is read at import, the booking account per call (see
# fork_deployment_values); both are fixed here, never the developer's own.
FORK_DEPLOYMENT = {
    "LINKEDIN_MCP_CANARY": "johnsmith",
    "LINKEDIN_MCP_CALENDLY_ACCOUNT": "acme_jane-doe",
}
os.environ.update(FORK_DEPLOYMENT)

import pytest  # noqa: E402

# The differential accounting is a plugin rather than a directory conftest so
# that it is loaded wherever those cases run, the xdist controller included,
# and counts cases no fixture ever set up.
pytest_plugins = ("linkedin.support.navigation", "differential.accounting")


# --------------------------------------------------------------------------- #
# Temporary directories and the developer's real home
# --------------------------------------------------------------------------- #

#: The account's real home, read before any test can redirect it.
_REAL_HOME = os.path.expanduser("~")

#: What a test run must never leave in the real home. The installer root falls
#: back to the home when %TEMP% is refused, and daemon state lives there; a
#: test that reaches either path writes into the developer's account.
_REAL_HOME_PATTERNS = ("linkedin-mcp-installer-*", ".mcp-server-linkedin*")

#: Per-run temporary roots under the checkout, newest kept, the rest pruned.
#: Deliberately short: daemon state nests a 64-character hash several levels
#: below a test's directory, and a longer root pushed those paths past the
#: Windows MAX_PATH of 260 (WinError 3 on os.replace).
_RUN_TMP_ROOT = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".pt")
_RUN_TMP_KEEP = 3


def _real_home_entries() -> set[str]:
    import glob

    found: set[str] = set()
    for pattern in _REAL_HOME_PATTERNS:
        found.update(glob.glob(os.path.join(_REAL_HOME, pattern)))
    return found


def _prune_old_runs() -> None:
    import shutil

    try:
        runs = sorted(
            (entry for entry in os.scandir(_RUN_TMP_ROOT) if entry.is_dir()),
            key=lambda entry: entry.stat().st_mtime,
        )
    except FileNotFoundError:
        return
    for entry in runs[: max(len(runs) - (_RUN_TMP_KEEP - 1), 0)]:
        shutil.rmtree(entry.path, ignore_errors=True)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    """Give every run its own basetemp under the checkout, unless one is named.

    pytest's default keeps numbered runs under %TEMP%\\pytest-of-<user> behind a
    ``pytest-current`` link; on Windows that link can become unreadable
    (WinError 5) and then every later run fails at session end. A unique
    directory per run needs no link, and old runs are pruned to the newest few.
    Workers inherit the controller's choice.
    """
    if hasattr(config, "workerinput") or config.option.basetemp:
        return
    import secrets

    _prune_old_runs()
    os.makedirs(_RUN_TMP_ROOT, exist_ok=True)
    config.option.basetemp = os.path.join(_RUN_TMP_ROOT, secrets.token_hex(3))


def pytest_sessionstart(session):
    if not hasattr(session.config, "workerinput"):
        session.config._real_home_before = _real_home_entries()


def pytest_sessionfinish(session, exitstatus):
    """Fail the run if it left anything new in the developer's real home."""
    before = getattr(session.config, "_real_home_before", None)
    if before is None:
        return
    new = sorted(_real_home_entries() - before)
    if new:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        message = "this run left new entries in the real home directory: " + ", ".join(
            new
        )
        if reporter is not None:
            reporter.write_line(message, red=True)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(scope="session")
def _stand_in_homes(tmp_path_factory):
    import itertools

    return tmp_path_factory.mktemp("homes"), itertools.count()


@pytest.fixture(autouse=True)
def keep_out_of_the_real_home(request, _stand_in_homes, monkeypatch):
    """Point ``Path.home()`` and ``~`` at a fresh stand-in home for every test.

    One per test, not one per session: a shared one carried ``~/.linkedin-mcp``
    from one test into the next. A counter under one session directory costs a
    single ``mkdir``, cheaper than a numbered ``tmp_path`` for every test.

    ``HOME`` and ``USERPROFILE`` are what ``expanduser`` and ``Path.home`` read,
    and the installer's home fallback went through them into the real account.
    ``APPDATA``/``LOCALAPPDATA`` stay as they are (see
    ``ignore_the_developers_environment``): they hold the installed browser the
    browser tests launch. Daemon state keys off the account home, which
    ``daemon_descriptor._account_home`` reads from the account rather than the
    environment; tests that need the real one take ``real_state_root``, which
    opts out here, and the session guard above catches anything else.
    """
    if "real_state_root" in request.fixturenames:
        return
    root, counter = _stand_in_homes
    home = root / str(next(counter))
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


@pytest.fixture(autouse=True)
def _no_temp_pace_raises(monkeypatch):
    """Tests measure against PACE_BUDGETS; a dated operator raise would shift them by calendar day."""
    from linkedin_mcp_server import ext_outreach

    monkeypatch.setattr(ext_outreach, "PACE_TEMP_RAISES", {})


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset global state for test isolation."""
    from linkedin_mcp_server.bootstrap import reset_bootstrap_for_testing
    from linkedin_mcp_server.config import reset_config
    from linkedin_mcp_server.daemon_descriptor import (
        reset_daemon_descriptor_for_testing,
    )
    from linkedin_mcp_server.daemon_liveness import reset_liveness_for_testing
    from linkedin_mcp_server.debug_trace import reset_trace_state_for_testing
    from linkedin_mcp_server.logging_config import teardown_trace_logging
    from linkedin_mcp_server.drivers.browser import reset_browser_for_testing
    from linkedin_mcp_server.profile_lease import reset_leases_for_testing
    from linkedin_mcp_server.server_role import reset_process_role_for_testing

    reset_bootstrap_for_testing()
    reset_daemon_descriptor_for_testing()
    # The owner's call tracker is process state like the rest. Left standing, a
    # call one test watched is still in flight for the next, which reads as an
    # owner that is never idle, and the moment of one test's last expiry scan
    # becomes the baseline for the next, where a gap of seconds reads as an
    # owner that was not running.
    reset_liveness_for_testing()
    reset_browser_for_testing()
    reset_leases_for_testing()
    reset_config()
    # Every test that builds a server records a role, and the auth gates read it
    # from process state. Left standing, one OWNER would refuse logins in every
    # test after it, in a suite where most never mention a role.
    reset_process_role_for_testing()
    # Rate-limit hooks (#957) are process state; registering the ext tools
    # installs them, and left standing they would read and write the real
    # pacer ledger from every later navigation test.
    from linkedin_mcp_server.core.rate_limit_hooks import set_rate_limit_hooks

    set_rate_limit_hooks(None, None)
    # The trace directory is derived from the profile root and cached, so a
    # test pointing USER_DATA_DIR at its tmp_path otherwise leaves every later
    # test on this worker writing into a directory pytest has removed. The log
    # handler holds the same path independently of that cache and has to be
    # unbound with it, or records keep landing in the previous test's
    # directory while a fresh one is resolved. `keep_traces` because removing
    # a directory is a test's own business, not this fixture's.
    teardown_trace_logging(keep_traces=True)
    reset_trace_state_for_testing()
    yield
    reset_bootstrap_for_testing()
    reset_daemon_descriptor_for_testing()
    reset_browser_for_testing()
    # After the browser, so a lease the browser still held is released by its
    # own bookkeeping first rather than yanked out from under it.
    reset_leases_for_testing()
    reset_config()
    reset_process_role_for_testing()
    reset_liveness_for_testing()
    teardown_trace_logging(keep_traces=True)
    reset_trace_state_for_testing()


#: The browser cache the run was started with, read before any test can write
#: the variable.
_INHERITED_BROWSERS_PATH = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")


@pytest.fixture(autouse=True)
def installed_browser_for_dom_tests(request, reset_singletons, monkeypatch):
    """Give ``browser_dom`` tests the browser cache the run was started with.

    ``reset_bootstrap_for_testing`` deletes ``PLAYWRIGHT_BROWSERS_PATH`` so that
    no bootstrap test measures, reports on or installs into a real cache. That
    also hid a browser installed outside patchright's default cache, such as the
    server's own under ``~/.linkedin-mcp/patchright-browsers``, and every DOM
    test skipped. These tests only launch Chromium against synthetic markup, so
    they get the caller's cache back and nothing else does.
    """
    if _INHERITED_BROWSERS_PATH and request.node.get_closest_marker("browser_dom"):
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", _INHERITED_BROWSERS_PATH)


@pytest.fixture(autouse=True)
def ignore_the_developers_environment(monkeypatch):
    """Run against an empty environment rather than whatever the shell holds.

    The configuration loader calls ``load_dotenv()`` at import, so a local
    ``.env`` is part of the environment too. A developer running a real proxy
    then fails a test that sets only ``PROXY_SERVER``, because the loader sees
    that server *and* the ambient credentials and refuses the pair. That failure
    says nothing about the code, reproduces on no other machine, and is
    invisible in CI, which is the worst combination a test can have.

    Two sources rather than a list kept here, because a hand-written list is the
    same bug one setting along: it has to be remembered whenever a setting is
    added, and forgetting it produces a test that passes or fails only on the
    machine that happens to define it.

    * every key the loader reads, from the loader's own table;
    * every variable this project names after itself. Those are read directly
      through ``os.environ`` all over the package — debug switches, the trace
      mode, the container hint, the update check — and none of them go through
      the table above. Measured: with ``LINKEDIN_DEBUG_BRIDGE_COOKIE_SET`` set,
      a cookie-import test fails, and a run with every one of them set failed 22
      tests.

    What is deliberately left alone is everything this project did not name:
    ``CI``, which pytest and the workflow both mean something by,
    ``PYTEST_CURRENT_TEST``, which pytest owns, ``PLAYWRIGHT_BROWSERS_PATH``,
    which points at an installed browser, and the platform's own
    ``LOCALAPPDATA``, ``APPDATA`` and ``XDG_CONFIG_HOME``. Clearing those would
    not be isolation, it would be pretending to run somewhere else.
    """
    import os

    from linkedin_mcp_server.config.loaders import EnvironmentKeys

    from_the_table = [
        value
        for name, value in vars(EnvironmentKeys).items()
        if not name.startswith("_") and isinstance(value, str)
    ]
    ours = [key for key in os.environ if key.startswith("LINKEDIN")]
    for key in (*from_the_table, *ours):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def fork_deployment_values(ignore_the_developers_environment, monkeypatch):
    """Put the fork's deployment values back after the LINKEDIN* sweep."""
    for key, value in FORK_DEPLOYMENT.items():
        monkeypatch.setenv(key, value)


@pytest.fixture(autouse=True)
def isolate_profile_dir(ignore_the_developers_environment, tmp_path, monkeypatch):
    """Redirect profile directory to tmp_path via config and DEFAULT_PROFILE_DIR.

    Takes the clearing fixture as an argument rather than trusting the order two
    autouse fixtures happen to run in. Pytest orders by scope, dependency and
    autouse, and explicitly not by where or in what order a fixture was defined;
    the order these two run in today comes from ``dir()`` being alphabetical,
    which is a coincidence a rename would end. Reversed, the clearing would
    delete the ``USER_DATA_DIR`` this sets, and tests would fall back to the
    real profile at ``~/.linkedin-mcp/profile`` — including anything that spawns
    a subprocess, which the in-process patches below do not reach.
    """
    fake_profile = tmp_path / "profile"
    monkeypatch.setenv("USER_DATA_DIR", str(fake_profile))

    # Patch DEFAULT_PROFILE_DIR for any code still referencing the constant
    for module in [
        "linkedin_mcp_server.drivers.browser",
        "linkedin_mcp_server.authentication",
        "linkedin_mcp_server.cli_main",
        "linkedin_mcp_server.setup",
        "linkedin_mcp_server.session_state",
    ]:
        try:
            monkeypatch.setattr(f"{module}.DEFAULT_PROFILE_DIR", fake_profile)
        except AttributeError:
            pass  # Module may not be imported yet

    # Patch get_profile_dir() in all modules that import it
    for gp_module in [
        "linkedin_mcp_server.drivers.browser",
        "linkedin_mcp_server.authentication",
        "linkedin_mcp_server.cli_main",
        "linkedin_mcp_server.setup",
    ]:
        try:
            monkeypatch.setattr(f"{gp_module}.get_profile_dir", lambda: fake_profile)
        except AttributeError:
            pass

    try:
        monkeypatch.setattr(
            "linkedin_mcp_server.session_state.get_source_profile_dir",
            lambda: fake_profile,
        )
    except AttributeError:
        pass

    for source_module in [
        "linkedin_mcp_server.authentication",
        "linkedin_mcp_server.drivers.browser",
        "linkedin_mcp_server.debug_trace",
        "linkedin_mcp_server.error_diagnostics",
    ]:
        try:
            monkeypatch.setattr(
                f"{source_module}.get_source_profile_dir",
                lambda: fake_profile,
            )
        except AttributeError:
            pass

    # Claim it, the same way `main()` does on the first run against a fresh
    # custom root. Without this every test that rotates, restores, resets or
    # clears would be refused: tmp_path is not the canonical default, and the
    # guard exists precisely to refuse roots nobody claimed. Tests that prove
    # the refusal use a *different* directory, which stays unclaimed.
    from linkedin_mcp_server.profile_claim import ensure_profile_claim

    ensure_profile_claim(fake_profile)

    return fake_profile


@pytest.fixture
def profile_dir(isolate_profile_dir):
    """Create a non-empty profile directory."""
    isolate_profile_dir.mkdir(parents=True, exist_ok=True)
    # Create a marker file so profile_exists() returns True
    (isolate_profile_dir / "Default" / "Cookies").parent.mkdir(
        parents=True, exist_ok=True
    )
    (isolate_profile_dir / "Default" / "Cookies").write_text("placeholder")
    return isolate_profile_dir


@pytest.fixture
def mock_context():
    """Mock FastMCP Context."""
    from unittest.mock import AsyncMock, MagicMock

    ctx = MagicMock()
    ctx.report_progress = AsyncMock()
    return ctx


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Fail a test that leaves ``sys.stdout`` or ``sys.stderr`` unusable.

    A test that closes one of them breaks every test that runs after it in the
    same process, and the traceback lands on the innocent one. The failure
    reads as unrelated: ``uvicorn`` asks ``sys.stdout.isatty()`` while building
    its log config, so a closed stream surfaces as ``Unable to configure
    formatter 'default'`` in a daemon test.

    Under pytest's own capturing every test gets a fresh ``sys.stdout``, which
    repairs the damage before anything can trip over it. CI runs with ``-s``,
    where nothing repairs it, so this class of defect passes locally and fails
    there. The usual cause is ``capsys`` in the signature of a test that also
    monkeypatches ``sys.stdout``: ``capsys`` installs the object that
    ``monkeypatch`` then records as the one to restore, tears down first, and
    closes it, so the undo reinstalls a closed stream.

    Checked on the teardown report rather than in a fixture, because a fixture
    cannot see what the teardown of another fixture did after it, and reported
    against the test that caused it rather than raised from a hook, which would
    abort the whole session as an internal error.
    """
    report = yield
    if call.when != "teardown" or report.get_result().failed:
        return
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        try:
            stream.write("")
            stream.flush()
        except Exception as exc:  # noqa: BLE001 - any failure is the same defect
            result = report.get_result()
            result.outcome = "failed"
            result.longrepr = (
                f"{item.nodeid} left sys.{name} unusable "
                f"({type(exc).__name__}: {exc}). Every later test in this "
                f"process would print into it. A `capsys` argument on a test "
                f"that also monkeypatches sys.{name} is the usual cause."
            )
            return
