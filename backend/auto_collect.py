from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import os
import re
import shutil
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image as PILImage

from astrbot.api import logger
from astrbot.core.message.components import Image

from ..config import PACKS_DIR, PLUGIN_DATA_DIR, TEMP_DIR
from ..utils import probability_hit
from .auto_collect_index import AutoCollectImageIndex
from .packs.categories import is_safe_category_name
from .packs.protocol import validate_pack_id
from .packs.resolver import get_pack_paths, load_pack_category_mapping
from .semantic.caption import (
    CAPTION_ANALYSIS_PROMPT,
    _structured_output_is_unsupported,
    prepare_visual_inputs,
)
from .semantic.models import PROMPT_VERSION, REVIEW_CATEGORY
from .semantic.storage import save_collected_image_semantic

AUTO_COLLECT_INBOX_DIR = PLUGIN_DATA_DIR / "auto_collect_inbox"
AUTO_COLLECT_INBOX_IMAGES_DIR = AUTO_COLLECT_INBOX_DIR / "images"
AUTO_COLLECT_INBOX_METADATA_PATH = AUTO_COLLECT_INBOX_DIR / "metadata.json"
AUTO_COLLECT_STATE_PATH = PLUGIN_DATA_DIR / "auto_collect_state.json"
AUTO_COLLECT_TEMP_DIR = TEMP_DIR / "auto_collect"
AUTO_COLLECT_SCHEMA_VERSION = 1
AUTO_COLLECT_PROMPT_VERSION = "auto-collect-v2-review"
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_DECISION_CACHE_ITEMS = 2000
QUEUE_SIZE = 20
_IMAGE_INDEX = AutoCollectImageIndex()

FORMAT_EXTENSIONS = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "GIF": ".gif",
    "WEBP": ".webp",
}

AUTO_COLLECT_SYSTEM_PROMPT = (
    """你是聊天表情包审核与分类器。请判断图片是否适合保存为聊天表情包，并从给定分类中选择最合适的一项。
普通照片、文档截图、聊天记录截图、商品图、二维码、收付款码、广告和没有明确聊天反应用途的图片默认不是表情包。
表情包可以是静态图或动图，也可以包含文字。不得创造分类名，只能逐字返回给定分类之一；无法可靠分类时 category 留空。
只返回 JSON，不要输出分析过程。"""
    + CAPTION_ANALYSIS_PROMPT
    + """
请根据图片实际内容选择分类，而非将已有分类当作前提。caption 必须描述核心含义及聊天使用场景；tags 返回 6-10 个细粒度标签；visible_text 必须忠实记录图片文字。仍然仅返回上述审核 JSON 格式。"""
)


@dataclass(slots=True)
class AutoCollectJob:
    """保存单张图片自动收集请求的快照。

    Args:
        snapshot_path: 在消息事件结束前创建的插件自有图片快照路径。
        target_pack_id: 最终接收图片的表情包 ID。
        categories: 用于识别分类的运行时分类描述。
        source_kind: 来源类型，值为 ``group`` 或 ``user``。
        source_id: 原始群聊或个人用户 ID。
    """

    snapshot_path: Path
    target_pack_id: str
    categories: dict[str, str]
    source_kind: str
    source_id: str
    digest: str = ""


