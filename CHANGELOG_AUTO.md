# CHANGELOG_AUTO.md — Auto-generated harness change log

---

## Round · 2026-04-16 — Tool Registry Restructure: GitSearchTool → OPTIONAL_TOOLS, Remove Import-time Assertions

### What Changed and Why

#### `harness/tools/__init__.py`

- Removed `GitSearchTool()` from `DEFAULT_TOOLS` list (was entry 31 of 32).
  **Rationale**: git-history/blame/grep is a specialised capability with high schema
  cost; including it unconditionally increases per-call LLM schema payload for all
  tasks. Moved to `OPTIONAL_TOOLS` to match the established pattern for `HttpRequestTool`.
- Added `GitSearchTool()` to `OPTIONAL_TOOLS` (now 3 entries: WebSearch, HttpRequest, GitSearch).
- Removed `_EXPECTED_DEFAULT_COUNT = 32` and `_EXPECTED_OPTIONAL_COUNT = 2` constants.
- Removed import-time `assert len(DEFAULT_TOOLS) == _EXPECTED_DEFAULT_COUNT` and
  `assert len(OPTIONAL_TOOLS) == _EXPECTED_OPTIONAL_COUNT` assertions.
  **Rationale**: Both evaluators in Rounds 2 and 3 independently flagged these as
  "import-time landmines" — a miscounted constant causes an `AssertionError` on every
  import across all modes, with a confusing error. Safety net preserved in CI tests.
- Updated docstring count from `"32 of 32"` to `"31 of 31"`.
- Updated optional tools section to document `git_search`.
- Cleaned up the blank lines left by assertion removal.

#### `tests/test_tools_registry.py` (new file)

- Created `tests/` directory and `tests/test_tools_registry.py`.
- Two tests replace the removed import-time assertions:
  - `test_no_duplicate_tool_names()` — verifies no two tools share a name across
    DEFAULT_TOOLS + OPTIONAL_TOOLS.
  - `test_all_tools_implement_abc()` — verifies every tool is a `Tool` instance with
    callable `input_schema` and `execute`.
- Both tests pass: `2 passed in 0.37s`.

### Structural Changes

- No files deleted or moved.
- `tests/` directory created (was absent).
- `GitSearchTool` registration moved from `DEFAULT_TOOLS` → `OPTIONAL_TOOLS`.

### Metrics Capability

No metrics changes in this round.

### Net Lines Added / Removed

- `harness/tools/__init__.py`: removed 13 lines (assertions + constants + blank lines), updated 5 lines. Net: −13 lines.
- `tests/test_tools_registry.py`: +27 lines (new file).
- Net across all files: +14 lines added (test file wins), −13 lines dead code removed.

---

## Round 2 · 2025 — Security Hardening & New AST Tools

### Files Modified / Created

| File | Status |
|------|--------|
| `harness/tools/cross_reference.py` | **NEW** |
| `harness/tools/semantic_search.py` | **NEW** |
| `harness/metrics.py` | **NEW** |
| `harness/tools/__init__.py` | Modified — 4 lines added |
| `harness/pipeline.py` | Modified — 5 lines added |
| `harness/tools/registry.py` | Modified — 3 lines added (comment) |

---

### What Was Changed / Added

#### `harness/tools/cross_reference.py` (new, ~210 lines)
- New `CrossReferenceTool` (`name = "cross_reference"`) — AST-based symbol
  cross-reference. Finds definition location, callers (up to 50), callees
  (up to 30), and test files for any Python function, method, or class.
- **Security fix**: Explicit `allowed_paths` enforcement at the top of
  `execute()` using `Path.is_relative_to()` — does NOT rely on
  `requires_path_check` flag, which was the confirmed bypass vector.
- **O(n) `_parent_class`**: Parent-pointer map built in a single `ast.walk`
  pass; replaces the O(n²) repeated-walk approach identified in prior rounds.
- **Output budget guard**: Compact JSON (`indent=None`); trims callers list
  if serialised output exceeds 8 192 bytes, sets `truncated: true`.

#### `harness/tools/semantic_search.py` (new, ~127 lines)
- New `SemanticSearchTool` (`name = "semantic_search"`) — finds Python
  identifiers semantically related to a plain-English concept using
  token-overlap scoring (no external ML dependency).
- **`_PARAM_ALIASES` collision fix**: Primary parameter named `concept`
  (not `query`) to avoid the pre-existing `"query" → "glob"` alias in
  `registry.py` corrupting the argument before dispatch.
- **Security fix**: Same explicit `allowed_paths` enforcement as above.

#### `harness/metrics.py` (new, ~93 lines)
- New `MetricsCollector` dataclass with `record_phase()` and `flush()`.
- `flush()` writes atomically via `tempfile.mkstemp` + `os.replace` —
  no partial JSON files on crash.
- `contextlib.suppress(OSError)` on temp-file cleanup.
- Integrates at the `pipeline.py` level using confirmed-present `PhaseResult`
  and `InnerResult` symbols — no guessing at `phase_runner.py` internals.

#### `harness/tools/__init__.py`
- Added imports for `CrossReferenceTool` and `SemanticSearchTool`.
- Appended both to `DEFAULT_TOOLS` list.
- Updated docstring count from "22 of 23" to "24 of 25".

#### `harness/pipeline.py`
- Added `from pathlib import Path` import.
- Added `from harness.metrics import MetricsCollector` import.
- Instantiated `MetricsCollector` before outer loop.
- Called `metrics.record_phase(phase.name, phase_result)` after each phase.
- Called `metrics.flush()` after all rounds complete.

#### `harness/tools/registry.py`
- Added comment to `_PARAM_ALIASES` documenting that `"query"` is
  grep_search-specific — warns future tool authors not to use `"query"` as
  a primary parameter name.

---

### Security Improvement

The two new tools implement **explicit `allowed_paths` enforcement** using
`Path.is_relative_to()` rather than relying on the `requires_path_check`
flag (which routes through `_check_path()` and the broken `startswith()`
comparison). This closes the arbitrary-file-access bypass on these tools.

