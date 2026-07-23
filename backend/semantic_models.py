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

SCHEMA_VERSION = "2.0"
PROMPT_VERSION = "meme-semantic-v8-category-reclassification"
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp"})
CAPTION_STATUSES = frozenset({"pending", "running", "done", "failed"})
EMBEDDING_STATUSES = frozenset({"pending", "running", "done", "failed", "cleared"})
CATEGORY_FITS = frozenset({"match", "uncertain", "conflict"})
CATEGORY_REVIEW_STATUSES = frozenset(
    {
        "unchecked",
        "auto_match",
        "needs_review",
        "manual_confirmed",
        "manual_rejected",
    }
)
RECLASSIFICATION_STATUSES = frozenset({"", "auto_reclassified", "moved_to_review"})
REVIEW_CATEGORY = "needs_review"
REVIEW_CATEGORY_DESCRIPTION = (
    "自动语义化发现图片与原分类明显不符，等待人工确认或重新移动"
)
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


def build_category_tag(category: str) -> str:
    """返回后端维护的固定分类标签。"""
    value = str(category or "").strip()
    return f"category:{value}" if value else ""


def is_category_tag(tag: Any) -> bool:
    value = str(tag or "").strip()
    return value.startswith("category:") or value.startswith("分类:")


def ensure_category_tag(tags: Any, category: str) -> list[str]:
    """把后端固定分类标签放在第一位，其余内容标签保持原样。"""
    fixed_tag = build_category_tag(category)
    content_tags = [tag for tag in normalize_tags(tags) if not is_category_tag(tag)]
    return [fixed_tag, *content_tags] if fixed_tag else content_tags


def runtime_category_mapping(mapping: Any) -> dict[str, str]:
    """Remove non-chat review buckets from a runtime category mapping.

    Args:
        mapping: Category-to-description mapping loaded from a pack.

    Returns:
        A normalized mapping that cannot expose ``needs_review`` to chat mode.
    """
    if not isinstance(mapping, dict):
        return {}
    return {
        str(category).strip(): str(description)
        for category, description in mapping.items()
        if str(category).strip() and str(category).strip() != REVIEW_CATEGORY
    }


def category_review_is_complete(status: Any) -> bool:
    """模型已判断或用户已确认时，分类审核才算完成。"""
    return str(status or "") in {
        "auto_match",
        "needs_review",
        "manual_confirmed",
        "manual_rejected",
    }


def category_analysis_is_current(item: Any) -> bool:
    """分类已审核，且 AI 内容由当前分类感知提示词生成时才可建立索引。"""
    if not isinstance(item, dict) or not category_review_is_complete(
        item.get("category_review_status")
    ):
        return False
    if item.get("manual_override") or item.get("provenance") in {"manual", "mixed"}:
        return True
    return str(item.get("prompt_version") or "") == PROMPT_VERSION


def semantic_entry_id(
    content_sha256: str, category: str, relative_path: str = ""
) -> str:
    """按“图片内容 + 分类 + 路径”生成稳定键。

    ``relative_path`` 保持可选是为了读取旧数据和兼容调用方；当前磁盘扫描会
    始终传入路径，使同内容图片也能被逐文件人工修改。
    """
    digest = str(content_sha256 or "").strip().lower()
    category_value = str(category or "").strip()
    path_value = str(relative_path or "").strip().replace("\\", "/")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("content_sha256 必须是完整 SHA-256")
    return hashlib.sha256(
        f"{digest}\0{category_value}\0{path_value}".encode()
    ).hexdigest()


def category_context_hash(
    content_sha256: str, category: str, category_description: str = ""
) -> str:
    """生成审核上下文指纹；图片、分类或描述变化都会得到不同结果。"""
    digest = str(content_sha256 or "").strip().lower()
    category_value = str(category or "").strip()
    description_value = re.sub(r"\s+", " ", str(category_description or "")).strip()
    return hashlib.sha256(
        f"{digest}\0{category_value}\0{description_value}".encode()
    ).hexdigest()


def anchor_caption_to_category(
    caption: str,
    tags: Any,
    category: str,
    category_fit: str,
    category_description: str = "",
) -> str:
    """在非明确冲突时，确保描述不会完全丢失用户已有分类这一主语。"""
    value = str(caption or "").strip()
    category_value = str(category or "").strip()
    if not value or not category_value or category_fit == "conflict":
        return value
    if value.startswith(f"以当前分类“{category_value}”"):
        return value
    description = re.sub(r"\s+", " ", str(category_description or "")).strip()[:160]
    description_hint = f"（{description}）" if description else ""
    return (
        f"以当前分类“{category_value}”{description_hint}所代表的情绪、态度或用途为主体："
        f"{value}"
    )


