import logging
import os

from .config import (
    BASE_DATA_DIR,
    MEMES_DATA_PATH,
    sync_active_pack_metadata,
)
from .utils import ensure_dir_exists, load_json, save_json

logger = logging.getLogger(__name__)


def init_plugin():
    """初始化运行时存储和兼容性元数据，不自动注入默认表情包。"""
    try:
        from .config import MEMES_DIR

        ensure_dir_exists(BASE_DATA_DIR)

        if not os.path.exists(MEMES_DATA_PATH):
            save_json({}, MEMES_DATA_PATH)
            logger.info("已创建兼容性描述文件: %s", MEMES_DATA_PATH)
        else:
            # 清理配置中无对应文件夹的孤立条目（如旧版默认值残留）
            try:
                descriptions = load_json(MEMES_DATA_PATH, {})
                local_dirs = {
                    d
                    for d in os.listdir(MEMES_DIR)
                    if os.path.isdir(os.path.join(MEMES_DIR, d))
                } if os.path.isdir(MEMES_DIR) else set()
                cleaned = {k: v for k, v in descriptions.items() if k in local_dirs}
                if len(cleaned) != len(descriptions):
                    save_json(cleaned, MEMES_DATA_PATH)
                    logger.info(
                        "已清理 %d 个孤立的配置条目",
                        len(descriptions) - len(cleaned),
                    )
            except Exception as clean_err:
                logger.warning("清理孤立配置条目失败: %s", clean_err)

        sync_active_pack_metadata()

        return True
    except Exception as e:
        logger.error("插件初始化失败: %s", e)
        return False