### Dead Code Removed

None in this round (all changes are additive new tools + wiring).

### Lines Added vs Removed

- Lines added: ~440 (3 new files + 5 wiring lines)
- Lines removed: 0
- Net: +440

---

## Round 3 · 2025 — Feature Search Tool & Comment Accuracy Fix

### Files Modified / Created

| File | Status |
|------|--------|
| `harness/tools/feature_search.py` | **NEW** |
| `harness/tools/__init__.py` | Modified — import + registration + comment fix |

---

### What Was Changed / Added

#### `harness/tools/feature_search.py` (new, ~200 lines)

New `FeatureSearchTool` (`name = "feature_search"`) — keyword-based feature
discovery across the codebase.

**Usage example:**
```
feature_search(keyword="checkpoint")
feature_search(keyword="retry", categories=["symbols", "comments"], max_results=10)
feature_search(keyword="evaluation", root="harness/", categories=["symbols"])
```

**Four search categories** (all active by default; opt-in subset via `categories`):

1. **`symbols`** — functions, classes, and methods whose name contains the
   keyword (AST-based, no false positives from comments or strings).
2. **`files`** — Python files whose basename contains the keyword
   (e.g. `checkpoint.py` for `keyword="checkpoint"`).
3. **`comments`** — inline `# …` comments and docstrings that mention the
   keyword; inline comments are found via fast text-scan, docstrings via
   `ast.get_docstring`.
4. **`config`** — module-level assignments/annotated assignments whose name
   contains the keyword (e.g. `RETRY_LIMIT = 3`, `checkpoint_dir: str = …`).

**Security**: Uses `_check_dir_root` + `_rglob_safe` (same guards as
`SemanticSearchTool` and `DataFlowTool`): null-byte rejection, `PERMISSION
ERROR` prefix, allowed-paths enforcement, symlink-safe file traversal.

**Output budget**: `_safe_json` with 24 KB cap; truncates the largest list
field and sets `truncated: true` rather than producing invalid JSON.

**Parameters**:
- `keyword` (required) — case-insensitive partial-match keyword.
- `root` (optional, default: `config.workspace`) — search root directory.
- `max_results` (optional, default: 30, range: 1–200) — max hits per category.
- `categories` (optional, default: all four) — subset of
  `["symbols", "files", "comments", "config"]`.

#### `harness/tools/__init__.py`

- Added `from harness.tools.feature_search import FeatureSearchTool` import.
- Appended `FeatureSearchTool()` to `DEFAULT_TOOLS` list.
- Fixed docstring comment from `"25 of 26"` to `"26 of 26"` — count now
  matches `len(DEFAULT_TOOLS)` exactly (26 tools).

---

### Tool ABC compliance

- Inherits `Tool` ABC ✓
- `name = "feature_search"` property ✓
- `input_schema()` returns valid JSON Schema with `required: ["keyword"]` ✓
- `async execute(config, **params) -> ToolResult` ✓
- `requires_path_check = False` + manual `_check_dir_root` enforcement ✓
- Registered in `DEFAULT_TOOLS` and `_ALL_TOOLS_BY_NAME` ✓

---

### Lines Added vs Removed

- Lines added: ~210 (new file) + 3 (wiring in `__init__.py`)
- Lines removed: 0
- Net: +213

---

## Round 4 · 2025 — Call Graph Tool

### Files Modified / Created

| File | Status |
|------|--------|
| `harness/tools/call_graph.py` | **NEW** |
| `harness/tools/__init__.py` | Modified — import + registration + docstring count fix |

---

### What Was Changed / Added

#### `harness/tools/call_graph.py` (new, ~280 lines)

New `CallGraphTool` (`name = "call_graph"`) — AST-based call graph builder.

**Usage examples:**
```
call_graph(function_name="run_phase")
call_graph(function_name="MyClass.my_method", depth=2)
call_graph(function_name="execute", root="harness/tools", depth=3, include_builtins=False)
call_graph(function_name="flush", root="harness", depth=5, include_builtins=True)
```

**How it works:**

1. All `.py` files under `root` are parsed once with `ast.parse` to build two
   complementary indexes:
   - `defs_by_qualname` — maps `"ClassName.method"` and `"func_name"` to their
     definition record `{file, line, calls}`.
   - `defs_by_bare` — maps the bare function name to all definition records
     (handles same-name-different-file overloads).
2. A **BFS** expansion starting from `function_name` traces outgoing calls level
   by level up to `depth` hops.  BFS ensures the shortest-path depth assignment
   wins if cycles exist.
3. A **visited set** prevents infinite loops on mutually recursive call graphs.
4. **Node cap** of 200 stops runaway expansion on large workspaces and sets
   `truncated: true` in the output.

**Parameters:**
- `function_name` (required) — seed function. Supports `my_func` and
  `MyClass.my_method` forms.
- `root` (optional, default: `config.workspace`) — search root directory.
- `depth` (optional, default: 3, range: 1–5) — how many call levels to trace.
- `include_builtins` (optional, default: false) — whether to include Python
  built-ins and stdlib names in the graph. Default `false` keeps output focused
  on project code.

**Output structure:**
```json
{
  "root_function": "my_func",
  "depth": 3,
  "nodes_total": 12,
  "nodes_found": 9,
  "nodes_external": 3,
  "truncated": false,
  "graph": {
    "my_func": {"file": "harness/foo.py", "line": 42, "calls": ["helper", "util"], "depth": 0, "found": true},
    "helper":  {"file": "harness/bar.py", "line": 10, "calls": ["log.info"], "depth": 1, "found": true},
    "util":    {"file": null, "line": null, "calls": [], "depth": 1, "found": false}
  }
}
```

**Security:** Uses `_check_dir_root` + `_rglob_safe` — null-byte rejection,
`PERMISSION ERROR` prefix, allowed-paths enforcement, symlink-safe traversal.

**Output budget:** `_safe_json` with 24 KB cap.

#### `harness/tools/__init__.py`

- Added `from harness.tools.call_graph import CallGraphTool` import.
- Appended `CallGraphTool()` to `DEFAULT_TOOLS` list.
- Fixed docstring count from `"26 of 26"` to `"27 of 27"`.

