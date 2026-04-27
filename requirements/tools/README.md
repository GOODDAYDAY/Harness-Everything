# Tools Domain Overview

## Tool ABC Interface

Every tool is a subclass of `Tool` (defined in `harness/tools/base.py`).

### Required Members

| Member | Type | Description |
|--------|------|-------------|
| `name` | `str` | Tool name used in tool calls and registry lookup |
| `description` | `str` | Human-readable description sent to the LLM as part of API schema |
| `input_schema()` | method returning `dict[str, Any]` | JSON Schema for the tool input parameters |
| `execute(config: HarnessConfig, **params) -> ToolResult` | async method | Runs the tool and returns a `ToolResult` |

### Optional Members

| Member | Type | Default | Description |
|--------|------|---------|-------------|
| `requires_path_check` | `bool` | `False` | When True, tool operates on file paths that should be checked against allowed_paths |
| `tags` | `frozenset[str]` | `frozenset()` | Tag-based filtering. Valid tags: `"file_read"`, `"file_write"`, `"search"`, `"git"`, `"analysis"`, `"execution"`, `"network"`, `"testing"` |
| `file_security` | `ClassVar[type]` | `FileSecurity` | Centralized security validation class for file operations |

### `api_schema()` Method

Returns a dict suitable for the Claude API tool definition:

```python
{
    "name": self.name,
    "description": self.description,
    "input_schema": self.input_schema(),
}
```

---

## ToolResult Dataclass

`ToolResult` is the uniform return type for all tool executions.

```python
@dataclass
class ToolResult:
    output: str = ""
    error: str = ""
    is_error: bool = False
    elapsed_s: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
```

### `to_api()` Method

Formats the result as a tool_result content block for the Claude API:

```python
def to_api(self) -> dict[str, Any]:
    text = self.error if self.is_error else self.output
    return {"type": "text", "text": text}
```

---

## `handle_atomic_result()` Utility

Top-level function in `base.py` that centralizes the type-checking logic for `FileSecurity.atomic_validate_and_*` return values.

```python
def handle_atomic_result(
    result: AtomicResult,
    metadata_keys: Tuple[str, ...] = ("text", "resolved_path")
) -> ToolResult
```

- If `result` is a `ToolResult` (error case), returns it unchanged.
- If `result` is a tuple, returns a success `ToolResult` with tuple data stored in `metadata` keyed by `metadata_keys`.
- Type alias: `AtomicResult = Union[Tuple[str, str], ToolResult]`

---

## FileSecurity Class

Centralized security validation for file operations, defined as a static-only class in `base.py`. All methods are `@staticmethod`.

### Methods

| Method | Signature | Description |
|--------|-----------|-------------|
| `atomic_validate_and_read` | `async (config, path, require_exists=True, check_scope=True, resolve_symlinks=False) -> (str, str) \| ToolResult` | Atomically validate and read a file. Returns `(text, resolved_path)` on success. |
| `atomic_validate_and_write` | `async (config, path, content, require_exists=False, check_scope=True, resolve_symlinks=False) -> ToolResult` | Atomically validate and write a file. |
| `atomic_validate_and_delete` | `async (config, path, check_scope=True, resolve_symlinks=False) -> ToolResult` | Atomically validate and delete a file. |
| `atomic_validate_and_move` | `async (config, source, destination, require_exists=True, check_scope=True, resolve_symlinks=False) -> (str, str) \| ToolResult` | Validate source and destination for a move. Returns `(validated_src, validated_dst)` on success. |
| `atomic_validate_and_copy` | `async (config, source, destination, require_exists=True, check_scope=True, resolve_symlinks=False) -> (str, str) \| ToolResult` | Validate source and destination for a copy. Returns `(validated_src, validated_dst)` on success. |
| `validate_atomic_path` | `async (config, path, require_exists=True, directory=False, check_scope=True, resolve_symlinks=False) -> (bool, str \| ToolResult)` | Validate a file path with TOCTOU protection. |
| `validate_and_prepare_parent_directory` | `async (config, parent_dir, require_exists=False, check_scope=True, resolve_symlinks=False) -> (bool, str \| ToolResult)` | Validate and optionally create a parent directory. |
| `_atomic_read_text` | `async (config, resolved_path) -> (str \| None, ToolResult \| None)` | Read file content atomically. Returns `(text, None)` on success, `(None, ToolResult)` on error. |

