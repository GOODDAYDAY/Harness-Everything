# S-03 Path Security

> Comprehensive path validation and TOCTOU-safe file operations.

**Source**: `harness/core/security.py`

---

## Background

The security module provides defence-in-depth path validation for the harness tool system. Every tool that accesses the filesystem must validate paths through these checks before performing I/O. The module addresses several attack vectors: null-byte truncation, control-character injection, Unicode homoglyph spoofing, symlink escape, hardlink escape, and time-of-check-to-time-of-use (TOCTOU) race conditions.

---

## Requirements

### Null Byte Validation

**F-01 `validate_path_no_null_bytes(path: str) -> str | None`**

- Returns an error message if the path contains `"\x00"`.
- Returns `None` if the path is clean.
- Error format: `"PERMISSION ERROR: path contains null byte: {path!r}"`.
- Rationale: null bytes truncate the path string at the OS syscall boundary, causing prefix checks to pass on the Python string while the OS operates on a shorter path.

### Control Character Validation

**F-02 `validate_path_no_control_chars(path: str) -> str | None`**

- Checks for all C0 control characters (`U+0000` through `U+001F`) plus DEL (`U+007F`).
- Each character has a named description (e.g., `"U+0000 (NULL)"`, `"U+0009 (TAB)"`, `"U+001B (ESC)"`).
- Returns the first match found: `"PERMISSION ERROR: Path contains disallowed control character: {description}"`.
- Returns `None` if no control characters are found.
- The full list: NULL, SOH, STX, ETX, EOT, ENQ, ACK, BEL, BS, TAB, LF, VT, FF, CR, SO, SI, DLE, DC1, DC2, DC3, DC4, NAK, SYN, ETB, CAN, EM, SUB, ESC, FS, GS, RS, US, DEL.

### Unicode Homoglyph Validation

**F-03 `validate_path_no_homoglyphs(path: str, config: HarnessConfig | None = None) -> str | None`**

Two complementary strategies:

1. **NFKC normalisation check**: if `unicodedata.normalize("NFKC", path) != path`, the path contains compatibility characters (superscripts, ligatures, full-width letters, combining diacritics) that could spoof ASCII-looking paths. Error: `"PERMISSION ERROR: Path contains Unicode homoglyphs or compatibility characters that change under NFKC normalisation: {path!r}"`.

2. **Explicit homoglyph blocklist**: checks each character against a blocklist. When `config` is provided and `config.homoglyph_blocklist` is non-empty, uses that blocklist. Otherwise falls back to a built-in minimal set of 8 characters:
   - `U+0430` Cyrillic small a
   - `U+04CF` Cyrillic small palochka
   - `U+0391` Greek capital alpha
   - `U+03B1` Greek small alpha
   - `U+041E` Cyrillic capital O
   - `U+043E` Cyrillic small o
   - `U+2044` Fraction slash
   - `U+FF0F` Full-width solidus

   Error format: `"PERMISSION ERROR: Path contains Unicode homoglyphs: {description} (U+{codepoint:04X})"`.

### Composite Validation

**F-04 `validate_path_security(path: str, config: HarnessConfig | None = None) -> str | None`**

Runs all checks in security-critical order:
1. `validate_path_no_null_bytes(path)` -- most critical, can truncate paths at OS level.
2. `validate_path_no_control_chars(path)` -- can cause unexpected behaviour.
3. `validate_path_no_homoglyphs(path, config)` -- visual spoofing attacks.

Returns the first error found, or `None` if all checks pass.

### Scope Validation

**F-05 `validate_path_scope(config, resolved_path) -> tuple[bool, str | None]`**

- Async function.
- Resolves `resolved_path` via `Path(...).resolve()` and `config.workspace` via `Path(...).resolve()`.
- Returns `(True, None)` if the resolved path is within workspace (workspace is in `resolved.parents` or `resolved == workspace`).
- Returns `(False, "PERMISSION ERROR: Path not allowed: ...")` otherwise.
- Returns `(False, "PERMISSION ERROR: Path validation error: ...")` on exception.

### Hardlink Rejection

**F-06 `_validate_file_within_allowed_paths(file_fd: int, allowed_paths: list[Path]) -> bool`**

Three-tier validation strategy on an open file descriptor:

**Hardlink check (pre-tier)**: Uses `os.fstat(file_fd)` to get `st_nlink`. If `st_nlink > 1`, returns `False` immediately -- files with multiple hardlinks are rejected because a hardlink inside an allowed directory could point to a file outside allowed paths. Cannot efficiently enumerate all hardlink locations, so conservatively rejects.

**Tier 1 (Linux)**: Reads `/proc/self/fd/{file_fd}` symlink, resolves to real path, checks if it is relative to any allowed path.

