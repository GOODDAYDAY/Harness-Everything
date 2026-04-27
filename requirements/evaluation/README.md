# Evaluation Domain

The evaluation domain provides scoring infrastructure for assessing agent execution quality. It comprises three modules: a dual-isolated evaluator that runs two independent LLM-based reviewers in parallel, a static analysis engine that performs deterministic code-quality checks, and a metrics module that measures score discrimination.

Source files:
- `harness/evaluation/__init__.py`
- `harness/evaluation/dual_evaluator.py` (full scenario in [dual-evaluator.md](dual-evaluator.md))
- `harness/evaluation/static_analysis.py`
- `harness/evaluation/metrics.py`

---

## Metrics (`metrics.py`)

A single-function module that measures how well an evaluator discriminates between submissions in the critical middle score range.

### EVAL-MET-01: Critical Range Discrimination

**Function:** `calculate_critical_range_discrimination(evaluations: List[Dict]) -> float`

The function calculates the sample standard deviation of scores that fall within the critical 4-7 range (inclusive on both ends). This measures whether the evaluator is spreading scores meaningfully in the range where decisions are hardest.

**Scenarios:**

| # | Given | When | Then |
|---|-------|------|------|
| 1 | `evaluations` is not a `list` | function is called | raises `TypeError("evaluations must be a list")` |
| 2 | `evaluations` contains dicts without a `'score'` key | extracting scores | those entries are silently skipped |
| 3 | a dict's `'score'` value cannot be converted via `float()` (raises `ValueError` or `TypeError`) | extracting scores | that entry is silently skipped |
| 4 | fewer than 2 scores fall in the 4.0-7.0 range | calculating standard deviation | returns `0.0` |
| 5 | 2 or more scores fall in the 4.0-7.0 range | calculating standard deviation | returns the **sample** standard deviation (dividing by N-1, not N) using `math.sqrt(variance)` |
| 6 | scores outside 4.0-7.0 exist alongside scores inside the range | filtering | only scores where `4.0 <= s <= 7.0` are included in the calculation; out-of-range scores are ignored |

---

## Static Analysis (`static_analysis.py`)

Lightweight, deterministic code-quality checks that run without invoking an LLM. The results are injected into the evaluator prompt to ground LLM verdicts in objective facts.

### Data Structures

#### Finding

```
@dataclass
class Finding:
    level: str        # "ERROR", "WARN", or "INFO"
    file: str         # relative or absolute path
    message: str      # human-readable description
    line: int = 0     # 0 means not applicable
```

Level constants are module-private: `_LEVEL_ERROR = "ERROR"`, `_LEVEL_WARN = "WARN"`, `_LEVEL_INFO = "INFO"`.

#### StaticReport

```
@dataclass
class StaticReport:
    findings: list[Finding] = field(default_factory=list)
    files_checked: int = 0
    files_skipped: int = 0
```

**Computed properties:**

| Property | Type | Behavior |
|----------|------|----------|
| `errors` | `list[Finding]` | Filters findings where `level == "ERROR"` |
| `warnings` | `list[Finding]` | Filters findings where `level == "WARN"` |
| `has_errors` | `bool` | `True` if any ERROR-level findings exist |
| `summary` | `str` | Format: `"{e} error(s), {w} warning(s), {ok} file(s) clean [{files_checked} checked, {files_skipped} skipped]"` where `ok` = `files_checked` minus count of distinct files with ERROR findings |

### EVAL-SA-01: `to_prompt_block()` Output Formatting

| # | Given | When | Then |
|---|-------|------|------|
| 1 | `files_checked == 0` | called | returns empty string `""` |
| 2 | `files_checked > 0` and `findings` is empty | called | returns block starting with `"## Static Analysis Results"`, the summary line in bold, and the line `"All changed Python files passed static analysis. ✓"` |
| 3 | `files_checked > 0` and findings exist | called | returns block with a markdown table with columns `Level`, `File`, `Line`, `Finding`; file names are wrapped in backtick code spans; pipe characters in `message` and `file` are escaped as `\|`; line value `0` renders as `"—"` |
| 4 | findings exist and `has_errors` is `True` | called | appends a bold warning paragraph stating ERROR findings indicate objective defects and the conservative reviewer MUST FAIL |

### EVAL-SA-02: Check 1 -- Syntax Validity

**Function:** `_check_syntax(path: Path, rel: str) -> list[Finding]`

| # | Given | When | Then |
|---|-------|------|------|
| 1 | file passes `py_compile.compile(str(path), doraise=True)` | checked | returns empty list |
| 2 | file raises `py_compile.PyCompileError` | checked | returns one Finding with `level="ERROR"`, message prefixed `"syntax error: "`, and line number extracted via regex `r", line (\d+)"` or `r":(\d+):"` (first match wins); line defaults to 0 if no match |
| 3 | file raises any other `Exception` | checked | returns one Finding with `level="WARN"`, message `"py_compile raised {type(exc).__name__}: {exc}"` |

