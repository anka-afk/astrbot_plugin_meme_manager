import logging
import os

from .config import (
    BASE_DATA_DIR,
    DEFAULT_CATEGORY_DESCRIPTIONS,
    MEMES_DATA_PATH,
    sync_active_pack_metadata,
)
from .utils import ensure_dir_exists, save_json

logger = logging.getLogger(__name__)


def init_plugin():
    """初始化运行时存储和兼容性元数据，不自动注入默认表情包。"""
    try:
        ensure_dir_exists(BASE_DATA_DIR)

        if not os.path.exists(MEMES_DATA_PATH):
            save_json(DEFAULT_CATEGORY_DESCRIPTIONS, MEMES_DATA_PATH)
            logger.info("已创建兼容性描述文件: %s", MEMES_DATA_PATH)

        sync_active_pack_metadata()

        return True
    except Exception as e:
        logger.error("插件初始化失败: %s", e)
        return False
