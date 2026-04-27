# Search Tools

Tools for finding files and content across the workspace: `glob_search`, `grep_search`, `git_search` (optional), `feature_search`, `todo_scan`.

---

## glob_search

**Source**: `harness/tools/search_glob.py`

| Field | Value |
|-------|-------|
| name | `"glob_search"` |
| requires_path_check | `True` |
| tags | `frozenset({"search"})` |
| MAX_CANDIDATES | `5000` |

### Description

Search for files matching a glob pattern. Returns matching file paths relative to the root, sorted by modification time (most recent first).

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `pattern` | `string` | Yes | -- | Glob pattern (e.g. `**/*.py`) |
| `path` | `string` | No | `""` | Root directory (default: workspace) |
| `limit` | `integer` | Yes | -- | Max results to return |

Required: `["pattern", "limit"]`

### Execution Behavior

1. Resolve root via `_check_path`. Default to `config.workspace` if `path` is empty.
2. Resolve allowed-paths list: `[Path(p).resolve(strict=False) for p in config.allowed_paths]` or `[root]`.
3. Enumerate via `root.glob(pattern)`:
   - Only files (`m.is_file()`).
   - Cap at `MAX_CANDIDATES` (5000) candidates; set `capped=True` if exceeded.
   - Per-file: resolve, stat for mtime, check against allowed paths.
4. Sort by mtime descending. Truncate to `limit`.
5. Output: relative paths, one per line.
6. Header: `"Found {n} file(s) matching '{pattern}':"` with truncation/capped notes.
7. No matches: `"No files match pattern '{pattern}' in {root}"`.

---

## grep_search

**Source**: `harness/tools/search_grep.py`

| Field | Value |
|-------|-------|
| name | `"grep_search"` |
| requires_path_check | `True` |
| tags | `frozenset({"search"})` |
| MAX_GLOB_FILES | `5000` |

### Description

Search file contents using a regex pattern. Returns matching lines with file paths and line numbers. Supports filtering by file glob and optional context lines.

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `pattern` | `string` | Yes | -- | Regex pattern to search for |
| `path` | `string` | No | `""` | Directory or file to search (default: workspace) |
| `file_glob` | `string` | No | `""` | Only search files matching this glob (e.g. `*.py`). If empty, uses `**/*`. |
| `case_insensitive` | `boolean` | No | `false` | Case-insensitive search |
| `context_lines` | `integer` | Yes | -- | Lines of context before and after each match. 0 for just matching lines. |
| `limit` | `integer` | Yes | -- | Max total matches to return |

Required: `["pattern", "limit", "context_lines"]`

### Execution Behavior

1. Resolve root via `_check_path`.
2. Compile regex. If invalid, return error `"Invalid regex: ..."`.
3. Resolve allowed-paths list.
4. Collect files:
   - If root is a file: single file.
   - If root is a dir: `root.glob(file_glob or "**/*")`, capped at `MAX_GLOB_FILES` (5000). Per-file resolve and allowed-path check. Sorted.
5. Search each file:
   - Read text (`encoding="utf-8", errors="replace"`).
   - For each matching line: record with relative path and line number.
   - Context: lines `[i - context_lines, i + context_lines + 1]`, formatted as `"  {lineno:>5}: {line}"`.
   - No context: `"{rel}:{lineno}: {line.rstrip()}"`.
   - Stop at `limit`.
6. Header: `"Found {total} match(es) for /{pattern}/:"`.

---

## git_search (OPTIONAL)

**Source**: `harness/tools/git_search.py`

| Field | Value |
|-------|-------|
| name | `"git_search"` |
| requires_path_check | `False` |
| tags | `frozenset({"git"})` |

### Description

Search git history, blame, and tracked files. Five modes.

### Constants

| Constant | Value |
|----------|-------|
| `_VALID_MODES` | `("log", "blame", "grep", "show", "log_file")` |
| `_MAX_OUTPUT_CHARS` | `20_000` |

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `mode` | `string` (enum) | No | `"log"` | `log`, `blame`, `grep`, `show`, `log_file` |
| `pattern` | `string` | No | `""` | Regex/text pattern. Required for `log`, `blame`, `grep`. |
| `path` | `string` | No | `""` | File/directory path. Required for `blame` and `log_file`. |
| `commit` | `string` | No | `""` | Commit hash for `show` mode. |
| `limit` | `integer` | Yes | -- | Max results (clamped to [1, 200]) |
| `case_insensitive` | `boolean` | No | `false` | Case-insensitive pattern matching |

Required: `["limit"]`

### Modes

**log**: `git log --oneline --decorate=no --grep={pattern} -{limit}`. Optional `-i` for case insensitive, optional `-- {path}` scope.

**blame**: `git blame --porcelain -- {path}`. Parsed via `_parse_blame_porcelain` method. Each result: `{commit, author, line, text}`. Pattern filters source lines.

**grep**: `git grep --line-number -e {pattern}`. Exit code 1 = no matches (not error). Optional `-i` and `-- {path}`.