**Tier 1.5 (macOS)**: Uses `fcntl.fcntl(file_fd, F_GETPATH, buf)` with `F_GETPATH = 50` and a 4096-byte buffer. Decodes the result and checks if the real path is within allowed paths.

**Tier 2 (cross-platform)**: Walks all files under `allowed_paths` with `os.walk()`. For each file, compares `(st_dev, st_ino)` from `os.lstat()` against the opened file's `(st_dev, st_ino)`. Uses `lstat` (not `stat`) so symlinks within allowed paths are compared by their own inode, not the target's.

**Tier 3 (default)**: Returns `False` if no tier could validate the file.

### TOCTOU Protection

**F-07 `_validate_dir_fd_consistent(dir_fd: int, parent_dir: Path) -> bool`**

- Compares `os.fstat(dir_fd)` against `os.stat(str(parent_dir))`.
- Returns `True` iff `(st_dev, st_ino)` match.
- Returns `False` on any `OSError`.

**F-08 `_is_parent_directory_symlink(dir_fd: int, child_name: str) -> bool`**

- Attempts to open `child_name` relative to `dir_fd` with `O_PATH | O_NOFOLLOW | O_CLOEXEC`.
- If `ELOOP` error, the target is a symlink -- returns `True`.
- Fallback: reads `/proc/self/fd/{dir_fd}` and checks `os.path.islink()`.
- Conservative default: returns `True` if determination fails.

**F-09 `_validate_filename_component(filename: str) -> str | None`**

- Rejects filenames containing `"/"` or `"\\"`.
- Rejects filenames where any component (split by `/` after normalising `\\` to `/`) equals `".."`.
- Error: `"PERMISSION ERROR: Path traversal detected in filename"`.

### Atomic File Reading

**F-10 `read_file_atomically(path: Path, allowed_paths: list[Path]) -> str | None`**

Complete TOCTOU-safe file read procedure:

1. Convert to absolute path (without resolving symlinks).
2. Extract `parent_dir` and `filename` from the absolute path.
3. Validate filename component via `_validate_filename_component()`.
4. Open parent directory with secure flags (`O_PATH | O_DIRECTORY | O_CLOEXEC`), falling back to `O_RDONLY | O_NOFOLLOW`, then `O_RDONLY`.
5. Validate directory FD consistency via `_validate_dir_fd_consistent(dir_fd, parent_dir)`.
6. Resolve real parent directory path; verify it is within `allowed_paths` (resolving allowed paths via `os.path.realpath()` to handle OS-level symlinks like macOS `/var` -> `/private/var`).
7. Open target file relative to `dir_fd` with `O_RDONLY | O_NOFOLLOW | O_CLOEXEC`. On `ELOOP` (target is symlink), retry without `O_NOFOLLOW`.
8. Validate opened file is within allowed paths via `_validate_file_within_allowed_paths()` -- prevents hardlink attacks.
9. Verify opened file's `(st_dev, st_ino)` matches `(parent_real / filename).stat()` -- ensures no swap between open and validation.
10. Read content via `os.fdopen(file_fd, 'r', encoding='utf-8', errors='replace')`.
11. Returns `None` on any security check failure or OS error. Cleans up file descriptors in a `finally` block.

---

## Implementation Approach

- Pure functions for validation checks (null bytes, control chars, homoglyphs, composite).
- Low-level `os.open()` / `os.fstat()` / `os.fdopen()` for TOCTOU-safe operations.
- Platform-specific tiers for file validation (`/proc`, `fcntl`, `os.walk` fallback).
- All functions log warnings on security violations.

---

## Expected Effects

- Null-byte truncation attacks are blocked at the earliest possible point.
- Control characters that could cause unexpected path handling are rejected.
- Unicode homoglyph spoofing (Cyrillic/Greek look-alikes, full-width slashes) is detected via both NFKC normalisation and explicit blocklist.
- Symlink-escape attacks are prevented by resolving real paths before containment checks.
- Hardlink-escape attacks are prevented by rejecting files with `st_nlink > 1`.
- TOCTOU races are mitigated by using `dir_fd`-relative opens and device/inode verification.
- macOS `/var` -> `/private/var` symlinks are handled by resolving allowed paths before comparison.

---

## Acceptance Criteria

- Paths containing null bytes are rejected before any OS syscall.
- All 33 control characters (C0 + DEL) are individually detected and named.
- NFKC-differing paths are rejected even if no explicit blocklist character matches.
- `read_file_atomically()` returns `None` for files outside allowed paths, files with multiple hardlinks, and paths with directory traversal.
- `_validate_dir_fd_consistent()` detects directory FD swaps.
- File descriptors are always cleaned up, even on error paths.
