# File Operations

This document specifies what the agent must be able to do with files and what safety properties those operations must guarantee.

## Context

The agent's primary output is modified source code. Every meaningful cycle involves reading files to understand context, editing files to make changes, and sometimes creating, moving, or deleting files as part of refactoring. File operations are the highest-volume tool category and the highest-risk: a botched write can corrupt a codebase, a path traversal can escape the workspace.

## Concern 1: Reading files

### R-FILE-01: Read with line-range control

The agent must be able to read a file and specify which portion to retrieve (start line, number of lines). Returning entire files by default wastes context budget; the agent needs to read only the section it cares about.

**Why:** LLM context windows are finite and expensive. A 3,000-line file read in full occupies context that could hold multiple smaller, targeted reads. Line-range control lets the agent read 50 lines around a function definition instead of the whole module.

**Acceptance criteria:**
- The agent can request lines 100-150 of a file and receive exactly those lines.
- Line numbers in the output are 1-based and match the file's actual line numbers.
- Requesting an offset beyond the file's length returns the available tail, not an error.

### R-FILE-02: Batch reading

The agent must be able to read multiple files in a single tool call. Each file in the batch uses the same line-range parameters and each is validated independently -- a failure on one file does not abort the others.

**Why:** Without batch reading, exploring a codebase costs one LLM round-trip per file. Reading 5 related files (a module, its tests, its config, two callers) takes 5 turns. Batch reading reduces this to 1 turn, which is critical because tool turns are the primary bottleneck in agent throughput.

**Acceptance criteria:**
- A single call can read up to 50 files.
- If file 3 of 5 does not exist, files 1, 2, 4, 5 are still returned.
- The per-file failure is reported inline (not as a top-level error that masks the successful reads).
- Total output is capped to prevent a large batch from flooding the context window.

### R-FILE-03: File metadata without content

The agent must be able to retrieve file metadata (line count, byte size, last-modified time) without reading the file's content. This supports informed decisions about what line range and limit to use for a subsequent read.

**Why:** The agent cannot know whether to request `limit=50` or `limit=500` without knowing the file's size. A metadata-first call lets it calibrate its reads and avoid either under-reading (missing context) or over-reading (wasting budget).

**Acceptance criteria:**
- Metadata retrieval accepts multiple paths in one call.
- The result includes at minimum: line count, byte size, and modification time.
- The result does not include file content.

## Concern 2: Writing files

### R-FILE-04: Full-file write with atomic semantics

The agent must be able to create a new file or completely overwrite an existing file. The write must be atomic: the file either contains the new content entirely or remains unchanged. No partial writes that leave a truncated or corrupted file.

**Why:** A crash or timeout during a write must not leave the workspace in a half-written state. If the agent writes a 200-line module and the process is killed at line 100, the file should still contain either the old content or nothing (for a new file), not a truncated fragment that fails to parse.

**Acceptance criteria:**
- Writing to a path whose parent directory does not exist creates the parent directory automatically.
- If the write fails mid-way (simulated by e.g., disk full), the original file content is preserved.
- Atomic write uses a temp-file-then-rename pattern, not in-place truncation.

### R-FILE-05: Batch writing

The agent must be able to write multiple files in a single tool call. Writes to different paths run in parallel since they do not race. Per-file failures are reported without aborting the batch.

**Why:** Scaffolding a new feature (module, test file, config, __init__ update) involves creating several files. Doing this one file per turn wastes turns. Batch writing handles the entire scaffolding in one round-trip.

**Acceptance criteria:**
- A single call can write up to 50 files.
- Total content size per batch is capped to prevent memory exhaustion.
- If file 2 of 4 fails (e.g., path outside allowed scope), files 1, 3, 4 are still written.

## Concern 3: Editing files (surgical modification)

### R-FILE-06: Search-and-replace editing

The agent must be able to modify a file by specifying an exact string to find and its replacement. The match must be character-for-character exact (including whitespace and indentation). By default, the search string must appear exactly once in the file -- zero matches or multiple matches are errors.

**Why:** Full-file write requires the agent to reproduce the entire file content, including parts it did not change. This wastes output tokens and risks accidentally dropping lines. Search-and-replace lets the agent specify only the changed portion, keeping edits minimal and reviewable.

