# Search and Analysis

This document specifies what questions the agent must be able to answer about a codebase through search tools and static analysis, without executing any code.

## Context

Before the agent can make useful changes, it must understand the code it is working with: where things are defined, how they are connected, what calls what, and where a concept lives across the codebase. The search and analysis tools are the agent's eyes -- they turn a codebase from an opaque directory tree into a navigable, queryable structure.

These tools are read-only. They never modify files. Their output is consumed by the agent to inform editing decisions.

## Concern 1: File discovery

### R-SEARCH-01: Glob-based file search

The agent must be able to find files by name or path pattern using glob syntax (e.g., `**/*.py`, `src/**/test_*.py`). Results must be sorted by modification time and capped to prevent unbounded output.

**Why:** Before reading a file, the agent needs to find it. Glob search answers "what files exist that match this pattern?" -- the first step in any exploration task.

**Acceptance criteria:**
- Standard glob patterns work: `*` matches within a directory, `**` matches across directories.
- Results are relative to the search root, not absolute paths.
- A hard cap on candidates prevents CPU exhaustion on patterns like `**/*` in large trees.
- Files that resolve outside allowed paths via symlinks are silently excluded.

### R-SEARCH-02: Content search with regex

The agent must be able to search file contents using regular expressions, with results showing file path, line number, and matching line. Context lines (before and after each match) must be configurable.

**Why:** Finding where a string or pattern appears in code is the most common search task. The agent needs this to locate usages, find definitions, check for patterns, and verify changes. Context lines help the agent understand the match without a separate read call.

**Acceptance criteria:**
- Regex patterns follow Python `re` syntax.
- Results can be filtered by file glob (e.g., only search `*.py` files).
- Case-insensitive search is supported.
- Total matches are capped to prevent a broad pattern from flooding the context.
- A hard cap on files scanned prevents unbounded I/O on `**/*` patterns.

## Concern 2: Concept-level search

### R-SEARCH-03: Feature/concept search

The agent must be able to search for a plain-English concept (e.g., "retry", "authentication", "checkpoint") and receive results grouped by category: symbol names containing the keyword, files whose names contain the keyword, comments and docstrings mentioning the keyword, and module-level constants/config containing the keyword.

**Why:** Regex search finds exact text matches but does not answer "where does the codebase deal with retries?" A concept search aggregates multiple signal types (names, comments, config keys) into a coherent answer about where a feature lives. This is the entry point for understanding an unfamiliar area of code.

**Acceptance criteria:**
- Results are grouped by signal type (symbols, files, comments, config), not interleaved.
- Supports both substring matching (default) and token-overlap scoring for multi-word concepts.
- Pure AST and text analysis -- no external dependencies, no code execution.

### R-SEARCH-04: TODO/FIXME annotation scanning

The agent must be able to scan source files for developer annotations (TODO, FIXME, HACK, NOTE, BUG, XXX) and return them grouped by tag and file, with optional context lines.

**Why:** Developer annotations are signals about known issues, intentional workarounds, and planned work. Before modifying a module, the agent should know about these annotations to avoid undoing intentional workarounds or duplicating planned work.

**Acceptance criteria:**
- Default scan finds all six standard tags; the tag set is configurable per call.
- Results can be sorted by file, tag, or line number.
- Results are capped to prevent a heavily-annotated codebase from flooding the context.

## Concern 3: Git history search

### R-SEARCH-05: Git history, blame, and working-tree search

The agent must be able to search git history (commit messages matching a regex), blame individual files (which commit last changed each line matching a pattern), search the working tree via `git grep`, show the diff of a specific commit, and view the commit log for a specific file.

**Why:** Understanding *why* code looks the way it does requires knowing its history. Blame tells the agent who changed a line and when. Commit message search finds when a feature was added or a bug was fixed. `git grep` is faster than Python-level search for large repos because it operates on the git index.

**Acceptance criteria:**
- All modes run with a timeout to prevent stalls on large repositories.
- Output is capped to prevent a verbose `git log` from flooding the context.
- This tool is optional (not registered by default) because it has high schema cost and is only useful in git repositories.

## Concern 4: Symbol-level analysis

### R-SEARCH-06: AST-based code analysis

The agent must be able to analyze a Python source file and receive a structured report containing: the import map (what the file imports), the symbol table (classes, functions, their line ranges and signatures), per-function cyclomatic complexity estimates, and the internal call graph (what each function calls).

**Why:** Reading source code gives the agent text; AST analysis gives it structure. The agent can ask "what functions are in this file and how complex are they?" without reading every line. This is essential for deciding which functions need attention and which are simple enough to skip.

**Acceptance criteria:**
- Analysis works on files with syntax errors (returns an error for that file, not a crash).
- Complexity estimates use a branch-counting heuristic, not an external tool.
- Pure stdlib `ast` -- no third-party dependencies.

### R-SEARCH-07: Symbol extraction by name

The agent must be able to extract the source text of a specific named symbol (function, class, method) from a file without reading the entire file. Supports extracting multiple symbols in one call, glob-style name patterns, and cross-file search (finding a symbol name across a directory).

**Why:** When the agent knows *which* function it needs to see but not *which lines* it occupies, symbol extraction is more precise than line-range reading. It also avoids the context cost of surrounding code the agent does not need.

**Acceptance criteria:**
- Dotted names work: `"MyClass.my_method"` extracts the method, not the whole class.
- Pattern matching: `"_check_*"` returns all private helpers starting with `_check_`.
- Optional context lines show decorators or docstrings above the definition.
- Output format supports both human-readable text and structured JSON.

## Concern 5: Cross-reference and dependency analysis

### R-SEARCH-08: Cross-reference (definition + callers + callees + tests)

