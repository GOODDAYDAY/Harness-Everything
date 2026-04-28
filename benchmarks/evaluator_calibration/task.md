# Evaluator Calibration Benchmark

This directory contains 6 proposals of varying quality for an identical task.
They are used to measure the evaluator's Spearman rank-correlation (rho) against
human-assigned ground-truth scores.

## Task Description

Implement a thread-safe LRU (Least Recently Used) cache class in
`harness/cache/lru_cache.py` with the following interface:

```
class LRUCache:
    def __init__(self, capacity: int) -> None: ...
    def get(self, key: str) -> Any | None: ...
    def put(self, key: str, value: Any) -> None: ...
    def __len__(self) -> int: ...
    def clear(self) -> None: ...
```

**Criterion (falsifiable):**
- `LRUCache(capacity=3)` → after put(a,1), put(b,2), put(c,3), put(d,4)
  the key `a` is evicted and `get(a)` returns `None`
- `get()` counts as a use: put(a,1), put(b,2), put(c,3), get(a), put(d,4)
  evicts `b` (LRU), not `a`
- Thread-safe under concurrent `get`/`put` from multiple threads
- Tests added in `tests/unit/core/test_lru_cache.py`

## Ground-Truth Scores

Human-assigned scores on 0-10 scale (see ground_truth.json):

| Proposal | Score | Rationale |
|---|---|---|
| excellent_cache_impl | 9.0 | Correct OrderedDict LRU, threading.Lock, comprehensive tests |
| excellent_cleanup_impl | 8.5 | Good cleanup cycle — different task, tests correct |
| good_cache_with_issues | 6.5 | Correct LRU logic, tests present, but lock scope too narrow |
| mediocre_cache_wrong_eviction | 4.0 | Uses FIFO not LRU; names file+function but wrong algorithm |
| poor_cache_no_eviction | 2.5 | Just a dict wrapper, no eviction, no thread-safety, minimal tests |
| very_poor_partial | 1.5 | Stub only, `pass` implementations, no tests, would not pass criterion |
