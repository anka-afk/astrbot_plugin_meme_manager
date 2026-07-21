"""运行时语义查询和候选 ID 校验。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .semantic_index import EmbeddingAdapter, search_index
from .semantic_models import parse_meme_id
from .semantic_storage import file_sha256, load_metadata, safe_relative_path


async def search_memes(
    pack_dir: Path | str,
    plugin_data_dir: Path | str,
    pack_id: str,
    query: str,
    embedding_provider: Any,
    *,
    top_k: int = 5,
    min_score: float = 0.25,
) -> dict[str, Any]:
    if not str(query or "").strip():
        return {"ok": True, "candidates": [], "reason": "查询词不能为空"}
    metadata = load_metadata(pack_dir)
    if not metadata.get("images"):
        return {"ok": True, "candidates": [], "reason": "资源包没有语义元数据"}
    candidates = await search_index(
        plugin_data_dir,
        pack_id,
        query,
        EmbeddingAdapter(embedding_provider),
        metadata,
        top_k=top_k,
        min_score=min_score,
    )
    for item in candidates:
        item.pop("content_sha256", None)
        item.pop("score", None)
    if not candidates:
        return {"ok": True, "candidates": [], "reason": "没有找到足够匹配的表情包"}
    return {"ok": True, "candidates": candidates, "max_selectable": 1}


def candidate_records(
    pack_dir: Path | str, candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """为事件上下文补回完整哈希，但不把它暴露给 LLM。"""
    metadata = load_metadata(pack_dir)
    records = []
    for candidate in candidates:
        value = str(candidate.get("id") or "")
        prefix = parse_meme_id(value)
        if not prefix:
            continue
        matches = [
            (digest, item)
            for digest, item in metadata.get("images", {}).items()
            if str(digest).startswith(prefix)
        ]
        if len(matches) != 1:
            continue
        digest, item = matches[0]
        records.append(
            {
                **candidate,
                "content_sha256": digest,
                "caption": item.get("caption", ""),
                "tags": item.get("tags", []),
            }
        )
    return records


def remember_candidates(event: Any, candidates: list[dict[str, Any]]) -> None:
    if hasattr(event, "set_extra"):
        existing = (
            event.get_extra("meme_manager_semantic_candidates")
            if hasattr(event, "get_extra")
            else None
        )
        candidate_map = dict(existing) if isinstance(existing, dict) else {}
        candidate_map.update(
            {str(item.get("id")): item for item in candidates if item.get("id")}
        )
        event.set_extra(
            "meme_manager_semantic_candidates",
            candidate_map,
        )


def validate_selected_id(event: Any, value: str, pack_dir: Path | str) -> Path | None:
    prefix = parse_meme_id(value)
    if not prefix:
        return None
    candidate_map = (
        event.get_extra("meme_manager_semantic_candidates")
        if hasattr(event, "get_extra")
        else None
    )
    candidate = (
        candidate_map.get(str(value).strip())
        if isinstance(candidate_map, dict)
        else None
    )
    if not isinstance(candidate, dict):
        return None
    digest = str(candidate.get("content_sha256") or "")
    if not digest.startswith(prefix):
        return None
    metadata = load_metadata(pack_dir)
    record = metadata.get("images", {}).get(digest)
    if not isinstance(record, dict):
        return None
    path = safe_relative_path(pack_dir, record.get("relative_path", ""))
    if path is None or not path.is_file():
        return None
    try:
        if file_sha256(path) != digest:
            return None
    except OSError:
        return None
    return path


def dumps_result(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False)