def build_semantic_text(
    caption: str,
    tags: Iterable[str],
    visible_text: str,
    category: str = "",
    category_description: str = "",
) -> str:
    """生成向量文本；固定分类只出现一次，但保持高权重的独立字段。"""
    normalized_tags = ensure_category_tag(tags, category)
    # 第一个标签是后端固定分类标签，已在独立字段中写入；后续标签即使也有
    # 分类性质，仍视为模型或人工提供的普通内容标签并予以保留。
    ordinary_tags = (
        normalized_tags[1:] if category and normalized_tags else normalized_tags
    )
    parts = [
        f"图片含义：{str(caption or '').strip()}",
    ]
    if category:
        parts.append(f"固定分类标签：{build_category_tag(category)}")
    if category_description:
        parts.append(f"分类含义：{str(category_description).strip()}")
    parts.extend(
        [
            f"语义标签：{'、'.join(ordinary_tags)}",
            f"图片文字：{str(visible_text or '').strip()}",
        ]
    )
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
    entry_id: str = ""
    category_description: str = ""
    category_tag: str = ""
    category_context_hash: str = ""
    category_fit: str = "uncertain"
    category_review_status: str = "unchecked"
    category_review_reason: str = ""
    category_review_context_hash: str = ""
    manual_confirmation_context_hash: str = ""
    suggested_category: str = ""
    reclassification_status: str = ""
    reclassified_from_category: str = ""
    reclassified_to_category: str = ""
    reclassification_reason: str = ""
    reclassified_at: str = ""
    reclassification_history: list[dict[str, str]] = field(default_factory=list)
    caption: str = ""
    tags: list[str] = field(default_factory=list)
    visible_text: str = ""
    caption_status: str = "pending"
    embedding_status: str = "pending"
    provenance: str = "ai"
    auto_caption: str = ""
    auto_tags: list[str] = field(default_factory=list)
    auto_visible_text: str = ""
    auto_category_fit: str = "uncertain"
    auto_category_review_status: str = "unchecked"
    auto_category_review_reason: str = ""
    manual_caption: str = ""
    manual_tags: list[str] = field(default_factory=list)
    manual_visible_text: str = ""
    manual_override: bool = False
    vision_model: str = ""
    prompt_version: str = PROMPT_VERSION
    text_hash: str = ""
    legacy_text_hash: str = ""
    updated_at: str = field(default_factory=utc_now)
    error: str | None = None

    def __post_init__(self) -> None:
        self.content_sha256 = str(self.content_sha256 or "").lower()
        self.relative_path = str(self.relative_path or "").replace("\\", "/")
        self.category = str(self.category or "").strip()
        self.category_description = str(self.category_description or "").strip()
        self.suggested_category = str(self.suggested_category or "").strip()
        self.reclassified_from_category = str(
            self.reclassified_from_category or ""
        ).strip()
        self.reclassified_to_category = str(self.reclassified_to_category or "").strip()
        self.reclassification_reason = str(self.reclassification_reason or "").strip()[
            :500
        ]
        self.reclassified_at = str(self.reclassified_at or "").strip()
        if self.reclassification_status not in RECLASSIFICATION_STATUSES:
            self.reclassification_status = ""
        if not isinstance(self.reclassification_history, list):
            self.reclassification_history = []
        self.reclassification_history = [
            {
                "from_category": str(value.get("from_category") or "").strip(),
                "to_category": str(value.get("to_category") or "").strip(),
                "reason": str(value.get("reason") or "").strip()[:500],
                "status": str(value.get("status") or "").strip(),
                "at": str(value.get("at") or "").strip(),
            }
            for value in self.reclassification_history[-20:]
            if isinstance(value, dict)
        ]
        if len(self.content_sha256) == 64:
            self.entry_id = semantic_entry_id(
                self.content_sha256, self.category, self.relative_path
            )
        self.category_tag = build_category_tag(self.category)
        current_context_hash = category_context_hash(
            self.content_sha256, self.category, self.category_description
        )
        if self.category_context_hash != current_context_hash:
            self.category_context_hash = current_context_hash
            self.category_review_status = "unchecked"
            self.category_review_reason = ""
            self.category_review_context_hash = ""
            self.manual_confirmation_context_hash = ""
        self.tags = ensure_category_tag(self.tags, self.category)
        self.auto_tags = normalize_tags(self.auto_tags)
        self.manual_tags = normalize_tags(self.manual_tags)
        if self.manual_override:
            if not self.manual_tags:
                self.manual_tags = [
                    tag for tag in self.tags if tag != self.category_tag
                ]
            self.tags = ensure_category_tag(self.manual_tags, self.category)
            self.caption = str(self.manual_caption or self.caption or "").strip()
            self.visible_text = str(
                self.manual_visible_text or self.visible_text or ""
            ).strip()
        if self.auto_category_fit not in CATEGORY_FITS:
            self.auto_category_fit = "uncertain"
        if self.auto_category_review_status not in CATEGORY_REVIEW_STATUSES:
            self.auto_category_review_status = "unchecked"
        if self.caption_status not in CAPTION_STATUSES:
            self.caption_status = "pending"
        if self.embedding_status not in EMBEDDING_STATUSES:
            self.embedding_status = "pending"
        if self.category_fit not in CATEGORY_FITS:
            self.category_fit = "uncertain"
        if self.category_review_status not in CATEGORY_REVIEW_STATUSES:
            self.category_review_status = "unchecked"
        if self.category_review_status in {"manual_confirmed", "manual_rejected"}:
            if self.manual_confirmation_context_hash != self.category_context_hash:
                self.category_review_status = "unchecked"
                self.manual_confirmation_context_hash = ""
        elif (
            self.category_review_status in {"auto_match", "needs_review"}
            and self.category_review_context_hash != self.category_context_hash
        ):
            self.category_review_status = "unchecked"
            self.category_review_reason = ""
            self.category_review_context_hash = ""
        if not self.text_hash and self.caption:
            self.text_hash = text_hash(self.vector_text)

    @property
    def vector_text(self) -> str:
        return build_semantic_text(
            self.caption,
            self.tags,
            self.visible_text,
            self.category,
            self.category_description,
        )

    def to_dict(self) -> dict[str, Any]:
        data = {
            "content_sha256": self.content_sha256,
            "relative_path": self.relative_path,
            "entry_id": self.entry_id,
            "category": self.category,
            "category_description": self.category_description,
            "category_tag": self.category_tag,
            "category_context_hash": self.category_context_hash,
            "category_fit": self.category_fit,
            "category_review_status": self.category_review_status,
            "category_review_reason": self.category_review_reason,
            "category_review_context_hash": self.category_review_context_hash,
            "manual_confirmation_context_hash": self.manual_confirmation_context_hash,
            "suggested_category": self.suggested_category,
            "reclassification_status": self.reclassification_status,
            "reclassified_from_category": self.reclassified_from_category,
            "reclassified_to_category": self.reclassified_to_category,
            "reclassification_reason": self.reclassification_reason,
            "reclassified_at": self.reclassified_at,
            "reclassification_history": self.reclassification_history,
            "caption": self.caption,
            "tags": self.tags,
            "visible_text": self.visible_text,
            "caption_status": self.caption_status,
            "embedding_status": self.embedding_status,
            "provenance": self.provenance,
            "auto_caption": self.auto_caption,
            "auto_tags": self.auto_tags,
            "auto_visible_text": self.auto_visible_text,
            "auto_category_fit": self.auto_category_fit,
            "auto_category_review_status": self.auto_category_review_status,
            "auto_category_review_reason": self.auto_category_review_reason,
            "manual_caption": self.manual_caption,
            "manual_tags": self.manual_tags,
            "manual_visible_text": self.manual_visible_text,
            "manual_override": self.manual_override,
            "vision_model": self.vision_model,
            "prompt_version": self.prompt_version,
            "text_hash": self.text_hash,
            "legacy_text_hash": self.legacy_text_hash,
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


def parse_caption_result_with_review(
    value: Any,
) -> tuple[str, list[str], str, str, str, str]:
    """解析带分类符合判断的视觉结果，并兼容旧模型的三字段结果。"""
    original = value
    if isinstance(value, str):
        raw = value.strip()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            decoder = json.JSONDecoder()
            candidates = []
            for start, character in enumerate(raw):
                if character != "{":
                    continue
                try:
                    candidate, _ = decoder.raw_decode(raw[start:])
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict) and candidate.get("caption"):
                    candidates.append(candidate)
            value = candidates[-1] if candidates else original
    caption, tags, visible_text = parse_caption_result(original)
    payload = value if isinstance(value, dict) else {}
    category_fit = str(payload.get("category_fit") or "uncertain").strip().lower()
    if category_fit not in CATEGORY_FITS:
        raise ValueError("视觉模型结果的 category_fit 无效")
    reason = str(payload.get("category_review_reason") or "").strip()
    reason = re.sub(r"\s+", " ", reason)[:240]
    if category_fit == "match":
        reason = ""
    elif not reason:
        reason = (
            "模型未返回分类判断原因"
            if "category_fit" in payload
            else "模型未返回分类符合判断"
        )
    suggested_category = str(payload.get("suggested_category") or "").strip()
    suggested_category = re.sub(r"\s+", " ", suggested_category)[:80]
    if category_fit != "conflict":
        suggested_category = ""
    return (
        caption,
        tags,
        visible_text,
        category_fit,
        reason,
        suggested_category,
    )