**Acceptance criteria:**
- An edit where `old_str` does not appear in the file returns an error, not a no-op.
- An edit where `old_str` appears more than once returns an error (unless `replace_all` is set).
- Setting `replace_all=true` replaces every occurrence.
- A `dry_run` mode shows what would change without writing to disk.

### R-FILE-07: Batch editing

The agent must be able to apply multiple search-and-replace edits in a single tool call, potentially across multiple files. Edits are independent: one failing edit does not abort the rest. Edits to the same file run serially to avoid read-modify-write races; edits to different files may run in parallel.

**Why:** A coherent change often touches multiple files (rename a function in its definition, all call sites, and tests). Doing this one edit per turn costs N turns. Batch editing lands the entire change in one round-trip, and the serial-within-file guarantee prevents race conditions.

**Acceptance criteria:**
- A single call can apply up to 100 edits.
- Edits to the same file are applied in the order they appear in the batch, each seeing the result of the previous one.
- Partial failures are reported per-edit with the edit index, not as a single top-level error.

### R-FILE-08: Structured diff patching

The agent must be able to apply unified diff patches (the format produced by `git diff` or `diff -u`) to files. Each hunk is applied independently with configurable line-offset fuzz tolerance. Application is all-or-nothing per file: either all hunks for a file succeed, or none are written.

**Why:** When the agent has a clear picture of the before/after state (e.g., from planning or from a previous diff output), a unified patch is a more natural format than search-and-replace for multi-hunk changes. It also interoperates with git workflows.

**Acceptance criteria:**
- A multi-file patch applies each file section independently.
- If a hunk cannot be located at its declared line number, the tool searches nearby lines (within fuzz tolerance) before failing.
- Dry-run mode reports which hunks would apply and which would fail.

### R-FILE-09: Multi-file regex find-and-replace

The agent must be able to apply a regex substitution across all files matching a glob pattern. This is the refactoring tool for symbol renames, import path updates, and bulk string corrections.

**Why:** Renaming a function that appears in 30 files via individual edit calls would cost 30 turns. A single regex find-and-replace does it in one call with a safety cap on the number of files affected.

**Acceptance criteria:**
- A safety cap limits the number of files changed per call (default 50). The agent must explicitly raise the cap for broader renames.
- Dry-run mode reports which files would be changed and how many substitutions per file, without writing.
- A `literal` mode escapes regex metacharacters so plain-string renames do not require manual escaping.
- Every candidate file is path-checked before being read or written.

### R-FILE-10: AST-aware symbol rename

The agent must be able to rename a Python symbol (function, class, variable, method) across the codebase using AST analysis, only touching actual code identifiers -- not occurrences in strings, comments, or unrelated scopes.

**Why:** Regex-based find-and-replace has false positives: renaming `run` to `execute` via regex would also change the word "run" inside docstrings and comments. AST-aware rename only modifies nodes in the parse tree that are actual identifier references.

**Acceptance criteria:**
- A rename of function `foo` to `bar` modifies definition sites, call sites, and import references, but not string literals or comments containing "foo."
- Preview mode (default) shows what would change without writing.
- The tool updates import statements (`from mod import foo` becomes `from mod import bar`).

## Concern 4: File management (move, copy, delete)

### R-FILE-11: Move, copy, and delete

The agent must be able to move (rename), copy, and delete individual files. All three operations validate both source and destination paths against the security boundary. Parent directories for destination paths are created automatically.

**Why:** Refactoring involves restructuring the file tree: moving modules to new packages, copying templates, deleting obsolete files. These are basic operations the agent needs beyond read/write/edit.

**Acceptance criteria:**
- Moving a file to a destination whose parent directory does not exist creates the directory.
- Deleting a file that does not exist returns an error, not a silent no-op.
- Both source and destination of a move/copy are validated against allowed paths -- an agent cannot move a file from inside the workspace to outside it (or vice versa).

## Concern 5: File safety boundary

### R-FILE-12: Path confinement

Every file operation must validate that the resolved path falls within the configured allowed directories. Path traversal sequences (`../`), symlinks that resolve outside the workspace, and absolute paths pointing outside allowed directories must all be rejected.

**Why:** An autonomous agent that can read and write arbitrary paths on the host filesystem is a security risk. Path confinement is the fundamental safety guarantee that makes unattended operation feasible.

