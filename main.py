import asyncio
import base64
import io
import json
import mimetypes
import os
import random
import re
import ssl
import tempfile
import time
import traceback

import aiohttp
from PIL import Image as PILImage

from astrbot.api import logger
from astrbot.api.all import *  # noqa: F403
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import *  # noqa: F403
from astrbot.api.message_components import Image
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain, ResultContentType
from astrbot.core.utils.session_waiter import (
    SessionController,
    SessionFilter,
    session_waiter,
)
from quart import jsonify, make_response, request, send_file

from .backend.category_manager import CategoryManager
from .backend.models import (
    clear_all_emojis,
    clear_category_emojis,
    get_emoji_by_category,
)
from .backend.pack_resolver import resolve_pack_context
from .config import DEFAULT_CATEGORY_DESCRIPTIONS, MEMES_DATA_PATH, MEMES_DIR
from .image_host.img_sync import ImageSync
from .init import init_plugin
from .utils import (
    dict_to_string,
    get_default_meme_categories,
    load_json,
    restore_default_memes,
)


class ConfirmationCancelled(Exception):
    """Raised when a dangerous command is cancelled by the user."""


class CategoryCreationCancelled(Exception):
    """Raised when category creation is cancelled by the user."""


MEME_PROMPT_MARKER_START = "<!-- meme_manager_prompt:start -->"
MEME_PROMPT_MARKER_END = "<!-- meme_manager_prompt:end -->"
PLUGIN_NAME = "meme_manager"
WEBUI_LOG_PREFIX = f"[{PLUGIN_NAME}][WebUI]"
MAX_PREVIEW_IMAGE_BYTES = 8 * 1024 * 1024
MAX_ORIGINAL_IMAGE_BYTES = 32 * 1024 * 1024
PREVIEW_IMAGE_MAX_DIMENSION = 512


class SenderScopedSessionFilter(SessionFilter):
    """Bind confirmation replies to the same sender within the same session."""

    def filter(self, event: AstrMessageEvent) -> str:
        sender_id = str(event.get_sender_id() or "").strip()
        return f"{event.unified_msg_origin}:{sender_id}"