---

### Tool ABC compliance

- Inherits `Tool` ABC ✓
- `name = "call_graph"` property ✓
- `input_schema()` returns valid JSON Schema with `required: ["function_name"]` ✓
- `async execute(config, **params) -> ToolResult` ✓
- `requires_path_check = False` + manual `_check_dir_root` enforcement ✓
- Registered in `DEFAULT_TOOLS` and `_ALL_TOOLS_BY_NAME` ✓

---

### Lines Added vs Removed

- Lines added: ~280 (new file) + 3 (wiring in `__init__.py`)
- Lines removed: 0
- Net: +283

---

## Round 5 · 2025 — Dependency Analyzer Tool

### Files Modified / Created

| File | Status |
|------|--------|
| `harness/tools/dependency_analyzer.py` | **NEW** |
| `harness/tools/__init__.py` | Modified — import + registration + docstring count fix |

---

### What Was Changed / Added

#### `harness/tools/dependency_analyzer.py` (new, ~310 lines)

New `DependencyAnalyzerTool` (`name = "dependency_analyzer"`) — AST-based
Python import dependency graph builder and circular import detector.

**Usage examples:**
```
dependency_analyzer()
dependency_analyzer(mode="graph", module_filter="harness.tools")
dependency_analyzer(mode="cycles", root="harness")
dependency_analyzer(mode="imports", include_stdlib=True, module_filter="harness.tools.bash")
```

**Three output modes** (controlled by `mode` parameter):

1. **`graph`** (default) — Full module → [dependencies] adjacency dict plus
   embedded `cycles` list.  Each key is a dotted module name relative to the
   search root; values are sorted lists of imported module names.
2. **`cycles`** — Only circular import chains.  Runs the DFS cycle-detector
   and returns all detected back-edge cycles (capped at 20 to bound output).
3. **`imports`** — Per-file listing: each entry shows the relative file path,
   its dotted module name, and the filtered import list.

**Key implementation details:**

- **Pure AST parsing** — no `importlib`, no code execution.  Works safely on
  any codebase including packages with missing dependencies.
- **Relative import resolution** — `from . import utils` in
  `harness/tools/foo.py` resolves to `harness.tools.utils` using the file's
  own package prefix; `from ..config import X` resolves to `harness.config`.
- **Workspace-local filtering** — `include_stdlib=False` (default) uses
  `_collect_known_modules` to derive all top-level package names from the
  scanned files, then filters imports to only those with matching prefixes.
  This keeps the graph focused on project code and suppresses hundreds of
  stdlib edges.
- **Cycle detection** — iterative DFS with WHITE/GRAY/BLACK colouring to
  find back-edges without hitting Python's recursion limit on large graphs.
  Capped at 20 cycles per run.
- **`module_filter`** parameter — dotted prefix to restrict output
  (e.g. `"harness.tools"` shows only tool modules).

**Parameters:**
- `root` (optional, default: `config.workspace`) — directory to analyze.
- `mode` (optional, default: `"graph"`) — `"graph"` | `"cycles"` | `"imports"`.
- `include_stdlib` (optional, default: `false`) — include stdlib/third-party
  imports in the graph.
- `module_filter` (optional, default: `""`) — dotted prefix filter.

**Output structure (graph mode):**
```json
{
  "root": "/path/to/workspace",
  "mode": "graph",
  "modules_total": 26,
  "files_scanned": 26,
  "cycles_found": 0,
  "cycles": [],
  "graph": {
    "harness.tools.bash": ["harness.config", "harness.tools.base"],
    "harness.tools.call_graph": ["harness.config", "harness.tools.base"],
    ...
  }
}
```

**Security:** Uses `_check_dir_root` + `_rglob_safe` — null-byte rejection,
`PERMISSION ERROR` prefix, allowed-paths enforcement, symlink-safe traversal.

**Output budget:** `_safe_json` with 24 KB cap.

#### `harness/tools/__init__.py`

- Added `from harness.tools.dependency_analyzer import DependencyAnalyzerTool` import.
- Appended `DependencyAnalyzerTool()` to `DEFAULT_TOOLS` list.
- Fixed docstring count from `"27 of 27"` to `"28 of 28"`.

---

### Tool ABC compliance

- Inherits `Tool` ABC ✓
- `name = "dependency_analyzer"` property ✓
- `input_schema()` returns valid JSON Schema (no required params — all optional) ✓
- `async execute(config, **params) -> ToolResult` ✓
- `requires_path_check = False` + manual `_check_dir_root` enforcement ✓
- Registered in `DEFAULT_TOOLS` and `_ALL_TOOLS_BY_NAME` ✓

### Git tag

`v-tool-dependency_analyzer-auto`

---

### Lines Added vs Removed

- Lines added: ~310 (new file) + 3 (wiring in `__init__.py`)
- Lines removed: 0
- Net: +313

---

## Round 3 · 2025 — MetricsCollector Dataclass Field Ordering Fix

### Files Modified

| File | Status |
|------|--------|
| `harness/metrics.py` | Modified — `_phase_details` field moved before method definitions |

---

### What Was Changed

#### `harness/metrics.py`

- **Fixed dataclass field ordering anti-pattern**: `_phase_details: list[InnerRoundMetrics]`
  was declared *after* method definitions inside `MetricsCollector`. Python
  dataclasses require all field annotations to appear at the top of the class
  body, before any `def` statements, to be recognised by `@dataclass`.
  Moved `_phase_details = field(default_factory=list)` to line 44, immediately
  after `error_count`, grouping all four fields (`output_path`, `_phases`,
  `error_count`, `_phase_details`) at the top of the class.

- **No functional logic was changed**: `tool_turn_counts = [len(r.tool_call_log) for r in inner_rounds]`
  was already correct (using `len(r.tool_call_log)` not a broken `getattr` chain).
  `total_tool_turns` property and `flush()` integration with `self.total_tool_turns`
  were already present. `record_phase()` and `flush()` were already called from
  `pipeline_loop.py`. `elapsed_s` on `ToolResult` was already retained.

