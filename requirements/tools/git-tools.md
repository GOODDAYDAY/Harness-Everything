# Git Tools

Read-only git information tools: `git_status`, `git_diff`, `git_log`.

All three are DEFAULT tools (always registered). They share a common `_run_git` helper that runs git commands as async subprocesses in the workspace directory with a 30-second timeout.

**Source**: `harness/tools/git.py`

---

## Shared Helper: `_run_git`

```python
async def _run_git(config: HarnessConfig, *args: str) -> ToolResult
```

1. Runs `git {args}` via `asyncio.create_subprocess_exec` with `cwd=config.workspace`.
2. Captures stdout and stderr via `PIPE`.
3. Waits with `asyncio.wait_for(proc.communicate(), timeout=30)`.
4. On timeout: kills the child process, reaps it (`proc.wait()`), returns error `"git command timed out"`.
5. On non-zero exit code: returns error from stderr (or stdout).
6. On success: returns output (or `"(empty)"` if stdout is empty).

---

## git_status

| Field | Value |
|-------|-------|
| name | `"git_status"` |
| requires_path_check | `False` |
| tags | `frozenset({"git"})` |

### Description

Show the working tree status (`git status`).

### Input Schema

```python
{"type": "object", "properties": {}}
```

No parameters. No required fields.

### Execution

Runs `git status --short`.

---

## git_diff

| Field | Value |
|-------|-------|
| name | `"git_diff"` |
| requires_path_check | `False` |
| tags | `frozenset({"git"})` |

### Description

Show file changes. By default shows unstaged changes. Set `staged=true` to see staged changes.

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `staged` | `boolean` | No | `false` | Show staged changes instead |
| `path` | `string` | No | `""` | Limit diff to a specific file/directory |

No required fields.

### Execution

Runs `git diff [--cached] [-- {path}]`.

---

## git_log

| Field | Value |
|-------|-------|
| name | `"git_log"` |
| requires_path_check | `False` |
| tags | `frozenset({"git"})` |

### Description

Show recent commit log.

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `count` | `integer` | Yes | -- | Number of commits to show |
| `oneline` | `boolean` | No | `true` | One-line format |

Required: `["count"]`

### Execution

Runs `git log -{count} [--oneline]`.
