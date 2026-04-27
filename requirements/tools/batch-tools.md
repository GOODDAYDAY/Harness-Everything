# Batch Tools

The batch tools (`batch_read`, `batch_edit`, `batch_write`) are the **primary** file operation tools. They supersede the single-file variants (`read_file`, `edit_file`, `write_file`) by performing multiple operations in a single LLM round-trip.

All three use `FileSecurity.atomic_validate_and_*` methods for path validation (same symlink / allowed_paths / phase-scope checks as the single-file tools). All three are decorated with `@enforce_atomic_validation`.

---

## batch_read

**Source**: `harness/tools/batch_read.py`

| Field | Value |
|-------|-------|
| name | `"batch_read"` |
| requires_path_check | `True` |
| tags | `frozenset({"file_read"})` |

### Description

Primary tool for reading files. Reads one or many files in a single call. Supports line ranges via `offset`/`limit`. The description instructs the LLM to always use this instead of `read_file` and to always specify `limit`.

### Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `MAX_FILES` | `50` | Maximum files per call |
| `MAX_LINES_PER_FILE` | `2000` | Maximum lines per file |
| `MAX_TOTAL_CHARS` | `500_000` | Total character budget for output |

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `paths` | `array[string]` | Yes | -- | List of file paths to read. Max 50 per call. |
| `limit` | `integer` | No | `2000` | Max lines per file. All files use the same limit. |
| `offset` | `integer` | No | `1` | Start reading from this line (1-based). |

Required: `["paths"]`

### Execution Behavior

1. **Input validation**:
   - `offset` must be >= 1.
   - `limit` must be in `[1, MAX_LINES_PER_FILE]`.
   - `paths` must be non-empty.
   - `len(paths)` must not exceed `MAX_FILES`.

2. **Parallel reads**: All files are read in parallel via `asyncio.gather`. Order is preserved.

3. **Per-file read (`_read_one`)**:
   - Validates path via `FileSecurity.atomic_validate_and_read(config, raw_path, require_exists=True, check_scope=False, resolve_symlinks=False)`.
   - Applies `handle_atomic_result` with `metadata_keys=("text", "resolved_path")`.
   - On error: returns formatted error section `"--- {path} ---\nERROR: {error}\n"`.
   - Splits text into lines, validates offset against total_lines.
   - Produces numbered output: `f"{line_number:>6}\t{line}"` (6-char right-aligned line number, tab, content).
   - Section header: `"--- {path} [lines {start}-{end} of {total}] ---"`.

4. **Output assembly**:
   - Accumulates sections until `total_chars >= MAX_TOTAL_CHARS`, then skips remaining files.
   - Summary line: `"Read {n_ok}/{total} file(s)[, {n_err} error(s)][, {skipped} skipped (...)]  [{total_chars} chars total]"`.
   - Metadata: `{"n_ok": n_ok, "n_err": n_err}`.

---

## batch_edit

**Source**: `harness/tools/batch_edit.py`

| Field | Value |
|-------|-------|
| name | `"batch_edit"` |
| requires_path_check | `True` |
| tags | `frozenset({"file_write"})` |

### Description

Primary tool for code modification. Applies up to 100 search/replace edits in a single call. Each edit is independent -- partial failures do not abort the batch.

### Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `MAX_EDITS` | `100` | Maximum edits per call |

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `edits` | `array[object]` | Yes | -- | List of edit operations (max 100) |

Each edit object:

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `path` | `string` | Yes | -- | File to edit (must exist) |
| `old_str` | `string` | Yes | -- | Exact text to find. Must be unique unless `replace_all=true`. Empty string not allowed. |
| `new_str` | `string` | Yes | -- | Replacement text |
| `replace_all` | `boolean` | No | `false` | Replace every occurrence if true; require exactly one match if false |

Required: `["edits"]`; per-edit required: `["path", "old_str", "new_str"]`

### Execution Behavior

1. **Input validation**:
   - `edits` must be non-empty.
   - `len(edits)` must not exceed `MAX_EDITS`.

2. **Grouping by path**: Edits are grouped by `path`. Same-path edits run serially (to avoid read-modify-write races). Different-path groups run in parallel via `asyncio.gather`.

3. **Per-edit execution (`_apply_one`)**:
   - Validates `path` non-empty, `old_str` non-empty.
   - Reads via `FileSecurity.atomic_validate_and_read(config, path, require_exists=True, check_scope=True, resolve_symlinks=False)`.
   - Counts occurrences of `old_str` in file text.
   - If count == 0: error "old_str not found".
   - If count > 1 and not replace_all: error "old_str appears {count} times".
   - Applies replacement: `text.replace(old_str, new_str)` or `text.replace(old_str, new_str, 1)`.
   - Writes via `FileSecurity.atomic_validate_and_write(config, resolved, new_text, require_exists=False, check_scope=True, resolve_symlinks=False)`.

4. **Output**:
   - Results are sorted back to declaration order.
   - Per-edit line: `"[{i}] {path}: OK ({n} replacement(s))"` or `"[{i}] {path}: ERROR: ..."`.
   - Summary: `"batch_edit: {n_ok}/{total} succeeded[, {n_err} failed]"`.
   - Metadata: `{"n_ok": n_ok, "n_err": n_err, "changed_paths": sorted(set)}`.

---

## batch_write

**Source**: `harness/tools/batch_write.py`

| Field | Value |
|-------|-------|
| name | `"batch_write"` |
| requires_path_check | `True` |
| tags | `frozenset({"file_write"})` |

### Description

Primary tool for creating or overwriting files. Writes up to 50 files in a single call. Partial failures are reported per-file.

### Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `MAX_FILES` | `50` | Maximum files per call |
| `MAX_TOTAL_CHARS` | `1_000_000` | 1 MB total write budget per call |

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `files` | `array[object]` | Yes | -- | List of files to write (max 50) |

Each file object:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `path` | `string` | Yes | Destination file path (parent directories created automatically; existing file overwritten) |
| `content` | `string` | Yes | Complete file content. Entire file is replaced. |

Required: `["files"]`; per-file required: `["path", "content"]`

### Execution Behavior

1. **Input validation**:
   - `files` must be non-empty.
   - `len(files)` must not exceed `MAX_FILES`.
   - Total content chars across all files must not exceed `MAX_TOTAL_CHARS`.

2. **Parallel writes**: All files are written in parallel via `asyncio.gather`. Same-path duplicates are handled by last-write-wins.

3. **Per-file write (`_write_one`)**:
   - Validates `path` non-empty.
   - Writes via `FileSecurity.atomic_validate_and_write(config, path, content, require_exists=False, check_scope=True, resolve_symlinks=False)`.
   - On success: `"[{i}] {path}: OK ({len(content)} bytes)"`.
   - On error: `"[{i}] {path}: ERROR: {error}"`.

4. **Output**:
   - Summary: `"batch_write: {n_ok}/{total} succeeded[, {n_err} failed]"`.
   - Metadata: `{"n_ok": n_ok, "n_err": n_err, "written_paths": [list of successfully written paths]}`.
