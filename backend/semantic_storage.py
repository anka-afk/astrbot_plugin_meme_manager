"""语义元数据的扫描、校验和原子保存。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .semantic_models import (
    IMAGE_EXTENSIONS,
    SCHEMA_VERSION,
    build_semantic_text,
    text_hash,
    utc_now,
)


def metadata_path(pack_dir: Path | str) -> Path:
    return Path(pack_dir).resolve() / "semantic_metadata.json"


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


def scan_images(pack_dir: Path | str) -> list[dict[str, str]]:
    """扫描图片并按 SHA-256 去重，保留每个哈希遇到的第一条路径。"""
    root = Path(pack_dir).resolve()
    memes_root = root / "memes"
    if not memes_root.is_dir():
        return []
    found: dict[str, dict[str, str]] = {}
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
        found.setdefault(
            digest,
            {"content_sha256": digest, "relative_path": relative, "category": category},
        )
    return list(found.values())


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
    images = data.get("images")
    if not isinstance(images, dict):
        images = {}
    normalized: dict[str, dict[str, Any]] = {}
    for key, value in images.items():
        if not isinstance(value, dict):
            continue
        digest = str(value.get("content_sha256") or key).lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            continue
        value = dict(value)
        value["content_sha256"] = digest
        normalized[digest] = value
    data["schema_version"] = str(data.get("schema_version") or SCHEMA_VERSION)
    data["pack_id"] = str(data.get("pack_id") or Path(pack_dir).name)
    data.setdefault("generated_at", utc_now())
    data["images"] = normalized
    return data


def save_metadata(pack_dir: Path | str, data: dict[str, Any]) -> Path:
    """使用同目录临时文件 + replace，避免断电留下半份 JSON。"""
    target = metadata_path(pack_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(data or {})
    payload["schema_version"] = str(payload.get("schema_version") or SCHEMA_VERSION)
    payload["pack_id"] = str(payload.get("pack_id") or target.parent.name)
    payload.setdefault("generated_at", utc_now())
    if not isinstance(payload.get("images"), dict):
        payload["images"] = {}
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file_obj:
            json.dump(payload, file_obj, ensure_ascii=False, indent=2)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return target


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
        for digest, value in images.items():
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
            normalized_images[str(digest)] = item
    payload["images"] = normalized_images
    payload["requires_local_index_rebuild"] = True
    return payload


def reconcile_metadata(
    pack_dir: Path | str, external_data: dict[str, Any] | None = None
) -> dict[str, Any]:
    """将磁盘扫描结果与现有/外部语义记录合并，并校验路径和哈希。"""
    root = Path(pack_dir).resolve()
    existing = load_metadata(root)
    external_images = (
        (external_data or {}).get("images", {})
        if isinstance(external_data, dict)
        else {}
    )
    if not isinstance(external_images, dict):
        external_images = {}
    normalized_external: dict[str, dict[str, Any]] = {}
    for key, value in external_images.items():
        if not isinstance(value, dict):
            continue
        digest = str(value.get("content_sha256") or key).lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            continue
        normalized_external[digest] = dict(value)
    external_images = normalized_external
    images: dict[str, dict[str, Any]] = {}
    scanned = scan_images(root)
    scanned_by_hash = {item["content_sha256"]: item for item in scanned}
    for digest, scan in scanned_by_hash.items():
        local_item = existing.get("images", {}).get(digest) or {}
        external_item = external_images.get(digest) or {}
        local_is_reusable = bool(
            isinstance(local_item, dict)
            and local_item.get("caption_status") == "done"
            and local_item.get("caption")
            and local_item.get("tags")
        )
        external_is_reusable = bool(
            isinstance(external_item, dict)
            and external_item.get("caption")
            and external_item.get("tags")
            and external_item.get("caption_status") in {None, "done"}
        )
        if (
            isinstance(local_item, dict)
            and local_item.get("provenance") in {"manual", "mixed"}
        ) or local_is_reusable:
            previous = local_item
        elif external_is_reusable:
            previous = external_item
        else:
            previous = local_item or external_item
        if not isinstance(previous, dict):
            previous = {}
        item = dict(previous)
        item["content_sha256"] = digest
        item["relative_path"] = scan["relative_path"]
        item["category"] = scan["category"]
        item.setdefault("caption", "")
        item.setdefault("tags", [])
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
        )
        calculated_hash = text_hash(current_text)
        if item.get("text_hash") and item["text_hash"] != calculated_hash:
            item["embedding_status"] = "pending"
        item["text_hash"] = calculated_hash if item.get("caption") else ""
        images[digest] = item
    # 保留导入文件中当前不存在/哈希不匹配的记录，但明确标成待处理，方便用户看到并重试。
    for source in (existing.get("images", {}), external_images):
        for digest, value in source.items():
            if (
                digest in images
                or not isinstance(value, dict)
                or len(str(digest)) != 64
                or any(char not in "0123456789abcdef" for char in str(digest).lower())
            ):
                continue
            item = dict(value)
            item["content_sha256"] = str(digest).lower()
            if safe_relative_path(root, item.get("relative_path", "")) is None:
                item["relative_path"] = ""
            item["caption_status"] = "pending"
            item["embedding_status"] = "pending"
            item["error"] = "图片不存在或内容哈希不匹配"
            item["updated_at"] = utc_now()
            images[item["content_sha256"]] = item
    result = dict(existing)
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
    result["unique_total"] = len(scanned_by_hash)
    result["reused_duplicate_files"] = max(
        0, result["file_total"] - len(scanned_by_hash)
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
        }
        predicate = predicates.get(status)
        if predicate is None:
            predicate = lambda item: item.get("caption_status") == status or item.get("embedding_status") == status
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
