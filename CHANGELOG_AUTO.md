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

### Security Consolidation Round
- **Enhanced**: Unicode homoglyph security guard in `base.py::_validate_path` now has explicit test verification
- **Cleaned**: Removed dead `tempfile` import from `tests/test_path_security.py`
- **Improved**: `test_unicode_normalization_attack` now has concrete assertions instead of passive print statements
- **Verified**: All path security tests pass with stricter validation checks

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

---

### Round N — Evaluator + Synthesis + Meta-Review Quality Pass

- **Evaluator prompts** (`harness/prompts/dual_evaluator.py`): replaced vague calibration scale with concrete 5-point anchors (0/3/5/7/10) aligned to the falsifiable criterion's specificity requirement; added mandatory `WHAT WOULD MAKE THIS 10/10` field to both BASIC and DIFFUSION outputs, forcing actionable feedback; added explicit `SCORE: <N>` on its own line instruction to enforce parse_score reliability.
- **parse_score hardening** (`harness/evaluation/dual_evaluator.py`): two-tier extraction — strict anchored `^SCORE: N$` pattern (last match) preferred over loose fallback, eliminating false positives from inline arithmetic lines; logs a warning when no score token is found.
- **Phase-mode adaptation** (`harness/evaluation/dual_evaluator.py`, `harness/pipeline/phase_runner.py`): `DualEvaluator.evaluate()` now accepts a `mode` parameter (`"debate"` or `"implement"`); a mode header is prepended to every evaluator user message so the rubric adapts — debate scores plan quality, implement scores executed code state. `_evaluate_and_log()` forwards `phase.mode` automatically.
- **Meta-review specificity** (`harness/prompts/meta_review.py`): every finding now requires a round number, file path, and function name; added `Score Trend Analysis` section requiring per-phase trend (IMPROVING/STAGNATING/DECLINING) with named evaluator evidence; prompt adjustment suggestions must cite a specific round finding.
- **Synthesis improvements** (`harness/prompts/synthesis.py`): added `ANTI-REPETITION RULE` blocking verbatim best-round copying; added explicit instructions to identify the single best idea and the worst round's specific defect; added `Best Idea and Worst Round Analysis` output section; synthesis must be demonstrably more specific than any single round.

### Round N+1 — LLM Call Quality: Short-Response Warning + Pruning Safety Audit

- **Short-response detection** (`harness/core/llm.py`): added `_SHORT_RESPONSE_CHARS = 50` constant and a `log.warning()` in `LLM.call()` that fires whenever a non-tool-call response is shorter than 50 chars — the primary signal of truncated/failed generation that previously caused `parse_score()` to silently return 0.0 and corrupt evaluator scores.
- **Pruning safety audit** (`harness/core/llm.py`): documented three invariants in `_prune_conversation_tool_outputs()` confirming the system prompt is structurally impossible to prune (it is a separate API parameter, never part of `messages`), plain-text initial user messages are excluded because the pruner only targets list-content `tool_result` blocks, and no messages are ever removed or reordered (preserving Anthropic's tool_use/tool_result ID pairing requirement).

### Round N+2 — Critical-Path Test Coverage

- **New**: `tests/test_critical_paths.py` (+555 lines, 35 new tests across 6 classes) covering previously untested high-risk paths:
  - `ToolRegistry.execute()`: routing, `allowed_tools` enforcement, alias normalisation (`file_path→path`, `text→content`), SCHEMA ERROR on missing/unknown params, PERMISSION ERROR on blocked tools.
  - `ToolRegistry.filter_by_tags()`: correct subset by single/multiple tags, untagged-tools-always-included invariant, result-is-new-registry (non-mutation), empty frozenset semantics.
  - `Tool._resolve_and_check()` path security: null-byte rejection, `../..` escape, symlink-outside-workspace rejection, legitimate path acceptance — across both `ReadFileTool` and `WriteFileTool`.
  - `PipelineConfig.from_dict()` and `HarnessConfig.from_dict()` comment stripping: `//` and `_` prefixed keys silently dropped; truly unknown keys still raise `ValueError`; `PhaseConfig.from_dict()` also covered.
  - `_auto_update_prompts()` variable-preservation guard: rewrite dropping `$file_context`/`$prior_best` is rejected and original prompt kept; rewrite preserving all vars is applied; LLM exception falls back to original.
  - `parse_score()` two-tier extraction: strict anchored `^SCORE: N$` preferred over loose fallback; last match wins; clamping at 0/10; no-match returns 0.0.
- **Test counts**: before=2, after=37 (+35); all 37 pass in 0.67s with zero warnings.

### Security Guard Enhancement Round
- **Hardened**: Modified `test_control_characters_in_path` to explicitly reject TAB character (`\x09`) instead of allowing it as an exception, strengthening path security validation.
- **Consistency**: All control characters now trigger the same security check, eliminating a potential bypass vector identified by the Diffusion Evaluator.
