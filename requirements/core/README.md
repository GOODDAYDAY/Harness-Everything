# Core Infrastructure

> Configuration, LLM client, path security, artifacts, checkpoint, project context, hooks, signal handling

## Scenarios

| Scenario | Location | Status |
|:---|:---|:---|
| S-01 Configuration | inline | Implemented |
| S-02 LLM Client | [llm.md](llm.md) | Implemented |
| S-03 Path Security | [security.md](security.md) | Implemented |
| S-04 Artifacts | inline | Implemented |
| S-05 Checkpoint | inline | Implemented |
| S-06 Project Context | inline | Implemented |
| S-07 Verification Hooks | [hooks.md](hooks.md) | Implemented |
| S-08 Signal Handling | inline | Implemented |

---

## S-01 Configuration

### Background

All harness knobs are centralised in a single `HarnessConfig` dataclass (`harness/core/config.py`). This avoids scattered env-var reads and provides a single validation gate at construction time. The config object is passed to every subsystem (LLM, tools, security) so runtime behaviour is fully determined by the dataclass fields.

### Requirements

**F-01 Dataclass fields with defaults**

`HarnessConfig` is a `@dataclass` with the following fields and defaults:

| Field | Type | Default |
|:---|:---|:---|
| `model` | `str` | `"bedrock/claude-sonnet-4-6"` |
| `max_tokens` | `int` | `8096` |
| `base_url` | `str` | `""` |
| `api_key` | `str` | `""` |
| `workspace` | `str` | `"."` |
| `allowed_paths` | `list[str]` | `[]` (factory) |
| `homoglyph_blocklist` | `dict[str, str]` | `{}` (factory) |
| `allowed_tools` | `list[str] \| None` | `[]` (factory) |
| `extra_tools` | `list[str]` | `[]` (factory) |
| `bash_command_denylist` | `list[str]` | `[]` (factory) |
| `max_iterations` | `int` | `5` |
| `max_tool_turns` | `int` | `60` |
| `max_concurrent_llm_calls` | `int` | `4` |
| `log_level` | `str` | `"INFO"` |

**F-02 Path resolution in `__post_init__`**

- `workspace` is resolved to an absolute path via `Path(self.workspace).resolve()`.
- If `allowed_paths` is empty, it defaults to `[workspace]`.
- Every entry in `allowed_paths` is resolved to an absolute path.

**F-03 Default homoglyph blocklist**

When `homoglyph_blocklist` is empty at construction, `__post_init__` populates it with a minimal high-risk set of 12 characters: Cyrillic small a (`U+0430`), Cyrillic small palochka (`U+04CF`), Cyrillic capital letter komi de (`U+0500`), Latin retroflex click (`U+01C3`), Greek capital alpha (`U+0391`), Greek small alpha (`U+03B1`), Cyrillic capital O (`U+041E`), Cyrillic small o (`U+043E`), Armenian capital letter oh (`U+0555`), Armenian hyphen (`U+058A`), Fraction slash (`U+2044`), Full-width solidus (`U+FF0F`).

**F-04 Numeric field validation**

- `max_tokens` must be in range `[1, 64_000]`; raises `ValueError` outside this range.
- `max_iterations` must be >= 1; raises `ValueError` if < 1. Logs a warning if > 100.
- `max_tool_turns` must be >= 1; raises `ValueError` if < 1. Logs a warning if > 200.

**F-05 Model string validation**

- Empty or whitespace-only `model` raises `ValueError`.
- A bare Anthropic model ID (starts with `"claude"` and contains no `/`) logs a warning suggesting the LiteLLM provider prefix (`anthropic/` or `bedrock/`).

**F-06 Workspace existence validation**

- `__post_init__` raises `ValueError` if `workspace` does not exist or is not a directory.

**F-07 Allowed paths outside workspace warning**

- Any entry in `allowed_paths` that is not equal to and does not start with `workspace + "/"` triggers a log warning.