**Acceptance criteria:**
- A path containing `../../etc/passwd` is rejected regardless of the workspace location.
- A symlink inside the workspace that points outside allowed directories is rejected.
- The check uses the resolved (real) path, not the user-supplied string, to prevent TOCTOU attacks where a symlink is swapped after the check but before the operation.

### R-FILE-13: TOCTOU protection

File path validation and the subsequent file operation (read, write, delete) must be atomic. Between the moment a path is validated and the moment the file is accessed, the path must not be swappable by a concurrent process.

**Why:** A time-of-check/time-of-use race allows an attacker to replace a validated path with a symlink to a sensitive file between the validation step and the actual file operation. Atomic validation-and-operation prevents this.

**Acceptance criteria:**
- File operations use `O_NOFOLLOW` or equivalent mechanisms to prevent symlink swaps between validation and access.
- The inode of the opened file is verified to match the inode seen during validation.
- If inode verification fails (path changed between check and use), the operation is rejected with a security error.

### R-FILE-14: Character-level path security

Paths must be checked for null bytes (which truncate paths at the OS level) and Unicode homoglyphs (visually similar characters that can spoof ASCII paths). Both checks run before any OS-level path resolution.

**Why:** Null bytes in `"safe/path\x00/../../etc/shadow"` may cause the OS to see only `"safe/path"` while the application sees the full string. Homoglyphs like Cyrillic "a" (U+0430) look identical to ASCII "a" but resolve to different filesystem entries, enabling path spoofing.

**Acceptance criteria:**
- A path containing a null byte is rejected before any file operation.
- A path containing Cyrillic or Greek look-alike characters is rejected with a message identifying the offending character.
- NFKC normalization is applied: paths that change under normalization (indicating compatibility characters like superscripts or ligatures) are rejected.

### R-FILE-15: Phase-scoped write restrictions

When a phase specifies `allowed_edit_globs`, write operations must be restricted to paths matching those glob patterns. Read operations are never restricted by phase scope -- only writes.

**Why:** Different phases of a run may have different scopes. A "fix tests" phase should only be allowed to edit test files, not production code. Phase scoping is an additional layer of confinement beyond the workspace-level allowed paths.

**Acceptance criteria:**
- A write to `harness/core/config.py` is rejected during a phase whose globs are `["tests/**"]`.
- A read of `harness/core/config.py` during the same phase succeeds (reads are unrestricted).
- An empty or missing glob list means no phase restriction (all workspace paths are writable).

## Concern 6: Directory operations

### R-FILE-16: Directory listing, creation, and tree view

The agent must be able to list directory contents (with file type and size), create directories (including parents), and generate tree-view representations of directory structures.

**Why:** Understanding project layout is a prerequisite to meaningful code changes. The agent needs to see what files exist, where they are, and how they are organized before it can navigate to the right file.

**Acceptance criteria:**
- Directory listing shows file type (file vs directory), file size, and file name.
- Directory creation is idempotent -- creating an already-existing directory is not an error.
- Tree view respects depth limits to prevent unbounded output on deep directory structures.

## Concern 7: Diff and comparison

### R-FILE-17: File-to-file and file-to-text diff

The agent must be able to produce unified diffs comparing two files or comparing a file against a provided text string. The output format must be compatible with the patch tool.

**Why:** The agent needs to verify its own changes ("did my edit produce the expected result?"), compare versions of a file, and detect regressions. The diff output also feeds into the patch tool for round-trip workflows.

**Acceptance criteria:**
- Diffing identical content reports "no differences" explicitly, not an empty string.
- Output is capped at a configurable number of lines to prevent large diffs from flooding the context.
- The output format is standard unified diff accepted by the patch tool.

## Concern 8: Persistent notes (scratchpad)

### R-FILE-18: Context-surviving notes

The agent must be able to save short text notes that survive conversation pruning. Notes are re-injected into the system prompt on every subsequent turn so the agent retains critical information even after old tool results are evicted from the context window.

**Why:** Conversation pruning removes old messages to stay within the context budget. Without scratchpad notes, the agent loses findings from early in a cycle (file locations, design decisions, bug descriptions) and wastes turns re-discovering them.

**Acceptance criteria:**
- A saved note appears in the system prompt on the next turn.
- Notes have a per-note size cap to prevent context pollution.
- Notes are per-cycle; a new cycle starts with a clean scratchpad.