---

## Tool Instance Path Validation Methods

Methods on the `Tool` base class for path security validation:

### `_validate_atomic_path_sync`

```python
def _validate_atomic_path_sync(
    self, config, path_str, require_exists=True,
    directory=False, check_scope=False, resolve_symlinks=True
) -> tuple[bool, str | ToolResult]
```

Synchronous atomic path validation with inode verification. Opens path with `os.O_RDONLY | os.O_NOFOLLOW`, validates via `_check_path`, and verifies file hasn't changed using `st_dev` and `st_ino`.

### `_validate_atomic_path`

```python
async def _validate_atomic_path(
    self, config, path_str, require_exists=True,
    directory=False, check_scope=False, resolve_symlinks=True
) -> tuple[bool, str | ToolResult]
```

Async wrapper that delegates to `_validate_atomic_path_sync` via `asyncio.to_thread`.

### `_validate_path_atomic`

```python
async def _validate_path_atomic(
    self, config, path
) -> tuple[bool, str | ToolResult]
```

Unified atomic path validation using `os.open` with `O_NOFOLLOW`. Resolves real path via `/proc/self/fd` (Linux) with fallback to `os.path.realpath`. Returns `(True, resolved_path_str)` on success.

### Other Path Methods

| Method | Description |
|--------|-------------|
| `_check_path(config, path, require_exists=False, resolve_symlinks=True)` | Validate a file path against security rules and allowed-paths scope. Returns `str` (resolved path) or `ToolResult` (error). |
| `_validate_path_result(path_result)` | Standardize type checking for `_check_path` return values. Returns `(is_valid, validated_path_or_error)`. |
| `_validate_directory_atomic(config, path_str, resolve_symlinks=True)` | Validate a path is an accessible directory. Delegates to `_validate_atomic_path` with `directory=True`. |
| `_validate_and_prepare_parent_directory(config, parent_path, require_exists=True, check_scope=False, resolve_symlinks=True)` | Validate and optionally create a parent directory. Skips if parent is `"."`. |
| `_validate_root_path(config, root)` | Validate a root path for directory operations. Returns `(resolved_path, None)` on success or `("", error_ToolResult)` on failure. |
| `_check_dir_root(config, root)` | Validate root against allowed_paths. Returns `(resolved_root, allowed_list, None)` on success or `(Path('.'), [], error_ToolResult)` on failure. |
| `_resolve_and_check(config, path)` | DEPRECATED: Use `_check_path` instead. |
| `_check_phase_scope(config, resolved_path)` | Reject writes outside the running phase's `allowed_edit_globs`. Returns `None` when allowed, `ToolResult` error when blocked. |

---

## `_safe_json` Static Method

```python
@staticmethod
def _safe_json(obj: object, max_bytes: int = 24_000) -> str
```

Serializes `obj` to JSON, trimming list fields if the result exceeds `max_bytes`.

### Truncation Algorithm

1. Serialize `obj` to JSON. If `len(raw) <= max_bytes`, return immediately.
2. Create a shallow copy `work` (if `obj` is a dict, use `dict(obj)`; otherwise wrap as `{"data": obj}`).
3. Set `work["truncated"] = True`.
4. Iterate up to 20 times:
   - Serialize `work`. If it fits, return.
   - Find all list-valued keys. If none, break.
   - Select the longest list (`max(list_keys, key=lambda k: len(work[k]))`).
   - Halve its length: `new_len = max(1, current_len // 2)`. If `new_len == current_len`, break.
   - Truncate: `work[biggest_key] = work[biggest_key][:new_len]`.
5. Final fallback: return `{"error": "output too large to serialize", "truncated": true}`.

The 20-iteration cap is safe because each pass reduces the largest list by half.