**F-08 String-list field validation**

- `extra_tools`: raises `ValueError` if any entry is not a non-empty string.
- `allowed_tools`: when not `None`, raises `ValueError` if any entry is not a non-empty string. `None` is a valid sentinel meaning "allow all tools".
- `bash_command_denylist`: when not `None`, raises `ValueError` if any entry is not a non-empty string.

**F-09 Log level validation**

- `log_level` is uppercased and stripped. Must be one of `{"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}` (defined in module-level `_VALID_LOG_LEVELS` frozenset); raises `ValueError` otherwise.

**F-10 `apply_log_level()` method**

- Sets the `"harness"` logger hierarchy to `self.log_level` via `logging.getLogger("harness").setLevel(...)`. Does not touch the root logger.

**F-11 `startup_banner()` method**

- Returns a single-line string: `"harness startup: model=... max_tokens=... workspace=... max_iterations=... max_tool_turns=... allowed_tools=... log_level=..."`.
- `allowed_tools` is rendered as `"all"` when `self.allowed_tools` is empty/falsy, otherwise comma-joined.

**F-12 `is_path_allowed()` method**

- Delegates to `harness.core.security.validate_path_security()` for comprehensive checks (null bytes, control chars, homoglyphs). Returns `False` if any check fails.
- Resolves the path via `os.path.realpath()` (not `Path.resolve()`) to follow symlinks.
- Returns `True` iff the resolved path equals or starts with (separated by `os.sep`) at least one entry in `allowed_paths`.

**F-13 `from_dict()` class method**

- Strips keys starting with `"//"` or `"_"` (JSON comment convention).
- Raises `ValueError` on any unknown top-level key not matching a dataclass field name.
- Passes remaining keys as keyword arguments to the constructor.

### Implementation Approach

Single-file `@dataclass` with all validation in `__post_init__`. Module-level `_VALID_LOG_LEVELS` frozenset defines accepted log levels.

### Expected Effects

- A single source of truth for all runtime configuration.
- Invalid configs fail fast at construction with clear error messages.
- Warnings surface potential misconfigurations (bare model IDs, paths outside workspace, extreme iteration counts) without blocking startup.

### Acceptance Criteria

- Constructing `HarnessConfig()` with no arguments succeeds when cwd is a directory.
- Each invalid field value raises `ValueError` with a descriptive message.
- `from_dict()` rejects unknown keys and strips comment keys.
- `is_path_allowed()` rejects null bytes, symlink escapes, and paths outside `allowed_paths`.

---

## S-04 Artifacts

### Background

`ArtifactStore` (`harness/core/artifacts.py`) manages the hierarchical artifact directory for a single harness run. It provides a clean API for writing, reading, and locating artifacts within a timestamp-based run directory.

### Requirements

**F-01 Constructor and run directory**

- `__init__(self, base_dir: str | Path, run_id: str | None = None)`.
- When `run_id` is `None`, generates `f"run_{datetime.now().strftime('%Y%m%dT%H%M%S')}"`.
- Creates `base_dir / run_id` with `mkdir(parents=True, exist_ok=True)`.
- Stores the result as `self.run_dir`.

**F-02 Core operations**

- `path(*segments: str) -> Path`: joins segments under `run_dir`.
- `write(content: str, *segments: str) -> Path`: creates parent directories, writes UTF-8 text, returns the path.
- `read(*segments: str) -> str`: reads UTF-8 text, returns `""` on any `OSError`.
- `exists(*segments: str) -> bool`: checks if the artifact file exists.

**F-03 Run-level markers**

- `write_final_summary(content: str) -> Path`: writes `"final_summary.md"` under `run_dir`.
- `is_complete` property: returns `True` iff `"final_summary.md"` exists.

**F-04 Convenience path helpers**

