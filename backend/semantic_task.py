"""语义化任务状态机：串行、可暂停、可重试、可断点续传。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .semantic_caption import generate_caption
from .semantic_index import EmbeddingAdapter, build_index, faiss_is_available
from .semantic_models import PROMPT_VERSION, SemanticImage, text_hash, utc_now
from .semantic_storage import (
    load_metadata,
    reconcile_metadata,
    safe_relative_path,
    save_metadata,
)


class SemanticTaskManager:
    def __init__(
        self,
        plugin_data_dir: Path | str,
        *,
        context: Any = None,
        config: dict | None = None,
    ):
        self.plugin_data_dir = Path(plugin_data_dir).resolve()
        self.context = context
        self.config = config or {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._pause_events: dict[str, asyncio.Event] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def close(self) -> None:
        """插件卸载时取消后台任务；每张图片的状态已在处理前后持久化。"""
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _lock(self, pack_id: str) -> asyncio.Lock:
        return self._locks.setdefault(pack_id, asyncio.Lock())

    @staticmethod
    def _validate_pack_id(pack_id: str) -> str:
        value = str(pack_id or "").strip()
        if not value or not re.fullmatch(r"[A-Za-z0-9._-]{2,64}", value):
            raise ValueError("pack_id 无效")
        return value

    def _state_path(self, pack_id: str) -> Path:
        return (
            self.plugin_data_dir / "semantic_indexes" / str(pack_id) / "task_state.json"
        )

    def _load_state(self, pack_id: str) -> dict[str, Any]:
        path = self._state_path(pack_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self, pack_id: str, state: dict[str, Any]) -> None:
        path = self._state_path(pack_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=".task_state.", suffix=".json", dir=path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file_obj:
                json.dump(state, file_obj, ensure_ascii=False, indent=2)
                file_obj.flush()
                os.fsync(file_obj.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def _pack_dir(self, pack_id: str) -> Path:
        return self.plugin_data_dir / "packs" / str(pack_id)

    def _safe_error(self, error: Any, pack_id: str = "") -> str:
        message = str(error or "未知错误")
        for secret_path in (str(self.plugin_data_dir), str(self._pack_dir(pack_id))):
            if secret_path:
                message = message.replace(secret_path, "<本地资源>")
        return message[:500]

    def _vision_provider_ready(self) -> bool:
        if self.context is None or not callable(
            getattr(self.context, "llm_generate", None)
        ):
            return False
        provider_id = str(
            self.config.get("vision_provider_id")
            or self.config.get("visual_provider_id")
            or ""
        ).strip()
        if not provider_id:
            return True
        resolver = getattr(self.context, "get_provider_by_id", None)
        if not callable(resolver):
            return False
        try:
            provider = resolver(provider_id)
        except Exception:
            return False
        if provider is None:
            return False
        provider_config = getattr(provider, "provider_config", {})
        modalities = (
            provider_config.get("modalities")
            if isinstance(provider_config, dict)
            else None
        )
        if isinstance(modalities, list) and modalities:
            return "image" in {
                str(modality or "").strip().lower() for modality in modalities
            }
        return True

    def capabilities(
        self, pack_id: str, *, embedding_provider: Any = None
    ) -> dict[str, Any]:
        pack_dir = self._pack_dir(pack_id)
        metadata = load_metadata(pack_dir)
        state = self._load_state(pack_id)
        provider = embedding_provider or self._resolve_embedding_provider()
        return {
            "vision_provider_ready": self._vision_provider_ready(),
            "embedding_provider_ready": EmbeddingAdapter(
                provider, str(self.config.get("embedding_provider_id") or "")
            ).ready,
            "faiss_ready": faiss_is_available(),
            "semantic_metadata_ready": bool(metadata.get("images")),
            "task_status": str(state.get("task_status") or "idle"),
        }

    def _resolve_embedding_provider(self) -> Any:
        if self.config.get("embedding_provider") is not None:
            return self.config.get("embedding_provider")
        context = self.context
        if context is None:
            return None
        provider_id = str(self.config.get("embedding_provider_id") or "").strip()
        if provider_id:
            resolver = getattr(context, "get_provider_by_id", None)
            if not callable(resolver):
                return None
            try:
                return resolver(provider_id)
            except Exception:
                return None
        resolver = getattr(context, "get_all_embedding_providers", None)
        if not callable(resolver):
            return None
        try:
            providers = resolver()
            return providers[0] if providers else None
        except Exception:
            return None

    def status(self, pack_id: str) -> dict[str, Any]:
        pack_id = self._validate_pack_id(pack_id)
        pack_dir = self._pack_dir(pack_id)
        data = load_metadata(pack_dir)
        if not data.get("images") and pack_dir.is_dir():
            data = reconcile_metadata(pack_dir)
        state = self._load_state(pack_id)
        images = data.get("images", {})
        caption_done = sum(
            1
            for item in images.values()
            if isinstance(item, dict) and item.get("caption_status") == "done"
        )
        caption_failed = sum(
            1
            for item in images.values()
            if isinstance(item, dict) and item.get("caption_status") == "failed"
        )
        embedding_done = sum(
            1
            for item in images.values()
            if isinstance(item, dict) and item.get("embedding_status") == "done"
        )
        embedding_failed = sum(
            1
            for item in images.values()
            if isinstance(item, dict) and item.get("embedding_status") == "failed"
        )
        pending = sum(
            1
            for item in images.values()
            if isinstance(item, dict)
            and (
                item.get("caption_status") != "done"
                or item.get("embedding_status") != "done"
            )
        )
        return {
            "pack_id": pack_id,
            "task_status": state.get("task_status", "idle"),
            "file_total": int(data.get("file_total", len(images))),
            "unique_total": int(data.get("unique_total", len(images))),
            "reused_duplicate_files": int(data.get("reused_duplicate_files", 0)),
            "caption_done": caption_done,
            "caption_failed": caption_failed,
            "embedding_done": embedding_done,
            "embedding_failed": embedding_failed,
            "pending": pending,
            "current": state.get("current", ""),
            "last_error": state.get("last_error"),
            "error_items": [
                {"relative_path": item.get("relative_path"), "error": item.get("error")}
                for item in images.values()
                if isinstance(item, dict) and item.get("error")
            ][-20:],
            **self.capabilities(pack_id),
        }

    async def start(
        self,
        pack_id: str,
        *,
        mode: str = "full",
        force: bool = False,
        external_data: dict | None = None,
    ) -> dict[str, Any]:
        pack_id = self._validate_pack_id(pack_id)
        mode = str(mode or "full").strip().lower()
        if mode not in {"full", "retry_failed"}:
            raise ValueError("mode 只能是 full 或 retry_failed")
        pack_dir = self._pack_dir(pack_id)
        if not pack_dir.is_dir():
            raise FileNotFoundError(f"表情包 {pack_id} 不存在")
        async with self._lock(pack_id):
            existing = self._tasks.get(pack_id)
            if existing and not existing.done():
                raise RuntimeError("同一个资源包已经有语义化任务在运行")
            metadata = reconcile_metadata(pack_dir, external_data=external_data)
            if force:
                for item in metadata.get("images", {}).values():
                    item["caption_status"] = "pending"
                    item["embedding_status"] = "pending"
                    item["error"] = None
            elif mode == "retry_failed":
                for item in metadata.get("images", {}).values():
                    if item.get("caption_status") == "failed":
                        item["caption_status"] = "pending"
                    if item.get("embedding_status") == "failed":
                        item["embedding_status"] = "pending"
                    item["error"] = None
            expected_prompt = str(self.config.get("prompt_version") or PROMPT_VERSION)
            expected_vision = str(self.config.get("vision_provider_id") or "")
            if not force:
                for item in metadata.get("images", {}).values():
                    if not isinstance(item, dict) or item.get("provenance") == "manual":
                        continue
                    if item.get("caption_status") == "done" and item.get(
                        "prompt_version"
                    ) not in {None, expected_prompt}:
                        item["caption_status"] = "pending"
                        item["embedding_status"] = "pending"
                    if (
                        expected_vision
                        and item.get("caption_status") == "done"
                        and item.get("vision_model")
                        and item.get("vision_model") != expected_vision
                    ):
                        item["caption_status"] = "pending"
                        item["embedding_status"] = "pending"
            needs_caption = any(
                isinstance(item, dict) and item.get("caption_status") != "done"
                for item in metadata.get("images", {}).values()
            )
            if needs_caption and not self._vision_provider_ready():
                raise RuntimeError("未配置视觉模型，无法生成图片描述")
            save_metadata(pack_dir, metadata)
            state = {
                "task_status": "running",
                "current": "",
                "started_at": utc_now(),
                "last_error": None,
            }
            self._save_state(pack_id, state)
            pause_event = self._pause_events.setdefault(pack_id, asyncio.Event())
            pause_event.set()
            task = asyncio.create_task(self._run(pack_id, mode=mode, force=force))
            self._tasks[pack_id] = task
        return self.status(pack_id)

    async def pause(self, pack_id: str) -> dict[str, Any]:
        pack_id = self._validate_pack_id(pack_id)
        state = self._load_state(pack_id)
        if state.get("task_status") == "running":
            state["task_status"] = "paused"
            state["updated_at"] = utc_now()
            self._save_state(pack_id, state)
            self._pause_events.setdefault(pack_id, asyncio.Event()).clear()
        return self.status(pack_id)

    async def resume(self, pack_id: str) -> dict[str, Any]:
        pack_id = self._validate_pack_id(pack_id)
        async with self._lock(pack_id):
            state = self._load_state(pack_id)
            task = self._tasks.get(pack_id)
            task_is_running = bool(task and not task.done())
            if state.get("task_status") in {
                "running",
                "paused",
                "failed",
                "completed_with_errors",
            }:
                state["task_status"] = "running"
                state["updated_at"] = utc_now()
                self._save_state(pack_id, state)
                event = self._pause_events.setdefault(pack_id, asyncio.Event())
                event.set()
                if not task_is_running:
                    self._tasks[pack_id] = asyncio.create_task(
                        self._run(pack_id, mode="full", force=False)
                    )
        return self.status(pack_id)

    async def retry(self, pack_id: str) -> dict[str, Any]:
        pack_id = self._validate_pack_id(pack_id)
        return await self.start(pack_id, mode="retry_failed")

    async def rebuild_index(
        self, pack_id: str, *, force: bool = False
    ) -> dict[str, Any]:
        pack_id = self._validate_pack_id(pack_id)
        pack_dir = self._pack_dir(pack_id)
        if not pack_dir.is_dir():
            raise FileNotFoundError(f"表情包 {pack_id} 不存在")
        async with self._lock(pack_id):
            active_task = self._tasks.get(pack_id)
            if active_task and not active_task.done():
                raise RuntimeError("语义化任务尚未结束，请等待任务完成后再重建索引")
            provider = EmbeddingAdapter(
                self._resolve_embedding_provider(),
                str(self.config.get("embedding_provider_id") or ""),
            )
            if not provider.ready:
                raise RuntimeError("未配置向量模型，无法建立索引")
            return await build_index(
                pack_dir, self.plugin_data_dir, pack_id, provider, force=force
            )

    async def _run(self, pack_id: str, *, mode: str, force: bool) -> None:
        pack_dir = self._pack_dir(pack_id)
        try:
            metadata = load_metadata(pack_dir)
            vision_provider = str(
                self.config.get("vision_provider_id")
                or self.config.get("visual_provider_id")
                or ""
            )
            embedding = EmbeddingAdapter(
                self._resolve_embedding_provider(),
                str(self.config.get("embedding_provider_id") or ""),
            )
            for digest, raw_item in list(metadata.get("images", {}).items()):
                state = self._load_state(pack_id)
                if state.get("task_status") == "paused":
                    await self._pause_events.setdefault(pack_id, asyncio.Event()).wait()
                item = SemanticImage.from_dict(raw_item)
                if (
                    not force
                    and mode != "retry_failed"
                    and item.caption_status == "done"
                    and item.embedding_status == "done"
                ):
                    continue
                self._save_state(
                    pack_id,
                    {
                        **state,
                        "task_status": "running",
                        "current": item.relative_path,
                        "updated_at": utc_now(),
                    },
                )
                path = safe_relative_path(pack_dir, item.relative_path)
                if path is None or not path.is_file():
                    item.caption_status = "pending"
                    item.embedding_status = "pending"
                    item.error = "图片路径无效或文件不存在"
                    raw_item.update(item.to_dict())
                    save_metadata(pack_dir, metadata)
                    continue
                try:
                    if force or item.caption_status != "done":
                        item.caption_status = "running"
                        raw_item.update(item.to_dict())
                        save_metadata(pack_dir, metadata)
                        caption = await generate_caption(
                            self.context, path, vision_provider
                        )
                        item.caption = caption["caption"]
                        item.tags = caption["tags"]
                        item.auto_tags = caption["tags"]
                        item.visible_text = caption["visible_text"]
                        item.vision_model = caption.get("vision_model", "")
                        item.prompt_version = caption.get(
                            "prompt_version", item.prompt_version
                        )
                        item.caption_status = "done"
                        item.embedding_status = "pending"
                        item.text_hash = text_hash(item.vector_text)
                        item.error = None
                except Exception as exc:
                    item.error = self._safe_error(exc, pack_id)
                    if item.caption_status != "done":
                        item.caption_status = "failed"
                item.updated_at = utc_now()
                raw_item.update(item.to_dict())
                save_metadata(pack_dir, metadata)
            has_caption = any(
                isinstance(item, dict) and item.get("caption_status") == "done"
                for item in metadata.get("images", {}).values()
            )
            if embedding.ready and has_caption:
                # build_index 会从旧 FAISS 复用未变化向量，只补充新增或变化的图片。
                await build_index(
                    pack_dir,
                    self.plugin_data_dir,
                    pack_id,
                    embedding,
                    force=force,
                )
            elif has_caption:
                for item in metadata.get("images", {}).values():
                    if (
                        not isinstance(item, dict)
                        or item.get("caption_status") != "done"
                    ):
                        continue
                    item["embedding_status"] = "failed"
                    item["error"] = "未配置 AstrBot 核心向量模型"
                save_metadata(pack_dir, metadata)
            latest = load_metadata(pack_dir)
            failed = any(
                isinstance(item, dict)
                and (
                    item.get("caption_status") != "done"
                    or item.get("embedding_status") != "done"
                )
                for item in latest.get("images", {}).values()
            )
            self._save_state(
                pack_id,
                {
                    "task_status": "completed_with_errors" if failed else "completed",
                    "current": "",
                    "updated_at": utc_now(),
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._save_state(
                pack_id,
                {
                    "task_status": "failed",
                    "current": "",
                    "last_error": self._safe_error(exc, pack_id),
                    "updated_at": utc_now(),
                },
            )