### EVAL-SA-03: Check 2 & 3 -- Import Sanity and Symbol Existence

**Function:** `_check_imports(source: str, rel: str, workspace: Path) -> list[Finding]`

Checks 2 and 3 are combined in a single function. They only run when Check 1 (syntax) passes for a file.

| # | Given | When | Then |
|---|-------|------|------|
| 1 | source has a `SyntaxError` on `ast.parse` | called | returns empty list (avoids double-reporting with Check 1) |
| 2 | `import X` statement where `X` is not in stdlib, not in site-packages (`importlib.util.find_spec`), and not a `.py` file or `__init__.py` package in the workspace | checked | emits WARN: `"import {mod!r} — module not found in stdlib, site-packages, or workspace"` |
| 3 | `from X import ...` where `X` is not resolvable by any method | checked | emits WARN: `"from {mod!r} import ... — module not found in stdlib, site-packages, or workspace"` |
| 4 | `from X import Y` where `X` resolves to an in-workspace `.py` file but `Y` is not among exported names | checked | emits ERROR with message including the symbol name and up to 8 sorted exported names (truncated with `"..."` if more than 8) |
| 5 | `from X import *` | checked | silently skipped (cannot verify statically) |
| 6 | `from X import Y` where `X` resolves to a package `__init__.py` and `Y` is a sibling sub-module file (`{name}.py`) or sub-package (`{name}/__init__.py`) | checked | treated as valid, no finding emitted |

**Exported names detection** for in-workspace modules includes:
- Top-level `class`, `def`, and `async def` names (via `ast.FunctionDef`, `ast.AsyncFunctionDef`, `ast.ClassDef`)
- Module-level variable assignments (`ast.Assign` targets that are `ast.Name`, `ast.AnnAssign` targets)
- Re-exports via `from X import Y` at module level (adds `alias.asname or alias.name`)
- Names from `import X` at module level (adds `alias.asname or alias.name.split(".")[0]`)

**Module resolution** (`_module_file_in_workspace`):
- Converts dotted module name to path: `harness.llm` becomes `workspace/harness/llm.py`
- Falls back to package init: `workspace/harness/llm/__init__.py`
- Returns `None` if neither exists

**Stdlib/installed detection** (`_is_stdlib_or_installed`):
- Checks only the top-level module name (`module.split(".")[0]`)
- Fast path: returns `True` if already in `sys.modules`
- Slow path: uses `importlib.util.find_spec(top)`, returns `True` if spec is not `None`
- Returns `False` on `ModuleNotFoundError` or `ValueError`

### EVAL-SA-04: Check 4 -- Structural Regression

**Function:** `_check_structural_regression(path: Path, rel: str, before_source: str | None) -> list[Finding]`

| # | Given | When | Then |
|---|-------|------|------|
| 1 | `before_source` is `None` (new file) | called | returns empty list |
| 2 | current file cannot be read (`OSError`) | called | returns empty list |
| 3 | a top-level class or function name existed in `before_source` but is absent in the current file | called | emits WARN per removed name: `"Top-level name {name!r} existed before execution but is now absent — potential regression if callers depend on it"` |

Top-level names are extracted by `_get_top_level_names(source: str) -> set[str]`, which parses the source with `ast.parse` and collects names from `ast.FunctionDef`, `ast.AsyncFunctionDef`, and `ast.ClassDef` nodes that are direct children of the module. Returns empty set on `SyntaxError`.

### EVAL-SA-05: `run_static_checks` Entry Point

**Function signature:**
```python
def run_static_checks(
    files_changed: list[str],
    workspace: str,
    *,
    before_snapshots: dict[str, str] | None = None,
) -> StaticReport:
```

| # | Given | When | Then |
|---|-------|------|------|
| 1 | a path in `files_changed` is relative | processing | resolved against `workspace`: `(ws / p).resolve()` |
| 2 | a path is absolute | processing | resolved via `p.resolve()` |
| 3 | file does not exist or is not a file | processing | increments `files_skipped`, logs debug, skips all checks |
| 4 | file suffix is not `.py` | processing | increments `files_skipped`, logs debug, skips all checks |
| 5 | file is a valid `.py` file | processing | increments `files_checked`; runs Check 1 (syntax); if syntax passes, runs Checks 2+3 (imports); runs Check 4 (structural regression) |
| 6 | file cannot be read (`OSError`) on `read_text` | processing | appends a WARN finding `"Could not read file: {exc}"` |
| 7 | `before_snapshots` is provided | running Check 4 | looks up pre-execution source via `snapshots.get(rel) or snapshots.get(str(p))` |
| 8 | `before_snapshots` is `None` | running Check 4 | Check 4 receives `None` for `before_source`, returning empty list (new file) |
| 9 | all checks complete | finishing | logs info with `files_checked`, `files_skipped`, error count, warning count |

Display path (`rel`) is computed via `p.relative_to(ws)`. If that raises `ValueError` (file is outside workspace), falls back to `str(p)`.
