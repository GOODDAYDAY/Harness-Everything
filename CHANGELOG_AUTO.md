# CHANGELOG_AUTO.md — Auto-generated harness change log

> **Condensed 2026-04-16**: 13 self-improvement rounds summarised below.
> Original verbose entries replaced with compact summaries.

---

## Summary

| Round | Theme | Key Changes | Net Lines |
|-------|-------|-------------|-----------|
| 1 | Registry restructure | GitSearchTool → OPTIONAL_TOOLS, removed import-time assertions, added `tests/` | +14 |
| 2 | Security + AST tools | New `cross_reference`, `semantic_search`, `metrics.py`; explicit allowed_paths enforcement | +440 |
| 3 | Feature search | New `feature_search` tool (keyword-based codebase discovery, 4 categories) | +213 |
| 4 | Call graph | New `call_graph` tool (AST-based, BFS traversal, depth-limited) | +283 |
| 5 | Dependency analyzer | New `dependency_analyzer` tool (import graph, cycle detection, DFS) | +320 |
| 6 | HTTP + JSON tools | New `http_client`, `json_transform` tools; subpackage reorganisation (core/, evaluation/, pipeline/) | +770 |
| 7 | Tool auto-discovery | New `discovery.py` (dynamic tool loading); `ToolRegistry.discover()` | +350 |
| 8 | Architecture fix | HttpRequestTool → OPTIONAL_TOOLS (reduce default schema cost) | −13 |
| 9 | Git search | New `git_search` tool (log/blame/grep/show/log_file modes) | +446 |
| 10 | TODO scanner | New `todo_scan` tool (6 tag types, context lines, sort modes) | +300 |
| R3-fix | MetricsCollector fix | Fixed dataclass field ordering (mutable default after non-default) | +2 |
| R3-trace | Registry hardening | Manifest generator, import-time assertions (later removed in Round 1) | +79 |
| R4-security | Symlink safety | `os.path.realpath` consistency in discovery.py; deleted dead `generate_manifest.py` | −67 |

**Cumulative**: 23 → 34 tools added across rounds, then consolidated to 33 (semantic_search merged into feature_search). 6 shim files created in Round 6, eliminated post-rounds.

---

## Round Details

### Round 1 — Tool Registry Restructure (2026-04-16)

- Moved `GitSearchTool` from DEFAULT_TOOLS to OPTIONAL_TOOLS (schema cost reduction)
- Removed import-time `assert len(...)` landmines; replaced with CI tests
- Created `tests/test_tools_registry.py` (2 tests: no duplicate names, ABC compliance)

### Round 2 — Security Hardening & New AST Tools

- **New**: `cross_reference.py` (~210L) — AST-based symbol xref (definition, callers, callees, tests)
- **New**: `semantic_search.py` (~127L) — token-overlap concept search (later merged into feature_search)
- **New**: `metrics.py` (~93L) — MetricsCollector with atomic JSON flush
- Security: explicit `allowed_paths` enforcement via `Path.is_relative_to()` in new tools

### Round 3 — Feature Search Tool

- **New**: `feature_search.py` (~200L) — keyword search across 4 categories: symbols, files, comments, config
- Uses `_check_dir_root` + `_rglob_safe` for security; `_safe_json` for output budget

### Round 4 — Call Graph Tool

- **New**: `call_graph.py` (~280L) — AST-based call graph with BFS traversal
- Modes: callers, callees, both; depth-limited; deduplicates cross-file calls

### Round 5 — Dependency Analyzer Tool

- **New**: `dependency_analyzer.py` (~310L) — import graph builder + DFS cycle detector
- Modes: graph, cycles, imports; relative import resolution; stdlib filtering

### Round 6 — HTTP Client, JSON Transform & Subpackage Reorganisation

- **New**: `http_client.py` (~160L) — HTTP requests with timeout/redirect/size guards
- **New**: `json_transform.py` (~200L) — JMESPath-style JSON query/transform
- **Reorganised**: moved modules into `harness/core/`, `harness/evaluation/`, `harness/pipeline/` subpackages
- Created 6 backward-compat shim files (later eliminated)

### Round 7 — Tool Auto-Discovery

- **New**: `discovery.py` (~180L) — dynamic tool loading from directory
- Added `ToolRegistry.discover()` method for runtime tool registration

### Round 8 — HttpRequestTool Architecture Fix

- Moved `HttpRequestTool` from DEFAULT_TOOLS to OPTIONAL_TOOLS
- Rationale: HTTP tool adds schema weight for tasks that don't need network access

### Round 9 — Git Search Tool

- **New**: `git_search.py` (~442L) — unified git history search
- 5 modes: log (message search), blame (line attribution), grep (content), show (commit stat), log_file (file history)
- Async subprocess with 30s timeout; commit ref sanitization; output truncation at 20KB

### Round 10 — TODO Scanner Tool

- **New**: `todo_scan.py` (~290L) — developer annotation scanner
- 6 tags: TODO, FIXME, HACK, NOTE, BUG, XXX; context lines; by_tag summary
- Sort modes: file, tag, line

### R3-fix — MetricsCollector Dataclass Fix

- Fixed field ordering: moved `phases` (mutable default) after `output_path` (non-default)
- Python dataclass constraint: fields with defaults must follow fields without

### R3-trace — Registry Hardening

- Added manifest generator and import-time count assertions
- Later superseded: assertions removed in Round 1, generator deleted in R4-security

### R4-security — Symlink Safety & Dead Code Removal

- Replaced `Path.resolve()` with `os.path.realpath()` in `discovery.py` for consistency with `base.py`
- Deleted `generate_manifest.py` (module-level side-effect: wrote to disk on import)
- Deleted `registry_manifest.json` (dead output, no consumers)

---

## Post-Round Consolidation (2026-04-16)

- **Merged** `semantic_search` into `feature_search` via `scoring="token_overlap"` parameter (34→33 tools)
- **Eliminated** 6 shim files: `harness/{config,llm,evaluator,dual_evaluator,phase,phase_runner}.py`
- **Migrated** ~47 old-path imports to new subpackage paths
- **Fixed** 3 circular import chains by simplifying `__init__.py` re-exports
- **Cleaned** unused imports (`Any`, `HarnessConfig`, `ToolResult`) across 5 files
