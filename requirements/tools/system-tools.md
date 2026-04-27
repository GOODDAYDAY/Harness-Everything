# System Tools

Execution, utility, and infrastructure tools: `bash`, `python_eval`, `http_client` (optional), `web_search` (optional), `test_runner`, `context_budget`, `scratchpad`, `lint_check`, `json_transform`, `discovery` (`tool_discovery`), `file_info`, `diff_files`, `find_replace`.

---

## bash

**Source**: `harness/tools/bash.py`

| Field | Value |
|-------|-------|
| name | `"bash"` |
| requires_path_check | `False` |
| tags | `frozenset({"execution"})` |

### Description

LAST RESORT tool. Execute a shell command only when no dedicated tool exists. Runs in the workspace directory with a default 60s timeout.

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `command` | `string` | Yes | -- | Shell command to run. Never use for reading source files. |
| `timeout` | `integer` | No | `60` | Timeout in seconds |

Required: `["command"]`

### Denylist Checking (`_denied_command`)

Checks the first token of **every** shell-chain segment, not just the overall command. Shell chain operators split by regex: `&&`, `||`, `;`, `|`, `&`.

Per-segment tokenization via `shlex.split`, with fallback to plain `.split()` on `shlex.ValueError`.

The denylist is sourced from `config.bash_denylist` (configurable).

### Shell Chain Detection

`_SHELL_CHAIN_RE = re.compile(r"&&|\|\||;|\||&")` -- splits the command into segments. Each segment's leading token is checked against the denylist.

---

## python_eval

**Source**: `harness/tools/python_eval.py`

| Field | Value |
|-------|-------|
| name | `"python_eval"` |
| requires_path_check | `False` |
| tags | `frozenset({"execution"})` |

### Description

Run a Python snippet in a subprocess and return structured output. Automatically prepends workspace to `sys.path`. Captures stdout, stderr, and the return value of the last expression separately.

### Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `_DEFAULT_TIMEOUT` | `30` | seconds |
| `_DEFAULT_MAX_OUTPUT` | `4_000` | characters |
| `_MAX_HARD_OUTPUT` | `20_000` | absolute ceiling |

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `code` | `string` | Yes | -- | Python code snippet to execute |
| `timeout` | `integer` | No | `30` | Timeout in seconds |
| `max_output_chars` | `integer` | No | `4000` | Max output characters |

Required: `["code"]`

### Execution Environment

- `cwd` = workspace
- `PYTHONPATH` = workspace (prepended to existing)
- `PYTHONUTF8=1`
- No stdin (`/dev/null`)
- `sys.argv = ['<harness_snippet>']`

### Return Value Extraction

When the snippet ends with an expression statement (not an assignment), the tool wraps that expression in `repr()` and prints it to a `__return_value__` marker channel on stderr.

### Wrapper Construction (`_build_wrapper`)

Parses snippet with `ast` to detect whether the last statement is an expression. If so, the expression is evaluated and its `repr()` is written to stderr under `__return_value__:`.

---

## http_client (OPTIONAL)

**Source**: `harness/tools/http_client.py`

| Field | Value |
|-------|-------|
| name | `"http_request"` |
| requires_path_check | `False` |
| tags | `frozenset({"network"})` |

### Description

Generic HTTP client. Supports GET, POST, PUT, DELETE, PATCH, HEAD. Uses stdlib `urllib` only.

### Constants

| Constant | Value |
|----------|-------|
| `_DEFAULT_TIMEOUT` | `30` seconds |
| `_DEFAULT_MAX_CHARS` | `16_000` characters |
| `_ALLOWED_METHODS` | `frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"})` |
| `_USER_AGENT` | `"HarnessHTTPClient/1.0 (stdlib urllib)"` |

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `url` | `string` | Yes | -- | URL to request |
| `method` | `string` | No | `"GET"` | HTTP method |
| `headers` | `object` | No | `{}` | Request headers |
| `body` | `string` | No | `""` | Request body |
| `json` | `object` | No | -- | JSON body (auto-sets Content-Type) |
| `timeout` | `integer` | No | `30` | Timeout in seconds |
| `max_chars` | `integer` | No | `16000` | Max response body characters |

