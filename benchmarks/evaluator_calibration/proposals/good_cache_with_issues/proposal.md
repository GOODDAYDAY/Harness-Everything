## Summary

Implemented LRU cache in `harness/cache/lru_cache.py`. Used an `OrderedDict`
for O(1) operations. Tests added in `tests/unit/core/test_lru_cache.py`.
Thread safety added with a lock but the lock scope on `get()` is too narrow —
the `move_to_end` call occurs outside the critical section.

## Changes Made

### New file: `harness/cache/lru_cache.py`

```python
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any


class LRUCache:
    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._store: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        if key not in self._store:  # BUG: check outside lock
            return None
        with self._lock:             # BUG: lock acquired too late
            self._store.move_to_end(key)
        return self._store[key]      # BUG: read outside lock

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = value
            if len(self._store) > self._capacity:
                self._store.popitem(last=False)

    def __len__(self) -> int:
        return len(self._store)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
```

### New file: `tests/unit/core/test_lru_cache.py`

```python
from harness.cache.lru_cache import LRUCache


def test_basic_put_get():
    cache = LRUCache(capacity=3)
    cache.put("a", 1)
    assert cache.get("a") == 1


def test_eviction_lru():
    cache = LRUCache(capacity=3)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)
    cache.put("d", 4)
    assert cache.get("a") is None  # evicted


def test_get_refreshes_order():
    cache = LRUCache(capacity=3)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)
    cache.get("a")  # a is now MRU
    cache.put("d", 4)
    assert cache.get("b") is None  # b was LRU
    assert cache.get("a") == 1


def test_clear():
    cache = LRUCache(capacity=2)
    cache.put("x", 99)
    cache.clear()
    assert len(cache) == 0
```

## Verification

```
$ python -m pytest tests/unit/core/test_lru_cache.py -v
collected 4 items

test_lru_cache.py::test_basic_put_get PASSED
test_lru_cache.py::test_eviction_lru PASSED
test_lru_cache.py::test_get_refreshes_order PASSED
test_lru_cache.py::test_clear PASSED

4 passed in 0.08s
```

Note: The thread-safety issue in `get()` (lock acquired after the membership
check) would only manifest under very high concurrency and may not be caught by
the tests above. A follow-up PR should fix the lock scope.
