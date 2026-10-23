# Updating this fork

Keep the upstream attribution and Apache license. The fork's README excludes
commercial sponsor panels; `scripts/fork_readme.py` removes the known upstream
panels and `--check` rejects a stale generated README. Technical installation,
proxy and authentication documentation stays available.

1. Record the current branch, dirty paths and their hashes. Use a separate Git
   worktree based on the fork's current `origin/main`; never reset a checkout
   containing someone else's work or move its browser/session directories.
2. Fetch `main` from `https://github.com/stickerdaniel/linkedin-mcp-server.git`
   and record its full commit ID. Inspect the incoming commits, then merge that
   exact commit in the isolated worktree. Keep the upstream parent in the Git
   history so future comparisons distinguish new upstream work from old work.
3. Resolve structural moves by porting the fork's current behavior to the new
   owner, rather than resurrecting a removed module. Preserve the guarded write
   tools, duplicate/unknown-outcome ledger, account pacer, navigation refusal
   checks and device-local authentication.
4. Install with `uv sync --frozen --group dev`. Regenerate the references:

   ```bash
   uv run python scripts/fork_readme.py
   uv run python scripts/generate_linkedin_architecture.py
   uv run python scripts/fork_readme.py --check
   uv run python scripts/generate_linkedin_architecture.py --check
   uv run python scripts/check_policy_traces.py --check
   ```

5. Review the generated diff, run lint/type checks and relevant tests, and
   compare any pre-existing baseline failures explicitly. Browser tests use
   synthetic pages and isolated test profiles. Live checks remain bounded and
   read-only unless the user separately authorizes an external write.
6. Obtain the repository's required review, publish the integration branch and
   merge only after checking the evidence. Reconcile the active checkout by
   fast-forwarding while retaining unrelated local edits. Use the fork Git
   source or canonical locked wrapper; a bare PyPI `@latest` installs upstream
   without the fork's guarded extensions.

Completion means the upstream commit is reachable in the fork history, the
required features and guards are tested, the README is current and free of
known promotion blocks, and the active checkout uses the integrated version.


The upstream release workflow is bound to its original publisher repository.
A version bump imported into this fork does not publish to upstream's PyPI or
Docker namespace. Fork publication needs a separate, explicitly configured
release identity; ordinary MCP updates use the source checkout and locked wrapper.


## Current upstream integration

Checked 2026-10-23: upstream v4.28.0, exact commit
`7a40310c6beb77e7e2cea8c649f3411f96b65f94`. This integration includes five
upstream commits after the previous sync: authentication-challenge classification,
virtualenv/OpenTelemetry updates, attribution-check tooling and release metadata.
Authentication challenges require the normal login flow; a cooldown is not a fix.
The fork's independent release restriction and guarded extensions remain in place.

Use a merge commit for upstream synchronization so that exact upstream commit
remains an ancestor of the fork's main branch. Squashing the upstream merge would
make GitHub continue to report already-integrated commits as missing. Ordinary
feature/fix PRs retain the repository's squash workflow.
