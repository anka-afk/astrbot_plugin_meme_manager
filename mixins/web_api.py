import asyncio
import base64
import binascii
import io
import json
import mimetypes
import time
from pathlib import Path

from PIL import Image as PILImage
from quart import jsonify, make_response, request, send_file
from astrbot.api import logger

from ..backend.models import (
    scan_emoji_folder,
    get_emoji_by_category,
    add_emoji_to_category,
    DuplicateEmojiError,
    delete_emoji_from_category,
    batch_delete_emojis,
    move_emoji_to_category,
    batch_move_emojis,
    batch_copy_emojis,
    clear_all_emojis,
    clear_category_emojis,
)
from ..backend.pack_storage import (
    export_runtime_backup,
    export_pack_archive,
    fetch_and_cache_community_index,
    find_cached_pack_entry,
    get_pack_detail,
    get_selection_rules,
    import_runtime_backup,
    install_pack_from_github_source,
    import_pack_archive,
    install_first_official_pack_from_index,
    list_installed_packs,
    load_cached_community_index,
    save_selection_rules,
    set_default_pack,
    uninstall_pack,
)
from ..config import COMMUNITY_INDEX_URL, MEMES_DIR, PACKS_DIR, TEMP_DIR
from ..config import PLUGIN_DATA_DIR, SEMANTIC_INDEXES_DIR
from ..backend.semantic_storage import load_metadata, metadata_items, import_metadata_file
from ..backend.semantic_index import EmbeddingAdapter, index_is_ready

PLUGIN_NAME = "meme_manager"
WEBUI_LOG_PREFIX = f"[{PLUGIN_NAME}][WebUI]"
MAX_PREVIEW_IMAGE_BYTES = 8 * 1024 * 1024
MAX_ORIGINAL_IMAGE_BYTES = 32 * 1024 * 1024
PREVIEW_IMAGE_MAX_DIMENSION = 512
IMG_HOST_STATUS_CACHE_TTL_SECONDS = 15