### Execution

Runs synchronous `_do_request` in executor via `asyncio.to_thread`. Returns: `{status, reason, headers, body, truncated, url}`. Auto-sets `Content-Type: application/json` for dict bodies.

---

## web_search (OPTIONAL)

**Source**: `harness/tools/web_search.py`

| Field | Value |
|-------|-------|
| name | `"web_search"` |
| requires_path_check | `False` |
| tags | `frozenset({"network"})` |

### Description

Search the web via DuckDuckGo or fetch the text content of a URL. No API key required.

### Constants

| Constant | Value |
|----------|-------|
| `_DDG_URL` | `"https://html.duckduckgo.com/html/"` |
| `_DEFAULT_TIMEOUT` | `15` seconds |
| `_DEFAULT_MAX_RESULTS` | `8` |
| `_DEFAULT_MAX_CHARS` | `12_000` |
| `_MAX_SNIPPET_CHARS` | `300` |

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | `string` | Yes | -- | Search query or URL to fetch |
| `action` | `string` (enum) | No | `"search"` | `search` or `fetch` |
| `max_results` | `integer` | No | `8` | Max search results (search only) |
| `max_chars` | `integer` | No | `12000` | Max page text characters (fetch only) |

### search Action

Uses DuckDuckGo HTML endpoint via POST. Parses results via `_DDGParser` (HTML parser looking for `result__a` and `result__snippet` classes). Each result: title, URL, snippet. `_extract_ddg_url` unwraps DDG redirect URLs (`/l/?uddg=<encoded-url>`).

### fetch Action

Downloads URL via `_http_get`. HTML converted to plain text via `_HTMLTextExtractor`:
- Strips tags in `_SKIP_TAGS` (script, style, nav, footer, etc.).
- Inserts newlines for `_BLOCK_TAGS` (p, div, h1-h6, etc.).
- Collapses whitespace.
- Truncation: head/tail split with `"... [N chars omitted] ..."` notice.

---

## test_runner

**Source**: `harness/tools/test_runner.py`

| Field | Value |
|-------|-------|
| name | `"test_runner"` |
| requires_path_check | `True` |
| tags | `frozenset({"testing"})` |

### Description

Run pytest and return structured, parsed results. Invokes pytest with `-v --tb=short --no-header`. Returns structured summary with pass/fail/error/skip counts, per-test outcomes, condensed failure tracebacks.

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `test_path` | `string` | Yes | -- | Path to test file or directory |
| `pytest_args` | `array[string]` | No | `[]` | Extra pytest arguments (e.g. `["-x", "-k", "test_login"]`) |
| `format` | `string` (enum) | No | `"text"` | `text` or `json` |
| `timeout` | `integer` | No | `120` | Timeout in seconds |

### Parsing

Stdout parsed via `_parse_pytest_stdout`:
- Per-test outcomes from `<nodeid> PASSED/FAILED/...` lines.
- Counts from summary line `N passed, M failed in Xs`.
- Failure sections from `_____ test_name _____` headers.

### Text Format

```
pytest  tests/  [3 passed, 1 failed, 0 error, 0 skipped / 4 total]  (1.23s)  [FAIL]

  v  tests/test_foo.py::test_one
  x  tests/test_bar.py::test_broken

-- Failures --
FAILED  tests/test_bar.py::test_broken
    AssertionError: assert 1 == 2
```

---

## context_budget

**Source**: `harness/tools/context_budget.py`

| Field | Value |
|-------|-------|
| name | `"context_budget"` |
| requires_path_check | `False` |
| tags | `frozenset({"analysis"})` |

### Description

Check current token usage, remaining budget, and tool turn count. **Intercepted** by the LLM tool loop in `harness/core/llm.py` -- the loop injects live token counts, turn numbers, and scratchpad stats.

### Input Schema

No parameters. `{"type": "object", "properties": {}}`.

### Execution

The `execute` method is a **fallback** for direct registry calls (tests, scripts). Returns a stub message. The real implementation is in `core/llm.py` tool loop where this tool name is intercepted.

