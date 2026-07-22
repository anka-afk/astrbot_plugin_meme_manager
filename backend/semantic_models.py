"""语义表情包的数据模型与纯函数。

这些对象不依赖 AstrBot，方便在没有启动主程序时做单元测试，也让语义文件格式
成为一个稳定的本地接口。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "1.0"
PROMPT_VERSION = "meme-semantic-v6"
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif"})
CAPTION_STATUSES = frozenset({"pending", "running", "done", "failed"})
EMBEDDING_STATUSES = frozenset({"pending", "running", "done", "failed"})
TASK_STATUSES = frozenset(
    {"idle", "running", "paused", "completed", "completed_with_errors", "failed"}
)
SEMANTIC_MARKER_PATTERN = re.compile(
    r"&&\s*(meme:[0-9a-f]{12,64})\s*&&",
    re.IGNORECASE,
)
SEMANTIC_BARE_REFERENCE_PATTERN = re.compile(
    r"(?<![&\w])(?:`{1,3})?\s*(meme:[0-9a-f]{12,64})\s*(?:`{1,3})?(?![&\w])",
    re.IGNORECASE,
)
SEMANTIC_META_HINTS = (
    "候选",
    "图片",
    "表情",
    "动作",
    "表达",
    "说明",
    "标签",
    "通过",
    "caption",
    "tag",
)
HIDDEN_REASONING_BLOCK_PATTERN = re.compile(
    r"<(?P<tag>think|thinking|analysis|reasoning)\b[^>]*>.*?</(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)
UNCLOSED_REASONING_BLOCK_PATTERN = re.compile(
    r"<(?:think|thinking|analysis|reasoning)\b[^>]*>.*$",
    re.IGNORECASE | re.DOTALL,
)
GENERIC_MEME_MARKER_PATTERN = re.compile(r"&&[^&\r\n]{1,100}&&")
SEMANTIC_QUERY_MAX_CHARS = 48


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
    if any(not math.isfinite(value) for value in values):
        raise ValueError("向量不能包含无效数值")
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


def extract_and_clean_semantic_meme_references(text: str) -> tuple[str, list[str]]:
    """提取语义图片 ID，并清理模型误输出的机器标记和候选说明。"""
    references: list[str] = []

    def remember(value: str) -> None:
        prefix = parse_meme_id(str(value or "").lower())
        normalized = f"meme:{prefix}" if prefix else ""
        if normalized and normalized not in references:
            references.append(normalized)

    def remove_wrapped(match: re.Match) -> str:
        remember(match.group(1))
        return ""

    clean_text = SEMANTIC_MARKER_PATTERN.sub(remove_wrapped, str(text or ""))
    cleaned_lines: list[str] = []
    for line in clean_text.splitlines():
        matches = list(SEMANTIC_BARE_REFERENCE_PATTERN.finditer(line))
        if not matches:
            cleaned_lines.append(line)
            continue

        for match in matches:
            remember(match.group(1))

        first_match = matches[0]
        prefix_text = line[: first_match.start()].strip(" \t`")
        if not prefix_text:
            remainder = line[first_match.end() :].lstrip()
            remainder = re.sub(r"^[,，:：;；\-—]+\s*", "", remainder)
            sentence_end = re.search(r"[。！？!?\.]", remainder)
            first_sentence = (
                remainder[: sentence_end.end()] if sentence_end else remainder
            )
            if not remainder or any(
                hint.lower() in first_sentence.lower() for hint in SEMANTIC_META_HINTS
            ):
                if sentence_end:
                    remainder = remainder[sentence_end.end() :].lstrip()
                    if remainder:
                        cleaned_lines.append(remainder)
                continue

        cleaned_line = SEMANTIC_BARE_REFERENCE_PATTERN.sub("", line)
        cleaned_line = re.sub(r"^\s*[,，:：;；\-—]+\s*", "", cleaned_line)
        cleaned_lines.append(cleaned_line)

    cleaned = "\n".join(cleaned_lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, references


def extract_visible_semantic_reply(text: str) -> str:
    """移除隐藏思考与机器标记，只保留适合生成检索词的可见回复。"""
    value = str(text or "")
    value = HIDDEN_REASONING_BLOCK_PATTERN.sub("", value)
    value = UNCLOSED_REASONING_BLOCK_PATTERN.sub("", value)
    value = GENERIC_MEME_MARKER_PATTERN.sub("", value)
    value = re.sub(r"```(?:json)?\s*|```", "", value, flags=re.IGNORECASE)
    value = re.sub(r"[\t\r\f\v]+", " ", value)
    value = re.sub(r" *\n+ *", "\n", value)
    return value.strip()


def compact_semantic_query(
    value: Any, max_chars: int = SEMANTIC_QUERY_MAX_CHARS
) -> str:
    """把模型生成的检索词压成单行短文本，避免整段回复进入向量模型。"""
    query = str(value or "").strip()
    query = re.sub(r"^```(?:json)?\s*|```$", "", query, flags=re.IGNORECASE)
    query = re.sub(r"^(?:query|检索词|搜索词)\s*[:：]\s*", "", query, flags=re.I)
    query = re.sub(r"\s+", " ", query).strip(" \t\r\n\"'`，,。；;")
    limit = max(8, int(max_chars or SEMANTIC_QUERY_MAX_CHARS))
    return query[:limit].rstrip(" ，,。；;")


def parse_semantic_query_result(value: Any, fallback: str = "") -> str:
    """解析短检索词 JSON；异常或空结果时使用严格截短的可见文本。"""
    raw = str(value or "").strip()
    query = ""
    data: Any = None
    if raw:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            decoder = json.JSONDecoder()
            query_objects = []
            for start, character in enumerate(raw):
                if character != "{":
                    continue
                try:
                    candidate, _ = decoder.raw_decode(raw[start:])
                except (TypeError, ValueError):
                    continue
                if isinstance(candidate, dict) and (
                    "query" in candidate or "keywords" in candidate
                ):
                    query_objects.append(candidate)
            if query_objects:
                data = query_objects[-1]
        if isinstance(data, dict):
            query = str(data.get("query") or data.get("keywords") or "")
        elif not raw.startswith("{"):
            query = raw
    return compact_semantic_query(query or fallback)


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
            decoder = json.JSONDecoder()
            decoded_objects = []
            valid_caption_objects = []
            for start, character in enumerate(raw):
                if character != "{":
                    continue
                try:
                    candidate, _ = decoder.raw_decode(raw[start:])
                except json.JSONDecodeError:
                    continue
                if not isinstance(candidate, dict):
                    continue
                decoded_objects.append(candidate)
                if str(candidate.get("caption") or "").strip() and normalize_tags(
                    candidate.get("tags")
                ):
                    valid_caption_objects.append(candidate)
            if valid_caption_objects:
                # 工具参数 JSON 可能出现在最终结果之前，只接受真正包含
                # caption/tags 的最后一个对象。
                value = valid_caption_objects[-1]
            elif decoded_objects:
                value = decoded_objects[-1]
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
