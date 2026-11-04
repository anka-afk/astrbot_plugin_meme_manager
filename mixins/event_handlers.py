import asyncio
import copy
import hashlib
import json
import os
import random
import re
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from PIL import Image as PILImage

from astrbot.api import ToolSet, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, ResultContentType, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.platform import MessageType
from astrbot.api.provider import LLMResponse, ProviderRequest

from ..astrbot_compat import (
    ContextWrapper,
    MessageSession,
    get_astrbot_temp_path,
    get_image_chat_routing,
    is_builtin_message_tool,
)
from ..backend.delivery_history import DeliveryHistory
from ..backend.meme_parser import MemeParser
from ..backend.meme_parser.text_safety import (
    _protected_reference_spans,
    strip_internal_image_ref_lines,
)
from ..backend.message_tool import MemeMessageTool, OutgoingEvent
from ..backend.packs.categories import resolve_safe_category_directory
from ..backend.packs.images import (
    IMAGE_EXTENSIONS,
    DuplicateEmojiError,
    save_chat_upload_image,
)
from ..backend.packs.resolver import get_pack_paths, load_pack_category_mapping
from ..backend.selection import SelectionTask, select_candidates
from ..backend.semantic.models import (
    REVIEW_CATEGORY,
    extract_visible_semantic_reply,
    runtime_category_mapping,
)
from ..backend.semantic.query import (
    candidate_records_multi_pack,
    remember_candidates,
    search_memes_multi_pack,
    validate_selected_id,
)
from ..backend.semantic.query_builder import build_semantic_query
from ..backend.semantic.storage import invalidate_semantic_metadata
from ..config import PLUGIN_DATA_DIR
from ..utils import probability_hit

TRIGGER_SCOPE_CHAT_ONLY = "only_chat_llm"
TRIGGER_SCOPE_CHAT_AND_PLUGIN = "chat_and_plugin_llm"
LLM_REQUEST_ORIGIN_EXTRA_KEY = "meme_manager_llm_request_origin"
LLM_REQUEST_ORIGIN_CHAT = "chat"
LLM_REQUEST_ORIGIN_PLUGIN = "plugin"
SEND_TOOL_PROMPT_MARKER_START = "<!-- meme_manager_send_tool:start -->"
SEND_TOOL_PROMPT_MARKER_END = "<!-- meme_manager_send_tool:end -->"


def normalize_trigger_scope(value: Any) -> str:
    """解析支持的触发范围，默认仅普通聊天。

    Args:
        value: 配置的 LLM 触发范围。

    Returns:
        两种受支持触发范围之一。
    """
    scope = str(value or TRIGGER_SCOPE_CHAT_ONLY).strip().lower()
    if scope == TRIGGER_SCOPE_CHAT_AND_PLUGIN:
        return scope
    return TRIGGER_SCOPE_CHAT_ONLY