---

## scratchpad

**Source**: `harness/tools/scratchpad.py`

| Field | Value |
|-------|-------|
| name | `"scratchpad"` |
| requires_path_check | `False` |
| tags | `frozenset({"analysis"})` |
| `MAX_NOTE_CHARS` | `2000` |

### Description

Save an important finding as a persistent note. Notes survive conversation pruning and are re-injected into the system prompt on every turn. Notes are per-cycle. **Intercepted** by the LLM tool loop.

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `note` | `string` | Yes | -- | The note to save (max 2000 chars) |

Required: `["note"]`

### Execution

Fallback path (outside tool loop):
- Empty note: error.
- Truncation at `MAX_NOTE_CHARS` (2000) with `"... [truncated]"` suffix.
- Output: `"[scratchpad] note saved ({len} chars): {first_80}..."`.
- Metadata: `{"note": note}`.

---

## lint_check

**Source**: `harness/tools/lint_check.py`

| Field | Value |
|-------|-------|
| name | `"lint_check"` |
| requires_path_check | `True` |
| tags | `frozenset({"analysis"})` |
| `_MAX_OUTPUT_CHARS` | `20_000` |

### Description

Run ruff (Python linter) on specific files or directories. Returns structured diagnostics: file, line, column, rule code, message. Supports `--fix` mode.

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `paths` | `array[string]` | Yes | -- | Files or directories to lint |
| `fix` | `boolean` | No | `false` | Auto-fix safe issues (`ruff --fix`) |
| `select` | `string` | No | `""` | Comma-separated rule codes (e.g. `E,W`, `F401`, `I`) |

Required: `["paths"]`

---

## json_transform

**Source**: `harness/tools/json_transform.py`

| Field | Value |
|-------|-------|
| name | `"json_transform"` |
| requires_path_check | `False` |
| tags | `frozenset({"analysis"})` |
| `_MAX_OUTPUT_CHARS` | `24_000` |

### Description

JSON parse, query, validate, merge, and diff. No external dependencies.

### Operations (via `op` parameter)

| Operation | Description |
|-----------|-------------|
| `parse` | Parse JSON string and pretty-print. Reports error position on failure. |
| `query` | Extract nested value using dot/bracket path (e.g. `"foo.bar[2].name"`). |
| `validate` | Check JSON against a basic JSON Schema (supports `type`, `required`, `properties`, `items`, `minLength`, `maxLength`, `minimum`, `maximum`, `enum`). |
| `merge` | Deep-merge two JSON objects. Right-hand values win on key conflicts. |
| `diff` | Structural diff showing added/removed/changed leaf paths. |

### Path Query Syntax (`_parse_path`)

- `"foo"` -> `["foo"]`
- `"foo.bar"` -> `["foo", "bar"]`
- `"foo[0]"` -> `["foo", 0]`
- `"foo.bar[2].baz"` -> `["foo", "bar", 2, "baz"]`

---

## tool_discovery

**Source**: `harness/tools/discovery.py`

| Field | Value |
|-------|-------|
| name | `"tool_discovery"` |
| requires_path_check | `False` |
| tags | `frozenset({"analysis"})` |

### Description

Introspect the currently registered tool set at runtime. Returns each tool's name, description, required/optional parameters, and whether it requires a path check.

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `filter` | `string` | No | `""` | Filter tools by name substring |
| `tool_name` | `string` | No | `""` | Show details for a specific tool |
| `show_schema` | `boolean` | No | `false` | Show full parameter schema |

### `discover_tools()` Utility Function

Separate from the tool class. Scans a directory for Python modules and returns all concrete `Tool` subclasses found.

```python
def discover_tools(
    directory: str | Path,
    *,
    package: str | None = None,
    skip_names: set[str] | None = None,
) -> list[type[Tool]]
```

- Non-recursive scan of `*.py` files.
- Imports each module and finds `Tool` subclasses that are not abstract.
- Catches and logs `ImportError` per module; broken plugins don't abort discovery.

---

## file_info

**Source**: `harness/tools/file_info.py`