- `inner_dir(outer: int, phase_label: str, inner: int) -> tuple[str, str, str]`: returns `(f"round_{outer+1}", f"phase_{phase_label}", f"inner_{inner+1}")`. All indices are 0-based input, 1-based output.
- `phase_dir(outer: int, phase_label: str) -> tuple[str, str]`: returns `(f"round_{outer+1}", f"phase_{phase_label}")`.

**F-05 `find_resumable()` class method**

- `find_resumable(cls, base_dir: str | Path) -> ArtifactStore | None`.
- Returns `None` if `base_dir` is not a directory.
- Scans `run_*` directories in reverse sort order (newest first).
- Skips runs that have `final_summary.md` (already complete).
- Returns the first run that has at least one `round_*` subdirectory (started but incomplete).
- Reconstructs the `ArtifactStore` via `cls.__new__(cls)` and sets `run_dir` directly.

### Implementation Approach

Single class with no external dependencies beyond `datetime` and `pathlib`. Directory layout is convention-driven via `inner_dir` / `phase_dir` helpers.

### Expected Effects

- All artifacts for a run live under a single timestamped directory.
- Resume support via `find_resumable()` allows interrupted runs to continue.
- `is_complete` provides a simple completion check.

### Acceptance Criteria

- `write()` creates nested directories and writes content.
- `read()` returns `""` for missing files without raising.
- `find_resumable()` returns `None` for complete runs and finds incomplete ones.

---

## S-05 Checkpoint

### Background

`CheckpointManager` (`harness/core/checkpoint.py`) manages `.done` marker files on top of an `ArtifactStore`. Each checkpoint is a zero-byte file whose presence tells the runner to skip re-executing that step on resume. A companion `CheckpointMetadata` dataclass stores structured evaluation data alongside markers.

### Requirements

**F-01 `CheckpointMetadata` dataclass**

Fields and defaults:

| Field | Type | Default |
|:---|:---|:---|
| `checkpoint_type` | `str` | (required) |
| `outer_round` | `int` | (required) |
| `phase_label` | `str` | `""` |
| `inner_index` | `int` | `-1` |
| `basic_score` | `float` | `0.0` |
| `diffusion_score` | `float` | `0.0` |
| `critique_count` | `int` | `0` |
| `actionable_critiques` | `int` | `0` |
| `synthesis_specificity_score` | `int` | `0` |
| `timestamp` | `datetime` | `field(default_factory=datetime.now)` |

`checkpoint_type` accepts values like `"phase"`, `"inner"`, `"synthesis"`, `"meta_review"`.

**F-02 Path segment validation (`_validate_path_segments`)**

- Rejects segments equal to `".."` or empty string `""` with `ValueError`.
- Calls `validate_path_security()` on each individual segment.
- Builds the full path via `self.store.path(*segments)` and verifies it is relative to `self.store.run_dir` (raises `ValueError` if it escapes).
- Validates the full path string via `validate_path_security()`.

**F-03 Basic marker operations**

- `is_done(*segments) -> bool`: checks for `".done"` file at the given path.
- `mark_done(*segments)`: validates path segments, then writes a zero-byte `".done"` file.
- `is_skipped(*segments) -> bool`: checks for `"skipped.done"` file.
- `mark_skipped(*segments)`: writes a zero-byte `"skipped.done"` file.

**F-04 Convenience helpers for inner/phase/synthesis/meta-review**

- `is_inner_done(outer, phase_label, inner)` / `mark_inner_done(...)`: delegates to `is_done` / `mark_done` with `store.inner_dir(...)` segments.
- `is_phase_done(outer, phase_label)` / `mark_phase_done(...)`: delegates with `store.phase_dir(...)`.
- `is_phase_skipped(outer, phase_label)` / `mark_phase_skipped(...)`: delegates to `is_skipped` / `mark_skipped`.
- `is_synthesis_done(outer, phase_label)` / `mark_synthesis_done(...)`: checks/writes `"synthesis.done"` within the phase directory.
- `is_meta_review_done(outer)` / `mark_meta_review_done(...)`: checks/writes `"meta_review.done"` within `f"round_{outer+1}"`.

