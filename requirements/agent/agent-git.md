# Agent Git Operations

> Git operations for the agent loop -- stage, commit, push, tag, diff queries

Source: `harness/agent/agent_git.py` (350 lines)

## Overview

All git interactions live here so that `agent_loop.py` contains only orchestration logic. Every public function is `async` and accepts resolved paths (no config objects). All git commands execute via `asyncio.create_subprocess_exec`. Functions handle errors internally -- they log warnings and return failure indicators rather than raising.

## Path Helpers

### `resolve_repo_paths(workspace, commit_repos) -> list[Path]`

Turns `commit_repos` entries into absolute `Path` objects.

- `workspace: str | Path` -- base workspace directory
- `commit_repos: list[str]` -- repo entries from `AgentConfig`
- For each entry: if absolute, use as-is; if relative, join with workspace
- Only includes entries where the resolved path is an existing directory (`p.is_dir()`)
- Logs warning for entries that don't exist
- Called once during `AgentLoop.__init__`

### `_primary_repo(repo_paths, workspace) -> Path`

Returns the first repo path, or falls back to workspace. Internal helper.

## Diff / Hash Queries

### `get_staged_diff(repo_path) -> str`

Returns `git diff --cached` (staged changes).

- Truncates output to 30,000 chars with `"\n\n... (diff truncated at 30k chars)"` suffix
- Logs debug when truncation occurs
- Returns `""` on any exception

Used by the agent loop to feed the evaluator before committing.

### `get_head_hash(repo_path) -> str`

Returns short HEAD hash (10 chars) via `git rev-parse --short=10 HEAD`.

- Returns `""` on failure (non-zero return code or exception)

### `get_review_git_delta(repo_path, since_hash) -> str`

Returns git log + diff stat since `since_hash` for meta-review input.

Runs two commands:
1. `git log --oneline {since_hash}..HEAD` -- output wrapped in `### Commits` with code block, truncated to 3000 chars
2. `git diff --stat {since_hash}..HEAD` -- output wrapped in `### File Stats` with code block, truncated to 3000 chars

Parts joined with `"\n\n"`. Returns `"(no git delta available)"` if both parts are empty. Returns `"(git delta unavailable: <exc>)"` on exception.

### `diff_summary(workspace, changed_paths) -> str`

Generates a one-line commit summary from `git diff --cached --stat`.

- Extracts the last line of stat output (the summary line)
- Takes first 5 file names from `changed_paths`, with `+N more` suffix if more exist
- Combines as `"file1.py, file2.py (3 files changed, 42 insertions(+), 10 deletions(-))"`, truncated to 80 chars
- Falls back to `"N file(s) changed"` on failure

## Stage / Commit

### `stage_changes(repo_paths, changed_paths) -> bool`

Runs `git add -- <paths>` in each repo.

- If `changed_paths` is empty: returns `True` immediately (nothing to stage)
- Passes all changed paths as arguments to a single `git add` command per repo
- Returns `True` only if all repos staged successfully
- Logs warning on failure with stderr output (truncated to 200 chars)

### `commit_staged(repo_paths, cycle, commit_msg) -> bool`

Commits already-staged changes in each repo.

- Command: `git commit --allow-empty -m <commit_msg>`
- Uses `--allow-empty` flag -- commits even if nothing is staged
- Returns `True` only if all repos committed successfully
- Logs info on success, warning on failure with stderr (truncated to 200 chars)

### `build_commit_message(cycle, agent_text, changed_paths, workspace, *, metrics_line="", eval_line="", hooks_line="") -> str`

Builds a structured commit message.

**Title line:**
- Format: `[harness] agent: cycle N — <summary>` (em dash U+2014)
- Summary: first line of agent text, truncated to 80 chars
- If summary contains `"unknown (tool loop was cut off)"`: falls back to `diff_summary()` result

**Body** (separated by blank line from title):
- `metrics: <metrics_line>` (if provided)
- `eval: <eval_line>` (if provided)
- `hooks: <hooks_line>` (if provided)

Example:
```
[harness] agent: cycle 5 -- fix calendar handler

metrics: tools=23 success=85% files=3 elapsed=45s
eval: basic=7.2 diffusion=6.8 combined=7.0
hooks: all passed
```

## Push / Tag

### `push_head(repo_paths, remote, branch, cycle) -> bool`

Runs `git push <remote> <branch>` from each repo.

- Returns `True` only if all repos pushed successfully
- Logs info on success, warning on failure with stderr (truncated to 200 chars)

### `tag_cycle(repo_paths, cycle, interval, prefix, push_remote, push_tag) -> None`

Creates and optionally pushes a tag after a cycle.

**Guard condition:** Returns immediately if `interval <= 0` or `(cycle + 1) % interval != 0`.

**Per repo:**
1. Gets short SHA (7 chars) via `git rev-parse --short=7 HEAD`
2. Constructs tag name: `<prefix>-<cycle+1>-<short_sha>` (e.g., `harness-r-10-a3f5d2c`)
3. Creates tag with `git tag -f <tag_name>` (force-creates, overwrites existing)
4. Logs info on success
5. If `push_tag` is True: runs `git push <push_remote> <tag_name>`
6. Logs info/warning for push success/failure

All exceptions are caught and logged per-repo -- one repo's failure does not prevent processing the next.

## Integration with Agent Loop

The agent loop calls git functions in this order per cycle:

1. `stage_changes(repo_paths, changed_paths)` -- Phase 3, only if `auto_commit=True` and no hook failures
2. `get_staged_diff(primary_repo)` -- Phase 4b, to get eval input (only if staged and changed_paths)
3. `build_commit_message(cycle, text, changed_paths, workspace, ...)` -- Phase 5, if staged
4. `commit_staged(repo_paths, cycle, commit_msg)` -- Phase 5, if staged
5. `push_head(repo_paths, remote, branch, cycle)` -- Phase 5, if committed and `auto_push=True`
6. `tag_cycle(repo_paths, cycle, interval, prefix, remote, push_tag)` -- Phase 5, if committed

For meta-review (in `agent_eval.run_meta_review`):
- `get_review_git_delta(repo_path, since_hash)`
- `get_head_hash(repo_path)`

Initialization (once):
- `resolve_repo_paths(workspace, commit_repos)` -- during `AgentLoop.__init__`

## Error Handling Pattern

All functions follow the same pattern:
- `async` functions, subprocess via `asyncio.create_subprocess_exec`
- `stdout` and `stderr` both piped
- Exceptions caught and logged as warnings
- Boolean return values (`True` = all repos succeeded, `False` = at least one failed)
- `tag_cycle` returns `None` and logs per-repo; uses `continue` to skip failed repos
- Stderr output in warnings is truncated to 200 chars via `[:200]`