| Field | Value |
|-------|-------|
| name | `"file_info"` |
| requires_path_check | `True` |
| tags | `frozenset({"file_read"})` |
| `MAX_PATHS` | `100` |

### Description

Get file metadata (line count, byte size, last modified) WITHOUT reading the content. Use BEFORE reading a file to decide what limit/offset to pass.

### Input Schema

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `paths` | `array[string]` | Yes | File paths to inspect. Max 100 per call. |

Required: `["paths"]`

### Output Format

One line per file: `"  {lines:>6} lines  {bytes:>9} bytes  {mtime}  {path}"`.

Line counting via `_count_lines` -- reads file in chunks without loading entire file into memory.

---

## diff_files

**Source**: `harness/tools/diff_files.py`

| Field | Value |
|-------|-------|
| name | `"diff_files"` |
| requires_path_check | `True` |
| tags | `frozenset({"analysis"})` |

### Description

Show a unified diff between two files or between a file and a text string. Uses stdlib `difflib`. Output is valid input for `file_patch`.

### Constants

| Constant | Value |
|----------|-------|
| `_DEFAULT_MAX_LINES` | `500` |
| `_DEFAULT_CONTEXT` | `3` |

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `path_a` | `string` | Yes | -- | First file path |
| `mode` | `string` (enum) | No | `"file_vs_text"` | `file_vs_text` or `file_vs_file` |
| `text_b` | `string` | No | `""` | Text to compare against (for `file_vs_text`) |
| `path_b` | `string` | No | `""` | Second file path (for `file_vs_file`) |
| `context` | `integer` | No | `3` | Context lines around changes |
| `max_lines` | `integer` | No | `500` | Max output lines |

### Modes

- **file_vs_text** (default): Compare file `path_a` content against literal `text_b`.
- **file_vs_file**: Compare two files. Both must be in allowed paths.

### Output

Standard unified-diff format (`---`/`+++` headers, `@@` hunks). When no differences: explicit "no differences" message. Truncation notice when exceeding `max_lines`.

---

## find_replace

**Source**: `harness/tools/find_replace.py`

| Field | Value |
|-------|-------|
| name | `"find_replace"` |
| requires_path_check | `True` |
| tags | `frozenset({"file_write"})` |

### Description

Regex search-and-replace across multiple files. Efficient for symbol renames, import path updates, and bulk fixes.

### Constants

| Constant | Value |
|----------|-------|
| `_DEFAULT_FILE_GLOB` | `"**/*.py"` |
| `_DEFAULT_MAX_FILES` | `50` |
| `_MAX_PREVIEW_LINES` | `3` |
| `_MAX_OUTPUT_LINES` | `200` |

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `pattern` | `string` | Yes | -- | Python regex pattern (or literal when `literal=true`) |
| `replacement` | `string` | Yes | -- | Replacement string. Supports back-references (`\1`, `\g<name>`). |
| `path` | `string` | No | `""` | Root directory (default: workspace) |
| `file_glob` | `string` | No | `"**/*.py"` | File glob filter |
| `literal` | `boolean` | No | `false` | Treat pattern as plain string (`re.escape` internally) |
| `dry_run` | `boolean` | No | `false` | Preview without writing |
| `max_files_changed` | `integer` | No | `50` | Safety cap on files rewritten |
| `count` | `integer` | No | `0` | Max substitutions per file (0 = unlimited) |
| `case_insensitive` | `boolean` | No | `false` | Case-insensitive matching |

Required: `["pattern", "replacement"]`

### Safety

- `dry_run=true`: shows per-file match counts and first matching lines (up to `_MAX_PREVIEW_LINES = 3` per file) without writing.
- `max_files_changed` (default 50): hard cap on files rewritten in one call.
- `count`: limits substitutions per file (1 = first only, 0 = unlimited).
- Every candidate file validated against `config.allowed_paths`.
- Atomic per-file writes via `write_text`.

### Output

```
3 file(s) changed  (8 substitution(s) total)

harness/llm.py                    2 substitution(s)
harness/tools/registry.py         5 substitution(s)
harness/config.py                 1 substitution(s)
```
