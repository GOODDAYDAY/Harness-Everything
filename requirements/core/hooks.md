# S-07 Verification Hooks

> Pluggable post-execution verification checks that gate commits and catch regressions.

**Source**: `harness/core/hooks.py`

---

## Background

The hooks system provides a pluggable pipeline of post-execution checks that run after each phase's executor completes. Hooks verify that the LLM's code changes are syntactically valid, importable, and free of critical static errors before they are committed. The system is designed for the self-improvement use case where the harness edits its own source -- a broken module would prevent the next round from running at all.

---

## Requirements

### Hook Result

**F-01 `HookResult` dataclass**

| Field | Type | Default |
|:---|:---|:---|
| `passed` | `bool` | (required) |
| `output` | `str` | (required) |
| `errors` | `str` | `""` |

### Abstract Base Class

**F-02 `VerificationHook` ABC**

- Abstract base class with `ABC`.
- Class attribute `name: str` -- identifies the hook in logs and reports.
- Class attribute `gates_commit: bool = False` -- when `True`, failure of this hook suppresses all subsequent hook executions in the same phase. When `False`, the hook is advisory and does not block the commit.
- Abstract method: `async run(self, config: HarnessConfig, context: dict[str, Any]) -> HookResult`.
- The `context` dict carries phase/round metadata and may include keys like `"inner_dir"`, `"phase"`, `"outer"`, `"files_changed"`, `"best_score"`, `"changes_summary"`, etc.

### SyntaxCheckHook

**F-03 Class attributes**

- `name = "syntax_check"`
- `gates_commit = True`

**F-04 Constructor**

`__init__(self, patterns: list[str] | None = None)` -- defaults to `["**/*.py"]`.

**F-05 `run()` behaviour**

- For each pattern in `self.patterns`, globs recursively relative to `config.workspace`.
- Compiles each matching file with `py_compile.compile(full_path, doraise=True)`.
- On `py_compile.PyCompileError`: appends `"{path_str}: {e.msg}"` to errors list.
- On any other `Exception`: silently ignores (the file might not be Python).
- Returns `HookResult(passed=False, output="", errors=error_text)` if any errors, where `error_text` is newline-joined.
- Returns `HookResult(passed=True, output="All syntax checks passed")` if clean.

### ImportSmokeHook

**F-06 Class attributes**

- `name = "import_smoke"`
- `gates_commit = True`

**F-07 Constructor**

`__init__(self, modules: list[str] | None = None, smoke_calls: list[str] | None = None, timeout: int = 30)`

- `modules` defaults to `["harness.core.config", "harness.agent", "harness.tools"]`.
- `smoke_calls` defaults to `[]`. These are arbitrary Python statements executed after imports to catch runtime-only `NameError`s that hide inside function bodies.
- `timeout` defaults to `30` seconds.

**F-08 `run()` behaviour**

- If both `modules` and `smoke_calls` are empty, returns `HookResult(passed=True, output="(no modules to check)")`.
- Constructs a Python script with:
  1. Import statements: `"import {m}"` for each module.
  2. Registry check: `"from harness.tools import build_registry\nbuild_registry()\n"` -- but **only** when the workspace resolves to the harness source root (`Path(__file__).resolve().parents[2]`). Skipped when running against external projects.
  3. Smoke calls: each entry from `self.smoke_calls` appended as-is.
- Executes via `asyncio.create_subprocess_exec(sys.executable, "-c", script, ...)`.
- Sets `PYTHONPATH` in the subprocess environment to include the harness source root, prepended to any existing `PYTHONPATH`.
- Uses `sys.executable` to ensure the same interpreter/venv as the harness itself.
- Working directory: `config.workspace`.
- On `asyncio.TimeoutError`: kills and reaps subprocess, returns `HookResult(passed=False, errors="import smoke timed out")`.
- On `FileNotFoundError`: returns error with `f"interpreter not found: {sys.executable}"`.
- On returncode 0: returns `HookResult(passed=True, output=f"import smoke OK ({len(self.modules)} modules)")`.
- On failure: returns stderr+stdout (truncated to 2,000 chars) in `errors`.

### StaticCheckHook

**F-09 Class attributes**

- `name = "static_check"`
- `gates_commit = True`
- `RUFF_RULES = "F821,F811,F401"` -- F821 (undefined name) is the critical rule; F811 (redefined name) and F401 (unused import) are cheap extras.

**F-10 Constructor**

`__init__(self, timeout: int = 20)`.

**F-11 `_run_tool()` helper**

`async _run_tool(self, argv: list[str], cwd: str) -> tuple[int | None, str]`

- Runs a subprocess with stdout+stderr captured.
- Returns `(returncode, combined_output)` on completion.
- Returns `(None, "static check timed out")` on `TimeoutError` (kills and reaps).
- Returns `(None, "tool not found")` on `FileNotFoundError`.
- Returns `(None, str(e))` on any other exception.

**F-12 `run()` behaviour**

