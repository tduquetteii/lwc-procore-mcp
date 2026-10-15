"""
An in-memory cache with a lifetime per entry.

Three things are worth remembering between calls: verified identities, each user's project
list, and the company's submittal statuses. Each has its own lifetime, and the verifier also
wants a lapsed identity back during a Procore outage, so an entry stays readable with peek()
for `keep_expired` seconds after it lapses. Lapsed entries are swept once the cache reaches
`limit` entries, so it stays bounded.
"""

import time


class TTLCache[K, V]:
    def __init__(self, limit: int = 1000, keep_expired: float = 0):
        self.limit = limit                # entry count at which lapsed entries are swept
        self.keep_expired = keep_expired  # seconds a lapsed entry stays readable with peek()
        self._entries: dict[K, tuple[float, V]] = {}  # key -> (expiry, value)

    def get(self, key: K) -> V | None:
        """The value while it lives, else None."""
        hit = self._entries.get(key)
        return hit[1] if hit and hit[0] > time.time() else None

    def peek(self, key: K) -> V | None:
        """The value whether or not it has lapsed, else None: for serving stale when fresh is not to be had."""
        hit = self._entries.get(key)
        return hit[1] if hit else None

    def set(self, key: K, value: V, ttl: float) -> None:
        """Remember `value` for `ttl` seconds."""
        now = time.time()
        if len(self._entries) >= self.limit:
            self._entries = {k: v for k, v in self._entries.items() if v[0] + self.keep_expired > now}
        if len(self._entries) >= self.limit:  # still full: the lapsed entries kept for an outage go too
            self._entries = {k: v for k, v in self._entries.items() if v[0] > now}
        self._entries[key] = (now + ttl, value)

    def pop(self, key: K) -> None:
        self._entries.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()
