"""语义表情包的数据模型与纯函数。

这些对象不依赖 AstrBot，方便在没有启动主程序时做单元测试，也让语义文件格式
成为一个稳定的本地接口。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "1.0"
PROMPT_VERSION = "meme-semantic-v1"
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif"})
CAPTION_STATUSES = frozenset({"pending", "running", "done", "failed"})
EMBEDDING_STATUSES = frozenset({"pending", "running", "done", "failed"})
TASK_STATUSES = frozenset(
    {"idle", "running", "paused", "completed", "completed_with_errors", "failed"}
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_tags(tags: Any) -> list[str]:
    if isinstance(tags, str):
        tags = [tags]
    if not isinstance(tags, (list, tuple, set)):
        return []
    result = []
    seen = set()
    for item in tags:
        value = str(item or "").strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def build_semantic_text(
    caption: str, tags: Iterable[str], visible_text: str, category: str = ""
) -> str:
    """按文档约定生成向量模型使用的标准文本。"""
    parts = [
        f"图片含义：{str(caption or '').strip()}",
        f"标签：{'、'.join(normalize_tags(tags))}",
        f"图片文字：{str(visible_text or '').strip()}",
    ]
    if category:
        parts.append(f"分类：{str(category).strip()}")
    return "\n".join(parts)


def text_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def normalize_vector(vector: Any) -> list[float]:
    if not isinstance(vector, (list, tuple)):
        raise ValueError("向量必须是数组")
    values = [float(value) for value in vector]
    norm = math.sqrt(sum(value * value for value in values))
    if not values or norm == 0:
        raise ValueError("向量不能为空且不能是全零向量")
    return [value / norm for value in values]


def cosine_similarity(left: Iterable[float], right: Iterable[float]) -> float:
    left_values = list(left)
    right_values = list(right)
    if len(left_values) != len(right_values):
        raise ValueError("向量维度不一致")
    return sum(a * b for a, b in zip(left_values, right_values))


def short_id(content_sha256: str, used_prefixes: Iterable[str] = ()) -> str:
    """生成 12 位起步、发生碰撞时自动扩展的模型可见 ID。"""
    digest = str(content_sha256 or "").lower()
    if len(digest) != 64:
        raise ValueError("content_sha256 必须是完整 SHA-256")
    used = {str(item) for item in used_prefixes}
    for length in (12, 16, 20, 24, 32, 64):
        prefix = digest[:length]
        if prefix not in used:
            return f"meme:{prefix}"
    return f"meme:{digest}"


def build_id_map(digests: Iterable[str]) -> dict[str, str]:
    """为一批完整哈希生成互不歧义的最短前缀。"""
    normalized = sorted({str(digest).lower() for digest in digests})
    result = {}
    for digest in normalized:
        for length in (12, 16, 20, 24, 32, 64):
            prefix = digest[:length]
            if sum(1 for other in normalized if other.startswith(prefix)) == 1:
                result[digest] = f"meme:{prefix}"
                break
        result.setdefault(digest, f"meme:{digest}")
    return result


def parse_meme_id(value: str) -> str:
    value = str(value or "").strip()
    if not value.startswith("meme:"):
        return ""
    prefix = value[5:].lower()
    if (
        not prefix
        or len(prefix) < 12
        or any(char not in "0123456789abcdef" for char in prefix)
    ):
        return ""
    return prefix


@dataclass
class SemanticImage:
    content_sha256: str
    relative_path: str
    category: str = ""
    caption: str = ""
    tags: list[str] = field(default_factory=list)
    visible_text: str = ""
    caption_status: str = "pending"
    embedding_status: str = "pending"
    provenance: str = "ai"
    auto_tags: list[str] = field(default_factory=list)
    manual_tags: list[str] = field(default_factory=list)
    manual_override: bool = False
    vision_model: str = ""
    prompt_version: str = PROMPT_VERSION
    text_hash: str = ""
    updated_at: str = field(default_factory=utc_now)
    error: str | None = None

    def __post_init__(self) -> None:
        self.content_sha256 = str(self.content_sha256 or "").lower()
        self.relative_path = str(self.relative_path or "").replace("\\", "/")
        self.tags = normalize_tags(self.tags)
        self.auto_tags = normalize_tags(self.auto_tags)
        self.manual_tags = normalize_tags(self.manual_tags)
        if self.manual_override and self.manual_tags:
            self.tags = list(self.manual_tags)
        if self.caption_status not in CAPTION_STATUSES:
            self.caption_status = "pending"
        if self.embedding_status not in EMBEDDING_STATUSES:
            self.embedding_status = "pending"
        if not self.text_hash and self.caption:
            self.text_hash = text_hash(self.vector_text)

    @property
    def vector_text(self) -> str:
        return build_semantic_text(
            self.caption, self.tags, self.visible_text, self.category
        )

    def to_dict(self) -> dict[str, Any]:
        data = {
            "content_sha256": self.content_sha256,
            "relative_path": self.relative_path,
            "category": self.category,
            "caption": self.caption,
            "tags": self.tags,
            "visible_text": self.visible_text,
            "caption_status": self.caption_status,
            "embedding_status": self.embedding_status,
            "provenance": self.provenance,
            "auto_tags": self.auto_tags,
            "manual_tags": self.manual_tags,
            "manual_override": self.manual_override,
            "vision_model": self.vision_model,
            "prompt_version": self.prompt_version,
            "text_hash": self.text_hash,
            "updated_at": self.updated_at,
            "error": self.error,
        }
        return data

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SemanticImage:
        return cls(
            **{key: value.get(key) for key in cls.__dataclass_fields__ if key in value}
        )


def parse_caption_result(value: Any) -> tuple[str, list[str], str]:
    """严格校验视觉模型结果，只返回可用于向量化的三个字段。"""
    if isinstance(value, str):
        raw = value.strip()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            start, end = raw.find("{"), raw.rfind("}")
            if start >= 0 and end > start:
                value = json.loads(raw[start : end + 1])
            else:
                raise ValueError("视觉模型没有返回 JSON")
    if not isinstance(value, dict):
        raise ValueError("视觉模型结果必须是 JSON 对象")
    caption = str(value.get("caption") or "").strip()
    tags = normalize_tags(value.get("tags"))
    visible_text = str(value.get("visible_text") or "").strip()
    if not caption or not tags:
        raise ValueError("视觉模型结果缺少 caption 或 tags")
    return caption, tags, visible_text