class MemeSender(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

        # 初始化插件
        if not init_plugin():
            raise RuntimeError("插件初始化失败")

        # 初始化类别管理器
        self.category_manager = CategoryManager()

        # 初始化图床同步客户端
        self.img_sync = None
        self.img_sync_config = None
        self.img_sync_provider_type = None
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
                self.img_sync = ImageSync(
                    config=self.img_sync_config,
                    local_dir=MEMES_DIR,
                    provider_type=self.img_sync_provider_type,
                )
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
                self.img_sync = ImageSync(
                    config=self.img_sync_config,
                    local_dir=MEMES_DIR,
                    provider_type=self.img_sync_provider_type,
                )
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
                self.img_sync = ImageSync(
                    config=webdav_config, local_dir=MEMES_DIR, provider_type="webdav"
                )
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

        # 初始化表情状态
        self.found_emotions = []  # 存储找到的表情
        self.upload_states = (
            {}
        )  # 存储上传状态：{user_session: {"category": str, "expire_time": float}}
        self.pending_images = {}  # 存储待发送的图片

        if hasattr(self, "_r2_bucket_name"):
            logger.info(f"Cloudflare R2 图床已初始化: {self._r2_bucket_name}")
            delattr(self, "_r2_bucket_name")
        if hasattr(self, "_webdav_url"):
            logger.info(f"WebDAV 图床已初始化: {self._webdav_url}")
            delattr(self, "_webdav_url")

        # 处理人格
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

        # 混合消息相关配置
        self.enable_mixed_message = self.config.get("enable_mixed_message", True)
        self.mixed_message_probability = self.config.get(
            "mixed_message_probability", 80
        )
        self.remove_invalid_alternative_markup = self.config.get(
            "remove_invalid_alternative_markup", False
        )
        self.convert_static_to_gif = self.config.get("convert_static_to_gif", False)

        # 流式传输兼容
        self.streaming_compatibility = self.config.get("streaming_compatibility", False)

        # 内容清理规则
        self.content_cleanup_rule = self.config.get(
            "content_cleanup_rule", "&&[a-zA-Z]*&&"
        )

        # 构建表情包提示词
        self.sys_prompt_add = ""
        self.persona_base_prompts = {}
        self._reload_personas()

        # 注册仪表盘插件页面 API
        self._register_web_apis()

    @filter.command_group("表情管理")
    def meme_manager(self):
        """表情包管理命令组:
        查看图库
        添加分类
        添加表情
        恢复默认表情包
        清空指定类型
        清空全部
        删除类型本身
        同步状态
        同步到云端
        从云端同步
        """
        pass

    def _get_image_host_type(self) -> str:
        """读取图床类型，兼容不同配置保存格式。"""
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
        """读取 WebDAV 配置，兼容嵌套、扁平和常见字段别名。"""
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

    def _register_web_apis(self):
71        self._register_webui_api(
            "emoji", self._api_get_emojis, ["GET"], "Get all emojis grouped by category"
        )
        self._register_webui_api(
            "emoji/<category>",
            self._api_get_emoji_by_category,
            ["GET"],
            "Get emojis in a specific category",
        )
        self._register_webui_api(
            "emoji/add/<category>",
            self._api_add_emoji,
            ["POST"],
            "Upload emoji to category (multipart: file field)",
        )
        self._register_webui_api(
            "emoji/delete", self._api_delete_emoji, ["POST"], "Delete a single emoji"
        )
        self._register_webui_api(
            "emoji/batch_delete",
            self._api_batch_delete_emojis,
            ["POST"],
            "Batch delete emojis",
        )
        self._register_webui_api(
            "emoji/move",
            self._api_move_emoji,
            ["POST"],
            "Move single emoji to another category",
        )
        self._register_webui_api(
            "emoji/batch_move",
            self._api_batch_move_emojis,
            ["POST"],
            "Batch move emojis",
        )
        self._register_webui_api(
            "emoji/batch_copy",
            self._api_batch_copy_emojis,
            ["POST"],
            "Batch copy emojis",
        )
        self._register_webui_api(
            "emoji/clear_all",
            self._api_clear_all_emojis,
            ["POST"],
            "Clear all emojis (keeps categories)",
        )

        # --- Category management ---
        self._register_webui_api(
            "emotions", self._api_get_emotions, ["GET"], "Get category descriptions"
        )
        self._register_webui_api(
            "category/delete",
            self._api_delete_category,
            ["POST"],
            "Delete category and its files",
        )
        self._register_webui_api(
            "category/clear",
            self._api_clear_category,
            ["POST"],
            "Clear emojis in category (keep category)",
        )
        self._register_webui_api(
            "category/restore",
            self._api_restore_category,
            ["POST"],
            "Restore or create a category",
        )
        self._register_webui_api(
            "category/rename", self._api_rename_category, ["POST"], "Rename a category"
        )
        self._register_webui_api(
            "category/update_description",
            self._api_update_description,
            ["POST"],
            "Update category description",
        )
        self._register_webui_api(
            "category/remove_from_config",
            self._api_remove_from_config,
            ["POST"],
            "Remove category from config only",
        )

        # --- Sync ---
        self._register_webui_api(
            "sync/status", self._api_sync_status, ["GET"], "Get config sync status"
        )
        self._register_webui_api(
            "sync/config",
            self._api_sync_config,
            ["POST"],
            "Sync config with filesystem",
        )

        # --- Image host sync ---
        self._register_webui_api(
            "img_host/sync/status",
            self._api_img_host_sync_status,
            ["GET"],
            "Get image host sync status",
        )
        self._register_webui_api(
            "img_host/sync/upload",
            self._api_img_host_sync_upload,
            ["POST"],
            "Start upload to image host",
        )
        self._register_webui_api(
            "img_host/sync/download",
            self._api_img_host_sync_download,
            ["POST"],
            "Start download from image host",
        )
        self._register_webui_api(
            "img_host/sync/overwrite_to_remote",
            self._api_img_host_sync_overwrite_to_remote,
            ["POST"],
            "Overwrite image host from local files",
        )
        self._register_webui_api(
            "img_host/sync/overwrite_from_remote",
            self._api_img_host_sync_overwrite_from_remote,
            ["POST"],
            "Overwrite local files from image host",
        )
        self._register_webui_api(
            "img_host/sync/progress",
            self._api_img_host_sync_progress,
            ["GET"],
            "SSE stream for sync progress",
        )
        self._register_webui_api(
            "img_host/sync/task_status",
            self._api_img_host_sync_task_status,
            ["GET"],
            "Get current image host sync task status",
        )

        # --- Image serving ---
        self._register_webui_api(
            "meme_image", self._api_serve_meme_image, ["GET"], "Serve a meme image file"
        )
        self._register_webui_api(
            "meme_image_data",
            self._api_get_meme_image_data,
            ["GET"],
            "Get a meme image as a data URL for plugin page previews",
        )

    def _register_webui_api(self, route, handler, methods, desc):
        route_path = f"/{PLUGIN_NAME}/{route.strip('/')}"

        async def logged_handler(*args, **kwargs):
            started_at = time.monotonic()
            logger.info(f"{WEBUI_LOG_PREFIX} {request.method} {route_path} start")
            try:
                response = await handler(*args, **kwargs)
            except Exception:
                elapsed_ms = int((time.monotonic() - started_at) * 1000)
                logger.error(
                    f"{WEBUI_LOG_PREFIX} {request.method} {route_path} "
                    f"failed duration_ms={elapsed_ms}",
                    exc_info=True,
                )
                raise

            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            status_code = self._get_webui_response_status(response)
            logger.info(
                f"{WEBUI_LOG_PREFIX} {request.method} {route_path} "
                f"done status={status_code} duration_ms={elapsed_ms}"
            )
            return response

        logged_handler.__name__ = f"webui_{handler.__name__}"
        self.context.register_web_api(route_path, logged_handler, methods, desc)

    @staticmethod
    def _get_webui_response_status(response) -> int | str:
        if isinstance(response, tuple) and len(response) > 1:
            return response[1]
        return getattr(response, "status_code", "unknown")

    # === API Handler Methods ===

    async def _api_get_emojis(self):
        from .backend.models import scan_emoji_folder

        emoji_data = await scan_emoji_folder()
        for category in emoji_data:
            if not isinstance(emoji_data[category], list):
                emoji_data[category] = []
        return jsonify(emoji_data)

    async def _api_get_emoji_by_category(self, category):
        from .backend.models import get_emoji_by_category

        emojis = get_emoji_by_category(category)
        if emojis is None:
            return jsonify({"message": "Category not found"}), 404
        return jsonify(emojis if isinstance(emojis, list) else []), 200

    async def _api_add_emoji(self, category):
        from .backend.models import DuplicateEmojiError, add_emoji_to_category

        try:
            files = await request.files
            if not files or "file" not in files:
                return jsonify({"message": "没有找到上传的图片文件"}), 400

            image_file = files["file"]
            if not image_file or not image_file.filename:
                return jsonify({"message": "无效的图片文件"}), 400

            logger.info(f"收到上传请求: 类别={category}, 文件名={image_file.filename}")

            try:
                result = add_emoji_to_category(category, image_file)
                self.category_manager.sync_with_filesystem()
                logger.info(f"表情包添加成功: {result['path']}")
                return (
                    jsonify(
                        {
                            "message": "表情包添加成功",
                            "path": result["path"],
                            "category": category,
                            "filename": result["filename"],
                        }
                    ),
                    201,
                )
            except DuplicateEmojiError as e:
                logger.info(f"跳过重复表情包: {e}")
                return (
                    jsonify(
                        {
                            "message": str(e),
                            "code": "duplicate_emoji",
                            "category": category,
                            "filename": e.existing_filename,
                        }
                    ),
                    409,
                )
        except Exception as e:
            logger.error(f"处理上传请求时出错: {e}", exc_info=True)
            return jsonify({"message": f"处理上传请求时出错: {str(e)}"}), 500

    async def _api_delete_emoji(self):
        from .backend.models import delete_emoji_from_category

        data = await request.get_json()
        category = data.get("category")
        image_file = data.get("image_file")
        if not category or not image_file:
            return jsonify({"message": "Category and image file are required"}), 400
        if delete_emoji_from_category(category, image_file):
            return (
                jsonify(
                    {
                        "message": "Emoji deleted successfully",
                        "category": category,
                        "filename": image_file,
                    }
                ),
                200,
            )
        return jsonify({"message": "Emoji not found"}), 404

    async def _api_batch_delete_emojis(self):
        from .backend.models import batch_delete_emojis

        data = await request.get_json()
        category = data.get("category")
        image_files = data.get("image_files")
        if not category or not isinstance(image_files, list) or not image_files:
            return jsonify({"message": "Category and image_files are required"}), 400
        result = batch_delete_emojis(category, image_files)
        if not result["category_exists"]:
            return jsonify({"message": "Category not found"}), 404
        return (
            jsonify(
                {
                    "message": "Batch delete completed",
                    "category": category,
                    "deleted_files": result["deleted_files"],
                    "missing_files": result["missing_files"],
                    "deleted_count": len(result["deleted_files"]),
                    "missing_count": len(result["missing_files"]),
                }
            ),
            200,
        )

    async def _api_move_emoji(self):
        from .backend.models import move_emoji_to_category

        data = await request.get_json()
        source_category = data.get("source_category")
        target_category = data.get("target_category")
        image_file = data.get("image_file")
        if not source_category or not target_category or not image_file:
            return (
                jsonify(
                    {
                        "message": "source_category, target_category and image_file are required"
                    }
                ),
                400,
            )
        if source_category == target_category:
            return (
                jsonify({"message": "Source and target category must be different"}),
                400,
            )
        result = move_emoji_to_category(source_category, image_file, target_category)
        if not result["source_category_exists"]:
            return jsonify({"message": "Source category not found"}), 404
        if result["conflict"]:
            return jsonify({"message": "Target file already exists"}), 409
        if result["missing"]:
            return jsonify({"message": "Emoji not found"}), 404
        return (
            jsonify(
                {
                    "message": "Emoji moved successfully",
                    "source_category": result["source_category"],
                    "target_category": result["target_category"],
                    "filename": result["filename"],
                }
            ),
            200,
        )

    async def _api_batch_move_emojis(self):
        from .backend.models import batch_move_emojis

        data = await request.get_json()
        source_category = data.get("source_category")
        target_category = data.get("target_category")
        image_files = data.get("image_files")
        if (
            not source_category
            or not target_category
            or not isinstance(image_files, list)
            or not image_files
        ):
            return (
                jsonify(
                    {
                        "message": "source_category, target_category and image_files are required"
                    }
                ),
                400,
            )
        if source_category == target_category:
            return (
                jsonify({"message": "Source and target category must be different"}),
                400,
            )
        result = batch_move_emojis(source_category, image_files, target_category)
        if not result["source_category_exists"]:
            return jsonify({"message": "Source category not found"}), 404
        return (
            jsonify(
                {
                    "message": "Batch move completed",
                    "source_category": source_category,
                    "target_category": target_category,
                    "moved_files": result["moved_files"],
                    "missing_files": result["missing_files"],
                    "conflicting_files": result["conflicting_files"],
                    "moved_count": len(result["moved_files"]),
                    "missing_count": len(result["missing_files"]),
                    "conflict_count": len(result["conflicting_files"]),
                }
            ),
            200,
        )

    async def _api_batch_copy_emojis(self):
        from .backend.models import batch_copy_emojis

        data = await request.get_json()
        source_category = data.get("source_category")
        target_category = data.get("target_category")
        image_files = data.get("image_files")
        if (
            not source_category
            or not target_category
            or not isinstance(image_files, list)
            or not image_files
        ):
            return (
                jsonify(
                    {
                        "message": "source_category, target_category and image_files are required"
                    }
                ),
                400,
            )
        result = batch_copy_emojis(source_category, image_files, target_category)
        if not result["source_category_exists"]:
            return jsonify({"message": "Source category not found"}), 404
        return (
            jsonify(
                {
                    "message": "Batch copy completed",
                    "source_category": source_category,
                    "target_category": target_category,
                    "copied_files": result["copied_files"],
                    "missing_files": result["missing_files"],
                    "conflicting_files": result["conflicting_files"],
                    "copied_count": len(result["copied_files"]),
                    "missing_count": len(result["missing_files"]),
                    "conflict_count": len(result["conflicting_files"]),
                }
            ),
            200,
        )

    async def _api_clear_all_emojis(self):
        from .backend.models import clear_all_emojis

        result = clear_all_emojis()
        deleted_count = sum(result["deleted_by_category"].values())
        return (
            jsonify(
                {
                    "message": "All emojis cleared successfully",
                    "deleted_by_category": result["deleted_by_category"],
                    "deleted_count": deleted_count,
                    "affected_categories": len(result["deleted_by_category"]),
                }
            ),
            200,
        )

    async def _api_get_emotions(self):
        try:
            descriptions = self.category_manager.get_descriptions()
            return jsonify(descriptions)
        except Exception as e:
            logger.error(f"获取标签描述失败: {e}")
            return jsonify({"error": "获取标签描述失败"}), 500

    async def _api_delete_category(self):
        try:
            data = await request.get_json()
            category = data.get("category")
            if not category:
                return jsonify({"message": "Category is required"}), 400
            if self.category_manager.delete_category(category):
                return jsonify({"message": "Category deleted successfully"}), 200
            return jsonify({"message": "Failed to delete category"}), 500
        except Exception as e:
            return jsonify({"message": f"Failed to delete category: {str(e)}"}), 500

    async def _api_clear_category(self):
        from .backend.models import clear_category_emojis

        data = await request.get_json()
        category = data.get("category")
        if not category:
            return jsonify({"message": "Category is required"}), 400
        result = clear_category_emojis(category)
        if not result["category_exists"]:
            return jsonify({"message": "Category not found"}), 404
        return (
            jsonify(
                {
                    "message": "Category cleared successfully",
                    "category": category,
                    "deleted_files": result["deleted_files"],
                    "deleted_count": len(result["deleted_files"]),
                }
            ),
            200,
        )

    async def _api_restore_category(self):
        try:
            data = await request.get_json()
            category = data.get("category")
            description = data.get("description", "请添加描述")
            if not category:
                return jsonify({"message": "Category is required"}), 400
            if self.category_manager.create_category(category, description):
                return (
                    jsonify(
                        {
                            "message": "Category created successfully",
                            "description": description,
                        }
                    ),
                    200,
                )
            return jsonify({"message": "Failed to create category"}), 500
        except Exception as e:
            return jsonify({"message": f"Failed to create category: {str(e)}"}), 500

    async def _api_rename_category(self):
        try:
            data = await request.get_json()
            old_name = data.get("old_name")
            new_name = data.get("new_name")
            if not old_name or not new_name:
                return (
                    jsonify({"message": "Old and new category names are required"}),
                    400,
                )
            if self.category_manager.rename_category(old_name, new_name):
                return jsonify({"message": "Category renamed successfully"}), 200
            return jsonify({"message": "Failed to rename category"}), 500
        except Exception as e:
            return jsonify({"message": f"Failed to rename category: {str(e)}"}), 500

    async def _api_update_description(self):
        try:
            data = await request.get_json()
            category = data.get("tag")
            description = data.get("description")
            if not category or not description:
                return (
                    jsonify({"message": "Category and description are required"}),
                    400,
                )
            if self.category_manager.update_description(category, description):
                return jsonify({"category": category, "description": description}), 200
            return jsonify({"message": "Failed to update category description"}), 500
        except Exception as e:
            return (
                jsonify(
                    {"message": f"Failed to update category description: {str(e)}"}
                ),
                500,
            )

    async def _api_remove_from_config(self):
        try:
            data = await request.get_json()
            category = data.get("category")
            if not category:
                return jsonify({"message": "Category is required"}), 400
            if self.category_manager.remove_from_config(category):
                return (
                    jsonify({"message": "Category removed from config successfully"}),
                    200,
                )
            return jsonify({"message": "Failed to remove category from config"}), 500
        except Exception as e:
            return (
                jsonify(
                    {"message": f"Failed to remove category from config: {str(e)}"}
                ),
                500,
            )

    async def _api_sync_status(self):
        try:
            missing_in_config, deleted_categories = (
                self.category_manager.get_sync_status()
            )
            return jsonify(
                {
                    "status": "ok",
                    "missing_in_config": missing_in_config,
                    "deleted_categories": deleted_categories,
                    "differences": {
                        "missing_in_config": missing_in_config,
                        "deleted_categories": deleted_categories,
                    },
                }
            )
        except Exception as e:
            logger.error(f"获取同步状态失败: {e}")
            return jsonify({"error": "获取同步状态失败"}), 500

    async def _api_sync_config(self):
        try:
            logger.info("开始同步配置...")
            if self.category_manager.sync_with_filesystem():
                logger.info("配置同步成功")
                return jsonify({"message": "配置同步成功"}), 200
            logger.warning("配置同步失败")
            return jsonify({"message": "配置同步失败"}), 500
        except Exception as e:
            logger.error(f"配置同步失败: {e}")
            return jsonify({"message": f"配置同步失败: {str(e)}"}), 500

    def _get_provider_label(self) -> str:
        if self.img_sync_provider_type == "cloudflare_r2":
            return "Cloudflare R2"
        if self.img_sync_provider_type == "stardots":
            return "StarDots"
        if self.img_sync and hasattr(self.img_sync, "provider"):
            return self.img_sync.provider.__class__.__name__
        return "未知图床"

    def _get_img_host_sync_task_status(self) -> dict:
        if not self.img_sync:
            return {
                "available": False,
                "running": False,
                "completed": True,
                "success": False,
                "message": "图床服务未配置",
            }

        process = getattr(self.img_sync, "sync_process", None)
        if not process:
            if self._last_img_host_sync_task_status:
                return self._last_img_host_sync_task_status.copy()
            return {
                "available": True,
                "running": False,
                "completed": True,
                "success": None,
                "message": "当前没有同步任务",
            }

        status = {
            "available": True,
            "pid": process.pid,
            "exit_code": process.exitcode,
        }
        if process.is_alive():
            status.update(
                {
                    "running": True,
                    "completed": False,
                    "success": None,
                    "message": "同步任务运行中",
                }
            )
            return status

        exit_code = process.exitcode
        try:
            process.join(timeout=0)
        except Exception as exc:
            logger.warning(f"回收图床同步进程失败: {exc}")
        self.img_sync.sync_process = None

        status.update(
            {
                "running": False,
                "completed": True,
                "success": exit_code == 0,
                "exit_code": exit_code,
                "message": "同步任务已完成" if exit_code == 0 else "同步任务失败",
            }
        )
        self._last_img_host_sync_task_status = status.copy()
        return status

    def _start_img_host_sync_task(self, task: str) -> dict:
        status = self._get_img_host_sync_task_status()
        if not status.get("available", False):
            raise RuntimeError(status.get("message") or "图床服务未配置")
        if status.get("running"):
            raise RuntimeError("已有同步任务正在运行，请等待当前任务完成")

        self._last_img_host_sync_task_status = None
        self.img_sync.sync_process = self.img_sync._start_sync_process(task)
        return self._get_img_host_sync_task_status()

    async def _api_img_host_sync_status(self):
        try:
            if not self.img_sync:
                return jsonify({"error": "图床服务未配置"}), 400
            status = self.img_sync.check_status()
            status["upload_count"] = len(status.get("to_upload", []))
            status["download_count"] = len(status.get("to_download", []))
            status["remote_extra_count"] = len(status.get("to_delete_remote", []))
            status["local_extra_count"] = len(status.get("to_delete_local", []))
            status["provider_label"] = self._get_provider_label()
            return jsonify(status)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    async def _api_img_host_sync_upload(self):
        try:
            if not self.img_sync:
                return jsonify({"message": "图床服务未配置"}), 400
            task_status = self._start_img_host_sync_task("upload")
            return jsonify({"success": True, "task": task_status})
        except Exception as e:
            status_code = 409 if "已有同步任务" in str(e) else 500
            return jsonify({"message": str(e)}), status_code

    async def _api_img_host_sync_overwrite_to_remote(self):
        try:
            if not self.img_sync:
                return jsonify({"message": "图床服务未配置"}), 400
            task_status = self._start_img_host_sync_task("overwrite_to_remote")
            return jsonify({"success": True, "task": task_status})
        except Exception as e:
            status_code = 409 if "已有同步任务" in str(e) else 500
            return jsonify({"message": str(e)}), status_code

    async def _api_img_host_sync_overwrite_from_remote(self):
        try:
            if not self.img_sync:
                return jsonify({"message": "图床服务未配置"}), 400
            task_status = self._start_img_host_sync_task("overwrite_from_remote")
            return jsonify({"success": True, "task": task_status})
        except Exception as e:
            status_code = 409 if "已有同步任务" in str(e) else 500
            return jsonify({"message": str(e)}), status_code

    async def _api_img_host_sync_download(self):
        try:
            if not self.img_sync:
                return jsonify({"message": "图床服务未配置"}), 400
            task_status = self._start_img_host_sync_task("download")
            return jsonify({"success": True, "task": task_status})
        except Exception as e:
            status_code = 409 if "已有同步任务" in str(e) else 500
            return jsonify({"message": str(e)}), status_code

    async def _api_img_host_sync_task_status(self):
        return jsonify(self._get_img_host_sync_task_status())

    async def _api_img_host_sync_progress(self):
        async def generate():
            while True:
                status = self._get_img_host_sync_task_status()
                yield f"data: {json.dumps(status)}\n\n"
                if status.get("completed"):
                    return
                if status.get("running"):
                    await asyncio.sleep(1)
                else:
                    return

        response = await make_response(
            generate(),
            {
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        response.timeout = None
        return response

    async def _api_serve_meme_image(self):
        category = request.args.get("category", "")
        filename = request.args.get("filename", "")
        file_path = (MEMES_DIR / category / filename).resolve()
        if not str(file_path).startswith(str(MEMES_DIR.resolve())):
            return jsonify({"status": "error", "message": "Invalid path"}), 403
        if not file_path.exists():
            return jsonify({"status": "error", "message": "File not found"}), 404
        return await send_file(str(file_path))

    async def _api_get_meme_image_data(self):
        category = request.args.get("category", "")
        filename = request.args.get("filename", "")
        size = request.args.get("size", "preview")
        file_path = (MEMES_DIR / category / filename).resolve()
        memes_root = MEMES_DIR.resolve()

        try:
            file_path.relative_to(memes_root)
        except ValueError:
            return jsonify({"status": "error", "message": "Invalid path"}), 403

        if not file_path.exists() or not file_path.is_file():
            return jsonify({"status": "error", "message": "File not found"}), 404

        max_bytes = (
            MAX_ORIGINAL_IMAGE_BYTES if size == "original" else MAX_PREVIEW_IMAGE_BYTES
        )
        file_size = file_path.stat().st_size
        if file_size > max_bytes:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Image is too large to preview in the plugin page",
                        "size": file_size,
                        "max_size": max_bytes,
                    }
                ),
                413,
            )

        mime_type = (
            mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        )
        if size == "preview" and mime_type != "image/gif":
            try:
                data_url, mime_type = self._build_preview_data_url(file_path)
            except Exception as exc:
                logger.warning(f"生成预览缩略图失败，回退原图数据: {exc}")
                data_url = self._build_file_data_url(file_path, mime_type)
        else:
            data_url = self._build_file_data_url(file_path, mime_type)

        return jsonify(
            {
                "category": category,
                "filename": filename,
                "mime_type": mime_type,
                "size": file_size,
                "data_url": data_url,
            }
        )

    @staticmethod
    def _build_file_data_url(file_path, mime_type: str) -> str:
        with open(file_path, "rb") as image_file:
            encoded = base64.b64encode(image_file.read()).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    @staticmethod
    def _build_preview_data_url(file_path) -> tuple[str, str]:
        resample_filter = getattr(
            getattr(PILImage, "Resampling", PILImage),
            "LANCZOS",
            PILImage.BICUBIC,
        )
        with PILImage.open(file_path) as image:
            image.thumbnail(
                (PREVIEW_IMAGE_MAX_DIMENSION, PREVIEW_IMAGE_MAX_DIMENSION),
                resample_filter,
            )
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA")
            output = io.BytesIO()
            image.save(output, format="WEBP", quality=82, method=4)
        encoded = base64.b64encode(output.getvalue()).decode("ascii")
        return f"data:image/webp;base64,{encoded}", "image/webp"

    def _get_manageable_categories(self) -> set[str]:
        """Return the union of configured and local categories."""
        return (
            set(self.category_manager.get_descriptions())
            | self.category_manager.get_local_categories()
        )

    def _extract_category_description_from_command(
        self, event: AstrMessageEvent, category: str
    ) -> str:
        command_prefix = "表情管理 添加分类"
        message = re.sub(r"\s+", " ", event.get_message_str().strip())
        if not message.startswith(command_prefix):
            return ""

        remaining = message[len(command_prefix) :].strip()
        if remaining == category:
            return ""
        if not remaining.startswith(f"{category} "):
            return ""

        return remaining[len(category) :].strip()

    async def _wait_for_category_description(
        self, event: AstrMessageEvent, category: str, timeout: int = 60
    ) -> str:
        description = ""

        @session_waiter(timeout=timeout, record_history_chains=False)
        async def description_waiter(
            controller: SessionController, description_event: AstrMessageEvent
        ) -> None:
            nonlocal description
            reply = (description_event.message_str or "").strip()

            if reply == "返回":
                await description_event.send(
                    description_event.plain_result("已取消创建分类。")
                )
                controller.stop(CategoryCreationCancelled())
                return

            if not reply:
                await description_event.send(
                    description_event.plain_result(
                        f"请发送分类「{category}」的描述，或发送“返回”取消创建。"
                    )
                )
                controller.keep(timeout=timeout, reset_timeout=True)
                return

            description = reply
            controller.stop()

        await description_waiter(event, SenderScopedSessionFilter())
        return description

    async def _wait_for_command_confirmation(
        self, event: AstrMessageEvent, timeout: int = 30
    ) -> bool:
        """Wait for the same sender to reply with confirmation text."""

        @session_waiter(timeout=timeout, record_history_chains=False)
        async def confirmation_waiter(
            controller: SessionController, confirm_event: AstrMessageEvent
        ) -> None:
            reply = (confirm_event.message_str or "").strip()

            if reply in {"确认", "确定"}:
                controller.stop()
                return

            if reply in {"取消", "退出"}:
                await confirm_event.send(confirm_event.plain_result("已取消本次操作。"))
                controller.stop(ConfirmationCancelled())
                return

            await confirm_event.send(
                confirm_event.plain_result(
                    "请回复“确认”继续执行，或回复“取消”终止本次操作。"
                )
            )
            controller.keep(timeout=timeout, reset_timeout=True)

        try:
            await confirmation_waiter(event, SenderScopedSessionFilter())
            return True
        except TimeoutError:
            await event.send(event.plain_result("⌛ 等待确认超时，操作已取消。"))
            return False
        except ConfirmationCancelled:
            return False

    def _format_category_counts(
        self, category_counts: dict[str, int], limit: int = 8
    ) -> str:
        """Render a compact category count summary for confirmation prompts."""
        non_empty_items = [
            (category, count)
            for category, count in sorted(category_counts.items())
            if count > 0
        ]
        if not non_empty_items:
            return "无可删除的表情包文件。"

        lines = [
            f"- {category}: {count} 个" for category, count in non_empty_items[:limit]
        ]
        if len(non_empty_items) > limit:
            lines.append(f"- 其余 {len(non_empty_items) - limit} 个类型已省略")
        return "\n".join(lines)

    def _ensure_default_category_descriptions(self, categories: list[str]) -> None:
        """为恢复出来但缺少描述的默认类别补回默认描述。"""
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

    def _build_meme_prompt(self, category_mapping_string: str | None = None) -> str:
        mapping_string = category_mapping_string or self.category_mapping_string
        return (
            self.prompt_head
            + mapping_string
            + self.prompt_tail_1
            + str(self.max_emotions_per_message)
            + self.prompt_tail_2
        )

    def _resolve_persona_id(
        self,
        event: AstrMessageEvent | None = None,
        req: ProviderRequest | None = None,
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
        self,
        event: AstrMessageEvent | None = None,
        req: ProviderRequest | None = None,
    ) -> dict:
        session_id = ""
        if req:
            session_id = str(req.session_id or "").strip()

        if not session_id and event is not None:
            session_id = str(getattr(event, "session_id", "") or "").strip()

        persona_id = self._resolve_persona_id(event=event, req=req)
        context = resolve_pack_context(
            session_id=session_id or None,
            persona_id=persona_id,
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
        """重新加载表情配置并构建提示词并注入全局人格"""
        pack_context = self._resolve_runtime_pack_context()
        self.category_mapping = pack_context.get("category_mapping") or load_json(
            MEMES_DATA_PATH, DEFAULT_CATEGORY_DESCRIPTIONS
        )
        self.category_mapping_string = dict_to_string(self.category_mapping)
        self._apply_persona_prompts()

    @meme_manager.command("查看图库")
    async def list_emotions(self, event: AstrMessageEvent):
        """查看所有可用表情包类别"""
        pack_context = self._resolve_runtime_pack_context(event=event)
        descriptions = pack_context.get("category_mapping") or self.category_mapping
        categories = "\n".join(
            [f"- {tag}: {desc}" for tag, desc in descriptions.items()]
        )
        yield event.plain_result(f"🖼️ 当前图库：\n{categories}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("添加分类")
    async def add_category_command(self, event: AstrMessageEvent, category: str = None):
        """创建新的表情包分类。"""
        if not category:
            yield event.plain_result(
                "📌 若要添加分类，请按照此格式操作：\n"
                "/表情管理 添加分类 [类别名称] [描述]\n"
                "也可以只发送类别名称，随后按提示补充描述。"
            )
            return

        category = category.strip()
        if category in self._get_manageable_categories():
            yield event.plain_result(f"ℹ️ 分类「{category}」已存在，无需重复创建。")
            return

        description = self._extract_category_description_from_command(event, category)
        if not description:
            yield event.plain_result(
                f"请发送分类「{category}」的描述，或发送“返回”取消创建。"
            )
            try:
                description = await self._wait_for_category_description(event, category)
            except TimeoutError:
                yield event.plain_result("⌛ 等待描述超时，已取消创建分类。")
                return
            except CategoryCreationCancelled:
                return

        if not self.category_manager.create_category(category, description):
            yield event.plain_result(f"❌ 创建分类「{category}」失败，请稍后重试。")
            return

        self._reload_personas()
        yield event.plain_result(f"✅ 已创建分类「{category}」：{description}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("添加表情")
    async def upload_meme(self, event: AstrMessageEvent, category: str = None):
        """上传表情包到指定类别"""
        if not category:
            yield event.plain_result(
                "📌 若要添加表情，请按照此格式操作：\n/表情管理 添加表情 [类别名称]\n（输入/查看图库 可获取类别列表）"
            )
            return

        if category not in self.category_manager.get_descriptions():
            yield event.plain_result(
                f"您输入的表情包类别「{category}」是无效的哦。\n可以使用/查看表情包来查看可用的类别。"
            )
            return

        user_key = f"{event.session_id}_{event.get_sender_id()}"
        self.upload_states[user_key] = {
            "category": category,
            "expire_time": time.time() + 30,
        }
        yield event.plain_result(
            f"请在30秒内发送要添加到【{category}】类别的图片（可发送多张图片）。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("恢复默认表情包")
    async def restore_default_memes_command(
        self, event: AstrMessageEvent, category: str = None
    ):
        """恢复内置默认表情包，可指定类别或恢复全部。"""
        available_default_categories = get_default_meme_categories()
        if not available_default_categories:
            yield event.plain_result("❌ 未找到插件内置默认表情包资源。")
            return

        normalized_category = category.strip() if category else None
        if (
            normalized_category
            and normalized_category not in available_default_categories
        ):
            category_list = "\n".join(
                f"- {name}" for name in available_default_categories
            )
            yield event.plain_result(
                f"⚠️ 默认表情包中不存在类别「{normalized_category}」。\n"
                f"当前可恢复的默认类别如下：\n{category_list}"
            )
            return

        restore_result = restore_default_memes(normalized_category)
        if not restore_result["source_exists"]:
            yield event.plain_result("❌ 未找到插件内置默认表情包资源。")
            return

        copied_files = restore_result["copied_files"]
        duplicate_files = restore_result["duplicate_files"]
        renamed_files = restore_result["renamed_files"]
        restored_categories = sorted(
            set(copied_files) | set(duplicate_files) | set(renamed_files)
        )

        if restored_categories:
            self._ensure_default_category_descriptions(restored_categories)

        copied_count = sum(len(files) for files in copied_files.values())
        duplicate_count = sum(len(files) for files in duplicate_files.values())
        renamed_count = sum(len(files) for files in renamed_files.values())

        if copied_count == 0 and duplicate_count > 0:
            yield event.plain_result(
                (
                    "ℹ️ 默认表情包已存在，本次未新增文件。"
                    if not normalized_category
                    else f"ℹ️ 类别「{normalized_category}」的默认表情包已存在，本次未新增文件。"
                )
            )
            return

        if copied_count == 0:
            yield event.plain_result("ℹ️ 本次没有恢复任何默认表情包文件。")
            return

        if normalized_category:
            yield event.plain_result(
                f"✅ 已恢复类别「{normalized_category}」的默认表情包，共新增 {copied_count} 个文件"
                f"{f'，其中 {renamed_count} 个因重名自动补序号' if renamed_count > 0 else ''}"
                f"{f'，跳过 {duplicate_count} 个重复文件' if duplicate_count > 0 else ''}。"
            )
            return

        yield event.plain_result(
            f"✅ 已恢复全部默认表情包，共新增 {copied_count} 个文件，涉及 {len(copied_files)} 个类别"
            f"{f'，其中 {renamed_count} 个因重名自动补序号' if renamed_count > 0 else ''}"
            f"{f'，跳过 {duplicate_count} 个重复文件' if duplicate_count > 0 else ''}。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("清空指定类型")
    async def clear_category_command(
        self, event: AstrMessageEvent, category: str = None
    ):
        """清空指定类型下的所有表情包，但保留类型本身。"""
        if not category:
            yield event.plain_result(
                "📌 若要清空指定类型，请按照此格式操作：\n/表情管理 清空指定类型 [类别名称]"
            )
            return

        category = category.strip()
        available_categories = self._get_manageable_categories()
        if category not in available_categories:
            yield event.plain_result(
                f"⚠️ 未找到类型「{category}」。\n可先使用 /表情管理 查看图库 查看当前类型。"
            )
            return

        emoji_count = len(get_emoji_by_category(category))
        if emoji_count == 0:
            yield event.plain_result(f"📭 类型「{category}」当前没有可清空的表情包。")
            return

        yield event.plain_result(
            f"⚠️ 即将清空类型「{category}」下的 {emoji_count} 个表情包，但会保留类型本身。\n"
            "请在 30 秒内回复“确认”继续执行，或回复“取消”终止本次操作。"
        )
        if not await self._wait_for_command_confirmation(event):
            return

        result = clear_category_emojis(category)
        deleted_count = len(result["deleted_files"])
        yield event.plain_result(
            f"✅ 已清空类型「{category}」，共删除 {deleted_count} 个表情包。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("清空全部")
    async def clear_all_emojis_command(self, event: AstrMessageEvent):
        """清空所有类型下的表情包，但保留类型和描述配置。"""
        available_categories = sorted(self._get_manageable_categories())
        category_counts = {
            category: len(get_emoji_by_category(category))
            for category in available_categories
        }
        total_count = sum(category_counts.values())

        if total_count == 0:
            yield event.plain_result("📭 当前没有可清空的表情包文件。")
            return

        category_count = sum(1 for count in category_counts.values() if count > 0)
        summary = self._format_category_counts(category_counts)
        yield event.plain_result(
            f"⚠️ 即将清空全部表情包，共 {total_count} 个文件，涉及 {category_count} 个类型。\n"
            "该操作会保留所有类型名称和描述配置。\n"
            f"{summary}\n"
            "请在 30 秒内回复“确认”继续执行，或回复“取消”终止本次操作。"
        )
        if not await self._wait_for_command_confirmation(event):
            return

        result = clear_all_emojis()
        deleted_total = sum(result["deleted_by_category"].values())
        yield event.plain_result(
            f"✅ 已清空全部表情包，共删除 {deleted_total} 个文件，类型配置已保留。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("删除类型本身")
    async def delete_category_command(
        self, event: AstrMessageEvent, category: str = None
    ):
        """删除指定类型本身，同时移除其描述配置和本地文件夹。"""
        if not category:
            yield event.plain_result(
                "📌 若要删除类型本身，请按照此格式操作：\n/表情管理 删除类型本身 [类别名称]"
            )
            return

        category = category.strip()
        available_categories = self._get_manageable_categories()
        if category not in available_categories:
            yield event.plain_result(
                f"⚠️ 未找到类型「{category}」。\n可先使用 /表情管理 查看图库 查看当前类型。"
            )
            return

        emoji_count = len(get_emoji_by_category(category))
        yield event.plain_result(
            f"⚠️ 即将删除类型「{category}」本身，并移除其描述配置"
            f"{f'，同时删除其中的 {emoji_count} 个表情包' if emoji_count > 0 else ''}。\n"
            "该操作不可恢复。\n"
            "请在 30 秒内回复“确认”继续执行，或回复“取消”终止本次操作。"
        )
        if not await self._wait_for_command_confirmation(event):
            return

        if not self.category_manager.delete_category(category):
            yield event.plain_result(f"❌ 删除类型「{category}」失败，请稍后重试。")
            return

        self._reload_personas()
        yield event.plain_result(
            f"✅ 已删除类型「{category}」"
            f"{f'，并移除 {emoji_count} 个表情包。' if emoji_count > 0 else '。'}"
        )

    @filter.event_message_type(EventMessageType.ALL)
    async def handle_upload_image(self, event: AstrMessageEvent):
        """处理用户上传的图片"""
        user_key = f"{event.session_id}_{event.get_sender_id()}"
        upload_state = self.upload_states.get(user_key)

        if not upload_state or time.time() > upload_state["expire_time"]:
            if user_key in self.upload_states:
                del self.upload_states[user_key]
            return

        images = [c for c in event.message_obj.message if isinstance(c, Image)]

        if not images:
            yield event.plain_result("请发送图片文件来进行上传哦。")
            return

        category = upload_state["category"]
        save_dir = os.path.join(MEMES_DIR, category)

        try:
            os.makedirs(save_dir, exist_ok=True)
            saved_files = []

            # 创建忽略 SSL 验证的上下文
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

            for idx, img in enumerate(images, 1):
                timestamp = int(time.time())

                try:
                    # 特殊处理腾讯多媒体域名
                    if "multimedia.nt.qq.com.cn" in img.url:
                        insecure_url = img.url.replace("https://", "http://", 1)
                        logger.warning(
                            f"检测到腾讯多媒体域名，使用 HTTP 协议下载: {insecure_url}"
                        )
                        async with aiohttp.ClientSession() as session:
                            async with session.get(insecure_url) as resp:
                                content = await resp.read()
                    else:
                        async with aiohttp.ClientSession(
                            connector=aiohttp.TCPConnector(ssl=ssl_context)
                        ) as session:
                            async with session.get(img.url) as resp:
                                content = await resp.read()

                    try:
                        with PILImage.open(io.BytesIO(content)) as img:
                            file_type = img.format.lower()
                    except Exception as e:
                        logger.error(f"图片格式检测失败: {str(e)}")
                        file_type = "unknown"

                    ext_mapping = {
                        "jpeg": ".jpg",
                        "png": ".png",
                        "gif": ".gif",
                        "webp": ".webp",
                    }
                    ext = ext_mapping.get(file_type, ".bin")
                    filename = f"{timestamp}_{idx}{ext}"
                    save_path = os.path.join(save_dir, filename)

                    with open(save_path, "wb") as f:
                        f.write(content)
                    saved_files.append(filename)

                except Exception as e:
                    logger.error(f"下载图片失败: {str(e)}")
                    yield event.plain_result(f"文件 {img.url} 下载失败啦: {str(e)}")
                    continue

            del self.upload_states[user_key]

            # 基础成功消息
            result_msg = [
                Plain(
                    f"✅ 已经成功收录了 {len(saved_files)} 张新表情到「{category}」图库！"
                )
            ]

            # 如果配置了图床，提示用户需要手动同步
            if self.img_sync:
                result_msg.append(Plain("\n"))
                result_msg.append(
                    Plain("☁️ 检测到已配置图床，如需同步到云端请使用命令：同步到云端")
                )

            yield event.chain_result(result_msg)
            await self.reload_emotions()

        except Exception as e:
            yield event.plain_result(f"保存失败了：{str(e)}")

    async def reload_emotions(self):
        """动态重新加载表情配置"""
        try:
            self.category_manager.sync_with_filesystem()
            # 重新加载表情配置后，需要重新构建提示词
            self._reload_personas()
        except Exception as e:
            logger.error(f"重新加载表情配置失败: {str(e)}")

    def _is_position_in_thinking_tags(self, text: str, position: int) -> bool:
        """检查指定位置是否在thinking标签内

        Args:
            text: 原始文本
            position: 要检查的位置

        Returns:
            True如果位置在thinking标签内，False否则
        """
        # 找到所有thinking标签的开始和结束位置
        thinking_pattern = re.compile(
            r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE
        )

        for match in thinking_pattern.finditer(text):
            if match.start() <= position < match.end():
                return True
        return False

    def _check_meme_directories(self):
        """检查表情包目录是否存在并且包含图片"""
        logger.info(f"开始检查表情包根目录: {MEMES_DIR}")
        if not os.path.exists(MEMES_DIR):
            logger.error(f"表情包根目录不存在，请检查: {MEMES_DIR}")
            return

        for emotion in self.category_manager.get_descriptions().values():
            emotion_path = os.path.join(MEMES_DIR, emotion)
            if not os.path.exists(emotion_path):
                logger.error(
                    f"表情分类 {emotion} 对应的目录不存在，请查看: {emotion_path}"
                )
                continue

            memes = [
                f
                for f in os.listdir(emotion_path)
                if f.endswith((".jpg", ".png", ".gif"))
            ]
            if not memes:
                logger.error(f"表情分类 {emotion} 对应的目录为空: {emotion_path}")
            else:
                logger.info(
                    f"表情分类 {emotion} 对应的目录 {emotion_path} 包含 {len(memes)} 个图片"
                )

    @filter.on_llm_request(priority=99999)
    async def inject_meme_prompt(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """Ensure edited personas still get the meme prompt before LLM calls."""
        self._apply_request_prompt(req, event)

    @filter.on_llm_response(priority=99999)
    async def resp(self, event: AstrMessageEvent, response: LLMResponse):
        """处理 LLM 响应，识别表情"""

        if not response or not response.completion_text:
            return

        text = response.completion_text

        pack_context = self._resolve_runtime_pack_context(event=event)
        runtime_category_mapping = (
            pack_context.get("category_mapping") or self.category_mapping
        )

        self.found_emotions = []  # 重置表情列表
        valid_emoticons = set(runtime_category_mapping.keys())

        clean_text = text

        # 第一阶段：严格匹配符号包裹的表情
        hex_pattern = r"&&([^&&]+)&&"
        matches = re.finditer(hex_pattern, clean_text)

        # 严格模式处理
        temp_replacements = []
        strict_emotions = []
        for match in matches:
            original = match.group(0)
            emotion = match.group(1).strip()

            # 合法性验证
            if emotion in valid_emoticons:
                temp_replacements.append((original, emotion))
                strict_emotions.append(emotion)
            else:
                temp_replacements.append((original, ""))  # 非法表情静默移除

        # 保持原始顺序替换
        for original, emotion in temp_replacements:
            clean_text = clean_text.replace(original, "", 1)  # 每次替换第一个匹配项
            if emotion:
                self.found_emotions.append(emotion)

        # 第二阶段：替代标记处理（如[emotion]、(emotion)等）
        if self.config.get("enable_alternative_markup", True):
            remove_invalid_markup = self.remove_invalid_alternative_markup
            # 处理[emotion]格式
            bracket_pattern = r"\[([^\[\]]+)\]"
            matches = re.finditer(bracket_pattern, clean_text)
            bracket_replacements = []
            invalid_brackets = [] if remove_invalid_markup else None

            for match in matches:
                original = match.group(0)
                emotion = match.group(1).strip()

                if emotion in valid_emoticons:
                    bracket_replacements.append((original, emotion))
                elif remove_invalid_markup:
                    invalid_brackets.append(original)

            if remove_invalid_markup:
                for invalid in invalid_brackets:
                    clean_text = clean_text.replace(invalid, "", 1)

            for original, emotion in bracket_replacements:
                clean_text = clean_text.replace(original, "", 1)
                self.found_emotions.append(emotion)

            # 处理(emotion)格式
            paren_pattern = r"\(([^()]+)\)"
            matches = re.finditer(paren_pattern, clean_text)
            paren_replacements = []
            invalid_parens = [] if remove_invalid_markup else None

            for match in matches:
                original = match.group(0)
                emotion = match.group(1).strip()

                if emotion in valid_emoticons:
                    # 需要额外验证，确保不是普通句子的一部分
                    if self._is_likely_emotion_markup(
                        original, clean_text, match.start()
                    ):
                        paren_replacements.append((original, emotion))
                elif remove_invalid_markup:
                    invalid_parens.append(original)

            if remove_invalid_markup:
                for invalid in invalid_parens:
                    clean_text = clean_text.replace(invalid, "", 1)

            for original, emotion in paren_replacements:
                clean_text = clean_text.replace(original, "", 1)
                self.found_emotions.append(emotion)

        # 第三阶段：处理重复表情模式（如angryangryangry）
        repeated_emotions = []
        if self.config.get("enable_repeated_emotion_detection", True):
            high_confidence_emotions = self.config.get("high_confidence_emotions", [])

            for emotion in valid_emoticons:
                # 跳过太短的表情词，避免误判
                if len(emotion) < 3:
                    continue

                # 对高置信度表情，重复两次即可识别
                if emotion in high_confidence_emotions:
                    # 检测重复两次的模式，如 happyhappy
                    repeat_pattern = f"({re.escape(emotion)})\\1{{1,}}"
                    matches = re.finditer(repeat_pattern, clean_text)
                    for match in matches:
                        # 跳过thinking标签内的内容
                        if self._is_position_in_thinking_tags(
                            clean_text, match.start()
                        ):
                            continue
                        original = match.group(0)
                        clean_text = clean_text.replace(original, "", 1)
                        self.found_emotions.append(emotion)
                        repeated_emotions.append(emotion)
                else:
                    # 普通表情词需要重复至少3次才识别
                    # 只检查长度>=4的表情，以减少误判
                    if len(emotion) >= 4:
                        # 查找表情词重复3次以上的模式
                        repeat_pattern = f"({re.escape(emotion)})\\1{{2,}}"
                        matches = re.finditer(repeat_pattern, clean_text)
                        for match in matches:
                            # 跳过thinking标签内的内容
                            if self._is_position_in_thinking_tags(
                                clean_text, match.start()
                            ):
                                continue
                            original = match.group(0)
                            clean_text = clean_text.replace(original, "", 1)
                            self.found_emotions.append(emotion)
                            repeated_emotions.append(emotion)

        logger.debug(f"[meme_manager] 重复检测阶段找到的表情: {repeated_emotions}")

        # 第四阶段：智能识别可能的表情（松散模式）
        loose_emotions = []
        if self.config.get("enable_loose_emotion_matching", True):
            # 查找所有可能的表情词
            for emotion in valid_emoticons:
                # 使用单词边界确保不是其他单词的一部分
                pattern = r"\b(" + re.escape(emotion) + r")\b"
                for match in re.finditer(pattern, clean_text):
                    word = match.group(1)
                    position = match.start()

                    # 跳过thinking标签内的内容
                    if self._is_position_in_thinking_tags(clean_text, position):
                        continue

                    # 判断是否可能是表情而非英文单词
                    if self._is_likely_emotion(
                        word, clean_text, position, valid_emoticons
                    ):
                        # 添加到表情列表
                        self.found_emotions.append(word)
                        loose_emotions.append(word)
                        # 替换文本中的表情词
                        clean_text = (
                            clean_text[:position] + clean_text[position + len(word) :]
                        )

        logger.debug(f"[meme_manager] 松散匹配阶段找到的表情: {loose_emotions}")

        if self.emotion_llm_enabled:
            try:
                provider_id = self.emotion_llm_provider_id
                if not provider_id:
                    provider_id = await self.context.get_current_chat_provider_id(
                        umo=event.unified_msg_origin
                    )
                if provider_id:
                    valid_list = sorted(valid_emoticons)
                    prompt = (
                        "你是表情标签选择器，只能从给定标签中选择。\n"
                        "请基于文本语义判断需要的表情，返回JSON格式："
                        '{"emotions":["tag1","tag2"]}。\n'
                        "只输出JSON，不要解释。\n"
                        f"可用标签: {', '.join(valid_list)}\n"
                        f"文本: {clean_text}"
                    )
                    llm_resp = await self.context.llm_generate(
                        chat_provider_id=provider_id, prompt=prompt
                    )
                    if llm_resp and llm_resp.completion_text:
                        raw_text = llm_resp.completion_text.strip()
                        data = None
                        try:
                            data = json.loads(raw_text)
                        except Exception:
                            match = re.search(r"\{[\s\S]*\}", raw_text)
                            if match:
                                try:
                                    data = json.loads(match.group(0))
                                except Exception:
                                    data = None
                        if isinstance(data, dict):
                            emotions = data.get("emotions")
                            if isinstance(emotions, list):
                                for emo in emotions:
                                    if isinstance(emo, str) and emo in valid_emoticons:
                                        self.found_emotions.append(emo)
                            elif (
                                isinstance(emotions, str)
                                and emotions in valid_emoticons
                            ):
                                self.found_emotions.append(emotions)
            except Exception as e:
                logger.error(f"[meme_manager] 情感模型调用失败: {e}")

        # 去重并应用数量限制
        seen = set()
        filtered_emotions = []
        for emo in self.found_emotions:
            if emo not in seen:
                seen.add(emo)
                filtered_emotions.append(emo)
            if len(filtered_emotions) >= self.max_emotions_per_message:
                break

        self.found_emotions = filtered_emotions
        logger.info(f"[meme_manager] 去重后的最终表情列表: {self.found_emotions}")

        # 防御性清理残留符号
        clean_text = re.sub(r"&&+", "", clean_text)  # 清除未成对的&&符号
        response.completion_text = clean_text.strip()
        logger.debug(
            f"[meme_manager] 清理后的最终文本内容长度: {len(response.completion_text)}"
        )

        # webchat 流式场景：在 "complete" 入队前发送干净文本，替换客户端已显示的含标记脏文本
        result = event.get_result()
        if (
            event.get_platform_name() == "webchat"
            and result is not None
            and result.result_content_type == ResultContentType.STREAMING_RESULT
        ):
            try:
                await event.send(MessageChain([Plain(response.completion_text)]))
                logger.debug("[meme_manager] webchat 流式文本已替换为干净版本")
            except Exception as e:
                logger.error(f"[meme_manager] webchat 流式文本替换失败: {e}")

    def _is_likely_emotion_markup(self, markup, text, position):
        """判断一个标记是否可能是表情而非普通文本的一部分"""
        # 获取标记前后的文本
        before_text = text[:position].strip()
        after_text = text[position + len(markup) :].strip()

        # 如果是在中文上下文中，更可能是表情
        has_chinese_before = bool(
            re.search(r"[\u4e00-\u9fff]", before_text[-1:] if before_text else "")
        )
        has_chinese_after = bool(
            re.search(r"[\u4e00-\u9fff]", after_text[:1] if after_text else "")
        )
        if has_chinese_before or has_chinese_after:
            return True

        # 如果在数字标记中，可能是引用标记如[1]，不是表情
        if re.match(r"\[\d+\]", markup):
            return False

        # 如果标记内有空格，可能是普通句子，不是表情
        if " " in markup[1:-1]:
            return False

        # 如果标记前后是完整的英文句子，可能不是表情
        english_context_before = bool(re.search(r"[a-zA-Z]\s+$", before_text))
        english_context_after = bool(re.search(r"^\s+[a-zA-Z]", after_text))
        if english_context_before and english_context_after:
            return False

        # 默认情况下认为可能是表情
        return True

    def _is_likely_emotion(self, word, text, position, valid_emotions):
        """判断一个单词是否可能是表情而非普通英文单词"""

        # 先获取上下文
        before_text = text[:position].strip()
        after_text = text[position + len(word) :].strip()

        # 规则1：检查是否在英文上下文中
        # 如果前面有英文单词+空格，或后面有空格+英文单词，可能是英文上下文
        english_context_before = bool(re.search(r"[a-zA-Z]\s+$", before_text))
        english_context_after = bool(re.search(r"^\s+[a-zA-Z]", after_text))

        # 在英文上下文中，不太可能是表情
        if english_context_before or english_context_after:
            return False

        # 规则2：前后有中文字符，更可能是表情
        has_chinese_before = bool(
            re.search(r"[\u4e00-\u9fff]", before_text[-1:] if before_text else "")
        )
        has_chinese_after = bool(
            re.search(r"[\u4e00-\u9fff]", after_text[:1] if after_text else "")
        )

        if has_chinese_before or has_chinese_after:
            return True

        # 规则3：如果是句子开头或结尾，可能是表情
        if not before_text or before_text.endswith(
            ("。", "，", "！", "？", ".", ",", ":", ";", "!", "?", "\n")
        ):
            return True

        # 规则4：如果前后都是标点或空格，可能是表情
        if (not before_text or before_text[-1] in " \t\n.,!?;:'\"()[]{}") and (
            not after_text or after_text[0] in " \t\n.,!?;:'\"()[]{}"
        ):
            return True

        # 规则5：如果是已知的表情占比很高(>=70%)的单词，即使在英文上下文中也可能是表情
        if word in self.config.get("high_confidence_emotions", []):
            return True

        return False

    def _convert_to_gif(self, image_path: str) -> str:
        """
        将静态图片转换为 GIF 格式。
        如果图片已经是 GIF，则返回原路径。
        如果转换成功，返回临时 GIF 文件的路径。
        """
        if not self.convert_static_to_gif:
            return image_path

        if image_path.lower().endswith(".gif"):
            return image_path

        try:
            with PILImage.open(image_path) as img:
                # 检查是否已经是 GIF (虽然后缀不是 .gif，但内容可能是)
                if img.format == "GIF":
                    return image_path

                # 创建临时文件
                temp_dir = tempfile.gettempdir()
                temp_filename = os.path.join(
                    temp_dir,
                    f"meme_{int(time.time())}_{random.randint(1000, 9999)}.gif",
                )

                # 转换为 RGB (如果是 RGBA 需要处理透明度)
                if img.mode in ("RGBA", "LA") or (
                    img.mode == "P" and "transparency" in img.info
                ):
                    # 创建白色背景
                    background = PILImage.new("RGB", img.size, (255, 255, 255))
                    if img.mode == "P":
                        img = img.convert("RGBA")
                    background.paste(img, mask=img.split()[3])  # 3 is the alpha channel
                    img = background
                else:
                    img = img.convert("RGB")

                # 保存为 GIF
                img.save(temp_filename, "GIF")
                logger.debug(f"[meme_manager] 已将静态图转换为 GIF: {temp_filename}")
                return temp_filename
        except Exception as e:
            logger.error(f"[meme_manager] 转换图片为 GIF 失败: {e}")
            return image_path

    async def _send_memes_streaming(self, event: AstrMessageEvent):
        """流式传输兼容模式：在流式消息发送完成后，主动发送表情图片作为独立消息。"""
        if not self.found_emotions:
            return

        memes_root = self._get_runtime_memes_dir_for_event(event)

        try:
            random_value = random.randint(1, 100)
            if random_value > self.emotions_probability:
                return

            for emotion in self.found_emotions:
                if not emotion:
                    continue

                emotion_path = os.path.join(memes_root, emotion)
                if not os.path.exists(emotion_path):
                    continue

                memes = [
                    f
                    for f in os.listdir(emotion_path)
                    if f.endswith((".jpg", ".png", ".gif"))
                ]
                if not memes:
                    continue

                meme = random.choice(memes)
                meme_file = os.path.join(emotion_path, meme)
                final_meme_file = self._convert_to_gif(meme_file)

                try:
                    await self._send_meme_image(
                        event, Image.fromFileSystem(final_meme_file)
                    )
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
            logger.error(f"[meme_manager] 流式模式处理表情失败: {e}")
            logger.error(traceback.format_exc())
        finally:
            self.found_emotions = []

    async def _send_meme_image(self, event: AstrMessageEvent, image: Image) -> None:
        if event.get_platform_name() in {"gewechat", "webchat"}:
            await event.send(MessageChain([image]))
            return

        await self.context.send_message(event.unified_msg_origin, MessageChain([image]))

    @filter.on_decorating_result(priority=99999)
    async def on_decorating_result(self, event: AstrMessageEvent):
        """在消息发送前清理文本中的表情标签，并添加表情图片"""
        logger.debug("[meme_manager] on_decorating_result 开始处理")

        result = event.get_result()
        if not result:
            return

        # 流式传输兼容处理
        if result.result_content_type == ResultContentType.STREAMING_FINISH:
            if self.streaming_compatibility or event.get_platform_name() == "webchat":
                await self._send_memes_streaming(event)
            return

        try:
            # 第一步：获取并清理原始消息链中的文本
            original_chain = result.chain
            cleaned_components = []

            if original_chain:
                # 处理不同类型的消息链
                if isinstance(original_chain, str):
                    # 字符串类型：清理后转为 Plain 组件
                    cleaned = (
                        re.sub(self.content_cleanup_rule, "", original_chain)
                        if self.content_cleanup_rule
                        else original_chain
                    )
                    if cleaned.strip():
                        cleaned_components.append(Plain(cleaned.strip()))

                elif isinstance(original_chain, MessageChain):
                    # MessageChain 类型：遍历清理 Plain 组件
                    for component in original_chain.chain:
                        if isinstance(component, Plain):
                            cleaned = (
                                re.sub(self.content_cleanup_rule, "", component.text)
                                if self.content_cleanup_rule
                                else component.text
                            )
                            if cleaned.strip():
                                cleaned_components.append(Plain(cleaned.strip()))
                        else:
                            # 保留非文本组件（如已有的图片等）
                            cleaned_components.append(component)

                elif isinstance(original_chain, list):
                    # 列表类型：遍历清理 Plain 组件
                    for component in original_chain:
                        if isinstance(component, Plain):
                            cleaned = (
                                re.sub(self.content_cleanup_rule, "", component.text)
                                if self.content_cleanup_rule
                                else component.text
                            )
                            if cleaned.strip():
                                cleaned_components.append(Plain(cleaned.strip()))
                        else:
                            cleaned_components.append(component)

            # 第二步：添加表情图片（如果有找到的表情）
            if self.found_emotions:
                memes_root = self._get_runtime_memes_dir_for_event(event)
                # 检查概率（注意：概率判断是"小于等于"才发送）
                random_value = random.randint(1, 100)
                threshold = self.emotions_probability

                if random_value <= threshold:
                    # 创建表情图片列表
                    emotion_images = []
                    temp_files = []  # 记录临时文件路径
                    for emotion in self.found_emotions:
                        if not emotion:
                            continue

                        emotion_path = os.path.join(memes_root, emotion)
                        path_exists = os.path.exists(emotion_path)

                        if not path_exists:
                            continue

                        memes = [
                            f
                            for f in os.listdir(emotion_path)
                            if f.endswith((".jpg", ".png", ".gif"))
                        ]

                        if not memes:
                            continue

                        meme = random.choice(memes)
                        meme_file = os.path.join(emotion_path, meme)

                        try:
                            # 转换静态图为 GIF（如果配置开启）
                            final_meme_file = self._convert_to_gif(meme_file)
                            if final_meme_file != meme_file:
                                temp_files.append(final_meme_file)
                            emotion_images.append(Image.fromFileSystem(final_meme_file))
                        except Exception as e:
                            logger.error(f"添加表情图片失败: {e}")

                    if emotion_images:
                        # 记录临时文件到 event extra
                        if temp_files:
                            existing_temp_files = (
                                event.get_extra("meme_manager_temp_files") or []
                            )
                            event.set_extra(
                                "meme_manager_temp_files",
                                existing_temp_files + temp_files,
                            )

                        use_mixed_message = False
                        if self.enable_mixed_message:
                            use_mixed_message = (
                                random.randint(1, 100) <= self.mixed_message_probability
                            )

                        if use_mixed_message:
                            cleaned_components = self._merge_components_with_images(
                                cleaned_components, emotion_images
                            )
                        else:
                            event.set_extra(
                                "meme_manager_pending_images", emotion_images
                            )
                    else:
                        pass

                # 清空已处理的表情列表
                self.found_emotions = []

            # 第三步：更新消息链
            if cleaned_components:
                # 直接使用组件列表，不要包装在 MessageChain 中
                result.chain = cleaned_components
            elif original_chain:
                # 如果原本有内容但清理后为空，也要更新（避免发送带标签的空消息）
                # 进行最后的防御性清理
                if isinstance(original_chain, str):
                    final_cleaned = re.sub(
                        r"&&+", "", original_chain
                    )  # 清除残留的&&符号
                    if final_cleaned.strip():
                        result.chain = [Plain(final_cleaned.strip())]
                elif isinstance(original_chain, MessageChain):
                    # 对 MessageChain 中的每个 Plain 组件进行最后清理
                    final_components = []
                    for component in original_chain.chain:
                        if isinstance(component, Plain):
                            final_cleaned = re.sub(r"&&+", "", component.text)
                            if final_cleaned.strip():
                                final_components.append(Plain(final_cleaned.strip()))
                        else:
                            final_components.append(component)
                    if final_components:
                        result.chain = final_components

            logger.debug("[meme_manager] on_decorating_result 处理完成")

        except Exception as e:
            logger.error(f"处理消息装饰失败: {str(e)}")
            logger.error(traceback.format_exc())

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent):
        """消息发送后处理。用于发送未混合的表情图片。"""
        pending_images = event.get_extra("meme_manager_pending_images")

        try:
            if pending_images:
                for image in pending_images:
                    await self._send_meme_image(event, image)
        except Exception as e:
            logger.error(f"发送表情图片失败: {str(e)}")
            logger.error(traceback.format_exc())
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

    @meme_manager.command("同步状态")
    async def check_sync_status(self, event: AstrMessageEvent, detail: str = None):
        """检查表情包与图床的同步状态"""
        if not self.img_sync:
            yield event.plain_result(
                "图床服务尚未配置，请先在插件页面的配置中完成图床配置哦。"
            )
            return

        try:
            # 获取图床配置信息
            provider_name = self.img_sync.provider.__class__.__name__
            if hasattr(self.img_sync.provider, "bucket_name"):
                storage_info = f"存储桶: {self.img_sync.provider.bucket_name}"
            elif hasattr(self.img_sync.provider, "album_id"):
                storage_info = f"相册ID: {self.img_sync.provider.album_id}"
            else:
                storage_info = "未知存储类型"

            # 获取同步状态
            status = self.img_sync.check_status()
            to_upload = status.get("to_upload", [])
            to_download = status.get("to_download", [])

            # 统计信息
            result = [
                "📊 图床同步状态报告",
                "",
                f"🔧 图床服务: {provider_name}",
                f"📁 {storage_info}",
                "",
                "📈 文件统计:",
                f"  • 需要上传: {len(to_upload)} 个文件",
                f"  • 需要下载: {len(to_download)} 个文件",
                "",
            ]

            # 分类统计
            upload_categories = {}
            download_categories = {}

            for file in to_upload:
                cat = file.get("category", "未分类")
                upload_categories[cat] = upload_categories.get(cat, 0) + 1

            for file in to_download:
                cat = file.get("category", "未分类")
                download_categories[cat] = download_categories.get(cat, 0) + 1

            # 显示上传分类统计
            if upload_categories:
                result.append("📤 待上传文件分类:")
                for cat, count in sorted(
                    upload_categories.items(), key=lambda x: x[1], reverse=True
                ):
                    result.append(f"  • {cat}: {count} 个")
                result.append("")

            # 显示下载分类统计
            if download_categories:
                result.append("📥 待下载文件分类:")
                for cat, count in sorted(
                    download_categories.items(), key=lambda x: x[1], reverse=True
                ):
                    result.append(f"  • {cat}: {count} 个")
                result.append("")

            # 显示文件详情（最多各显示5个）
            if to_upload:
                result.append("📤 待上传文件示例（前5个）:")
                for file in to_upload[:5]:
                    result.append(
                        f"  • {file.get('category', '未分类')}/{file['filename']}"
                    )
                if len(to_upload) > 5:
                    result.append(f"  • ...还有 {len(to_upload) - 5} 个文件")
                result.append("")

            if to_download:
                result.append("📥 待下载文件示例（前5个）:")
                for file in to_download[:5]:
                    result.append(
                        f"  • {file.get('category', '未分类')}/{file['filename']}"
                    )
                if len(to_download) > 5:
                    result.append(f"  • ...还有 {len(to_download) - 5} 个文件")
                result.append("")

            # 同步状态总结
            if not to_upload and not to_download:
                result.append("✅ 云端与本地图库已经完全同步啦！")

                # 如果用户要求详细信息，显示更多内容
                if detail and detail.strip() == "详细":
                    result.append("")
                    result.append("📋 详细信息:")

                    # 显示所有文件类别的统计
                    try:
                        if hasattr(self.img_sync.provider, "get_image_list"):
                            remote_images = self.img_sync.provider.get_image_list()
                            remote_stats = {}
                            for img in remote_images:
                                cat = img.get("category", "未分类")
                                remote_stats[cat] = remote_stats.get(cat, 0) + 1

                            if remote_stats:
                                result.append("📂 云端文件分类详情:")
                                for cat, count in sorted(
                                    remote_stats.items(),
                                    key=lambda x: x[1],
                                    reverse=True,
                                ):
                                    result.append(f"  • {cat}: {count} 个")

                                # 显示文件总数
                                result.append(
                                    f"📊 云端总计: {len(remote_images)} 个文件"
                                )
                            else:
                                result.append("📂 云端无文件")
                    except Exception as e:
                        result.append(f"⚠️ 获取云端详情失败: {str(e)}")

                    # 显示本地图库统计
                    local_stats = {}
                    local_total = 0
                    if os.path.exists(MEMES_DIR):
                        for category in os.listdir(MEMES_DIR):
                            category_path = os.path.join(MEMES_DIR, category)
                            if os.path.isdir(category_path):
                                files = [
                                    f
                                    for f in os.listdir(category_path)
                                    if f.endswith(
                                        (".jpg", ".jpeg", ".png", ".gif", ".webp")
                                    )
                                ]
                                count = len(files)
                                local_stats[category] = count
                                local_total += count

                    if local_stats:
                        result.append("")
                        result.append("📂 本地文件分类详情:")
                        for cat, count in sorted(
                            local_stats.items(), key=lambda x: x[1], reverse=True
                        ):
                            result.append(f"  • {cat}: {count} 个")
                        result.append(f"📊 本地总计: {local_total} 个文件")
                    else:
                        result.append("")
                        result.append("📂 本地无文件")
            else:
                result.append("⏳ 需要同步以保持云端与本地图库一致")
                result.append(
                    "💡 使用 '/表情管理 同步到云端' 或 '/表情管理 从云端同步' 进行同步"
                )

            # 上传记录统计（如果有的话）
            if (
                hasattr(self.img_sync.sync_manager, "upload_tracker")
                and self.img_sync.sync_manager.upload_tracker
            ):
                try:
                    # 获取上传记录总数
                    if hasattr(
                        self.img_sync.sync_manager.upload_tracker, "get_uploaded_files"
                    ):
                        uploaded_files = (
                            self.img_sync.sync_manager.upload_tracker.get_uploaded_files()
                        )
                        result.append("")
                        result.append(
                            f"📝 上传记录: 已记录 {len(uploaded_files)} 个文件"
                        )
                except Exception:
                    pass  # 忽略获取上传记录时的错误

            yield event.plain_result("\n".join(result))
        except Exception as e:
            logger.error(f"检查同步状态失败: {str(e)}")
            yield event.plain_result(f"检查同步状态失败: {str(e)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("同步到云端")
    async def sync_to_remote(self, event: AstrMessageEvent):
        """将本地表情包同步到云端"""
        if not self.img_sync:
            yield event.plain_result(
                "图床服务尚未配置，请先在配置文件中完成图床配置哦。"
            )
            return

        try:
            yield event.plain_result("⚡ 正在开启云端同步任务...")
            success = await self.img_sync.start_sync("upload")
            if success:
                yield event.plain_result("云端同步已完成！")
            else:
                yield event.plain_result("云端同步失败，请查看日志哦。")
        except Exception as e:
            logger.error(f"同步到云端失败: {str(e)}")
            yield event.plain_result(f"同步到云端失败: {str(e)}")

    @meme_manager.command("图库统计")
    async def show_library_stats(self, event: AstrMessageEvent):
        """显示图库详细统计信息"""
        try:
            result = ["📊 表情包图库统计报告", "", "📁 本地图库统计:"]

            # 统计本地文件
            local_stats = {}
            local_total = 0

            if os.path.exists(MEMES_DIR):
                for category in os.listdir(MEMES_DIR):
                    category_path = os.path.join(MEMES_DIR, category)
                    if os.path.isdir(category_path):
                        files = [
                            f
                            for f in os.listdir(category_path)
                            if f.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp"))
                        ]
                        count = len(files)
                        local_stats[category] = count
                        local_total += count

            # 显示本地统计
            if local_stats:
                result.append(f"  • 总文件数: {local_total} 个")
                result.append(f"  • 分类数: {len(local_stats)} 个")
                result.append("")
                result.append("📂 本地分类详情:")
                for cat, count in sorted(
                    local_stats.items(), key=lambda x: x[1], reverse=True
                ):
                    result.append(f"  • {cat}: {count} 个")
            else:
                result.append("  • 本地图库为空")

            # 云端统计（如果配置了图床）
            if self.img_sync:
                result.append("")
                result.append("☁️ 云端图库统计:")

                try:
                    remote_images = self.img_sync.provider.get_image_list()
                    remote_stats = {}
                    remote_total = len(remote_images)

                    for img in remote_images:
                        cat = img.get("category", "未分类")
                        remote_stats[cat] = remote_stats.get(cat, 0) + 1

                    result.append(f"  • 总文件数: {remote_total} 个")
                    result.append(f"  • 分类数: {len(remote_stats)} 个")
                    result.append("")
                    result.append("📂 云端分类详情:")
                    for cat, count in sorted(
                        remote_stats.items(), key=lambda x: x[1], reverse=True
                    ):
                        result.append(f"  • {cat}: {count} 个")

                    # 对比统计
                    result.append("")
                    result.append("📈 本地与云端对比:")
                    result.append(f"  • 本地文件: {local_total} 个")
                    result.append(f"  • 云端文件: {remote_total} 个")

                    if local_total > remote_total:
                        result.append(
                            f"  • 本地比云端多 {local_total - remote_total} 个文件"
                        )
                    elif remote_total > local_total:
                        result.append(
                            f"  • 云端比本地多 {remote_total - local_total} 个文件"
                        )
                    else:
                        result.append("  • 本地与云端文件数相同")

                    # 分类对比
                    local_categories = set(local_stats.keys())
                    remote_categories = set(remote_stats.keys())

                    only_local = local_categories - remote_categories
                    only_remote = remote_categories - local_categories
                    common_categories = local_categories & remote_categories

                    if only_local:
                        result.append(
                            f"  • 仅本地有的分类: {', '.join(sorted(only_local))}"
                        )
                    if only_remote:
                        result.append(
                            f"  • 仅云端有的分类: {', '.join(sorted(only_remote))}"
                        )
                    if common_categories:
                        result.append(f"  • 共同分类: {len(common_categories)} 个")

                except Exception as e:
                    result.append(f"  • 获取云端统计失败: {str(e)}")
            else:
                result.append("")
                result.append("☁️ 云端图库: 未配置")

            # 存储空间估算
            result.append("")
            result.append("💾 存储空间估算:")
            if local_total > 0:
                # 假设平均每个文件 500KB
                estimated_size = local_total * 500 / 1024  # 转换为MB
                result.append(f"  • 本地图库约: {estimated_size:.1f} MB")

            if self.img_sync and "remote_total" in locals():
                estimated_remote_size = remote_total * 500 / 1024
                result.append(f"  • 云端图库约: {estimated_remote_size:.1f} MB")

            yield event.plain_result("\n".join(result))

        except Exception as e:
            logger.error(f"获取图库统计失败: {str(e)}")
            yield event.plain_result(f"获取图库统计失败: {str(e)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("从云端同步")
    async def sync_from_remote(self, event: AstrMessageEvent):
        """从云端同步表情包到本地"""
        if not self.img_sync:
            yield event.plain_result(
                "图床服务尚未配置，请先在配置文件中完成图床配置哦。"
            )
            return

        try:
            yield event.plain_result("开始从云端进行同步...")
            success = await self.img_sync.start_sync("download")
            if success:
                yield event.plain_result("从云端同步已完成！")
                # 重新加载表情配置
                await self.reload_emotions()
            else:
                yield event.plain_result("从云端同步失败，请查看日志哦。")
        except Exception as e:
            logger.error(f"从云端同步失败: {str(e)}")
            yield event.plain_result(f"从云端同步失败: {str(e)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("覆盖到云端")
    async def overwrite_to_remote(self, event: AstrMessageEvent):
        """让云端完全和本地一致（会删除云端多出的图）"""
        if not self.img_sync:
            yield event.plain_result(
                "图床服务尚未配置，请先在配置文件中完成图床配置哦。"
            )
            return

        try:
            yield event.plain_result(
                "⚠️ 正在执行覆盖到云端任务（将清理云端多余文件）..."
            )
            success = await self.img_sync.start_sync("overwrite_to_remote")
            if success:
                yield event.plain_result(
                    "覆盖到云端任务已完成！云端现在与本地完全一致。"
                )
            else:
                yield event.plain_result("任务失败，请查看日志。")
        except Exception as e:
            logger.error(f"覆盖到云端失败: {str(e)}")
            yield event.plain_result(f"覆盖到云端失败: {str(e)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("从云端覆盖")
    async def overwrite_from_remote(self, event: AstrMessageEvent):
        """让本地完全和云端一致（会删除本地多出的图）"""
        if not self.img_sync:
            yield event.plain_result(
                "图床服务尚未配置，请先在配置文件中完成图床配置哦。"
            )
            return

        try:
            yield event.plain_result(
                "⚠️ 正在执行从云端覆盖任务（将清理本地多余文件）..."
            )
            success = await self.img_sync.start_sync("overwrite_from_remote")
            if success:
                yield event.plain_result(
                    "从云端覆盖任务已完成！本地现在与云端完全一致。"
                )
            else:
                yield event.plain_result("任务失败，请查看日志。")
        except Exception as e:
            logger.error(f"从云端覆盖失败: {str(e)}")
            yield event.plain_result(f"从云端覆盖失败: {str(e)}")

    async def terminate(self):
        """清理资源"""
        # 恢复人格
        personas = self.context.provider_manager.personas
        self._sync_persona_base_prompts(personas)
        for index, persona in enumerate(personas):
            key = self._get_persona_key(persona, index)
            persona["prompt"] = self.persona_base_prompts[key]

        # 停止图床同步
        if self.img_sync:
            self.img_sync.stop_sync()

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