**F-05 Structured checkpoint metadata persistence**

- `write_checkpoint_metadata(metadata: CheckpointMetadata, *segments)`:
  - Validates path segments via `_validate_path_segments`.
  - Validates `synthesis_specificity_score` is an `int` in range `[0, 10]`; raises `ValueError` otherwise.
  - Serialises metadata to `"checkpoint_metadata.json"` with `indent=2`, converting `timestamp` to ISO format.
- `read_checkpoint_metadata(*segments) -> CheckpointMetadata | None`:
  - Validates path segments.
  - Returns `None` if the JSON file does not exist.
  - Parses `timestamp` back from ISO format.
  - Validates `synthesis_specificity_score` exists, is an `int`, and is in `[0, 10]`; raises `ValueError` on invalid data.
  - Returns `None` (with a log warning) on `json.JSONDecodeError` or `KeyError`.

**F-06 Hash-based incremental review**

- `read_last_review_hash() -> str`: reads `"meta_review_hash.txt"`, returns stripped content or `""`.
- `write_last_review_hash(commit_hash: str)`: writes `commit_hash` to `"meta_review_hash.txt"`.

### Implementation Approach

Thin layer over `ArtifactStore`. All marker files are zero-byte. Metadata files are JSON. Path validation delegates to `harness.core.security.validate_path_security`.

### Expected Effects

- Interrupted runs can resume without re-executing completed steps.
- Structured metadata enables post-run analysis of evaluation scores and critique counts.
- Path traversal attacks via malicious segment values are blocked.

### Acceptance Criteria

- `mark_done()` / `is_done()` round-trip correctly.
- Path segments containing `".."` or empty strings raise `ValueError`.
- `write_checkpoint_metadata` / `read_checkpoint_metadata` round-trip with correct score validation.

---

## S-06 Project Context

### Background

`ProjectContextBuilder` (`harness/core/project_context.py`) collects a compact, signal-dense snapshot of the target project's structure and recent history. This block is injected into the planner's prompt so the LLM can reason about what already exists before deciding what to change. All collection is best-effort: missing git, empty globs, or permission errors produce silent omissions.

### Requirements

**F-01 Tuneable constants**

| Constant | Value | Purpose |
|:---|:---|:---|
| `_TREE_MAX_DEPTH` | `3` | Max directory tree recursion depth |
| `_TREE_MAX_ENTRIES` | `150` | Hard cap on total tree entries |
| `_GIT_LOG_COUNT` | `12` | Number of recent commits shown |
| `_FILE_GLOB_LIMIT` | `40` | Max files per glob category |
| `_MAX_OUTPUT_CHARS` | `6_000` | Total cap on formatted output |

**F-02 Skipped directories**

`_TREE_SKIP_DIRS` is a frozenset: `{".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", ".venv", "venv", "env", ".env", "dist", "build", ".eggs", "*.egg-info", ".tox", ".nox", "htmlcov", ".coverage"}`.

Additionally, entries starting with `"."` and entries ending with `".egg-info"` are filtered out in the tree builder.

**F-03 File categories for inventory**

`_FILE_CATEGORIES` is a list of `(label, glob_pattern)` tuples:
- `("Python sources", "**/*.py")`
- `("Tests", "**/test_*.py")`
- `("Config files", "*.{json,yaml,yml,toml,ini,cfg}")`
- `("Docs / markdown", "**/*.md")`

**F-04 `_run_cmd()` async helper**

- Runs a subprocess with stdout captured, stderr discarded.
- Returns stdout as string on success (returncode 0), `""` on any failure.
- 10-second default timeout; kills and reaps the process on `TimeoutError`.

**F-05 `_build_tree()` recursive directory tree**

