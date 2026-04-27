# File Operations Tools

Single-file read/write/edit tools plus delete, move, copy, and directory operations.

Note: `read_file` and `write_file` are now in OPTIONAL_TOOLS (superseded by `batch_read` and `batch_write`). `edit_file` remains in DEFAULT_TOOLS.

---

## read_file (OPTIONAL)

**Source**: `harness/tools/file_read.py`

| Field | Value |
|-------|-------|
| name | `"read_file"` |
| requires_path_check | `True` |
| tags | `frozenset({"file_read"})` |
| MAX_READ_LINES | `10000` |

### Description

Read a single file. The description directs the LLM to use `batch_read` instead for most cases.

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `path` | `string` | Yes | -- | Absolute or relative file path |
| `offset` | `integer` | No | `1` | Start reading from this line (1-based) |
| `limit` | `integer` | Yes | -- | Max number of lines to read |

Required: `["path", "limit"]`

### Execution Behavior

1. **Integer coercion**: `offset` and `limit` are defensively coerced to `int()` (LLM sometimes sends JSON integers as strings). Per-value error messages on failure.
2. **Validation**: `offset >= 1`, `limit >= 1`, `limit <= MAX_READ_LINES` (10000).
3. **Atomic read**: Uses `FileSecurity.atomic_validate_and_read(config, path, require_exists=True, check_scope=True, resolve_symlinks=False)`.
4. **Offset validation**: `offset > total_lines + 1` or `(total_lines == 0 and offset > 1)` returns error with specific message for empty files.
5. **Output format**: `"[{filename}] lines {start}-{end} of {total}\n{numbered_lines}"`. Line numbers are 6-char right-aligned with tab separator: `f"{lineno:>6}\t{line}"`.
6. **Metadata**: `{"lines": [(lineno, line_content), ...]}`.

---

## write_file (OPTIONAL)

**Source**: `harness/tools/file_write.py`

| Field | Value |
|-------|-------|
| name | `"write_file"` |
| requires_path_check | `True` |
| tags | `frozenset({"file_write"})` |

### Input Schema

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | `string` | Yes | File path to create or overwrite (directories created automatically) |
| `content` | `string` | Yes | Complete new file content. Replaces entire file. |

Required: `["path", "content"]`

### Execution Behavior

Delegates to `FileSecurity.atomic_validate_and_write(config, path, content, require_exists=False, check_scope=True, resolve_symlinks=False)`. Result processed via `handle_atomic_result(result, metadata_keys=())`.

---

## edit_file (DEFAULT)

**Source**: `harness/tools/file_edit.py`

| Field | Value |
|-------|-------|
| name | `"edit_file"` |
| requires_path_check | `True` |
| tags | `frozenset({"file_write"})` |

### Description

Surgical single-file search/replace. `old_str` must match exactly once unless `replace_all=true`. Supports `dry_run` mode for preview.

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `path` | `string` | Yes | -- | File path to edit (must exist) |
| `old_str` | `string` | Yes | -- | Exact text to replace. Must match exactly once. |
| `new_str` | `string` | Yes | -- | Replacement text. Use empty string to delete. |
| `replace_all` | `boolean` | No | `false` | Replace all occurrences |
| `dry_run` | `boolean` | No | `false` | Preview changes without writing |

Required: `["path", "old_str", "new_str"]`

### Execution Behavior

1. **Empty-string guard**: If `old_str == ""` and not `replace_all` and `new_str != ""`, returns error (empty string matches at every position).
2. **Read**: `FileSecurity.atomic_validate_and_read(config, path, require_exists=True, check_scope=True, resolve_symlinks=False)`.
3. **Phase scope check**: `_check_phase_scope(config, resolved)`.
4. **Change calculation** (`_calculate_changes` method):
   - Counts occurrences of `old_str`.
   - Generates `new_text` via `text.replace(old_str, new_str)` or `text.replace(old_str, new_str, 1)`.
   - Builds `changes_preview`: list of `(line_number, old_line, new_line)`.
5. **Match validation**:
   - 0 matches and not empty-to-empty: error "old_str not found in file".
   - `count > 1` and not `replace_all`: error with line numbers where `old_str` appears (up to first 5).
6. **Dry-run**: Returns preview showing per-line changes (`[ADD]`, `[REMOVE]`, or `'old' -> 'new'`). Metadata: `{"changes_preview": changes_preview}`.
7. **Write**: `FileSecurity.atomic_validate_and_write(config, path, new_text, require_exists=True, check_scope=True, resolve_symlinks=False)`.
8. **Success output**: `"Replaced {n} occurrence(s) in {resolved}"`.

---

## file_patch

**Source**: `harness/tools/file_patch.py`

| Field | Value |
|-------|-------|
| name | `"file_patch"` |
| requires_path_check | `True` |
| tags | `frozenset({"file_write"})` |

### Description

Apply a unified diff (patch) to files in the workspace. Accepts standard `diff -u` or `git diff` output. Supports multi-file patches and dry-run.

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `patch` | `string` | Yes | -- | Unified diff text (multi-file or bare hunks) |
| `path` | `string` | No | `""` | Target file path. Required when patch has no `+++ b/...` headers. |
| `fuzz` | `integer` | No | `3` | Line-offset fuzz tolerance. 0 for exact match, 5-10 for recently edited files, 15-20 for autogenerated files. |
| `dry_run` | `boolean` | No | `false` | Preview changes without writing |

