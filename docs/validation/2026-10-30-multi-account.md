# Multi-account validation, 2026-10-30

The full baseline failed. Its fourteen failures were reproduced and repaired;
all of them passed in the final focused run. The repaired branch has **not**
received another complete test-suite run.

## Measured revisions and results

Platform: Windows, CPython 3.13.12, frozen `uv.lock`. Runs used one pytest
worker. The original checkout's uncommitted work was not used or modified.

| Run | Source revision | Result | Pytest duration |
|---|---|---|---|
| Full offline baseline | `9f39d2a716c303e1548e9eb9290dd7f9251e78c6` (`feat/multi-account`) | 10,413 passed, 14 failed, 625 skipped; 3 Docker cases deselected | 3,499.40 s |
| Final focused verification | `98db45d487a585ca4a3c0d776f87c074cc60de2e` | 575 passed, 19 platform skips, zero failures or errors | 87.63 s |

The full baseline selected 11,052 of 11,055 collected cases. Its process wall
time was 3,508.342 s. The focused verification selected 594 cases across the
thirteen modules listed below; its process wall time was 89.291 s.

## Findings and repairs

1. **Windows workflow-test transport:** all fourteen baseline failures came
   from the label-workflow harness in `tests/test_pr_title.py`: eight valid-title
   cases and six invalid-title cases. Windows selected the System32 WSL Bash
   launcher, and passing a multiline script through `bash -c` corrupted quotes
   in a Python heredoc. The harness now selects native Git Bash on Windows,
   feeds UTF-8 bytes through stdin, and uses the release-workflow harness's
   `python3` shim pattern with the current test interpreter. Byte input avoids
   CRLF translation; explicit UTF-8 preserves invisible-character cases.
   All 135 PR-title cases ran and passed. No failing case was removed or skipped.
2. **Cache deletion lacked an ownership boundary:** a synthetic reproduction
   showed the old helper deleting a `notes.CHROME_DELETE` directory in a root
   that `require_profile_claim` refused. The helper now uses the existing
   `session_state._owned` guard, restricts targets to the owned source or its
   standard derived runtime profile, keeps reparse entries, and refuses a
   profile replaced during the operation. The browser driver passes source
   authority separately from the runtime target. An unowned direct
   `BrowserManager` call skips optional cache recovery with a warning.
3. **Static validation:** eight type diagnostics in the account test doubles
   and formatting errors in two cleanup files were repaired.

The final focused run includes real synthetic Chromium identity/containment
tests and real Windows-junction preservation tests. Three mutations made in
external copies—removing ownership, target containment, or directory-identity
checks—each made its regression test fail. Their positive control passed all
three cases. The working source was not changed by those mutations.

Ruff lint, Ruff formatting (459 Python files), `ty`, the generated fork README
check, the generated architecture check, and `git diff --check` passed after
the final code edits. The complete `pre-commit run --all-files` also passed,
including policy traces and file hygiene (21.672 s). Generation checks ran without deployment-specific values;
their warnings do not describe the configuration of any running client.

## Reproduction

Use isolated checkouts at the revisions above and an installed Patchright
browser cache. No signed-in profile is needed.

```powershell
uv sync --frozen
uv run --frozen pytest tests -m "not image_build" -q -o addopts= --strict-markers -ra
```

Final focused selection:

```text
tests/test_pr_title.py
tests/test_profile_cleanup.py
tests/test_accounts.py
tests/test_browser_driver.py
tests/test_core_browser.py
tests/test_profile_claim.py
tests/test_session_state.py
tests/test_browser_security.py
tests/test_browser_downgrade.py
tests/test_browser_launch.py
tests/test_browser_containment.py
tests/test_browser_identity.py
tests/test_core_auth_browser_contract.py
```

The long baseline progressed through synthetic DOM, architecture regeneration,
server-process and Windows guardian/crash probes. These were local fixtures,
not LinkedIn requests.

## Evidence and remaining limits

Raw logs, JUnit XML, source hashes, mutation results and timings remain outside
Git under `%LOCALAPPDATA%/LinkedInMcpValidation/2026-10-30/`:

- `run-original.json`, `pytest-original.log`, `junit-original.xml`
- `baseline-failures.json`, `pr-title-repro.log`
- `run-final-focused.json`, `pytest-final-focused.log`, `junit-final-focused.xml`
- `static-postfix.json`, `cleanup-mutation-evidence.json`
- `pre-commit-final.json`, `pre-commit-final.log`

The three Docker image-build integration cases were excluded. Standard
platform and opt-in differential-CI skips remain explicit in JUnit. Linux,
macOS and a full rerun of the repaired head were not measured here. Live
account identity/selection and authenticated LinkedIn behavior remain
unverified. No running client was reconfigured or restarted.