- Builds a tree listing with `"├── "` / `"└── "` connectors.
- Filters out hidden entries (starting with `"."`), entries in `_TREE_SKIP_DIRS`, and entries ending with `".egg-info"`.
- Connector logic is based on the **filtered** visible list, not the raw directory listing.
- Appends `"... (truncated)"` when `_TREE_MAX_ENTRIES` is exceeded.
- Directories get a trailing `/` in the display.

**F-06 `_file_inventory()` glob-based listing**

- For each category in `_FILE_CATEGORIES`, globs relative to workspace.
- Deduplicates by resolved path.
- Caps at `_FILE_GLOB_LIMIT` per category; appends `"(+N more)"` suffix when exceeded (note: due to early `break`, N is always 0 — the true overflow count is unknown).
- Sorts matches for determinism (sort happens after truncation, so different runs may include different files when limit is hit).

**F-07 `ProjectContextBuilder.build()` public API**

- Fires four tasks in parallel via `asyncio.gather`: tree (in executor), git log, git status, file inventory (in executor).
- Uses `asyncio.get_running_loop()` (not the deprecated `get_event_loop()`).
- Assembles sections in priority order: Recent Commits > Working Tree Status > Project Structure > File Inventory.
- Each section is markdown-formatted with `###` headers and code fences.
- Truncates final output to `_MAX_OUTPUT_CHARS` with `"... [project context truncated]"` suffix.
- Returns `""` if no sections produced content.
- Prefixes with `"## Project Context\n\n"`.

### Implementation Approach

Single class with async `build()` method. CPU-bound work (tree building, glob inventory) runs in the default executor via `run_in_executor`. Git commands use `asyncio.create_subprocess_exec`.

### Expected Effects

- The planner receives a compact, deterministic snapshot of the project.
- Git sections have highest priority and survive truncation.
- Missing git or empty projects produce a valid (possibly empty) string rather than errors.

### Acceptance Criteria

- `build()` returns a string starting with `"## Project Context"` when content is available.
- Output never exceeds `_MAX_OUTPUT_CHARS + header length`.
- Missing git returns `""` for git sections without raising.

---

## S-08 Signal Handling

### Background

`signal_util` (`harness/core/signal_util.py`) provides shared SIGINT/SIGTERM handler installation for async loops. It enables "finish the current unit of work, then exit cleanly on Ctrl-C" semantics.

### Requirements

**F-01 Handled signals**

`_HANDLED_SIGNALS` is a tuple: `(signal.SIGINT, signal.SIGTERM)`.

**F-02 `install_shutdown_handlers(callback: Callable[[], None])`**

- Gets the running event loop via `asyncio.get_running_loop()`.
- If no loop is running, logs a debug message and returns (no-op).
- Registers `callback` for both SIGINT and SIGTERM via `loop.add_signal_handler()`.
- On `NotImplementedError` (Windows), logs a debug message and returns (no-op). Callers on Windows rely on the default `KeyboardInterrupt` behaviour.

**F-03 `uninstall_shutdown_handlers()`**

- Gets the running event loop; returns immediately if no loop is running.
- Calls `loop.remove_signal_handler(sig)` for each signal in `_HANDLED_SIGNALS`.
- Silently catches `NotImplementedError` and `RuntimeError`.

### Implementation Approach

Two module-level functions. No state is maintained; the event loop owns the handler registration. The `try/except NotImplementedError` pattern ensures cross-platform compatibility.

### Expected Effects

- Unix: graceful shutdown on Ctrl-C or `kill` by invoking the provided callback.
- Windows: silent no-op; default `KeyboardInterrupt` behaviour is preserved.
- `uninstall_shutdown_handlers()` is safe to call unconditionally (idempotent).

### Acceptance Criteria

- On Unix, `install_shutdown_handlers()` registers handlers for both SIGINT and SIGTERM.
- On Windows (or without a running loop), the functions are silent no-ops.
- `uninstall_shutdown_handlers()` does not raise on any platform.
