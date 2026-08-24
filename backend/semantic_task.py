"""语义化任务状态机：有限并发、可暂停、可重试、可断点续传。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .semantic_caption import generate_caption
from .semantic_index import (
    EmbeddingAdapter,
    build_index,
    faiss_is_available,
    index_is_ready,
    load_index_manifest,
)
from .semantic_models import (
    REVIEW_CATEGORY,
    SemanticImage,
    ensure_category_tag,
    is_category_tag,
    semantic_caption_is_complete,
    text_hash,
    utc_now,
)
from .semantic_storage import (
    apply_conflict_reclassifications,
    confirm_image_category,
    get_image_semantic_detail,
    load_category_descriptions,
    load_metadata,
    reconcile_metadata,
    restore_image_auto_semantic,
    safe_relative_path,
    save_manual_image_semantic,
    save_manual_image_semantic_and_move,
    save_metadata,
    set_image_embedding_failure,
    validate_image_edit_snapshot,
)


def _revision_original_category(
    item: SemanticImage,
    existing_categories: set[str],
) -> str:
    """返回仍然存在的自动重分类前原分类，旧数据缺字段时查历史记录。"""
    direct = str(item.reclassified_from_category or "").strip()
    if (
        direct
        and direct != item.category
        and direct != REVIEW_CATEGORY
        and direct in existing_categories
    ):
        return direct
    for record in reversed(item.reclassification_history):
        candidate = str(record.get("from_category") or "").strip()
        if (
            candidate
            and candidate != item.category
            and candidate != REVIEW_CATEGORY
            and candidate in existing_categories
        ):
            return candidate
    return ""


def _revision_category_choice(
    *,
    current_category: str,
    original_category: str,
    category_fit: str,
    suggested_category: str,
    selectable_categories: set[str],
) -> tuple[str, str]:
    """把模型分类结果转换成前端可直接复核的目标分类和动作。"""
    current = str(current_category or "").strip()
    original = str(original_category or "").strip()
    fit = str(category_fit or "uncertain").strip()
    suggested = str(suggested_category or "").strip()
    if (
        fit == "conflict"
        and suggested
        and suggested != current
        and suggested in selectable_categories
    ):
        action = "return_original" if suggested == original else "move_to_other"
        return suggested, action
    if current != REVIEW_CATEGORY and fit in {"match", "uncertain"}:
        return current, "keep_current"
    return "", "manual_required"


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
        self._index_tasks: dict[str, asyncio.Task] = {}
        self._external_pack_operations: dict[str, str] = {}
        self._pause_events: dict[str, asyncio.Event] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def close(self) -> None:
        """插件卸载时取消后台任务；每张图片的状态已在处理前后持久化。"""
        tasks = list(
            {
                task
                for task in (*self._tasks.values(), *self._index_tasks.values())
                if not task.done()
            }
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _lock(self, pack_id: str) -> asyncio.Lock:
        return self._locks.setdefault(pack_id, asyncio.Lock())

    @staticmethod
    def _task_is_alive(task: asyncio.Task | None) -> bool:
        return bool(task and not task.done())

    def _semantic_operation_is_alive(self, pack_id: str) -> bool:
        return self._task_is_alive(self._tasks.get(pack_id)) or self._task_is_alive(
            self._index_tasks.get(pack_id)
        )

    def active_pack_tasks(self, exclude_pack_id: str = "") -> list[dict[str, Any]]:
        """返回本进程内其他资源包的活动任务，用于提示总请求压力。"""
        result = []
        pack_ids = set(self._tasks) | set(self._index_tasks)
        for pack_id in sorted(pack_ids):
            if pack_id == exclude_pack_id or not self._semantic_operation_is_alive(
                pack_id
            ):
                continue
            state = self._load_state(pack_id)
            try:
                concurrency = max(
                    1,
                    min(
                        16,
                        int(state.get("concurrency") or self._configured_concurrency()),
                    ),
                )
            except (TypeError, ValueError):
                concurrency = self._configured_concurrency()
            phase = str(state.get("task_phase") or "")
            result.append(
                {
                    "pack_id": pack_id,
                    "task_status": str(state.get("task_status") or "running"),
                    "task_phase": phase,
                    "concurrency": concurrency if phase != "indexing" else 0,
                }
            )
        return result

    def begin_external_pack_operation(self, pack_id: str, operation: str) -> None:
        """登记会改动资源包文件的外部任务，阻止语义任务同时启动。"""
        pack_id = self._validate_pack_id(pack_id)
        self.assert_pack_mutation_allowed(pack_id, operation)
        self._external_pack_operations[pack_id] = str(operation or "外部文件任务")

    def end_external_pack_operation(self, pack_id: str) -> None:
        self._external_pack_operations.pop(str(pack_id or "").strip(), None)

    def assert_pack_mutation_allowed(
        self, pack_id: str, operation: str = "修改资源包"
    ) -> None:
        """拒绝与语义任务、暂停队列或其他文件任务冲突的资源包修改。"""
        pack_id = self._validate_pack_id(pack_id)
        external_operation = self._external_pack_operations.get(pack_id)
        if external_operation:
            raise RuntimeError(
                f"资源包 {pack_id} 正在执行“{external_operation}”，暂时不能{operation}"
            )
        if self._semantic_operation_is_alive(pack_id):
            raise RuntimeError(
                f"资源包 {pack_id} 的语义任务尚未结束，暂时不能{operation}；"
                "请等待完成，如必须立即修改，请先在语义页清空任务队列"
            )
        state = self._load_state(pack_id)
        if state.get("task_status") in {"running", "paused"}:
            raise RuntimeError(
                f"资源包 {pack_id} 存在暂停或中断的语义队列，暂时不能{operation}；"
                "请先继续完成或清空任务队列"
            )

    async def confirm_category(
        self,
        pack_id: str,
        image_path: Path | str,
        *,
        expected_content_sha256: str = "",
        expected_entry_id: str = "",
    ) -> dict[str, Any]:
        """在图包任务锁内保存人工确认，避免与开始语义任务并发覆盖。"""
        pack_id = self._validate_pack_id(pack_id)
        async with self._lock(pack_id):
            self.assert_pack_mutation_allowed(pack_id, "确认图片分类")
            return confirm_image_category(
                self._pack_dir(pack_id),
                image_path,
                expected_content_sha256=expected_content_sha256,
                expected_entry_id=expected_entry_id,
            )

    async def save_image_manual_semantic(
        self,
        pack_id: str,
        image_path: Path | str,
        *,
        caption: str,
        tags: Any,
        visible_text: str = "",
        category_decision: str = "keep",
        expected_content_sha256: str = "",
        expected_entry_id: str = "",
        update_vector: bool = False,
        target_category: str = "",
    ) -> dict[str, Any]:
        """保存单图人工语义，并可在同一图包锁内只更新该图向量。"""
        pack_id = self._validate_pack_id(pack_id)
        pack_dir = self._pack_dir(pack_id)
        normalized_target = str(target_category or "").strip()
        if normalized_target and not update_vector:
            raise ValueError("移动分类必须使用“保存并更新该图向量”")
        current_task = asyncio.current_task()
        async with self._lock(pack_id):
            operation = (
                "保存图片人工语义并移动分类"
                if normalized_target
                else "保存图片人工语义"
            )
            self.assert_pack_mutation_allowed(pack_id, operation)
            move_result: dict[str, Any] = {}
            effective_image_path = Path(image_path)
            if normalized_target:
                move_result = save_manual_image_semantic_and_move(
                    pack_dir,
                    image_path,
                    normalized_target,
                    caption=caption,
                    tags=tags,
                    visible_text=visible_text,
                    expected_content_sha256=expected_content_sha256,
                    expected_entry_id=expected_entry_id,
                )
                detail = move_result["semantic"]
                effective_image_path = Path(move_result["image_path"])
            else:
                detail = save_manual_image_semantic(
                    pack_dir,
                    image_path,
                    caption=caption,
                    tags=tags,
                    visible_text=visible_text,
                    category_decision=category_decision,
                    expected_content_sha256=expected_content_sha256,
                    expected_entry_id=expected_entry_id,
                )
            result = {
                "semantic": detail,
                "semantic_saved": True,
                "moved": bool(move_result),
                "source_category": str(move_result.get("source_category") or ""),
                "target_category": str(move_result.get("target_category") or ""),
                "category": str(detail.get("category") or ""),
                "filename": Path(str(detail.get("relative_path") or "")).name,
                "vector_update": {
                    "status": "pending",
                    "provider_available": False,
                    "requested_images": 0,
                    "reused_images": 0,
                },
            }
            if not update_vector:
                return result

            embedding = self._embedding_adapter(pack_id)
            if not embedding.ready:
                result["vector_update"].update(
                    {
                        "status": "waiting_provider",
                        "message": "语义已保存，当前没有可用向量模型，向量等待更新",
                    }
                )
                return result
            result["vector_update"]["provider_available"] = True
            if current_task is None:
                result["vector_update"].update(
                    {
                        "status": "pending",
                        "message": "语义已保存，但当前请求无法登记向量任务，请稍后重试",
                    }
                )
                return result

            self._index_tasks[pack_id] = current_task
            try:
                try:
                    await self._verify_embedding_dimension(pack_id, embedding)
                except Exception as exc:
                    failed_detail = set_image_embedding_failure(
                        pack_dir,
                        effective_image_path,
                        self._safe_error(exc, pack_id),
                        expected_content_sha256=detail["content_sha256"],
                        expected_entry_id=detail["entry_id"],
                    )
                    result["semantic"] = failed_detail
                    result["vector_update"].update(
                        {
                            "status": "failed",
                            "message": "语义已保存，但向量模型校验失败",
                        }
                    )
                    return result

                try:
                    manifest = await build_index(
                        pack_dir,
                        self.plugin_data_dir,
                        pack_id,
                        embedding,
                        force=False,
                        target_entry_ids={detail["entry_id"]},
                    )
                except Exception as exc:
                    latest_detail = get_image_semantic_detail(
                        pack_dir,
                        effective_image_path,
                    )
                    result["semantic"] = latest_detail
                    failed = latest_detail.get("embedding_status") == "failed"
                    result["vector_update"].update(
                        {
                            "status": "failed" if failed else "pending",
                            "message": (
                                "语义已保存，但当前图片向量更新失败"
                                if failed
                                else f"语义已保存，向量等待更新：{self._safe_error(exc, pack_id)}"
                            ),
                        }
                    )
                    return result

                latest_detail = get_image_semantic_detail(
                    pack_dir,
                    effective_image_path,
                )
                result["semantic"] = latest_detail
                result["vector_update"].update(
                    {
                        "status": "done",
                        "requested_images": int(
                            manifest.get("requested_embedding_count", 0) or 0
                        ),
                        "reused_images": int(
                            manifest.get("reused_vector_count", 0) or 0
                        ),
                        "item_count": int(manifest.get("item_count", 0) or 0),
                        "message": "人工语义和当前图片向量已更新",
                    }
                )
                return result
            finally:
                if self._index_tasks.get(pack_id) is current_task:
                    self._index_tasks.pop(pack_id, None)

    async def restore_image_auto_semantic(
        self,
        pack_id: str,
        image_path: Path | str,
        *,
        expected_content_sha256: str = "",
        expected_entry_id: str = "",
    ) -> dict[str, Any]:
        """在图包锁内显式放弃当前路径的人工修改。"""
        pack_id = self._validate_pack_id(pack_id)
        async with self._lock(pack_id):
            self.assert_pack_mutation_allowed(pack_id, "恢复图片自动语义")
            return restore_image_auto_semantic(
                self._pack_dir(pack_id),
                image_path,
                expected_content_sha256=expected_content_sha256,
                expected_entry_id=expected_entry_id,
            )

    async def propose_image_semantic_revision(
        self,
        pack_id: str,
        image_path: Path | str,
        *,
        review_instruction: str,
        expected_content_sha256: str = "",
        expected_entry_id: str = "",
    ) -> dict[str, Any]:
        """按人工复审意见调用视觉模型，只返回候选内容而不写入语义。"""
        instruction = str(review_instruction or "").strip()
        if not instruction:
            raise ValueError("请先填写人工复审意见")
        if len(instruction) > 2000:
            raise ValueError("人工复审意见不能超过 2000 个字符")

        pack_id = self._validate_pack_id(pack_id)
        pack_dir = self._pack_dir(pack_id)
        async with self._lock(pack_id):
            self.assert_pack_mutation_allowed(pack_id, "调用视觉模型重写图片语义")
            vision = self._vision_provider_details()
            if not vision["ready"]:
                raise RuntimeError("当前没有可用的视觉模型，请先在插件配置中选择")
            snapshot = validate_image_edit_snapshot(
                pack_dir,
                image_path,
                expected_content_sha256=expected_content_sha256,
                expected_entry_id=expected_entry_id,
            )
            item = snapshot["item"]
            category_descriptions = load_category_descriptions(pack_dir)
            memes_root = pack_dir / "memes"
            category_paths = (
                [
                    category_path
                    for category_path in memes_root.iterdir()
                    if category_path.is_dir() and not category_path.is_symlink()
                ]
                if memes_root.is_dir()
                else []
            )
            for category_path in category_paths:
                category_descriptions.setdefault(category_path.name, "")
            selectable_categories = {
                category_path.name
                for category_path in category_paths
                if category_path.name and category_path.name != REVIEW_CATEGORY
            }
            available_categories = {
                category: description
                for category, description in category_descriptions.items()
                if category in selectable_categories
            }
            original_category = _revision_original_category(
                item,
                selectable_categories,
            )
            try:
                proposal = await generate_caption(
                    self.context,
                    snapshot["source"],
                    str(vision["id"]),
                    category=item.category,
                    category_description=item.category_description,
                    available_categories=available_categories,
                    review_instruction=instruction,
                    current_semantic={
                        "caption": item.caption,
                        "tags": [tag for tag in item.tags if not is_category_tag(tag)],
                        "visible_text": item.visible_text,
                        "current_category": item.category,
                        "original_category": original_category,
                        "reclassification_status": item.reclassification_status,
                        "reclassification_reason": item.reclassification_reason,
                    },
                )
            except Exception as exc:
                self._record_vision_usage(
                    pack_id,
                    getattr(exc, "token_usage", None),
                )
                raise
            self._record_vision_usage(pack_id, proposal.get("token_usage"))
            validate_image_edit_snapshot(
                pack_dir,
                image_path,
                expected_content_sha256=snapshot["content_sha256"],
                expected_entry_id=snapshot["entry_id"],
            )
            category_fit = str(proposal.get("category_fit") or "uncertain")
            suggested_category = str(proposal.get("suggested_category") or "").strip()
            selected_category, classification_action = _revision_category_choice(
                current_category=item.category,
                original_category=original_category,
                category_fit=category_fit,
                suggested_category=suggested_category,
                selectable_categories=selectable_categories,
            )
            return {
                "caption": str(proposal.get("caption") or "").strip(),
                "tags": [
                    tag for tag in proposal.get("tags", []) if not is_category_tag(tag)
                ],
                "visible_text": str(proposal.get("visible_text") or "").strip(),
                "category_fit": category_fit,
                "category_review_reason": str(
                    proposal.get("category_review_reason") or ""
                ).strip(),
                "suggested_category": suggested_category,
                "current_category": item.category,
                "original_category": original_category,
                "selected_category": selected_category,
                "classification_action": classification_action,
                "vision_model": str(proposal.get("vision_model") or vision["id"]),
                "vision_requests": int(
                    (proposal.get("token_usage") or {}).get("calls", 1) or 1
                ),
            }

    async def run_locked_pack_mutation(
        self, pack_id: str, operation: str, mutation: Any
    ) -> Any:
        """在语义任务锁内执行一个不含 await 的图包变更。"""
        pack_id = self._validate_pack_id(pack_id)
        if not callable(mutation):
            raise TypeError("mutation 必须可调用")
        async with self._lock(pack_id):
            self.assert_pack_mutation_allowed(pack_id, operation)
            return await self._run_blocking(mutation)

    @staticmethod
    async def _run_blocking(function: Any, *args: Any, **kwargs: Any) -> Any:
        """运行同步工作，并在协程取消前等待工作线程安全收尾。

        Args:
            function: 要在工作线程中执行的同步可调用对象。
            *args: 传递给可调用对象的位置参数。
            **kwargs: 传递给可调用对象的关键字参数。

        Returns:
            同步可调用对象的返回值。

        Raises:
            asyncio.CancelledError: 调用协程被取消且工作线程已经收尾。
        """
        worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(worker)
            except Exception:
                pass
            raise

    async def _cancel_index_task(self, pack_id: str) -> None:
        index_task = self._index_tasks.get(pack_id)
        if not self._task_is_alive(index_task) or index_task is asyncio.current_task():
            return
        index_task.cancel()
        await asyncio.gather(index_task, return_exceptions=True)

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

    def _selection_path(self, pack_id: str) -> Path:
        return (
            self.plugin_data_dir
            / "semantic_indexes"
            / str(pack_id)
            / "provider_selection.json"
        )

    def _load_selection(self, pack_id: str) -> dict[str, Any]:
        path = self._selection_path(pack_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_json_atomic(
        self, path: Path, payload: dict[str, Any], prefix: str
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=prefix, suffix=".json", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file_obj:
                json.dump(payload, file_obj, ensure_ascii=False, indent=2)
                file_obj.flush()
                os.fsync(file_obj.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def _load_state(self, pack_id: str) -> dict[str, Any]:
        path = self._state_path(pack_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self, pack_id: str, state: dict[str, Any]) -> None:
        path = self._state_path(pack_id)
        self._save_json_atomic(path, state, ".task_state.")

    def _pack_dir(self, pack_id: str) -> Path:
        return self.plugin_data_dir / "packs" / str(pack_id)

    def _safe_error(self, error: Any, pack_id: str = "") -> str:
        message = str(error or "未知错误")
        for secret_path in (str(self.plugin_data_dir), str(self._pack_dir(pack_id))):
            if secret_path:
                message = message.replace(secret_path, "<本地资源>")
        return message[:500]

    def _configured_concurrency(self) -> int:
        try:
            value = int(self.config.get("concurrency", 1) or 1)
        except (TypeError, ValueError):
            value = 1
        return max(1, min(16, value))

    @staticmethod
    def _elapsed_seconds(started_at: Any, finished_at: Any = None) -> int:
        try:
            start = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
            end = (
                datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
                if finished_at
                else datetime.now(timezone.utc)
            )
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            return max(0, int((end - start).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return 0

    @staticmethod
    def _normalize_token_usage(value: Any) -> dict[str, int]:
        if not isinstance(value, dict):
            value = {}
        result = {}
        for key in ("input", "output", "total", "calls"):
            try:
                result[key] = max(0, int(value.get(key, 0) or 0))
            except (TypeError, ValueError):
                result[key] = 0
        if result["total"] <= 0:
            result["total"] = result["input"] + result["output"]
        return result

    def _record_vision_usage(self, pack_id: str, usage: Any) -> None:
        """把本次视觉调用的 token 累加到任务状态，供页面实时展示。"""
        state = self._load_state(pack_id)
        total = self._normalize_token_usage(state.get("token_usage"))
        current = self._normalize_token_usage(usage)
        for key in ("input", "output", "total"):
            total[key] += current[key]
        total["calls"] += max(1, current["calls"])
        state["token_usage"] = total
        state["vision_calls"] = total["calls"]
        state["updated_at"] = utc_now()
        self._save_state(pack_id, state)

    def _vision_provider_details(self) -> dict[str, str | bool]:
        provider_id = str(
            self.config.get("vision_provider_id")
            or self.config.get("visual_provider_id")
            or ""
        ).strip()
        details: dict[str, str | bool] = {
            "id": provider_id,
            "model": "",
            "ready": False,
        }
        if not provider_id or self.context is None:
            return details
        resolver = getattr(self.context, "get_provider_by_id", None)
        if not callable(resolver) or not callable(
            getattr(self.context, "llm_generate", None)
        ):
            return details
        try:
            provider = resolver(provider_id)
        except Exception:
            provider = None
        if provider is None:
            return details
        provider_config = getattr(provider, "provider_config", {})
        modalities = (
            provider_config.get("modalities")
            if isinstance(provider_config, dict)
            else None
        )
        if isinstance(modalities, list) and modalities:
            if "image" not in {
                str(value or "").strip().lower() for value in modalities
            }:
                return details
        model = ""
        meta = getattr(provider, "meta", None)
        if callable(meta):
            try:
                model = str(getattr(meta(), "model", "") or "")
            except Exception:
                model = ""
        if not model:
            for key in ("model", "model_name", "chat_model"):
                if isinstance(provider_config, dict) and provider_config.get(key):
                    model = str(provider_config[key])
                    break
        details.update({"model": model, "ready": True})
        return details

    def _vision_provider_ready(self) -> bool:
        return bool(self._vision_provider_details()["ready"])

    def capabilities(
        self, pack_id: str, *, embedding_provider: Any = None
    ) -> dict[str, Any]:
        pack_dir = self._pack_dir(pack_id)
        metadata = load_metadata(pack_dir)
        state = self._load_state(pack_id)
        provider = (
            embedding_provider
            if embedding_provider is not None
            else self._resolve_embedding_provider(pack_id)
        )
        embedding = EmbeddingAdapter(provider)
        return {
            "vision_provider_ready": self._vision_provider_ready(),
            "embedding_provider_ready": embedding.ready,
            "faiss_ready": faiss_is_available(),
            "semantic_metadata_ready": bool(metadata.get("images")),
            "task_status": str(state.get("task_status") or "idle"),
        }

    def _persist_provider_selection(
        self, pack_id: str, embedding: EmbeddingAdapter, selection_mode: str
    ) -> dict[str, Any]:
        if not pack_id or not embedding.ready:
            return {}
        previous = self._load_selection(pack_id)
        same_signature = (
            str(previous.get("effective_provider_id") or "") == embedding.provider_id
            and str(previous.get("embedding_model") or "") == embedding.model_name
            and int(previous.get("configured_dimension", 0) or 0) == embedding.dimension
        )
        payload = {
            "schema_version": "1.0",
            "selection_mode": selection_mode,
            "configured_provider_id": str(
                self.config.get("embedding_provider_id") or ""
            ).strip(),
            "effective_provider_id": embedding.provider_id,
            "embedding_model": embedding.model_name,
            "configured_dimension": embedding.dimension,
            "dimension_verified": bool(
                previous.get("dimension_verified") if same_signature else False
            ),
            "verified_dimension": int(
                previous.get("verified_dimension", 0) if same_signature else 0
            ),
            "verification_error": str(
                previous.get("verification_error") or "" if same_signature else ""
            ),
            "verified_at": str(
                previous.get("verified_at") or "" if same_signature else ""
            ),
            "updated_at": utc_now(),
        }
        self._save_json_atomic(
            self._selection_path(pack_id), payload, ".provider_selection."
        )
        return payload

    def _resolve_embedding_provider(self, pack_id: str = "") -> Any:
        if self.config.get("embedding_provider") is not None:
            provider = self.config.get("embedding_provider")
            adapter = EmbeddingAdapter(provider)
            if pack_id and adapter.ready:
                self._persist_provider_selection(pack_id, adapter, "configured")
            return provider
        context = self.context
        if context is None:
            return None
        provider_id = str(self.config.get("embedding_provider_id") or "").strip()
        resolver = getattr(context, "get_provider_by_id", None)
        if provider_id:
            if not callable(resolver):
                return None
            try:
                provider = resolver(provider_id)
            except Exception:
                return None
            adapter = EmbeddingAdapter(provider, provider_id)
            if pack_id and adapter.ready:
                self._persist_provider_selection(pack_id, adapter, "configured")
            return provider

        # 留空时自动选择，但每个资源包会记住实际选择结果，避免查询时悄悄换模型。
        saved_id = str(
            self._load_selection(pack_id).get("effective_provider_id") or ""
        ).strip()
        if saved_id and callable(resolver):
            try:
                saved_provider = resolver(saved_id)
            except Exception:
                saved_provider = None
            saved_adapter = EmbeddingAdapter(saved_provider, saved_id)
            if saved_adapter.ready:
                self._persist_provider_selection(pack_id, saved_adapter, "automatic")
                return saved_provider

        list_providers = getattr(context, "get_all_embedding_providers", None)
        if not callable(list_providers):
            return None
        try:
            providers = list_providers() or []
        except Exception:
            providers = []
        for provider in providers:
            adapter = EmbeddingAdapter(provider)
            if not adapter.ready:
                continue
            if pack_id:
                self._persist_provider_selection(pack_id, adapter, "automatic")
            return provider
        return None

    def _embedding_adapter(self, pack_id: str = "") -> EmbeddingAdapter:
        configured_id = str(self.config.get("embedding_provider_id") or "").strip()
        return EmbeddingAdapter(
            self._resolve_embedding_provider(pack_id), configured_id
        )

    def _require_embedding_provider(
        self, pack_id: str, action: str
    ) -> EmbeddingAdapter:
        embedding = self._embedding_adapter(pack_id)
        if not embedding.ready:
            configured_id = str(self.config.get("embedding_provider_id") or "").strip()
            if configured_id:
                detail = "配置的 Embedding Provider 不可用"
            else:
                detail = "没有可自动选择的已启用 Embedding Provider"
            raise RuntimeError(
                f"{detail}，无法{action}。请先在 AstrBot 的模型提供商中"
                "启用一个 Embedding 类型模型；也可以在插件配置中明确选择。"
            )
        return embedding

    async def _verify_embedding_dimension(
        self,
        pack_id: str,
        embedding: EmbeddingAdapter,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        selection = self._persist_provider_selection(
            pack_id,
            embedding,
            "configured"
            if str(self.config.get("embedding_provider_id") or "").strip()
            else "automatic",
        )
        if (
            not force
            and selection.get("dimension_verified") is True
            and int(selection.get("verified_dimension", 0) or 0) == embedding.dimension
        ):
            return selection
        try:
            vector = await embedding.embed(
                "AstrBot 表情包向量维度校验", use_cache=False
            )
            selection.update(
                {
                    "dimension_verified": True,
                    "verified_dimension": len(vector),
                    "verification_error": "",
                    "verified_at": utc_now(),
                    "updated_at": utc_now(),
                }
            )
        except Exception as exc:
            selection.update(
                {
                    "dimension_verified": False,
                    "verified_dimension": 0,
                    "verification_error": self._safe_error(exc, pack_id),
                    "verified_at": utc_now(),
                    "updated_at": utc_now(),
                }
            )
            self._save_json_atomic(
                self._selection_path(pack_id), selection, ".provider_selection."
            )
            raise RuntimeError(
                "Embedding 向量维度校验失败，任务未创建："
                f"{self._safe_error(exc, pack_id)}"
            ) from exc
        self._save_json_atomic(
            self._selection_path(pack_id), selection, ".provider_selection."
        )
        return selection

    def status(self, pack_id: str) -> dict[str, Any]:
        pack_id = self._validate_pack_id(pack_id)
        pack_dir = self._pack_dir(pack_id)
        state = self._load_state(pack_id)
        data = load_metadata(pack_dir)
        metadata_read_only = bool(data.get("metadata_read_only"))
        metadata_migration_required = bool(data.get("metadata_migration_required"))
        queue_cleared = bool(state.get("queue_cleared"))
        if (
            not data.get("images")
            and pack_dir.is_dir()
            and not queue_cleared
            and not metadata_read_only
        ):
            data = reconcile_metadata(pack_dir)
        task_status = str(state.get("task_status") or "idle")
        worker_alive = self._semantic_operation_is_alive(pack_id)
        if (
            not worker_alive
            and task_status in {"running", "paused"}
            and not metadata_read_only
            and not metadata_migration_required
        ):
            # 进程重启或硬暂停后，旧请求已不可能再返回。把磁盘上
            # 的 running 记录恢复成 pending，避免记录列表永久显示“进行中”。
            data, recovered = self._reset_running_items(pack_id, data)
            if recovered or state.get("active_items") or state.get("current"):
                state["active_items"] = []
                state["current"] = ""
                state["updated_at"] = utc_now()
                self._save_state(pack_id, state)
        images = data.get("images", {})
        caption_done = sum(
            1 for item in images.values() if semantic_caption_is_complete(item)
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
        pending = sum(1 for item in images.values() if self._item_is_queued(item))
        waiting = sum(
            1
            for item in images.values()
            if isinstance(item, dict)
            and (
                not semantic_caption_is_complete(item)
                or item.get("embedding_status") == "pending"
            )
        )
        embedding = self._embedding_adapter(pack_id)
        selection = self._load_selection(pack_id)
        manifest = load_index_manifest(self.plugin_data_dir, pack_id)
        vision = self._vision_provider_details()
        total_items = len(images)
        running = sum(
            1
            for item in images.values()
            if isinstance(item, dict)
            and (
                item.get("caption_status") == "running"
                or item.get("embedding_status") == "running"
            )
        )
        completed = sum(
            1
            for item in images.values()
            if isinstance(item, dict)
            and semantic_caption_is_complete(item)
            and item.get("embedding_status") == "done"
        )
        failed = sum(
            1
            for item in images.values()
            if isinstance(item, dict)
            and (
                item.get("caption_status") == "failed"
                or item.get("embedding_status") == "failed"
            )
        )
        token_usage = self._normalize_token_usage(state.get("token_usage"))
        active_items = state.get("active_items")
        if not isinstance(active_items, list):
            active_items = []
        active_items = [
            str(value) for value in active_items if str(value or "").strip()
        ]
        task_phase = str(state.get("task_phase") or "")
        external_operation = self._external_pack_operations.get(pack_id, "")
        other_active_tasks = self.active_pack_tasks(exclude_pack_id=pack_id)
        other_task_parts = []
        for item in other_active_tasks:
            if item.get("task_phase") == "indexing":
                detail = "正在建立索引"
            else:
                detail = f"{item.get('concurrency', 1)} 并发"
            other_task_parts.append(f"{item.get('pack_id')}（{detail}）")
        other_tasks_warning = (
            "其他资源包也在运行语义任务："
            + "、".join(other_task_parts)
            + "。各资源包并发会叠加，请自行评估模型限流和调用消耗。"
            if other_task_parts
            else ""
        )
        queued_caption_tasks = sum(
            1
            for item in images.values()
            if isinstance(item, dict)
            and (
                not semantic_caption_is_complete(item)
                or (not worker_alive and item.get("caption_status") == "running")
            )
        )
        queued_embedding_tasks = sum(
            1
            for item in images.values()
            if isinstance(item, dict)
            and semantic_caption_is_complete(item)
            and (
                item.get("embedding_status") == "pending"
                or (not worker_alive and item.get("embedding_status") == "running")
            )
        )
        try:
            task_concurrency = max(
                1,
                min(
                    16,
                    int(state.get("concurrency") or self._configured_concurrency()),
                ),
            )
        except (TypeError, ValueError):
            task_concurrency = self._configured_concurrency()
        ready = bool(
            embedding.ready
            and index_is_ready(
                self.plugin_data_dir,
                pack_id,
                data,
                embedding.provider_id,
                embedding.model_name,
                embedding.dimension,
            )
        )
        caption_complete = bool(
            not queue_cleared
            and images
            and all(semantic_caption_is_complete(item) for item in images.values())
        )
        # active_items 是进程内请求快照；插件重载后旧值只能视为待恢复记录，
        # 不能再显示成“仍有模型请求执行中”。
        if not worker_alive:
            active_items = []
        active_request_count = len(active_items)
        if metadata_read_only:
            queue_status = "metadata_error"
            status_message = str(
                data.get("metadata_error") or "语义元数据无法安全读取，原文件已保持不变"
            )
        elif metadata_migration_required:
            queue_status = "migration_required"
            status_message = (
                "检测到旧版语义数据，描述和人工内容已在内存中保留；"
                "开始明确任务后才会备份并原子升级文件。"
            )
        elif external_operation:
            queue_status = "external_operation"
            status_message = (
                f"资源包正在执行“{external_operation}”，完成前不能启动语义任务。"
            )
        elif queue_cleared:
            queue_status = "cleared"
            status_message = "当前任务队列已清空；已有图片描述仍然保留。"
        elif task_status == "paused":
            queue_status = "settling" if active_request_count else "paused"
            if active_request_count:
                status_message = (
                    f"已暂停领取新图片；{active_request_count} 个已经提交给视觉模型的请求"
                    "仍在收尾，结束后不会继续补位。"
                )
            else:
                status_message = (
                    f"队列已完全暂停，仍有 {queued_caption_tasks} 张图片等待生成描述；"
                    "点击“继续队列”即可恢复。"
                )
        elif task_status == "running" and not worker_alive:
            queue_status = "interrupted"
            status_message = "后台任务已经中断，但进度已保存；请点击“继续队列”。"
        elif task_status == "running" and task_phase == "indexing":
            queue_status = "indexing"
            status_message = (
                "图片描述队列已处理完，正在建立向量索引；此阶段请等待完成。"
            )
        elif task_status == "running":
            queue_status = "running"
            status_message = (
                f"正在生成图片描述：{active_request_count} 个模型请求执行中，"
                f"{queued_caption_tasks} 张排队等待。"
            )
        elif failed:
            queue_status = "failed"
            status_message = f"当前有 {failed} 张图片处理失败，可点击“重试失败项”。"
        elif queued_caption_tasks or queued_embedding_tasks:
            queue_status = "waiting"
            status_message = "存在尚未处理的图片或向量，请开始任务或继续已有队列。"
        elif total_items:
            queue_status = "done"
            status_message = "当前没有排队或执行中的图片任务。"
        else:
            queue_status = "empty"
            status_message = "当前资源包没有可处理的图片。"
        can_pause = bool(
            task_status == "running" and worker_alive and task_phase != "indexing"
        )
        can_resume = bool(
            task_status == "paused"
            or task_status in {"failed", "completed_with_errors"}
            or (task_status == "running" and not worker_alive)
        )
        can_start = bool(
            not metadata_read_only
            and not worker_alive
            and not external_operation
            and task_status != "paused"
        )
        can_retry = bool(
            not worker_alive
            and not metadata_read_only
            and not external_operation
            and task_status != "paused"
            and failed
        )
        can_rebuild_index = bool(
            not worker_alive
            and not metadata_read_only
            and not external_operation
            and task_status != "paused"
            and caption_complete
        )
        if task_status == "paused" and state.get("paused_at"):
            elapsed_until = state.get("paused_at")
        elif task_status not in {"running", "paused"}:
            elapsed_until = state.get("finished_at")
        else:
            elapsed_until = None
        try:
            paused_seconds = max(0, int(state.get("paused_seconds", 0) or 0))
        except (TypeError, ValueError):
            paused_seconds = 0
        elapsed_seconds = max(
            0,
            self._elapsed_seconds(state.get("started_at"), elapsed_until)
            - paused_seconds,
        )
        return {
            "pack_id": pack_id,
            "task_status": task_status,
            "task_mode": str(state.get("mode") or "full"),
            "started_at": str(state.get("started_at") or ""),
            "finished_at": str(state.get("finished_at") or ""),
            "elapsed_seconds": elapsed_seconds,
            "concurrency": task_concurrency,
            "task_phase": task_phase,
            "worker_alive": worker_alive,
            "external_operation": external_operation,
            "other_active_tasks": other_active_tasks,
            "other_tasks_warning": other_tasks_warning,
            "queue_status": queue_status,
            "status_message": status_message,
            "metadata_read_only": metadata_read_only,
            "metadata_error": str(data.get("metadata_error") or ""),
            "metadata_migration_required": metadata_migration_required,
            "migrated_from_schema_version": str(
                data.get("migrated_from_schema_version") or ""
            ),
            "can_pause": can_pause,
            "can_resume": can_resume,
            "can_start": can_start,
            "can_retry": can_retry,
            "can_rebuild_index": can_rebuild_index,
            "file_total": int(data.get("file_total", len(images))),
            "unique_total": int(data.get("unique_total", len(images))),
            "reused_duplicate_files": int(data.get("reused_duplicate_files", 0)),
            "caption_done": caption_done,
            "caption_failed": caption_failed,
            "embedding_done": embedding_done,
            "embedding_failed": embedding_failed,
            "pending": pending,
            "waiting_tasks": waiting,
            "total_tasks": total_items,
            "completed_tasks": completed,
            "running_tasks": running,
            "failed_tasks": failed,
            "queued_caption_tasks": queued_caption_tasks,
            "queued_embedding_tasks": queued_embedding_tasks,
            "active_request_count": active_request_count,
            "vision_calls": token_usage["calls"],
            "reclassified_items": int(state.get("reclassified_items", 0) or 0),
            "token_usage_input": token_usage["input"],
            "token_usage_output": token_usage["output"],
            "token_usage_total": token_usage["total"],
            "current": state.get("current", ""),
            "active_items": active_items,
            "vision_provider_id": str(vision["id"]),
            "vision_model": str(vision["model"]),
            "last_error": state.get("last_error"),
            "embedding_provider_id": embedding.provider_id,
            "embedding_configured_provider_id": str(
                self.config.get("embedding_provider_id") or ""
            ).strip(),
            "embedding_selection_mode": str(selection.get("selection_mode") or ""),
            "embedding_model": embedding.model_name,
            "embedding_configured_dimension": embedding.dimension,
            "embedding_verified_dimension": int(
                selection.get("verified_dimension", 0) or 0
            ),
            "embedding_dimension_verified": bool(selection.get("dimension_verified")),
            "embedding_dimension_error": str(selection.get("verification_error") or ""),
            "index_embedding_provider_id": str(
                manifest.get("embedding_provider_id") or ""
            ),
            "index_embedding_dimension": int(
                manifest.get("embedding_dimension", 0) or 0
            ),
            "index_ready": ready,
            "dimension_rebuild_required": bool(
                caption_complete and embedding.ready and not ready
            ),
            "semantic_caption_complete": caption_complete,
            "queue_cleared": queue_cleared,
            "error_items": [
                {"relative_path": item.get("relative_path"), "error": item.get("error")}
                for item in images.values()
                if isinstance(item, dict) and item.get("error")
            ][-20:],
            **self.capabilities(pack_id, embedding_provider=embedding.provider),
        }

    @staticmethod
    def _item_is_queued(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        return not semantic_caption_is_complete(item) or item.get(
            "embedding_status"
        ) in {"pending", "running", "failed"}

    def _reset_running_items(
        self, pack_id: str, metadata: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], int]:
        """将已不可能返回的进行中记录退回等待队列。"""
        pack_dir = self._pack_dir(pack_id)
        current = metadata if isinstance(metadata, dict) else load_metadata(pack_dir)
        recovered = 0
        for item in current.get("images", {}).values():
            if not isinstance(item, dict):
                continue
            changed = False
            if item.get("caption_status") == "running":
                item["caption_status"] = "pending"
                changed = True
            if item.get("embedding_status") == "running":
                item["embedding_status"] = "pending"
                changed = True
            if changed:
                item["updated_at"] = utc_now()
                recovered += 1
        if recovered:
            save_metadata(pack_dir, current)
        return current, recovered

    async def start(
        self,
        pack_id: str,
        *,
        mode: str = "full",
        force: bool = False,
        concurrency: int | None = None,
        external_data: dict | None = None,
    ) -> dict[str, Any]:
        pack_id = self._validate_pack_id(pack_id)
        mode = str(mode or "full").strip().lower()
        if mode not in {"full", "caption_only", "retry_failed"}:
            raise ValueError("mode 只能是 full、caption_only 或 retry_failed")
        if concurrency is None:
            effective_concurrency = self._configured_concurrency()
        else:
            try:
                effective_concurrency = max(1, min(16, int(concurrency)))
            except (TypeError, ValueError) as exc:
                raise ValueError("并发数必须是 1 到 16 的整数") from exc
        pack_dir = self._pack_dir(pack_id)
        if not pack_dir.is_dir():
            raise FileNotFoundError(f"表情包 {pack_id} 不存在")
        if self._task_is_alive(self._index_tasks.get(pack_id)):
            raise RuntimeError("当前资源包正在建立向量索引，请等待完成后再开始")
        async with self._lock(pack_id):
            external_operation = self._external_pack_operations.get(pack_id)
            if external_operation:
                raise RuntimeError(
                    f"资源包正在执行“{external_operation}”，请等待完成后再开始语义任务"
                )
            existing = self._tasks.get(pack_id)
            if existing and not existing.done():
                raise RuntimeError("同一个资源包已经有语义化任务在运行")
            persisted_state = self._load_state(pack_id)
            if persisted_state.get("task_status") == "paused":
                raise RuntimeError(
                    "当前语义队列处于暂停状态，请使用“继续队列”；"
                    "如需重新扫描，请先清空当前任务队列"
                )
            if persisted_state.get("task_status") == "running":
                raise RuntimeError(
                    "检测到上次运行被中断，请使用“继续队列”恢复；"
                    "如需重新扫描，请先清空当前任务队列"
                )
            embedding = None
            selection = {}
            if mode in {"full", "retry_failed"}:
                embedding = self._require_embedding_provider(pack_id, "开始完整语义化")
                selection = await self._verify_embedding_dimension(pack_id, embedding)
            metadata = reconcile_metadata(pack_dir, external_data=external_data)
            if force:
                for item in metadata.get("images", {}).values():
                    if item.get("manual_override") or item.get("provenance") in {
                        "manual",
                        "mixed",
                    }:
                        continue
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
            elif mode == "caption_only":
                for item in metadata.get("images", {}).values():
                    if item.get("caption_status") == "failed":
                        item["caption_status"] = "pending"
                    item["error"] = None
            # 普通“一键语义化”只处理没有描述或之前失败的图片。提示词升级、
            # 视觉模型切换都不能静默覆盖已有描述；需要重做时只能由操作者明确
            # 点击“强制重新生成”（force=True）。
            needs_caption = any(
                isinstance(item, dict)
                and not (
                    item.get("manual_override")
                    or item.get("provenance") in {"manual", "mixed"}
                )
                and (force or not semantic_caption_is_complete(item))
                for item in metadata.get("images", {}).values()
            )
            if needs_caption and not self._vision_provider_ready():
                raise RuntimeError("未配置视觉模型，无法生成图片描述")
            save_metadata(pack_dir, metadata)
            state = {
                "task_status": "running",
                "task_phase": "captioning" if needs_caption else "indexing",
                "queue_cleared": False,
                "mode": mode,
                "concurrency": effective_concurrency,
                "current": "",
                "active_items": [],
                "started_at": utc_now(),
                "finished_at": "",
                "paused_at": "",
                "paused_seconds": 0,
                "last_error": None,
                "embedding_provider_id": str(
                    selection.get("effective_provider_id") or ""
                ),
                "embedding_selection_mode": str(selection.get("selection_mode") or ""),
                "embedding_dimension": int(selection.get("verified_dimension", 0) or 0),
                "token_usage": {"input": 0, "output": 0, "total": 0, "calls": 0},
                "vision_calls": 0,
                "reclassified_items": 0,
            }
            self._save_state(pack_id, state)
            pause_event = self._pause_events.setdefault(pack_id, asyncio.Event())
            pause_event.set()
            task = asyncio.create_task(self._run(pack_id, mode=mode, force=force))
            self._tasks[pack_id] = task
        result = self.status(pack_id)
        queue_summary = (
            f"待生成描述 {result.get('queued_caption_tasks', 0)} 张，"
            f"待建立向量 {result.get('queued_embedding_tasks', 0)} 张"
        )
        if mode == "retry_failed":
            result["message"] = (
                f"失败项已重新入队：并发上限 {effective_concurrency}，{queue_summary}。"
            )
        else:
            result["message"] = (
                f"语义化队列已启动：并发上限 {effective_concurrency}，{queue_summary}。"
            )
        if result.get("other_tasks_warning"):
            result["message"] += "\n" + str(result["other_tasks_warning"])
        return result

    async def pause(self, pack_id: str) -> dict[str, Any]:
        pack_id = self._validate_pack_id(pack_id)
        if self._task_is_alive(self._index_tasks.get(pack_id)):
            raise RuntimeError("正在建立向量索引；这个收尾阶段不能暂停，请等待完成")
        async with self._lock(pack_id):
            state = self._load_state(pack_id)
            active_task = self._tasks.get(pack_id)
            task_is_running = self._task_is_alive(active_task)
            if state.get("task_status") == "paused" and not task_is_running:
                self._reset_running_items(pack_id)
                state = self._load_state(pack_id)
                if state.get("active_items") or state.get("current"):
                    state["active_items"] = []
                    state["current"] = ""
                    state["updated_at"] = utc_now()
                    self._save_state(pack_id, state)
                result = self.status(pack_id)
                result["message"] = str(result.get("status_message") or "任务已经暂停")
                return result
            if state.get("task_status") not in {"running", "paused"}:
                raise RuntimeError("当前没有正在运行的语义化任务")
            if state.get("task_phase") == "indexing":
                raise RuntimeError(
                    "图片描述队列已经处理完，正在建立向量索引；这个收尾阶段不能暂停，请等待完成"
                )
            # 先关掉领取闸门并持久化暂停，再取消整个描述任务。
            # 不再等待已发出的模型请求“自然收尾”，否则上游卡住时
            # 页面会永久停在暂停中。
            self._pause_events.setdefault(pack_id, asyncio.Event()).clear()
            state["task_status"] = "paused"
            state["paused_at"] = str(state.get("paused_at") or utc_now())
            state["updated_at"] = utc_now()
            self._save_state(pack_id, state)
            interrupted_requests = len(state.get("active_items") or [])
            if task_is_running and active_task is not None:
                active_task.cancel()
                await asyncio.gather(active_task, return_exceptions=True)
                if self._tasks.get(pack_id) is active_task:
                    self._tasks.pop(pack_id, None)
            _, recovered = self._reset_running_items(pack_id)
            state = self._load_state(pack_id)
            state.update(
                {
                    "task_status": "paused",
                    "task_phase": "captioning",
                    "active_items": [],
                    "current": "",
                    "interrupted_requests": max(interrupted_requests, recovered),
                    "updated_at": utc_now(),
                }
            )
            self._save_state(pack_id, state)
        result = self.status(pack_id)
        interrupted = int(state.get("interrupted_requests", 0) or 0)
        result["message"] = (
            f"队列已完全暂停；已中断 {interrupted} 个模型请求并退回等待队列。"
            if interrupted
            else str(result.get("status_message") or "任务已暂停")
        )
        return result

    async def resume(
        self, pack_id: str, *, concurrency: int | None = None
    ) -> dict[str, Any]:
        pack_id = self._validate_pack_id(pack_id)
        async with self._lock(pack_id):
            state = self._load_state(pack_id)
            task = self._tasks.get(pack_id)
            task_is_running = bool(task and not task.done())
            mode = str(state.get("mode") or "full")
            if state.get("task_status") in {
                "running",
                "paused",
                "failed",
                "completed_with_errors",
            }:
                if state.get("queue_cleared"):
                    raise RuntimeError(
                        "当前任务队列已经清空，请点击开始语义化重新扫描图片"
                    )
                if concurrency is not None:
                    try:
                        requested_concurrency = max(1, min(16, int(concurrency)))
                    except (TypeError, ValueError) as exc:
                        raise ValueError("并发数必须是 1 到 16 的整数") from exc
                    try:
                        current_concurrency = max(
                            1,
                            min(
                                16,
                                int(
                                    state.get("concurrency")
                                    or self._configured_concurrency()
                                ),
                            ),
                        )
                    except (TypeError, ValueError):
                        current_concurrency = self._configured_concurrency()
                    if task_is_running and requested_concurrency != current_concurrency:
                        raise RuntimeError(
                            f"当前队列实际并发数是 {current_concurrency}，暂停期间不能修改；"
                            "如需调整，请清空当前队列后重新开始"
                        )
                    state["concurrency"] = requested_concurrency
                if mode in {"full", "retry_failed", "index_only"}:
                    embedding = self._require_embedding_provider(
                        pack_id, "继续完整语义化"
                    )
                    selection = await self._verify_embedding_dimension(
                        pack_id, embedding
                    )
                    state.update(
                        {
                            "embedding_provider_id": embedding.provider_id,
                            "embedding_selection_mode": str(
                                selection.get("selection_mode") or ""
                            ),
                            "embedding_dimension": embedding.dimension,
                        }
                    )
                if not task_is_running:
                    metadata, _ = self._reset_running_items(pack_id)
                    state["active_items"] = []
                    state["current"] = ""
                    state["task_phase"] = (
                        "captioning"
                        if any(
                            isinstance(item, dict)
                            and not semantic_caption_is_complete(item)
                            for item in metadata.get("images", {}).values()
                        )
                        else "indexing"
                    )
                if state.get("task_status") == "paused" and state.get("paused_at"):
                    try:
                        previous_paused_seconds = max(
                            0, int(state.get("paused_seconds", 0) or 0)
                        )
                    except (TypeError, ValueError):
                        previous_paused_seconds = 0
                    state["paused_seconds"] = (
                        previous_paused_seconds
                        + self._elapsed_seconds(state.get("paused_at"))
                    )
                state["task_status"] = "running"
                state["paused_at"] = ""
                state["updated_at"] = utc_now()
                self._save_state(pack_id, state)
                event = self._pause_events.setdefault(pack_id, asyncio.Event())
                event.set()
                if not task_is_running:
                    self._tasks[pack_id] = asyncio.create_task(
                        self._run(pack_id, mode=mode, force=False)
                    )
        result = self.status(pack_id)
        if result.get("task_status") == "running":
            result["message"] = (
                f"队列已继续：并发上限 {result.get('concurrency', 1)}，"
                f"待生成描述 {result.get('queued_caption_tasks', 0)} 张，"
                f"待建立向量 {result.get('queued_embedding_tasks', 0)} 张。"
            )
            if result.get("other_tasks_warning"):
                result["message"] += "\n" + str(result["other_tasks_warning"])
        return result

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
        current_task = asyncio.current_task()
        async with self._lock(pack_id):
            active_task = self._tasks.get(pack_id)
            if active_task and not active_task.done():
                raise RuntimeError("语义化任务尚未结束，请等待任务完成后再重建索引")
            active_index_task = self._index_tasks.get(pack_id)
            if (
                active_index_task
                and active_index_task is not current_task
                and not active_index_task.done()
            ):
                raise RuntimeError("当前资源包已经在建立向量索引")
            external_operation = self._external_pack_operations.get(pack_id)
            if external_operation:
                raise RuntimeError(
                    f"资源包正在执行“{external_operation}”，暂时不能建立向量索引"
                )
            provider = self._require_embedding_provider(pack_id, "建立向量索引")
            metadata = load_metadata(pack_dir)
            images = metadata.get("images", {})
            if not isinstance(images, dict) or not images:
                raise RuntimeError("还没有可建立索引的图片描述")
            incomplete = sum(
                1
                for item in images.values()
                if (
                    not isinstance(item, dict) or not semantic_caption_is_complete(item)
                )
            )
            if incomplete:
                raise RuntimeError(
                    f"还有 {incomplete} 个图片描述未完成，请先完成全部描述后再建立向量索引"
                )
            if current_task is None:
                raise RuntimeError("无法登记当前索引任务")
            self._index_tasks[pack_id] = current_task
            index_state = self._load_state(pack_id)
            index_state.update(
                {
                    "task_status": "running",
                    "task_phase": "indexing",
                    "mode": "index_only",
                    "current": "正在建立向量索引",
                    "active_items": [],
                    "started_at": utc_now(),
                    "finished_at": "",
                    "paused_at": "",
                    "paused_seconds": 0,
                    "last_error": None,
                    "updated_at": utc_now(),
                }
            )
            self._save_state(pack_id, index_state)
            try:
                await self._verify_embedding_dimension(pack_id, provider, force=force)
                result = await build_index(
                    pack_dir, self.plugin_data_dir, pack_id, provider, force=force
                )
                latest = load_metadata(pack_dir)
                has_errors = any(
                    isinstance(item, dict) and item.get("embedding_status") == "failed"
                    for item in latest.get("images", {}).values()
                )
                final_state = self._load_state(pack_id)
                final_state.update(
                    {
                        "task_status": (
                            "completed_with_errors" if has_errors else "completed"
                        ),
                        "task_phase": "finished",
                        "current": "",
                        "active_items": [],
                        "finished_at": utc_now(),
                        "updated_at": utc_now(),
                    }
                )
                self._save_state(pack_id, final_state)
                return result
            except asyncio.CancelledError:
                cancelled_state = self._load_state(pack_id)
                cancelled_state.update(
                    {
                        "task_status": "failed",
                        "task_phase": "failed",
                        "current": "",
                        "active_items": [],
                        "finished_at": utc_now(),
                        "last_error": "向量索引任务已取消",
                        "updated_at": utc_now(),
                    }
                )
                self._save_state(pack_id, cancelled_state)
                raise
            except Exception as exc:
                failed_state = self._load_state(pack_id)
                failed_state.update(
                    {
                        "task_status": "failed",
                        "task_phase": "failed",
                        "current": "",
                        "active_items": [],
                        "finished_at": utc_now(),
                        "last_error": self._safe_error(exc, pack_id),
                        "updated_at": utc_now(),
                    }
                )
                self._save_state(pack_id, failed_state)
                raise
            finally:
                if self._index_tasks.get(pack_id) is current_task:
                    self._index_tasks.pop(pack_id, None)

    async def clear_local_semantic_state(self, pack_id: str) -> dict[str, Any]:
        """清理错误任务和本机向量，但保留可发布的图片描述。"""
        pack_id = self._validate_pack_id(pack_id)
        pack_dir = self._pack_dir(pack_id)
        if not pack_dir.is_dir():
            raise FileNotFoundError(f"表情包 {pack_id} 不存在")
        external_operation = self._external_pack_operations.get(pack_id)
        if external_operation:
            raise RuntimeError(
                f"资源包正在执行“{external_operation}”，暂时不能清空语义任务"
            )
        await self._cancel_index_task(pack_id)
        async with self._lock(pack_id):
            active_task = self._tasks.get(pack_id)
            if active_task and not active_task.done():
                active_task.cancel()
                await asyncio.gather(active_task, return_exceptions=True)
            self._tasks.pop(pack_id, None)
            self._index_tasks.pop(pack_id, None)
            self._pause_events.pop(pack_id, None)

            index_root = self.plugin_data_dir / "semantic_indexes" / pack_id
            for index_file in index_root.glob("index*.faiss"):
                index_file.unlink(missing_ok=True)
            (index_root / "index_manifest.json").unlink(missing_ok=True)

            metadata = load_metadata(pack_dir)
            kept_images = {}
            for digest, raw_item in metadata.get("images", {}).items():
                if not isinstance(raw_item, dict):
                    continue
                # 已完成的图片描述不是“待处理队列”，保留它们；其向量标记为 cleared，
                # 这样清理后不会再次显示为待处理，但以后点击开始仍会重新建立向量。
                if raw_item.get("caption_status") == "done":
                    item = dict(raw_item)
                    item["embedding_status"] = "cleared"
                    item["error"] = None
                    item["updated_at"] = utc_now()
                    kept_images[str(digest)] = item
            metadata["images"] = kept_images
            metadata["requires_local_index_rebuild"] = True
            save_metadata(pack_dir, metadata)
            self._save_state(
                pack_id,
                {
                    "task_status": "idle",
                    "queue_cleared": True,
                    "current": "",
                    "last_error": None,
                    "updated_at": utc_now(),
                },
            )
        return self.status(pack_id)

    async def delete_all_semantic_data(self, pack_id: str) -> dict[str, Any]:
        """删除当前资源包的全部语义结果和本机索引，但保留原始图片。"""
        pack_id = self._validate_pack_id(pack_id)
        pack_dir = self._pack_dir(pack_id)
        if not pack_dir.is_dir():
            raise FileNotFoundError(f"表情包 {pack_id} 不存在")
        external_operation = self._external_pack_operations.get(pack_id)
        if external_operation:
            raise RuntimeError(
                f"资源包正在执行“{external_operation}”，暂时不能删除语义数据"
            )
        await self._cancel_index_task(pack_id)
        async with self._lock(pack_id):
            active_task = self._tasks.get(pack_id)
            if active_task and not active_task.done():
                active_task.cancel()
                await asyncio.gather(active_task, return_exceptions=True)
            self._tasks.pop(pack_id, None)
            self._index_tasks.pop(pack_id, None)
            self._pause_events.pop(pack_id, None)

            shutil.rmtree(
                self.plugin_data_dir / "semantic_indexes" / pack_id,
                ignore_errors=True,
            )
            save_metadata(
                pack_dir,
                {
                    "pack_id": pack_id,
                    "generated_at": utc_now(),
                    "images": {},
                    "file_total": 0,
                    "unique_total": 0,
                    "reused_duplicate_files": 0,
                    "requires_local_index_rebuild": True,
                    "semantic_data_deleted": True,
                },
            )
            self._save_state(
                pack_id,
                {
                    "task_status": "idle",
                    "queue_cleared": True,
                    "semantic_data_deleted": True,
                    "current": "",
                    "active_items": [],
                    "last_error": None,
                    "deleted_at": utc_now(),
                    "updated_at": utc_now(),
                },
            )
        return self.status(pack_id)

    async def _process_caption_item(
        self,
        pack_id: str,
        pack_dir: Path,
        metadata: dict[str, Any],
        digest: str,
        raw_item: dict[str, Any],
        vision_provider: str,
        available_categories: dict[str, str],
        force: bool,
        semaphore: asyncio.Semaphore,
    ) -> None:
        item = SemanticImage.from_dict(raw_item)
        if item.manual_override or item.provenance in {"manual", "mixed"}:
            return
        if not force and semantic_caption_is_complete(item.to_dict()):
            return
        event = self._pause_events.setdefault(pack_id, asyncio.Event())
        # 第一次等待避免暂停状态下无意义地争抢并发名额。
        await event.wait()
        path = safe_relative_path(pack_dir, item.relative_path)
        if path is None or not path.is_file():
            item.caption_status = "failed"
            item.embedding_status = "pending"
            item.error = "图片路径无效或文件不存在"
            item.updated_at = utc_now()
            raw_item.update(item.to_dict())
            save_metadata(pack_dir, metadata)
            return

        active_items: list[str] = []
        try:
            async with semaphore:
                # 关键的第二次检查：图片可能在暂停前已经通过第一次等待，随后
                # 一直堵在并发闸门外。拿到名额后必须重新确认，暂停后才能真正
                # 停止补位，只让已经发出的模型请求自然收尾。
                while True:
                    await event.wait()
                    state = self._load_state(pack_id)
                    if state.get("task_status") != "paused":
                        break
                    event.clear()
                active_items = [
                    str(value)
                    for value in state.get("active_items", [])
                    if str(value or "").strip()
                ]
                if item.relative_path not in active_items:
                    active_items.append(item.relative_path)
                self._save_state(
                    pack_id,
                    {
                        **state,
                        "task_phase": "captioning",
                        "active_items": active_items,
                        "current": active_items[0]
                        if len(active_items) == 1
                        else f"并发处理中（{len(active_items)} 项）",
                        "updated_at": utc_now(),
                    },
                )
                item.caption_status = "running"
                raw_item.update(item.to_dict())
                save_metadata(pack_dir, metadata)
                caption = await generate_caption(
                    self.context,
                    path,
                    vision_provider,
                    category=item.category,
                    category_description=item.category_description,
                    available_categories=available_categories,
                )
            self._record_vision_usage(pack_id, caption.get("token_usage"))
            manual_content = bool(
                item.manual_override or item.provenance in {"manual", "mixed"}
            )
            if not manual_content:
                item.caption = caption["caption"]
                item.tags = ensure_category_tag(caption["tags"], item.category)
                item.visible_text = caption["visible_text"]
                item.auto_caption = item.caption
                item.auto_tags = caption["tags"]
                item.auto_visible_text = item.visible_text
            item.vision_model = caption.get("vision_model", "")
            item.prompt_version = caption.get("prompt_version", item.prompt_version)
            item.category_fit = str(caption.get("category_fit") or "uncertain")
            item.suggested_category = str(
                caption.get("suggested_category") or ""
            ).strip()
            if not (
                item.category_review_status == "manual_confirmed"
                and item.manual_confirmation_context_hash == item.category_context_hash
            ):
                if item.reclassification_status:
                    item.category_review_status = "needs_review"
                    item.category_review_reason = str(
                        item.category_review_reason
                        or item.reclassification_reason
                        or "图片曾被自动重分类，等待人工确认"
                    ).strip()
                else:
                    item.category_review_status = (
                        "auto_match" if item.category_fit == "match" else "needs_review"
                    )
                    item.category_review_reason = str(
                        caption.get("category_review_reason") or ""
                    ).strip()
                item.category_review_context_hash = item.category_context_hash
                item.manual_confirmation_context_hash = ""
            if not manual_content:
                item.auto_category_fit = item.category_fit
                item.auto_category_review_status = item.category_review_status
                item.auto_category_review_reason = item.category_review_reason
            item.caption_status = "done"
            item.embedding_status = "pending"
            item.text_hash = text_hash(item.vector_text)
            item.error = None
        except Exception as exc:
            self._record_vision_usage(pack_id, getattr(exc, "token_usage", None))
            item.error = self._safe_error(exc, pack_id)
            result_preview = str(getattr(exc, "result_preview", "") or "").strip()
            if result_preview:
                item.error = f"{item.error}；模型返回：{result_preview[:1000]}"
            if item.caption_status != "done":
                item.caption_status = "failed"
        finally:
            item.updated_at = utc_now()
            raw_item.update(item.to_dict())
            save_metadata(pack_dir, metadata)
            state = self._load_state(pack_id)
            active_items = [
                str(value)
                for value in state.get("active_items", [])
                if str(value or "").strip() and str(value) != item.relative_path
            ]
            self._save_state(
                pack_id,
                {
                    **state,
                    "active_items": active_items,
                    "current": active_items[0]
                    if len(active_items) == 1
                    else (
                        f"并发处理中（{len(active_items)} 项）" if active_items else ""
                    ),
                    "updated_at": utc_now(),
                },
            )

    async def _run(self, pack_id: str, *, mode: str, force: bool) -> None:
        pack_dir = self._pack_dir(pack_id)
        try:
            metadata = await self._run_blocking(load_metadata, pack_dir)
            vision_provider = str(
                self.config.get("vision_provider_id")
                or self.config.get("visual_provider_id")
                or ""
            )
            embedding = self._embedding_adapter(pack_id)
            descriptions = await self._run_blocking(
                load_category_descriptions, pack_dir
            )
            memes_root = pack_dir / "memes"
            available_categories = await self._run_blocking(
                lambda: (
                    {
                        path.name: str(descriptions.get(path.name) or "")
                        for path in memes_root.iterdir()
                        if path.is_dir() and path.name != REVIEW_CATEGORY
                    }
                    if memes_root.is_dir()
                    else {}
                )
            )
            state = await self._run_blocking(self._load_state, pack_id)
            try:
                concurrency = max(1, min(16, int(state.get("concurrency") or 1)))
            except (TypeError, ValueError):
                concurrency = self._configured_concurrency()
            semaphore = asyncio.Semaphore(concurrency)
            caption_tasks = [
                asyncio.create_task(
                    self._process_caption_item(
                        pack_id,
                        pack_dir,
                        metadata,
                        str(digest),
                        raw_item,
                        vision_provider,
                        available_categories,
                        force,
                        semaphore,
                    ),
                    name=f"meme-semantic:{pack_id}:{str(digest)[:12]}",
                )
                for digest, raw_item in list(metadata.get("images", {}).items())
                if isinstance(raw_item, dict)
                and not (
                    raw_item.get("manual_override")
                    or raw_item.get("provenance") in {"manual", "mixed"}
                )
                and (force or not semantic_caption_is_complete(raw_item))
            ]
            if caption_tasks:
                try:
                    await asyncio.gather(*caption_tasks)
                except BaseException:
                    # gather 遇到第一个异常会立即向上抛出，但不会自动等待其余
                    # 协程结束。必须主动取消并收拢，否则页面已显示失败后，旧
                    # 图片请求仍可能继续运行并覆盖下一轮任务的状态。
                    for caption_task in caption_tasks:
                        if not caption_task.done():
                            caption_task.cancel()
                    await asyncio.gather(*caption_tasks, return_exceptions=True)
                    raise
            pause_event = self._pause_events.setdefault(pack_id, asyncio.Event())
            while (await self._run_blocking(self._load_state, pack_id)).get(
                "task_status"
            ) == "paused":
                await pause_event.wait()
            reclassification_result = await self._run_blocking(
                apply_conflict_reclassifications, pack_dir, metadata
            )
            if reclassification_result.get("moved"):
                state = await self._run_blocking(self._load_state, pack_id)
                state["reclassified_items"] = int(
                    state.get("reclassified_items", 0) or 0
                ) + int(reclassification_result["moved"])
                state["updated_at"] = utc_now()
                await self._run_blocking(self._save_state, pack_id, state)
            has_caption = any(
                isinstance(item, dict) and semantic_caption_is_complete(item)
                for item in metadata.get("images", {}).values()
            )
            all_captions_done = bool(
                metadata.get("images")
                and all(
                    isinstance(item, dict) and semantic_caption_is_complete(item)
                    for item in metadata.get("images", {}).values()
                )
            )
            if mode == "caption_only":
                failed = any(
                    isinstance(item, dict) and not semantic_caption_is_complete(item)
                    for item in metadata.get("images", {}).values()
                )
            elif embedding.ready and has_caption and all_captions_done:
                # build_index 会从旧 FAISS 复用未变化向量，只补充新增或变化的图片。
                index_state = await self._run_blocking(self._load_state, pack_id)
                await self._run_blocking(
                    self._save_state,
                    pack_id,
                    {
                        **index_state,
                        "task_status": "running",
                        "task_phase": "indexing",
                        "current": "正在建立向量索引",
                        "updated_at": utc_now(),
                    },
                )
                await build_index(
                    pack_dir,
                    self.plugin_data_dir,
                    pack_id,
                    embedding,
                    force=force,
                )
            elif has_caption and not embedding.ready:
                for item in metadata.get("images", {}).values():
                    if not isinstance(item, dict) or not semantic_caption_is_complete(
                        item
                    ):
                        continue
                    item["embedding_status"] = "failed"
                    item["error"] = "未配置 AstrBot 核心向量模型"
                await self._run_blocking(save_metadata, pack_dir, metadata)
            latest = await self._run_blocking(load_metadata, pack_dir)
            if mode != "caption_only":
                failed = any(
                    isinstance(item, dict)
                    and (
                        not semantic_caption_is_complete(item)
                        or item.get("embedding_status") != "done"
                    )
                    for item in latest.get("images", {}).values()
                )
            final_state = await self._run_blocking(self._load_state, pack_id)
            final_state.update(
                {
                    "task_status": "completed_with_errors" if failed else "completed",
                    "task_phase": "finished",
                    "mode": mode,
                    "current": "",
                    "active_items": [],
                    "finished_at": utc_now(),
                    "updated_at": utc_now(),
                }
            )
            await self._run_blocking(self._save_state, pack_id, final_state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failed_state = await self._run_blocking(self._load_state, pack_id)
            failed_state.update(
                {
                    "task_status": "failed",
                    "task_phase": "failed",
                    "mode": mode,
                    "current": "",
                    "active_items": [],
                    "finished_at": utc_now(),
                    "last_error": self._safe_error(exc, pack_id),
                    "updated_at": utc_now(),
                }
            )
            await self._run_blocking(self._save_state, pack_id, failed_state)
