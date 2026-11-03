from __future__ import annotations

import asyncio
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger
from astrbot.api.message_components import Image, Reply

from ..astrbot_compat import CommandFilter, CommandGroupFilter
from .auto_collect import AutoCollectManager

IDLE_TIMEOUT_SECONDS = 600
MAX_MANUAL_IMAGES = 10
COMMAND_HANDLERS = {"collect_memes", "start_collecting_memes", "stop_collecting_memes"}
OUTCOME_LABELS = {
    "imported": "已入包",
    "pending": "待审核",
    "waiting_import": "等待自动入包",
    "pending_failed": "入包失败，已保留待审",
    "duplicate": "重复",
    "rejected": "拒收",
    "quota": "识别额度不足",
    "queue_full": "队列已满",
    "inbox_full": "待审箱已满",
    "failed": "处理失败",
    "limit": "超出单条上限",
}


@dataclass
class ManualCollection:
    """Keep the target and outcomes of one explicit collection operation."""

    pack_id: str
    categories: dict[str, str]
    event: Any
    counts: Counter = field(default_factory=Counter)
    pending: int = 0
    total: int = 0
    closed: bool = False
    notified: bool = False
    last_activity: float = field(default_factory=time.monotonic)
    expiry_task: asyncio.Task | None = None

    def summary(self) -> str:
        """Return completed outcomes separately from outstanding work.

        Returns:
            A summary suitable for a chat response.
        """
        parts = [f"本次收到 {self.total} 张，目标表情包「{self.pack_id}」"]
        parts.extend(
            f"{label} {self.counts[key]} 张"
            for key, label in OUTCOME_LABELS.items()
            if self.counts[key]
        )
        if self.pending:
            parts.append(f"仍在处理 {self.pending} 张")
        return "；".join(parts) + "。"


