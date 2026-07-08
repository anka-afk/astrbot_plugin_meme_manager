import re
from pathlib import Path

from astrbot.api import logger
from astrbot.api.all import *
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import *
from astrbot.api.provider import ProviderRequest, LLMResponse
from astrbot.api.star import Context, Star

from .backend.category_manager import CategoryManager
from .config import MEMES_DATA_PATH, MEMES_DIR, DEFAULT_CATEGORY_DESCRIPTIONS
from .image_host.img_sync import ImageSync
from .init import init_plugin
from .utils import dict_to_string, load_json
from .mixins.web_api import WebAPIMixin
from .mixins.commands import CommandMixin
from .mixins.event_handlers import EventHandlerMixin

MEME_PROMPT_MARKER_START = "<!-- meme_manager_prompt:start -->"
MEME_PROMPT_MARKER_END = "<!-- meme_manager_prompt:end -->"
PLUGIN_NAME = "meme_manager"
WEBUI_LOG_PREFIX = f"[{PLUGIN_NAME}][WebUI]"


class MemeSender(Star, WebAPIMixin, CommandMixin, EventHandlerMixin):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

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
            stardots_config = self.config.get("image_host_config", {}).get(
                "stardots", {}
            )
            if stardots_config.get("key") and stardots_config.get("secret"):
                stardots_config["provider"] = "stardots"
                self.img_sync_config = {
                    "key": stardots_config["key"],
                    "secret": stardots_config["secret"],
                    "space": stardots_config.get("space", "memes"),
                    "provider": "stardots",
                }
                self.img_sync_provider_type = "stardots"
        elif image_host_type == "cloudflare_r2":
            r2_config = self.config.get("image_host_config", {}).get(
                "cloudflare_r2", {}
            )
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
        self.found_emotions = []
        self.upload_states = {}
        self.pending_images = {}

        # 配置项
        self.prompt_head = self.config.get("prompt").get("prompt_head")
        self.prompt_tail_1 = self.config.get("prompt").get("prompt_tail_1")
        self.prompt_tail_2 = self.config.get("prompt").get("prompt_tail_2")
        self.max_emotions_per_message = self.config.get("max_emotions_per_message")
        self.emotions_probability = self.config.get("emotions_probability")
        self.strict_max_emotions_per_message = self.config.get(
            "strict_max_emotions_per_message"
        )
        self.emotion_llm_enabled = self.config.get("emotion_llm_enabled", False)
        self.emotion_llm_provider_id = self.config.get("emotion_llm_provider_id", "")
        self.enable_mixed_message = self.config.get("enable_mixed_message", True)
        self.mixed_message_probability = self.config.get(
            "mixed_message_probability", 80
        )
        self.remove_invalid_alternative_markup = self.config.get(
            "remove_invalid_alternative_markup", False
        )
        self.convert_static_to_gif = self.config.get("convert_static_to_gif", False)
        self.streaming_compatibility = self.config.get("streaming_compatibility", False)
        self.content_cleanup_rule = self.config.get(
            "content_cleanup_rule", "&&[a-zA-Z]*&&"
        )

        # 构建表情包提示词
        self.sys_prompt_add = ""
        self.persona_base_prompts = {}
        self._reload_personas()

        # 注册 WebUI API
        self._register_web_apis()

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
        image_host = self.config.get("image_host", "stardots")
        if isinstance(image_host, dict):
            image_host = (
                image_host.get("name")
                or image_host.get("value")
                or image_host.get("type", "stardots")
            )
        return str(image_host or "stardots").strip().lower()

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
        webdav_config = dict(self._get_nested_config("image_host_config", "webdav"))
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

    def _wrap_meme_prompt(self, prompt: str) -> str:
        return f"\n\n{MEME_PROMPT_MARKER_START}\n{prompt}\n{MEME_PROMPT_MARKER_END}"

    def _strip_meme_prompt(self, prompt: str | None) -> str:
        prompt = prompt or ""
        marker_pattern = re.compile(
            rf"\n*{re.escape(MEME_PROMPT_MARKER_START)}[\s\S]*?{re.escape(MEME_PROMPT_MARKER_END)}"
        )
        prompt = marker_pattern.sub("", prompt).rstrip()
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
        if self.emotion_llm_enabled:
            req.system_prompt = self._strip_meme_prompt(req.system_prompt)
            return
        pack_context = self._resolve_runtime_pack_context(event=event, req=req)
        category_mapping = pack_context.get("category_mapping") or self.category_mapping
        if not category_mapping:
            return
        category_mapping_string = dict_to_string(category_mapping)
        sys_prompt_add = self._build_meme_prompt(category_mapping_string)
        req.system_prompt = self._strip_meme_prompt(
            req.system_prompt
        ) + self._wrap_meme_prompt(sys_prompt_add)

    def _reload_personas(self):
        pack_context = self._resolve_runtime_pack_context()
        self.category_mapping = pack_context.get("category_mapping") or load_json(
            MEMES_DATA_PATH, DEFAULT_CATEGORY_DESCRIPTIONS
        )
        self.category_mapping_string = dict_to_string(self.category_mapping)
        self._apply_persona_prompts()

    async def reload_emotions(self):
        try:
            self.category_manager.sync_with_filesystem()
            self._reload_personas()
        except Exception as e:
            logger.error(f"重新加载表情配置失败: {str(e)}")

    async def terminate(self):
        personas = self.context.provider_manager.personas
        self._sync_persona_base_prompts(personas)
        for index, persona in enumerate(personas):
            key = self._get_persona_key(persona, index)
            persona["prompt"] = self.persona_base_prompts[key]
        if self.img_sync:
            self.img_sync.stop_sync()