The agent must be able to ask "where is this symbol defined, who calls it, what does it call, and which tests exercise it?" in a single tool call. Analysis uses AST parsing, not regex, to avoid false positives from comments and string literals.

**Why:** Before refactoring a function, the agent needs the full picture: definition location, all callers (to assess impact), all callees (to understand dependencies), and test coverage (to know if tests exist). Without cross-reference, the agent must make 4 separate calls (grep for def, grep for calls, read the function, grep for test files) and mentally combine the results.

**Acceptance criteria:**
- Returns definition location (file, line, signature).
- Returns list of callers with file and line.
- Returns list of callees.
- Returns list of test files that import or reference the symbol.
- Uses AST, not regex -- a comment containing the function name does not appear as a caller.

### R-SEARCH-09: Data flow tracing

The agent must be able to trace how a symbol is used across the codebase in three modes: (1) find all direct callers of a function, (2) find all sites where an attribute is read (e.g., `config.workspace`), and (3) trace callers-of-callers to a configurable depth.

**Why:** Cross-reference gives the full picture for one symbol. Data flow gives focused answers to specific questions: "who calls this function?" (impact analysis), "where is this config field read?" (before renaming it), "how deeply is this helper embedded?" (before moving it).

**Acceptance criteria:**
- Caller mode accepts a bare function name and returns all functions that call it.
- Read mode accepts `obj.attr` notation and returns all sites where the attribute is accessed.
- Call-chain mode traverses callers-of-callers up to a limited depth (capped at 2 levels to avoid combinatorial explosion).
- Uses AST analysis, not text matching.

### R-SEARCH-10: Call graph construction

The agent must be able to trace the outgoing call graph from a starting function: all functions it calls, and recursively all functions those call, up to a configurable depth. The output is a directed graph of nodes with their definition locations and call edges.

**Why:** Understanding the downstream impact of a function change requires seeing everything that function transitively depends on. The call graph answers "if I change function X, what other functions might be affected?"

**Acceptance criteria:**
- BFS traversal with a depth cap (hard maximum of 5) prevents runaway expansion.
- A node cap (maximum 200 unique nodes) prevents unbounded output on large codebases.
- Cycle detection prevents infinite loops on mutually recursive functions.
- Each node records its definition file, line, and list of callees.

### R-SEARCH-11: Import dependency graph and circular import detection

The agent must be able to build a module-level import dependency graph for a directory and detect circular import cycles. Only workspace-local modules are included by default (stdlib edges are filtered out).

**Why:** Circular imports cause runtime failures that are hard to diagnose from error messages alone. The dependency graph also reveals the module structure (which modules are central, which are leaf) and guides refactoring decisions about where to break dependencies.

**Acceptance criteria:**
- Relative imports are resolved to absolute dotted module names.
- Circular import cycles are detected and reported as ordered lists of modules forming the cycle.
- The graph can optionally include stdlib imports.
- Output uses the `_safe_json` budget cap to prevent large graphs from flooding the context.

## Concern 6: Project orientation

### R-SEARCH-12: Project map

The agent must be able to generate a high-level project overview in one call: a list of all Python modules with line counts, class counts, and function counts; entry points (files with `if __name__ == "__main__"`); and the inter-module import graph.

**Why:** When the agent starts work on an unfamiliar codebase, its first task is orientation: what is here, how is it organized, what are the entry points? Without a project map, the agent must call `tree`, then `glob_search`, then `batch_read` on several files, burning multiple turns. A project map answers all orientation questions in one call.

**Acceptance criteria:**
- Covers all Python files under the specified directory up to a configurable depth.
- Reports per-module: relative path, line count, class count, function count.
- Identifies entry points.
- Shows which modules import which other modules.
- Test files can be included or excluded.
- Output is capped to prevent large projects from flooding the context.

## Concern 7: Specialized analysis tools

### R-SEARCH-13: Context budget awareness

The agent must be able to check its current token usage, remaining budget, turn count, and scratchpad status. This is not a search tool per se, but it is a read-only analysis tool that helps the agent decide how to allocate its remaining resources.

**Why:** An agent that does not know how close it is to its turn limit or context budget will either waste resources on unnecessary exploration or get cut off mid-task. Budget awareness lets the agent pace itself.

**Acceptance criteria:**
- Returns current input tokens used, output tokens used, turn number, max turns, and scratchpad note count.
- No parameters required -- the agent just calls it.

### R-SEARCH-14: Tool self-discovery

The agent must be able to introspect the currently registered tool set at runtime: what tools are available, their descriptions, parameter schemas, and whether they require path checks. Supports filtering by name substring.

**Why:** With 30+ tools available, the agent may not remember what each tool does or what parameters it takes. Self-discovery lets the agent look up the right tool for a task without hallucinating tool names or parameters.

**Acceptance criteria:**
- Returns name, description, and required/optional parameters for each matching tool.
- A name filter narrows results to tools whose name contains the given substring.
- Detailed mode shows the full JSON Schema for a specific tool.

### R-SEARCH-15: JSON operations

The agent must be able to parse, query, validate, merge, and diff JSON data without external dependencies. This supports working with configuration files, API responses, and structured data that the agent encounters during its work.

**Why:** Agents frequently work with JSON config files and structured data. Simple operations (extract a nested key, validate against a schema, diff two configs) should not require shelling out to `jq` or writing Python snippets.

**Acceptance criteria:**
- Parse mode validates JSON syntax and reports error position on failure.
- Query mode supports dot/bracket path notation for nested access.
- Diff mode shows added/removed/changed leaf paths between two JSON values.
- Merge mode deep-merges two objects with right-hand-side-wins semantics.