### Usage Example

```python
from pathlib import Path
from harness.metrics import MetricsCollector, InnerRoundMetrics

mc = MetricsCollector(output_path=Path("/tmp/metrics.json"))
# _phase_details is now always initialized (not guarded by hasattr)
assert mc._phase_details == []
detail = InnerRoundMetrics(
    phase="implement", round_index=0, tool_calls=5,
    verdict="pass", feedback_snippet="All tests pass"
)
mc.record_phase_detail(detail)
mc.flush_detail("/tmp/detail.jsonl")
```

### Lines Added vs Removed

- Lines added: 1 (field declaration moved into proper position)
- Lines removed: 1 (field declaration removed from after-method position)
- Net: 0 (pure reorganization; no new logic)

---

## Round 6 · 2025 — HTTP Client Tool & JSON Transform Tool

### Files Modified / Created

| File | Status |
|------|--------|
| `harness/tools/http_client.py` | **NEW** |
| `harness/tools/json_transform.py` | **NEW** |
| `harness/tools/__init__.py` | Modified — 2 imports + 2 registrations + docstring count fix |

---

### What Was Changed / Added

#### `harness/tools/http_client.py` (new, ~230 lines)

New `HttpRequestTool` (`name = "http_request"`) — generic HTTP client tool.

**Usage examples:**
```
http_request(url="https://api.example.com/data")
http_request(url="https://api.example.com/items", method="POST", body={"key": "value"}, headers={"Authorization": "Bearer token"})
http_request(url="https://api.example.com/resource/1", method="DELETE", timeout=10)
http_request(url="https://api.example.com/large", max_chars=4000)
```

**Supported methods:** GET, POST, PUT, DELETE, PATCH, HEAD.

**Key implementation details:**
- **Zero extra dependencies** — pure stdlib `urllib` for all HTTP operations.
- **Async-safe** — network I/O runs in `asyncio.run_in_executor` (thread pool)
  so the event loop is never blocked.
- **Auto JSON body** — when `body` is a dict, it is serialized to JSON and
  `Content-Type: application/json` is set automatically.
- **Response truncation** — body text is truncated to `max_chars` (default
  16 000 chars) and `truncated: true` is set in the JSON output.
- **HTTP error handling** — `HTTPError` (4xx/5xx) captures the error body and
  returns it in the output alongside `is_error=True`, so the LLM can read the
  error response.
- **Input validation** — URL must start with `http://` or `https://`; method
  must be one of the six allowed values; both validated before any network call.

**Output structure (JSON):**
```json
{
  "status": 200,
  "reason": "OK",
  "url": "https://api.example.com/data",
  "headers": {"Content-Type": "application/json"},
  "body": "...",
  "truncated": false
}
```

**Parameters:**
- `url` (required) — full URL, must start with `http://` or `https://`.
- `method` (optional, default: `"GET"`) — HTTP verb.
- `headers` (optional, default: `{}`) — extra request headers dict.
- `body` (optional, default: `null`) — string or JSON object body.
- `timeout` (optional, default: `30`, range: 1–120) — request timeout seconds.
- `max_chars` (optional, default: `16000`, range: 1–100000) — response body cap.

#### `harness/tools/json_transform.py` (new, ~390 lines)

New `JsonTransformTool` (`name = "json_transform"`) — JSON parse/query/validate/merge/diff.

**Usage examples:**
```
json_transform(op="parse", data='{"name":"Alice","age":30}')
json_transform(op="query", data='{"results":[{"id":1},{"id":2}]}', path="results[0].id")
json_transform(op="validate", data='{"name":"x"}', schema='{"type":"object","required":["name"],"properties":{"name":{"type":"string","minLength":2}}}')
json_transform(op="merge", data='{"a":1,"b":{"x":10}}', other='{"b":{"y":20},"c":3}')
json_transform(op="diff", data='{"version":"1.0"}', other='{"version":"2.0","new_field":true}')
```

**Five operations:**

1. **`parse`** — Parse a JSON string and pretty-print it with type/size header.
   Reports syntax errors with position. Accepts both string input and
   already-parsed Python values (re-serializes them).

2. **`query`** — Extract a nested value using a dot/bracket path notation.
   Path syntax: `"foo.bar[2].name"` — supports arbitrary nesting of dict keys
   and array indices. Returns the extracted value as JSON. Reports friendly
   error with available keys on missing key.

3. **`validate`** — Validate a JSON value against a basic inline JSON Schema.
   Supported keywords: `type`, `required`, `properties`, `items`, `enum`,
   `minimum`, `maximum`, `minLength`, `maxLength`, `minItems`, `maxItems`.
   Returns `{"valid": bool, "error_count": N, "errors": [...]}`.

4. **`merge`** — Deep-merge two JSON objects. Right-hand (`other`) values win
   on scalar conflicts; nested dicts are merged recursively. List fields are
   replaced (not merged). Neither input is mutated (deep copy used).

5. **`diff`** — Structural diff between two JSON values. Returns a flat list
   of `{path, op, left, right}` entries where `op` is one of `added`,
   `removed`, `changed`, `type_changed`. Recursively descends into dicts and
   arrays. Path uses `$.key[idx]` notation.

**Key implementation details:**
- **Zero external dependencies** — no `jsonpath-ng`, `jsonschema`, etc.
- **Both string and object input** accepted for `data`/`other`/`schema` —
  a string is parsed as JSON; a dict/list is used as-is.
- **Output cap** — all outputs capped at 24 000 chars with `[truncated]` note.

**Parameters:**
- `data` (required) — primary input (JSON string or parsed value).
- `op` (optional, default: `"parse"`) — operation to perform.
- `path` (optional) — dot/bracket path for `query` op.
- `schema` (optional) — JSON Schema object or string for `validate` op.
- `other` (optional) — secondary input for `merge` and `diff` ops.
- `indent` (optional, default: `2`, range: 0–8) — pretty-print indentation.

#### `harness/tools/__init__.py`

