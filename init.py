import logging
import os
import json

from .config import (
    ACTIVE_PACK_MANIFEST_PATH,
    BASE_DATA_DIR,
    MEMES_DATA_PATH,
    MEMES_DIR,
    sync_active_pack_metadata,
)
from .utils import ensure_dir_exists, load_json, save_json

logger = logging.getLogger(__name__)


def _build_descriptions_from_manifest_and_dirs() -> dict[str, str]:
    """从当前包 manifest 与本地目录构建兼容描述配置。"""
    descriptions: dict[str, str] = {}
    local_dirs = set()

    if os.path.isdir(MEMES_DIR):
        for category in os.listdir(MEMES_DIR):
            category_path = os.path.join(MEMES_DIR, category)
            if os.path.isdir(category_path):
                local_dirs.add(category)

    try:
        if ACTIVE_PACK_MANIFEST_PATH.is_file():
            with ACTIVE_PACK_MANIFEST_PATH.open(encoding="utf-8-sig") as file_obj:
                manifest = json.load(file_obj)
            categories = (
                manifest.get("categories", {}) if isinstance(manifest, dict) else {}
            )
            if isinstance(categories, dict):
                for category, meta in categories.items():
                    key = str(category or "").strip()
                    if not key or key not in local_dirs:
                        continue
                    if isinstance(meta, dict):
                        descriptions[key] = str(meta.get("description") or "请添加描述")
                    else:
                        descriptions[key] = str(meta or "请添加描述")
    except Exception as exc:
        logger.warning("读取 manifest 分类描述失败: %s", exc)

    for category in local_dirs:
        descriptions.setdefault(category, "请添加描述")

    return descriptions


def init_plugin():
    """初始化运行时存储和兼容性元数据，不自动注入默认表情包。"""
    try:
        ensure_dir_exists(BASE_DATA_DIR)
        ensure_dir_exists(MEMES_DIR)

        if not os.path.exists(MEMES_DATA_PATH):
            descriptions = _build_descriptions_from_manifest_and_dirs()
            save_json(descriptions, MEMES_DATA_PATH)
            logger.info("已初始化兼容性描述文件: %s", MEMES_DATA_PATH)
        else:
            # 归一化已有配置：补齐本地目录、按需清理孤立项，避免启动时写空对象。
            try:
                original_descriptions = load_json(MEMES_DATA_PATH, {})
                descriptions = (
                    dict(original_descriptions)
                    if isinstance(original_descriptions, dict)
                    else {}
                )
                local_dirs = (
                    {
                        d
                        for d in os.listdir(MEMES_DIR)
                        if os.path.isdir(os.path.join(MEMES_DIR, d))
                    }
                    if os.path.isdir(MEMES_DIR)
                    else set()
                )

                rebuilt = False
                if not descriptions:
                    descriptions = _build_descriptions_from_manifest_and_dirs()
                    rebuilt = True

                for category in local_dirs:
                    descriptions.setdefault(category, "请添加描述")

                # 仅在明确拿到本地目录时才清理孤立项，避免瞬时目录不可见导致写空。
                if local_dirs:
                    descriptions = {
                        key: value
                        for key, value in descriptions.items()
                        if key in local_dirs
                    }

                if rebuilt or descriptions != original_descriptions:
                    save_json(descriptions, MEMES_DATA_PATH)
                    logger.info(
                        "已归一化兼容性描述配置，当前分类数: %d",
                        len(descriptions),
                    )
            except Exception as clean_err:
                logger.warning("清理孤立配置条目失败: %s", clean_err)

        sync_active_pack_metadata()

        return True
    except Exception as e:
        logger.error("插件初始化失败: %s", e)
        return False