- Extracts Python files from `context["files_changed"]` (filters for `.py` suffix).
- If no Python files changed, returns `HookResult(passed=True, output="static_check: no python files changed")`.
- **Probe ruff**: runs `sys.executable -m ruff --version`. If available (rc 0):
  - Runs `sys.executable -m ruff check --select F821,F811,F401 --no-cache <files>`.
  - On rc 0: returns passed with output `f"static_check (ruff {self.RUFF_RULES}): clean on {len(changed)} file(s)"`.
  - On failure: returns errors `f"ruff reported issues:\n{output[:1500]}"`.
- **Probe pyflakes** (fallback): runs `sys.executable -m pyflakes --version`. If available:
  - Runs `sys.executable -m pyflakes <files>`.
  - Same pass/fail logic.
- **Neither available**: logs WARNING, returns `HookResult(passed=True, ...)` with output explaining that static checks were SKIPPED and suggesting `pip install ruff`. The `errors` field is set to `"SKIPPED: no linting tool available"` but `passed` remains `True` -- the build does not block.

### PytestHook

**F-13 Class attributes**

- `name = "pytest"`
- `gates_commit = False` (default, not overridden) -- advisory only.

**F-14 Constructor**

`__init__(self, test_path: str = "tests/", timeout: int = 120)`.

**F-15 `run()` behaviour**

- Runs `sys.executable -m pytest {self.test_path} -v --tb=short`.
- Working directory: `config.workspace`.
- Captures stdout+stderr.
- On `TimeoutError`: kills and reaps subprocess, returns `HookResult(passed=False, errors="pytest timed out")`.
- On `FileNotFoundError`: returns `HookResult(passed=False, errors="pytest not found")`.
- On completion: `passed = (proc.returncode == 0)`. Output includes combined stdout+stderr. Errors is empty string on pass, same combined output on failure.

### GitCommitHook

**F-16 Class attributes**

- `name = "git_commit"`
- `gates_commit = False` (default, not overridden).

**F-17 Constructor**

`__init__(self, repos: list[str] | None = None, rich_metadata: bool = False)`

- `repos` defaults to `[]`.
- `rich_metadata` defaults to `False`.

**F-18 `run()` behaviour -- commit message construction**

Reads from `context`:
- `outer` (default `0`), `phase_name` (default `"unknown"`).

When `rich_metadata` is `True`, also reads: `best_score`, `changes_summary`, `files_changed`, `basic_critique`, `diffusion_critique`, `tool_summary`, `inner_rounds_run`, `all_scores`.

- **Simple mode** (`rich_metadata=False`): commit message is `f"[harness] R{outer+1} {phase_name}"`.
- **Rich mode** (`rich_metadata=True`): commit message is `f"[harness] R{outer+1} {phase_name} [score={score:.1f}]"` with a body containing:
  - Summary (if provided).
  - Files modified (first 10 files, with `"... and N more"` for overflow).
  - Inner rounds count with score trajectory.
  - Tool usage summary.
  - Basic evaluator critique (truncated to 300 chars).
  - Diffusion evaluator critique (truncated to 300 chars).

**F-19 `run()` behaviour -- git operations**

For each repo in `self.repos`:
1. Resolves `repo_path = Path(config.workspace) / repo`.
2. If not a directory, appends `"{repo}: directory not found, skipped"` and continues.
3. Runs `git add -A` with 30-second timeout.
4. Runs `git commit --allow-empty -m {commit_msg}` with 30-second timeout.
5. On `TimeoutError`: appends `"{repo}: git command timed out"`.
6. On success: appends `"{repo}: committed"`.
7. On failure: appends the error message.

Returns `HookResult(passed=all_passed, output=joined_results)`.

---

## Implementation Approach

- Abstract base class `VerificationHook` with concrete implementations for each check type.
- All hooks are async and use `asyncio.create_subprocess_exec` for subprocess invocations.
- `gates_commit` is a class-level flag; the orchestrator (not the hook itself) decides whether to abort subsequent hooks on failure.
- Hooks are stateless; all configuration is passed via constructor and context dict.

---

## Expected Effects

- Syntax errors in generated code are caught before commit (SyntaxCheckHook gates commit).
- Import breakage is caught in a fresh subprocess so cached modules don't mask errors (ImportSmokeHook gates commit).
- Undefined names (F821), redefined names (F811), and unused imports (F401) are caught by ruff/pyflakes (StaticCheckHook gates commit).
- Missing linting tools produce a warning but do not block the build.
- Test failures are reported but do not gate commits (PytestHook is advisory).
- Git commits include structured metadata for post-run analysis when `rich_metadata=True`.

---

## Acceptance Criteria

- `SyntaxCheckHook` detects a `SyntaxError` in a `.py` file and returns `passed=False`.
- `ImportSmokeHook` runs imports in a subprocess (not the parent process) and catches `ImportError`.
- `ImportSmokeHook` includes `build_registry()` only when workspace is the harness source root.
- `StaticCheckHook` probes ruff first, falls back to pyflakes, passes when neither is available.
- `StaticCheckHook` only checks files listed in `context["files_changed"]`, not the entire tree.
- `PytestHook` does not set `gates_commit = True`.
- `GitCommitHook` uses `--allow-empty` to avoid failure on no-change commits.
- All hooks clean up subprocesses (kill + reap) on timeout.
