"""语义元数据的扫描、校验和原子保存。"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .semantic_models import (
    IMAGE_EXTENSIONS,
    REVIEW_CATEGORY,
    REVIEW_CATEGORY_DESCRIPTION,
    SCHEMA_VERSION,
    SemanticImage,
    build_category_tag,
    build_semantic_text,
    category_analysis_is_current,
    category_context_hash,
    ensure_category_tag,
    is_category_tag,
    normalize_tags,
    semantic_entry_id,
    text_hash,
    utc_now,
)


def metadata_path(pack_dir: Path | str) -> Path:
    return Path(pack_dir).resolve() / "semantic_metadata.json"


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON through a same-directory temporary file.

    Args:
        path: Destination JSON path.
        payload: JSON object to persist.

    Raises:
        OSError: If the temporary file cannot be written or replaced.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file_obj:
            json.dump(payload, file_obj, ensure_ascii=False, indent=2)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def safe_relative_path(pack_dir: Path | str, relative_path: str) -> Path | None:
    """将相对路径安全地解析到资源包内，拒绝绝对路径和 .. 穿越。"""
    try:
        root = Path(pack_dir).resolve()
        raw_path = Path(str(relative_path or ""))
        if not raw_path.parts or raw_path.is_absolute() or ".." in raw_path.parts:
            return None
        candidate = (root / raw_path).resolve()
        candidate.relative_to(root)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return candidate


def file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        while True:
            chunk = file_obj.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def load_category_descriptions(pack_dir: Path | str) -> dict[str, str]:
    """读取图包分类描述，优先使用可编辑的 memes_data.json。"""
    root = Path(pack_dir).resolve()
    descriptions: dict[str, str] = {}
    data_path = root / "memes_data.json"
    if data_path.is_file():
        try:
            payload = json.loads(data_path.read_text(encoding="utf-8-sig"))
            if isinstance(payload, dict):
                descriptions.update(
                    {
                        str(key).strip(): str(value or "").strip()
                        for key, value in payload.items()
                        if str(key or "").strip()
                    }
                )
        except (OSError, json.JSONDecodeError):
            pass
    manifest_path = root / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            categories = (
                manifest.get("categories", {}) if isinstance(manifest, dict) else {}
            )
            if isinstance(categories, dict):
                for name, meta in categories.items():
                    category = str(name or "").strip()
                    if not category or category in descriptions:
                        continue
                    description = (
                        meta.get("description") if isinstance(meta, dict) else meta
                    )
                    descriptions[category] = str(description or "请添加描述").strip()
        except (OSError, json.JSONDecodeError):
            pass
    return descriptions


def scan_images(pack_dir: Path | str) -> list[dict[str, str]]:
    """逐文件扫描图片；同内容的不同路径也必须能独立人工修改。"""
    root = Path(pack_dir).resolve()
    memes_root = root / "memes"
    if not memes_root.is_dir():
        return []
    found: list[dict[str, str]] = []
    for path in sorted(memes_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        try:
            path.resolve().relative_to(memes_root.resolve())
        except ValueError:
            continue
        digest = file_sha256(path)
        relative = path.relative_to(root).as_posix()
        category = path.parent.name
        entry_id = semantic_entry_id(digest, category, relative)
        found.append(
            {
                "entry_id": entry_id,
                "content_sha256": digest,
                "relative_path": relative,
                "category": category,
            }
        )
    return found


def _is_sha256(value: Any) -> bool:
    text = str(value or "").lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _normalize_image_records(
    images: Any, category_descriptions: dict[str, str]
) -> dict[str, dict[str, Any]]:
    """规范 v2 记录，并统一改用“内容加分类”的稳定键。"""
    if not isinstance(images, dict):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for key, raw_value in images.items():
        if not isinstance(raw_value, dict):
            continue
        value = dict(raw_value)
        digest = str(value.get("content_sha256") or "").lower()
        if not _is_sha256(digest) and _is_sha256(key):
            # 允许 v2 文件仍以内容哈希作字典键；正式条目键不能反推图片内容。
            digest = str(key).lower()
        if not _is_sha256(digest):
            continue
        category = str(value.get("category") or "").strip()
        relative_path = str(value.get("relative_path") or "").replace("\\", "/")
        description = str(
            category_descriptions.get(category, value.get("category_description") or "")
        ).strip()
        entry_id = semantic_entry_id(digest, category, relative_path)
        old_context = str(value.get("category_context_hash") or "")
        new_context = category_context_hash(digest, category, description)
        context_changed = bool(old_context and old_context != new_context)
        previous_fixed_tag = str(value.get("category_tag") or "").strip()
        source_tags = [
            tag
            for tag in normalize_tags(value.get("tags"))
            if not previous_fixed_tag or tag != previous_fixed_tag
        ]
        value.update(
            {
                "entry_id": entry_id,
                "content_sha256": digest,
                "relative_path": relative_path,
                "category": category,
                "category_description": description,
                "category_tag": build_category_tag(category),
                "category_context_hash": new_context,
                "tags": ensure_category_tag(source_tags, category),
            }
        )
        value.setdefault(
            "caption_status",
            "done" if value.get("caption") and value.get("tags") else "pending",
        )
        value.setdefault("embedding_status", "pending")
        if context_changed:
            # 分类名/描述变化后必须重新让模型判断；已有描述仍保留，人工内容
            # 也不会在这里被删除，但旧确认和旧向量立即失效。
            value["caption_status"] = "pending"
            value["embedding_status"] = "pending"
            value["category_review_status"] = "unchecked"
            value["category_fit"] = "uncertain"
            value["category_review_reason"] = ""
            value["category_review_context_hash"] = ""
            value["manual_confirmation_context_hash"] = ""
            value["text_hash"] = ""
        elif not old_context:
            # v2 导入数据若缺少上下文指纹，保留描述但重新做分类符合判断。
            value.setdefault("category_review_status", "unchecked")
            value.setdefault("category_review_reason", "")
            value.setdefault("category_review_context_hash", "")
            value.setdefault("manual_confirmation_context_hash", "")
            if value.get("caption_status") == "done":
                value["embedding_status"] = "pending"
                value["text_hash"] = ""
        item = SemanticImage.from_dict(value).to_dict()
        normalized[entry_id] = item
    return normalized


def load_metadata(pack_dir: Path | str) -> dict[str, Any]:
    path = metadata_path(pack_dir)
    if not path.is_file():
        return {
            "schema_version": SCHEMA_VERSION,
            "pack_id": Path(pack_dir).name,
            "generated_at": utc_now(),
            "images": {},
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    loaded_schema_version = str(data.get("schema_version") or "")
    if loaded_schema_version != SCHEMA_VERSION:
        return {
            "schema_version": SCHEMA_VERSION,
            "pack_id": str(data.get("pack_id") or Path(pack_dir).name),
            "generated_at": utc_now(),
            "images": {},
            "legacy_semantic_data_discarded": bool(data),
            "requires_local_index_rebuild": True,
        }
    category_descriptions = load_category_descriptions(pack_dir)
    normalized = _normalize_image_records(data.get("images"), category_descriptions)
    data["schema_version"] = SCHEMA_VERSION
    data["pack_id"] = str(data.get("pack_id") or Path(pack_dir).name)
    data.setdefault("generated_at", utc_now())
    data["images"] = normalized
    return data


def semantic_metadata_is_complete(
    pack_dir: Path | str,
    data: dict[str, Any] | None = None,
    *,
    require_embeddings: bool = False,
) -> bool:
    """Return whether every current image has complete semantic metadata.

    Args:
        pack_dir: Root directory of the meme pack.
        data: Previously loaded metadata, if available.
        require_embeddings: Whether every image must also have a completed
            local embedding.

    Returns:
        True only when disk contents, the scan snapshot, and completed records
        match exactly.
    """
    root = Path(pack_dir).resolve()
    memes_root = root / "memes"
    metadata_file = metadata_path(root)
    image_paths = (
        [
            path
            for path in sorted(memes_root.rglob("*"))
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
        ]
        if memes_root.is_dir()
        else []
    )

    metadata = data if isinstance(data, dict) else load_metadata(root)
    images = metadata.get("images", {})
    complete = False
    if metadata_file.is_file() and image_paths and isinstance(images, dict) and images:
        scanned = scan_images(root)
        current_entry_ids = {str(item.get("entry_id") or "") for item in scanned}
        try:
            recorded_file_total = int(metadata.get("file_total"))
            recorded_unique_total = int(metadata.get("unique_total"))
        except (TypeError, ValueError):
            recorded_file_total = -1
            recorded_unique_total = -1
        complete = bool(
            current_entry_ids
            and set(images) == current_entry_ids
            and recorded_file_total == len(image_paths)
            and recorded_unique_total == len(current_entry_ids)
            and all(
                isinstance(images.get(entry_id), dict)
                and images[entry_id].get("caption_status") == "done"
                and category_analysis_is_current(images[entry_id])
                and str(images[entry_id].get("caption") or "").strip()
                and normalize_tags(images[entry_id].get("tags"))
                and (
                    not require_embeddings
                    or images[entry_id].get("embedding_status") == "done"
                )
                for entry_id in current_entry_ids
            )
        )
    return complete


def get_pack_semantic_summary(
    pack_dir: Path | str, image_count: int | None = None
) -> dict[str, Any]:
    """返回适合 WebUI 展示的图包语义化进度摘要。

    语义任务按具体文件路径保留独立记录，因此同内容图片也能被逐张人工修改。
    完成状态同时校验上次语义扫描时的文件数，避免在完整语义化后新增图片，
    主页仍错误显示为“已完成语义化”。
    """
    root = Path(pack_dir).resolve()
    if image_count is None:
        memes_root = root / "memes"
        current_file_total = (
            sum(
                1
                for path in memes_root.rglob("*")
                if path.is_file()
                and path.suffix.lower() in IMAGE_EXTENSIONS.union({".webp"})
            )
            if memes_root.is_dir()
            else 0
        )
    else:
        current_file_total = max(0, int(image_count))

    metadata_file = root / "semantic_metadata.json"
    if not metadata_file.is_file():
        return {
            "semantic_status": "none",
            "semantic_caption_done": 0,
            "semantic_caption_total": 0,
            "semantic_caption_failed": 0,
            "semantic_file_total": current_file_total,
            "semantic_snapshot_matches": False,
            "semantic_files_changed": False,
        }

    metadata = load_metadata(root)
    images = [
        item for item in metadata.get("images", {}).values() if isinstance(item, dict)
    ]
    caption_done = sum(
        1
        for item in images
        if item.get("caption_status") == "done"
        and str(item.get("caption") or "").strip()
        and item.get("tags")
    )
    caption_failed = sum(1 for item in images if item.get("caption_status") == "failed")
    try:
        recorded_unique_total = int(metadata.get("unique_total", len(images)))
    except (TypeError, ValueError):
        recorded_unique_total = len(images)
    semantic_total = max(0, recorded_unique_total, len(images))

    raw_snapshot_total = metadata.get("file_total")
    if raw_snapshot_total is None:
        # 外部语义文件没有文件数快照时，不能仅凭“现有记录都完成”
        # 就断言整个图包已完成；至少要求记录数能覆盖当前文件数。
        snapshot_file_total = semantic_total
    else:
        try:
            snapshot_file_total = max(0, int(raw_snapshot_total))
        except (TypeError, ValueError):
            snapshot_file_total = -1
    snapshot_matches = snapshot_file_total == current_file_total
    all_records_done = bool(images) and caption_done == len(images)
    completion_candidate = (
        current_file_total > 0
        and semantic_total > 0
        and snapshot_matches
        and all_records_done
        and caption_done >= semantic_total
    )
    strictly_complete = bool(
        completion_candidate and semantic_metadata_is_complete(root, metadata)
    )
    if strictly_complete:
        semantic_status = "complete"
    elif images:
        semantic_status = "partial"
    else:
        semantic_status = "none"

    return {
        "semantic_status": semantic_status,
        "semantic_caption_done": caption_done,
        "semantic_caption_total": semantic_total,
        "semantic_caption_failed": caption_failed,
        "semantic_file_total": current_file_total,
        "semantic_snapshot_matches": snapshot_matches,
        "semantic_files_changed": bool(
            not snapshot_matches or (completion_candidate and not strictly_complete)
        ),
    }


def get_image_semantic_detail(
    pack_dir: Path | str, image_path: Path | str
) -> dict[str, Any]:
    """按当前文件路径读取语义，并报告同内容的其他文件但不共享修改。"""
    root = Path(pack_dir).resolve()
    memes_root = (root / "memes").resolve()
    requested_source = Path(image_path)
    if requested_source.is_symlink():
        raise ValueError("不允许通过符号链接编辑图片")
    source = requested_source.resolve()
    try:
        source.relative_to(memes_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("图片路径不属于当前表情包") from exc
    if not source.is_file():
        raise FileNotFoundError("图片不存在")

    digest = file_sha256(source)
    relative_path = source.relative_to(root).as_posix()
    category = source.parent.name
    entry_id = semantic_entry_id(digest, category, relative_path)
    same_content_paths: list[str] = []
    source_size = source.stat().st_size
    if memes_root.is_dir():
        for candidate in sorted(memes_root.rglob("*")):
            if (
                candidate == source
                or not candidate.is_file()
                or candidate.suffix.lower() not in IMAGE_EXTENSIONS
            ):
                continue
            try:
                candidate.resolve().relative_to(memes_root)
                if candidate.stat().st_size != source_size:
                    continue
                if file_sha256(candidate) == digest:
                    same_content_paths.append(candidate.relative_to(root).as_posix())
            except (OSError, RuntimeError, ValueError):
                continue

    empty_result = {
        "status": "none",
        "entry_id": entry_id,
        "content_sha256": digest,
        "relative_path": relative_path,
        "caption": "",
        "tags": [],
        "editable_tags": [],
        "visible_text": "",
        "caption_status": "pending",
        "embedding_status": "pending",
        "error": "",
        "embedding_error": "",
        "category": category,
        "category_tag": build_category_tag(category),
        "fixed_category_tags": [build_category_tag(category)],
        "category_review_status": "unchecked",
        "category_review_reason": "",
        "reclassification_status": "",
        "reclassified_from_category": "",
        "reclassified_to_category": "",
        "reclassification_reason": "",
        "reclassified_at": "",
        "can_confirm_category": False,
        "can_edit_semantic": True,
        "manual_override": False,
        "manual_modified": False,
        "generation_source": "automatic",
        "can_restore_auto": False,
        "same_content_paths": same_content_paths,
        "same_content_count": len(same_content_paths),
        "edit_scope": "current_path",
        "shared_update": False,
    }
    metadata = load_metadata(root)
    images = metadata.get("images", {})
    if not images:
        return empty_result

    item = images.get(entry_id)
    if not isinstance(item, dict):
        return empty_result

    caption = str(item.get("caption") or "").strip()
    tags = normalize_tags(item.get("tags"))
    caption_status = str(item.get("caption_status") or "pending")
    if caption_status == "done" and caption and tags:
        status = "complete"
    elif caption_status == "failed":
        status = "failed"
    else:
        status = "pending"
    category_tag = str(item.get("category_tag") or build_category_tag(category))
    editable_tags = [tag for tag in tags if not is_category_tag(tag)]
    manual_modified = bool(
        item.get("manual_override") or item.get("provenance") in {"manual", "mixed"}
    )
    error = str(item.get("error") or "").strip()
    embedding_status = str(item.get("embedding_status") or "pending")
    return {
        "status": status,
        "entry_id": entry_id,
        "content_sha256": digest,
        "relative_path": relative_path,
        "caption": caption,
        "tags": tags,
        "editable_tags": editable_tags,
        "visible_text": str(item.get("visible_text") or "").strip(),
        "caption_status": caption_status,
        "embedding_status": embedding_status,
        "error": error,
        "embedding_error": error if embedding_status == "failed" else "",
        "category": str(item.get("category") or category),
        "category_description": str(item.get("category_description") or ""),
        "category_tag": category_tag,
        "fixed_category_tags": [category_tag] if category_tag else [],
        "category_review_status": str(
            item.get("category_review_status") or "unchecked"
        ),
        "category_review_reason": str(item.get("category_review_reason") or ""),
        "reclassification_status": str(item.get("reclassification_status") or ""),
        "reclassified_from_category": str(item.get("reclassified_from_category") or ""),
        "reclassified_to_category": str(item.get("reclassified_to_category") or ""),
        "reclassification_reason": str(item.get("reclassification_reason") or ""),
        "reclassified_at": str(item.get("reclassified_at") or ""),
        "can_confirm_category": bool(
            item.get("caption_status") == "done"
            and item.get("category_review_status") != "manual_confirmed"
        ),
        "can_edit_semantic": True,
        "manual_override": manual_modified,
        "manual_modified": manual_modified,
        "generation_source": "manual" if manual_modified else "automatic",
        "can_restore_auto": manual_modified,
        "same_content_paths": same_content_paths,
        "same_content_count": len(same_content_paths),
        "edit_scope": "current_path",
        "shared_update": False,
    }


def save_metadata(pack_dir: Path | str, data: dict[str, Any]) -> Path:
    """使用同目录临时文件 + replace，避免断电留下半份 JSON。"""
    target = metadata_path(pack_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(data or {})
    payload["schema_version"] = SCHEMA_VERSION
    payload["pack_id"] = str(payload.get("pack_id") or target.parent.name)
    payload.setdefault("generated_at", utc_now())
    payload["images"] = _normalize_image_records(
        payload.get("images"), load_category_descriptions(target.parent)
    )
    _write_json_atomic(target, payload)
    return target


def _resolve_image_edit_snapshot(
    pack_dir: Path | str,
    image_path: Path | str,
    *,
    expected_content_sha256: str = "",
    expected_entry_id: str = "",
) -> tuple[Path, Path, str, str, dict[str, Any], dict[str, Any]]:
    """重新核对编辑对象，拒绝删除、移动、换图或旧页面提交。"""
    root = Path(pack_dir).resolve()
    memes_root = (root / "memes").resolve()
    requested_source = Path(image_path)
    if requested_source.is_symlink():
        raise ValueError("不允许通过符号链接编辑图片")
    source = requested_source.resolve()
    try:
        source.relative_to(memes_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("图片路径不属于当前表情包") from exc
    if not source.is_file():
        raise FileNotFoundError("图片已被删除或移动，请重新打开后再编辑")
    if source.suffix.lower() not in IMAGE_EXTENSIONS:
        raise ValueError("当前文件不是支持的图片格式")

    digest = file_sha256(source)
    expected_digest = str(expected_content_sha256 or "").strip().lower()
    if expected_digest and digest != expected_digest:
        raise RuntimeError("图片内容已发生变化，请重新打开后再编辑")
    relative_path = source.relative_to(root).as_posix()
    entry_id = semantic_entry_id(digest, source.parent.name, relative_path)
    if expected_entry_id and str(expected_entry_id) != entry_id:
        raise RuntimeError("图片位置或内容已发生变化，请重新打开后再编辑")

    metadata = reconcile_metadata(root)
    raw_item = metadata.get("images", {}).get(entry_id)
    if not isinstance(raw_item, dict):
        raise RuntimeError("图片语义记录已发生变化，请重新打开后再编辑")
    if str(raw_item.get("relative_path") or "") != relative_path:
        raise RuntimeError("图片位置已发生变化，请重新打开后再编辑")
    return root, source, digest, entry_id, metadata, raw_item


def _assert_image_snapshot_unchanged(
    root: Path, source: Path, expected_digest: str, expected_entry_id: str
) -> None:
    """写入前最后复核文件，缩小外部文件变更造成的竞态窗口。"""
    if not source.is_file():
        raise FileNotFoundError("图片已被删除或移动，请重新打开后再编辑")
    current_digest = file_sha256(source)
    relative_path = source.relative_to(root).as_posix()
    current_entry_id = semantic_entry_id(
        current_digest, source.parent.name, relative_path
    )
    if current_digest != expected_digest or current_entry_id != expected_entry_id:
        raise RuntimeError("图片位置或内容已发生变化，请重新打开后再编辑")


def save_manual_image_semantic(
    pack_dir: Path | str,
    image_path: Path | str,
    *,
    caption: str,
    tags: Any,
    visible_text: str = "",
    category_decision: str = "keep",
    expected_content_sha256: str = "",
    expected_entry_id: str = "",
) -> dict[str, Any]:
    """原子保存一张图片的人工语义，并只让该记录等待向量更新。"""
    normalized_caption = str(caption or "").strip()
    normalized_visible_text = str(visible_text or "").strip()
    if not normalized_caption:
        raise ValueError("图片含义不能为空")
    if len(normalized_caption) > 4000:
        raise ValueError("图片含义不能超过 4000 个字符")
    if len(normalized_visible_text) > 4000:
        raise ValueError("图片内文字不能超过 4000 个字符")
    normalized_tags = normalize_tags(tags)
    if len(normalized_tags) > 50 or any(len(tag) > 80 for tag in normalized_tags):
        raise ValueError("普通语义标签最多 50 个，每个不能超过 80 个字符")

    root, source, digest, entry_id, metadata, raw_item = _resolve_image_edit_snapshot(
        pack_dir,
        image_path,
        expected_content_sha256=expected_content_sha256,
        expected_entry_id=expected_entry_id,
    )
    item = SemanticImage.from_dict(raw_item)
    fixed_tag = item.category_tag
    illegal_fixed_tags = [
        tag for tag in normalized_tags if is_category_tag(tag) and tag != fixed_tag
    ]
    if illegal_fixed_tags:
        raise ValueError("固定分类标签不能在语义标签中新增或修改；请移动图片分类")
    editable_tags = [
        tag for tag in normalized_tags if tag != fixed_tag and not is_category_tag(tag)
    ]

    was_manual = bool(item.manual_override or item.provenance in {"manual", "mixed"})
    if not was_manual:
        item.auto_caption = item.caption
        item.auto_tags = [tag for tag in item.tags if tag != fixed_tag]
        item.auto_visible_text = item.visible_text
        item.auto_category_fit = item.category_fit
        item.auto_category_review_status = item.category_review_status
        item.auto_category_review_reason = item.category_review_reason

    item.manual_caption = normalized_caption
    item.manual_tags = editable_tags
    item.manual_visible_text = normalized_visible_text
    item.caption = normalized_caption
    item.tags = ensure_category_tag(editable_tags, item.category)
    item.visible_text = normalized_visible_text
    item.manual_override = True
    item.provenance = "manual"
    decision = str(category_decision or "keep").strip().lower()
    if decision == "match":
        item.category_fit = "match"
        item.category_review_status = "manual_confirmed"
        item.category_review_reason = ""
        item.category_review_context_hash = item.category_context_hash
        item.manual_confirmation_context_hash = item.category_context_hash
    elif decision in {"mismatch", "conflict"}:
        item.category_fit = "conflict"
        item.category_review_status = "manual_rejected"
        item.category_review_reason = "人工确认当前分类不符合，请移动到正确分类"
        item.category_review_context_hash = item.category_context_hash
        item.manual_confirmation_context_hash = item.category_context_hash
    elif decision != "keep":
        raise ValueError("分类确认状态无效")

    item.caption_status = "done"
    item.embedding_status = "pending"
    item.text_hash = text_hash(item.vector_text)
    item.error = None
    item.updated_at = utc_now()
    _assert_image_snapshot_unchanged(root, source, digest, entry_id)
    raw_item.update(item.to_dict())
    metadata["requires_local_index_rebuild"] = True
    metadata["last_manual_edit_at"] = item.updated_at
    save_metadata(root, metadata)
    return get_image_semantic_detail(root, source)


def restore_image_auto_semantic(
    pack_dir: Path | str,
    image_path: Path | str,
    *,
    expected_content_sha256: str = "",
    expected_entry_id: str = "",
) -> dict[str, Any]:
    """显式放弃当前路径的人工内容，恢复旧自动结果或等待重新生成。"""
    root, source, digest, entry_id, metadata, raw_item = _resolve_image_edit_snapshot(
        pack_dir,
        image_path,
        expected_content_sha256=expected_content_sha256,
        expected_entry_id=expected_entry_id,
    )
    item = SemanticImage.from_dict(raw_item)
    if not (item.manual_override or item.provenance in {"manual", "mixed"}):
        raise ValueError("这张图片当前没有人工修改，无需恢复")

    item.manual_override = False
    item.provenance = "ai"
    item.manual_caption = ""
    item.manual_tags = []
    item.manual_visible_text = ""
    if item.auto_caption and item.auto_tags:
        item.caption = item.auto_caption
        item.tags = ensure_category_tag(item.auto_tags, item.category)
        item.visible_text = item.auto_visible_text
        item.category_fit = item.auto_category_fit
        item.category_review_status = item.auto_category_review_status
        item.category_review_reason = item.auto_category_review_reason
        if item.category_review_status in {"auto_match", "needs_review"}:
            item.category_review_context_hash = item.category_context_hash
        else:
            item.category_review_status = "unchecked"
            item.category_review_context_hash = ""
        item.caption_status = "done"
        item.text_hash = text_hash(item.vector_text)
    else:
        item.caption = ""
        item.tags = ensure_category_tag([], item.category)
        item.visible_text = ""
        item.category_fit = "uncertain"
        item.category_review_status = "unchecked"
        item.category_review_reason = ""
        item.category_review_context_hash = ""
        item.caption_status = "pending"
        item.text_hash = ""
    item.manual_confirmation_context_hash = ""
    item.embedding_status = "pending"
    item.error = None
    item.updated_at = utc_now()
    _assert_image_snapshot_unchanged(root, source, digest, entry_id)
    raw_item.update(item.to_dict())
    metadata["requires_local_index_rebuild"] = True
    metadata["last_manual_restore_at"] = item.updated_at
    save_metadata(root, metadata)
    return get_image_semantic_detail(root, source)


def set_image_embedding_failure(
    pack_dir: Path | str,
    image_path: Path | str,
    error: str,
    *,
    expected_content_sha256: str = "",
    expected_entry_id: str = "",
) -> dict[str, Any]:
    """记录单图向量更新失败，不改变已保存的人工语义。"""
    root, source, digest, entry_id, metadata, raw_item = _resolve_image_edit_snapshot(
        pack_dir,
        image_path,
        expected_content_sha256=expected_content_sha256,
        expected_entry_id=expected_entry_id,
    )
    item = SemanticImage.from_dict(raw_item)
    item.embedding_status = "failed"
    item.error = str(error or "向量更新失败").strip()[:1000]
    item.updated_at = utc_now()
    _assert_image_snapshot_unchanged(root, source, digest, entry_id)
    raw_item.update(item.to_dict())
    metadata["requires_local_index_rebuild"] = True
    save_metadata(root, metadata)
    return get_image_semantic_detail(root, source)


def _is_safe_category_key(category: str) -> bool:
    value = str(category or "").strip()
    return bool(
        value
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and Path(value).name == value
    )


def _ensure_pack_category(pack_dir: Path, category: str, description: str) -> None:
    """Create a safe pack category and synchronize its metadata.

    Args:
        pack_dir: Meme pack root directory.
        category: Exact category key to create.
        description: Human-readable category description.

    Raises:
        ValueError: If the category key is unsafe.
        OSError: If the directory or metadata files cannot be written.
    """
    if not _is_safe_category_key(category):
        raise ValueError("自动复核分类名称无效")
    memes_root = pack_dir / "memes"
    (memes_root / category).mkdir(parents=True, exist_ok=True)

    descriptions = load_category_descriptions(pack_dir)
    if descriptions.get(category) != description:
        descriptions[category] = description
        _write_json_atomic(pack_dir / "memes_data.json", descriptions)

    manifest_path = pack_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        manifest = {}
    if not isinstance(manifest, dict):
        manifest = {}
    manifest.setdefault("schema_version", 1)
    manifest.setdefault("id", pack_dir.name)
    manifest.setdefault("name", f"Meme Pack {pack_dir.name}")
    manifest.setdefault("version", "1.0.0")
    manifest.setdefault("description", "Runtime-managed meme pack")
    manifest.setdefault("tags", ["runtime"])
    categories = manifest.get("categories")
    if not isinstance(categories, dict):
        categories = {}
        manifest["categories"] = categories
    expected = {"description": description}
    if categories.get(category) != expected:
        categories[category] = expected
        _write_json_atomic(manifest_path, manifest)


def apply_conflict_reclassifications(
    pack_dir: Path | str, data: dict[str, Any]
) -> dict[str, Any]:
    """Move explicit category conflicts and persist their audit records.

    Args:
        pack_dir: Meme pack root directory.
        data: Current schema-v2 semantic metadata.

    Returns:
        Counts for moved, existing-target, review-target, and skipped items.

    Raises:
        OSError: If a file move or metadata write fails.
        ValueError: If a generated target path escapes the meme pack.
    """
    root = Path(pack_dir).resolve()
    memes_root = (root / "memes").resolve()
    images = data.get("images")
    if not isinstance(images, dict):
        return {"moved": 0, "to_existing": 0, "to_review": 0, "skipped": 0}

    descriptions = load_category_descriptions(root)
    existing_categories = (
        {
            path.name
            for path in memes_root.iterdir()
            if path.is_dir() and _is_safe_category_key(path.name)
        }
        if memes_root.is_dir()
        else set()
    )
    original_images = copy.deepcopy(images)
    completed_moves: list[tuple[Path, Path]] = []
    result = {"moved": 0, "to_existing": 0, "to_review": 0, "skipped": 0}

    try:
        for old_entry_id, raw_item in list(images.items()):
            if not isinstance(raw_item, dict):
                continue
            item = SemanticImage.from_dict(raw_item)
            if (
                item.caption_status != "done"
                or item.category_fit != "conflict"
                or item.category_review_status == "manual_confirmed"
            ):
                continue
            if item.manual_override or item.provenance in {"manual", "mixed"}:
                result["skipped"] += 1
                continue

            source = safe_relative_path(root, item.relative_path)
            if (
                source is None
                or not source.is_file()
                or source.parent.name != item.category
                or file_sha256(source) != item.content_sha256
            ):
                result["skipped"] += 1
                continue

            suggested = str(item.suggested_category or "").strip()
            use_existing = bool(
                suggested
                and suggested != item.category
                and suggested != REVIEW_CATEGORY
                and suggested in existing_categories
            )
            target_category = suggested if use_existing else REVIEW_CATEGORY
            if item.category == target_category:
                result["skipped"] += 1
                continue
            if target_category == REVIEW_CATEGORY:
                _ensure_pack_category(
                    root, REVIEW_CATEGORY, REVIEW_CATEGORY_DESCRIPTION
                )
                descriptions[REVIEW_CATEGORY] = REVIEW_CATEGORY_DESCRIPTION
                existing_categories.add(REVIEW_CATEGORY)

            target_dir = (memes_root / target_category).resolve()
            try:
                target_dir.relative_to(memes_root)
            except ValueError as exc:
                raise ValueError("自动重分类目标路径无效") from exc
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / source.name
            suffix = source.suffix
            stem = source.stem
            index = 1
            while target.exists():
                target = target_dir / f"{stem}_{index}{suffix}"
                index += 1
            source.rename(target)
            completed_moves.append((target, source))

            moved_at = utc_now()
            reason = str(item.category_review_reason or "图片与原分类明显不符").strip()
            move_status = "auto_reclassified" if use_existing else "moved_to_review"
            history_item = {
                "from_category": item.category,
                "to_category": target_category,
                "reason": reason,
                "status": move_status,
                "at": moved_at,
            }
            target_relative_path = target.relative_to(root).as_posix()
            new_entry_id = semantic_entry_id(
                item.content_sha256, target_category, target_relative_path
            )
            existing_target = images.get(new_entry_id)
            keep_existing_target = bool(
                isinstance(existing_target, dict)
                and new_entry_id != old_entry_id
                and (
                    existing_target.get("manual_override")
                    or existing_target.get("provenance") in {"manual", "mixed"}
                    or (
                        existing_target.get("caption_status") == "done"
                        and existing_target.get("caption")
                        and existing_target.get("tags")
                    )
                )
            )
            if keep_existing_target:
                moved_item = SemanticImage.from_dict(existing_target)
            else:
                moved_value = dict(raw_item)
                previous_fixed_tag = build_category_tag(item.category)
                moved_value.update(
                    {
                        "relative_path": target_relative_path,
                        "category": target_category,
                        "category_description": str(
                            descriptions.get(target_category) or ""
                        ),
                        "category_context_hash": "",
                        "category_review_status": "unchecked",
                        "category_review_context_hash": "",
                        "manual_confirmation_context_hash": "",
                        "category_tag": "",
                        "tags": [
                            tag
                            for tag in normalize_tags(raw_item.get("tags"))
                            if tag != previous_fixed_tag
                        ],
                        "auto_tags": [
                            tag
                            for tag in normalize_tags(raw_item.get("auto_tags"))
                            if tag != previous_fixed_tag
                        ],
                        "manual_tags": [
                            tag
                            for tag in normalize_tags(raw_item.get("manual_tags"))
                            if tag != previous_fixed_tag
                        ],
                    }
                )
                moved_item = SemanticImage.from_dict(moved_value)

            moved_item.reclassification_history.append(history_item)
            moved_item.reclassification_history = moved_item.reclassification_history[
                -20:
            ]
            moved_item.reclassification_status = move_status
            moved_item.reclassified_from_category = item.category
            moved_item.reclassified_to_category = target_category
            moved_item.reclassification_reason = reason
            moved_item.reclassified_at = moved_at
            moved_item.suggested_category = suggested if use_existing else ""
            if moved_item.category_review_status != "manual_confirmed":
                moved_item.category_fit = "uncertain"
                moved_item.category_review_status = "needs_review"
                moved_item.category_review_reason = (
                    f"已从 {item.category} 自动移至 {target_category}：{reason}"
                )[:500]
                moved_item.category_review_context_hash = (
                    moved_item.category_context_hash
                )
                moved_item.manual_confirmation_context_hash = ""
            moved_item.embedding_status = "pending"
            moved_item.text_hash = text_hash(moved_item.vector_text)
            moved_item.updated_at = moved_at

            images.pop(old_entry_id, None)
            images[new_entry_id] = moved_item.to_dict()
            result["moved"] += 1
            if use_existing:
                result["to_existing"] += 1
            else:
                result["to_review"] += 1

        if result["moved"]:
            scanned = scan_images(root)
            scanned_entry_ids = {item["entry_id"] for item in scanned}
            data["file_total"] = sum(
                1
                for path in memes_root.rglob("*")
                if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
            )
            data["unique_total"] = len(scanned_entry_ids)
            data["content_unique_total"] = len(
                {item["content_sha256"] for item in scanned}
            )
            data["reused_duplicate_files"] = max(
                0,
                data["file_total"]
                - len(
                    {(entry["content_sha256"], entry["category"]) for entry in scanned}
                ),
            )
            data["cross_category_duplicate_entries"] = max(
                0, data["unique_total"] - data["content_unique_total"]
            )
            data["requires_local_index_rebuild"] = True
            data["last_reclassification_at"] = utc_now()
            save_metadata(root, data)
    except Exception:
        for target, source in reversed(completed_moves):
            if target.is_file() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                target.rename(source)
        data["images"] = original_images
        raise
    return result


def reset_local_embedding_state(data: dict[str, Any]) -> dict[str, Any]:
    """移除只对生成者本机有效的向量状态，保留可发布的图片语义描述。"""
    payload = dict(data or {})
    for key in (
        "embedding_provider_id",
        "embedding_model",
        "embedding_dimension",
        "verified_embedding_dimension",
        "embedding_verified_dimension",
        "embedding_dimension_verified",
        "dimension_verified",
        "verified_dimension",
        "index_dimension",
        "index_embedding_dimension",
        "embedding_signature",
        "embedding",
        "embeddings",
        "vector",
        "vectors",
        "faiss_id",
    ):
        payload.pop(key, None)
    images = payload.get("images", {})
    normalized_images: dict[str, dict[str, Any]] = {}
    if isinstance(images, dict):
        for entry_id, value in images.items():
            if not isinstance(value, dict):
                continue
            item = dict(value)
            item["embedding_status"] = "pending"
            for key in (
                "embedding_provider_id",
                "embedding_model",
                "embedding_dimension",
                "verified_embedding_dimension",
                "embedding_verified_dimension",
                "embedding_dimension_verified",
                "dimension_verified",
                "verified_dimension",
                "index_dimension",
                "index_embedding_dimension",
                "embedding_signature",
                "embedding",
                "embeddings",
                "vector",
                "vectors",
                "faiss_id",
            ):
                item.pop(key, None)
            if item.get("caption_status") == "done":
                item["error"] = None
            normalized_images[str(entry_id)] = item
    payload["images"] = normalized_images
    payload["requires_local_index_rebuild"] = True
    return payload


def reconcile_metadata(
    pack_dir: Path | str, external_data: dict[str, Any] | None = None
) -> dict[str, Any]:
    """合并磁盘与语义记录，并按具体文件路径隔离人工内容和向量状态。"""
    root = Path(pack_dir).resolve()
    descriptions = load_category_descriptions(root)
    existing = load_metadata(root)
    external_is_current = bool(
        isinstance(external_data, dict)
        and str(external_data.get("schema_version") or "") == SCHEMA_VERSION
    )
    external_images = _normalize_image_records(
        (external_data or {}).get("images", {}) if external_is_current else {},
        descriptions,
    )
    local_images = existing.get("images", {})
    scanned = scan_images(root)
    scanned_by_id = {item["entry_id"]: item for item in scanned}

    def records_for_content(
        source: dict[str, dict[str, Any]], digest: str
    ) -> list[dict[str, Any]]:
        return [
            item
            for item in source.values()
            if isinstance(item, dict)
            and str(item.get("content_sha256") or "").lower() == digest
        ]

    def choose_previous(entry_id: str, digest: str) -> tuple[dict[str, Any], bool]:
        exact_local = local_images.get(entry_id)
        exact_external = external_images.get(entry_id)
        for candidate in (exact_local, exact_external):
            if isinstance(candidate, dict) and (
                candidate.get("provenance") in {"manual", "mixed"}
                or (candidate.get("caption") and candidate.get("tags"))
            ):
                return candidate, True
        for candidate in (exact_local, exact_external):
            if isinstance(candidate, dict):
                return candidate, True
        # 移动/重命名时可复用内容描述作为起点，但分类判断、固定标签、人工
        # 确认和向量一律失效，下一轮必须携带新分类重新识别。
        content_candidates = records_for_content(local_images, digest)
        content_candidates.extend(records_for_content(external_images, digest))
        manual_candidates = [
            item
            for item in content_candidates
            if item.get("provenance") in {"manual", "mixed"}
            or item.get("manual_override")
        ]
        reusable = manual_candidates or [
            item
            for item in content_candidates
            if item.get("caption") and item.get("tags")
        ]
        if not reusable:
            return {}, False
        candidate = dict(reusable[0])
        source_entry_id = str(candidate.get("entry_id") or "")
        if source_entry_id in scanned_by_id:
            # 来源记录仍在磁盘上，说明这是另一个分类中新出现的重复图，而不是
            # 原图被移动/分类被重命名。人工内容只属于来源条目，不能把保护状态
            # 复制到新分类，否则视觉模型无法按新分类重新生成语义。
            was_manual = bool(
                candidate.get("manual_override")
                or candidate.get("provenance") in {"manual", "mixed"}
            )
            candidate["manual_override"] = False
            candidate["manual_caption"] = ""
            candidate["manual_tags"] = []
            candidate["manual_visible_text"] = ""
            candidate["provenance"] = "ai"
            if was_manual:
                candidate["caption"] = str(candidate.get("auto_caption") or "")
                candidate["tags"] = normalize_tags(candidate.get("auto_tags"))
                candidate["visible_text"] = str(
                    candidate.get("auto_visible_text") or ""
                )
                candidate["category_fit"] = str(
                    candidate.get("auto_category_fit") or "uncertain"
                )
                candidate["category_review_status"] = str(
                    candidate.get("auto_category_review_status") or "unchecked"
                )
                candidate["category_review_reason"] = str(
                    candidate.get("auto_category_review_reason") or ""
                )
        return candidate, False

    images: dict[str, dict[str, Any]] = {}
    for entry_id, scan in scanned_by_id.items():
        digest = scan["content_sha256"]
        previous, exact_context = choose_previous(entry_id, digest)
        item = dict(previous)
        category = scan["category"]
        description = str(descriptions.get(category, "")).strip()
        current_context = category_context_hash(digest, category, description)
        previous_context = str(item.get("category_context_hash") or "")
        previous_fixed_tag = str(item.get("category_tag") or "").strip()
        item.update(
            {
                "entry_id": entry_id,
                "content_sha256": digest,
                "relative_path": scan["relative_path"],
                "category": category,
                "category_description": description,
                "category_tag": build_category_tag(category),
                "category_context_hash": current_context,
            }
        )
        item.setdefault("caption", "")
        source_tags = [
            tag
            for tag in normalize_tags(item.get("tags"))
            if not previous_fixed_tag or tag != previous_fixed_tag
        ]
        item["tags"] = ensure_category_tag(source_tags, category)
        for tag_field in ("manual_tags", "auto_tags"):
            item[tag_field] = [
                tag
                for tag in normalize_tags(item.get(tag_field))
                if not previous_fixed_tag or tag != previous_fixed_tag
            ]
        item.setdefault("visible_text", "")
        item.setdefault(
            "caption_status",
            "done" if item.get("caption") and item.get("tags") else "pending",
        )
        item.setdefault("embedding_status", "pending")
        item.setdefault("provenance", "ai")
        item.setdefault("auto_tags", item.get("tags", []))
        item.setdefault("manual_tags", [])
        item.setdefault("manual_override", False)
        item.setdefault("prompt_version", "meme-semantic-v1")
        item.setdefault("error", None)
        item.setdefault("category_fit", "uncertain")
        item.setdefault("category_review_status", "unchecked")
        item.setdefault("category_review_reason", "")
        item.setdefault("category_review_context_hash", "")
        item.setdefault("manual_confirmation_context_hash", "")
        context_changed = bool(previous_context and previous_context != current_context)
        if not exact_context or context_changed:
            item["caption_status"] = "pending"
            item["embedding_status"] = "pending"
            item["category_review_status"] = "unchecked"
            item["category_fit"] = "uncertain"
            item["category_review_reason"] = ""
            item["category_review_context_hash"] = ""
            item["manual_confirmation_context_hash"] = ""
            item["text_hash"] = ""
            item["error"] = None
        if item.get("caption_status") == "done" and (
            not str(item.get("caption") or "").strip() or not item.get("tags")
        ):
            item["caption_status"] = "pending"
            item["embedding_status"] = "pending"
        item["updated_at"] = utc_now()
        current_text = build_semantic_text(
            item.get("caption", ""),
            item.get("tags", []),
            item.get("visible_text", ""),
            item.get("category", ""),
            item.get("category_description", ""),
        )
        calculated_hash = text_hash(current_text)
        if item.get("text_hash") and item["text_hash"] != calculated_hash:
            item["embedding_status"] = "pending"
        item["text_hash"] = calculated_hash if item.get("caption") else ""
        normalized_item = SemanticImage.from_dict(item).to_dict()
        images[entry_id] = normalized_item
    # 仅保留本次外部导入中缺图的记录供用户排查；本地已删除、移动或重命名
    # 产生的旧记录必须移除，否则旧分类标签会进入下一次索引队列。
    for source in (external_images,):
        for entry_id, value in source.items():
            if entry_id in images or not isinstance(value, dict):
                continue
            item = dict(value)
            if safe_relative_path(root, item.get("relative_path", "")) is None:
                item["relative_path"] = ""
            item["caption_status"] = "pending"
            item["embedding_status"] = "pending"
            item["error"] = "图片不存在或内容哈希不匹配"
            item["updated_at"] = utc_now()
            images[str(entry_id)] = SemanticImage.from_dict(item).to_dict()
    result = dict(existing)
    result["schema_version"] = SCHEMA_VERSION
    result["pack_id"] = root.name
    result["images"] = images
    result["file_total"] = (
        sum(
            1
            for path in (root / "memes").rglob("*")
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
        )
        if (root / "memes").is_dir()
        else 0
    )
    result["unique_total"] = len(scanned_by_id)
    result["content_unique_total"] = len(
        {scan["content_sha256"] for scan in scanned_by_id.values()}
    )
    result["reused_duplicate_files"] = max(
        0,
        result["file_total"]
        - len(
            {
                (scan["content_sha256"], scan["category"])
                for scan in scanned_by_id.values()
            }
        ),
    )
    result["cross_category_duplicate_entries"] = max(
        0,
        len(
            {
                (scan["content_sha256"], scan["category"])
                for scan in scanned_by_id.values()
            }
        )
        - result["content_unique_total"],
    )
    result["requires_local_index_rebuild"] = bool(
        set(local_images) != set(images)
        or any(
            item.get("embedding_status") != "done"
            for entry_id, item in images.items()
            if entry_id in scanned_by_id and isinstance(item, dict)
        )
    )
    return result


def import_metadata_file(path: Path | str) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError("语义元数据文件不存在")
    data = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or not isinstance(data.get("images", {}), dict):
        raise ValueError("语义元数据格式无效")
    return data


def metadata_items(
    pack_dir: Path | str, status: str | None = None
) -> list[dict[str, Any]]:
    data = load_metadata(pack_dir)
    items = list(data.get("images", {}).values())
    if status:
        status = str(status).strip().lower()
        predicates = {
            "all": lambda item: True,
            "pending": lambda item: item.get("caption_status") == "pending"
            or item.get("embedding_status") == "pending",
            "running": lambda item: item.get("caption_status") == "running"
            or item.get("embedding_status") == "running",
            "failed": lambda item: item.get("caption_status") == "failed"
            or item.get("embedding_status") == "failed",
            "caption_failed": lambda item: item.get("caption_status") == "failed",
            "embedding_failed": lambda item: item.get("embedding_status") == "failed",
            "completed": lambda item: item.get("caption_status") == "done"
            and item.get("embedding_status") == "done",
            "caption_done": lambda item: item.get("caption_status") == "done",
            "embedding_done": lambda item: item.get("embedding_status") == "done",
            "reclassified": lambda item: bool(item.get("reclassification_status")),
        }
        predicate = predicates.get(status)
        if predicate is None:
            items = [
                item
                for item in items
                if item.get("caption_status") == status
                or item.get("embedding_status") == status
            ]
        else:
            items = [item for item in items if predicate(item)]

    def item_sort_key(item: dict[str, Any]) -> tuple[int, str, str]:
        statuses = {
            str(item.get("caption_status") or ""),
            str(item.get("embedding_status") or ""),
        }
        # 进行中的项目始终排在第一页，随后是待处理/失败，最后才是已完成项目。
        if "running" in statuses:
            priority = 0
        elif "pending" in statuses:
            priority = 1
        elif "failed" in statuses:
            priority = 2
        elif statuses == {"done"}:
            priority = 3
        else:
            priority = 4
        return (
            priority,
            str(item.get("updated_at") or ""),
            str(item.get("relative_path") or ""),
        )

    return sorted(items, key=item_sort_key)


def get_category_review_overview(pack_dir: Path | str) -> dict[str, Any]:
    """返回主页审核筛选所需的逐文件状态与统计。"""
    root = Path(pack_dir).resolve()
    empty_statistics = {
        "auto_match": 0,
        "needs_review": 0,
        "manual_confirmed": 0,
        "manual_rejected": 0,
        "unchecked": 0,
        "total": 0,
        "reclassified": 0,
    }
    semantic_status = str(
        get_pack_semantic_summary(root).get("semantic_status") or "none"
    )
    if semantic_status == "none":
        # 未开始语义化的普通图包、以及已废弃的旧版空元数据都不属于分类审核
        # 范围。这里提前返回还能避免为整包图片计算哈希后伪造“尚未检查”。
        return {
            "available": False,
            "semantic_status": "none",
            "items": [],
            "statistics": empty_statistics,
        }

    # 主页读取必须保持只读；后台语义任务可能正持有同一份元数据，GET 接口
    # 如果在此 reconcile + save 会用旧快照覆盖刚完成的模型结果。
    metadata = load_metadata(root) if metadata_path(root).is_file() else {"images": {}}
    images = metadata.get("images", {})
    items: list[dict[str, Any]] = []
    memes_root = root / "memes"
    if memes_root.is_dir():
        for path in sorted(memes_root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            try:
                path.resolve().relative_to(memes_root.resolve())
            except ValueError:
                continue
            digest = file_sha256(path)
            category = path.parent.name
            relative_path = path.relative_to(root).as_posix()
            entry_id = semantic_entry_id(digest, category, relative_path)
            record = images.get(entry_id, {})
            status = str(record.get("category_review_status") or "unchecked")
            items.append(
                {
                    "entry_id": entry_id,
                    "relative_path": relative_path,
                    "category": category,
                    "filename": path.name,
                    "category_tag": str(
                        record.get("category_tag") or build_category_tag(category)
                    ),
                    "category_review_status": status,
                    "category_review_reason": str(
                        record.get("category_review_reason") or ""
                    ),
                    "reclassification_status": str(
                        record.get("reclassification_status") or ""
                    ),
                    "reclassified_from_category": str(
                        record.get("reclassified_from_category") or ""
                    ),
                    "reclassified_to_category": str(
                        record.get("reclassified_to_category") or ""
                    ),
                    "reclassification_reason": str(
                        record.get("reclassification_reason") or ""
                    ),
                    "reclassified_at": str(record.get("reclassified_at") or ""),
                    "caption_status": str(record.get("caption_status") or "pending"),
                }
            )
    statistics = {
        status: sum(1 for item in items if item.get("category_review_status") == status)
        for status in (
            "auto_match",
            "needs_review",
            "manual_confirmed",
            "manual_rejected",
            "unchecked",
        )
    }
    statistics["total"] = len(items)
    statistics["reclassified"] = sum(
        1 for item in items if item.get("reclassification_status")
    )
    return {
        "available": bool(items),
        "semantic_status": semantic_status,
        "items": items,
        "statistics": statistics,
    }


def confirm_image_category(
    pack_dir: Path | str,
    image_path: Path | str,
    *,
    expected_content_sha256: str = "",
    expected_entry_id: str = "",
) -> dict[str, Any]:
    """由用户确认当前分类，并把确认绑定到当前图片和分类描述。"""
    root, source, digest, entry_id, metadata, raw_item = _resolve_image_edit_snapshot(
        pack_dir,
        image_path,
        expected_content_sha256=expected_content_sha256,
        expected_entry_id=expected_entry_id,
    )
    item = SemanticImage.from_dict(raw_item)
    item.category_review_status = "manual_confirmed"
    item.category_fit = "match"
    item.category_review_reason = ""
    item.category_review_context_hash = item.category_context_hash
    item.manual_confirmation_context_hash = item.category_context_hash
    item.updated_at = utc_now()
    _assert_image_snapshot_unchanged(root, source, digest, entry_id)
    raw_item.update(item.to_dict())
    save_metadata(root, metadata)
    return get_image_semantic_detail(root, source)


def invalidate_semantic_metadata(pack_dir: Path | str) -> dict[str, Any]:
    """文件或分类变更后立即刷新元数据，使旧标签、确认和向量失效。"""
    metadata = reconcile_metadata(pack_dir)
    save_metadata(pack_dir, metadata)
    return metadata
