"""Bounded, in-memory records of confirmed meme deliveries."""

import time
from collections import OrderedDict


class DeliveryHistory:
    """Retain recent successful deliveries separately for each conversation."""

    def __init__(self, max_sessions=128, max_records=10, ttl=3600):
        self.max_sessions = max_sessions
        self.max_records = max_records
        self.ttl = ttl
        self._sessions = OrderedDict()

    def record(self, origin: str, record: dict) -> None:
        """Append one confirmed image without retaining paths or message text.

        Args:
            origin: Conversation identity, never sent to the selection backend.
            record: Public category, candidate ID, and content fingerprint.
        """
        if not origin:
            return
        now = time.monotonic()
        existing = self._sessions.pop(origin, (now, []))
        records = existing[1] if now - existing[0] < self.ttl else []
        records = [*records, dict(record)][-self.max_records :]
        self._sessions[origin] = (now, records)
        while len(self._sessions) > self.max_sessions:
            self._sessions.popitem(last=False)

    def recent(self, origin: str) -> list[dict]:
        """Return a copy of this conversation's unexpired delivery records.

        Args:
            origin: Conversation identity.

        Returns:
            Recent records, or an empty list when history is unknown.
        """
        existing = self._sessions.get(origin)
        if existing is None:
            return []
        if time.monotonic() - existing[0] >= self.ttl:
            self._sessions.pop(origin, None)
            return []
        return [dict(item) for item in existing[1]]