class WebAPIMixin:
    """包含所有 WebUI 仪表盘 API 的注册与处理逻辑"""

    def _register_web_apis(self):
        # 将所有路由委托给 _register_webui_api
        self._register_webui_api(
            "emoji", self._api_get_emojis, ["GET"], "获取所有分类的表情列表"
        )
        self._register_webui_api(
            "emoji/<category>",
            self._api_get_emoji_by_category,
            ["GET"],
            "获取某个分类下的表情",
        )
        self._register_webui_api(
            "emoji/add/<category>",
            self._api_add_emoji,
            ["POST"],
            "上传表情到指定分类（表单字段 file）",
        )
        self._register_webui_api(
            "emoji/delete", self._api_delete_emoji, ["POST"], "删除单个表情"
        )
        self._register_webui_api(
            "emoji/batch_delete",
            self._api_batch_delete_emojis,
            ["POST"],
            "批量删除表情",
        )
        self._register_webui_api(
            "emoji/move", self._api_move_emoji, ["POST"], "移动单个表情到其他分类"
        )
        self._register_webui_api(
            "emoji/batch_move", self._api_batch_move_emojis, ["POST"], "批量移动表情"
        )
        self._register_webui_api(
            "emoji/batch_copy", self._api_batch_copy_emojis, ["POST"], "批量复制表情"
        )
        self._register_webui_api(
            "emoji/clear_all",
            self._api_clear_all_emojis,
            ["POST"],
            "清空所有表情（保留分类）",
        )

        self._register_webui_api(
            "emotions", self._api_get_emotions, ["GET"], "获取分类描述"
        )
        self._register_webui_api(
            "category/delete", self._api_delete_category, ["POST"], "删除分类及其文件"
        )
        self._register_webui_api(
            "category/clear",
            self._api_clear_category,
            ["POST"],
            "清空分类内表情（保留分类）",
        )
        self._register_webui_api(
            "category/restore", self._api_restore_category, ["POST"], "恢复或创建分类"
        )
        self._register_webui_api(
            "category/rename", self._api_rename_category, ["POST"], "重命名分类"
        )
        self._register_webui_api(
            "category/update_description",
            self._api_update_description,
            ["POST"],
            "更新分类描述",
        )
        self._register_webui_api(
            "category/remove_from_config",
            self._api_remove_from_config,
            ["POST"],
            "仅从配置中移除分类",
        )

        self._register_webui_api(
            "sync/status", self._api_sync_status, ["GET"], "获取配置同步状态"
        )
        self._register_webui_api(
            "sync/config", self._api_sync_config, ["POST"], "同步配置与文件系统"
        )

        self._register_webui_api(
            "img_host/sync/status",
            self._api_img_host_sync_status,
            ["GET"],
            "图床同步状态",
        )
        self._register_webui_api(
            "img_host/sync/upload",
            self._api_img_host_sync_upload,
            ["POST"],
            "开始上传至图床",
        )
        self._register_webui_api(
            "img_host/sync/download",
            self._api_img_host_sync_download,
            ["POST"],
            "开始从图床下载",
        )
        self._register_webui_api(
            "img_host/sync/overwrite_to_remote",
            self._api_img_host_sync_overwrite_to_remote,
            ["POST"],
            "覆盖远程图床（以本地为准）",
        )
        self._register_webui_api(
            "img_host/sync/overwrite_from_remote",
            self._api_img_host_sync_overwrite_from_remote,
            ["POST"],
            "覆盖本地（以远程为准）",
        )
        self._register_webui_api(
            "img_host/sync/progress",
            self._api_img_host_sync_progress,
            ["GET"],
            "同步进度 SSE 流",
        )
        self._register_webui_api(
            "img_host/sync/task_status",
            self._api_img_host_sync_task_status,
            ["GET"],
            "当前同步任务状态",
        )

        self._register_webui_api(
            "meme_image", self._api_serve_meme_image, ["GET"], "直接返回表情图片文件"
        )
        self._register_webui_api(
            "meme_image_data",
            self._api_get_meme_image_data,
            ["GET"],
            "获取表情图片的 Data URL（预览）",
        )

        # Phase 3: pack-aware API
        self._register_webui_api(
            "packs",
            self._api_list_packs,
            ["GET"],
            "获取已安装表情包列表",
        )
        self._register_webui_api(
            "packs/<pack_id>",
            self._api_get_pack_detail,
            ["GET"],
            "获取单个表情包详情",
        )
        self._register_webui_api(
            "packs/default",
            self._api_set_default_pack,
            ["POST"],
            "设置默认表情包",
        )
        self._register_webui_api(
            "packs/export",
            self._api_export_pack,
            ["POST"],
            "导出表情包压缩文件",
        )
        self._register_webui_api(
            "packs/import",
            self._api_import_pack,
            ["POST"],
            "导入表情包压缩文件",
        )
        self._register_webui_api(
            "packs/uninstall",
            self._api_uninstall_pack,
            ["POST"],
            "卸载表情包",
        )
        self._register_webui_api(
            "community/index/fetch",
            self._api_fetch_community_index,
            ["POST"],
            "拉取并缓存社区索引",
        )
        self._register_webui_api(
            "community/index/cache",
            self._api_get_cached_community_index,
            ["GET"],
            "读取已缓存的社区索引",
        )
        self._register_webui_api(
            "community/install",
            self._api_install_community_pack,
            ["POST"],
            "按社区 source 安装表情包",
        )
        self._register_webui_api(
            "community/install_official_first",
            self._api_install_official_first_pack,
            ["POST"],
            "安装官方首个表情包",
        )
        self._register_webui_api(
            "settings/rules",
            self._api_settings_rules,
            ["GET", "POST"],
            "获取或保存表情包选择规则",
        )
        self._register_webui_api(
            "settings/targets",
            self._api_settings_targets,
            ["GET"],
            "获取规则 target 建议值",
        )
        self._register_webui_api(
            "settings/backup/export",
            self._api_export_runtime_backup,
            ["POST"],
            "导出运行时全量备份",
        )
        self._register_webui_api(
            "settings/backup/import",
            self._api_import_runtime_backup,
            ["POST"],
            "导入运行时全量备份",
        )
        self._register_webui_api(
            "bridge/auth_token",
            self._api_bridge_auth_token,
            ["GET"],
            "获取当前会话 Bearer Token（用于插件页安全跳转）",
        )
        self._register_webui_api(
            "semantic/status", self._api_semantic_status, ["GET"], "获取语义化任务状态"
        )
        self._register_webui_api(
            "semantic/items", self._api_semantic_items, ["GET"], "获取语义图片记录"
        )
        self._register_webui_api(
            "semantic/start", self._api_semantic_start, ["POST"], "开始语义化任务"
        )
        self._register_webui_api(
            "semantic/pause", self._api_semantic_pause, ["POST"], "暂停语义化任务"
        )
        self._register_webui_api(
            "semantic/resume", self._api_semantic_resume, ["POST"], "继续语义化任务"
        )
        self._register_webui_api(
            "semantic/retry", self._api_semantic_retry, ["POST"], "重试失败语义项"
        )
        self._register_webui_api(
            "semantic/rebuild-index", self._api_semantic_rebuild_index, ["POST"], "重建语义向量索引"
        )

    def _register_webui_api(self, route, handler, methods, desc):
        route_path = f"/{PLUGIN_NAME}/{route.strip('/')}"

        async def logged_handler(*args, **kwargs):
            started_at = time.monotonic()
            logger.info(f"{WEBUI_LOG_PREFIX} {request.method} {route_path} 开始")
            try:
                response = await handler(*args, **kwargs)
            except Exception:
                elapsed_ms = int((time.monotonic() - started_at) * 1000)
                logger.error(
                    f"{WEBUI_LOG_PREFIX} {request.method} {route_path} 失败 耗时={elapsed_ms}ms",
                    exc_info=True,
                )
                raise
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            status_code = self._get_webui_response_status(response)
            logger.info(
                f"{WEBUI_LOG_PREFIX} {request.method} {route_path} 完成 状态={status_code} 耗时={elapsed_ms}ms"
            )
            return response

        logged_handler.__name__ = f"webui_{handler.__name__}"
        self.context.register_web_api(route_path, logged_handler, methods, desc)

    @staticmethod
    def _get_webui_response_status(response) -> int | str:
        if isinstance(response, tuple) and len(response) > 1:
            return response[1]
        return getattr(response, "status_code", "unknown")

    async def _semantic_request_pack_id(self, data: dict | None = None) -> str:
        payload = data or {}
        pack_id = str(payload.get("pack_id") or request.args.get("pack_id") or request.args.get("managed_pack_id") or "").strip()
        if not pack_id:
            pack_id = str(getattr(self, "_resolve_runtime_pack_context", lambda: {})().get("pack_id") or "")
        if not pack_id:
            raise ValueError("pack_id 不能为空")
        pack_dir = (PACKS_DIR / pack_id).resolve()
        try:
            pack_dir.relative_to(PACKS_DIR.resolve())
        except ValueError as exc:
            raise ValueError("pack_id 无效") from exc
        if not pack_dir.is_dir():
            raise FileNotFoundError(f"表情包 {pack_id} 不存在")
        return pack_id

    @staticmethod
    def _resolve_webui_pack_view_context() -> dict | None:
        managed_pack_id = str(request.args.get("managed_pack_id") or "").strip()
        if not managed_pack_id:
            return None

        pack_dir = (PACKS_DIR / managed_pack_id).resolve()
        packs_root = PACKS_DIR.resolve()
        try:
            pack_dir.relative_to(packs_root)
        except ValueError:
            return None
        if not pack_dir.is_dir():
            return None

        return {
            "pack_id": managed_pack_id,
            "pack_dir": pack_dir,
            "memes_dir": pack_dir / "memes",
            "memes_data_path": pack_dir / "memes_data.json",
            "manifest_path": pack_dir / "manifest.json",
        }

    @staticmethod
    def _scan_pack_emojis(memes_dir: Path) -> dict:
        emojis = {}
        if not memes_dir.is_dir():
            return emojis
        for category_dir in memes_dir.iterdir():
            if not category_dir.is_dir():
                continue
            category = category_dir.name
            files = []
            for file_path in category_dir.iterdir():
                if file_path.is_file() and file_path.suffix.lower() in {
                    ".png",
                    ".jpg",
                    ".jpeg",
                    ".gif",
                    ".webp",
                }:
                    files.append(file_path.name)
            emojis[category] = files
        return emojis

    @staticmethod
    def _load_pack_descriptions(view_context: dict) -> dict:
        descriptions = {}
        memes_data_path = view_context["memes_data_path"]
        if memes_data_path.is_file():
            try:
                with memes_data_path.open(encoding="utf-8-sig") as file_obj:
                    data = json.load(file_obj)
                if isinstance(data, dict):
                    descriptions.update(
                        {
                            str(key): str(value)
                            for key, value in data.items()
                            if str(key).strip()
                        }
                    )
            except Exception:
                pass

        manifest_path = view_context["manifest_path"]
        if manifest_path.is_file():
            try:
                with manifest_path.open(encoding="utf-8-sig") as file_obj:
                    manifest = json.load(file_obj)
                categories = (
                    manifest.get("categories", {}) if isinstance(manifest, dict) else {}
                )
                if isinstance(categories, dict):
                    for category_name, category_meta in categories.items():
                        key = str(category_name or "").strip()
                        if not key or key in descriptions:
                            continue
                        if isinstance(category_meta, dict):
                            descriptions[key] = str(
                                category_meta.get("description") or "请添加描述"
                            )
                        else:
                            descriptions[key] = str(category_meta or "请添加描述")
            except Exception:
                pass

        return descriptions

    async def _api_get_emojis(self):
        view_context = self._resolve_webui_pack_view_context()
        if view_context:
            emoji_data = self._scan_pack_emojis(view_context["memes_dir"])
        else:
            emoji_data = await scan_emoji_folder()
        for category in emoji_data:
            if not isinstance(emoji_data[category], list):
                emoji_data[category] = []
        return jsonify(emoji_data)

    async def _api_bridge_auth_token(self):
        auth_header = request.headers.get("Authorization", "").strip()
        if auth_header.startswith("Bearer "):
            token = auth_header.removeprefix("Bearer ").strip()
            if token:
                return jsonify({"token": token}), 200
        return jsonify({"message": "当前请求缺少 Bearer Token"}), 401

    async def _api_get_emoji_by_category(self, category):
        view_context = self._resolve_webui_pack_view_context()
        if view_context:
            category_path = view_context["memes_dir"] / category
            if not category_path.is_dir():
                emojis = []
            else:
                emojis = [
                    file_path.name
                    for file_path in category_path.iterdir()
                    if file_path.is_file()
                    and file_path.suffix.lower()
                    in {
                        ".png",
                        ".jpg",
                        ".jpeg",
                        ".gif",
                        ".webp",
                    }
                ]
        else:
            emojis = get_emoji_by_category(category)
        if emojis is None:
            return jsonify({"message": "分类未找到"}), 404
        return jsonify(emojis if isinstance(emojis, list) else []), 200

    async def _api_add_emoji(self, category):
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
                logger.info(f"表情添加成功: {result['path']}")
                return (
                    jsonify(
                        {
                            "message": "表情添加成功",
                            "path": result["path"],
                            "category": category,
                            "filename": result["filename"],
                        }
                    ),
                    201,
                )
            except DuplicateEmojiError as e:
                logger.info(f"跳过重复表情: {e}")
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
        data = await request.get_json()
        category = data.get("category")
        image_file = data.get("image_file")
        if not category or not image_file:
            return jsonify({"message": "分类和文件名不能为空"}), 400
        if delete_emoji_from_category(category, image_file):
            return (
                jsonify(
                    {
                        "message": "表情删除成功",
                        "category": category,
                        "filename": image_file,
                    }
                ),
                200,
            )
        return jsonify({"message": "表情未找到"}), 404

    async def _api_batch_delete_emojis(self):
        data = await request.get_json()
        category = data.get("category")
        image_files = data.get("image_files")
        if not category or not isinstance(image_files, list) or not image_files:
            return jsonify({"message": "分类和文件名列表不能为空"}), 400
        result = batch_delete_emojis(category, image_files)
        if not result["category_exists"]:
            return jsonify({"message": "分类未找到"}), 404
        return (
            jsonify(
                {
                    "message": "批量删除完成",
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
        data = await request.get_json()
        source_category = data.get("source_category")
        target_category = data.get("target_category")
        image_file = data.get("image_file")
        if not source_category or not target_category or not image_file:
            return jsonify({"message": "源分类、目标分类和文件名不能为空"}), 400
        if source_category == target_category:
            return jsonify({"message": "源分类和目标分类不能相同"}), 400
        result = move_emoji_to_category(source_category, image_file, target_category)
        if not result["source_category_exists"]:
            return jsonify({"message": "源分类未找到"}), 404
        if result["conflict"]:
            return jsonify({"message": "目标文件已存在"}), 409
        if result["missing"]:
            return jsonify({"message": "表情未找到"}), 404
        return (
            jsonify(
                {
                    "message": "表情移动成功",
                    "source_category": result["source_category"],
                    "target_category": result["target_category"],
                    "filename": result["filename"],
                }
            ),
            200,
        )

    async def _api_batch_move_emojis(self):
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
            return jsonify({"message": "源分类、目标分类和文件名列表不能为空"}), 400
        if source_category == target_category:
            return jsonify({"message": "源分类和目标分类不能相同"}), 400
        result = batch_move_emojis(source_category, image_files, target_category)
        if not result["source_category_exists"]:
            return jsonify({"message": "源分类未找到"}), 404
        return (
            jsonify(
                {
                    "message": "批量移动完成",
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
            return jsonify({"message": "源分类、目标分类和文件名列表不能为空"}), 400
        result = batch_copy_emojis(source_category, image_files, target_category)
        if not result["source_category_exists"]:
            return jsonify({"message": "源分类未找到"}), 404
        return (
            jsonify(
                {
                    "message": "批量复制完成",
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
        result = clear_all_emojis()
        deleted_count = sum(result["deleted_by_category"].values())
        return (
            jsonify(
                {
                    "message": "所有表情已清空",
                    "deleted_by_category": result["deleted_by_category"],
                    "deleted_count": deleted_count,
                    "affected_categories": len(result["deleted_by_category"]),
                }
            ),
            200,
        )

    async def _api_get_emotions(self):
        try:
            view_context = self._resolve_webui_pack_view_context()
            if view_context:
                descriptions = self._load_pack_descriptions(view_context)
            else:
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
                return jsonify({"message": "分类不能为空"}), 400
            if self.category_manager.delete_category(category):
                return jsonify({"message": "分类删除成功"}), 200
            return jsonify({"message": "分类删除失败"}), 500
        except Exception as e:
            return jsonify({"message": f"分类删除失败: {str(e)}"}), 500

    async def _api_clear_category(self):
        data = await request.get_json()
        category = data.get("category")
        if not category:
            return jsonify({"message": "分类不能为空"}), 400
        result = clear_category_emojis(category)
        if not result["category_exists"]:
            return jsonify({"message": "分类未找到"}), 404
        return (
            jsonify(
                {
                    "message": "分类表情已清空",
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
                return jsonify({"message": "分类不能为空"}), 400
            if self.category_manager.create_category(category, description):
                return (
                    jsonify({"message": "分类创建成功", "description": description}),
                    200,
                )
            return jsonify({"message": "分类创建失败"}), 500
        except Exception as e:
            return jsonify({"message": f"分类创建失败: {str(e)}"}), 500

    async def _api_rename_category(self):
        try:
            data = await request.get_json()
            old_name = data.get("old_name")
            new_name = data.get("new_name")
            if not old_name or not new_name:
                return jsonify({"message": "旧分类名和新分类名不能为空"}), 400
            if self.category_manager.rename_category(old_name, new_name):
                return jsonify({"message": "分类重命名成功"}), 200
            return jsonify({"message": "分类重命名失败"}), 500
        except Exception as e:
            return jsonify({"message": f"分类重命名失败: {str(e)}"}), 500

    async def _api_update_description(self):
        try:
            data = await request.get_json()
            category = data.get("tag")
            description = data.get("description")
            if not category or not description:
                return jsonify({"message": "分类和描述不能为空"}), 400
            if self.category_manager.update_description(category, description):
                return jsonify({"category": category, "description": description}), 200
            return jsonify({"message": "更新分类描述失败"}), 500
        except Exception as e:
            return jsonify({"message": f"更新分类描述失败: {str(e)}"}), 500

    async def _api_remove_from_config(self):
        try:
            data = await request.get_json()
            category = data.get("category")
            if not category:
                return jsonify({"message": "分类不能为空"}), 400
            if self.category_manager.remove_from_config(category):
                return jsonify({"message": "已从配置中移除分类"}), 200
            return jsonify({"message": "从配置中移除分类失败"}), 500
        except Exception as e:
            return jsonify({"message": f"从配置中移除分类失败: {str(e)}"}), 500

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

    @staticmethod
    def _resolve_requested_sync_pack_id(payload: dict | None = None) -> str:
        managed_pack_id = str(request.args.get("managed_pack_id") or "").strip()
        if managed_pack_id:
            return managed_pack_id
        if isinstance(payload, dict):
            for key in ("managed_pack_id", "pack_id"):
                value = str(payload.get(key) or "").strip()
                if value:
                    return value
        return ""

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

    def _ensure_img_host_status_cache(self) -> dict[str, dict]:
        cache = getattr(self, "_img_host_sync_status_cache", None)
        if isinstance(cache, dict):
            return cache
        cache = {}
        self._img_host_sync_status_cache = cache
        return cache

    def _invalidate_img_host_status_cache(self, pack_id: str | None = None) -> None:
        cache = self._ensure_img_host_status_cache()
        if not pack_id:
            cache.clear()
            return
        target_pack_id = str(pack_id).strip()
        keys_to_remove = [key for key in cache if key.startswith(f"{target_pack_id}::")]
        for key in keys_to_remove:
            cache.pop(key, None)

    def _get_img_host_status_cache_ttl(self) -> int:
        raw_value = self._read_config_value(
            ("sync", "status_cache_ttl_seconds"),
            default=IMG_HOST_STATUS_CACHE_TTL_SECONDS,
            legacy_keys=("img_host_status_cache_ttl_seconds",),
        )
        try:
            ttl = int(raw_value)
        except (TypeError, ValueError):
            return IMG_HOST_STATUS_CACHE_TTL_SECONDS
        return max(0, min(ttl, 300))

    @staticmethod
    def _make_img_host_status_cache_key(pack_id: str, local_dir: Path | str) -> str:
        normalized_pack_id = str(pack_id or "").strip() or "__default__"
        normalized_local_dir = str(local_dir or "").replace("\\", "/").rstrip("/")
        return f"{normalized_pack_id}::{normalized_local_dir}"

    def _start_img_host_sync_task(self, task: str, pack_id: str | None = None) -> dict:
        sync_client = self._ensure_img_sync_for_pack(pack_id)
        if not sync_client:
            raise RuntimeError("图床服务未配置")

        status = self._get_img_host_sync_task_status()
        if not status.get("available", False):
            raise RuntimeError(status.get("message") or "图床服务未配置")
        if status.get("running"):
            raise RuntimeError("已有同步任务正在运行，请等待当前任务完成")

        self._invalidate_img_host_status_cache(pack_id)
        self._last_img_host_sync_task_status = None
        sync_client.sync_process = sync_client._start_sync_process(task)
        return self._get_img_host_sync_task_status()

    async def _api_img_host_sync_status(self):
        try:
            pack_id = self._resolve_requested_sync_pack_id()
            sync_client = self._ensure_img_sync_for_pack(pack_id)
            if not sync_client:
                return jsonify({"error": "图床服务未配置"}), 400

            task_status = self._get_img_host_sync_task_status()
            cache_ttl = self._get_img_host_status_cache_ttl()
            cache_key = self._make_img_host_status_cache_key(
                pack_id, getattr(sync_client, "local_dir", "")
            )
            cache_store = self._ensure_img_host_status_cache()
            now = time.monotonic()
            if not task_status.get("running") and cache_ttl > 0:
                cached_entry = cache_store.get(cache_key)
                if (
                    cached_entry
                    and (now - cached_entry.get("created_at", 0.0)) < cache_ttl
                ):
                    cached_payload = dict(cached_entry.get("payload") or {})
                    cached_payload["status_cache_hit"] = True
                    cached_payload["status_cache_ttl"] = cache_ttl
                    return jsonify(cached_payload)

            status = sync_client.check_status()
            status["upload_count"] = len(status.get("to_upload", []))
            status["download_count"] = len(status.get("to_download", []))
            status["remote_extra_count"] = len(status.get("to_delete_remote", []))
            status["local_extra_count"] = len(status.get("to_delete_local", []))
            status["provider_label"] = self._get_provider_label()
            status["status_cache_hit"] = False
            status["status_cache_ttl"] = cache_ttl
            if pack_id:
                status["managed_pack_id"] = pack_id

            if not task_status.get("running") and cache_ttl > 0:
                cache_store[cache_key] = {
                    "created_at": now,
                    "payload": dict(status),
                }
            return jsonify(status)
        except Exception as e:
            error_text = str(e)
            lower_error_text = error_text.lower()
            is_rate_limited = any(
                keyword in lower_error_text
                for keyword in (
                    "exceed times limit",
                    "rate limit",
                    "too many requests",
                    "调用频次",
                    "调用次数",
                    "请求频率",
                )
            )
            if is_rate_limited:
                return (
                    jsonify(
                        {
                            "error": "图床接口触发频率限制，请稍后再试",
                            "details": error_text,
                        }
                    ),
                    429,
                )
            return jsonify({"error": error_text}), 500

    async def _api_img_host_sync_upload(self):
        try:
            payload = await request.get_json(silent=True)
            pack_id = self._resolve_requested_sync_pack_id(payload)
            if not self._ensure_img_sync_for_pack(pack_id):
                return jsonify({"message": "图床服务未配置"}), 400
            task_status = self._start_img_host_sync_task("upload", pack_id=pack_id)
            return jsonify({"success": True, "task": task_status})
        except Exception as e:
            status_code = 409 if "已有同步任务" in str(e) else 500
            return jsonify({"message": str(e)}), status_code

    async def _api_img_host_sync_overwrite_to_remote(self):
        try:
            payload = await request.get_json(silent=True)
            pack_id = self._resolve_requested_sync_pack_id(payload)
            if not self._ensure_img_sync_for_pack(pack_id):
                return jsonify({"message": "图床服务未配置"}), 400
            task_status = self._start_img_host_sync_task(
                "overwrite_to_remote", pack_id=pack_id
            )
            return jsonify({"success": True, "task": task_status})
        except Exception as e:
            status_code = 409 if "已有同步任务" in str(e) else 500
            return jsonify({"message": str(e)}), status_code

    async def _api_img_host_sync_overwrite_from_remote(self):
        try:
            payload = await request.get_json(silent=True)
            pack_id = self._resolve_requested_sync_pack_id(payload)
            if not self._ensure_img_sync_for_pack(pack_id):
                return jsonify({"message": "图床服务未配置"}), 400
            task_status = self._start_img_host_sync_task(
                "overwrite_from_remote", pack_id=pack_id
            )
            return jsonify({"success": True, "task": task_status})
        except Exception as e:
            status_code = 409 if "已有同步任务" in str(e) else 500
            return jsonify({"message": str(e)}), status_code

    async def _api_img_host_sync_download(self):
        try:
            payload = await request.get_json(silent=True)
            pack_id = self._resolve_requested_sync_pack_id(payload)
            if not self._ensure_img_sync_for_pack(pack_id):
                return jsonify({"message": "图床服务未配置"}), 400
            task_status = self._start_img_host_sync_task("download", pack_id=pack_id)
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
        view_context = self._resolve_webui_pack_view_context()
        memes_root = (
            view_context["memes_dir"].resolve() if view_context else MEMES_DIR.resolve()
        )
        file_path = (memes_root / category / filename).resolve()
        if not str(file_path).startswith(str(memes_root)):
            return jsonify({"status": "error", "message": "非法路径"}), 403
        if not file_path.exists():
            return jsonify({"status": "error", "message": "文件不存在"}), 404
        return await send_file(str(file_path))

    async def _api_get_meme_image_data(self):
        category = request.args.get("category", "")
        filename = request.args.get("filename", "")
        size = request.args.get("size", "preview")
        view_context = self._resolve_webui_pack_view_context()
        memes_root = (
            view_context["memes_dir"].resolve() if view_context else MEMES_DIR.resolve()
        )
        file_path = (memes_root / category / filename).resolve()

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

    async def _api_list_packs(self):
        try:
            return jsonify({"packs": list_installed_packs()})
        except Exception as e:
            logger.error(f"获取已安装表情包列表失败: {e}", exc_info=True)
            return jsonify({"message": f"获取已安装表情包列表失败: {str(e)}"}), 500

    async def _api_get_pack_detail(self, pack_id: str):
        try:
            return jsonify(get_pack_detail(pack_id))
        except FileNotFoundError as e:
            return jsonify({"message": str(e)}), 404
        except ValueError as e:
            return jsonify({"message": str(e)}), 400
        except Exception as e:
            logger.error(f"获取表情包详情失败: {e}", exc_info=True)
            return jsonify({"message": f"获取表情包详情失败: {str(e)}"}), 500

    async def _api_semantic_status(self):
        try:
            pack_id = await self._semantic_request_pack_id()
            result = self.semantic_task_manager.status(pack_id)
            metadata = load_metadata(PACKS_DIR / pack_id)
            provider = EmbeddingAdapter(
                self.semantic_task_manager._resolve_embedding_provider(),
                str(getattr(self, "semantic_embedding_provider_id", "") or ""),
            )
            result["index_ready"] = index_is_ready(SEMANTIC_INDEXES_DIR, pack_id, metadata, provider.provider_id)
            result["semantic_enabled"] = bool(getattr(self, "semantic_enabled", False))
            return jsonify(result), 200
        except FileNotFoundError as exc:
            return jsonify({"message": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"message": str(exc)}), 400
        except Exception as exc:
            logger.error("读取语义状态失败: %s", exc, exc_info=True)
            return jsonify({"message": "读取语义状态失败"}), 500

    async def _api_semantic_items(self):
        try:
            pack_id = await self._semantic_request_pack_id()
            return jsonify({"pack_id": pack_id, "items": metadata_items(PACKS_DIR / pack_id, request.args.get("status"))}), 200
        except (FileNotFoundError, ValueError) as exc:
            return jsonify({"message": str(exc)}), 400
        except Exception as exc:
            logger.error("读取语义记录失败: %s", exc, exc_info=True)
            return jsonify({"message": "读取语义记录失败"}), 500

    async def _api_semantic_start(self):
        try:
            data = await request.get_json() or {}
            pack_id = await self._semantic_request_pack_id(data)
            external_data = data.get("external_metadata") if isinstance(data.get("external_metadata"), dict) else None
            external_path = data.get("external_metadata_path")
            if external_path:
                source = Path(str(external_path)).expanduser().resolve()
                allowed_roots = [PLUGIN_DATA_DIR.resolve(), TEMP_DIR.resolve()]
                if not any(source == root or root in source.parents for root in allowed_roots):
                    raise ValueError("外部语义文件必须位于插件数据目录或临时目录")
                external_data = import_metadata_file(source)
            result = await self.semantic_task_manager.start(
                pack_id,
                mode=str(data.get("mode") or "full"),
                force=bool(data.get("force", False)),
                external_data=external_data,
            )
            return jsonify(result), 202
        except FileNotFoundError as exc:
            return jsonify({"message": str(exc)}), 404
        except (ValueError, RuntimeError) as exc:
            return jsonify({"message": str(exc)}), 400
        except Exception as exc:
            logger.error("启动语义任务失败: %s", exc, exc_info=True)
            return jsonify({"message": "启动语义任务失败"}), 500

    async def _api_semantic_pause(self):
        return await self._api_semantic_task_action("pause")

    async def _api_semantic_resume(self):
        return await self._api_semantic_task_action("resume")

    async def _api_semantic_retry(self):
        try:
            data = await request.get_json() or {}
            pack_id = await self._semantic_request_pack_id(data)
            return jsonify(await self.semantic_task_manager.retry(pack_id)), 202
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            return jsonify({"message": str(exc)}), 400
        except Exception as exc:
            logger.error("重试语义任务失败: %s", exc, exc_info=True)
            return jsonify({"message": "重试语义任务失败"}), 500

    async def _api_semantic_task_action(self, action: str):
        try:
            data = await request.get_json() or {}
            pack_id = await self._semantic_request_pack_id(data)
            result = await getattr(self.semantic_task_manager, action)(pack_id)
            return jsonify(result), 200
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            return jsonify({"message": str(exc)}), 400
        except Exception as exc:
            logger.error("语义任务操作失败: %s", exc, exc_info=True)
            return jsonify({"message": "语义任务操作失败"}), 500

    async def _api_semantic_rebuild_index(self):
        try:
            data = await request.get_json() or {}
            pack_id = await self._semantic_request_pack_id(data)
            result = await self.semantic_task_manager.rebuild_index(pack_id, force=bool(data.get("force", False)))
            return jsonify({"message": "向量索引已建立", **result}), 200
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            return jsonify({"message": str(exc)}), 400
        except Exception as exc:
            logger.error("重建语义索引失败: %s", exc, exc_info=True)
            return jsonify({"message": "重建语义索引失败"}), 500

    async def _api_set_default_pack(self):
        try:
            data = await request.get_json()
            pack_id = str((data or {}).get("pack_id") or "").strip()
            if not pack_id:
                return jsonify({"message": "pack_id 不能为空"}), 400
            result = set_default_pack(pack_id)
            self._reload_personas()
            return jsonify({"message": "默认表情包设置成功", **result}), 200
        except FileNotFoundError as e:
            return jsonify({"message": str(e)}), 404
        except ValueError as e:
            return jsonify({"message": str(e)}), 400
        except Exception as e:
            logger.error(f"设置默认表情包失败: {e}", exc_info=True)
            return jsonify({"message": f"设置默认表情包失败: {str(e)}"}), 500

    async def _api_export_pack(self):
        try:
            data = await request.get_json()
            payload = data or {}
            pack_id = str(payload.get("pack_id") or "").strip()
            output_dir = payload.get("output_dir")
            include_value = payload.get("include_semantic", payload.get("semantic", True))
            include_semantic = str(include_value).lower() not in {"0", "false", "no", "off"}
            result = export_pack_archive(pack_id, output_dir=output_dir, include_semantic=include_semantic)
            return jsonify({"message": "导出成功", **result}), 200
        except FileNotFoundError as e:
            return jsonify({"message": str(e)}), 404
        except ValueError as e:
            return jsonify({"message": str(e)}), 400
        except Exception as e:
            logger.error(f"导出表情包失败: {e}", exc_info=True)
            return jsonify({"message": f"导出表情包失败: {str(e)}"}), 500

    async def _api_import_pack(self):
        temp_zip_path = None
        try:
            form = await request.form
            overwrite = str(form.get("overwrite", "false")).lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            set_as_default = str(form.get("set_as_default", "false")).lower() in {
                "1",
                "true",
                "yes",
                "on",
            }

            files = await request.files
            if not files or "file" not in files:
                return jsonify({"message": "缺少上传文件字段 file"}), 400

            archive_file = files["file"]
            if not archive_file or not archive_file.filename:
                return jsonify({"message": "无效的压缩包文件"}), 400

            filename = str(archive_file.filename)
            if not filename.lower().endswith(".zip"):
                return jsonify({"message": "仅支持 zip 压缩包"}), 400

            temp_dir = TEMP_DIR
            temp_dir.mkdir(parents=True, exist_ok=True)
            safe_name = f"import_{int(time.time() * 1000)}.zip"
            temp_zip_path = (temp_dir / safe_name).resolve()
            archive_file.save(str(temp_zip_path))

            result = import_pack_archive(
                temp_zip_path,
                overwrite=overwrite,
                set_as_default=set_as_default,
            )
            self._reload_personas()
            return jsonify({"message": "导入成功", **result}), 200
        except FileExistsError as e:
            return jsonify({"message": str(e)}), 409
        except (FileNotFoundError, ValueError) as e:
            return jsonify({"message": str(e)}), 400
        except Exception as e:
            logger.error(f"导入表情包失败: {e}", exc_info=True)
            return jsonify({"message": f"导入表情包失败: {str(e)}"}), 500
        finally:
            if temp_zip_path and temp_zip_path.exists():
                try:
                    temp_zip_path.unlink()
                except Exception:
                    pass

    async def _api_uninstall_pack(self):
        try:
            data = await request.get_json()
            pack_id = str((data or {}).get("pack_id") or "").strip()
            if not pack_id:
                return jsonify({"message": "pack_id 不能为空"}), 400
            result = uninstall_pack(pack_id)
            self._reload_personas()
            return jsonify({"message": "卸载成功", **result}), 200
        except FileNotFoundError as e:
            return jsonify({"message": str(e)}), 404
        except ValueError as e:
            return jsonify({"message": str(e)}), 400
        except Exception as e:
            logger.error(f"卸载表情包失败: {e}", exc_info=True)
            return jsonify({"message": f"卸载表情包失败: {str(e)}"}), 500

    async def _api_fetch_community_index(self):
        try:
            index_url = COMMUNITY_INDEX_URL
            cache_data = fetch_and_cache_community_index(index_url)
            packs = cache_data.get("index", {}).get("packs", [])
            return (
                jsonify(
                    {
                        "message": "社区索引拉取成功",
                        "fetched_at": cache_data.get("fetched_at"),
                        "source_url": cache_data.get("source_url"),
                        "pack_count": len(packs) if isinstance(packs, list) else 0,
                        "index": cache_data.get("index", {}),
                    }
                ),
                200,
            )
        except ValueError as e:
            return jsonify({"message": str(e)}), 400
        except Exception as e:
            logger.error(f"拉取社区索引失败: {e}", exc_info=True)
            return jsonify({"message": f"拉取社区索引失败: {str(e)}"}), 500

    async def _api_get_cached_community_index(self):
        try:
            cache_data = load_cached_community_index()
            packs = cache_data.get("index", {}).get("packs", [])
            return (
                jsonify(
                    {
                        "fetched_at": cache_data.get("fetched_at"),
                        "source_url": cache_data.get("source_url"),
                        "pack_count": len(packs) if isinstance(packs, list) else 0,
                        "index": cache_data.get("index", {}),
                    }
                ),
                200,
            )
        except FileNotFoundError as e:
            return jsonify({"message": str(e)}), 404
        except ValueError as e:
            return jsonify({"message": str(e)}), 400
        except Exception as e:
            logger.error(f"读取社区索引缓存失败: {e}", exc_info=True)
            return jsonify({"message": f"读取社区索引缓存失败: {str(e)}"}), 500

    async def _api_install_community_pack(self):
        data = None
        try:
            data = await request.get_json()
            payload = data or {}
            overwrite = bool(payload.get("overwrite", False))
            set_as_default = bool(payload.get("set_as_default", False))
            pack_id = str(payload.get("pack_id") or "").strip()

            source = payload.get("source")
            if not isinstance(source, dict):
                if not pack_id:
                    return (
                        jsonify(
                            {
                                "message": "请提供 source 或 pack_id（用于从缓存索引安装）"
                            }
                        ),
                        400,
                    )
                source = find_cached_pack_entry(pack_id).get("source")
                if not isinstance(source, dict):
                    return jsonify({"message": "缓存条目缺少 source 信息"}), 400

            result = install_pack_from_github_source(
                source=source,
                overwrite=overwrite,
                set_as_default=set_as_default,
            )
            self._reload_personas()
            return jsonify({"message": "社区表情包安装成功", **result}), 200
        except FileExistsError as e:
            return jsonify({"message": str(e)}), 409
        except (FileNotFoundError, ValueError) as e:
            logger.warning(
                "社区表情包安装参数或资源错误: %s | pack_id=%s | payload_source=%s",
                e,
                str((data or {}).get("pack_id") or "").strip(),
                bool(isinstance((data or {}).get("source"), dict)),
            )
            return jsonify({"message": str(e)}), 400
        except Exception as e:
            logger.error(f"安装社区表情包失败: {e}", exc_info=True)
            return jsonify({"message": f"安装社区表情包失败: {str(e)}"}), 500

    async def _api_install_official_first_pack(self):
        data = None
        try:
            data = await request.get_json()
            payload = data or {}
            overwrite = bool(payload.get("overwrite", False))
            set_as_default = bool(payload.get("set_as_default", True))

            result = install_first_official_pack_from_index(
                index_url=COMMUNITY_INDEX_URL,
                overwrite=overwrite,
                set_as_default=set_as_default,
            )
            self._reload_personas()
            return jsonify({"message": "官方表情包安装成功", **result}), 200
        except FileExistsError as e:
            return jsonify({"message": str(e)}), 409
        except (FileNotFoundError, ValueError) as e:
            logger.warning(
                "安装官方首个表情包失败: %s | payload=%s",
                e,
                data,
            )
            return jsonify({"message": str(e)}), 400
        except Exception as e:
            logger.error(f"安装官方首个表情包失败: {e}", exc_info=True)
            return jsonify({"message": f"安装官方首个表情包失败: {str(e)}"}), 500

    async def _api_settings_rules(self):
        if request.method == "GET":
            try:
                return jsonify(get_selection_rules()), 200
            except Exception as e:
                logger.error(f"获取规则失败: {e}", exc_info=True)
                return jsonify({"message": f"获取规则失败: {str(e)}"}), 500

        try:
            data = await request.get_json()
            rules = (data or {}).get("rules", [])
            saved = save_selection_rules(rules)
            self._reload_personas()
            return jsonify({"message": "规则保存成功", **saved}), 200
        except ValueError as e:
            return jsonify({"message": str(e)}), 400
        except Exception as e:
            logger.error(f"保存规则失败: {e}", exc_info=True)
            return jsonify({"message": f"保存规则失败: {str(e)}"}), 500

    async def _api_export_runtime_backup(self):
        try:
            data = await request.get_json()
            output_dir = (data or {}).get("output_dir")
            result = export_runtime_backup(output_dir=output_dir)
            return jsonify({"message": "全量备份导出成功", **result}), 200
        except Exception as e:
            logger.error(f"导出全量备份失败: {e}", exc_info=True)
            return jsonify({"message": f"导出全量备份失败: {str(e)}"}), 500

    async def _api_settings_targets(self):
        try:
            rules_payload = get_selection_rules()
            rules = (
                rules_payload.get("rules", [])
                if isinstance(rules_payload, dict)
                else []
            )

            session_targets = []
            seen_session_targets = set()
            if isinstance(rules, list):
                for rule in rules:
                    if not isinstance(rule, dict):
                        continue
                    if str(rule.get("scope") or "").strip() != "session":
                        continue
                    target = str(rule.get("target") or "").strip()
                    if not target or target in seen_session_targets:
                        continue
                    seen_session_targets.add(target)
                    session_targets.append(target)

            persona_targets = []
            personas = getattr(self.context.provider_manager, "personas", [])
            for index, persona in enumerate(
                personas if isinstance(personas, list) else []
            ):
                if not isinstance(persona, dict):
                    continue
                if hasattr(self, "_get_persona_key"):
                    persona_id = str(self._get_persona_key(persona, index)).strip()
                else:
                    persona_id = str(
                        persona.get("id") or persona.get("name") or index
                    ).strip()
                if not persona_id:
                    continue
                persona_name = str(persona.get("name") or persona_id)
                persona_targets.append({"id": persona_id, "label": persona_name})

            return (
                jsonify(
                    {
                        "persona_targets": persona_targets,
                        "session_targets": session_targets,
                    }
                ),
                200,
            )
        except Exception as e:
            logger.error(f"获取规则 target 建议值失败: {e}", exc_info=True)
            return jsonify({"message": f"获取规则 target 建议值失败: {str(e)}"}), 500

    async def _api_import_runtime_backup(self):
        temp_zip_path = None
        try:
            overwrite_param = request.args.get("overwrite")
            form = await request.form
            json_payload = await request.get_json(silent=True)

            overwrite_raw = overwrite_param
            if overwrite_raw is None:
                if isinstance(form, dict) and form.get("overwrite") is not None:
                    overwrite_raw = form.get("overwrite")
                elif isinstance(json_payload, dict):
                    overwrite_raw = json_payload.get("overwrite", "false")
                else:
                    overwrite_raw = "false"

            overwrite = str(overwrite_raw).lower() in {
                "1",
                "true",
                "yes",
                "on",
            }

            TEMP_DIR.mkdir(parents=True, exist_ok=True)
            safe_name = f"runtime_restore_{int(time.time() * 1000)}.zip"
            temp_zip_path = (TEMP_DIR / safe_name).resolve()

            files = await request.files
            if files and "file" in files:
                archive_file = files["file"]
                if not archive_file or not archive_file.filename:
                    return jsonify({"message": "无效的备份文件"}), 400
                if not str(archive_file.filename).lower().endswith(".zip"):
                    return jsonify({"message": "仅支持 zip 备份文件"}), 400
                archive_file.save(str(temp_zip_path))
            elif isinstance(json_payload, dict):
                file_name = str(json_payload.get("file_name") or "").strip()
                file_b64 = str(json_payload.get("file_b64") or "").strip()
                if not file_name or not file_name.lower().endswith(".zip"):
                    return jsonify({"message": "仅支持 zip 备份文件"}), 400
                if not file_b64:
                    return jsonify({"message": "缺少 file_b64"}), 400
                try:
                    raw_bytes = base64.b64decode(file_b64, validate=True)
                except (ValueError, binascii.Error):
                    return jsonify({"message": "file_b64 非法"}), 400
                temp_zip_path.write_bytes(raw_bytes)
            else:
                return (
                    jsonify({"message": "缺少上传文件字段 file 或 JSON file_b64"}),
                    400,
                )

            result = import_runtime_backup(temp_zip_path, overwrite=overwrite)
            self._reload_personas()
            return jsonify({"message": "全量备份导入成功", **result}), 200
        except (FileNotFoundError, ValueError) as e:
            return jsonify({"message": str(e)}), 400
        except Exception as e:
            logger.error(f"导入全量备份失败: {e}", exc_info=True)
            return jsonify({"message": f"导入全量备份失败: {str(e)}"}), 500
        finally:
            if temp_zip_path and temp_zip_path.exists():
                try:
                    temp_zip_path.unlink()
                except Exception:
                    pass