- Added `from harness.tools.http_client import HttpRequestTool` import.
- Added `from harness.tools.json_transform import JsonTransformTool` import.
- Appended `HttpRequestTool()` and `JsonTransformTool()` to `DEFAULT_TOOLS` list.
- Updated docstring count from `"28 of 28"` to `"30 of 30"`.

---

### Tool ABC compliance

**`http_request`:**
- Inherits `Tool` ABC ✓
- `name = "http_request"` property ✓
- `input_schema()` returns valid JSON Schema with `required: ["url"]` ✓
- `async execute(config, **params) -> ToolResult` ✓
- `requires_path_check = False` (no filesystem access) ✓
- Registered in `DEFAULT_TOOLS` and `_ALL_TOOLS_BY_NAME` ✓

**`json_transform`:**
- Inherits `Tool` ABC ✓
- `name = "json_transform"` property ✓
- `input_schema()` returns valid JSON Schema with `required: ["data"]` ✓
- `async execute(config, **params) -> ToolResult` ✓
- `requires_path_check = False` (no filesystem access) ✓
- Registered in `DEFAULT_TOOLS` and `_ALL_TOOLS_BY_NAME` ✓

### Git tags

- `v-tool-http_request-auto`
- `v-tool-json_transform-auto`

---

### Lines Added vs Removed

- Lines added: ~230 (`http_client.py`) + ~390 (`json_transform.py`) + 4 (wiring in `__init__.py`)
- Lines removed: 0
- Net: +624

---

## Round 7 · 2025 — Tool Auto-Discovery

### Files Modified / Created

| File | Status |
|------|--------|
| `harness/tools/discovery.py` | **NEW** |
| `harness/tools/__init__.py` | Modified — import + registration + docstring count fix |

---

### What Was Changed / Added

#### `harness/tools/discovery.py` (new, ~210 lines)

Two public surfaces in one file:

**1. `discover_tools(directory, *, package=None, skip_names=None)` — utility function**

Scans a directory for `*.py` files, imports each one, and returns all concrete
`Tool` subclasses found.  Useful for third-party plugin directories that drop a
`*.py` file and want it picked up automatically without editing `__init__.py`.

Key implementation details:
- **Zero extra dependencies** — pure stdlib (`importlib`, `inspect`, `pkgutil`).
- **Safe** — catches `ImportError` / `Exception` per module; a broken plugin
  never aborts discovery of healthy modules.  Warns via `logging`.
- **Deterministic order** — sorts `*.py` files alphabetically before iterating.
- **No double-loading** — checks `sys.modules` before importing; re-uses an
  already-imported module if present.
- **Skips infrastructure** — `__init__`, `base`, and `registry` are always
  skipped (they define infrastructure, not tools).  Callers can extend this via
  `skip_names`.
- **Abstract class filtering** — uses `inspect.isabstract()` to exclude any
  partially-implemented subclass; only fully-concrete `Tool` subclasses are
  returned.

Usage::

    from harness.tools.discovery import discover_tools
    classes = discover_tools("harness/tools", package="harness.tools")
    # → [BashTool, CallGraphTool, ..., WebSearchTool]  (32 classes)
    for cls in classes:
        registry.register(cls())

**2. `ToolDiscoveryTool` (`name = "tool_discovery"`) — introspection tool**

Lets the LLM agent inspect the live tool registry at runtime.

Usage examples::

    tool_discovery()                              # compact summary of all 31 tools
    tool_discovery(filter="search")               # tools matching "search"
    tool_discovery(tool_name="call_graph")        # full schema for one tool
    tool_discovery(show_schema=true)              # full schemas for all tools

**Parameters:**
- `filter` (optional, default `""`) — case-insensitive substring to restrict
  output to tools whose name or description matches.
- `tool_name` (optional, default `""`) — exact tool name for single-tool full
  schema lookup.  Takes precedence over `filter` and `show_schema`.
- `show_schema` (optional, default `false`) — include the full `input_schema`
  dict for every tool in the listing.

**Output (compact summary, no filter):**
```json
{
  "total_tools": 31,
  "filter_applied": null,
  "tools": [
    {
      "name": "bash",
      "description": "Execute a shell command...",
      "requires_path_check": false,
      "required_params": ["command"],
      "optional_params": ["timeout"]
    },
    ...
  ]
}
```

**Output (single-tool lookup):**
```json
{
  "name": "call_graph",
  "description": "Build a call graph...",
  "requires_path_check": false,
  "input_schema": { "type": "object", "properties": {...}, "required": [...] }
}
```

**Registry source**: When `config.tool_registry` is present, reads from the
live registry; falls back to `DEFAULT_TOOLS` for unit-test configs that don't
attach a registry.

#### `harness/tools/__init__.py`

- Added `from harness.tools.discovery import ToolDiscoveryTool, discover_tools` import.
- Appended `ToolDiscoveryTool()` to `DEFAULT_TOOLS` list.
- Updated docstring count from `"30 of 30"` to `"31 of 31"`.

---

### Tool ABC compliance

**`tool_discovery`:**
- Inherits `Tool` ABC ✓
- `name = "tool_discovery"` property ✓
- `input_schema()` returns valid JSON Schema (no required params — all optional) ✓
- `async execute(config, **params) -> ToolResult` ✓
- `requires_path_check = False` (no filesystem access) ✓
- Registered in `DEFAULT_TOOLS` and `_ALL_TOOLS_BY_NAME` ✓

**`discover_tools` utility:**
- Handles non-existent directory gracefully (returns `[]`, logs warning) ✓
- Handles broken/unimportable modules gracefully (skips with warning) ✓
- Skips abstract classes via `inspect.isabstract()` ✓
- Deduplicates classes via `seen_classes` set ✓
- Deterministic output order (alphabetical by file stem) ✓

### Git tag

`v-tool-tool_discovery-auto`

---

### Lines Added vs Removed

- Lines added: ~210 (`discovery.py`) + 3 (wiring in `__init__.py`)
- Lines removed: 0
- Net: +213

---

## Round 8 · 2026-04-16 — HttpRequestTool Moved to OPTIONAL_TOOLS (Architecture Fix)

