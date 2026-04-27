# Agent Git Operations -- Requirements

The git operations layer handles all interactions with git repositories on behalf of the agent loop. The agent itself never runs git commands directly -- all git operations are mediated by the framework to ensure consistency, safety, and structured commit messages.

---

## R-GIT-01: Auto-commit after verified cycles

When auto-commit is enabled and all gating hooks pass, the framework must stage the cycle's changed files and commit them. The commit must happen only after verification (hooks) and evaluation (scoring), so that commit messages can include quality metadata.

**Why:** The agent produces incremental changes every cycle. Without auto-commit, changes accumulate as unstaged modifications that can be lost on process restart, and there is no history to diff, revert, or review. Gating on hooks ensures that only verified code enters the git history.

**Acceptance criteria:**
- After a cycle where hooks pass and files were changed, `git add -- <changed files>` and `git commit` succeed in each configured repository.
- After a cycle where hooks fail, no commit is created. The changes remain in the working tree for the next cycle to fix.
- When no files were changed in a cycle, the staging step reports success (vacuously) and the commit proceeds (as an `--allow-empty` commit with cycle metadata).
- Staging or commit failures are logged but do not crash the agent loop. The cycle continues to the Persist and Control phases.

---

## R-GIT-02: Multi-repository support

The framework must support committing to multiple repositories per cycle. This covers scenarios where the agent modifies both a primary codebase and a supporting repository (e.g., a shared library).

**Why:** Some project configurations have the target codebase and harness output in different git repositories, or the agent operates on a monorepo with multiple nested git roots. Each repository needs its own stage-commit sequence.

**Acceptance criteria:**
- The `commit_repos` configuration accepts a list of paths (absolute or relative to workspace).
- Each path is resolved at startup. Invalid paths (non-existent directories) are logged as warnings and excluded.
- Stage and commit operations run independently per repository. A failure in one repository does not prevent operations in others.

---

## R-GIT-03: Structured commit messages

Every commit message must follow a consistent format that includes the cycle number, a one-line summary, and structured metadata lines for metrics, evaluation scores, and hook status.

**Why:** Commit messages are the primary audit trail. Operators read them in `git log`, CI scripts parse them, and the meta-review system analyzes them. A consistent format makes all of these reliable. Without structure, commit messages would be whatever the LLM happened to say, which varies wildly in quality and format.

**Acceptance criteria:**
- The commit message title starts with `[harness] agent: cycle N` followed by a summary derived from the agent's output.
- When the agent's output is truncated (tool loop cut off), the summary falls back to a diff-based description (file names and diff stat).
- The commit body includes `metrics:`, `eval:`, and `hooks:` lines when the corresponding data is available.
- The title is capped at a reasonable length to avoid line-wrapping issues in git tooling.

---

## R-GIT-04: Selective file staging

The framework must stage only the files that were actually changed by the current cycle's tool calls, not blindly `git add -A`. Changed files are determined from the tool execution log.

**Why:** `git add -A` would stage unrelated changes that happened to be in the working tree -- operator scratch files, other processes' output, or files from a previous failed cycle. Selective staging ensures each commit contains exactly the changes the agent made in that cycle.

**Acceptance criteria:**
- Only paths that appear in the tool execution log as write/edit targets are staged.
- Paths are passed to `git add -- <paths>`, not `git add -A` or `git add .`.
- If the tool log contains paths that don't exist on disk (deleted files), git handles them correctly via `git add --`.

---

## R-GIT-05: Auto-push to remote

When auto-push is enabled, the framework must push the committed branch to a configured remote after each successful commit. The remote name and branch are configurable.

**Why:** In production deployments, the agent runs on a remote server and the operator monitors progress by pulling from the remote. Without auto-push, the operator must SSH into the server to see the agent's work. Auto-push also enables CI/CD pipelines to trigger on each commit.

