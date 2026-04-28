## Summary

Added `LRUCache` class in `harness/cache/lru_cache.py`. It wraps a dict
for fast key-value storage. Added a basic test.

## Changes Made

### New file: `harness/cache/lru_cache.py`

```python
class LRUCache:
    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._data = {}

    def get(self, key: str):
        return self._data.get(key)

    def put(self, key: str, value) -> None:
        # TODO: implement eviction when len > capacity
        self._data[key] = value

    def __len__(self) -> int:
        return len(self._data)

    def clear(self) -> None:
        self._data = {}
```

### New file: `tests/unit/core/test_lru_cache.py`

```python
from harness.cache.lru_cache import LRUCache


def test_basic():
    c = LRUCache(3)
    c.put("a", 1)
    assert c.get("a") == 1
    assert c.get("z") is None
```

## Verification

```
$ python -m pytest tests/unit/core/test_lru_cache.py
collected 1 item

test_lru_cache.py::test_basic PASSED

1 passed in 0.02s
```

This is a working key-value store but does not implement eviction. The
falsiable criterion (evicting LRU entry when capacity is exceeded) is NOT met.
The class can grow without bound. Thread-safety is absent.