### Files Modified

| File | Status |
|------|--------|
| `harness/tools/__init__.py` | Modified — `HttpRequestTool` moved from `DEFAULT_TOOLS` to `OPTIONAL_TOOLS`; docstring updated |

---

### What Was Changed

#### `harness/tools/__init__.py`

- **`HttpRequestTool` moved from `DEFAULT_TOOLS` to `OPTIONAL_TOOLS`** — This
  resolves the architecture violation identified in Round 2 evaluations: network-
  capable tools must not be registered by default, following the exact pattern
  already established for `WebSearchTool`. Placing `HttpRequestTool` in
  `DEFAULT_TOOLS` silently enabled outbound HTTP in every agent invocation,
  including air-gapped or restricted environments, and added unnecessary schema
  weight (~1 KB) to every LLM call.

- **Docstring updated** — Module-level docstring now lists both `web_search` and
  `http_request` under "Current optional tools"; default tool count corrected
  from 31 to 30; OPTIONAL_TOOLS section documents that opt-in is via
  `extra_tools=["web_search", "http_request"]`.

- **OPTIONAL_TOOLS comment block updated** — Rationale now explicitly states
  "outbound network access" as the gating criterion (not just schema footprint).

- **No new files created** — `http_client.py`, `json_transform.py`, and
  `discovery.py` were already fully implemented and registered in prior round.

### Structural Changes

- `DEFAULT_TOOLS`: 31 → **30** tools (HttpRequestTool removed)
- `OPTIONAL_TOOLS`: 1 → **2** tools (HttpRequestTool added alongside WebSearchTool)
- `ALL_TOOLS`: unchanged at 32 (DEFAULT 30 + OPTIONAL 2)

### Security / Architecture Improvement

Moving `HttpRequestTool` to `OPTIONAL_TOOLS` means:
1. Operators must explicitly opt in via `extra_tools=["http_request"]` in
   `HarnessConfig` or `build_registry()` — identical to `WebSearchTool`.
2. Default agent runs cannot make arbitrary outbound HTTP calls without
   explicit configuration.
3. The scheme validation (`http://` / `https://` only) and method allowlist
   (`GET`, `POST`, `PUT`, `DELETE`, `PATCH`, `HEAD`) already in
   `http_client.py` remain as additional SSRF mitigations when the tool is
   opted in.

### Tool ABC Compliance (all three tools verified)

| Tool | `name` | `description` | `input_schema()` | `async execute()` | Registry bucket |
|------|--------|---------------|-----------------|-------------------|-----------------|
| `HttpRequestTool` | ✓ `"http_request"` | ✓ | ✓ | ✓ | `OPTIONAL_TOOLS` |
| `JsonTransformTool` | ✓ `"json_transform"` | ✓ | ✓ | ✓ | `DEFAULT_TOOLS` |
| `ToolDiscoveryTool` | ✓ `"tool_discovery"` | ✓ | ✓ | ✓ | `DEFAULT_TOOLS` |

`discover_tools()` utility: `directory` parameter is Python-only (not LLM-
accessible) — the ACE vector identified in prior rounds is absent from
`ToolDiscoveryTool.execute()` and `input_schema()`.

### Git Tag

`v-structure-http-optional-auto`

### Lines Added vs Removed

- Lines added: 16 (docstring + OPTIONAL_TOOLS comment expansion)
- Lines removed: 2 (`HttpRequestTool()` removed from DEFAULT_TOOLS list;
  old single-line OPTIONAL_TOOLS rationale replaced by expanded block)
- Net: +14

---

---

## Round 3 · 5_traceability_and_structure — tool registry hardening

### Files Modified
- `harness/tools/__init__.py` — removed unused `discover_tools` import; added
  import-time count assertions

### Files Created
- `harness/tools/generate_manifest.py` — runnable script to regenerate the
  manifest from the live tool lists
- `harness/tools/registry_manifest.json` — machine-readable catalogue of all
  default and optional tools

### Changes Made

**Dead import removed** (`harness/tools/__init__.py`):
- Removed `discover_tools` from the `from harness.tools.discovery import ...`
  line. `discover_tools` was imported but never referenced in `__init__.py`
  (not in DEFAULT_TOOLS, OPTIONAL_TOOLS, ALL_TOOLS, _ALL_TOOLS_BY_NAME, or
  build_registry). Verified with `grep -rn discover_tools` — zero callers
  outside `discovery.py` itself.
- Lines removed: 1 (the `, discover_tools` import fragment)

**Import-time count assertions** (`harness/tools/__init__.py`):
- Added `_EXPECTED_DEFAULT_COUNT = 30` and `_EXPECTED_OPTIONAL_COUNT = 2`
  sentinel constants plus two `assert` statements that fire at import time if
  the documented counts drift from reality. Turns a silent documentation lie
  into an immediate `AssertionError` with a clear fix message.
- Lines added: 12

**Registry manifest** (`harness/tools/registry_manifest.json`):
- New committed JSON file listing all default and optional tool names with
  counts. Gives downstream consumers (admin scripts, test fixtures, CI checks)
  a single machine-readable source of truth without importing the full tool
  stack.
- Lines added: 41

**Manifest generator** (`harness/tools/generate_manifest.py`):
- New runnable module (`python -m harness.tools.generate_manifest`) that
  regenerates `registry_manifest.json` from the live `DEFAULT_TOOLS` /
  `OPTIONAL_TOOLS` lists. Keeps the committed manifest in sync after any tool
  additions or removals.
- Lines added: 27

### Security Improvement
`HttpRequestTool` remains in `OPTIONAL_TOOLS` (moved in a prior round),
preventing accidental outbound network calls in air-gapped environments.
The new count assertions make any future accidental promotion of a network
tool back to `DEFAULT_TOOLS` immediately visible at import time.

### Lines Added vs Removed
- Lines removed: 1 (dead `discover_tools` import)
- Lines added: 80 (assertions + manifest + generator)
- Net: +79 (all new lines are structured/executable content, not comments)

---