class AutoCollectManager:
    """执行有界的后台视觉分类，并安全地收集图片。"""

    def __init__(self, plugin: Any, config: dict[str, Any]):
        """初始化运行时状态，但不启动后台任务。

        Args:
            plugin: MemeSender 插件实例。
            config: 已规范化的自动收集配置。
        """
        self.plugin = plugin
        self.enabled = bool(config.get("enabled", False))
        self.manual_review = bool(config.get("manual_review", True))
        self.pending_limit = max(1, int(config.get("pending_limit", 200) or 200))
        self.counters: dict[str, int] = {}
        self._inflight: set[str] = set()
        self.vision_provider_id = str(config.get("vision_provider_id") or "").strip()
        self.scope = {
            str(value).strip()
            for value in config.get("scope", [])
            if str(value).strip()
        }
        self.target_pack_id = str(config.get("target_pack_id") or "").strip()
        self.sampling_probability = max(
            0, min(100, int(config.get("sampling_probability", 100) or 0))
        )
        self.cooldown_seconds = max(
            0, min(3600, int(config.get("cooldown_seconds", 20) or 0))
        )
        self.daily_recognition_limit = max(
            0, int(config.get("daily_recognition_limit", 100) or 0)
        )
        self.min_meme_confidence = max(
            0.0, min(1.0, float(config.get("min_meme_confidence", 0.85) or 0))
        )
        self.min_category_confidence = max(
            0.0,
            min(1.0, float(config.get("min_category_confidence", 0.65) or 0)),
        )
        self.queue: asyncio.Queue[AutoCollectJob] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._worker_task: asyncio.Task | None = None
        self._snapshot_tasks: set[asyncio.Task] = set()
        self._ready = False
        self._cooldowns: dict[str, float] = {}
        self._inbox_lock = asyncio.Lock()
        self._state = self._load_json(
            AUTO_COLLECT_STATE_PATH,
            {"schema_version": AUTO_COLLECT_SCHEMA_VERSION, "decisions": {}},
        )

    @staticmethod
    def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
        """加载 JSON 对象，失败时返回独立的默认值副本。

        Args:
            path: JSON 文件路径。
            default: 加载失败时使用的默认映射。

        Returns:
            解析后的映射，或默认映射的副本。
        """
        try:
            with path.open(encoding="utf-8") as file_obj:
                value = json.load(file_obj)
            return value if isinstance(value, dict) else dict(default)
        except Exception:
            return dict(default)

    @staticmethod
    def _save_json(path: Path, value: dict[str, Any]) -> None:
        """以原子方式保存 JSON 对象。

        Args:
            path: 目标 JSON 文件路径。
            value: 可序列化的映射。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        with temporary.open("w", encoding="utf-8") as file_obj:
            json.dump(value, file_obj, ensure_ascii=False, indent=2)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temporary, path)

    async def start(self) -> None:
        """校验已配置的视觉模型，并启动一个后台工作任务。"""
        if not self.enabled or self._worker_task is not None:
            return
        if not self.vision_provider_id:
            logger.warning("[meme_manager] 已启用自动收集，但尚未选择视觉模型")
            return
        provider = self.plugin.context.get_provider_by_id(self.vision_provider_id)
        if provider is None:
            logger.info(
                "[meme_manager] 自动收集视觉模型尚未就绪，等待加载后重试：%s",
                self.vision_provider_id,
            )
            return
        provider_config = getattr(provider, "provider_config", {})
        modalities = (
            provider_config.get("modalities")
            if isinstance(provider_config, dict)
            else None
        )
        if (
            isinstance(modalities, list)
            and modalities
            and "image" not in {str(item).strip().lower() for item in modalities}
        ):
            logger.warning(
                "[meme_manager] 自动收集视觉模型不支持图片输入：%s",
                self.vision_provider_id,
            )
            return
        self._ready = True
        self._worker_task = asyncio.create_task(
            self._worker(), name="meme_manager_auto_collect"
        )
        logger.info(
            "[meme_manager] 自动收集后台任务已启动，视觉模型：%s",
            self.vision_provider_id,
        )

    async def close(self) -> None:
        """停止后台任务，并等待取消清理完成。"""
        self._ready = False
        snapshot_tasks = tuple(self._snapshot_tasks)
        for snapshot_task in snapshot_tasks:
            snapshot_task.cancel()
        if snapshot_tasks:
            await asyncio.gather(*snapshot_tasks, return_exceptions=True)
        self._snapshot_tasks.clear()
        task = self._worker_task
        self._worker_task = None
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        while True:
            try:
                job = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._inflight.discard(f"{job.target_pack_id}:{job.digest}")
            job.snapshot_path.unlink(missing_ok=True)
            self.queue.task_done()

    def _source_allowed(self, event: Any) -> tuple[bool, str, str]:
        """检查消息是否匹配配置的群聊或个人来源范围。

        Args:
            event: 当前 AstrBot 消息事件。

        Returns:
            是否允许该来源、来源类型和来源 ID 组成的元组。
        """
        private = bool(event.is_private_chat())
        source_kind = "user" if private else "group"
        source_id = str(
            event.get_sender_id() if private else event.get_group_id()
        ).strip()
        if not source_id:
            source_id = str(event.get_session_id() or "").strip()
        if not self.scope:
            return True, source_kind, source_id
        candidates = {
            source_id,
            f"{source_kind}:{source_id}",
            str(getattr(event, "unified_msg_origin", "") or "").strip(),
        }
        return bool(self.scope.intersection(candidates)), source_kind, source_id

    async def submit(self, event: Any) -> bool:
        """过滤消息，并将第一张符合条件的直接图片快照加入队列。

        Args:
            event: 当前 AstrBot 消息事件。

        Returns:
            成功加入任务队列时返回 True。
        """
        if not self.enabled:
            return False
        images = [
            component
            for component in event.message_obj.message
            if isinstance(component, Image)
        ]
        if not images:
            return False
        allowed, source_kind, source_id = self._source_allowed(event)
        if not self._ready:
            self.counters["not_ready"] = self.counters.get("not_ready", 0) + 1
            logger.info(
                "[meme_manager] 自动收集跳过图片：后台任务尚未就绪，来源：%s:%s",
                source_kind,
                source_id,
            )
            return False
        if not allowed:
            self.counters["scope"] = self.counters.get("scope", 0) + 1
            logger.info(
                "[meme_manager] 自动收集跳过图片：消息来源不在收集范围内，来源：%s:%s",
                source_kind,
                source_id,
            )
            return False
        if not probability_hit(self.sampling_probability):
            self.counters["sampling"] = self.counters.get("sampling", 0) + 1
            logger.info(
                "[meme_manager] 自动收集跳过图片：未命中采样概率，来源：%s:%s",
                source_kind,
                source_id,
            )
            return False
        raw_message = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw_message, Mapping):
            raw_segments = raw_message.get("message")
            if isinstance(raw_segments, list):
                raw_image_data = [
                    segment.get("data")
                    for segment in raw_segments
                    if isinstance(segment, Mapping)
                    and segment.get("type") == "image"
                    and isinstance(segment.get("data"), Mapping)
                ]
                if len(raw_image_data) == len(images):
                    # NapCat 仅在原始 OneBot 消息段中提供图片分类。
                    # AstrBot 的 Image 组件会丢弃这些扩展字段，因此按位置对应图片。
                    classified_images = []
                    classifier_metadata_available = False
                    for image, data in zip(images, raw_image_data, strict=True):
                        summary = str(data.get("summary") or "").strip()
                        explicit_summary = summary in {
                            "动画表情",
                            "[动画表情]",
                            "[表情]",
                        }
                        try:
                            image_sub_type = int(data.get("sub_type"))
                        except (TypeError, ValueError):
                            image_sub_type = None
                        if (
                            image_sub_type is not None
                            or data.get("emoji_id")
                            or data.get("emoji_package_id")
                            or explicit_summary
                        ):
                            classifier_metadata_available = True
                        if (
                            image_sub_type is None
                            or image_sub_type == 1
                            or bool(data.get("emoji_id"))
                            or bool(data.get("emoji_package_id"))
                            or explicit_summary
                        ):
                            classified_images.append(image)
                    if classifier_metadata_available:
                        images = classified_images
        if not images:
            self.counters["platform_filtered"] = (
                self.counters.get("platform_filtered", 0) + 1
            )
            logger.info(
                "[meme_manager] 自动收集跳过图片：平台图片分类未匹配表情，来源：%s:%s",
                source_kind,
                source_id,
            )
            return False
        if self.queue.qsize() + len(self._snapshot_tasks) >= QUEUE_SIZE:
            self.counters["queue_full"] = self.counters.get("queue_full", 0) + 1
            logger.info(
                "[meme_manager] 自动收集跳过图片：待处理队列已满，来源：%s:%s",
                source_kind,
                source_id,
            )
            return False

        try:
            if self.target_pack_id:
                target_pack_id = validate_pack_id(
                    self.target_pack_id, "自动收集目标表情包"
                )
                if not (PACKS_DIR / target_pack_id).is_dir():
                    logger.warning(
                        "[meme_manager] 自动收集目标表情包不存在：%s",
                        target_pack_id,
                    )
                    return False
                categories = load_pack_category_mapping(target_pack_id)
            else:
                context = self.plugin._resolve_runtime_pack_context(event=event)
                target_pack_id = str(context.get("pack_id") or "").strip()
                categories = dict(context.get("category_mapping") or {})
        except Exception as exc:
            logger.warning("[meme_manager] 解析自动收集目标失败：%s", exc)
            return False
        if not target_pack_id or not categories:
            self.counters["no_categories"] = self.counters.get("no_categories", 0) + 1
            logger.warning(
                "[meme_manager] 自动收集跳过图片：目标表情包没有可用分类，表情包：%s",
                target_pack_id,
            )
            return False

        image = images[0]
        source = str(image.url or image.file or "")
        snapshot = self._snapshot_and_enqueue(
            image, target_pack_id, categories, source_kind, source_id
        )
        if source.startswith(("https://", "http://")):
            task = asyncio.create_task(
                snapshot, name="meme_manager_auto_collect_snapshot"
            )
            self._snapshot_tasks.add(task)
            task.add_done_callback(self._snapshot_tasks.discard)
            return True
        return await snapshot

    async def _snapshot_and_enqueue(
        self,
        image: Image,
        target_pack_id: str,
        categories: dict[str, str],
        source_kind: str,
        source_id: str,
    ) -> bool:
        """先为候选图片创建快照并预留摘要，再检查冷却时间。

        Args:
            image: 与消息事件独立的候选图片组件。
            target_pack_id: 接收候选图片的表情包 ID。
            categories: 目标表情包的分类描述。
            source_kind: 群聊或个人消息来源类型。
            source_id: 用于冷却检查的来源 ID。

        Returns:
            快照是否成功进入识别队列。
        """
        snapshot_path: Path | None = None
        copy_task: asyncio.Task | None = None
        record_id: str | None = None
        reserved = False
        queued = False
        try:
            local_path = Path(
                await asyncio.wait_for(image.convert_to_file_path(), timeout=30)
            )
            file_size = (await asyncio.to_thread(local_path.stat)).st_size
            if file_size > MAX_IMAGE_BYTES:
                self.counters["snapshot_failed"] = (
                    self.counters.get("snapshot_failed", 0) + 1
                )
                logger.warning("[meme_manager] 自动收集跳过图片：文件超过 20 MiB 限制")
                return False
            AUTO_COLLECT_TEMP_DIR.mkdir(parents=True, exist_ok=True)
            snapshot_file = tempfile.NamedTemporaryFile(
                prefix="queued_",
                suffix=local_path.suffix,
                dir=AUTO_COLLECT_TEMP_DIR,
                delete=False,
            )
            snapshot_path = Path(snapshot_file.name)
            snapshot_file.close()
            copy_task = asyncio.create_task(
                asyncio.to_thread(shutil.copyfile, local_path, snapshot_path)
            )
            # 取消线程任务不会停止复制，删除快照前必须等待复制结束。
            await asyncio.shield(copy_task)
            if (await asyncio.to_thread(snapshot_path.stat)).st_size > MAX_IMAGE_BYTES:
                self.counters["snapshot_failed"] = (
                    self.counters.get("snapshot_failed", 0) + 1
                )
                logger.warning("[meme_manager] 自动收集跳过图片：快照超过 20 MiB 限制")
                return False
            copy_task = asyncio.create_task(asyncio.to_thread(snapshot_path.read_bytes))
            digest = hashlib.sha256(await asyncio.shield(copy_task)).hexdigest()
            record_id = f"{target_pack_id}:{digest}"
            pack_duplicate = await asyncio.to_thread(
                self._pack_contains_digest, target_pack_id, digest
            )
            metadata = self._load_json(AUTO_COLLECT_INBOX_METADATA_PATH, {"items": {}})
            if (
                pack_duplicate
                or record_id in self._inflight
                or record_id in metadata.get("items", {})
                or digest in self._state.get("rejected", {}).get(target_pack_id, [])
            ):
                self.counters["duplicate_or_rejected"] = (
                    self.counters.get("duplicate_or_rejected", 0) + 1
                )
                if pack_duplicate:
                    reason = "表情包中已有相同图片"
                elif record_id in self._inflight:
                    reason = "相同图片正在处理"
                elif record_id in metadata.get("items", {}):
                    reason = "相同图片已在待审箱"
                else:
                    reason = "图片已在拒绝名单"
                logger.info(
                    "[meme_manager] 自动收集跳过图片：%s，表情包：%s，图片摘要：%s",
                    reason,
                    target_pack_id,
                    digest[:12],
                )
                return False
            if not self._ready:
                self.counters["not_ready"] = self.counters.get("not_ready", 0) + 1
                logger.info(
                    "[meme_manager] 自动收集跳过图片：快照完成后后台任务已停止，表情包：%s，图片摘要：%s",
                    target_pack_id,
                    digest[:12],
                )
                return False
            cooldown_key = f"{source_kind}:{source_id}"
            now = time.monotonic()
            if now - self._cooldowns.get(cooldown_key, 0.0) < self.cooldown_seconds:
                self.counters["cooldown"] = self.counters.get("cooldown", 0) + 1
                logger.info(
                    "[meme_manager] 自动收集跳过图片：来源仍在冷却时间内，来源：%s:%s，表情包：%s",
                    source_kind,
                    source_id,
                    target_pack_id,
                )
                return False
            self._inflight.add(record_id)
            reserved = True
            self.queue.put_nowait(
                AutoCollectJob(
                    snapshot_path=snapshot_path,
                    target_pack_id=target_pack_id,
                    categories=categories,
                    source_kind=source_kind,
                    source_id=source_id,
                    digest=digest,
                )
            )
            queued = True
            self._cooldowns[cooldown_key] = now
            return True
        except asyncio.CancelledError:
            if copy_task is not None and not copy_task.done():
                await asyncio.gather(copy_task, return_exceptions=True)
            raise
        except asyncio.QueueFull:
            self.counters["queue_full"] = self.counters.get("queue_full", 0) + 1
            logger.info(
                "[meme_manager] 自动收集跳过图片：入队时队列已满，表情包：%s",
                target_pack_id,
            )
            return False
        except Exception as exc:
            self.counters["snapshot_failed"] = (
                self.counters.get("snapshot_failed", 0) + 1
            )
            logger.warning("[meme_manager] 自动收集图片快照失败：%s", exc)
            return False
        finally:
            if not queued:
                if reserved and record_id is not None:
                    self._inflight.discard(record_id)
                if snapshot_path is not None:
                    snapshot_path.unlink(missing_ok=True)

    async def _worker(self) -> None:
        """串行处理队列中的图片，以限制视觉模型负载。"""
        while True:
            job = await self.queue.get()
            try:
                await self._process_job(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.counters["failed"] = self.counters.get("failed", 0) + 1
                logger.error(
                    "[meme_manager] 自动收集处理图片失败：%s",
                    exc,
                    exc_info=True,
                )
            finally:
                self._inflight.discard(f"{job.target_pack_id}:{job.digest}")
                job.snapshot_path.unlink(missing_ok=True)
                self.queue.task_done()

    @staticmethod
    def _validate_image(content: bytes) -> str:
        """校验图片的实际内容，并返回可信的扩展名。

        Args:
            content: 原始图片字节。

        Returns:
            可信的小写文件扩展名。

        Raises:
            ValueError: 图片为空、过大、格式不受支持或内容不安全。
        """
        if not content:
            raise ValueError("图片内容为空")
        if len(content) > MAX_IMAGE_BYTES:
            raise ValueError("图片超过 20 MiB 限制")
        try:
            with PILImage.open(io.BytesIO(content)) as image:
                image_format = str(image.format or "").upper()
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    raise ValueError("图片尺寸无效或过大")
                image.verify()
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"图片格式不受支持或文件已损坏：{exc}") from exc
        extension = FORMAT_EXTENSIONS.get(image_format)
        if not extension:
            raise ValueError(f"不支持的图片格式：{image_format or '未知'}")
        return extension

    def _daily_call_available(self) -> bool:
        """在配额可用时占用一次当天的识图次数。

        Returns:
            视觉识别请求可以继续时返回 True。
        """
        today = datetime.now(timezone.utc).date().isoformat()
        if str(self._state.get("recognition_date") or "") != today:
            self._state["recognition_date"] = today
            self._state["recognition_count"] = 0
        count = max(0, int(self._state.get("recognition_count", 0) or 0))
        if self.daily_recognition_limit and count >= self.daily_recognition_limit:
            return False
        self._state["recognition_count"] = count + 1
        self._save_json(AUTO_COLLECT_STATE_PATH, self._state)
        return True

    def _cache_key(
        self, digest: str, target_pack_id: str, categories: dict[str, str]
    ) -> str:
        """生成会随目标分类变化的缓存键。

        Args:
            digest: 图片的 SHA-256 摘要。
            target_pack_id: 分类目标表情包 ID。
            categories: 当前分类描述。

        Returns:
            稳定的识别结果缓存键。
        """
        catalog = json.dumps(categories, ensure_ascii=False, sort_keys=True)
        catalog_hash = hashlib.sha256(catalog.encode("utf-8")).hexdigest()[:16]
        provider = (
            self.plugin.context.get_provider_by_id(self.vision_provider_id)
            if hasattr(self.plugin, "context")
            else None
        )
        provider_config = getattr(provider, "provider_config", {})
        signature = hashlib.sha256(
            json.dumps(provider_config, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]
        return f"{AUTO_COLLECT_PROMPT_VERSION}:{PROMPT_VERSION}:{self.vision_provider_id}:{signature}:{target_pack_id}:{catalog_hash}:{digest}"

    def _remember_decision(self, key: str, decision: dict[str, Any]) -> None:
        """保存一次成功的分类结果，并限制历史记录数量。

        Args:
            key: 识别结果缓存键。
            decision: 已规范化的分类结果。
        """
        decisions = self._state.setdefault("decisions", {})
        if not isinstance(decisions, dict):
            decisions = {}
            self._state["decisions"] = decisions
        decisions[key] = {
            **decision,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        while len(decisions) > MAX_DECISION_CACHE_ITEMS:
            decisions.pop(next(iter(decisions)))
        self._save_json(AUTO_COLLECT_STATE_PATH, self._state)

    async def _classify(
        self,
        content: bytes,
        extension: str,
        categories: dict[str, str],
    ) -> dict[str, Any]:
        """调用已配置的视觉模型，并规范化其 JSON 结果。

        Args:
            content: 已通过校验的图片字节。
            extension: 可信的图片扩展名。
            categories: 允许使用的分类及其描述。

        Returns:
            已规范化的分类结果。
        """
        AUTO_COLLECT_TEMP_DIR.mkdir(parents=True, exist_ok=True)
        source_file = tempfile.NamedTemporaryFile(
            prefix="incoming_",
            suffix=extension,
            dir=AUTO_COLLECT_TEMP_DIR,
            delete=False,
        )
        try:
            source_file.write(content)
            source_file.close()
            visual_paths, frame_paths = prepare_visual_inputs(source_file.name)
            prompt = (
                "可选分类及用途如下：\n"
                + json.dumps(categories, ensure_ascii=False, indent=2)
                + "\n请返回："
                + '{"is_meme":true,"meme_confidence":0.0,"category":"",'
                + '"category_confidence":0.0,"reason":"",'
                + '"caption":"","tags":[],"visible_text":""}'
            )
            request_data = {
                "chat_provider_id": self.vision_provider_id,
                "prompt": prompt,
                "image_urls": visual_paths,
                "system_prompt": AUTO_COLLECT_SYSTEM_PROMPT,
                "temperature": 0,
                "max_tokens": 1400,
                "response_format": {"type": "json_object"},
            }
            try:
                response = await self.plugin.context.llm_generate(**request_data)
            except Exception as exc:
                if not _structured_output_is_unsupported(exc):
                    raise
                request_data.pop("response_format", None)
                response = await self.plugin.context.llm_generate(**request_data)
            finally:
                for frame_path in frame_paths:
                    Path(frame_path).unlink(missing_ok=True)
        finally:
            source_file.close()
            Path(source_file.name).unlink(missing_ok=True)

        raw_text = str(getattr(response, "completion_text", "") or "").strip()
        try:
            payload = json.loads(raw_text)
        except (TypeError, ValueError):
            match = re.search(r"\{[\s\S]*\}", raw_text)
            if not match:
                raise ValueError("视觉模型未返回 JSON 对象")
            payload = json.loads(match.group(0))
        if not isinstance(payload, dict):
            raise ValueError("视觉模型结果必须是 JSON 对象")

        raw_is_meme = payload.get("is_meme", False)
        is_meme = (
            raw_is_meme
            if isinstance(raw_is_meme, bool)
            else str(raw_is_meme).strip().lower() in {"true", "1", "yes"}
        )
        confidences = {}
        for key in ("meme_confidence", "category_confidence"):
            try:
                value = float(payload.get(key, 0) or 0)
            except (TypeError, ValueError):
                value = 0.0
            confidences[key] = (
                max(0.0, min(1.0, value)) if math.isfinite(value) else 0.0
            )
        meme_confidence = confidences["meme_confidence"]
        category_confidence = confidences["category_confidence"]
        requested_category = str(payload.get("category") or "").strip()
        folded_categories = {name.casefold(): name for name in categories}
        category = (
            requested_category
            if requested_category in categories
            else folded_categories.get(requested_category.casefold(), "")
        )
        tags = payload.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        return {
            "semantic_prompt_version": PROMPT_VERSION,
            "semantic_category": category,
            "vision_model": self.vision_provider_id,
            "is_meme": bool(is_meme),
            "meme_confidence": meme_confidence,
            "category": category,
            "category_confidence": category_confidence,
            "reason": str(payload.get("reason") or "").strip()[:500],
            "caption": str(payload.get("caption") or "").strip()[:1000],
            "tags": [str(tag).strip()[:100] for tag in tags[:12] if str(tag).strip()],
            "visible_text": str(payload.get("visible_text") or "").strip()[:1000],
        }

    @staticmethod
    def _pack_contains_digest(pack_id: str, digest: str) -> bool:
        """检查表情包内是否已有内容完全相同的受支持图片。

        Args:
            pack_id: 目标表情包 ID。
            digest: 要查找的 SHA-256 摘要。

        Returns:
            表情包内已存在相同字节内容时返回 True。
        """
        return (
            _IMAGE_INDEX.lookup(get_pack_paths(pack_id)["memes_dir"], digest).exact_path
            is not None
        )

    @staticmethod
    def _write_pack_image(
        pack_id: str,
        category: str,
        content: bytes,
        digest: str,
        extension: str,
    ) -> Path | None:
        """通过原子重命名将图片写入指定表情包。

        Args:
            pack_id: 有效的目标表情包 ID。
            category: 安全的分类名。
            content: 已通过校验的图片字节。
            digest: SHA-256 摘要。
            extension: 可信的文件扩展名。

        Returns:
            保存后的路径；图片已存在时返回 ``None``。

        Raises:
            ValueError: 目标表情包或分类路径不安全。
        """
        pack_id = validate_pack_id(pack_id, "自动收集目标表情包")
        if not is_safe_category_name(category):
            raise ValueError("自动收集分类名不安全")
        pack_dir = (PACKS_DIR / pack_id).resolve()
        packs_root = PACKS_DIR.resolve()
        try:
            pack_dir.relative_to(packs_root)
        except ValueError as exc:
            raise ValueError("自动收集表情包路径超出了表情包根目录") from exc
        if not pack_dir.is_dir() or (PACKS_DIR / pack_id).is_symlink():
            raise ValueError("自动收集目标表情包不可用")
        if AutoCollectManager._pack_contains_digest(pack_id, digest):
            return None
        category_dir = (pack_dir / "memes" / category).resolve()
        try:
            category_dir.relative_to(pack_dir / "memes")
        except ValueError as exc:
            raise ValueError("自动收集分类路径超出了目标表情包") from exc
        if category_dir.exists() and category_dir.is_symlink():
            raise ValueError("自动收集分类目录不能是符号链接")
        category_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{int(time.time() * 1000)}_{digest[:12]}{extension}"
        target = category_dir / filename
        temporary = category_dir / f".{filename}.tmp"
        temporary.write_bytes(content)
        os.replace(temporary, target)
        return target

    async def _save_to_inbox(
        self,
        job: AutoCollectJob,
        content: bytes,
        digest: str,
        extension: str,
        category: str,
        decision: dict[str, Any],
    ) -> None:
        """将已收集图片保存到所属表情包的独立待审箱。

        Args:
            job: 自动收集任务快照。
            content: 已通过校验的图片字节。
            digest: SHA-256 摘要。
            extension: 可信的扩展名。
            category: 建议的目标分类。
            decision: 已规范化的视觉识别结果。
        """
        async with self._inbox_lock:
            metadata = self._load_json(
                AUTO_COLLECT_INBOX_METADATA_PATH,
                {"schema_version": AUTO_COLLECT_SCHEMA_VERSION, "items": {}},
            )
            items = metadata.setdefault("items", {})
            if not isinstance(items, dict):
                items = {}
                metadata["items"] = items
            record_id = f"{job.target_pack_id}:{digest}"
            if record_id in items:
                logger.info(
                    "[meme_manager] 自动收集跳过图片：保存时发现图片已在待审箱，表情包：%s，图片摘要：%s",
                    job.target_pack_id,
                    digest[:12],
                )
                return
            if (
                sum(
                    isinstance(item, dict)
                    and item.get("target_pack_id") == job.target_pack_id
                    for item in items.values()
                )
                >= self.pending_limit
            ):
                self.counters["inbox_full"] = self.counters.get("inbox_full", 0) + 1
                logger.info(
                    "[meme_manager] 自动收集跳过图片：待审箱已满，表情包：%s，图片摘要：%s",
                    job.target_pack_id,
                    digest[:12],
                )
                return
            AUTO_COLLECT_INBOX_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
            filename = f"{job.target_pack_id}__{digest}{extension}"
            target = AUTO_COLLECT_INBOX_IMAGES_DIR / filename
            temporary = AUTO_COLLECT_INBOX_IMAGES_DIR / f".{filename}.tmp"
            temporary.write_bytes(content)
            os.replace(temporary, target)
            items[record_id] = {
                "id": record_id,
                "target_pack_id": job.target_pack_id,
                "content_sha256": digest,
                "filename": filename,
                "suggested_category": category,
                "source_kind": job.source_kind,
                "source_id": job.source_id,
                "received_at": datetime.now(timezone.utc).isoformat(),
                "classification": decision,
            }
            self._save_json(AUTO_COLLECT_INBOX_METADATA_PATH, metadata)
        self.counters["collected"] = self.counters.get("collected", 0) + 1
        logger.info(
            "[meme_manager] 已收集图片，等待审核；表情包：%s，分类：%s",
            job.target_pack_id,
            category,
        )

    async def _process_job(self, job: AutoCollectJob) -> None:
        """下载、去重、分类并路由一张队列中的图片。

        Args:
            job: 自动收集任务快照。
        """
        content = await asyncio.to_thread(job.snapshot_path.read_bytes)
        extension = await asyncio.to_thread(self._validate_image, content)
        digest = hashlib.sha256(content).hexdigest()
        metadata = self._load_json(AUTO_COLLECT_INBOX_METADATA_PATH, {"items": {}})
        if f"{job.target_pack_id}:{digest}" in metadata.get(
            "items", {}
        ) or digest in self._state.get("rejected", {}).get(job.target_pack_id, []):
            self.counters["duplicate_or_rejected"] = (
                self.counters.get("duplicate_or_rejected", 0) + 1
            )
            reason = (
                "图片已在待审箱"
                if f"{job.target_pack_id}:{digest}" in metadata.get("items", {})
                else "图片已在拒绝名单"
            )
            logger.info(
                "[meme_manager] 自动收集跳过图片：%s，表情包：%s，图片摘要：%s",
                reason,
                job.target_pack_id,
                digest[:12],
            )
            return
        if (
            sum(
                isinstance(item, dict)
                and item.get("target_pack_id") == job.target_pack_id
                for item in metadata.get("items", {}).values()
            )
            >= self.pending_limit
        ):
            self.counters["inbox_full"] = self.counters.get("inbox_full", 0) + 1
            logger.info(
                "[meme_manager] 自动收集跳过图片：待审箱已满，表情包：%s，图片摘要：%s",
                job.target_pack_id,
                digest[:12],
            )
            return
        if await asyncio.to_thread(
            self._pack_contains_digest, job.target_pack_id, digest
        ):
            self.counters["duplicate_or_rejected"] = (
                self.counters.get("duplicate_or_rejected", 0) + 1
            )
            logger.info(
                "[meme_manager] 自动收集跳过图片：表情包中已有相同图片，表情包：%s，图片摘要：%s",
                job.target_pack_id,
                digest[:12],
            )
            return

        cache_key = self._cache_key(digest, job.target_pack_id, job.categories)
        decisions = self._state.get("decisions", {})
        decision = decisions.get(cache_key) if isinstance(decisions, dict) else None
        if not isinstance(decision, dict):
            if not self._daily_call_available():
                self.counters["quota"] = self.counters.get("quota", 0) + 1
                logger.info(
                    "[meme_manager] 自动收集跳过图片：已达到每日识别上限，表情包：%s，图片摘要：%s",
                    job.target_pack_id,
                    digest[:12],
                )
                return
            decision = await self._classify(content, extension, job.categories)
            self._remember_decision(cache_key, decision)
        if (
            not bool(decision.get("is_meme"))
            or float(decision.get("meme_confidence", 0) or 0) < self.min_meme_confidence
        ):
            self.counters["model_rejected"] = self.counters.get("model_rejected", 0) + 1
            reason = (
                "视觉模型判定不是表情包"
                if not bool(decision.get("is_meme"))
                else "视觉模型的表情包置信度不足"
            )
            logger.info(
                "[meme_manager] 自动收集跳过图片：%s，表情包：%s，图片摘要：%s",
                reason,
                job.target_pack_id,
                digest[:12],
            )
            return
        category = str(decision.get("category") or "").strip()
        if category not in job.categories or (
            not self.manual_review
            and float(decision.get("category_confidence", 0) or 0)
            < self.min_category_confidence
        ):
            category = REVIEW_CATEGORY
        match = await asyncio.to_thread(
            _IMAGE_INDEX.lookup,
            get_pack_paths(job.target_pack_id)["memes_dir"],
            digest,
            job.snapshot_path,
        )
        if match.similar_path is not None:
            decision = {
                **decision,
                "near_duplicate": str(
                    match.similar_path.relative_to(
                        get_pack_paths(job.target_pack_id)["memes_dir"]
                    )
                ).replace("\\", "/"),
            }
        await self._save_to_inbox(job, content, digest, extension, category, decision)
        if not self.manual_review:
            try:
                await self.accept_pending(
                    job.target_pack_id,
                    [{"id": f"{job.target_pack_id}:{digest}"}],
                    manual_confirmed=False,
                )
            except RuntimeError:
                self.counters["pack_busy"] = self.counters.get("pack_busy", 0) + 1
                logger.info("[meme_manager] 表情包正在处理，已收集图片保留在待审箱")

    async def pending_status(self, pack_id: str) -> dict[str, Any]:
        """List every candidate belonging to a pack, with confident items first.

        Args:
            pack_id: Target pack identifier.

        Returns:
            Review items, categories, limits, and collection counters.
        """
        pack_id = validate_pack_id(pack_id, "表情包")
        async with self._inbox_lock:
            metadata = self._load_json(AUTO_COLLECT_INBOX_METADATA_PATH, {"items": {}})
            records = []
            raw_items = metadata.get("items", {})
            for item in raw_items.values() if isinstance(raw_items, dict) else []:
                if not isinstance(item, dict) or item.get("target_pack_id") != pack_id:
                    continue
                decision = item.get("classification", {})
                if not isinstance(decision, dict):
                    decision = {}
                confidence_values = {}
                for key in ("meme_confidence", "category_confidence"):
                    try:
                        value = float(decision.get(key, 0) or 0)
                    except (TypeError, ValueError):
                        value = 0.0
                    confidence_values[key] = (
                        max(0.0, min(1.0, value)) if math.isfinite(value) else 0.0
                    )
                records.append(
                    {
                        **{
                            key: item.get(key, "")
                            for key in (
                                "id",
                                "suggested_category",
                                "source_kind",
                                "source_id",
                                "received_at",
                            )
                        },
                        **{
                            key: decision.get(key, "")
                            for key in (
                                "reason",
                                "caption",
                                "visible_text",
                                "near_duplicate",
                            )
                        },
                        "tags": decision.get("tags", []),
                        **confidence_values,
                    }
                )
        records.sort(
            key=lambda item: (
                item["category_confidence"],
                item["meme_confidence"],
                item["received_at"],
            ),
            reverse=True,
        )
        return {
            "visible": True,
            "count": len(records),
            "items": records,
            "categories": load_pack_category_mapping(pack_id),
            "manual_review": self.manual_review,
            "pending_limit": self.pending_limit,
            "counters": dict(self.counters),
        }

    def pending_image_path(
        self, pack_id: str, record_id: str, *, _record: dict[str, Any] | None = None
    ) -> Path:
        """Resolve a candidate while enforcing pack ownership and file containment.

        Args:
            pack_id: Target pack identifier.
            record_id: Candidate identifier.
            _record: Internal batch record read while holding the inbox lock.

        Returns:
            Safe existing image path.

        Raises:
            ValueError: The record belongs to another pack or has an unsafe path.
            FileNotFoundError: The candidate or image no longer exists.
        """
        pack_id = validate_pack_id(pack_id, "表情包")
        item = _record
        if item is None:
            metadata = self._load_json(AUTO_COLLECT_INBOX_METADATA_PATH, {"items": {}})
            items = metadata.get("items", {})
            item = items.get(record_id) if isinstance(items, dict) else None
        if not isinstance(item, dict):
            raise FileNotFoundError("待审核图片不存在")
        digest = str(item.get("content_sha256") or "")
        if (
            item.get("target_pack_id") != pack_id
            or record_id != f"{pack_id}:{digest}"
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ValueError("待审核图片不属于当前表情包")
        source = AUTO_COLLECT_INBOX_IMAGES_DIR / str(item.get("filename") or "")
        if (
            source.is_symlink()
            or source.resolve().parent != AUTO_COLLECT_INBOX_IMAGES_DIR.resolve()
        ):
            raise ValueError("待审核图片路径无效")
        if not source.is_file():
            raise FileNotFoundError("待审核图片文件不存在")
        return source

    async def accept_pending(
        self,
        pack_id: str,
        selections: list[dict[str, Any]],
        *,
        manual_confirmed: bool = True,
    ) -> dict[str, int]:
        """Accept selected candidates, retaining failed candidates for retry.

        Args:
            pack_id: Target pack identifier.
            selections: Candidate IDs with optional category overrides.
            manual_confirmed: Whether a person explicitly accepted these images.

        Returns:
            Imported, duplicate, and failed item counts.

        Raises:
            ValueError: Selection, ownership, or category is invalid.
            RuntimeError: Another operation holds the pack write lock.
        """
        pack_id = validate_pack_id(pack_id, "表情包")
        if not (PACKS_DIR / pack_id).is_dir():
            raise FileNotFoundError("表情包不存在")
        if not isinstance(selections, list) or not selections or len(selections) > 2000:
            raise ValueError("请选择要接收的图片")
        categories = load_pack_category_mapping(pack_id)
        result = {"imported": 0, "duplicates": 0, "failed": 0}
        async with self._inbox_lock:
            metadata = self._load_json(AUTO_COLLECT_INBOX_METADATA_PATH, {"items": {}})
            items = metadata.get("items", {})
            validated = {}
            for selection in selections:
                if not isinstance(selection, dict):
                    raise ValueError("图片选择格式无效")
                record_id = str(selection.get("id") or "")
                source = self.pending_image_path(
                    pack_id, record_id, _record=items.get(record_id, {})
                )
                item = items[record_id]
                category = str(
                    selection.get("category")
                    or item.get("suggested_category")
                    or REVIEW_CATEGORY
                )
                if category not in categories and category != REVIEW_CATEGORY:
                    raise ValueError("请选择当前表情包中的分类")
                validated[record_id] = (source, category)
            self.plugin.semantic_task_manager.begin_external_pack_operation(
                pack_id, "审核自动收集图片"
            )
            try:
                for record_id, (source, category) in validated.items():
                    item = items[record_id]
                    try:
                        content = await asyncio.to_thread(source.read_bytes)
                        digest = hashlib.sha256(content).hexdigest()
                        if digest != item.get("content_sha256"):
                            raise ValueError("待审核图片内容已改变")
                        extension = await asyncio.to_thread(
                            self._validate_image, content
                        )
                        saved = await asyncio.to_thread(
                            self._write_pack_image,
                            pack_id,
                            category,
                            content,
                            digest,
                            extension,
                        )
                        if saved is not None:
                            try:
                                await asyncio.to_thread(
                                    save_collected_image_semantic,
                                    PACKS_DIR / pack_id,
                                    saved,
                                    item.get("classification", {}),
                                    manual_confirmed=manual_confirmed,
                                )
                            except Exception:
                                saved.unlink(missing_ok=True)
                                raise
                            result["imported"] += 1
                        else:
                            result["duplicates"] += 1
                        del items[record_id]
                        self._save_json(AUTO_COLLECT_INBOX_METADATA_PATH, metadata)
                        source.unlink(missing_ok=True)
                    except Exception as exc:
                        result["failed"] += 1
                        logger.error(
                            "[meme_manager] 接收候选图片 %s 失败：%s",
                            record_id,
                            exc,
                        )
            finally:
                self.plugin.semantic_task_manager.end_external_pack_operation(pack_id)
        if result["imported"]:
            await self.plugin.reload_emotions()
        return result

    async def discard_pending(
        self, pack_id: str, ids: list[str], remember_rejection: bool = False
    ) -> dict[str, int]:
        """Discard selected candidates and optionally block their recollection.

        Args:
            pack_id: Target pack identifier.
            ids: Selected candidate identifiers.
            remember_rejection: Persist exact hashes as rejected for this pack.

        Returns:
            Number of discarded candidates.

        Raises:
            ValueError: Selection or pack ownership is invalid.
        """
        if (
            not isinstance(ids, list)
            or not ids
            or len(ids) > 2000
            or any(not isinstance(record_id, str) for record_id in ids)
        ):
            raise ValueError("请选择要丢弃的图片")
        async with self._inbox_lock:
            metadata = self._load_json(AUTO_COLLECT_INBOX_METADATA_PATH, {"items": {}})
            items = metadata.get("items", {})
            paths = {
                record_id: self.pending_image_path(
                    pack_id, record_id, _record=items.get(record_id, {})
                )
                for record_id in ids
            }
            if remember_rejection:
                rejected = self._state.setdefault("rejected", {}).setdefault(
                    pack_id, []
                )
                rejected.extend(
                    items[record_id]["content_sha256"]
                    for record_id in paths
                    if items[record_id]["content_sha256"] not in rejected
                )
                self._save_json(AUTO_COLLECT_STATE_PATH, self._state)
            for record_id in paths:
                del items[record_id]
            self._save_json(AUTO_COLLECT_INBOX_METADATA_PATH, metadata)
            for source in paths.values():
                source.unlink(missing_ok=True)
        return {"discarded": len(paths)}

    async def import_pending(self, pack_id: str) -> dict[str, int]:
        """Import legacy inbox candidates only when manual review is disabled.

        Args:
            pack_id: Target pack identifier.

        Returns:
            Import result counts.

        Raises:
            RuntimeError: Manual review requires explicit candidate selections.
        """
        if self.manual_review:
            raise RuntimeError("请在表情包管理页面预览并选择要接收的图片")
        status = await self.pending_status(pack_id)
        if not status["items"]:
            return {"imported": 0, "duplicates": 0, "failed": 0}
        return await self.accept_pending(
            pack_id, [{"id": item["id"]} for item in status["items"]]
        )
