"""TTL in-memory cache for dashboard endpoints.

Single-worker gunicorn (-w 1) makes a simple module-level dict safe.
Caching strategy per TIDALHIRES_DASHBOARD_API.md: short TTLs (5-60s)
keyed by (endpoint_path, params). Write actions evict relevant keys.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

_cache: dict[tuple, tuple[float, Any]] = {}
_lock = threading.Lock()
_MAX_ENTRIES = 200


def get_or_compute(key: tuple, ttl_sec: float, compute: Callable[[], Any]) -> Any:
    """Return cached value if fresh, else compute + cache."""
    now = time.monotonic()
    with _lock:
        entry = _cache.get(key)
        if entry is not None and (now - entry[0]) < ttl_sec:
            return entry[1]

    value = compute()

    with _lock:
        if len(_cache) >= _MAX_ENTRIES:
            # LRU-lite: drop oldest entry
            oldest = min(_cache, key=lambda k: _cache[k][0])
            _cache.pop(oldest, None)
        _cache[key] = (now, value)
    return value


def invalidate_prefix(prefix: str) -> int:
    """Drop all cache entries whose first key-element starts with prefix.
    Used after write-actions to force fresh data on next read."""
    with _lock:
        keys_to_drop = [k for k in _cache if isinstance(k[0], str) and k[0].startswith(prefix)]
        for k in keys_to_drop:
            del _cache[k]
        return len(keys_to_drop)


def clear() -> None:
    with _lock:
        _cache.clear()
