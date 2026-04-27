# File Operations

User stories covering all file-level capabilities: reading, writing, editing, patching, searching/replacing within files, moving, copying, deleting, and directory operations.

**Actors:**
- "As the agent" -- the agent performing file operations to accomplish tasks
- "As a file operation" -- security and integrity constraints on file access

---

## Reading Files

## US-01: As the agent, I need to read a specific portion of a file by line range so that I consume only the context I need

Reading an entire large file wastes context window budget. The agent specifies a starting line and a maximum number of lines to read, and receives only that slice with line numbers prepended for orientation.

### Acceptance Criteria
- Given a file and a line range, when the agent reads it, then only the requested lines are returned with 1-based line numbers
- Given a starting line beyond the end of the file, when the agent reads it, then an error is returned indicating the valid range
- Given a line count exceeding the file's length from the starting point, when the agent reads it, then lines up to the end of the file are returned without error
- Given an extremely large line count request, when the agent reads it, then the request is rejected to prevent resource exhaustion

## US-02: As the agent, I need to read multiple files in a single operation so that I save round-trips when exploring a codebase

When the agent needs to examine several files at once (e.g., a class and its test file), a batch read returns all requested file contents in one call instead of requiring sequential single-file reads.

### Acceptance Criteria
- Given a list of file paths, when the agent performs a batch read, then the contents of all valid files are returned in one result
- Given a list where some files do not exist, when the agent performs a batch read, then existing files are returned and missing files are reported as errors within the same result

## US-03: As the agent, I need the file read result to include the file name and total line count so that I can orient myself within the file

Each read result includes a header showing which file was read, which lines are being shown, and the total number of lines in the file. This lets the agent decide whether to read more and from where.

### Acceptance Criteria
- Given a file read, when the result is returned, then it includes the file name, the line range shown, and the total line count

---

## Writing Files

## US-04: As the agent, I need to create a new file or completely replace an existing file's contents so that I can produce new artifacts

Writing places the provided content into the specified path, creating any missing parent directories automatically. This is an all-or-nothing operation: the entire prior content is replaced.

### Acceptance Criteria
- Given a path that does not exist, when the agent writes content to it, then the file is created with the specified content and any missing parent directories are created
- Given a path that already exists, when the agent writes content to it, then the file's entire content is replaced with the new content
- Given a path outside the allowed directories, when the agent writes to it, then the operation is rejected with a permission error

## US-05: As the agent, I need to write multiple files in a single operation so that I save round-trips when producing several artifacts at once

A batch write creates or overwrites multiple files in one call. Each file is handled independently -- a failure in one does not prevent the others from being written.

### Acceptance Criteria
- Given a list of path-content pairs, when the agent performs a batch write, then each file is written independently and the result reports per-file success or failure

## US-06: As a file operation, I need file writes to be atomic so that a crash mid-write does not leave a corrupted file

Write operations first write to a temporary file in the same directory, then atomically replace the target. This ensures that readers never see a half-written file.

### Acceptance Criteria
- Given a write operation that succeeds, when the file is read back, then it contains exactly the written content
- Given a write operation that fails after the temporary file is created, when the failure is handled, then the temporary file is cleaned up and the original file is unchanged

---

## Editing Files

## US-07: As the agent, I need to perform a surgical search-and-replace within a single file so that I can modify code without rewriting the entire file

The agent provides the exact text to find and its replacement. By default the match must appear exactly once -- zero matches or multiple matches produce an error, forcing the agent to be precise.

### Acceptance Criteria
- Given a search string that appears exactly once, when the agent edits the file, then that occurrence is replaced and the file is saved
- Given a search string that appears zero times, when the agent edits the file, then an error is returned indicating no match was found
- Given a search string that appears multiple times without the replace-all flag, when the agent edits the file, then an error is returned showing how many times and on which lines the string appears
- Given a search string that appears multiple times with the replace-all flag, when the agent edits the file, then all occurrences are replaced

## US-08: As the agent, I need to edit multiple files in a single operation so that related changes across files are applied together

A batch edit applies independent search-and-replace operations to multiple files in one call. Each edit is applied independently -- a failure in one file does not prevent edits to other files.

### Acceptance Criteria
- Given a list of per-file edit instructions, when the agent performs a batch edit, then each file is edited independently and the result reports per-file outcomes

## US-09: As the agent, I need a dry-run mode for edits so that I can preview what would change before committing