class ManualCollectManager:
    """Coordinate quoted images and sender-scoped continuous collection."""

    def __init__(self, collection: AutoCollectManager):
        """Initialize transient state without starting background work.

        Args:
            collection: Shared bounded classification and storage pipeline.
        """
        self.collection = collection
        self.sessions: dict[tuple[str, str], ManualCollection] = {}
        self.tasks: set[asyncio.Task] = set()
        self.closing = False

    @staticmethod
    def key(event: Any) -> tuple[str, str]:
        """Identify a collector across platform, chat, and sender.

        Args:
            event: Incoming message event.

        Returns:
            Conversation identity and sender ID.
        """
        return (
            str(getattr(event, "unified_msg_origin", "") or event.session_id),
            str(event.get_sender_id()),
        )

    async def start(self, event: Any) -> str:
        """Start receiving direct images from this sender in this chat.

        Args:
            event: Administrator command event.

        Returns:
            Start instructions or an actionable configuration error.
        """
        key = self.key(event)
        if key in self.sessions:
            return "已在收集中；发送 /表情管理 结束收集 可结束。"
        try:
            pack_id, categories = await self.collection.prepare_manual(event)
        except (ValueError, OSError) as exc:
            return f"无法开始收集：{exc}"
        # Provider startup can yield; retain an already-created session.
        if key in self.sessions:
            return "已在收集中；发送 /表情管理 结束收集 可结束。"
        session = ManualCollection(pack_id, categories, event)
        self.sessions[key] = session
        task = asyncio.create_task(self._expire(key, session))
        session.expiry_task = task
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        destination = (
            "自动识别分类后进入后台待审核"
            if self.collection.manual_review
            else "符合条件的图片自动分类入包，必须复核的结果留待审核"
        )
        return (
            f"已开始收集，目标表情包：「{pack_id}」。\n"
            f"接下来仅接收你在本聊天发送的图片，每条最多 {MAX_MANUAL_IMAGES} 张；{destination}。\n"
            "发送 /表情管理 结束收集 结束；10 分钟未发送图片自动结束。"
        )

    async def quick(self, event: Any) -> str:
        """Collect direct images from a quoted message, or the command itself.

        Args:
            event: Command event optionally containing a reply component.

        Returns:
            Error text, or an empty string after acknowledging the submission.
        """
        components = event.message_obj.message
        reply = next((item for item in components if isinstance(item, Reply)), None)
        if reply is not None:
            if not reply.chain:
                return "无法读取引用消息内容，可能已失效或平台未提供；请重新发送图片并附带 /表情管理 收集表情。"
            components = reply.chain
        images = [item for item in components if isinstance(item, Image)]
        if not images:
            return (
                "引用消息中没有可收集的直接图片。"
                if reply is not None
                else "请引用含图片的消息，或附带图片发送 /表情管理 收集表情。"
            )
        try:
            pack_id, categories = await self.collection.prepare_manual(event)
        except (ValueError, OSError) as exc:
            return f"无法收集：{exc}"
        session = ManualCollection(pack_id, categories, event, closed=True)
        return await self._submit(event, session, images)

    def stop(self, event: Any) -> str:
        """Stop receiving new images while preserving submitted jobs.

        Args:
            event: End command event used for the eventual final summary.

        Returns:
            Current outcomes, including jobs that are still processing.
        """
        session = self.sessions.pop(self.key(event), None)
        if session is None:
            return "当前没有连续收集任务；发送 /表情管理 开始收集 可开始。"
        session.closed = True
        session.event = event
        if session.expiry_task:
            session.expiry_task.cancel()
        session.notified = session.pending == 0
        return "已结束收集。" + session.summary()

    async def consume(self, event: Any) -> str | None:
        """Route active uploads and suppress passive handling of commands.

        Args:
            event: Incoming message, after AstrBot has resolved command handlers.

        Returns:
            Response text, an empty string for a consumed event, or None otherwise.
        """
        handlers = event.get_extra("activated_handlers") or []
        if event.get_extra("meme_manager_manual_collection_command") or any(
            handler.handler_name in COMMAND_HANDLERS for handler in handlers
        ):
            return ""
        session = self.sessions.get(self.key(event))
        if session is None:
            return None
        # Other commands must not accidentally supply images to this session.
        if any(
            isinstance(event_filter, (CommandFilter, CommandGroupFilter))
            for handler in handlers
            for event_filter in handler.event_filters
        ):
            return ""
        if time.monotonic() - session.last_activity >= IDLE_TIMEOUT_SECONDS:
            self.sessions.pop(self.key(event), None)
            session.closed = True
            session.event = event
            session.notified = session.pending == 0
            if session.expiry_task:
                session.expiry_task.cancel()
            return "连续收集已因空闲 10 分钟结束，本条图片未收集。" + session.summary()
        images = [item for item in event.message_obj.message if isinstance(item, Image)]
        if not images:
            return ""
        session.last_activity = time.monotonic()
        return await self._submit(event, session, images)

    async def _submit(
        self, event: Any, session: ManualCollection, images: list[Image]
    ) -> str:
        """Snapshot a bounded batch and observe its eventual real outcomes.

        Args:
            event: Event owning the source image files.
            session: Target and aggregate statistics for this operation.
            images: Direct images from one message.

        Returns:
            An empty string after sending the acknowledgment before final results.
        """
        selected = images[:MAX_MANUAL_IMAGES]
        session.total += len(images)
        session.counts["limit"] += len(images) - len(selected)
        session.pending += len(selected)
        snapshot = asyncio.create_task(
            self.collection.submit_manual(
                event, selected, session.pack_id, session.categories
            )
        )
        self.tasks.add(snapshot)
        snapshot.add_done_callback(self.tasks.discard)
        completions = await snapshot
        if self.closing:
            return ""
        note = (
            f"，另有 {len(images) - len(selected)} 张超出单条上限"
            if len(images) > len(selected)
            else ""
        )
        acknowledgment = f"已提交 {len(selected)} 张至表情包「{session.pack_id}」处理{note}；识别、去重与入包结果以处理汇总为准。"
        try:
            await event.send(event.plain_result(acknowledgment))
        finally:
            if not self.closing:
                task = asyncio.create_task(self._observe(session, completions))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
        return ""

    async def _observe(
        self, session: ManualCollection, completions: list[asyncio.Future[str]]
    ) -> None:
        """Accumulate completed results and send one final closed-session summary.

        Args:
            session: Operation to update.
            completions: Futures resolved by the shared image worker.
        """
        try:
            for completion in asyncio.as_completed(completions):
                outcome = await completion
                session.counts[outcome] += 1
                session.pending -= 1
            if (
                session.closed
                and not session.pending
                and not session.notified
                and not self.closing
            ):
                session.notified = True
                await session.event.send(
                    session.event.plain_result("收集处理完成。" + session.summary())
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "[meme_manager] Failed to report manual collection outcomes"
            )

    async def _expire(self, key: tuple[str, str], session: ManualCollection) -> None:
        """End an idle receiver without cancelling its queued image processing.

        Args:
            key: Conversation and sender that own this session.
            session: Exact session instance protected against replacement.
        """
        try:
            while self.sessions.get(key) is session:
                remaining = IDLE_TIMEOUT_SECONDS - (
                    time.monotonic() - session.last_activity
                )
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    continue
                del self.sessions[key]
                session.closed = True
                session.notified = session.pending == 0
                await session.event.send(
                    session.event.plain_result(
                        "连续收集已因空闲 10 分钟结束。" + session.summary()
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[meme_manager] Failed to report idle collection expiry")

    async def close(self) -> None:
        """Cancel transient receivers and reporters on plugin unload."""
        self.closing = True
        self.sessions.clear()
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
