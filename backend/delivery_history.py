"""在内存中有限保留已确认的表情投递记录。"""

import time
from collections import OrderedDict


class DeliveryHistory:
    """为每个会话分别保留近期成功投递记录。"""

    def __init__(self, max_sessions=128, max_records=10, ttl=3600):
        self.max_sessions = max_sessions
        self.max_records = max_records
        self.ttl = ttl
        self._sessions = OrderedDict()

    def record(self, origin: str, record: dict) -> None:
        """追加一张已确认投递的图片，不保留路径或消息文本。

        Args:
            origin: 会话标识，不会发送给选图后端。
            record: 公开分类、候选 ID 及内容指纹。
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
        """返回该会话尚未过期的投递记录副本。

        Args:
            origin: 会话标识。

        Returns:
            近期记录；历史未知时返回空列表。
        """
        existing = self._sessions.get(origin)
        if existing is None:
            return []
        if time.monotonic() - existing[0] >= self.ttl:
            self._sessions.pop(origin, None)
            return []
        return [dict(item) for item in existing[1]]
