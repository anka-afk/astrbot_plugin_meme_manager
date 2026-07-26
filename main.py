import asyncio
import re
from pathlib import Path

from astrbot.api import llm_tool, logger
from astrbot.api.all import *
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import *
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star

from .backend.category_manager import CategoryManager
from .backend.semantic_index import EmbeddingAdapter, index_is_ready
from .backend.semantic_models import runtime_category_mapping
from .backend.semantic_query import (
    candidate_records,
    dumps_result,
    remember_candidates,
    search_memes,
)
from .backend.semantic_storage import load_metadata, semantic_metadata_is_complete
from .backend.semantic_task import SemanticTaskManager
from .config import (
    DEFAULT_CATEGORY_DESCRIPTIONS,
    MEMES_DATA_PATH,
    MEMES_DIR,
    PLUGIN_DATA_DIR,
)
from .image_host.img_sync import ImageSync
from .init import init_plugin
from .mixins.commands import CommandMixin
from .mixins.event_handlers import EventHandlerMixin
from .mixins.web_api import WebAPIMixin
from .utils import dict_to_string, load_json, normalize_probability, probability_hit

MEME_PROMPT_MARKER_START = "<!-- meme_manager_prompt:start -->"
MEME_PROMPT_MARKER_END = "<!-- meme_manager_prompt:end -->"
SEMANTIC_PROMPT_MARKER_START = "<!-- meme_manager_semantic_prompt:start -->"
SEMANTIC_PROMPT_MARKER_END = "<!-- meme_manager_semantic_prompt:end -->"
PLUGIN_NAME = "meme_manager"
WEBUI_LOG_PREFIX = f"[{PLUGIN_NAME}][WebUI]"


