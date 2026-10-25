from collections import OrderedDict
from threading import Lock
from time import monotonic

from donghaeng_agent.mock_contracts import RunResult


class RunStore:
    """Process-local, FIFO bounded demo history. Not persistent or multi-worker safe."""

    def __init__(self, capacity: int, ttl_seconds: int, clock=monotonic):
        self.capacity = capacity
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._lock = Lock()
        self._items: OrderedDict[str, tuple[float, RunResult]] = OrderedDict()

    def _expire(self):
        now = self.clock()
        expired = [key for key, (at, _) in self._items.items() if now - at >= self.ttl_seconds]
        for key in expired:
            del self._items[key]

    def put(self, result: RunResult):
        with self._lock:
            self._expire()
            self._items.pop(result.run_id, None)
            self._items[result.run_id] = (self.clock(), result.model_copy(deep=True))
            while len(self._items) > self.capacity:
                self._items.popitem(last=False)

    def get(self, run_id: str) -> RunResult | None:
        with self._lock:
            self._expire()
            item = self._items.get(run_id)
            return item[1].model_copy(deep=True) if item else None