**Acceptance criteria:**
- After a successful commit, `git push <remote> <branch>` runs in each configured repository.
- Push failures are logged as warnings but do not crash the loop or prevent subsequent cycles.
- Auto-push is off by default (safe for local/sandboxed development).

---

## R-GIT-06: Periodic cycle tagging

At configurable intervals (every N committed cycles), the framework must create a git tag on the current HEAD with a structured name that includes the cycle number and short SHA. Tags can optionally be pushed to the remote.

**Why:** Tags provide stable reference points in the git history. They enable deploy workflows (CI triggers on tag push), milestone tracking (tag every 10 cycles), and easy rollback (`git reset --hard harness-r-50-a3f5d2c`). The structured name makes tags sortable and identifiable.

**Acceptance criteria:**
- Tags are created when `(cycle_number) % interval == 0`. Setting the interval to 0 disables tagging entirely.
- The tag name follows the format `<prefix>-<cycle_number>-<short_sha>`, e.g., `harness-r-10-a3f5d2c`.
- Tags are created with `-f` (force) to handle re-runs that might produce the same tag name.
- When tag push is enabled, `git push <remote> <tag_name>` runs after tag creation.
- Tag creation or push failures are logged but do not crash the loop.

---

## R-GIT-07: Diff and history queries

The framework must be able to query git for staged diffs, HEAD hashes, commit history, and diff stats. These queries serve evaluation (staged diff as evaluator input), meta-review (git log + diffstat for trend analysis), and commit message construction.

**Why:** Multiple parts of the framework need git data: the evaluator needs the diff to score, the meta-review needs the history to analyze trends, and the commit message builder needs diffstat for fallback summaries. Centralizing these queries avoids duplicated subprocess management and ensures consistent truncation (large diffs are capped to prevent memory issues).

**Acceptance criteria:**
- Staged diff output is truncated to a configurable maximum (e.g., 30K characters) to prevent memory exhaustion on large changes.
- HEAD hash queries return a short hash (10 characters) for use in tag names and review baselines.
- History queries support a `since_hash..HEAD` range for delta reporting.
- All git queries handle errors gracefully (return empty strings or empty lists) rather than raising exceptions.

---

## R-GIT-08: Squash safety

The smart squash operation (grouping and combining related commits) must be atomic: if the rebase fails at any point, the repository must be left in exactly the state it was in before the squash attempt. No partial squash states should be possible.

**Why:** Squash uses `git rebase -i`, which can fail due to merge conflicts, invalid todo sequences, or unexpected repository states. A half-completed rebase leaves the repository in a detached state that the agent cannot recover from. Atomicity ensures the worst case is "squash didn't happen" rather than "repository is broken".

**Acceptance criteria:**
- A failed rebase is followed by `git rebase --abort`.
- `git rebase --abort` is attempted even when the rebase process throws an unexpected exception (cleanup in finally block).
- Temporary files used by the rebase (todo script, editor script) are cleaned up in all cases (success, failure, exception).
- After a failed squash, the HEAD hash is unchanged from before the attempt.

---

## R-GIT-09: Squash group validation

Before executing a squash, the framework must validate that the LLM-proposed commit groups are contiguous, cover all commits in the range, and use valid SHA references. Invalid groupings must be rejected without attempting the rebase.

**Why:** The squash groups come from an LLM, which may produce invalid JSON, reference non-existent SHAs, propose non-contiguous groups (which would reorder commits), or omit some commits. Executing a rebase with bad groups would corrupt the history.

**Acceptance criteria:**
- Groups must be ordered oldest-first, and the flattened SHA order must match the actual commit order.
- All SHAs in the groups must match (by prefix) actual commits in the range.
- Short SHAs from the LLM are normalized to full hashes before execution.
- Groups missing required fields (`shas`, `message`) are rejected.
- Single-commit groups are valid but do not trigger actual squashing (no rebase needed if all groups are singletons).
- Commit messages in groups that don't start with the expected prefix are automatically prefixed.