**show**: `git show --stat {commit}`. Commit reference sanitized against shell-special characters (only hex, `~^@{}./:-_` allowed).

**log_file**: `git log --oneline --decorate=no --follow -{limit} -- {path}`. `--follow` follows renames.

All modes use a 30-second timeout. Output truncated at `_MAX_OUTPUT_CHARS` (20000).

---

## feature_search

**Source**: `harness/tools/feature_search.py`

| Field | Value |
|-------|-------|
| name | `"feature_search"` |
| requires_path_check | `False` |
| tags | `frozenset({"search"})` |
| `_MAX_OUTPUT_BYTES` | `24_000` |

### Description

Find all code related to a feature or concept keyword. Searches four categories: symbol names, file names, comments/docstrings, and module-level config/constant names.

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `keyword` | `string` | Yes | -- | Feature keyword (case-insensitive, partial matches included) |
| `root` | `string` | No | `""` | Directory to search (default: workspace) |
| `max_results` | `integer` | Yes | -- | Max results per category (clamped to [1, 200]) |
| `categories` | `array[string]` | No | `["symbols", "files", "comments", "config"]` | Which categories to search. Enum values: `symbols`, `files`, `comments`, `config`. |
| `scoring` | `string` | No | `"substring"` | `substring` (match if keyword in name) or `token_overlap` (split keyword into tokens, score by matches) |

Required: `["keyword", "max_results"]`

### Categories

**files**: Python files whose basename contains the keyword (case-insensitive).

**symbols**: Functions/classes/methods whose name contains the keyword (or matches via token overlap). AST-based. Result: `{file, line, kind, name[, score]}`. Kinds: `class`, `async_function`, `function`.

**comments**: Inline comments (`# ...`) and docstrings mentioning the keyword. Text scan for comments; AST `ast.get_docstring` for docstrings. Result: `{file, line, kind, text[, symbol]}`. Kinds: `comment`, `docstring`.

**config**: Module-level assignments (`ast.Assign`, `ast.AnnAssign`) whose target name contains the keyword. Result: `{file, line, name, value_snippet}`. Value snippet capped at 60 chars.

### Scoring Modes

- **substring**: `kw_lower in identifier_lower`.
- **token_overlap**: Split keyword by spaces into tokens. Score = count of tokens found in identifier. Results sorted by score descending, then name.

### Output

JSON via `_safe_json(results, max_bytes=24_000)`. Structure: `{keyword, files_scanned, files?, symbols?, comments?, config?}`.

---

## todo_scan

**Source**: `harness/tools/todo_scan.py`

| Field | Value |
|-------|-------|
| name | `"todo_scan"` |
| requires_path_check | `False` |
| tags | `frozenset({"search"})` |

### Description

Scan source files for developer annotation comments: TODO, FIXME, HACK, NOTE, BUG, XXX.

### Constants

| Constant | Value |
|----------|-------|
| `_DEFAULT_TAGS` | `["TODO", "FIXME", "HACK", "NOTE", "BUG", "XXX"]` |
| `_DEFAULT_FILE_GLOB` | `"**/*.py"` |
| `_DEFAULT_MAX_RESULTS` | `200` |
| `_MAX_RESULTS_HARD_CAP` | `1000` |

### Tag Matching Pattern

`#\s*({tags})\s*[:()\-\s]` -- matches `# TODO`, `# TODO:`, `# TODO(user):`, `# FIXME -`, `# BUG `, etc. Case-insensitive by default.

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `root` | `string` | No | `""` | Root directory (default: workspace) |
| `file_glob` | `string` | No | `"**/*.py"` | Glob pattern for files to scan |
| `tags` | `array[string]` | No | all six | Annotation tags to find (case-insensitive) |
| `sort_by` | `string` (enum) | No | `"file"` | `file`, `tag`, or `line` |
| `include_context` | `boolean` | No | `false` | Include one line of context before and after each annotation |
| `max_results` | `integer` | Yes | -- | Max total annotations (clamped to [1, 1000]) |

Required: `["max_results"]`

### Sort Orders

- `file`: by `(file, line)`.
- `tag`: by `(tag, file, line)`.
- `line`: by `(file, line)` (same as `file`).

### Output

JSON via `_safe_json(result_dict, max_bytes=32_000)`. Structure:

```json
{
  "root": "/path/to/workspace",
  "files_scanned": 42,
  "total_found": 17,
  "by_tag": {"TODO": 10, "FIXME": 5, "BUG": 2},
  "tags_searched": ["TODO", "FIXME", "HACK", "NOTE", "BUG", "XXX"],
  "truncated": false,
  "results": [
    {
      "file": "harness/loop.py",
      "line": 42,
      "tag": "TODO",
      "text": "# TODO: add retry logic",
      "context_before": "    for attempt in ...",
      "context_after": "    raise MaxRetriesExceeded()"
    }
  ]
}
```

Context fields only present when `include_context=true`. Files collected via `_rglob_safe(search_root, file_glob, allowed, limit=2000)`.