class EventHandlerMixin:
    """处理上传、LLM 回复及表情投递。"""

    def _track_prepared_meme(
        self, event, image, source_path, *, category="", candidate_id=""
    ):
        """将准备好的图片关联到原始来源，直到完成投递。

        Args:
            event: 所属回复事件。
            image: 准备好的消息图片组件。
            source_path: 可选转换之前的本地图片。
            category: 所选分类（如已知）。
            candidate_id: 确切的语义候选（如适用）。
        """
        registry = event.get_extra("meme_manager_delivery_registry")
        if not isinstance(registry, dict):
            registry = {}
            event.set_extra("meme_manager_delivery_registry", registry)
        record = {
            "source": str(source_path),
            "category": category,
            "candidate_id": candidate_id,
            "confirmed": False,
        }
        registry[id(image)] = record
        registry[str(image.file)] = record
        if not event.get_extra("meme_manager_direct_send_attempted"):
            event.set_extra("meme_manager_tag_image_prepared", True)

    async def _confirm_meme_delivery(self, event, images):
        """仅在成功发送返回后记录已注册图片。

        Args:
            event: 所属回复事件。
            images: 成功发送的消息中的组件。
        """
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        try:
            registry = event.get_extra("meme_manager_delivery_registry")
        except AttributeError:
            return
        if not origin or not isinstance(registry, dict):
            return
        history = getattr(self, "_delivery_history", None)
        if history is None:
            history = self._delivery_history = DeliveryHistory()
        for image in images:
            if not isinstance(image, Image):
                continue
            record = registry.get(id(image)) or registry.get(str(image.file))
            if not record or record["confirmed"]:
                continue
            # 让出执行权前先占用状态，发送包装器与直接发送共用此状态。
            record["confirmed"] = True
            digest = ""
            try:
                digest = await asyncio.to_thread(
                    lambda path=record["source"]: hashlib.sha256(
                        Path(path).read_bytes()
                    ).hexdigest()
                )
            except OSError:
                pass
            finally:
                # 可选的指纹计算被取消时，不能清除
                # 适配器调用已成功返回的投递记录。
                history.record(
                    origin,
                    {
                        "category": record["category"],
                        "candidate_id": record["candidate_id"],
                        "content_sha256": digest,
                    },
                )

    def _alias_prepared_meme(self, event, original, normalized):
        """适配器替换组件时保留图片来源信息。

        Args:
            event: 所属回复事件。
            original: 先前注册的组件。
            normalized: 替换后的组件，可能包含 base64。
        """
        try:
            registry = event.get_extra("meme_manager_delivery_registry")
        except AttributeError:
            return
        if not isinstance(registry, dict):
            return
        record = registry.get(id(original))
        if record:
            registry[id(normalized)] = record
            registry[str(normalized.file)] = record

    def _meme_send_allowed(self, event: AstrMessageEvent) -> bool:
        """缓存一个事件回复的独立投递决策。

        Args:
            event: 钩子、重试与流式投递共享的消息事件。

        Returns:
            当前回复是否可以发送所选表情。
        """
        if event.get_extra("meme_manager_direct_send_attempted"):
            return False
        if not event.get_extra("meme_manager_outgoing_processing") and str(
            getattr(event, "unified_msg_origin", "")
        ) in (event.get_extra("meme_manager_tool_message_targets") or ()):
            return False
        decision = event.get_extra("meme_manager_send_probability_hit")
        if decision is None:
            decision = probability_hit(self.emotions_probability)
            event.set_extra("meme_manager_send_probability_hit", decision)
        return bool(decision)

    async def _mark_llm_request_origin_impl(self, event: AstrMessageEvent) -> None:
        """在 AstrBot 构建默认请求前记录本轮 LLM 请求来源。"""
        origin = (
            LLM_REQUEST_ORIGIN_PLUGIN
            if event.get_extra("provider_request") is not None
            else LLM_REQUEST_ORIGIN_CHAT
        )
        event.set_extra(LLM_REQUEST_ORIGIN_EXTRA_KEY, origin)

        send_message = getattr(event, "send", None)
        if callable(send_message) and not event.get_extra(
            "meme_manager_delivery_observer_installed"
        ):

            async def send_observed(message, *args, **kwargs):
                result = await send_message(message, *args, **kwargs)
                await self._confirm_meme_delivery(
                    event, getattr(message, "chain", []) or []
                )
                return result

            event.send = send_observed
            event.set_extra("meme_manager_delivery_observer_installed", True)

        # AstrBot 对流式结果不会执行装饰钩子，因此要在代理开始消费
        # 响应流之前拦截该事件的发送过程。
        send_streaming = getattr(event, "send_streaming", None)
        if callable(send_streaming) and not event.get_extra(
            "meme_manager_stream_filter_installed"
        ):

            async def send_filtered_stream(generator, *args, **kwargs):
                event.set_extra("meme_manager_stream_exhausted", False)
                filtered = self._filter_meme_stream(event, generator)
                stream_completed = False
                try:
                    try:
                        result = await send_streaming(filtered, *args, **kwargs)
                    finally:
                        await filtered.aclose()
                    stream_completed = bool(
                        event.get_extra("meme_manager_stream_exhausted")
                    )
                finally:
                    if not stream_completed:
                        event.set_extra("meme_manager_stream_exhausted", False)
                        event.set_extra("found_emotions", None)
                        event.set_extra("meme_manager_semantic_selected_ids", None)
                        event.set_extra("meme_manager_image_only_reply", False)
                        event.set_extra("meme_manager_legacy_probability_hit", None)
                # 插件发起的流可能在 AstrBot 中丢失 STREAMING_FINISH 阶段。
                # 等待来源流耗尽，且适配器完成最后一次缓冲输出。
                if stream_completed and (
                    self.streaming_compatibility
                    or event.get_platform_name() == "webchat"
                    or event.get_extra("meme_manager_image_only_reply")
                ):
                    try:
                        await self._send_memes_streaming(event, stream_completed=True)
                    except Exception:
                        logger.exception("[meme_manager] 发送流式回复表情失败")
                return result

            event.send_streaming = send_filtered_stream
            event.set_extra("meme_manager_stream_filter_installed", True)

    def _scope_allows_llm_origin(self, event: AstrMessageEvent) -> bool:
        """判断当前配置是否允许处理该来源的 LLM 请求。"""
        scope = normalize_trigger_scope(getattr(self, "trigger_scope", None))
        origin = event.get_extra(LLM_REQUEST_ORIGIN_EXTRA_KEY)
        if origin == LLM_REQUEST_ORIGIN_CHAT:
            return True
        return (
            scope == TRIGGER_SCOPE_CHAT_AND_PLUGIN
            and origin == LLM_REQUEST_ORIGIN_PLUGIN
        )

    def _should_attach_for_result(
        self,
        event: AstrMessageEvent,
        result: Any,
    ) -> bool:
        """仅允许配置范围内的内部聊天或插件 LLM 结果附加表情。"""
        if result is None:
            return False
        if not self._scope_allows_llm_origin(event):
            return False
        content_type = getattr(result, "result_content_type", None)
        content_type_name = str(getattr(content_type, "name", content_type) or "")
        if content_type_name == "STREAMING_FINISH":
            return True
        checker = getattr(result, "is_llm_result", None)
        if callable(checker):
            return bool(checker())
        return content_type_name == "LLM_RESULT"

    @staticmethod
    def _stringify_emotion_context_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (int, float, bool)):
            return str(value)
        if isinstance(value, list):
            parts = [
                EventHandlerMixin._stringify_emotion_context_text(item)
                for item in value
            ]
            return " ".join(part for part in parts if part).strip()
        if isinstance(value, dict):
            for key in ("text", "content", "message", "body", "value"):
                text = EventHandlerMixin._stringify_emotion_context_text(value.get(key))
                if text:
                    return text
            try:
                return json.dumps(value, ensure_ascii=False)
            except Exception:
                return ""
        return str(value).strip()

    @staticmethod
    def _extract_emotion_context_role(item: Any) -> str:
        if isinstance(item, dict):
            role = str(item.get("role") or item.get("sender") or item.get("type") or "")
            return role.strip().lower()
        role = str(
            getattr(item, "role", "")
            or getattr(item, "sender", "")
            or getattr(item, "type", "")
            or ""
        )
        return role.strip().lower()

    @classmethod
    def _extract_emotion_context_content(cls, item: Any) -> str:
        if isinstance(item, dict):
            for key in ("content", "text", "message", "body"):
                text = cls._stringify_emotion_context_text(item.get(key))
                if text:
                    return text
            return ""
        for attr in ("content", "text", "message", "body"):
            text = cls._stringify_emotion_context_text(getattr(item, attr, None))
            if text:
                return text
        return ""

    def _collect_emotion_context_lines_from_request(
        self, req: ProviderRequest
    ) -> list[str]:
        turns = int(getattr(self, "emotion_llm_context_turns", 0) or 0)
        if turns <= 0:
            return []

        message_items: list[Any] = []
        containers = [req, getattr(req, "conversation", None)]
        candidate_attrs = (
            "contexts",
            "messages",
            "history",
            "chat_history",
            "recent_messages",
            "conversation_history",
        )
        for owner in containers:
            if owner is None:
                continue
            for attr in candidate_attrs:
                value = getattr(owner, attr, None)
                if isinstance(value, str) and value.strip():
                    try:
                        value = json.loads(value)
                    except (TypeError, ValueError):
                        value = None
                if isinstance(value, (list, tuple)) and value:
                    message_items = list(value)
                    break
            if message_items:
                break

        if not message_items:
            return []

        parsed_items: list[tuple[str, str]] = []
        for item in message_items:
            role = self._extract_emotion_context_role(item)
            if role in {"system", "tool", "function"}:
                continue
            content = self._extract_emotion_context_content(item)
            if not content:
                continue
            content = re.sub(r"\s+", " ", content).strip()
            if not content:
                continue
            parsed_items.append((role, content[:300]))

        if not parsed_items:
            return []

        max_messages = max(1, min(turns * 2, 40))
        tail_items = parsed_items[-max_messages:]

        role_map = {
            "user": "用户",
            "assistant": "助手",
            "bot": "助手",
            "ai": "助手",
        }
        return [
            f"{role_map.get(role, '消息')}: {content}" for role, content in tail_items
        ]

    def _resolve_emotion_persona_prompt(self, event: AstrMessageEvent) -> str:
        if not bool(getattr(self, "emotion_llm_inject_persona", False)):
            return ""

        persona_id = str(self._resolve_persona_id(event=event) or "").strip()
        if not persona_id:
            return ""

        personas = getattr(self.context.provider_manager, "personas", []) or []
        for index, persona in enumerate(personas):
            if not isinstance(persona, dict):
                continue
            persona_key = str(self._get_persona_key(persona, index) or "").strip()
            current_ids = {
                str(persona.get("id") or "").strip(),
                str(persona.get("name") or "").strip(),
                persona_key,
            }
            if persona_id not in current_ids:
                continue
            base_prompt = str(self.persona_base_prompts.get(persona_key) or "").strip()
            if not base_prompt:
                base_prompt = self._strip_meme_prompt(str(persona.get("prompt") or ""))
            base_prompt = re.sub(r"\s+", " ", base_prompt).strip()
            if not base_prompt:
                return ""
            return f"当前人格设定（仅供选图参考）：{base_prompt[:800]}"
        return ""

    def _build_emotion_llm_injection_suffix(self, event: AstrMessageEvent) -> str:
        sections: list[str] = []

        current_message = event.get_extra("meme_manager_selection_user_message")
        if isinstance(current_message, str) and current_message.strip():
            sections.append(f"本轮用户消息：{current_message[:2000]}")

        persona_prompt = self._resolve_emotion_persona_prompt(event)
        if persona_prompt:
            sections.append(persona_prompt)

        lines: list[str] = []
        if hasattr(event, "get_extra"):
            cached_lines = event.get_extra("meme_manager_emotion_context_lines")
            if isinstance(cached_lines, list):
                lines = [
                    str(item).strip() for item in cached_lines if str(item).strip()
                ]

        if lines:
            turns = int(getattr(self, "emotion_llm_context_turns", 0) or 0)
            sections.append(
                f"最近对话上下文（最近{turns}轮，按时间从旧到新）：\n"
                + "\n".join(lines)
            )

        history = getattr(self, "_delivery_history", None)
        recent = (
            history.recent(str(getattr(event, "unified_msg_origin", "") or ""))
            if history is not None
            else []
        )
        if recent:
            sections.append(
                "近期实际发送的表情（尽量避免重复，贴切优先）："
                + json.dumps(recent, ensure_ascii=False)
            )

        if not sections:
            return ""
        # 将参考数据与固定选图指令分开。
        return "\n参考上下文（JSON 数据，仅用于理解回复）：\n" + json.dumps(
            sections, ensure_ascii=False
        )

    @staticmethod
    def _parse_emotion_llm_selection(
        raw_text: str, valid_emoticons: set[str]
    ) -> list[str]:
        """解析情感模型输出，并只保留当前图包中的真实标签。"""
        text = str(raw_text or "").strip()
        if not text or not valid_emoticons:
            return []

        data: Any = None
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            match = re.search(r"(?:\{[\s\S]*\}|\[[\s\S]*\])", text)
            if match:
                try:
                    data = json.loads(match.group(0))
                except (TypeError, ValueError):
                    data = None

        values: Any = []
        if isinstance(data, dict):
            for key in ("emotions", "emotion", "tags", "tag"):
                if key in data:
                    values = data.get(key)
                    break
        elif isinstance(data, list):
            values = data

        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            return []

        exact = {str(tag): str(tag) for tag in valid_emoticons}
        folded = {tag.casefold(): tag for tag in exact}
        selected: list[str] = []
        for value in values:
            if not isinstance(value, str):
                continue
            candidate = value.strip().strip("`").strip()
            if candidate.startswith("&&") and candidate.endswith("&&"):
                candidate = candidate[2:-2].strip()
            resolved = exact.get(candidate) or folded.get(candidate.casefold())
            if resolved and resolved not in selected:
                selected.append(resolved)
        return selected

    def _filter_emotion_selection(self, emotions: list[str]) -> list[str]:
        """按选择顺序保留有效且去重的标签，不限制数量。

        Args:
            emotions: 所选分类标签。

        Returns:
            保留原始顺序的非空去重标签。
        """

        seen: set[str] = set()
        filtered: list[str] = []
        for emotion in emotions:
            if not isinstance(emotion, str) or not emotion or emotion in seen:
                continue
            seen.add(emotion)
            filtered.append(emotion)
        return filtered

    def _resolve_emotion_llm_model(
        self, event: AstrMessageEvent, *, use_reply: bool = False
    ) -> str | None:
        """仅在跟随实际回复提供方时解析模型覆盖值。

        Args:
            event: 当前回复事件。
            use_reply: 忽略单独配置的选图提供方。

        Returns:
            实际回复模型的覆盖值；显式选择提供方时返回 None。
        """
        if not use_reply and str(self.emotion_llm_provider_id or "").strip():
            return None

        cached_model = str(event.get_extra("meme_manager_reply_model") or "").strip()
        if cached_model:
            return cached_model

        provider_request = event.get_extra("provider_request")
        request_model = str(getattr(provider_request, "model", "") or "").strip()
        selected_model = str(event.get_extra("selected_model") or "").strip()
        model = request_model or selected_model
        if model:
            event.set_extra("meme_manager_reply_model", model)
        return model or None

    async def _emotion_llm_generate(
        self, event: AstrMessageEvent, prompt: str
    ) -> LLMResponse | None:
        """使用独立情感模型，留空时精确复用本轮回复 Provider 与模型。"""
        provider_id = await self._resolve_emotion_llm_provider_id(event)
        if not provider_id:
            return None

        kwargs: dict[str, Any] = {
            "chat_provider_id": provider_id,
            "prompt": prompt,
        }
        model = self._resolve_emotion_llm_model(event)
        if model:
            kwargs["model"] = model
        return await self.context.llm_generate(**kwargs)

    def _normalize_outgoing_message_components(self, message: Any) -> list:
        """将外部传入消息统一为组件列表。"""
        if isinstance(message, MessageChain):
            components = message.chain or []
        elif isinstance(message, list):
            components = message
        elif isinstance(message, str):
            components = [Plain(message)]
        else:
            raise TypeError("message must be str, list or MessageChain")
        normalized = []
        for component in components:
            if isinstance(component, Plain):
                if normalized and isinstance(normalized[-1], Plain):
                    normalized[-1].text += component.text or ""
                else:
                    normalized.append(Plain(component.text or ""))
            else:
                normalized.append(component)
        return normalized

    def _clean_outgoing_plain_text(self, text: str) -> str:
        cleaned = MemeParser.parse(text or "", strip_references=True).text
        if self.content_cleanup_rule and self.content_cleanup_rule != r"&&[a-zA-Z]*&&":
            protected = _protected_reference_spans(cleaned)
            edits = [
                match.span()
                for match in re.finditer(self.content_cleanup_rule, cleaned)
                if not any(
                    match.start() < end and match.end() > start
                    for start, end in protected
                )
            ]
            for start, end in reversed(edits):
                cleaned = cleaned[:start] + cleaned[end:]
        if getattr(self, "filter_all_tags", False):
            cleaned = MemeParser.parse(cleaned, filter_all=True).text
        return cleaned

    def _make_meme_parser(
        self, categories, *, semantic=False, fallbacks=True, final_cleanup=False
    ):
        """根据当前插件设置构建隔离的解析器。

        Args:
            categories: 当前请求资源包允许的键。
            semantic: 是否接受语义 ID 而不是分类。
            fallbacks: 是否允许独立或重复分类。
            final_cleanup: 此解析器是否处理最终投递文本。

        Returns:
            不共享可变请求状态的新解析器。
        """
        return MemeParser(
            categories,
            semantic=semantic,
            alternative=self._read_config_value(
                ("generation", "markup", "enable_alternative"), default=True
            ),
            loose=fallbacks
            and self._read_config_value(
                ("generation", "matching", "enable_loose_matching"), default=False
            ),
            repeated=fallbacks
            and self._read_config_value(
                ("generation", "markup", "enable_repeated_detection"), default=True
            ),
            remove_invalid=self.remove_invalid_alternative_markup,
            strip_references=True,
            filter_all=final_cleanup and getattr(self, "filter_all_tags", False),
        )

    def _extract_marked_emotions_from_text(
        self, text: str, valid_emoticons: set[str]
    ) -> tuple[str, list[str]]:
        """解析兼容接口消息，不启用独立分类回退检测。

        Args:
            text: 兼容接口的原始消息文本。
            valid_emoticons: 所选资源包允许的键。

        Returns:
            可见文本及按来源顺序排列的有效标记。
        """
        parser = self._make_meme_parser(valid_emoticons, fallbacks=False)
        cleaned = parser.feed(text or "") + parser.finish()
        return cleaned, [token.value for token in parser.tokens if token.valid]

    async def _filter_meme_stream(self, event, source):
        """在平台消费异步流之前过滤可见文本增量。

        Args:
            event: 提供所选资源包的请求事件。
            source: 原始异步 MessageChain 流。

        Yields:
            包含已解析可见文本的消息链，保留其他组件。
        """
        parser = None
        last_text_chunk = None
        try:
            async for chunk in source:
                if chunk is None:
                    continue
                if getattr(chunk, "type", None) not in {None, "text"}:
                    yield chunk
                    continue
                if not isinstance(chunk, MessageChain):
                    yield chunk
                    continue
                # 请求钩子在源生成器内运行，必须等钩子初始化完成后
                # 再解析表情包和语义模式。
                event.set_extra("meme_manager_stream_filtered", True)
                if parser is None:
                    context = self._resolve_runtime_pack_context(event=event)
                    mapping = context.get("category_mapping")
                    categories = runtime_category_mapping(
                        mapping if isinstance(mapping, dict) else self.category_mapping
                    )
                    parser = self._make_meme_parser(
                        categories,
                        semantic=self._semantic_mode_active(event),
                        final_cleanup=True,
                    )
                last_text_chunk = chunk
                components = []
                for component in chunk.chain:
                    if isinstance(component, Plain):
                        visible = parser.feed(component.text or "")
                        if visible:
                            components.append(Plain(visible))
                    else:
                        # 非文本组件会结束连续的文本片段。
                        visible = parser.finish()
                        if visible:
                            components.append(Plain(visible))
                        components.append(component)
                        parser = self._make_meme_parser(
                            categories,
                            semantic=self._semantic_mode_active(event),
                            final_cleanup=True,
                        )
                if components:
                    filtered = copy.copy(chunk)
                    filtered.chain = components
                    yield filtered
            remaining = parser.finish() if parser is not None else ""
            if remaining:
                final_chunk = (
                    copy.copy(last_text_chunk)
                    if last_text_chunk is not None
                    else MessageChain()
                )
                final_chunk.chain = [Plain(remaining)]
                yield final_chunk
            event.set_extra("meme_manager_stream_exhausted", True)
        finally:
            if hasattr(source, "aclose"):
                await source.aclose()

    def _limit_meme_selection(self, selections: list[str], event=None) -> list[str]:
        """在构造或发送图片前截取回复所选表情。

        Args:
            selections: 有序的分类名称或语义图片 ID。
            event: 可选的携带先前自动工具投递记录的事件。

        Returns:
            配置数量内的前若干个选择；负数限制表示全部。
        """
        limit = getattr(self, "max_memes_per_message", -1)
        if event is not None and limit >= 0:
            limit = max(
                0, limit - (event.get_extra("meme_manager_automatic_sent_count") or 0)
            )
        return selections[:limit] if limit >= 0 else selections

    def _get_category_meme_files(
        self, event: AstrMessageEvent, emotion: str
    ) -> list[Path]:
        """从当前绑定的全部表情包中收集分类图片。

        Args:
            event: 用于解析当前表情包绑定的事件。
            emotion: 为投递选择的分类名称。

        Returns:
            全部绑定表情包中的图片路径，不包含待复核分类。
        """
        if not emotion or emotion == REVIEW_CATEGORY:
            return []
        context = self._resolve_runtime_pack_context(event=event)
        roots = [
            Path(directory) / "memes"
            for directory in context.get("pack_dirs", {}).values()
        ]
        if not roots:
            roots = [Path(self._get_runtime_memes_dir_for_event(event))]
        images = []
        for root in roots:
            try:
                directory = resolve_safe_category_directory(root, emotion)
            except ValueError:
                continue
            if directory.is_dir():
                images.extend(
                    path
                    for path in directory.iterdir()
                    if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
                )
        return images

    async def _build_emotion_images_for_event(
        self,
        event: AstrMessageEvent,
        emotions: list[str],
    ) -> tuple[list[Image], list[str]]:
        """根据表情列表构建待发送图片组件，并返回临时文件列表。"""
        return await self._build_selected_meme_images(event, emotions)

    async def _build_selected_meme_images(self, event, selections, *, semantic=False):
        """构造并重新校验所选图片，提供取消时安全的清理。

        Args:
            event: 选图上下文及所属投递门控。
            selections: 有序的分类名称或已验证候选 ID。
            semantic: 选择结果是否为确切的语义候选。

        Returns:
            准备好的图片及其临时转换文件。
        """
        images, temp_files = [], []
        if not selections or not self._meme_send_allowed(event):
            return images, temp_files
        context = self._resolve_runtime_pack_context(event=event)
        if semantic and not self._semantic_mode_active(event):
            return images, temp_files
        for selection in self._limit_meme_selection(selections, event):
            if semantic:
                source = validate_selected_id(event, selection, context)
            else:
                files = self._get_category_meme_files(event, selection)
                source = random.choice(files) if files else None
            if source is None:
                continue
            final_path = str(source)
            conversion = asyncio.create_task(
                asyncio.to_thread(self._convert_to_gif, str(source))
            )
            try:
                final_path = await asyncio.shield(conversion)
                current = self._resolve_runtime_pack_context(event=event)
                valid = (
                    validate_selected_id(event, selection, current) == source
                    if semantic
                    else source in self._get_category_meme_files(event, selection)
                )
                if not valid:
                    continue
                image = Image.fromFileSystem(final_path)
                self._track_prepared_meme(
                    event,
                    image,
                    source,
                    category=source.parent.name if semantic else selection,
                    candidate_id=selection if semantic else "",
                )
                normalized = await self._ensure_image_send_format(image)
                self._alias_prepared_meme(event, image, normalized)
                images.append(normalized)
            except asyncio.CancelledError:
                # 取消等待不会停止转换线程。
                while not conversion.done():
                    try:
                        await asyncio.shield(conversion)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not conversion.cancelled() and conversion.exception() is None:
                    final_path = conversion.result()
                raise
            except Exception:
                logger.exception("[meme_manager] 准备所选表情失败")
            finally:
                if final_path != str(source):
                    temp_files.append(final_path)
                # 立即登记，避免后续图片被取消时泄漏当前图片。
                event.set_extra(
                    "meme_manager_temp_files",
                    list(
                        dict.fromkeys(
                            (event.get_extra("meme_manager_temp_files") or [])
                            + temp_files
                        )
                    ),
                )
        return images, temp_files

    async def _cleanup_outgoing_files(self, event, *, files=None):
        """在工具调用完成或中断后移除临时图片。

        Args:
            event: 转换输出所属的隔离事件。
            files: 公开的已准备消息数据中可选的显式文件列表。
        """
        if files is None:
            files = event.get_extra("meme_manager_temp_files") or []
        for filename in files:
            try:
                Path(filename).unlink(missing_ok=True)
            except OSError:
                logger.warning("[meme_manager] 清理外发表情失败")

    async def _resolve_outgoing_event(self, event):
        """解析目标人格与语义就绪状态，不写入来源状态。

        Args:
            event: 已包含已校验目标的隔离事件。
        """
        if event.unified_msg_origin == str(event.source.unified_msg_origin):
            return
        event.platform_name = ""
        for platform in getattr(
            getattr(self.context, "platform_manager", None), "platform_insts", []
        ):
            if platform.meta().id == event.session.platform_id:
                event.platform_name = platform.meta().name
                break
        event.set_extra("meme_manager_resolved_persona_id", "")
        event.set_extra("meme_manager_direct_send_attempted", False)
        event.set_extra("meme_manager_semantic_active", False)
        event.set_extra("meme_manager_semantic_mode", "")
        event.set_extra("meme_manager_semantic_verified_pack_ids", "")
        event.set_extra("meme_manager_reply_provider_id", "")
        event.set_extra("meme_manager_reply_model", "")
        event.set_extra("selected_provider", "")
        event.set_extra("persona_id", "")
        event.persona_id = ""
        try:
            manager = self.context.conversation_manager
            cid = await manager.get_curr_conversation_id(event.unified_msg_origin)
            conversation = (
                await manager.get_conversation(event.unified_msg_origin, cid)
                if cid
                else None
            )
            (
                persona_id,
                _,
                _,
                _,
            ) = await self.context.persona_manager.resolve_selected_persona(
                umo=event.unified_msg_origin,
                conversation_persona_id=getattr(conversation, "persona_id", None),
                platform_name=event.get_platform_name(),
                provider_settings=self.context.get_config(event.unified_msg_origin).get(
                    "provider_settings", {}
                ),
            )
            event.set_extra("meme_manager_resolved_persona_id", persona_id or "")
        except Exception:
            logger.warning("[meme_manager] 无法解析外发消息的目标人格", exc_info=True)
        context = self._resolve_runtime_pack_context(event=event)
        mode = (
            "llm"
            if self.emotion_llm_enabled
            else str(event.source.get_extra("meme_manager_semantic_mode") or "")
        )
        if mode and self._semantic_pack_ready(event=event):
            event.set_extra("meme_manager_semantic_active", True)
            event.set_extra("meme_manager_semantic_mode", mode)
            event.set_extra(
                "meme_manager_semantic_verified_pack_ids",
                ",".join(context.get("pack_ids", [])),
            )

    async def _prepare_outgoing_message(self, event, message):
        """复用回复选图逻辑，保留外发消息的组件。

        Args:
            event: 目标会话的隔离选图事件。
            message: 原始已校验组件或公开兼容接口输入。

        Returns:
            干净的组件、所选图片及转换文件归属。
        """
        components = self._normalize_outgoing_message_components(message)
        text = "\n".join(c.text for c in components if isinstance(c, Plain))
        response = LLMResponse(role="assistant", completion_text=text)
        await self._resp_impl(event, response)
        context = self._resolve_runtime_pack_context(event=event)
        semantic = self._semantic_mode_active(event)
        categories = runtime_category_mapping(context.get("category_mapping") or {})
        parser = self._make_meme_parser(
            categories, semantic=semantic, final_cleanup=True
        )
        parser.feed(text)
        parser.finish()
        edits = [(token.start, token.end) for token in parser.tokens]
        if self.content_cleanup_rule and self.content_cleanup_rule != r"&&[a-zA-Z]*&&":
            protected = _protected_reference_spans(text)
            edits.extend(
                match.span()
                for match in re.finditer(self.content_cleanup_rule, text)
                if not any(
                    match.start() < end and match.end() > start
                    for start, end in protected
                )
            )
        edits.sort()
        cleaned = []
        offset = 0
        for component in components:
            if not isinstance(component, Plain):
                cleaned.append(component)
                continue
            # 将来源坐标上的文本编辑映射回原始组件。
            # 保留跨媒体的代码上下文，同时不移动媒体组件。
            end_offset = offset + len(component.text)
            cursor, pieces = 0, []
            for start, end in edits:
                if start >= end_offset or end <= offset:
                    continue
                start, end = (
                    max(0, start - offset),
                    min(len(component.text), end - offset),
                )
                pieces.append(component.text[cursor : max(cursor, start)])
                cursor = max(cursor, end)
            pieces.append(component.text[cursor:])
            value = "".join(pieces)
            # 最终过滤还会移除没有对应标记记录的未知标记。
            if getattr(self, "filter_all_tags", False):
                value = MemeParser.parse(value, filter_all=True).text
            value = value.strip()
            if value:
                cleaned.append(Plain(value))
            offset = end_offset + 1
        ids = event.get_extra("meme_manager_semantic_selected_ids") or []
        emotions = event.get_extra("found_emotions") or []
        delivered = event.get_extra("meme_manager_tool_delivered_choices") or ()
        selections = [
            value
            for value in (ids if semantic else emotions)
            if (event.unified_msg_origin, value) not in delivered
        ]
        images, files = await self._build_selected_meme_images(
            event, selections, semantic=semantic
        )
        return {
            "cleaned_chain": MessageChain(cleaned),
            "emotions": emotions,
            "selected_ids": ids,
            "images": images,
            "temp_files": files,
            "delivery_event": event,
        }

    async def compat_prepare_message(
        self,
        event: AstrMessageEvent,
        message: str | list | MessageChain,
    ) -> dict:
        """对外兼容接口：清理消息中的表情标记并准备待发送表情图片。"""
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        local = OutgoingEvent(
            event, MessageSession.from_str(origin) if origin else None
        )
        try:
            prepared = await self._prepare_outgoing_message(local, message)
            if prepared["images"]:
                event.set_extra("meme_manager_tag_image_prepared", True)
            return prepared
        except BaseException:
            await self._cleanup_outgoing_files(local)
            raise

    async def compat_send_message(
        self,
        event: AstrMessageEvent,
        message: str | list | MessageChain,
        *,
        send_images: bool = True,
    ) -> dict:
        """对外兼容接口：使用本插件逻辑清理后发送消息，并可附带发送表情图片。"""
        prepared = await self.compat_prepare_message(event, message)

        return await self.compat_send_prepared_message(
            event,
            prepared,
            send_images=send_images,
        )

    async def compat_send_prepared_message(
        self,
        event: AstrMessageEvent,
        prepared: dict,
        *,
        send_text: bool = True,
        send_images: bool = True,
    ) -> dict:
        """对外兼容接口：发送由 compat_prepare_message 生成的处理结果。"""
        cleaned_chain: MessageChain = prepared.get("cleaned_chain") or MessageChain([])
        emotion_images: list[Image] = prepared.get("images") or []
        temp_files: list[str] = prepared.get("temp_files") or []

        try:
            if send_text and cleaned_chain.chain:
                await event.send(cleaned_chain)

            if send_images and emotion_images:
                for image in emotion_images:
                    await self._send_meme_image(event, image)
                    await self._confirm_meme_delivery(
                        prepared.get("delivery_event", event), [image]
                    )
        finally:
            await self._cleanup_outgoing_files(event, files=temp_files)

        return {
            "sent_text": bool(send_text and cleaned_chain.chain),
            "sent_images_count": len(emotion_images) if send_images else 0,
            "detected_emotions": prepared.get("emotions") or [],
        }

    async def _handle_upload_image_impl(self, event: AstrMessageEvent):
        """将待处理的聊天上传图片保存到确定的主表情包。

        Args:
            event: 携带待处理上传状态及图片组件的事件。

        Yields:
            一个结果消息链，汇总已保存、重复、失败、
            未处理数量以及可能的刷新警告。
        """
        user_key = f"{event.session_id}_{event.get_sender_id()}"
        upload_state = self.upload_states.get(user_key)
        try:
            expired = time.time() > float(upload_state.get("expire_time") or 0)
        except (AttributeError, TypeError, ValueError):
            expired = True
        if not upload_state or expired:
            if self.upload_states.get(user_key) is upload_state:
                self.upload_states.pop(user_key, None)
            return

        images = [c for c in event.message_obj.message if isinstance(c, Image)]
        if not images:
            yield event.plain_result("请发送图片文件来进行上传哦。")
            return

        category = str(upload_state.get("category") or "").strip()
        pack_id = str(upload_state.get("pack_id") or "").strip()
        recorded_memes_dir = str(upload_state.get("memes_dir") or "").strip()
        state_label = f"主表情包「{pack_id or '未知'}」分类「{category or '未知'}」"
        if not pack_id or not category or not recorded_memes_dir:
            if self.upload_states.get(user_key) is upload_state:
                self.upload_states.pop(user_key, None)
            yield event.plain_result(
                f"⚠️ {state_label}的上传状态无效，"
                "请重新执行 /表情管理 添加表情 [类别名称]。"
            )
            return

        try:
            pack_paths = get_pack_paths(pack_id)
            pack_dir = Path(pack_paths["pack_dir"]).resolve()
            canonical_memes_dir = Path(pack_paths["memes_dir"]).resolve()
            recorded_path = Path(recorded_memes_dir).resolve()
            if canonical_memes_dir == pack_dir:
                raise ValueError("memes directory must be a child of the pack")
            canonical_memes_dir.relative_to(pack_dir)
        except (OSError, RuntimeError, ValueError):
            pack_dir = None
            canonical_memes_dir = None
            recorded_path = None
        if (
            pack_dir is None
            or canonical_memes_dir is None
            or not pack_dir.is_dir()
            or canonical_memes_dir != recorded_path
        ):
            if self.upload_states.get(user_key) is upload_state:
                self.upload_states.pop(user_key, None)
            yield event.plain_result(
                f"⚠️ {state_label}的记录路径已失效，"
                "请重新执行 /表情管理 添加表情 [类别名称]。"
            )
            return

        try:
            self.semantic_task_manager.begin_external_pack_operation(
                pack_id, "接收并保存表情图片"
            )
        except RuntimeError as exc:
            # 保留待处理状态，使用户可以在有效期内重试。
            yield event.plain_result(
                f"⚠️ {state_label}暂时无法写入：{exc}\n"
                "上传请求仍然有效，请在30秒内重新发送图片重试。"
            )
            return

        saved_count = 0
        duplicate_count = 0
        failed_count = 0
        unprocessed_count = 0
        stop_note = ""
        refresh_notes: list[str] = []

        def route_problem() -> str:
            # 在路由等待结束后、实际写入前，同步重新解析
            # 已捕获的主表情包及其 memes 根目录。
            try:
                current_paths = get_pack_paths(pack_id)
                current_pack_dir = Path(current_paths["pack_dir"]).resolve()
                current_memes_dir = Path(current_paths["memes_dir"]).resolve()
                if current_memes_dir == current_pack_dir:
                    raise ValueError("memes directory must be a child of the pack")
                current_memes_dir.relative_to(current_pack_dir)
                context = self._resolve_runtime_pack_context(event=event)
                current_pack_id = str(context.get("pack_id") or "").strip()
                routed_memes_dir = Path(str(context.get("memes_dir") or "")).resolve()
            except Exception as exc:
                logger.warning(
                    "聊天上传无法重新解析路由：表情包=%s 分类=%s 错误=%s",
                    pack_id,
                    category,
                    exc,
                )
                return "无法确认当前主表情包"
            if current_pack_id != pack_id:
                return "主表情包在接收过程中发生了变化"
            if routed_memes_dir != canonical_memes_dir:
                return "主表情包路径在接收过程中发生了变化"
            if current_pack_dir != pack_dir or current_memes_dir != canonical_memes_dir:
                return "主表情包路径在接收过程中发生了变化"
            return ""

        async def category_problem() -> str:
            try:
                primary_mapping = await asyncio.to_thread(
                    load_pack_category_mapping, pack_id
                )
                category_path = resolve_safe_category_directory(
                    canonical_memes_dir, category
                )
            except Exception as exc:
                logger.warning(
                    "聊天上传无法重新检查分类 %s：表情包=%s 错误=%s",
                    category,
                    pack_id,
                    exc,
                )
                return "无法确认分类是否仍属于当前主表情包"
            if category in primary_mapping or category_path.is_dir():
                return ""
            return "分类已被删除或不再属于当前主表情包"

        async def commit_image(content: bytes, index: int) -> None:
            nonlocal saved_count, duplicate_count, failed_count
            try:
                await asyncio.to_thread(
                    save_chat_upload_image,
                    canonical_memes_dir,
                    category,
                    content,
                )
                # 仅在工作任务完成写入后计数。
                saved_count += 1
            except DuplicateEmojiError as exc:
                duplicate_count += 1
                logger.info(
                    "聊天上传跳过重复图片：表情包=%s 分类=%s 图片=%s",
                    pack_id,
                    category,
                    exc.existing_filename,
                )
            except Exception as exc:
                failed_count += 1
                logger.warning(
                    "聊天上传第 %s/%s 张图片保存失败：表情包=%s 分类=%s 错误=%s",
                    index + 1,
                    len(images),
                    pack_id,
                    category,
                    exc,
                )

        async def refresh_metadata() -> None:
            try:
                await asyncio.to_thread(invalidate_semantic_metadata, pack_dir)
            except Exception as exc:
                refresh_notes.append("语义元数据刷新失败，已保存的图片不受影响。")
                logger.warning(
                    "聊天上传已保存图片，但使语义数据失效时失败：表情包=%s 错误=%s",
                    pack_id,
                    exc,
                )
            try:
                await self.reload_emotions()
            except Exception as exc:
                refresh_notes.append("表情配置刷新失败，已保存的图片不受影响。")
                logger.warning(
                    "聊天上传已保存图片，但重新加载分类失败：表情包=%s 错误=%s",
                    pack_id,
                    exc,
                )

        async def wait_for_worker(task: asyncio.Task) -> None:
            """在向上传播取消之前完成受屏蔽保护的工作任务。

            Args:
                task: 持有表情包保护锁的图片提交或元数据刷新任务。

            Returns:
                工作任务完成后返回 None。

            Raises:
                asyncio.CancelledError: 完成前收到取消请求，
                    仅在工作任务结束后向上传播。
            """
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # 无法安全中断写入或刷新工作任务，因此在其返回前
                # 保持表情包锁定，随后再向上传播取消。
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if task.done() and not task.cancelled():
                    task.exception()
                raise

        try:
            initial_problem = await category_problem()
            if not initial_problem:
                initial_problem = route_problem()
            if initial_problem:
                stop_note = initial_problem
                unprocessed_count = len(images)
            else:
                for index, image in enumerate(images):
                    try:
                        local_path = Path(
                            await asyncio.wait_for(
                                image.convert_to_file_path(), timeout=30
                            )
                        )
                        content = await asyncio.to_thread(local_path.read_bytes)
                    except asyncio.TimeoutError:
                        failed_count += 1
                        logger.warning(
                            "聊天上传第 %s/%s 张图片超时：表情包=%s 分类=%s",
                            index + 1,
                            len(images),
                            pack_id,
                            category,
                        )
                        continue
                    except Exception as exc:
                        failed_count += 1
                        logger.warning(
                            "聊天上传第 %s/%s 张图片读取失败：表情包=%s 分类=%s 错误=%s",
                            index + 1,
                            len(images),
                            pack_id,
                            category,
                            exc,
                        )
                        continue

                    problem = await category_problem()
                    if not problem:
                        problem = route_problem()
                    if problem:
                        stop_note = problem
                        unprocessed_count = len(images) - index
                        break
                    await wait_for_worker(
                        asyncio.create_task(commit_image(content, index))
                    )

        except Exception as exc:
            logger.exception(
                "聊天上传出现意外错误：表情包=%s 分类=%s 错误=%s",
                pack_id,
                category,
                exc,
            )
            unprocessed_count = max(
                unprocessed_count,
                len(images) - saved_count - duplicate_count - failed_count,
            )
            stop_note = stop_note or "上传过程中发生内部错误，已保存的文件不受影响"
        finally:
            try:
                if saved_count:
                    # 仍持有表情包操作保护锁时执行刷新；被取消的
                    # 消费方必须等待元数据工作任务结束。
                    await wait_for_worker(asyncio.create_task(refresh_metadata()))
            finally:
                if self.upload_states.get(user_key) is upload_state:
                    self.upload_states.pop(user_key, None)
                self.semantic_task_manager.end_external_pack_operation(pack_id)

        if saved_count and not failed_count and not stop_note:
            if duplicate_count:
                message = (
                    f"✅ 已收录 {saved_count} 张新表情到{state_label}；"
                    f"另有 {duplicate_count} 张重复图片未新增。"
                )
            else:
                message = f"✅ 已经成功收录了 {saved_count} 张新表情到{state_label}！"
        elif saved_count:
            message = (
                f"⚠️ 部分完成：{state_label}新增 {saved_count} 张，"
                f"重复 {duplicate_count} 张，失败 {failed_count} 张。"
            )
        elif duplicate_count and not failed_count and not stop_note:
            message = (
                f"ℹ️ 未新增表情：{state_label}已有 {duplicate_count} 张相同图片，"
                "未写入任何文件。"
            )
        elif stop_note:
            message = (
                f"❌ 未能写入：{state_label}新增 {saved_count} 张，"
                f"重复 {duplicate_count} 张，失败 {failed_count} 张。"
            )
        else:
            message = (
                f"❌ 收录失败：{state_label}新增 {saved_count} 张，"
                f"重复 {duplicate_count} 张，失败 {failed_count} 张。"
            )
        if stop_note:
            message += (
                f"\n⚠️ {stop_note}"
                + (f"，剩余 {unprocessed_count} 张未处理" if unprocessed_count else "")
                + f"；请重新执行 /表情管理 添加表情 [{category}]。"
            )
        for note in refresh_notes:
            message += f"\n⚠️ {note}"
        if saved_count and getattr(self, "img_sync", None):
            message += "\n☁️ 检测到已配置图床，如需同步到云端请使用命令：同步到云端"
        yield event.plain_result(message)

    async def _inject_meme_prompt_impl(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        if not self._scope_allows_llm_origin(event):
            self._remove_meme_tool(req)
            self._remove_meme_tool(req, "send_meme")
            event.set_extra("meme_manager_send_tool_available", False)
            req.system_prompt = self._strip_meme_prompt(req.system_prompt)
            event.set_extra("meme_manager_semantic_active", False)
            event.set_extra("meme_manager_semantic_mode", "")
            return
        tool_set = getattr(req, "func_tool", None)
        get_tool = getattr(tool_set, "get_tool", None)
        message_tool = get_tool("send_message_to_user") if callable(get_tool) else None
        if (
            message_tool is not None
            and getattr(message_tool, "active", True)
            and not isinstance(message_tool, MemeMessageTool)
            and is_builtin_message_tool(message_tool)
        ):
            get_full = getattr(tool_set, "get_full_tool_set", None)
            local_tools = get_full() if callable(get_full) else copy.copy(tool_set)
            local_tools.tools = list(local_tools.tools)
            local_tools.add_tool(MemeMessageTool(message_tool, self))
            req.func_tool = local_tools
        event.set_extra("meme_manager_resolved_persona_id", None)
        try:
            # 与 AstrBot 的会话覆盖和继承默认人格选择保持一致。
            (
                persona_id,
                _,
                _,
                _,
            ) = await self.context.persona_manager.resolve_selected_persona(
                umo=event.unified_msg_origin,
                conversation_persona_id=getattr(req.conversation, "persona_id", None),
                platform_name=event.get_platform_name(),
                provider_settings=self.context.get_config(event.unified_msg_origin).get(
                    "provider_settings", {}
                ),
            )
            event.set_extra("meme_manager_resolved_persona_id", persona_id or "")
        except Exception:
            logger.warning(
                "无法解析表情选择使用的当前人格",
                exc_info=True,
            )
        self._apply_request_prompt(req, event)
        if event.get_extra("meme_manager_semantic_mode") == "tool":
            if event.get_extra("meme_manager_tool_probability_enabled") is None:
                enabled = bool(getattr(self, "tool_probability_enabled", False))
                event.set_extra("meme_manager_tool_probability_enabled", enabled)
                if enabled:
                    event.set_extra(
                        "meme_manager_tool_probability_hit",
                        probability_hit(getattr(self, "tool_call_probability", 100)),
                    )
            if event.get_extra("meme_manager_tool_probability_enabled"):
                await self._prepare_probability_tool_search(event, req)
        tool_set = getattr(req, "func_tool", None)
        get_tool = getattr(tool_set, "get_tool", None)
        send_tool = get_tool("send_meme") if callable(get_tool) else None
        send_tool_available = bool(
            getattr(self, "send_meme_tool_enabled", True)
            and send_tool
            and getattr(send_tool, "active", True)
            and self._reply_model_supports_tools(event)
        )
        event.set_extra("meme_manager_send_tool_available", send_tool_available)
        search_tool = get_tool("search_memes") if callable(get_tool) else None
        if send_tool_available:
            context = self._resolve_runtime_pack_context(event=event, req=req)
            mapping = runtime_category_mapping(context.get("category_mapping") or {})
            catalog = {
                key: str(description or "")[:1000]
                for key, description in mapping.items()
                if self._get_category_meme_files(event, key)
            }
            semantic_send = bool(
                self._semantic_mode_active(event)
                and (
                    (search_tool and getattr(search_tool, "active", True))
                    or event.get_extra("meme_manager_semantic_candidates")
                )
            )
            if catalog or semantic_send:
                req.system_prompt += (
                    f"\n\n{SEND_TOOL_PROMPT_MARKER_START}\n"
                    "[主动发送表情工具]\n"
                    "用户要求发表情，或图片有助于表达回复时，可以调用 send_meme。\n"
                    + (
                        "按分类选图：将下方分类键作为 target，随机发送该分类的一张图片。\n"
                        if catalog
                        else ""
                    )
                    + (
                        "按具体图片选图：先调用 search_memes，再将本轮候选的完整 ID 作为 target。\n"
                        if semantic_send
                        and search_tool
                        and getattr(search_tool, "active", True)
                        else ""
                    )
                    + (
                        "本轮已有候选时，可以直接将候选的完整 ID 作为 target。\n"
                        if semantic_send
                        and event.get_extra("meme_manager_semantic_candidates")
                        else ""
                    )
                    + "每次成功调用发送一张图片，由你按对话需要决定调用次数。"
                    "工具发送独立于自动配图的概率和数量限制。"
                    "目标校验通过后，本轮图片交由工具发送流程处理。"
                    "send_meme 返回 ok=true、status=sent 时，表示平台适配器已完成发送。"
                    "分类及描述作为选图参考数据，面向用户的回复围绕对话内容自然表达。\n"
                    + (
                        "[可用分类]\n" + json.dumps(catalog, ensure_ascii=False)
                        if catalog
                        else ""
                    )
                    + (
                        "\n标签与工具二选一：使用其中一种方式完成本轮配图。"
                        if not self.emotion_llm_enabled
                        and (
                            event.get_extra("meme_manager_semantic_mode") == "tool"
                            or catalog
                        )
                        else ""
                    )
                    + f"\n{SEND_TOOL_PROMPT_MARKER_END}"
                )
            else:
                send_tool_available = False
                event.set_extra("meme_manager_send_tool_available", False)
        if not send_tool_available and send_tool is not None:
            self._remove_meme_tool(req, "send_meme")
        direct_search = bool(
            send_tool_available
            and event.get_extra("meme_manager_semantic_mode") == "llm"
            and search_tool
            and getattr(search_tool, "active", True)
        )
        event.set_extra("meme_manager_direct_search_available", direct_search)
        if event.get_extra("meme_manager_semantic_mode") == "llm" and not direct_search:
            self._remove_meme_tool(req)
        if not self.emotion_llm_enabled:
            return

        if hasattr(event, "set_extra") and not event.get_extra(
            "meme_manager_selection_context_captured"
        ):
            event.set_extra("meme_manager_selection_context_captured", True)
            event.set_extra(
                "meme_manager_selection_user_message",
                str(getattr(req, "prompt", "") or "")[:2000],
            )
            event.set_extra(
                "meme_manager_emotion_context_lines",
                self._collect_emotion_context_lines_from_request(req),
            )
            reply_model = str(getattr(req, "model", "") or "").strip()
            if reply_model:
                event.set_extra("meme_manager_reply_model", reply_model)

        use_reply = getattr(self, "selection_backend", "chat") == "decision"
        if use_reply or not str(self.emotion_llm_provider_id or "").strip():
            # AstrBot 的本轮 Provider/模型覆盖只存在于事件和 ProviderRequest 中。
            # 必须在回复请求发出前缓存，不能仅查询会话默认 Provider。
            await self._resolve_emotion_llm_provider_id(event, use_reply=use_reply)

    async def _prepare_probability_tool_search(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """使用回复提供方与模型尝试一次必需搜索。

        Args:
            event: 保存当前回复决策及已验证候选的事件。
            req: 普通回复请求，仅修改其本地工具集与提示词。
        """
        lock = event.get_extra("meme_manager_tool_probability_lock")
        if lock is None:
            lock = asyncio.Lock()
            event.set_extra("meme_manager_tool_probability_lock", lock)
        async with lock:
            if not event.get_extra("meme_manager_tool_probability_attempted"):
                # 在等待前先登记尝试，即使随后取消也保留状态。
                event.set_extra("meme_manager_tool_probability_attempted", True)
                tool_set = getattr(req, "func_tool", None)
                full_set = getattr(tool_set, "get_full_tool_set", None)
                if callable(full_set):
                    tool_set = full_set()
                tool = tool_set.get_tool("search_memes") if tool_set else None
                self._remove_meme_tool(req)
                result = {"ok": False, "reason": "tool_probability_miss"}
                try:
                    if event.get_extra("meme_manager_tool_probability_hit"):
                        if tool is None or not getattr(tool, "active", True):
                            raise ValueError("search_memes is unavailable")
                        selected = event.get_extra("selected_provider")
                        if isinstance(selected, str) and selected:
                            provider = self.context.get_provider_by_id(selected)
                        else:
                            provider = await self.context.get_using_provider_async(
                                umo=event.unified_msg_origin
                            )
                        if provider is None:
                            raise ValueError("Reply provider is unavailable")
                        if req.image_urls:
                            model_config = (
                                self.context.get_config(event.unified_msg_origin)
                                .get("agent_runner", {})
                                .get("config", {})
                                .get("model", {})
                            )
                            fallback_ids = model_config.get("fallback_provider_ids", [])
                            if fallback_ids:
                                # 本钩子执行前，核心可能已经将图片请求
                                # 路由到备用提供方。复用其选择策略，
                                # 不修改原始事件。
                                try:
                                    (
                                        _get_fallback_chat_providers,
                                        _select_image_chat_provider,
                                    ) = get_image_chat_routing()
                                except ImportError:
                                    # 旧版核心不会执行此图片路由。
                                    pass
                                else:
                                    provider = _select_image_chat_provider(
                                        provider,
                                        req,
                                        _get_fallback_chat_providers(
                                            provider, self.context, fallback_ids
                                        ),
                                    )
                        modalities = provider.provider_config.get("modalities")
                        if (
                            isinstance(modalities, list)
                            and modalities
                            and "tool_use" not in modalities
                        ):
                            raise ValueError("Reply model does not support tools")
                        response = await provider.text_chat(
                            prompt=req.prompt,
                            session_id=req.session_id,
                            contexts=copy.deepcopy(req.contexts),
                            tool_calls_result=copy.deepcopy(req.tool_calls_result),
                            image_urls=list(req.image_urls or []),
                            audio_urls=list(req.audio_urls or []),
                            extra_user_content_parts=copy.deepcopy(
                                req.extra_user_content_parts
                            ),
                            system_prompt=req.system_prompt
                            + (
                                "\n本次仅生成一次 search_memes 调用，不输出回复正文。"
                                "根据对话、人格和准备回复的意图生成简短检索词。"
                            ),
                            func_tool=ToolSet([tool]),
                            model=req.model,
                            tool_choice="required",
                            request_max_retries=0,
                        )
                        names = getattr(response, "tools_call_name", []) or []
                        arguments = getattr(response, "tools_call_args", []) or []
                        # 仅执行声明的第一个调用，即使必需调用返回了重复项，
                        # 或第一个调用的参数无效。
                        if not names or names[0] != "search_memes" or not arguments:
                            raise ValueError(
                                "Required search_memes call was not returned"
                            )
                        args = arguments[0]
                        if (
                            not isinstance(args, dict)
                            or set(args) != {"query"}
                            or not isinstance(args["query"], str)
                            or not args["query"].strip()
                        ):
                            raise ValueError("Invalid search_memes arguments")
                        event.set_extra("meme_manager_tool_probability_running", True)
                        try:
                            # 存在 AstrBot 权限代理时予以保留。
                            if tool.handler is not None:
                                raw_result = await tool.handler(event, **args)
                            else:
                                raw_result = await tool.call(
                                    ContextWrapper(
                                        SimpleNamespace(
                                            context=self.context, event=event
                                        )
                                    ),
                                    **args,
                                )
                            result = json.loads(raw_result)
                            if not isinstance(result, dict):
                                raise ValueError("Invalid search_memes result")
                        finally:
                            event.set_extra(
                                "meme_manager_tool_probability_running", False
                            )
                except asyncio.CancelledError:
                    result = {"ok": False, "reason": "tool_search_cancelled"}
                    event.set_extra("meme_manager_semantic_candidates", {})
                    logger.info("[meme_manager] 按概率触发的工具搜索已取消")
                    raise
                except Exception as exc:
                    result = {"ok": False, "reason": "tool_search_failed"}
                    event.set_extra("meme_manager_semantic_candidates", {})
                    logger.warning("[meme_manager] 已跳过按概率触发的工具搜索：%s", exc)
                finally:
                    event.set_extra("meme_manager_semantic_search_completed", True)
                    if not result.get("ok"):
                        event.set_extra("meme_manager_semantic_candidates", {})
                    event.set_extra(
                        "meme_manager_tool_probability_result",
                        json.dumps(result, ensure_ascii=False),
                    )
                    if not result.get("ok") or not result.get("candidates"):
                        logger.info(
                            "[meme_manager] 按概率触发的工具搜索未选中候选：%s",
                            result.get("reason", "no_candidates"),
                        )
            self._remove_meme_tool(req)
            req.system_prompt = self._strip_meme_prompt(
                req.system_prompt
            ) + self._semantic_system_prompt(
                event.get_extra("meme_manager_tool_probability_result")
            )

    async def _resolve_emotion_llm_provider_id(
        self, event: AstrMessageEvent, *, use_reply: bool = False
    ) -> str:
        """解析已配置的图片提供方，或缓存的实际回复提供方。

        Args:
            event: 包含路由覆盖值的当前回复事件。
            use_reply: 始终使用回复提供方，例如生成检索词时。

        Returns:
            提供方 ID；没有可用聊天提供方时返回空字符串。
        """
        configured_id = (
            "" if use_reply else str(self.emotion_llm_provider_id or "").strip()
        )
        if configured_id:
            return configured_id

        cached_id = str(event.get_extra("meme_manager_reply_provider_id") or "").strip()
        if cached_id:
            return cached_id

        selected_id = str(event.get_extra("selected_provider") or "").strip()
        if selected_id:
            get_provider = getattr(self.context, "get_provider_by_id", None)
            if not callable(get_provider) or get_provider(selected_id) is not None:
                event.set_extra("meme_manager_reply_provider_id", selected_id)
                logger.info(
                    "[meme_manager] 情感模型未单独配置，本轮复用事件指定回复模型: %s",
                    selected_id,
                )
                return selected_id
            logger.warning(
                "[meme_manager] 事件指定的回复模型不存在，回退会话模型: %s",
                selected_id,
            )

        try:
            provider_id = str(
                await self.context.get_current_chat_provider_id(
                    umo=event.unified_msg_origin
                )
                or ""
            ).strip()
        except Exception as exc:
            logger.warning(
                "[meme_manager] 情感模型未配置，且无法取得当前回复模型: %s",
                exc,
            )
            return ""

        if not provider_id:
            logger.warning("[meme_manager] 情感模型未配置，当前回复模型也不可用")
            return ""

        event.set_extra("meme_manager_reply_provider_id", provider_id)
        logger.info(
            "[meme_manager] 情感模型未单独配置，本轮复用会话回复模型: %s",
            provider_id,
        )
        return provider_id

    async def _resp_semantic_llm_impl(
        self, event: AstrMessageEvent, response: LLMResponse, text: str
    ):
        """检索候选，并由配置的回复后后端选择。

        Args:
            event: 持有已验证候选绑定的当前回复事件。
            response: 将原地清理文本的回复响应。
            text: 用于检索与选择的完整回复。
        """
        parser = self._make_meme_parser((), semantic=True)
        clean_text = parser.feed(text) + parser.finish()
        if event.get_extra("meme_manager_direct_send_attempted"):
            return await self._resp_semantic_impl(event, response, clean_text)
        visible_text = extract_visible_semantic_reply(clean_text)
        query = await self._build_emotion_semantic_query(event, visible_text)
        pack_context = self._resolve_runtime_pack_context(event=event)
        pack_ids = pack_context.get("pack_ids", [])
        selected_id = ""
        try:
            if ",".join(pack_ids) != str(
                event.get_extra("meme_manager_semantic_verified_pack_ids") or ""
            ):
                return await self._resp_semantic_impl(event, response, clean_text)
            pack_contexts = [
                {"pack_id": pack_id, "pack_dir": get_pack_paths(pack_id)["pack_dir"]}
                for pack_id in pack_ids
            ]
            result = await search_memes_multi_pack(
                pack_contexts,
                PLUGIN_DATA_DIR,
                query,
                self._resolve_embedding_provider,
                top_k=self.semantic_top_k,
                min_score=self.semantic_min_score,
            )
            candidates = result.get("candidates") or []
            records = candidate_records_multi_pack(pack_contexts, candidates)
            remember_candidates(event, records)
            event.set_extra("meme_manager_semantic_query", query)
            if candidates:
                visible_candidates = [
                    {
                        "id": item.get("id"),
                        "caption": item.get("caption", ""),
                        "tags": item.get("tags") or [],
                    }
                    for item in candidates
                ]
                selection = await select_candidates(
                    SelectionTask(
                        kind="semantic",
                        reply=visible_text,
                        candidates=visible_candidates,
                        reference=self._build_emotion_llm_injection_suffix(event),
                        quantity_prompt=self._build_meme_quantity_prompt(),
                    ),
                    backend=event.get_extra("meme_manager_selection_backend")
                    or getattr(self, "selection_backend", "chat"),
                    generate=lambda prompt: self._emotion_llm_generate(event, prompt),
                    decision_client=getattr(self, "decision_client", None),
                )
                event.set_extra("meme_manager_selection_result", selection)
                if selection.status == "selected" and selection.selected_ids:
                    selected_id = selection.selected_ids[0]
                logger.info(
                    "[meme_manager] 选图完成：状态=%s 后端=%s 原因=%s",
                    selection.status,
                    selection.backend,
                    selection.reason,
                )
        except Exception as exc:
            logger.error(
                "[meme_manager] 情感辅助语义检索失败：%s",
                exc,
                exc_info=True,
            )

        marked_text = clean_text + (f"\n&&{selected_id}&&" if selected_id else "")
        return await self._resp_semantic_impl(event, response, marked_text)

    async def _build_emotion_semantic_query(
        self, event: AstrMessageEvent, visible_text: str
    ) -> str:
        """使用配置的文本来源与聊天提供方构造查询。

        Args:
            event: 包含实际聊天提供方及模型的回复事件。
            visible_text: 需要检索配图的干净可见回复。

        Returns:
            长度受限的查询，生成失败时回退到可见文本。
        """
        explicit_provider = getattr(self, "semantic_query_provider_id", "")
        decision_backend = (
            event.get_extra("meme_manager_selection_backend")
            or getattr(self, "selection_backend", "chat")
        ) == "decision"

        async def generate_query(prompt):
            """通过明确指定或继承的聊天模型生成搜索文本。

            Args:
                prompt: 固定的查询生成指令与参考数据。

            Returns:
                聊天响应；无法解析提供方时返回 None。
            """
            if not explicit_provider and not decision_backend:
                return await self._emotion_llm_generate(event, prompt)
            provider_id = (
                explicit_provider
                or await self._resolve_emotion_llm_provider_id(event, use_reply=True)
            )
            if not provider_id:
                return None
            kwargs = {"chat_provider_id": provider_id, "prompt": prompt}
            if not explicit_provider:
                model = self._resolve_emotion_llm_model(event, use_reply=True)
                if model:
                    kwargs["model"] = model
            return await self.context.llm_generate(**kwargs)

        return await build_semantic_query(
            visible_text,
            source=getattr(self, "semantic_query_source", "generated"),
            reference=self._build_emotion_llm_injection_suffix(event),
            generate=generate_query,
        )

    async def _resp_impl(self, event: AstrMessageEvent, response: LLMResponse):
        """处理 LLM 响应，识别表情"""

        if not self._scope_allows_llm_origin(event):
            return
        if not response or not response.completion_text:
            return
        if (
            getattr(response, "role", "assistant") != "assistant"
            or getattr(response, "is_chunk", False)
            or getattr(response, "tools_call_name", None)
        ):
            return

        text = response.completion_text
        if not event.get_extra("meme_manager_outgoing_processing") and str(
            getattr(event, "unified_msg_origin", "")
        ) in (event.get_extra("meme_manager_tool_message_targets") or ()):
            response.completion_text = self._clean_outgoing_plain_text(text).strip()
            event.set_extra("found_emotions", [])
            event.set_extra("meme_manager_semantic_selected_ids", [])
            return
        cleaned_text = strip_internal_image_ref_lines(text)
        if cleaned_text != text:
            response.completion_text = cleaned_text
            text = cleaned_text
            logger.debug("[meme_manager] 已从回复中移除内部图片引用")
        # 语义表情包仅使用一种互斥路径：回复模型工具调用或情感辅助候选选择。
        # 两条路径都不会使用旧版猜测逻辑。
        semantic_mode = (
            str(event.get_extra("meme_manager_semantic_mode") or "")
            if self._semantic_mode_active(event)
            else ""
        )
        if bool(event.get_extra("meme_manager_semantic_response_processed")):
            logger.info("[meme_manager] 回复已处理，忽略重复钩子")
            return
        # 在任何异步工作前占用本次响应，防止重复钩子覆盖语义选择结果或
        # 旧版分类匹配结果。
        event.set_extra("meme_manager_semantic_response_processed", True)
        if semantic_mode == "llm":
            return await self._resp_semantic_llm_impl(event, response, text)
        if semantic_mode == "tool":
            return await self._resp_semantic_impl(event, response, text)

        pack_context = self._resolve_runtime_pack_context(event=event)
        context_mapping = pack_context.get("category_mapping")
        active_category_mapping = (
            runtime_category_mapping(context_mapping)
            if isinstance(context_mapping, dict)
            else runtime_category_mapping(self.category_mapping)
        )

        found_emotions: list[str] = []
        valid_emoticons = set(active_category_mapping.keys())

        parser = self._make_meme_parser(valid_emoticons)
        clean_text = parser.feed(text) + parser.finish()
        found_emotions = parser.selections

        if event.get_extra("meme_manager_direct_send_attempted"):
            found_emotions = []
        elif self.emotion_llm_enabled:
            # 仅由回复后选择器决定选择结果，包括主动放弃。
            found_emotions = []
            try:
                category_catalog = [
                    {
                        "id": tag,
                        "description": str(active_category_mapping.get(tag) or ""),
                    }
                    for tag in sorted(valid_emoticons)
                    if self._get_category_meme_files(event, tag)
                ]
                selection = await select_candidates(
                    SelectionTask(
                        kind="category",
                        reply=extract_visible_semantic_reply(clean_text),
                        candidates=category_catalog,
                        reference=self._build_emotion_llm_injection_suffix(event),
                        quantity_prompt=self._build_meme_quantity_prompt(),
                    ),
                    backend=event.get_extra("meme_manager_selection_backend")
                    or getattr(self, "selection_backend", "chat"),
                    generate=lambda prompt: self._emotion_llm_generate(event, prompt),
                    decision_client=getattr(self, "decision_client", None),
                )
                event.set_extra("meme_manager_selection_result", selection)
                if selection.status == "selected":
                    found_emotions = selection.selected_ids
                logger.info(
                    "[meme_manager] 选图完成：状态=%s 后端=%s 原因=%s",
                    selection.status,
                    selection.backend,
                    selection.reason,
                )
            except Exception as e:
                logger.error(f"[meme_manager] 情感模型调用失败: {e}")

        # 按模型选择的顺序保留分类，每个分类只保留一次。
        filtered_emotions = self._filter_emotion_selection(found_emotions)

        event.set_extra("found_emotions", filtered_emotions)
        logger.info(f"[meme_manager] 去重后的最终表情列表: {filtered_emotions}")

        if getattr(self, "filter_all_tags", False):
            clean_text = self._clean_outgoing_plain_text(clean_text)
        response.completion_text = clean_text.strip()
        if filtered_emotions and not response.completion_text:
            # 保留消息链，让 AstrBot 在空消息正文下仍进入装饰阶段；
            # 选中的图片会在该阶段成为回复。
            if response.result_chain is None:
                response.result_chain = MessageChain([Plain("")])
            event.set_extra("meme_manager_image_only_reply", True)
            # 此图片就是回复本身，而不是可选的文本附件。
            # 纯图片回复与文字回复使用相同的投递门控。
        logger.debug(
            f"[meme_manager] 清理后的最终文本内容长度: {len(response.completion_text)}"
        )

        # webchat 流式场景：在 "complete" 入队前发送干净文本，替换客户端已显示的含标记脏文本
        result = event.get_result()
        if (
            event.get_platform_name() == "webchat"
            and not event.get_extra("meme_manager_stream_filtered")
            and result is not None
            and result.result_content_type == ResultContentType.STREAMING_RESULT
        ):
            try:
                await event.send(MessageChain([Plain(response.completion_text)]))
                logger.debug("[meme_manager] webchat 流式文本已替换为干净版本")
            except Exception as e:
                logger.error(f"[meme_manager] webchat 流式文本替换失败: {e}")

    async def _resp_semantic_impl(
        self, event: AstrMessageEvent, response: LLMResponse, text: str
    ):
        """清理语义图片标记并记录本轮经过候选校验的精确 ID。"""
        pack_context = self._resolve_runtime_pack_context(event=event)
        selected_ids: list[str] = []

        parser = self._make_meme_parser((), semantic=True)
        clean_text = parser.feed(text or "") + parser.finish()
        referenced_ids = parser.selections
        for value in referenced_ids:
            if validate_selected_id(event, value, pack_context):
                if value not in selected_ids:
                    selected_ids.append(value)
            else:
                logger.warning("忽略不在本轮候选中的语义图片 ID: %s", value)
        if selected_ids:
            logger.info("[meme_manager] 已选择语义表情：%s", selected_ids)
        else:
            logger.debug("[meme_manager] 本次回复未选择语义表情")
        event.set_extra("meme_manager_semantic_selected_ids", selected_ids)
        event.set_extra("found_emotions", None)
        if getattr(self, "filter_all_tags", False):
            clean_text = self._clean_outgoing_plain_text(clean_text)
        response.completion_text = clean_text.strip()
        if selected_ids and not response.completion_text:
            if response.result_chain is None:
                response.result_chain = MessageChain([Plain("")])
            event.set_extra("meme_manager_image_only_reply", True)
        result = event.get_result()
        if (
            event.get_platform_name() == "webchat"
            and not event.get_extra("meme_manager_stream_filtered")
            and result is not None
            and result.result_content_type == ResultContentType.STREAMING_RESULT
        ):
            try:
                await event.send(MessageChain([Plain(response.completion_text)]))
            except Exception as exc:
                logger.error("webchat 语义文本替换失败: %s", exc)

    async def _on_decorating_result_impl(self, event: AstrMessageEvent):
        """在消息发送前清理文本中的表情标签，并添加表情图片"""
        logger.debug("[meme_manager] on_decorating_result 开始处理")

        result = event.get_result()
        if not result:
            return

        if result.result_content_type == ResultContentType.STREAMING_RESULT:
            return

        try:
            # 第一步：获取并清理原始消息链中的文本
            original_chain = result.chain
            cleaned_components = []

            if original_chain:
                for component in self._normalize_outgoing_message_components(
                    original_chain
                ):
                    if isinstance(component, Plain):
                        cleaned = self._clean_outgoing_plain_text(component.text)
                        if cleaned.strip():
                            cleaned_components.append(Plain(cleaned.strip()))
                    else:
                        cleaned_components.append(component)

            # 流式结果同样需要最后一道 Plain 文本清理。
            if result.result_content_type == ResultContentType.STREAMING_FINISH:
                if isinstance(original_chain, (str, MessageChain, list)):
                    result.chain = cleaned_components
                if (
                    self.streaming_compatibility
                    or event.get_platform_name() == "webchat"
                    or event.get_extra("meme_manager_image_only_reply")
                ):
                    await self._send_memes_streaming(event)
                return

            if self._should_attach_for_result(event, result):
                ids = event.get_extra("meme_manager_semantic_selected_ids") or []
                emotions = event.get_extra("found_emotions") or []
                images, files = await self._build_selected_meme_images(
                    event, ids or emotions, semantic=bool(ids)
                )
                if images:
                    mixed = not cleaned_components or (
                        self.enable_mixed_message
                        and random.randint(1, 100) <= self.mixed_message_probability
                    )
                    if mixed:
                        cleaned_components = self._merge_components_with_images(
                            cleaned_components, images
                        )
                    else:
                        event.set_extra("meme_manager_pending_images", images)

            # 清空当前事件已处理的表情列表
            event.set_extra("found_emotions", None)
            event.set_extra("meme_manager_semantic_selected_ids", None)

            # 第三步：更新消息链
            if isinstance(original_chain, (str, MessageChain, list)):
                # 始终写回安全处理后的组件列表，空列表也不能回退到原始脏文本。
                result.chain = cleaned_components

            logger.debug("[meme_manager] on_decorating_result 处理完成")

        except Exception as e:
            logger.exception("处理消息装饰失败：%s", e)

    @filter.after_message_sent()
    async def _after_message_sent_impl(self, event: AstrMessageEvent):
        """消息发送后处理。用于发送未混合的表情图片。"""
        pending_images = event.get_extra("meme_manager_pending_images")

        try:
            if pending_images:
                for image in pending_images:
                    await self._send_meme_image(event, image)
        except Exception as e:
            logger.exception("发送表情图片失败：%s", e)
        finally:
            event.set_extra("meme_manager_pending_images", None)

            # 清理临时文件
            temp_files = event.get_extra("meme_manager_temp_files")
            if temp_files:
                for temp_file in temp_files:
                    try:
                        if os.path.exists(temp_file):
                            os.remove(temp_file)
                            logger.debug(f"[meme_manager] 已清理临时文件: {temp_file}")
                    except Exception as e:
                        logger.error(f"[meme_manager] 清理临时文件失败: {e}")
                event.set_extra("meme_manager_temp_files", None)

    # 辅助方法
    def _convert_to_gif(self, image_path: str) -> str:
        """将静态图片转换为独占的临时 GIF。

        Args:
            image_path: 启用转换时待转换的来源图片。

        Returns:
            由调用方负责管理的新 GIF 路径；跳过或转换失败时
            返回来源路径。调用方在投递后负责清理。
        """
        if not self.convert_static_to_gif:
            return image_path

        if image_path.lower().endswith(".gif"):
            return image_path

        temp_filename = None
        try:
            with PILImage.open(image_path) as img:
                # 检查是否已经是 GIF (虽然后缀不是 .gif，但内容可能是)
                if img.format == "GIF":
                    return image_path

                # 转换为 RGB (如果是 RGBA 需要处理透明度)
                if img.mode in ("RGBA", "LA") or (
                    img.mode == "P" and "transparency" in img.info
                ):
                    # 创建白色背景
                    background = PILImage.new("RGB", img.size, (255, 255, 255))
                    if img.mode == "P":
                        img = img.convert("RGBA")
                    background.paste(img, mask=img.getchannel("A"))
                    img = background
                else:
                    img = img.convert("RGB")

                temp_dir = Path(get_astrbot_temp_path())
                temp_dir.mkdir(parents=True, exist_ok=True)
                fd, temp_filename = tempfile.mkstemp(
                    prefix="meme_", suffix=".gif", dir=temp_dir
                )
                # 通过自己持有的句柄写入，Windows 上会在投递前关闭句柄。
                with os.fdopen(fd, "wb") as output:
                    img.save(output, "GIF")
                logger.debug("[meme_manager] 已将静态图片转换为 GIF：%s", temp_filename)
                return temp_filename
        except Exception as e:
            if temp_filename is not None:
                Path(temp_filename).unlink(missing_ok=True)
            logger.error("[meme_manager] GIF 转换失败：%s", e)
            return image_path

    async def _send_memes_streaming(
        self, event: AstrMessageEvent, *, stream_completed: bool = False
    ):
        """流式回复完成后发送所选表情。

        Args:
            event: 包含当前回复已校验选择结果的事件。
            stream_completed: 适配器是否已完成对耗尽流的处理。
                跳过结果类型检查，但仍保留来源门控。
        """
        if not self._scope_allows_llm_origin(event) or (
            not stream_completed
            and not self._should_attach_for_result(event, event.get_result())
        ):
            event.set_extra("meme_manager_semantic_selected_ids", None)
            event.set_extra("found_emotions", None)
            return
        if not self._meme_send_allowed(event):
            event.set_extra("meme_manager_semantic_selected_ids", None)
            event.set_extra("found_emotions", None)
            return
        semantic_selected_ids = (
            event.get_extra("meme_manager_semantic_selected_ids") or []
        )
        if semantic_selected_ids and self._semantic_mode_active(event):
            pack_context = self._resolve_runtime_pack_context(event=event)
            try:
                for selected_id in self._limit_meme_selection(
                    semantic_selected_ids, event
                ):
                    image_path = validate_selected_id(event, selected_id, pack_context)
                    if image_path is None:
                        continue
                    final_path = await asyncio.to_thread(
                        self._convert_to_gif, str(image_path)
                    )
                    try:
                        image = Image.fromFileSystem(final_path)
                        self._track_prepared_meme(
                            event, image, image_path, candidate_id=selected_id
                        )
                        await self._send_meme_image(event, image)
                    finally:
                        if final_path != str(image_path) and os.path.exists(final_path):
                            os.remove(final_path)
            except Exception as exc:
                logger.error("流式语义表情发送失败: %s", exc, exc_info=True)
            finally:
                event.set_extra("meme_manager_semantic_selected_ids", None)
            return
        found_emotions = event.get_extra("found_emotions") or []
        if not found_emotions:
            return

        try:
            for emotion in self._limit_meme_selection(found_emotions, event):
                memes = self._get_category_meme_files(event, emotion)
                if not memes:
                    continue
                meme_file = str(random.choice(memes))
                final_meme_file = await asyncio.to_thread(
                    self._convert_to_gif, meme_file
                )

                try:
                    image = Image.fromFileSystem(final_meme_file)
                    self._track_prepared_meme(event, image, meme_file, category=emotion)
                    await self._send_meme_image(event, image)
                except Exception as e:
                    logger.error(f"[meme_manager] 流式模式发送表情失败: {e}")
                finally:
                    # 清理临时文件
                    if final_meme_file != meme_file and os.path.exists(final_meme_file):
                        try:
                            os.remove(final_meme_file)
                        except Exception:
                            pass
        except Exception as e:
            logger.exception("[meme_manager] 流式模式处理表情失败：%s", e)
        finally:
            event.set_extra("found_emotions", None)

    async def _send_meme_tool_impl(self, event: AstrMessageEvent, target: str) -> str:
        """独立于门控发送一个分类或已验证的语义候选。

        Args:
            event: 工具已通过请求过滤的当前聊天事件。
            target: 确切的绑定分类，或本轮检索到的候选 ID。

        Returns:
            JSON，表示适配器已确认的发送结果或稳定失败原因。

        Raises:
            asyncio.CancelledError: 所属模型或工具请求已被取消。
        """
        if not getattr(self, "send_meme_tool_enabled", True):
            return json.dumps({"ok": False, "reason": "tool_disabled"})
        if not self._scope_allows_llm_origin(event) or not event.get_extra(
            "meme_manager_send_tool_available"
        ):
            return json.dumps({"ok": False, "reason": "tool_unavailable"})
        if (
            str(event.unified_msg_origin)
            in (event.get_extra("meme_manager_tool_tag_targets") or ())
            or event.get_extra("meme_manager_tag_image_prepared")
            or event.get_extra("found_emotions")
            or event.get_extra("meme_manager_semantic_selected_ids")
        ):
            return json.dumps({"ok": False, "reason": "tag_delivery_selected"})
        if not isinstance(target, str) or not target:
            return json.dumps(
                {
                    "ok": False,
                    "reason": "invalid_target",
                    "instruction": "请将当前可用的分类键或本轮候选的完整 ID 作为 target。",
                }
            )
        context = self._resolve_runtime_pack_context(event=event)
        semantic = target.startswith("meme:")
        if semantic:
            if not self._semantic_mode_active(event):
                return json.dumps({"ok": False, "reason": "semantic_unavailable"})
            source = validate_selected_id(event, target, context)
            if source is None:
                if event.get_extra("meme_manager_semantic_candidates"):
                    instruction = (
                        "请从本轮搜索结果中选择候选，并将其完整 ID 作为 target。"
                    )
                elif event.get_extra("meme_manager_semantic_search_completed"):
                    instruction = "本轮搜索已完成，可以继续纯文字回复。"
                elif event.get_extra(
                    "meme_manager_semantic_mode"
                ) == "tool" or event.get_extra("meme_manager_direct_search_available"):
                    instruction = "请调用 search_memes 获取本轮候选，再使用选中候选的完整 ID 发送。"
                else:
                    instruction = (
                        "请从当前可用分类中选择分类键作为 target，或继续纯文字回复。"
                    )
                return json.dumps(
                    {
                        "ok": False,
                        "reason": "invalid_candidate",
                        "instruction": instruction,
                    }
                )
            category = source.parent.name
        else:
            mapping = runtime_category_mapping(context.get("category_mapping") or {})
            if target not in mapping:
                return json.dumps(
                    {
                        "ok": False,
                        "reason": "invalid_category",
                        "instruction": "请从当前可用分类中选择分类键作为 target。",
                    }
                )
            category = target
            files = self._get_category_meme_files(event, category)
            if not files:
                return json.dumps({"ok": False, "reason": "no_images"})
            source = random.choice(files)
        # 目标校验通过后，登记自动附图的处理归属。
        # 投递失败时仍保留此归属，因为适配器的实际结果可能不确定。
        event.set_extra("meme_manager_direct_send_attempted", True)
        final_path = str(source)
        conversion = None
        try:
            conversion = asyncio.create_task(
                asyncio.to_thread(self._convert_to_gif, str(source))
            )
            final_path = await asyncio.shield(conversion)
            current = self._resolve_runtime_pack_context(event=event)
            if current.get("pack_ids") != context.get("pack_ids") or (
                validate_selected_id(event, target, current) != source
                if semantic
                else source not in self._get_category_meme_files(event, category)
            ):
                return json.dumps({"ok": False, "reason": "candidate_changed"})
            image = Image.fromFileSystem(final_path)
            self._track_prepared_meme(
                event,
                image,
                source,
                category=category,
                candidate_id=target if semantic else "",
            )
            await self._send_meme_image(event, image)
            return json.dumps({"ok": True, "status": "sent", "sent_count": 1})
        except asyncio.CancelledError:
            # 取消等待无法停止转换线程；允许本次工具调用完成取消前，
            # 先回收转换输出。
            if conversion is not None:
                while not conversion.done():
                    try:
                        await asyncio.shield(conversion)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not conversion.cancelled():
                    try:
                        final_path = conversion.result()
                    except Exception:
                        pass
            raise
        except Exception:
            logger.exception("[meme_manager] 主动表情工具投递失败")
            return json.dumps({"ok": False, "reason": "send_failed"})
        finally:
            if final_path != str(source):
                try:
                    Path(final_path).unlink(missing_ok=True)
                except OSError:
                    logger.warning("[meme_manager] 主动表情工具清理失败")

    async def _send_meme_image(self, event: AstrMessageEvent, image: Image) -> None:
        original = image
        image = await self._ensure_image_send_format(image)
        self._alias_prepared_meme(event, original, image)
        platform_name = event.get_platform_name()
        if platform_name in {"gewechat", "webchat"}:
            await event.send(MessageChain([image]))
            await self._confirm_meme_delivery(event, [image])
            return
        if (
            platform_name == "qq_official"
            and event.get_message_type() == MessageType.GROUP_MESSAGE
        ):
            raw_message = getattr(
                getattr(event, "message_obj", None), "raw_message", None
            )
            try:
                from botpy.message import GroupMessage
            except ImportError:
                pass
            else:
                # QQ 群图片回复必须复用触发消息的上下文。
                if isinstance(raw_message, GroupMessage):
                    lock = event.get_extra("meme_manager_qq_image_send_lock")
                    if lock is None:
                        lock = asyncio.Lock()
                        event.set_extra("meme_manager_qq_image_send_lock", lock)
                    async with lock:
                        # QQ 的流式与普通发送共用 send_buffer，因此将待发送的
                        # 流式文本与本次即时图片分开处理。
                        has_buffer = hasattr(event, "send_buffer")
                        buffered = getattr(event, "send_buffer", None)
                        try:
                            await event.send(MessageChain([image]))
                        finally:
                            if has_buffer:
                                event.send_buffer = buffered
                    await self._confirm_meme_delivery(event, [image])
                    return
        sent = await self.context.send_message(
            event.unified_msg_origin, MessageChain([image])
        )
        if sent is False:
            raise RuntimeError("No matching platform for meme delivery")
        await self._confirm_meme_delivery(event, [image])

    async def _ensure_image_send_format(self, image: Image) -> Image:
        """根据配置规范图片发送格式。"""
        if not self.send_image_as_base64:
            return image

        image_ref = image.file or image.url or ""
        if isinstance(image_ref, str) and image_ref.startswith("base64://"):
            return image

        try:
            base64_data = await image.convert_to_base64()
            if not base64_data:
                return image
            return Image.fromBase64(base64_data)
        except Exception as e:
            logger.error(f"[meme_manager] 转换图片为 base64 失败: {e}")
            return image

    def _merge_components_with_images(self, components, images):
        """将表情图片与文本组件智能配对，支持分段回复

        Args:
            components: 清理后的消息组件列表
            images: 表情图片列表

        Returns:
            合并后的消息组件列表，图片会合理地分布在文本中
        """
        logger.debug(
            f"[meme_manager] _merge_components_with_images 输入: 组件总数={len(components)}, 图片总数={len(images)}"
        )

        if not images:
            return components

        if not components:
            # 没有文本组件，只发送图片
            return images

        # 找到所有 Plain 组件的索引
        plain_indices = [
            i for i, comp in enumerate(components) if isinstance(comp, Plain)
        ]
        logger.debug(f"[meme_manager] Plain 组件的索引位置列表: {plain_indices}")

        if not plain_indices:
            # 没有 Plain 组件，直接添加图片到末尾
            return components + images

        # 策略：将图片均匀分布在文本组件中，优先在文本后添加图片
        # 这样在分段回复时，图片更容易和对应的文本一起发送
        merged_components = components.copy()
        images_per_text = max(
            1, len(images) // len(plain_indices)
        )  # 每个文本至少配一张图片
        image_index = 0
        images_inserted_so_far = 0  # 跟踪已插入的图片数量

        for idx, plain_idx in enumerate(plain_indices):
            if image_index >= len(images):
                break

            # 计算这个文本应该配多少张图片
            if idx == len(plain_indices) - 1:
                # 最后一个文本组件，分配所有剩余图片
                images_for_this_text = len(images) - image_index
            else:
                images_for_this_text = min(images_per_text, len(images) - image_index)

            logger.debug(
                f"[meme_manager] Plain 组件 {idx} (索引={plain_idx}) 分配的图片数量: {images_for_this_text}"
            )

            # 在这个文本组件后插入图片
            # 注意：plain_idx 是在原始 components 中的位置，但由于我们已经插入了一些图片，
            # 需要考虑已插入图片对当前位置的影响
            insert_pos = plain_idx + 1 + images_inserted_so_far

            for _ in range(images_for_this_text):
                if image_index < len(images):
                    merged_components.insert(insert_pos, images[image_index])
                    image_index += 1
                    insert_pos += 1
                    images_inserted_so_far += 1

        logger.debug(
            f"[meme_manager] 合并前组件总数: {len(components)}, 合并后组件总数: {len(merged_components)}"
        )

        return merged_components