---

## `_rglob_safe` Static Method

```python
@staticmethod
def _rglob_safe(
    root: Path, pattern: str, allowed: list[Path], limit: int = 500
) -> list[Path]
```

`rglob` that rejects files resolving outside `allowed_paths`.

### Algorithm

1. Use `itertools.islice(root.rglob(pattern), limit * 4)` to cap memory at 4x limit entries before filtering.
2. For each candidate:
   - Resolve with `.resolve()`. Skip on `OSError` (dangling symlinks).
   - Check `resolved == a or resolved.is_relative_to(a)` for each allowed path.
   - If allowed, append to results. Stop when `len(results) >= limit`.
3. Return the results list (not sorted -- caller sorts if needed).

Note: Python < 3.13 `rglob` follows symlinks during traversal. The `islice(limit * 4)` bounds memory exposure.

---

## `enforce_atomic_validation` Decorator

```python
def enforce_atomic_validation(tool_cls)
```

Class decorator that ensures tools with `requires_path_check=True` use atomic validation. Wraps the `execute` method to track whether `_validate_atomic_path` or `_validate_path_atomic` was called. If not, logs a warning. Adds `_enforces_atomic_validation = True` class attribute.

---

## ToolRegistry

Defined in `harness/tools/registry.py`.

### Class Structure

```python
class ToolRegistry:
    _tools: dict[str, Tool]
```

### Methods

| Method | Signature | Description |
|--------|-----------|-------------|
| `register` | `(tool: Tool) -> None` | Store tool by its `.name` |
| `get` | `(name: str) -> Tool \| None` | Lookup by name |
| `names` | `@property -> list[str]` | All registered tool names |
| `to_api_schema` | `() -> list[dict[str, Any]]` | Export all tool definitions for the Claude API |
| `filter_by_tags` | `(tags: frozenset[str]) -> ToolRegistry` | Return new registry with tools matching at least one tag. Tools with empty tags are always included. |
| `execute` | `async (name, config, params) -> ToolResult` | Look up and execute a tool by name |

### `execute()` Error Categories

The dispatch method categorizes errors into three types, each with a distinct prefix for the LLM:

| Category | Trigger | Prefix |
|----------|---------|--------|
| `SCHEMA ERROR` | `TypeError` — missing or wrong-type parameter | `SCHEMA ERROR calling {name!r}: ...` |
| `PERMISSION ERROR` | `PermissionError` or `OSError` with EACCES/EPERM | `PERMISSION ERROR in {name!r}: ...` |
| `TOOL ERROR` | Any other exception | `TOOL ERROR in {name!r} — {type(exc).__name__}: {exc}` |

Additionally:
- Unknown tool name returns `ToolResult(error=f"Unknown tool: {name!r}", is_error=True)`.
- If `config.allowed_tools` is non-empty and the tool name is not in the list, returns `PERMISSION ERROR`.
- Every execution is timed with `time.monotonic()` and logged via `TOOL_TRACE` JSON entries.
- `result.elapsed_s` is set to `round(duration_ms / 1000, 4)`.

### Parameter Alias Normalization (`_PARAM_ALIASES`)

Before dispatch, common LLM parameter-name mistakes are corrected via `_normalise_params()`. The complete alias map:

| Wrong Name (LLM sends) | Correct Name (schema expects) | Context |
|------------------------|-------------------------------|---------|
| `file_content` | `content` | write_file |
| `file_path` | `path` | many tools |
| `filename` | `path` | many tools |
| `filepath` | `path` | many tools |
| `text` | `content` | write_file |
| `old_string` | `old_str` | edit_file |
| `new_string` | `new_str` | edit_file |
| `old_text` | `old_str` | edit_file |
| `new_text` | `new_str` | edit_file |
| `directory` | `path` | list_directory / tree |
| `dir` | `path` | list_directory / tree |
| `pattern` | `glob` | grep_search |
| `query` | `glob` | grep_search |
| `search` | `regex` | grep_search |
| `cmd` | `command` | bash |

