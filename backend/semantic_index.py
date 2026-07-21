"""每个资源包独立的本地语义索引。

优先使用 FAISS；服务器没有安装 FAISS 时使用同样的 `index.faiss` 文件保存 JSON
向量，检索行为保持一致，不会让旧模式因可选依赖缺失而启动失败。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .semantic_models import (
    SemanticImage,
    build_id_map,
    cosine_similarity,
    normalize_vector,
    text_hash,
    utc_now,
)
from .semantic_storage import load_metadata, save_metadata


class EmbeddingAdapter:
    """兼容同步/异步 EmbeddingProvider 与 embedding_adapter 插件。"""

    def __init__(self, provider: Any, provider_id: str = ""):
        self.provider = provider
        self.provider_id = provider_id or self._read_provider_id(provider)

    @staticmethod
    def _read_provider_id(provider: Any) -> str:
        for name in ("get_model_name", "get_provider_name", "get_model_id"):
            method = getattr(provider, name, None)
            if callable(method):
                try:
                    value = method()
                    if value:
                        return str(value)
                except Exception:
                    continue
        return provider.__class__.__name__ if provider is not None else ""

    @property
    def ready(self) -> bool:
        if self.provider is None:
            return False
        availability = getattr(self.provider, "is_available", None)
        if callable(availability):
            try:
                if availability() is False:
                    return False
            except Exception:
                return False
        return any(
            callable(getattr(self.provider, name, None))
            for name in (
                "get_embedding",
                "get_embedding_async",
                "get_embeddings",
                "get_embeddings_async",
            )
        )

    async def embed(self, text: str) -> list[float]:
        if not self.ready:
            raise RuntimeError("未配置向量模型")
        for name in ("get_embedding_async", "get_embedding"):
            method = getattr(self.provider, name, None)
            if not callable(method):
                continue
            result = method(text)
            if hasattr(result, "__await__"):
                result = await result
            return normalize_vector(result)
        raise RuntimeError("向量模型不支持单文本向量化")

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        if not self.ready:
            raise RuntimeError("未配置向量模型")
        for name in ("get_embeddings_async", "get_embeddings"):
            method = getattr(self.provider, name, None)
            if not callable(method):
                continue
            result = method(texts)
            if hasattr(result, "__await__"):
                result = await result
            if not isinstance(result, (list, tuple)):
                raise RuntimeError("向量模型返回格式无效")
            return [normalize_vector(item) for item in result]
        return [await self.embed(text) for text in texts]


def index_dir(plugin_data_dir: Path | str, pack_id: str) -> Path:
    value = str(pack_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{2,64}", value):
        raise ValueError("pack_id 无效")
    return Path(plugin_data_dir).resolve() / "semantic_indexes" / value


def index_manifest_path(plugin_data_dir: Path | str, pack_id: str) -> Path:
    return index_dir(plugin_data_dir, pack_id) / "index_manifest.json"


def _index_file(plugin_data_dir: Path | str, pack_id: str) -> Path:
    return index_dir(plugin_data_dir, pack_id) / "index.faiss"


def _load_vectors(plugin_data_dir: Path | str, pack_id: str) -> dict[str, list[float]]:
    path = _index_file(plugin_data_dir, pack_id)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        vectors = data.get("vectors", {}) if isinstance(data, dict) else {}
        return {
            str(key): normalize_vector(value)
            for key, value in vectors.items()
            if isinstance(value, list)
        }
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _save_vectors(
    plugin_data_dir: Path | str, pack_id: str, vectors: dict[str, list[float]]
) -> None:
    path = _index_file(plugin_data_dir, pack_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".index.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file_obj:
            json.dump(
                {"format": "meme-manager-json-faiss-fallback-v1", "vectors": vectors},
                file_obj,
                ensure_ascii=False,
            )
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def load_index_manifest(plugin_data_dir: Path | str, pack_id: str) -> dict[str, Any]:
    path = index_manifest_path(plugin_data_dir, pack_id)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def index_is_ready(
    plugin_data_dir: Path | str,
    pack_id: str,
    metadata: dict[str, Any] | None = None,
    embedding_provider_id: str | None = None,
) -> bool:
    manifest = load_index_manifest(plugin_data_dir, pack_id)
    if not manifest or not _index_file(plugin_data_dir, pack_id).is_file():
        return False
    if int(manifest.get("item_count", 0)) <= 0:
        return False
    if embedding_provider_id and str(
        manifest.get("embedding_provider_id") or ""
    ) != str(embedding_provider_id):
        return False
    current = metadata or {}
    done = {
        digest: item
        for digest, item in current.get("images", {}).items()
        if isinstance(item, dict)
        and item.get("embedding_status") == "done"
        and item.get("text_hash")
    }
    if any(
        isinstance(item, dict)
        and (
            item.get("caption_status") != "done"
            or item.get("embedding_status") != "done"
        )
        for item in current.get("images", {}).values()
    ):
        return False
    if (
        int(manifest.get("item_count", -1)) != len(done)
        or str(manifest.get("metadata_schema_version")) != "1.0"
    ):
        return False
    stored_hashes = manifest.get("text_hashes")
    if isinstance(stored_hashes, dict):
        return all(
            str(stored_hashes.get(digest) or "") == str(item.get("text_hash") or "")
            for digest, item in done.items()
        )
    return True


async def build_index(
    pack_dir: Path | str,
    plugin_data_dir: Path | str,
    pack_id: str,
    embedding: EmbeddingAdapter,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """为已完成视觉语义的记录建立索引，并回写每张图片的向量状态。"""
    old_manifest = load_index_manifest(plugin_data_dir, pack_id)
    if (
        old_manifest
        and old_manifest.get("embedding_provider_id")
        and str(old_manifest.get("embedding_provider_id")) != str(embedding.provider_id)
    ):
        force = True
    metadata = load_metadata(pack_dir)
    images = metadata.get("images", {})
    candidates: list[tuple[str, dict[str, Any]]] = []
    for digest, value in images.items():
        if not isinstance(value, dict) or value.get("caption_status") != "done":
            continue
        if not value.get("caption") or not value.get("tags"):
            continue
        semantic_text = SemanticImage.from_dict(value).vector_text
        current_hash = text_hash(semantic_text)
        if (
            force
            or value.get("text_hash") != current_hash
            or value.get("embedding_status") != "done"
        ):
            value["embedding_status"] = "running"
        value["text_hash"] = current_hash
        candidates.append((str(digest), value))
    save_metadata(pack_dir, metadata)
    vectors = {} if force else _load_vectors(plugin_data_dir, pack_id)
    allowed_digests = {digest for digest, _ in candidates}
    vectors = {
        digest: vector
        for digest, vector in vectors.items()
        if digest in allowed_digests
    }
    pending = [
        (digest, value)
        for digest, value in candidates
        if force or digest not in vectors or value.get("embedding_status") != "done"
    ]
    if pending:
        try:
            generated = await embedding.embed_many(
                [SemanticImage.from_dict(item).vector_text for _, item in pending]
            )
            if len(generated) != len(pending):
                raise RuntimeError("向量模型返回数量与输入不一致")
            for (digest, value), vector in zip(pending, generated):
                vectors[digest] = vector
                value["embedding_status"] = "done"
                value["error"] = None
                value["updated_at"] = utc_now()
        except Exception as batch_error:
            # 批量接口失败时逐张重试，确保单张失败不会抹掉其他已完成结果。
            for digest, value in pending:
                try:
                    vectors[digest] = await embedding.embed(
                        SemanticImage.from_dict(value).vector_text
                    )
                    value["embedding_status"] = "done"
                    value["error"] = None
                except Exception as item_error:
                    value["embedding_status"] = "failed"
                    value["error"] = str(item_error or batch_error)[:500]
                value["updated_at"] = utc_now()
    for digest, value in candidates:
        if digest in vectors:
            value["embedding_status"] = "done"
    save_metadata(pack_dir, metadata)
    dimensions = len(next(iter(vectors.values()))) if vectors else 0
    _save_vectors(plugin_data_dir, pack_id, vectors)
    manifest = {
        "pack_id": pack_id,
        "metadata_schema_version": "1.0",
        "embedding_provider_id": embedding.provider_id,
        "embedding_dimension": dimensions,
        "distance": "cosine",
        "item_count": len(vectors),
        "text_hashes": {
            digest: str(item.get("text_hash") or "")
            for digest, item in candidates
            if digest in vectors
        },
        "built_at": utc_now(),
    }
    manifest_path = index_manifest_path(plugin_data_dir, pack_id)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".manifest.", dir=manifest_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file_obj:
            json.dump(manifest, file_obj, ensure_ascii=False, indent=2)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temp_name, manifest_path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return manifest


async def search_index(
    plugin_data_dir: Path | str,
    pack_id: str,
    query: str,
    embedding: EmbeddingAdapter,
    metadata: dict[str, Any] | None = None,
    *,
    top_k: int = 5,
    min_score: float = 0.25,
) -> list[dict[str, Any]]:
    manifest = load_index_manifest(plugin_data_dir, pack_id)
    vectors = _load_vectors(plugin_data_dir, pack_id)
    if not manifest or not vectors:
        return []
    query_vector = await embedding.embed(str(query or ""))
    data = metadata or {}
    candidates = []
    for digest, vector in vectors.items():
        try:
            score = cosine_similarity(query_vector, vector)
        except ValueError:
            continue
        item = data.get("images", {}).get(digest)
        if not isinstance(item, dict) or item.get("caption_status") != "done":
            continue
        candidates.append((score, digest, item))
    candidates.sort(key=lambda row: (-row[0], row[1]))
    result = []
    id_map = build_id_map(digest for _, digest, _ in candidates)
    for score, digest, item in candidates[: max(0, int(top_k))]:
        if score < float(min_score):
            continue
        meme_id = id_map[digest]
        result.append(
            {
                "id": meme_id,
                "content_sha256": digest,
                "caption": str(item.get("caption") or ""),
                "tags": item.get("tags") or [],
                "score": score,
            }
        )
    return result