Required: `["patch"]`

### Hunk Data Structure (`_Hunk`)

- `old_start`: 1-based line in original
- `old_count`: lines consumed from original
- `new_start`: 1-based line in patched result
- `new_count`: lines produced in result
- `lines`: raw diff lines (`+`, `-`, ` ` prefixed)
- Properties: `context_before`, `old_lines`, `new_lines`

### Hunk Application Algorithm (`_apply_hunk`)

1. Declared 0-based start: `declared = max(0, hunk.old_start - 1)`.
2. **Pure insertion** (`old_count == 0`): splice `new` lines at `declared`.
3. **Fuzz search**: For `delta` in `range(fuzz + 1)`, try `declared + delta` and `declared - delta`. Check if `file_lines[candidate:candidate+len(old)] == old`.
4. On match: replace old block with new lines.
5. On no match: raise `_PatchError` with excerpt.

### Multi-file Patch Parsing (`_split_by_file`)

Splits by `+++ b/<path>` headers or `diff --git a/.+ b/(.+)` headers. Returns `dict[path, patch_text]`.

### Execution Flow

1. Parse for multi-file headers (`_split_by_file`).
2. If multi-file: `_apply_multi` -- each file patched independently, errors collected.
3. If single-file: `path` required; `_apply_single`.
4. Hunks sorted by `old_start` before application.
5. Cumulative offset tracked across hunks.
6. Trailing newline convention preserved.

---

## delete_file

**Source**: `harness/tools/file_ops.py`

| Field | Value |
|-------|-------|
| name | `"delete_file"` |
| requires_path_check | `True` |
| tags | `frozenset({"file_write"})` |

### Input Schema

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | `string` | Yes | File path to delete |

### Execution

Delegates to `FileSecurity.atomic_validate_and_delete(config, path, check_scope=True, resolve_symlinks=False)`.

---

## move_file

**Source**: `harness/tools/file_ops.py`

| Field | Value |
|-------|-------|
| name | `"move_file"` |
| requires_path_check | `True` |
| tags | `frozenset({"file_write"})` |

### Input Schema

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `source` | `string` | Yes | Source file path |
| `destination` | `string` | Yes | Destination path |

### Execution

1. `FileSecurity.atomic_validate_and_move(config, source, destination, require_exists=True, check_scope=True, resolve_symlinks=False)`.
2. Validate and prepare parent directory of destination.
3. `os.rename(src, dst)` -- atomic on same filesystem.
4. **Cross-device fallback** (EXDEV): `shutil.copy2(src, dst)` then `os.unlink(src)`. If delete fails, cleans up the copy.

---

## copy_file

**Source**: `harness/tools/file_ops.py`

| Field | Value |
|-------|-------|
| name | `"copy_file"` |
| requires_path_check | `True` |
| tags | `frozenset({"file_write"})` |

### Input Schema

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `source` | `string` | Yes | Source file path |
| `destination` | `string` | Yes | Destination path |

### Execution

1. `FileSecurity.atomic_validate_and_copy(config, source, destination, require_exists=True, check_scope=True, resolve_symlinks=False)`.
2. Validate and prepare parent directory.
3. `shutil.copy2(src, dst)` via `asyncio.to_thread`.
4. Cross-device fallback: `shutil.copyfile` + `shutil.copystat`.
5. Specific error messages for ENOSPC, EACCES, ENOENT, EISDIR, ENOTDIR.

---

## list_directory

**Source**: `harness/tools/directory.py`

| Field | Value |
|-------|-------|
| name | `"list_directory"` |
| requires_path_check | `True` |
| tags | `frozenset({"file_read"})` |

### Input Schema

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | `string` | Yes | Directory path to list |

### Execution

1. Atomic directory validation via `_validate_directory_atomic`.
2. No phase-scope check (listing is a read, not a write).
3. Iterates `sorted(Path(resolved).iterdir())`.
4. Output format: directories as `"  [dir]  {name}/"`, files as `"  {size:>8}  {name}"`.

---

## create_directory

**Source**: `harness/tools/directory.py`

| Field | Value |
|-------|-------|
| name | `"create_directory"` |
| requires_path_check | `True` |
| tags | `frozenset({"file_write"})` |

### Input Schema

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | `string` | Yes | Directory path to create |

### Execution

1. `_validate_atomic_path(config, path, require_exists=False, directory=False)`.
2. Phase scope check.
3. `Path(resolved).mkdir(parents=True, exist_ok=True)`.

---

## tree

**Source**: `harness/tools/directory.py`

| Field | Value |
|-------|-------|
| name | `"tree"` |
| requires_path_check | `True` |
| tags | `frozenset({"file_read"})` |

### Input Schema

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `path` | `string` | Yes | -- | Root directory |
| `max_depth` | `integer` | No | `3` | Max recursion depth. 1-2 for overview, 3-4 for detail. |

Required: `["path"]`

### Execution

1. Atomic directory validation.
2. No phase-scope check (read operation).
3. Recursive `_walk` method:
   - Filters hidden entries (names starting with `.`) before computing connectors.
   - Uses `` `-- `` for last visible entry, `|-- ` otherwise.
   - Directories: recurse with extension `"    "` (last) or `"|   "` (not last).
   - Stops at `max_depth`.