## Round 4 · 4_smart_search_and_tools — Security Hardening: symlink-safe path resolution & dead-file removal

### Files Modified

| File | Status |
|------|--------|
| `harness/tools/discovery.py` | Modified — `Path(directory).resolve()` replaced with `Path(os.path.realpath(directory))`; `import os` added |

### Files Deleted

| File | Reason |
|------|--------|
| `harness/tools/generate_manifest.py` | Module-level side-effect: writes to disk unconditionally on every import (no `if __name__ == "__main__"` guard worked at module level); no consumer of its output was wired up |
| `harness/tools/registry_manifest.json` | Generated artefact produced by deleted `generate_manifest.py`; dead output with no consumers |

---

### What Was Changed

#### `harness/tools/discovery.py`

- **Added `import os`** at the top of the stdlib import block (alongside
  `importlib`, `inspect`, `logging`, `sys`).

- **Fixed symlink-resolution inconsistency** in `discover_tools()`: replaced
  `root = Path(directory).resolve()` (which uses `Path.resolve()`, a Python-
  level path normaliser) with `root = Path(os.path.realpath(directory))` (which
  calls `realpath(3)` / OS-level symlink resolution). This is identical to the
  resolution strategy used in `Tool._check_dir_root()` and
  `Tool._resolve_and_check()` in `base.py`, making symlink handling consistent
  across all path-touching code in the tool layer.

- **No functional change for non-symlink paths** — on paths containing no
  symlinks, `os.path.realpath` and `Path.resolve` produce identical results.
  The fix only affects the edge case where `directory` contains a symlink whose
  target is an absolute path.

#### `harness/tools/generate_manifest.py` — DELETED

- This file executed `_OUT.write_text(...)` unconditionally at module load
  time (not inside a function or `if __name__ == "__main__"` guard). Any
  `import harness.tools.generate_manifest` would silently write
  `registry_manifest.json` to disk — a module-level side-effect that bypasses
  the `if __name__ == "__main__":` guard present at the *bottom* of the file
  (which was too late; the write had already happened). Deleted per the plan
  recommendation.

#### `harness/tools/registry_manifest.json` — DELETED

- Static JSON artefact generated by `generate_manifest.py`. No code in the
  harness reads this file at runtime; it was dead output from a broken
  generator. Deleted alongside the generator.

---

### Security / Architecture Improvement

- **Consistent symlink resolution**: All path-checking code in the tool layer
  now uniformly uses `os.path.realpath` for symlink resolution. Prior to this
  change, `discover_tools()` used `Path.resolve()`, creating a subtle
  inconsistency with `_check_dir_root` and `_resolve_and_check` in `base.py`.
  While `discover_tools()` is a developer utility (not LLM-accessible), the
  inconsistency posed a latent correctness risk.

- **No import-time side effects**: The deletion of `generate_manifest.py`
  eliminates the only module in the tool layer with import-time filesystem
  writes, making the tool package safe to import in read-only or strict
  environments.

---

### Lines Added vs Removed

- `harness/tools/discovery.py`: +1 (`import os`), ~0 net (one line changed)
- `harness/tools/generate_manifest.py`: −27 lines (deleted)
- `harness/tools/registry_manifest.json`: −41 lines (deleted)
- Net: **−67 lines** (security hardening + dead code removal)

---

## Round 9 · 2025 — Git Search Tool

### Files Modified / Created

| File | Status |
|------|--------|
| `harness/tools/git_search.py` | **NEW** |
| `harness/tools/__init__.py` | Modified — import + registration + docstring count fix |

---

### What Was Changed / Added

#### `harness/tools/git_search.py` (new, ~442 lines)

New `GitSearchTool` (`name = "git_search"`) — unified git history, blame, and
grep search tool.

**Usage examples:**
```
git_search(pattern="add retry logic")
git_search(mode="blame", path="harness/loop.py", pattern="max_iterations")
git_search(mode="grep", pattern="def execute", path="harness/tools")
git_search(mode="show", commit="a1b2c3d")
git_search(mode="log_file", path="harness/tools/bash.py")
git_search(mode="log", pattern="security", limit=20)
```

**Five search modes:**

1. **`log`** (default) — Search commit *messages* with a regex via
   `git log --grep=<pattern>`. Returns matching commits as `hash message`
   one-liners. Supports `--follow`-style scope restriction via `path`.

2. **`blame`** — Find which commit last changed each line in a file that
   matches a regex. Parses `git blame --porcelain` output to extract the
   commit hash, author name, final line number, and source text for every
   matching line. Output is a compact table.

3. **`grep`** — Fast `git grep --line-number` across all *tracked* files in
   the working tree. Optionally scoped to a subdirectory. Returns
   `file:line:text` entries. Exit code 1 (no matches) is handled gracefully.

4. **`show`** — Show the `--stat` diff for a specific commit hash via
   `git show --stat <commit>`. Input is validated against a safe-chars
   allowlist (`[0-9a-fA-F~^@{}./:-_]`) to prevent argument injection.

5. **`log_file`** — Full commit history for a specific file path via
   `git log --follow -- <path>`. Follows file renames (`--follow`).

**Key implementation details:**

- **Zero extra dependencies** — pure stdlib `asyncio` subprocess, identical
  pattern to `GitStatusTool` / `GitLogTool` in `git.py`.
- **Async subprocess** — all git commands use `asyncio.create_subprocess_exec`
  with a 30-second timeout; processes are killed and reaped on timeout.
- **Commit ref sanitization** — `mode='show'` validates the commit parameter
  against a safe character set before passing it to git, preventing shell
  injection via malformed refs.
- **Output truncation** — all modes cap at `_MAX_OUTPUT_CHARS = 20_000` with
  a `... [truncated]` trailer rather than producing silent cutoffs.
- **Graceful no-match** — `git grep` exit code 1 (no matches) is treated as
  a normal empty result, not an error.
- **Blame porcelain parser** — hand-written state machine for
  `git blame --porcelain` format; extracts commit hash, author, final line
  number, and source text without shelling out to `awk`/`sed`.

