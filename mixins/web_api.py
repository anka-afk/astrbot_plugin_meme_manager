import asyncio
import base64
import io
import json
import mimetypes
import time

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
    export_pack_archive,
    get_pack_detail,
    import_pack_archive,
    list_installed_packs,
    set_default_pack,
    uninstall_pack,
)
from ..config import MEMES_DIR, TEMP_DIR

PLUGIN_NAME = "meme_manager"
WEBUI_LOG_PREFIX = f"[{PLUGIN_NAME}][WebUI]"
MAX_PREVIEW_IMAGE_BYTES = 8 * 1024 * 1024
MAX_ORIGINAL_IMAGE_BYTES = 32 * 1024 * 1024
PREVIEW_IMAGE_MAX_DIMENSION = 512


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

    async def _api_get_emojis(self):
        emoji_data = await scan_emoji_folder()
        for category in emoji_data:
            if not isinstance(emoji_data[category], list):
                emoji_data[category] = []
        return jsonify(emoji_data)

    async def _api_get_emoji_by_category(self, category):
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
            return jsonify({"status": "error", "message": "非法路径"}), 403
        if not file_path.exists():
            return jsonify({"status": "error", "message": "文件不存在"}), 404
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
            result = export_pack_archive(pack_id, output_dir=output_dir)
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