Renaming conditions (all three must hold):
1. The wrong-name key exists in the params dict.
2. The correct name is a known property in the tool's `input_schema()`.
3. The correct name is not already present in params (no clobbering).

### Unknown Parameter Check (`_check_unknown_params`)

After alias normalization, params are validated against the tool's schema properties. Any keys not in `schema["properties"]` trigger a `SCHEMA ERROR` with the sorted list of unknown and known parameters.

---

## `build_registry()` Function

Defined in `harness/tools/__init__.py`.

```python
def build_registry(
    allowed_tools: list[str] | None = None,
    extra_tools: list[str] | None = None,
) -> ToolRegistry
```

- `allowed_tools`: Filter applied to `DEFAULT_TOOLS` only. An empty list is treated as `None` (no filter).
- `extra_tools`: Tool names looked up from `_ALL_TOOLS_BY_NAME` (union of DEFAULT and OPTIONAL). Always added regardless of `allowed_tools`. Unknown names are logged as warnings and skipped.
- Double-registration is prevented: if a tool was already registered via `DEFAULT_TOOLS`, it is not registered again from `extra_tools`.

---

## DEFAULT_TOOLS (Complete List, in Registration Order)

### Batch File Tools (primary)
1. `batch_read` (BatchReadTool)
2. `batch_edit` (BatchEditTool)
3. `batch_write` (BatchWriteTool)
4. `edit_file` (EditFileTool)
5. `scratchpad` (ScratchpadTool)

### Search and Analysis
6. `grep_search` (GrepSearchTool)
7. `glob_search` (GlobSearchTool)
8. `symbol_extractor` (SymbolExtractorTool)
9. `code_analysis` (CodeAnalysisTool)
10. `cross_reference` (CrossReferenceTool)
11. `feature_search` (FeatureSearchTool)
12. `project_map` (ProjectMapTool)
13. `file_info` (FileInfoTool)

### Testing and Verification
14. `test_runner` (TestRunnerTool)
15. `lint_check` (LintCheckTool)
16. `context_budget` (ContextBudgetTool)

### File / Dir Operations
17. `delete_file` (DeleteFileTool)
18. `move_file` (MoveFileTool)
19. `copy_file` (CopyFileTool)
20. `list_directory` (ListDirectoryTool)
21. `create_directory` (CreateDirectoryTool)
22. `tree` (TreeTool)
23. `file_patch` (FilePatchTool)
24. `find_replace` (FindReplaceTool)
25. `diff_files` (DiffFilesTool)

### Git
26. `git_status` (GitStatusTool)
27. `git_diff` (GitDiffTool)
28. `git_log` (GitLogTool)

### Specialized
29. `data_flow` (DataFlowTool)
30. `call_graph` (CallGraphTool)
31. `dependency_analyzer` (DependencyAnalyzerTool)
32. `python_eval` (PythonEvalTool)
33. `json_transform` (JsonTransformTool)
34. `ast_rename` (AstRenameTool)
35. `tool_discovery` (ToolDiscoveryTool)
36. `todo_scan` (TodoScanTool)

### Bash (last)
37. `bash` (BashTool)

---

## OPTIONAL_TOOLS (Complete List)

These are NOT registered by default. Opt in via `HarnessConfig.extra_tools` or `build_registry(extra_tools=[...])`.

| Tool | Class | Reason Optional |
|------|-------|-----------------|
| `web_search` | WebSearchTool | DuckDuckGo search + page fetch; needs network access |
| `http_request` | HttpRequestTool | Generic HTTP client (GET/POST/etc.); needs network access |
| `git_search` | GitSearchTool | Git history/blame/grep; high schema cost, specialized |
| `read_file` | ReadFileTool | Superseded by `batch_read` |
| `write_file` | WriteFileTool | Superseded by `batch_write` |

---

## ALL_TOOLS

Union of `DEFAULT_TOOLS + OPTIONAL_TOOLS`. Exported for catalogue queries, validation, and test fixtures.

`_ALL_TOOLS_BY_NAME: dict[str, Tool]` maps `tool.name -> tool instance` for both default and optional tools.