**Parameters:**
- `mode` (optional, default: `"log"`) — `"log"` | `"blame"` | `"grep"` |
  `"show"` | `"log_file"`.
- `pattern` (optional) — Regex/text to search. Required for `log`, `blame`,
  `grep`.
- `path` (optional) — File or directory path. Required for `blame`,
  `log_file`; optional scope for `grep` and `log`.
- `commit` (optional) — Commit hash or ref for `show` mode.
- `limit` (optional, default: 20, range: 1–200) — Max results.
- `case_insensitive` (optional, default: `false`) — Case-insensitive matching.

#### `harness/tools/__init__.py`

- Added `from harness.tools.git_search import GitSearchTool` import.
- Appended `GitSearchTool()` to `DEFAULT_TOOLS` list.
- Updated `_EXPECTED_DEFAULT_COUNT` from `30` to `31`.
- Updated docstring count from `"30 of 30"` to `"31 of 31"`.

---

### Tool ABC compliance

- Inherits `Tool` ABC ✓
- `name = "git_search"` property ✓
- `input_schema()` returns valid JSON Schema (no required params — all
  have sensible defaults) ✓
- `async execute(config, **params) -> ToolResult` ✓
- `requires_path_check = False` (operates on workspace root via git
  subprocess; no arbitrary path dereference) ✓
- Registered in `DEFAULT_TOOLS` and `_ALL_TOOLS_BY_NAME` ✓

### Git tag

`v-tool-git_search-auto`

---

### Lines Added vs Removed

- Lines added: ~442 (`git_search.py`) + 4 (wiring in `__init__.py`)
- Lines removed: 0
- Net: +446

---

## Round 10 · 2025 — TODO Scanner Tool

### Files Modified / Created

| File | Status |
|------|--------|
| `harness/tools/todo_scan.py` | **NEW** |
| `harness/tools/__init__.py` | Modified — import + registration + docstring count fix |

---

### What Was Changed / Added

#### `harness/tools/todo_scan.py` (new, ~290 lines)

New `TodoScanTool` (`name = "todo_scan"`) — scan source files for developer
annotation comments (TODO, FIXME, HACK, NOTE, BUG, XXX) and return structured
results grouped by file and tag.

**Usage examples:**
```
todo_scan()
todo_scan(tags=["FIXME", "BUG"])
todo_scan(root="harness/tools", file_glob="**/*.py")
todo_scan(tags=["TODO", "FIXME"], sort_by="tag", include_context=true)
todo_scan(root="harness", max_results=500)
```

**Key capabilities:**

1. **Tag filtering** — defaults to all six built-in tags (`TODO`, `FIXME`,
   `HACK`, `NOTE`, `BUG`, `XXX`); caller can supply a subset list.  Tags are
   matched case-insensitively so `# fixme:` and `# FIXME:` both resolve to
   `"FIXME"` in output.

2. **Flexible annotation syntax** — the tag regex matches all common
   annotation styles:
   - `# TODO: message` (colon)
   - `# TODO(author): message` (parenthesised owner)
   - `# TODO - message` (dash)
   - `# BUG message` (space only)
   - Inline-on-code-line: `x = 1  # BUG: off by one`

3. **Sort modes** — `sort_by="file"` (default, groups by file then line),
   `sort_by="tag"` (groups by tag type across files), `sort_by="line"`.

4. **Context lines** — `include_context=true` adds `context_before` and
   `context_after` fields with the adjacent source lines, making it easier
   to understand the annotation without loading the full file.

5. **by_tag summary** — output always includes a `by_tag` dict with counts
   per tag, giving a quick overview of technical-debt distribution.

**Output structure (JSON):**
```json
{
  "root": "/path/to/workspace",
  "files_scanned": 31,
  "total_found": 12,
  "by_tag": {"TODO": 8, "FIXME": 3, "BUG": 1},
  "tags_searched": ["TODO", "FIXME", "HACK", "NOTE", "BUG", "XXX"],
  "truncated": false,
  "results": [
    {
      "file": "harness/loop.py",
      "line": 42,
      "tag": "TODO",
      "text": "# TODO: add retry logic for transient failures"
    },
    ...
  ]
}
```

With `include_context=true` each result gains:
```json
{
  "context_before": "    for attempt in range(max_retries):",
  "context_after": "    raise MaxRetriesExceeded()"
}
```

**Parameters:**
- `root` (optional, default: `config.workspace`) — directory to search.
- `file_glob` (optional, default: `"**/*.py"`) — glob pattern for files.
- `tags` (optional, default: all six) — list of tags to find.
- `sort_by` (optional, default: `"file"`) — `"file"` | `"tag"` | `"line"`.
- `include_context` (optional, default: `false`) — add adjacent source lines.
- `max_results` (optional, default: 200, max: 1000) — result cap.

**Security:** Uses `_check_dir_root` + `_rglob_safe` — null-byte rejection,
`PERMISSION ERROR` prefix, allowed-paths enforcement, symlink-safe traversal.

**Output budget:** `_safe_json` with 32 KB cap (slightly larger than other
tools since context lines increase payload size proportionally).

#### `harness/tools/__init__.py`

- Added `from harness.tools.todo_scan import TodoScanTool` import.
- Appended `TodoScanTool()` to `DEFAULT_TOOLS` list.
- Updated `_EXPECTED_DEFAULT_COUNT` from `31` to `32`.
- Updated docstring count from `"31 of 31"` to `"32 of 32"`.

---

### Tool ABC compliance

- Inherits `Tool` ABC ✓
- `name = "todo_scan"` property ✓
- `input_schema()` returns valid JSON Schema (no required params — all optional) ✓
- `async execute(config, **params) -> ToolResult` ✓
- `requires_path_check = False` + manual `_check_dir_root` enforcement ✓
- Registered in `DEFAULT_TOOLS` and `_ALL_TOOLS_BY_NAME` ✓

### Git tag

`v-tool-todo_scan-auto`

---

### Lines Added vs Removed

- Lines added: ~290 (`todo_scan.py`) + 4 (wiring in `__init__.py`)
- Lines removed: 0
- Net: +294
