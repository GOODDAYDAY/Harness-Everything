# CHANGELOG_AUTO.md — Auto-generated harness change log

---

## Round 2 · 2025 — Security Hardening & New AST Tools

### Files Modified / Created

| File | Status |
|------|--------|
| `harness/tools/cross_reference.py` | **NEW** |
| `harness/tools/semantic_search.py` | **NEW** |
| `harness/metrics.py` | **NEW** |
| `harness/tools/__init__.py` | Modified — 4 lines added |
| `harness/pipeline.py` | Modified — 5 lines added |
| `harness/tools/registry.py` | Modified — 3 lines added (comment) |

---

### What Was Changed / Added

#### `harness/tools/cross_reference.py` (new, ~210 lines)
- New `CrossReferenceTool` (`name = "cross_reference"`) — AST-based symbol
  cross-reference. Finds definition location, callers (up to 50), callees
  (up to 30), and test files for any Python function, method, or class.
- **Security fix**: Explicit `allowed_paths` enforcement at the top of
  `execute()` using `Path.is_relative_to()` — does NOT rely on
  `requires_path_check` flag, which was the confirmed bypass vector.
- **O(n) `_parent_class`**: Parent-pointer map built in a single `ast.walk`
  pass; replaces the O(n²) repeated-walk approach identified in prior rounds.
- **Output budget guard**: Compact JSON (`indent=None`); trims callers list
  if serialised output exceeds 8 192 bytes, sets `truncated: true`.

#### `harness/tools/semantic_search.py` (new, ~127 lines)
- New `SemanticSearchTool` (`name = "semantic_search"`) — finds Python
  identifiers semantically related to a plain-English concept using
  token-overlap scoring (no external ML dependency).
- **`_PARAM_ALIASES` collision fix**: Primary parameter named `concept`
  (not `query`) to avoid the pre-existing `"query" → "glob"` alias in
  `registry.py` corrupting the argument before dispatch.
- **Security fix**: Same explicit `allowed_paths` enforcement as above.

#### `harness/metrics.py` (new, ~93 lines)
- New `MetricsCollector` dataclass with `record_phase()` and `flush()`.
- `flush()` writes atomically via `tempfile.mkstemp` + `os.replace` —
  no partial JSON files on crash.
- `contextlib.suppress(OSError)` on temp-file cleanup.
- Integrates at the `pipeline.py` level using confirmed-present `PhaseResult`
  and `InnerResult` symbols — no guessing at `phase_runner.py` internals.

#### `harness/tools/__init__.py`
- Added imports for `CrossReferenceTool` and `SemanticSearchTool`.
- Appended both to `DEFAULT_TOOLS` list.
- Updated docstring count from "22 of 23" to "24 of 25".

#### `harness/pipeline.py`
- Added `from pathlib import Path` import.
- Added `from harness.metrics import MetricsCollector` import.
- Instantiated `MetricsCollector` before outer loop.
- Called `metrics.record_phase(phase.name, phase_result)` after each phase.
- Called `metrics.flush()` after all rounds complete.

#### `harness/tools/registry.py`
- Added comment to `_PARAM_ALIASES` documenting that `"query"` is
  grep_search-specific — warns future tool authors not to use `"query"` as
  a primary parameter name.

---

### Security Improvement

The two new tools implement **explicit `allowed_paths` enforcement** using
`Path.is_relative_to()` rather than relying on the `requires_path_check`
flag (which routes through `_check_path()` and the broken `startswith()`
comparison). This closes the arbitrary-file-access bypass on these tools.

### Dead Code Removed

None in this round (all changes are additive new tools + wiring).

### Lines Added vs Removed

- Lines added: ~440 (3 new files + 5 wiring lines)
- Lines removed: 0
- Net: +440

---
