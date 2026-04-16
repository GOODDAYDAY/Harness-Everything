# CHANGELOG_AUTO.md — Auto-generated harness change log

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
