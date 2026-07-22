"""使用 AstrBot 核心 EmbeddingProvider 和真实 FAISS 的本地语义索引。"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .semantic_models import (
    SemanticImage,
    build_id_map,
    normalize_vector,
    text_hash,
    utc_now,
)
from .semantic_storage import load_metadata, save_metadata

INDEX_FORMAT = "faiss-indexflatip-v1"
QUERY_CACHE_SIZE = 128
INDEX_CACHE_SIZE = 16
_QUERY_VECTOR_CACHE: OrderedDict[tuple[str, str], tuple[float, ...]] = OrderedDict()
_FAISS_INDEX_CACHE: OrderedDict[str, tuple[tuple[int, int], Any]] = OrderedDict()


def _import_faiss_modules():
    try:
        import faiss
        import numpy as np
    except ModuleNotFoundError as exc:
        raise RuntimeError("AstrBot 当前环境缺少 faiss-cpu，无法建立语义索引") from exc
    return faiss, np


def faiss_is_available() -> bool:
    try:
        _import_faiss_modules()
        return True
    except RuntimeError:
        return False


class EmbeddingAdapter:
    """为 AstrBot 核心 EmbeddingProvider 提供统一调用和小型查询缓存。"""

    def __init__(self, provider: Any, provider_id: str = ""):
        self.provider = provider
        self.provider_id = provider_id or self._read_provider_id(provider)
        self.model_name = self._read_model_name(provider)

    @staticmethod
    def _read_provider_id(provider: Any) -> str:
        meta = getattr(provider, "meta", None)
        if callable(meta):
            try:
                value = getattr(meta(), "id", "")
                if value:
                    return str(value)
            except Exception:
                pass
        config = getattr(provider, "provider_config", None)
        if isinstance(config, dict) and config.get("id"):
            return str(config["id"])
        return ""

    @staticmethod
    def _read_model_name(provider: Any) -> str:
        method = getattr(provider, "get_model", None)
        if callable(method):
            try:
                value = str(method() or "")
                if value:
                    return value
            except Exception:
                pass
        for attr in ("model_name", "model", "embed_model"):
            value = getattr(provider, attr, None)
            if value:
                return str(value)
        config = getattr(provider, "provider_config", None)
        if isinstance(config, dict):
            for key in ("embedding_model", "model", "model_name"):
                if config.get(key):
                    return str(config[key])
        return ""

    @property
    def dimension(self) -> int:
        method = getattr(self.provider, "get_dim", None)
        if not callable(method):
            return 0
        try:
            return int(method() or 0)
        except Exception:
            return 0

    @property
    def ready(self) -> bool:
        return bool(
            self.provider is not None
            and self.provider_id
            and self.dimension > 0
            and callable(getattr(self.provider, "get_embedding", None))
        )

    @property
    def signature(self) -> str:
        return f"{self.provider_id}:{self.model_name}:{self.dimension}"

    async def embed(self, text: str, *, use_cache: bool = True) -> list[float]:
        if not self.ready:
            raise RuntimeError("未配置 AstrBot 核心向量模型")
        normalized_text = str(text or "").strip()
        cache_key = (self.signature, normalized_text)
        if use_cache and cache_key in _QUERY_VECTOR_CACHE:
            vector = _QUERY_VECTOR_CACHE.pop(cache_key)
            _QUERY_VECTOR_CACHE[cache_key] = vector
            return list(vector)

        result = self.provider.get_embedding(normalized_text)
        if hasattr(result, "__await__"):
            result = await result
        vector = normalize_vector(result)
        if len(vector) != self.dimension:
            raise RuntimeError(
                f"向量模型返回维度不一致：期望 {self.dimension}，实际 {len(vector)}"
            )
        if use_cache:
            _QUERY_VECTOR_CACHE[cache_key] = tuple(vector)
            while len(_QUERY_VECTOR_CACHE) > QUERY_CACHE_SIZE:
                _QUERY_VECTOR_CACHE.popitem(last=False)
        return vector

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        if not self.ready:
            raise RuntimeError("未配置 AstrBot 核心向量模型")
        method = getattr(self.provider, "get_embeddings", None)
        if callable(method):
            result = method(texts)
            if hasattr(result, "__await__"):
                result = await result
            if not isinstance(result, (list, tuple)):
                raise RuntimeError("向量模型返回格式无效")
            vectors = [normalize_vector(item) for item in result]
            if any(len(vector) != self.dimension for vector in vectors):
                raise RuntimeError("向量模型返回维度不一致")
            return vectors
        return [await self.embed(text, use_cache=False) for text in texts]


def index_dir(plugin_data_dir: Path | str, pack_id: str) -> Path:
    value = str(pack_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{2,64}", value):
        raise ValueError("pack_id 无效")
    return Path(plugin_data_dir).resolve() / "semantic_indexes" / value


def index_manifest_path(plugin_data_dir: Path | str, pack_id: str) -> Path:
    return index_dir(plugin_data_dir, pack_id) / "index_manifest.json"


def _index_file(plugin_data_dir: Path | str, pack_id: str) -> Path:
    return index_dir(plugin_data_dir, pack_id) / "index.faiss"


def load_index_manifest(plugin_data_dir: Path | str, pack_id: str) -> dict[str, Any]:
    path = index_manifest_path(plugin_data_dir, pack_id)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _read_faiss_index(plugin_data_dir: Path | str, pack_id: str):
    path = _index_file(plugin_data_dir, pack_id)
    if not path.is_file():
        return None
    try:
        stat = path.stat()
        cache_key = str(path)
        fingerprint = (stat.st_mtime_ns, stat.st_size)
        cached = _FAISS_INDEX_CACHE.get(cache_key)
        if cached and cached[0] == fingerprint:
            _FAISS_INDEX_CACHE.move_to_end(cache_key)
            return cached[1]
        faiss, _ = _import_faiss_modules()
        index = faiss.read_index(str(path))
        _FAISS_INDEX_CACHE[cache_key] = (fingerprint, index)
        while len(_FAISS_INDEX_CACHE) > INDEX_CACHE_SIZE:
            _FAISS_INDEX_CACHE.popitem(last=False)
        return index
    except Exception:
        return None


def _write_faiss_index(plugin_data_dir: Path | str, pack_id: str, index: Any) -> None:
    faiss, _ = _import_faiss_modules()
    path = _index_file(plugin_data_dir, pack_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".index.", suffix=".faiss", dir=path.parent)
    os.close(fd)
    try:
        faiss.write_index(index, temp_name)
        os.replace(temp_name, path)
        stat = path.stat()
        cache_key = str(path)
        _FAISS_INDEX_CACHE[cache_key] = ((stat.st_mtime_ns, stat.st_size), index)
        _FAISS_INDEX_CACHE.move_to_end(cache_key)
        while len(_FAISS_INDEX_CACHE) > INDEX_CACHE_SIZE:
            _FAISS_INDEX_CACHE.popitem(last=False)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _write_manifest(
    plugin_data_dir: Path | str, pack_id: str, manifest: dict[str, Any]
) -> None:
    path = index_manifest_path(plugin_data_dir, pack_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=".manifest.", suffix=".json", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file_obj:
            json.dump(manifest, file_obj, ensure_ascii=False, indent=2)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def index_is_ready(
    plugin_data_dir: Path | str,
    pack_id: str,
    metadata: dict[str, Any] | None = None,
    embedding_provider_id: str | None = None,
    embedding_model: str | None = None,
    embedding_dimension: int | None = None,
) -> bool:
    manifest = load_index_manifest(plugin_data_dir, pack_id)
    if manifest.get("index_format") != INDEX_FORMAT:
        return False
    item_count = int(manifest.get("item_count", 0) or 0)
    if item_count <= 0:
        return False
    if embedding_provider_id and str(
        manifest.get("embedding_provider_id") or ""
    ) != str(embedding_provider_id):
        return False
    if embedding_model is not None and str(
        manifest.get("embedding_model") or ""
    ) != str(embedding_model):
        return False
    if embedding_dimension and int(manifest.get("embedding_dimension", 0) or 0) != int(
        embedding_dimension
    ):
        return False

    index = _read_faiss_index(plugin_data_dir, pack_id)
    if index is None or int(index.ntotal) != item_count:
        return False
    if int(index.d) != int(manifest.get("embedding_dimension", 0) or 0):
        return False

    current = metadata or {}
    images = current.get("images", {})
    manifest_items = manifest.get("items", {})
    if not isinstance(manifest_items, dict) or len(manifest_items) != item_count:
        return False
    indexable = {
        digest: item
        for digest, item in images.items()
        if isinstance(item, dict)
        and item.get("caption_status") == "done"
        and item.get("embedding_status") == "done"
        and item.get("caption")
        and item.get("tags")
        and item.get("text_hash")
    }
    if not indexable or len(indexable) != item_count:
        return False
    for digest, item in indexable.items():
        current_hash = text_hash(SemanticImage.from_dict(item).vector_text)
        if str(item.get("text_hash") or "") != current_hash:
            return False
        if str(manifest_items.get(digest, {}).get("text_hash") or "") != current_hash:
            return False
    return True


def _reconstruct_reusable_vectors(
    old_index: Any,
    old_manifest: dict[str, Any],
    candidates: list[tuple[str, dict[str, Any]]],
) -> dict[str, list[float]]:
    if old_index is None or old_manifest.get("index_format") != INDEX_FORMAT:
        return {}
    old_items = old_manifest.get("items", {})
    if not isinstance(old_items, dict):
        return {}
    vectors: dict[str, list[float]] = {}
    for digest, item in candidates:
        old_item = old_items.get(digest)
        if not isinstance(old_item, dict):
            continue
        if str(old_item.get("text_hash") or "") != str(item.get("text_hash") or ""):
            continue
        try:
            vector = old_index.reconstruct(int(old_item["faiss_id"]))
            vectors[digest] = normalize_vector(vector.tolist())
        except Exception:
            continue
    return vectors


async def build_index(
    pack_dir: Path | str,
    plugin_data_dir: Path | str,
    pack_id: str,
    embedding: EmbeddingAdapter,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """建立真实 FAISS 精确索引，只重新向量化新增或语义变化的图片。"""
    if not embedding.ready:
        raise RuntimeError("未配置 AstrBot 核心向量模型")
    faiss, np = _import_faiss_modules()
    metadata = load_metadata(pack_dir)
    candidates: list[tuple[str, dict[str, Any]]] = []
    for digest, value in metadata.get("images", {}).items():
        if not isinstance(value, dict) or value.get("caption_status") != "done":
            continue
        if not value.get("caption") or not value.get("tags"):
            continue
        current_hash = text_hash(SemanticImage.from_dict(value).vector_text)
        value["text_hash"] = current_hash
        candidates.append((str(digest), value))
    if not candidates:
        raise RuntimeError("没有已完成的语义描述，无法建立索引")

    old_manifest = load_index_manifest(plugin_data_dir, pack_id)
    same_provider = bool(
        old_manifest.get("index_format") == INDEX_FORMAT
        and old_manifest.get("embedding_provider_id") == embedding.provider_id
        and old_manifest.get("embedding_model", "") == embedding.model_name
        and int(old_manifest.get("embedding_dimension", 0) or 0) == embedding.dimension
    )
    old_index = (
        _read_faiss_index(plugin_data_dir, pack_id)
        if same_provider and not force
        else None
    )
    vectors = (
        _reconstruct_reusable_vectors(old_index, old_manifest, candidates)
        if old_index is not None
        else {}
    )

    pending = [(digest, item) for digest, item in candidates if digest not in vectors]
    for _, item in pending:
        item["embedding_status"] = "running"
    for digest, item in candidates:
        if digest in vectors:
            item["embedding_status"] = "done"
            item["error"] = None
    save_metadata(pack_dir, metadata)

    if pending:
        try:
            generated = await embedding.embed_many(
                [SemanticImage.from_dict(item).vector_text for _, item in pending]
            )
            if len(generated) != len(pending):
                raise RuntimeError("向量模型返回数量与输入不一致")
            for (digest, item), vector in zip(pending, generated):
                vectors[digest] = vector
                item["embedding_status"] = "done"
                item["error"] = None
                item["updated_at"] = utc_now()
        except Exception:
            for digest, item in pending:
                try:
                    vectors[digest] = await embedding.embed(
                        SemanticImage.from_dict(item).vector_text,
                        use_cache=False,
                    )
                    item["embedding_status"] = "done"
                    item["error"] = None
                except Exception:
                    item["embedding_status"] = "failed"
                    item["error"] = "向量生成失败"
                item["updated_at"] = utc_now()
    save_metadata(pack_dir, metadata)

    successful = [
        (digest, item)
        for digest, item in sorted(candidates)
        if digest in vectors and item.get("embedding_status") == "done"
    ]
    base_index = faiss.IndexFlatIP(embedding.dimension)
    index = faiss.IndexIDMap2(base_index)
    manifest_items: dict[str, dict[str, Any]] = {}
    if successful:
        matrix = np.asarray(
            [vectors[digest] for digest, _ in successful], dtype="float32"
        )
        faiss.normalize_L2(matrix)
        ids = np.arange(1, len(successful) + 1, dtype="int64")
        index.add_with_ids(matrix, ids)
        for faiss_id, (digest, item) in zip(ids.tolist(), successful):
            manifest_items[digest] = {
                "faiss_id": faiss_id,
                "text_hash": str(item.get("text_hash") or ""),
            }

    _write_faiss_index(plugin_data_dir, pack_id, index)
    manifest = {
        "pack_id": pack_id,
        "metadata_schema_version": "1.0",
        "index_format": INDEX_FORMAT,
        "embedding_provider_id": embedding.provider_id,
        "embedding_model": embedding.model_name,
        "embedding_dimension": embedding.dimension,
        "distance": "cosine",
        "item_count": len(successful),
        "items": manifest_items,
        "built_at": utc_now(),
    }
    _write_manifest(plugin_data_dir, pack_id, manifest)
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
    if manifest.get("index_format") != INDEX_FORMAT:
        return []
    if (
        manifest.get("embedding_provider_id") != embedding.provider_id
        or manifest.get("embedding_model", "") != embedding.model_name
        or int(manifest.get("embedding_dimension", 0) or 0) != embedding.dimension
    ):
        return []
    index = _read_faiss_index(plugin_data_dir, pack_id)
    if index is None or int(index.ntotal) <= 0:
        return []
    faiss, np = _import_faiss_modules()
    query_vector = np.asarray(
        [await embedding.embed(str(query or ""))], dtype="float32"
    )
    faiss.normalize_L2(query_vector)
    result_limit = min(max(0, int(top_k)), int(index.ntotal))
    if result_limit <= 0:
        return []
    scores, ids = index.search(query_vector, result_limit)
    id_to_digest = {
        int(item.get("faiss_id")): digest
        for digest, item in manifest.get("items", {}).items()
        if isinstance(item, dict) and item.get("faiss_id") is not None
    }
    data = metadata or {}
    ranked = []
    for score, faiss_id in zip(scores[0].tolist(), ids[0].tolist()):
        digest = id_to_digest.get(int(faiss_id))
        item = data.get("images", {}).get(digest) if digest else None
        if not isinstance(item, dict) or item.get("caption_status") != "done":
            continue
        if float(score) < float(min_score):
            continue
        ranked.append((float(score), digest, item))
    # ID 必须针对整个索引检查碰撞，不能只看当前 Top-K。
    id_map = build_id_map(manifest.get("items", {}).keys())
    return [
        {
            "id": id_map[digest],
            "content_sha256": digest,
            "caption": str(item.get("caption") or ""),
            "tags": item.get("tags") or [],
            "score": score,
        }
        for score, digest, item in ranked
    ]