class MemeSender(Star, WebAPIMixin, CommandMixin, EventHandlerMixin):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self._normalize_mixin_handler_module_paths()
        self.config = config or {}

        # 语义任务管理器只负责在实际操作时调用模型；缺少模型不会阻止旧版插件启动。
        self.semantic_enabled = bool(
            self._read_config_value(
                ("semantic", "enabled"),
                default=False,
                legacy_keys=("semantic_enabled",),
            )
        )
        self.semantic_vision_provider_id = str(
            self._read_config_value(
                ("semantic", "vision_provider_id"),
                default="",
                legacy_keys=("vision_provider_id",),
            )
            or ""
        )
        self.semantic_embedding_provider_id = str(
            self._read_config_value(
                ("semantic", "embedding_provider_id"),
                default="",
                legacy_keys=("embedding_provider_id",),
            )
            or ""
        )
        self.semantic_top_k = int(
            self._read_config_value(("semantic", "top_k"), default=5) or 5
        )
        self.semantic_min_score = float(
            self._read_config_value(("semantic", "min_score"), default=0.25) or 0.25
        )
        self.semantic_task_manager = SemanticTaskManager(
            PLUGIN_DATA_DIR,
            context=context,
            config={
                "vision_provider_id": self.semantic_vision_provider_id,
                "embedding_provider_id": self.semantic_embedding_provider_id,
            },
        )

        # 初始化插件
        if not init_plugin():
            raise RuntimeError("插件初始化失败")

        # 初始化类别管理器
        self.category_manager = CategoryManager()

        # 图床初始化
        self.img_sync = None
        self.img_sync_config = None
        self.img_sync_provider_type = None
        self._img_sync_pack_id = ""
        self._last_img_host_sync_task_status = None
        image_host_type = self._get_image_host_type()
        webdav_config = self._get_webdav_config()
        if image_host_type == "stardots" and self._has_required_config(
            webdav_config, ["url", "username", "password"]
        ):
            image_host_type = "webdav"
            logger.info("检测到完整 WebDAV 配置，自动启用 WebDAV 图床。")

        if image_host_type == "stardots":
            stardots_config = self._get_provider_config("stardots")
            if stardots_config.get("key") and stardots_config.get("secret"):
                stardots_config["provider"] = "stardots"
                self.img_sync_config = {
                    "key": stardots_config["key"],
                    "secret": stardots_config["secret"],
                    "space": stardots_config.get("space", "memes"),
                    "list_cache_ttl": stardots_config.get("list_cache_ttl", 60),
                    "provider": "stardots",
                }
                self.img_sync_provider_type = "stardots"
        elif image_host_type == "cloudflare_r2":
            r2_config = self._get_provider_config("cloudflare_r2")
            required_fields = [
                "account_id",
                "access_key_id",
                "secret_access_key",
                "bucket_name",
            ]
            if all(r2_config.get(field) for field in required_fields):
                if r2_config.get("public_url"):
                    r2_config["public_url"] = r2_config["public_url"].rstrip("/")
                r2_config["provider"] = "cloudflare_r2"
                self.img_sync_config = dict(r2_config)
                self.img_sync_provider_type = "cloudflare_r2"
                self._r2_bucket_name = r2_config.get("bucket_name")
        elif image_host_type == "webdav":
            required_fields = ["url", "username", "password"]
            if all(webdav_config.get(field) for field in required_fields):
                if webdav_config.get("url"):
                    webdav_config["url"] = str(webdav_config["url"]).rstrip("/")
                if webdav_config.get("public_url"):
                    webdav_config["public_url"] = str(
                        webdav_config["public_url"]
                    ).rstrip("/")
                webdav_config["provider"] = "webdav"
                self.img_sync_config = dict(webdav_config)
                self.img_sync_provider_type = "webdav"
                self._webdav_url = webdav_config.get("url")
            else:
                missing_fields = [
                    field for field in required_fields if not webdav_config.get(field)
                ]
                logger.warning(
                    "WebDAV 图床未初始化，缺少必要配置项: %s。当前已读取字段: %s",
                    ", ".join(missing_fields),
                    ", ".join(sorted(webdav_config.keys())) or "无",
                )

        # 图床客户端按当前默认/目标表情包动态构建，避免固定绑定插件启动时目录。
        if self.img_sync_config and self.img_sync_provider_type:
            self._ensure_img_sync_for_pack()

        # 上传与待发送状态
        self.upload_states = {}
        self.pending_images = {}

        # 配置项
        self.prompt_head = self._read_config_value(
            ("generation", "prompt", "head"),
            default="",
            legacy_paths=(("prompt", "prompt_head"),),
        )
        self.prompt_tail_1 = self._read_config_value(
            ("generation", "prompt", "tail_before_limit"),
            default="",
            legacy_paths=(("prompt", "prompt_tail_1"),),
        )
        self.prompt_tail_2 = self._read_config_value(
            ("generation", "prompt", "tail_after_limit"),
            default="",
            legacy_paths=(("prompt", "prompt_tail_2"),),
        )
        self.max_emotions_per_message = self._read_config_value(
            ("generation", "emotion", "max_per_message"),
            default=2,
            legacy_keys=("max_emotions_per_message",),
        )
        self.emotions_probability = self._read_config_value(
            ("generation", "emotion", "probability"),
            default=50,
            legacy_keys=("emotions_probability",),
        )
        self.strict_max_emotions_per_message = self._read_config_value(
            ("generation", "emotion", "strict_max_per_message"),
            default=True,
            legacy_keys=("strict_max_emotions_per_message",),
        )
        self.emotion_llm_enabled = self._read_config_value(
            ("generation", "emotion", "llm", "enabled"),
            default=False,
            legacy_keys=("emotion_llm_enabled",),
        )
        self.emotion_llm_provider_id = self._read_config_value(
            ("generation", "emotion", "llm", "provider_id"),
            default="",
            legacy_keys=("emotion_llm_provider_id",),
        )
        emotion_llm_context_turns = self._read_config_value(
            ("generation", "emotion", "llm", "context_turns"),
            default=0,
            legacy_keys=("emotion_llm_context_turns",),
        )
        try:
            emotion_llm_context_turns = int(emotion_llm_context_turns)
        except (TypeError, ValueError):
            emotion_llm_context_turns = 0
        self.emotion_llm_context_turns = max(0, min(20, emotion_llm_context_turns))
        self.emotion_llm_inject_persona = bool(
            self._read_config_value(
                ("generation", "emotion", "llm", "inject_persona"),
                default=False,
                legacy_keys=("emotion_llm_inject_persona",),
            )
        )
        self.enable_mixed_message = self._read_config_value(
            ("generation", "message", "enable_mixed"),
            default=False,
            legacy_keys=("enable_mixed_message",),
        )
        self.mixed_message_probability = self._read_config_value(
            ("generation", "message", "mixed_probability"),
            default=50,
            legacy_keys=("mixed_message_probability",),
        )
        self.remove_invalid_alternative_markup = self._read_config_value(
            ("generation", "markup", "remove_invalid_alternative"),
            default=True,
            legacy_keys=("remove_invalid_alternative_markup",),
        )
        self.convert_static_to_gif = self._read_config_value(
            ("generation", "message", "convert_static_to_gif"),
            default=False,
            legacy_keys=("convert_static_to_gif",),
        )
        self.streaming_compatibility = self._read_config_value(
            ("generation", "message", "streaming_compatibility"),
            default=True,
            legacy_keys=("streaming_compatibility",),
        )
        self.send_image_as_base64 = self._read_config_value(
            ("generation", "message", "send_image_as_base64"),
            default=False,
            legacy_keys=("send_image_as_base64",),
        )
        self.content_cleanup_rule = self._read_config_value(
            ("generation", "message", "content_cleanup_rule"),
            default="&&[a-zA-Z]*&&",
            legacy_keys=("content_cleanup_rule",),
        )

        # 构建表情包提示词
        self.sys_prompt_add = ""
        self.persona_base_prompts = {}
        self._reload_personas()

        # 注册 WebUI API
        self._register_web_apis()

        self._semantic_initial_rebuild_task = None

    @filter.on_astrbot_loaded()
    async def _schedule_semantic_initial_rebuild(self):
        """等 AstrBot 核心和所有 Provider 加载完成后再安排首次静默重建。"""
        if not self.semantic_enabled:
            return
        task = self._semantic_initial_rebuild_task
        if task and not task.done():
            return
        self._semantic_initial_rebuild_task = asyncio.create_task(
            self._auto_rebuild_initial_pack()
        )

    @classmethod
    def _normalize_mixin_handler_module_paths(cls):
        """兼容尚未原生支持 Mixin 指令处理器的 AstrBot 版本。"""
        try:
            from astrbot.core.star.star_handler import star_handlers_registry
        except (ImportError, AttributeError):
            return

        plugin_package = cls.__module__.rsplit(".", 1)[0]
        mixin_module_prefix = f"{plugin_package}.mixins."
        adjusted_count = 0
        for handler_metadata in star_handlers_registry:
            if not handler_metadata.handler_module_path.startswith(
                mixin_module_prefix,
            ):
                continue
            handler_metadata.handler_module_path = cls.__module__
            adjusted_count += 1

        if adjusted_count:
            logger.info(f"已将 {adjusted_count} 个 Mixin 指令处理器绑定到插件主模块。")

    @filter.event_message_type(EventMessageType.ALL)
    async def handle_upload_image(self, event: AstrMessageEvent):
        async for result in self._handle_upload_image_impl(event):
            yield result

    @filter.on_llm_request(priority=99999)
    async def inject_meme_prompt(self, event: AstrMessageEvent, req: ProviderRequest):
        return await self._inject_meme_prompt_impl(event, req)

    @filter.on_llm_response(priority=99999)
    async def resp(self, event: AstrMessageEvent, response: LLMResponse):
        return await self._resp_impl(event, response)

    @filter.on_decorating_result(priority=99999)
    async def on_decorating_result(self, event: AstrMessageEvent):
        return await self._on_decorating_result_impl(event)

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent):
        return await self._after_message_sent_impl(event)

    def _get_image_host_type(self) -> str:
        image_host = self._read_config_value(
            ("storage", "provider"),
            default="stardots",
            legacy_keys=("image_host",),
        )
        if isinstance(image_host, dict):
            image_host = (
                image_host.get("name")
                or image_host.get("value")
                or image_host.get("type", "stardots")
            )
        return str(image_host or "stardots").strip().lower()

    def _read_path(self, path: tuple[str, ...], missing=None):
        current = self.config
        for key in path:
            if not isinstance(current, dict) or key not in current:
                return missing
            current = current[key]
        return current

    def _read_config_value(
        self,
        primary_path: tuple[str, ...],
        default=None,
        legacy_paths: tuple[tuple[str, ...], ...] = (),
        legacy_keys: tuple[str, ...] = (),
    ):
        missing = object()
        value = self._read_path(primary_path, missing)
        if value is not missing and value is not None:
            return value

        for legacy_path in legacy_paths:
            value = self._read_path(legacy_path, missing)
            if value is not missing and value is not None:
                return value

        for legacy_key in legacy_keys:
            value = self.config.get(legacy_key, missing)
            if value is not missing and value is not None:
                return value

        return default

    def _get_provider_config(self, provider_key: str) -> dict:
        merged_config = {}
        legacy_config = self._read_path(("image_host_config", provider_key), {})
        modern_config = self._read_path(("storage", "providers", provider_key), {})
        if isinstance(legacy_config, dict):
            merged_config.update(legacy_config)
        if isinstance(modern_config, dict):
            merged_config.update(modern_config)
        return merged_config

    def _get_nested_config(self, *keys: str) -> dict:
        current = self.config
        for key in keys:
            if not isinstance(current, dict):
                return {}
            current = current.get(key, {})
        return current if isinstance(current, dict) else {}

    def _has_required_config(self, config: dict, required_fields: list[str]) -> bool:
        return all(config.get(field) not in (None, "") for field in required_fields)

    def _get_webdav_config(self) -> dict:
        webdav_config = dict(self._get_provider_config("webdav"))
        if not webdav_config:
            webdav_config = dict(self._get_nested_config("webdav"))
        aliases = {
            "url": ["webdav_url", "endpoint", "base_url", "host"],
            "username": ["webdav_username", "user", "account"],
            "password": ["webdav_password", "pass", "token", "access_token"],
            "base_path": ["webdav_base_path", "path", "root_path", "remote_path"],
            "public_url": ["webdav_public_url", "cdn_url"],
            "verify_ssl": ["webdav_verify_ssl", "ssl_verify"],
            "timeout": ["webdav_timeout"],
        }
        for target_key, alias_keys in aliases.items():
            if webdav_config.get(target_key) not in (None, ""):
                continue
            for alias_key in alias_keys:
                value = webdav_config.get(alias_key, self.config.get(alias_key))
                if value not in (None, ""):
                    webdav_config[target_key] = value
                    break
        return webdav_config

    def _resolve_sync_pack_target(self, preferred_pack_id: str | None = None):
        from .backend.pack_resolver import get_pack_paths, resolve_pack_id

        pack_id = str(preferred_pack_id or "").strip()
        if pack_id:
            paths = get_pack_paths(pack_id)
            pack_dir = paths["pack_dir"]
            if pack_dir.is_dir():
                memes_dir = paths["memes_dir"]
                memes_dir.mkdir(parents=True, exist_ok=True)
                return pack_id, memes_dir

        resolved_pack_id = resolve_pack_id()
        paths = get_pack_paths(resolved_pack_id)
        memes_dir = paths["memes_dir"]
        memes_dir.mkdir(parents=True, exist_ok=True)
        return resolved_pack_id, memes_dir

    def _ensure_img_sync_for_pack(self, preferred_pack_id: str | None = None):
        if not (self.img_sync_config and self.img_sync_provider_type):
            return None

        running_process = (
            getattr(self.img_sync, "sync_process", None) if self.img_sync else None
        )
        if running_process and running_process.is_alive():
            return self.img_sync

        target_pack_id, target_memes_dir = self._resolve_sync_pack_target(
            preferred_pack_id
        )

        current_dir = None
        if self.img_sync and getattr(self.img_sync, "local_dir", None):
            try:
                current_dir = Path(self.img_sync.local_dir).resolve()
            except Exception:
                current_dir = None

        if current_dir != target_memes_dir.resolve():
            self.img_sync = ImageSync(
                config=self.img_sync_config,
                local_dir=target_memes_dir,
                provider_type=self.img_sync_provider_type,
            )

        self._img_sync_pack_id = target_pack_id
        return self.img_sync

    def _build_meme_prompt(self, category_mapping_string: str | None = None) -> str:
        mapping_string = category_mapping_string or self.category_mapping_string
        return (
            self.prompt_head
            + mapping_string
            + self.prompt_tail_1
            + str(self.max_emotions_per_message)
            + self.prompt_tail_2
        )

    def _resolve_embedding_provider(self, pack_id: str = ""):
        return self.semantic_task_manager._resolve_embedding_provider(pack_id)

    async def _auto_rebuild_initial_pack(self):
        """语义开关已开启时，后台静默补齐当前包的本机向量索引。"""
        await asyncio.sleep(3)
        try:
            pack_context = self._resolve_runtime_pack_context()
            pack_id = str(pack_context.get("pack_id") or "").strip()
            pack_dir = pack_context.get("pack_dir")
            if not pack_id or not pack_dir:
                logger.info(
                    "%s 语义检索已开启，但当前没有可重建的表情包。", WEBUI_LOG_PREFIX
                )
                return
            metadata = load_metadata(pack_dir)
            images = metadata.get("images", {})
            caption_done = sum(
                1
                for item in images.values()
                if isinstance(item, dict) and item.get("caption_status") == "done"
            )
            if (
                not images
                or caption_done != len(images)
                or not semantic_metadata_is_complete(pack_dir, metadata)
            ):
                logger.info(
                    "%s 首次开启语义检索：%s 的图片描述尚未全部完成，暂不自动建立向量；"
                    "请先在语义化页面完成描述。",
                    WEBUI_LOG_PREFIX,
                    pack_id,
                )
                return
            embedding = self.semantic_task_manager._require_embedding_provider(
                pack_id, "首次建立语义索引"
            )
            if index_is_ready(
                PLUGIN_DATA_DIR,
                pack_id,
                metadata,
                embedding.provider_id,
                embedding.model_name,
                embedding.dimension,
            ):
                return
            logger.info(
                "%s 首次开启语义检索：后台静默重建 %s 的向量索引（Provider=%s，维度=%s）。",
                WEBUI_LOG_PREFIX,
                pack_id,
                embedding.provider_id,
                embedding.dimension,
            )
            result = await self.semantic_task_manager.rebuild_index(pack_id, force=True)
            logger.info(
                "%s 首次开启语义检索：%s 向量索引完成，共 %s 条。",
                WEBUI_LOG_PREFIX,
                pack_id,
                result.get("item_count", 0),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "%s 首次开启语义检索的后台向量重建未完成：%s",
                WEBUI_LOG_PREFIX,
                str(exc),
            )

    def _reply_model_supports_tools(self, event: AstrMessageEvent | None) -> bool:
        """仅在模型明确声明不支持工具时关闭语义模式。"""
        if event is None:
            return True
        try:
            selected = event.get_extra("selected_provider")
            provider = (
                self.context.get_provider_by_id(selected)
                if isinstance(selected, str) and selected
                else self.context.get_using_provider(event.unified_msg_origin)
            )
            provider_config = getattr(provider, "provider_config", {})
            modalities = (
                provider_config.get("modalities")
                if isinstance(provider_config, dict)
                else None
            )
            if not isinstance(modalities, list) or not modalities:
                return True
            return "tool_use" in {
                str(modality or "").strip().lower() for modality in modalities
            }
        except Exception:
            return True

    def _semantic_pack_ready(
        self,
        event: AstrMessageEvent | None = None,
        req: ProviderRequest | None = None,
        *,
        require_tool: bool = False,
    ) -> bool:
        """Return whether the selected runtime pack can use semantic search.

        Args:
            event: Current AstrBot message event.
            req: Current provider request, when checking request-time tools.
            require_tool: Whether the reply model and request must expose the
                semantic search tool.

        Returns:
            True when the pack is complete and its matching index is ready.
        """
        if not self.semantic_enabled:
            return False
        if (
            require_tool
            and req is not None
            and not self._reply_model_supports_tools(event)
        ):
            return False
        if require_tool and req is not None:
            tool_set = getattr(req, "func_tool", None)
            if (
                tool_set is None
                or not callable(getattr(tool_set, "get_tool", None))
                or not tool_set.get_tool("search_memes")
            ):
                return False
        context = self._resolve_runtime_pack_context(event=event, req=req)
        pack_id = str(context.get("pack_id") or "")
        if not pack_id:
            return False
        pack_dir = context.get("pack_dir")
        metadata = load_metadata(pack_dir)
        if not semantic_metadata_is_complete(
            pack_dir, metadata, require_embeddings=True
        ):
            return False
        try:
            provider = self._resolve_embedding_provider(pack_id)
            embedding = EmbeddingAdapter(
                provider, self.semantic_embedding_provider_id or ""
            )
        except Exception as exc:
            logger.warning(
                "Semantic search is unavailable; falling back to legacy categories: %s",
                exc,
            )
            return False
        provider_id = embedding.provider_id
        return (
            index_is_ready(
                PLUGIN_DATA_DIR,
                pack_id,
                metadata,
                provider_id,
                embedding.model_name,
                embedding.dimension,
            )
            and embedding.ready
        )

    @staticmethod
    def _remove_semantic_tool(req: ProviderRequest) -> None:
        """语义模式不可用时，从当前请求的工具集中移除搜索工具。"""
        tool_set = getattr(req, "func_tool", None)
        get_full_tool_set = getattr(tool_set, "get_full_tool_set", None)
        if callable(get_full_tool_set):
            req.func_tool = get_full_tool_set()
            tool_set = req.func_tool
        remove_tool = getattr(tool_set, "remove_tool", None)
        if callable(remove_tool):
            remove_tool("search_memes")

    def _semantic_mode_active(self, event: AstrMessageEvent | None) -> bool:
        """Return whether the event still targets its verified semantic pack.

        Args:
            event: Current AstrBot message event.

        Returns:
            True when request-time verification and the runtime pack match.
        """
        if event is None or not hasattr(event, "get_extra"):
            return False
        verified_pack_id = str(
            event.get_extra("meme_manager_semantic_verified_pack_id") or ""
        )
        runtime_pack_id = str(event.get_extra("meme_manager_runtime_pack_id") or "")
        return (
            bool(event.get_extra("meme_manager_semantic_active"))
            and bool(verified_pack_id)
            and verified_pack_id == runtime_pack_id
        )

    def _semantic_system_prompt(self) -> str:
        return (
            f"\n\n{SEMANTIC_PROMPT_MARKER_START}\n"
            "本轮必须调用且只能调用一次 search_memes，然后才能给出最终回复。不要直接复制用户原话，"
            "先判断你准备如何回应，再用第一人称描述自己的情绪、态度、动作和潜台词作为查询词。"
            "工具返回候选后：若候选列表非空，必须选择最贴合的一张，并在最终文本中使用 "
            "&&meme:候选ID&&；该机器标记必须独占最后一行，不能加反引号。"
            "例如候选 id 是 meme:123456789abc，最后一行就写 &&meme:123456789abc&&。"
            "最终可见正文绝对不要复述候选 ID、图片说明、caption 或 tags。"
            "只有候选列表为空时才可以不添加表情。不要捏造候选列表之外的 ID，也不要重复调用工具。\n"
            f"{SEMANTIC_PROMPT_MARKER_END}"
        )

    def _wrap_meme_prompt(self, prompt: str) -> str:
        return f"\n\n{MEME_PROMPT_MARKER_START}\n{prompt}\n{MEME_PROMPT_MARKER_END}"

    def _strip_meme_prompt(self, prompt: str | None) -> str:
        prompt = prompt or ""
        marker_pattern = re.compile(
            rf"\n*{re.escape(MEME_PROMPT_MARKER_START)}[\s\S]*?{re.escape(MEME_PROMPT_MARKER_END)}"
        )
        prompt = marker_pattern.sub("", prompt).rstrip()
        semantic_marker_pattern = re.compile(
            rf"\n*{re.escape(SEMANTIC_PROMPT_MARKER_START)}[\s\S]*?"
            rf"{re.escape(SEMANTIC_PROMPT_MARKER_END)}"
        )
        prompt = semantic_marker_pattern.sub("", prompt).rstrip()
        if self.sys_prompt_add and prompt.endswith(self.sys_prompt_add):
            prompt = prompt[: -len(self.sys_prompt_add)].rstrip()
        if not self.prompt_head:
            return prompt
        start = prompt.find(self.prompt_head)
        if start < 0:
            return prompt
        if not self.prompt_tail_2:
            return prompt[:start].rstrip()
        end = prompt.find(self.prompt_tail_2, start)
        if end >= 0:
            end += len(self.prompt_tail_2)
            prompt = (prompt[:start] + prompt[end:]).rstrip()
        return prompt

    def _resolve_persona_id(
        self, event: AstrMessageEvent | None = None, req: ProviderRequest | None = None
    ) -> str | None:
        if req and req.conversation:
            persona_id = str(getattr(req.conversation, "persona_id", "") or "").strip()
            if persona_id:
                return persona_id
        if event is None:
            return None
        persona_id = str(getattr(event, "persona_id", "") or "").strip()
        if persona_id:
            return persona_id
        if hasattr(event, "get_extra"):
            persona_id = str(event.get_extra("persona_id") or "").strip()
            if persona_id:
                return persona_id
        return None

    def _resolve_runtime_pack_context(
        self, event: AstrMessageEvent | None = None, req: ProviderRequest | None = None
    ) -> dict:
        from .backend.pack_resolver import resolve_pack_context

        session_id = ""
        if req:
            session_id = str(req.session_id or "").strip()
        if not session_id and event is not None:
            session_id = str(getattr(event, "session_id", "") or "").strip()
        persona_id = self._resolve_persona_id(event=event, req=req)
        context = resolve_pack_context(
            session_id=session_id or None, persona_id=persona_id
        )
        if event is not None and hasattr(event, "set_extra"):
            event.set_extra(
                "meme_manager_runtime_memes_dir",
                str(context.get("memes_dir", MEMES_DIR)),
            )
            event.set_extra(
                "meme_manager_runtime_pack_id", str(context.get("pack_id") or "")
            )
        return context

    def _get_runtime_memes_dir_for_event(self, event: AstrMessageEvent):
        if hasattr(event, "get_extra"):
            runtime_memes_dir = str(
                event.get_extra("meme_manager_runtime_memes_dir") or ""
            ).strip()
            if runtime_memes_dir:
                return runtime_memes_dir
        return str(MEMES_DIR)

    def _get_manageable_categories(self) -> set[str]:
        return (
            set(self.category_manager.get_descriptions())
            | self.category_manager.get_local_categories()
        )

    def _ensure_default_category_descriptions(self, categories: list[str]) -> None:
        existing_descriptions = self.category_manager.get_descriptions()
        updated = False
        for category in categories:
            if category in existing_descriptions:
                continue
            default_description = DEFAULT_CATEGORY_DESCRIPTIONS.get(category)
            if not default_description:
                continue
            if self.category_manager.update_description(category, default_description):
                existing_descriptions[category] = default_description
                updated = True
        if updated:
            self._reload_personas()

    def _get_persona_key(self, persona: dict, index: int) -> str:
        return str(persona.get("name") or persona.get("id") or index)

    def _sync_persona_base_prompts(self, personas: list[dict]) -> None:
        active_keys = set()
        for index, persona in enumerate(personas):
            key = self._get_persona_key(persona, index)
            active_keys.add(key)
            self.persona_base_prompts[key] = self._strip_meme_prompt(
                persona.get("prompt", "")
            )
        for key in set(self.persona_base_prompts) - active_keys:
            del self.persona_base_prompts[key]

    def _apply_persona_prompts(self) -> None:
        personas = self.context.provider_manager.personas
        self._sync_persona_base_prompts(personas)
        self.sys_prompt_add = ""
        for index, persona in enumerate(personas):
            key = self._get_persona_key(persona, index)
            persona["prompt"] = self.persona_base_prompts[key]

    def _apply_request_prompt(
        self, req: ProviderRequest, event: AstrMessageEvent | None = None
    ) -> None:
        semantic_mode = ""
        semantic_tool_ready = False
        semantic_probability_hit = None
        if self.emotion_llm_enabled:
            if self._semantic_pack_ready(event=event, req=req):
                semantic_mode = "llm"
        else:
            semantic_tool_ready = self._semantic_pack_ready(
                event=event, req=req, require_tool=True
            )
            if semantic_tool_ready:
                semantic_probability_hit = probability_hit(self.emotions_probability)
                if semantic_probability_hit:
                    semantic_mode = "tool"
                logger.info(
                    "[meme_manager] 语义表情触发判定: 概率=%s%%, 结果=%s",
                    normalize_probability(self.emotions_probability),
                    "命中" if semantic_probability_hit else "跳过",
                )
        semantic_active = bool(semantic_mode)
        if event is not None and hasattr(event, "set_extra"):
            event.set_extra("meme_manager_semantic_active", semantic_active)
            event.set_extra("meme_manager_semantic_mode", semantic_mode)
            event.set_extra(
                "meme_manager_semantic_probability_hit",
                semantic_probability_hit,
            )
            event.set_extra(
                "meme_manager_semantic_verified_pack_id",
                str(event.get_extra("meme_manager_runtime_pack_id") or "")
                if semantic_active
                else "",
            )
            event.set_extra("meme_manager_semantic_selected_ids", [])
            event.set_extra("meme_manager_semantic_candidates", {})
            event.set_extra("meme_manager_semantic_query", "")
            event.set_extra("meme_manager_semantic_search_completed", False)
            event.set_extra("meme_manager_semantic_default_id", "")
            event.set_extra("meme_manager_semantic_response_processed", False)
            event.set_extra("meme_manager_reply_provider_id", "")
            event.set_extra("meme_manager_reply_model", "")
        if semantic_mode == "tool":
            req.system_prompt = (
                self._strip_meme_prompt(req.system_prompt)
                + self._semantic_system_prompt()
            )
            return
        self._remove_semantic_tool(req)
        if semantic_tool_ready:
            # 本轮概率未命中：既不暴露工具，也不回退到旧版标签提示，
            # 避免绕过本轮概率判定后仍然产生表情。
            req.system_prompt = self._strip_meme_prompt(req.system_prompt)
            return
        if semantic_mode == "llm" or self.emotion_llm_enabled:
            req.system_prompt = self._strip_meme_prompt(req.system_prompt)
            return
        pack_context = self._resolve_runtime_pack_context(event=event, req=req)
        context_mapping = pack_context.get("category_mapping")
        category_mapping = (
            runtime_category_mapping(context_mapping)
            if isinstance(context_mapping, dict)
            else runtime_category_mapping(self.category_mapping)
        )
        if not category_mapping:
            return
        category_mapping_string = dict_to_string(category_mapping)
        sys_prompt_add = self._build_meme_prompt(category_mapping_string)
        req.system_prompt = self._strip_meme_prompt(
            req.system_prompt
        ) + self._wrap_meme_prompt(sys_prompt_add)

    def _reload_personas(self):
        pack_context = self._resolve_runtime_pack_context()
        context_mapping = pack_context.get("category_mapping")
        self.category_mapping = runtime_category_mapping(
            context_mapping
            if isinstance(context_mapping, dict)
            else load_json(MEMES_DATA_PATH, DEFAULT_CATEGORY_DESCRIPTIONS)
        )
        self.category_mapping_string = dict_to_string(self.category_mapping)
        self._apply_persona_prompts()

    async def reload_emotions(self):
        try:
            self.category_manager.sync_with_filesystem()
            self._reload_personas()
        except Exception as e:
            logger.error(f"重新加载表情配置失败: {str(e)}")

    @llm_tool(name="search_memes")
    async def search_memes_tool(self, event: AstrMessageEvent, query: str) -> str:
        """搜索与 Bot 当前表达意图相符的表情包。

        Args:
            query(string): Bot 自己准备表达的情绪、态度、动作和潜台词，不要直接复制用户原话。
        """
        if (
            not self._semantic_mode_active(event)
            or str(event.get_extra("meme_manager_semantic_mode") or "") != "tool"
        ):
            return dumps_result({"ok": False, "reason": "语义查询未启用或索引不可用"})
        if bool(event.get_extra("meme_manager_semantic_search_completed")):
            return dumps_result(
                {
                    "ok": False,
                    "reason": "本轮已经完成唯一一次搜索，请直接根据上次候选完成最终回复",
                }
            )
        event.set_extra("meme_manager_semantic_search_completed", True)
        provider_request = event.get_extra("provider_request")
        if provider_request is not None:
            self._remove_semantic_tool(provider_request)
        context = self._resolve_runtime_pack_context(event=event)
        if str(context.get("pack_id") or "") != str(
            event.get_extra("meme_manager_semantic_verified_pack_id") or ""
        ):
            return dumps_result({"ok": False, "reason": "当前语义图包已经变化"})
        try:
            result = await search_memes(
                context["pack_dir"],
                PLUGIN_DATA_DIR,
                str(context["pack_id"]),
                query,
                self._resolve_embedding_provider(str(context["pack_id"])),
                top_k=self.semantic_top_k,
                min_score=self.semantic_min_score,
                _verified_complete=True,
            )
            records = candidate_records(
                context["pack_dir"], result.get("candidates") or []
            )
            remember_candidates(event, records)
            event.set_extra(
                "meme_manager_semantic_default_id",
                str(records[0].get("id") or "") if records else "",
            )
            event.set_extra("meme_manager_semantic_query", str(query or ""))
            return dumps_result(
                {
                    **result,
                    "instruction": (
                        "本轮唯一一次搜索已经完成，禁止再次调用 search_memes。"
                        "候选非空时选择一张，只在最终回复最后一行输出 "
                        "&&meme:候选ID&&，不要解释 ID、caption 或 tags。"
                    ),
                }
            )
        except Exception as exc:
            logger.error("语义表情查询失败: %s", exc, exc_info=True)
            return dumps_result({"ok": False, "reason": "语义查询失败"})

    async def terminate(self):
        initial_task = getattr(self, "_semantic_initial_rebuild_task", None)
        if initial_task and not initial_task.done():
            initial_task.cancel()
            await asyncio.gather(initial_task, return_exceptions=True)
        if getattr(self, "semantic_task_manager", None):
            await self.semantic_task_manager.close()
        personas = self.context.provider_manager.personas
        self._sync_persona_base_prompts(personas)
        for index, persona in enumerate(personas):
            key = self._get_persona_key(persona, index)
            persona["prompt"] = self.persona_base_prompts[key]
        if self.img_sync:
            self.img_sync.stop_sync()