When dry-run is enabled, the edit computes and reports the changes (which lines would be modified, added, or removed) without writing anything to disk.

### Acceptance Criteria
- Given a valid edit with dry-run enabled, when the agent previews it, then the preview shows the affected lines and no file is modified
- Given an invalid edit with dry-run enabled (e.g., no match), when the agent previews it, then the same error as a real edit is returned

---

## Patching Files

## US-10: As the agent, I need to apply a unified diff (patch) to files so that I can make complex, multi-hunk changes efficiently

The agent provides standard unified-diff text (the same format produced by diff tools). Each hunk is located in the target file and applied independently. The operation is all-or-nothing per file: either all hunks succeed or none are written.

### Acceptance Criteria
- Given a valid single-file patch with a target path, when the agent applies it, then all hunks are applied in order and the file is written
- Given a multi-file patch with file headers, when the agent applies it, then each file is patched independently and the result reports per-file outcomes
- Given a hunk that cannot be located at its declared line number, when the agent applies the patch, then a configurable fuzz tolerance searches nearby lines before giving up
- Given a patch with a hunk that cannot be located even with fuzz, when the agent applies it, then a clear error is returned identifying the failing hunk

## US-11: As the agent, I need a dry-run mode for patches so that I can verify hunks apply cleanly before writing

When dry-run is enabled, the patch reports which hunks would apply, the resulting line counts, and (for single-file patches) the full resulting file content, without modifying any file.

### Acceptance Criteria
- Given a valid patch with dry-run enabled, when the agent previews it, then the result shows hunk-by-hunk status and no file is modified

---

## Multi-file Search and Replace

## US-12: As the agent, I need to perform a regex-based search-and-replace across many files so that I can rename symbols or fix patterns codebase-wide in one operation

Unlike single-file editing, this operation applies a single regex substitution to every file matching a glob pattern. This makes bulk renames, import path updates, and repeated fixes efficient.

### Acceptance Criteria
- Given a regex pattern and a replacement string, when the agent runs the operation, then every matching file has substitutions applied and a per-file summary is returned
- Given a literal flag, when the agent runs the operation, then the pattern is treated as a plain string with no regex metacharacter interpretation
- Given no matching files, when the agent runs the operation, then a message reports zero matches without error

## US-13: As the agent, I need a safety cap on multi-file replacements so that a badly anchored regex cannot accidentally rewrite the entire codebase

A configurable maximum limits how many files can be modified in a single replace operation. If the match count exceeds this cap, the operation stops and reports that the cap was reached, requiring the agent to explicitly raise the limit or narrow the scope.

### Acceptance Criteria
- Given more matching files than the cap, when the agent runs the operation, then only files up to the cap are modified and a warning indicates how many additional files were skipped
- Given the agent explicitly sets a higher cap, when the operation runs, then up to that higher limit of files may be modified

## US-14: As the agent, I need a dry-run mode for multi-file replacement so that I can verify scope and correctness before committing changes

When dry-run is enabled, the operation reports which files would be changed, how many substitutions each would receive, and the first few matching lines per file, without writing anything.

### Acceptance Criteria
- Given a dry-run replacement, when the agent previews it, then per-file match counts and sample matching lines are shown and no file is modified

---

## File Operations (Delete, Move, Copy)

## US-15: As the agent, I need to delete a file so that I can remove obsolete or incorrect artifacts

The agent provides a file path, and the file is removed after security validation.

### Acceptance Criteria
- Given an existing file within allowed directories, when the agent deletes it, then the file is removed
- Given a non-existent file, when the agent tries to delete it, then an error is returned
- Given a file outside allowed directories, when the agent tries to delete it, then a permission error is returned

## US-16: As the agent, I need to move or rename a file so that I can reorganize the codebase structure

The agent provides source and destination paths. The file is moved atomically when possible, with a copy-then-delete fallback for cross-device moves.

### Acceptance Criteria
- Given valid source and destination paths, when the agent moves a file, then the file appears at the destination and no longer exists at the source
- Given a destination whose parent directory does not exist, when the agent moves a file, then the parent directory is created automatically
- Given a cross-device move, when the agent moves a file, then a copy-then-delete fallback succeeds transparently

## US-17: As the agent, I need to copy a file so that I can create a variant without losing the original

The agent provides source and destination paths. The file is duplicated, preserving metadata (timestamps, permissions).

