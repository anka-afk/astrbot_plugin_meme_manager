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
    """保存一次显式收集操作的目标与结果。"""

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
        """分别返回已完成结果与待处理任务。

        Returns:
            适合聊天回复的汇总信息。
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
    """协调引用图片收集与按发送者隔离的连续收集。"""

    def __init__(self, collection: AutoCollectManager):
        """初始化临时状态，不启动后台任务。

        Args:
            collection: 共享的有限分类与存储处理管线。
        """
        self.collection = collection
        self.sessions: dict[tuple[str, str], ManualCollection] = {}
        self.tasks: set[asyncio.Task] = set()
        self.closing = False

    @staticmethod
    def key(event: Any) -> tuple[str, str]:
        """根据平台、聊天与发送者识别收集会话。

        Args:
            event: 收到的消息事件。

        Returns:
            会话标识与发送者 ID。
        """
        return (
            str(getattr(event, "unified_msg_origin", "") or event.session_id),
            str(event.get_sender_id()),
        )

    async def start(self, event: Any) -> str:
        """开始接收此发送者在当前聊天中直接发送的图片。

        Args:
            event: 管理员命令事件。

        Returns:
            开始收集的指引，或可据此处理的配置错误。
        """
        key = self.key(event)
        if key in self.sessions:
            return "已在收集中；发送 /表情管理 结束收集 可结束。"
        try:
            pack_id, categories = await self.collection.prepare_manual(event)
        except (ValueError, OSError) as exc:
            return f"无法开始收集：{exc}"
        # 提供方启动时可能让出执行权，保留已创建的会话。
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
        """收集引用消息或命令本身直接附带的图片。

        Args:
            event: 可选地包含引用组件的命令事件。

        Returns:
            错误文本；已确认提交时返回空字符串。
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
        """停止接收新图片，保留已提交的任务。

        Args:
            event: 用于发送最终汇总的结束命令事件。

        Returns:
            当前结果，包含仍在处理的任务。
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
        """路由收集中上传的图片，并阻止命令触发被动处理。

        Args:
            event: AstrBot 已解析命令处理器后的传入消息。

        Returns:
            回复文本、表示事件已消费的空字符串，或 None。
        """
        handlers = event.get_extra("activated_handlers") or []
        if event.get_extra("meme_manager_manual_collection_command") or any(
            handler.handler_name in COMMAND_HANDLERS for handler in handlers
        ):
            return ""
        session = self.sessions.get(self.key(event))
        if session is None:
            return None
        # 避免其他命令意外向当前收集会话提供图片。
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
        """为数量受限的一批图片创建快照，并观察其最终真实结果。

        Args:
            event: 来源图片文件所属的事件。
            session: 本次操作的目标与累计统计信息。
            images: 一条消息直接附带的图片。

        Returns:
            在最终结果前发送确认消息，然后返回空字符串。
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
        """累计已完成结果，并为已结束的收集会话发送一次最终汇总。

        Args:
            session: 待更新的收集会话。
            completions: 由共享图片工作任务完成的 Future。
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
            logger.exception("[meme_manager] 汇报手动收集结果失败")

    async def _expire(self, key: tuple[str, str], session: ManualCollection) -> None:
        """结束空闲的接收会话，不取消已排队的图片处理。

        Args:
            key: 当前收集会话所属的聊天与发送者。
            session: 用于防止替换混淆的确切会话实例。
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
            logger.exception("[meme_manager] 汇报收集空闲超时失败")

    async def close(self) -> None:
        """卸载插件时取消临时接收会话与汇报任务。"""
        self.closing = True
        self.sessions.clear()
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
