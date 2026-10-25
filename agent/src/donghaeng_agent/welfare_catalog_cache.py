"""Bounded, short-lived copies of successful public catalog reads."""
import os
from collections import OrderedDict
from concurrent.futures import Future
from copy import deepcopy
from hashlib import sha256
from threading import Lock
from time import monotonic


class CatalogCache:
    def __init__(self, ttl_seconds=60, max_entries=64, clock=monotonic):
        if ttl_seconds <= 0 or max_entries < 1:
            raise ValueError('cache lifetime and size must be positive')
        self.ttl_seconds, self.max_entries, self.clock = ttl_seconds, max_entries, clock
        self._entries = OrderedDict()
        self._pending = {}
        self._lock = Lock()

    def get_or_load(self, key, loader):
        if key is None:
            return deepcopy(loader())
        with self._lock:
            entry = self._entries.get(key)
            cached = entry is not None and entry[0] > self.clock()
            if cached:
                self._entries.move_to_end(key)
                value = entry[1]
            else:
                self._entries.pop(key, None)
                future = self._pending.get(key)
                owner = future is None
                if owner:
                    future = self._pending[key] = Future()
        if cached:
            return deepcopy(value)
        if not owner:
            return deepcopy(future.result())
        try:
            value = deepcopy(loader())
            with self._lock:
                self._entries[key] = (self.clock() + self.ttl_seconds, value)
                self._entries.move_to_end(key)
                while len(self._entries) > self.max_entries:
                    self._entries.popitem(last=False)
                self._pending.pop(key, None)
            future.set_result(value)
            return deepcopy(value)
        except BaseException as exc:
            with self._lock:
                self._pending.pop(key, None)
            future.set_exception(exc)
            raise

    def clear(self):
        """Clear completed reads; already-running reads may subsequently refill."""
        with self._lock:
            self._entries.clear()


def catalog_cache_namespace():
    """Separate connection identities without storing credentials in cache keys."""
    url = os.getenv('WELFARE_DATABASE_URL', '').strip()
    if not url:
        return None
    values = [url, os.getenv('WELFARE_DATABASE_PASSWORD', ''),
              os.getenv('WELFARE_DATABASE_SCHEMA', 'welfare').strip()]
    # Length prefixes avoid collisions between values containing separators.
    encoded = ''.join(f'{len(value)}:{value}' for value in values).encode()
    return sha256(encoded).hexdigest()


CATALOG_CACHE = CatalogCache()
