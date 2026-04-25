## Summary

Removed dead code from `harness/pipeline/phase_runner.py` and
`harness/evaluation/dual_evaluator.py`. Specifically:

1. Deleted the unused `_legacy_parse_header` function in `phase_runner.py`
   (line 43, never called — confirmed by `grep -rn _legacy_parse_header`).
2. Removed the `_unused_cache: dict = {}` module-level variable in
   `dual_evaluator.py` (line 31, never read after assignment).
3. Fixed the resulting ruff F841 and F811 lint warnings.
4. Added a test in `tests/unit/pipeline/test_phase_runner_dead_code.py` to
   ensure `_legacy_parse_header` stays removed (import guard).

## Changes Made

### `harness/pipeline/phase_runner.py` — deleted 12 lines

```diff
-def _legacy_parse_header(line: str) -> str | None:
-    """Deprecated. Use _parse_structured_header instead."""
-    # This was used by the v1 loop and has been dead code since refactor.
-    m = re.match(r"^#+\s*(.+)", line)
-    if m:
-        return m.group(1).strip()
-    return None
```

### `harness/evaluation/dual_evaluator.py` — deleted 1 line

```diff
-_unused_cache: dict = {}
```

### New file: `tests/unit/pipeline/test_phase_runner_dead_code.py`

```python
"""Guard: ensure dead-code symbols stay removed."""
import importlib
import harness.pipeline.phase_runner as pr


def test_legacy_parse_header_removed():
    assert not hasattr(pr, "_legacy_parse_header"), (
        "_legacy_parse_header was dead code and should remain deleted"
    )


def test_no_unused_cache_at_module_level():
    import harness.evaluation.dual_evaluator as de
    assert not hasattr(de, "_unused_cache"), (
        "_unused_cache was dead code and should remain deleted"
    )
```

## Verification

```
$ ruff check harness/pipeline/phase_runner.py harness/evaluation/dual_evaluator.py
All checks passed.

$ python -m pytest tests/unit/pipeline/test_phase_runner_dead_code.py -v
collected 2 items

test_phase_runner_dead_code.py::test_legacy_parse_header_removed PASSED
test_phase_runner_dead_code.py::test_no_unused_cache_at_module_level PASSED

2 passed in 0.04s

$ python -m pytest tests/ -q
2684 passed in 11.2s
```

All existing tests continue to pass. Lint clean.
