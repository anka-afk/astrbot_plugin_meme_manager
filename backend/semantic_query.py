"""运行时语义查询和候选 ID 校验。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .semantic_index import EmbeddingAdapter, search_index
from .semantic_models import REVIEW_CATEGORY, parse_meme_id
from .semantic_storage import (
    file_sha256,
    load_metadata,
    safe_relative_path,
    semantic_metadata_is_complete,
)


async def search_memes(
    pack_dir: Path | str,
    plugin_data_dir: Path | str,
    pack_id: str,
    query: str,
    embedding_provider: Any,
    *,
    top_k: int = 5,
    min_score: float = 0.25,
    _verified_complete: bool = False,
) -> dict[str, Any]:
    """Search a fully semanticized meme pack.

    Args:
        pack_dir: Root directory of the selected meme pack.
        plugin_data_dir: Plugin data directory containing semantic indexes.
        pack_id: Selected meme pack identifier.
        query: Semantic query text.
        embedding_provider: Embedding provider used by the pack index.
        top_k: Maximum number of candidates to return.
        min_score: Minimum cosine similarity for returned candidates.
        _verified_complete: Internal request-scoped proof that the same pack was
            already checked before prompt injection.

    Returns:
        Search result with validated public candidate identifiers.
    """
    if not str(query or "").strip():
        return {"ok": True, "candidates": [], "reason": "查询词不能为空"}
    metadata = load_metadata(pack_dir)
    if not metadata.get("images"):
        return {"ok": True, "candidates": [], "reason": "资源包没有语义元数据"}
    if not _verified_complete and not semantic_metadata_is_complete(
        pack_dir, metadata, require_embeddings=True
    ):
        return {
            "ok": True,
            "candidates": [],
            "reason": "资源包尚未完成100%语义化，不能作为语义检索目标",
        }
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
        item.pop("entry_id", None)
        item.pop("score", None)
    if not candidates:
        return {"ok": True, "candidates": [], "reason": "没有找到足够匹配的表情包"}
    return {"ok": True, "candidates": candidates, "max_selectable": 1}


def candidate_records(
    pack_dir: Path | str, candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """为事件上下文补回完整语义键和内容哈希，但不把它们暴露给 LLM。"""
    metadata = load_metadata(pack_dir)
    records = []
    for candidate in candidates:
        value = str(candidate.get("id") or "")
        prefix = parse_meme_id(value)
        if not prefix:
            continue
        matches = [
            (entry_id, item)
            for entry_id, item in metadata.get("images", {}).items()
            if str(entry_id).startswith(prefix)
        ]
        if len(matches) != 1:
            continue
        entry_id, item = matches[0]
        records.append(
            {
                **candidate,
                "entry_id": entry_id,
                "content_sha256": str(item.get("content_sha256") or ""),
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
    entry_id = str(candidate.get("entry_id") or "")
    if not entry_id.startswith(prefix):
        return None
    metadata = load_metadata(pack_dir)
    record = metadata.get("images", {}).get(entry_id)
    if not isinstance(record, dict):
        return None
    if str(record.get("category") or "") == REVIEW_CATEGORY:
        return None
    path = safe_relative_path(pack_dir, record.get("relative_path", ""))
    if path is None or not path.is_file():
        return None
    try:
        if file_sha256(path) != str(record.get("content_sha256") or ""):
            return None
    except OSError:
        return None
    return path


def dumps_result(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False)