### Acceptance Criteria
- Given valid source and destination paths, when the agent copies a file, then the destination contains the same content and the source is unchanged
- Given a destination whose parent directory does not exist, when the agent copies a file, then the parent directory is created automatically

---

## Directory Operations

## US-18: As the agent, I need to list the contents of a directory so that I can understand what files and subdirectories exist at a location

The agent provides a directory path and receives a list of entries with type indicators (file or directory) and file sizes.

### Acceptance Criteria
- Given a valid directory path, when the agent lists it, then all entries are returned sorted alphabetically with type and size information
- Given a non-existent or non-directory path, when the agent lists it, then an error is returned

## US-19: As the agent, I need to create a directory (including any missing parents) so that I can set up directory structures before writing files

The agent provides a directory path. All missing intermediate directories are created automatically.

### Acceptance Criteria
- Given a path with missing intermediate directories, when the agent creates the directory, then all levels are created
- Given a path that already exists as a directory, when the agent creates it, then no error is raised (idempotent)

## US-20: As the agent, I need to view a directory tree so that I can understand the hierarchical structure of a codebase at a glance

The agent provides a root directory and a maximum depth. The result is a visual tree showing the nesting structure with directory and file entries.

### Acceptance Criteria
- Given a root directory and a depth limit, when the agent requests the tree, then directories and files are displayed in a hierarchical tree format up to the specified depth
- Given hidden entries (names starting with dot), when the tree is rendered, then they are excluded from the output

---

## File Comparison

## US-21: As the agent, I need to see a unified diff between two files, or between a file and a text string, so that I can verify what changed or what still needs to change

The agent can compare two existing files, or compare a file against an expected text string. The output is standard unified-diff format, directly compatible with the patch tool.

### Acceptance Criteria
- Given two files, when the agent diffs them, then a unified diff is returned showing additions and removals
- Given a file and an expected text string, when the agent diffs them, then a unified diff is returned showing how the file differs from the expected state
- Given two identical inputs, when the agent diffs them, then a message explicitly states there are no differences
- Given a very large diff, when the output exceeds the line cap, then it is truncated with a notice

---

## Security Constraints

## US-22: As a file operation, I need to reject any path that resolves outside the allowed directories so that the agent cannot access unauthorized parts of the filesystem

Every file operation validates the target path against a configured list of allowed directories. Paths that resolve (after symlink resolution and normalization) outside these directories are rejected.

### Acceptance Criteria
- Given a path within the allowed directories, when it is validated, then it passes and the resolved absolute path is returned
- Given a path that resolves outside allowed directories (via "..", symlinks, or absolute path), when it is validated, then a permission error is returned
- Given a relative path, when it is validated, then it is resolved relative to the workspace root before checking

## US-23: As a file operation, I need to reject paths containing security-sensitive characters so that path injection attacks are prevented

Paths containing null bytes, homoglyph characters, or other attack vectors are rejected before any filesystem access occurs.

### Acceptance Criteria
- Given a path containing a null byte, when it is validated, then a security error is returned
- Given a path containing homoglyph characters, when it is validated, then a security error is returned

## US-24: As a file operation, I need to prevent time-of-check-to-time-of-use (TOCTOU) attacks so that a path verified as safe is still safe when accessed

Path validation and file access are performed atomically where possible. Symlinks are not followed unless explicitly allowed, and file identity is verified via inode after opening to detect path swaps.

### Acceptance Criteria
- Given a symlink within the workspace, when it is accessed without symlink resolution enabled, then the operation is rejected
- Given a file that is swapped between validation and access, when the atomic validation detects the mismatch, then the operation is rejected with a security violation error

## US-25: As a file operation, I need to enforce phase-scoped edit restrictions so that the agent can only modify files permitted by the current execution phase

When the configuration specifies edit-scope glob patterns for the current phase, write operations are restricted to files matching those patterns. Read operations are never restricted by phase scope.

### Acceptance Criteria
- Given phase edit globs and a write to a file matching the globs, when the operation runs, then it succeeds
- Given phase edit globs and a write to a file outside the globs, when the operation runs, then a scope error is returned identifying the mismatch
- Given no phase edit globs configured, when any write operation runs, then no scope restriction is applied
- Given a read operation on a file outside the phase edit globs, when the operation runs, then it succeeds (reads are unrestricted)
