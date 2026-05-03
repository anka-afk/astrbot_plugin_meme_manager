import logging
import os
import shutil
from pathlib import Path

from ..config import DEFAULT_CATEGORY_DESCRIPTIONS, MEMES_DATA_PATH, MEMES_DIR
from ..utils import ensure_dir_exists, load_json, save_json

logger = logging.getLogger(__name__)


def is_safe_category_name(category: str) -> bool:
    """Return whether category stays within one memes directory segment."""
    if not category or category != category.strip():
        return False
    if category in {".", ".."}:
        return False
    return (
        "/" not in category and "\\" not in category and Path(category).name == category
    )


class CategoryManager:
    def __init__(self):
        """初始化类别管理器"""
        ensure_dir_exists(MEMES_DIR)
        self._ensure_data_file()
        self.descriptions = self._load_descriptions()

    def _ensure_data_file(self) -> None:
        """确保 memes_data.json 文件存在，不存在则创建并写入默认数据"""
        if not os.path.exists(MEMES_DATA_PATH):
            save_json(DEFAULT_CATEGORY_DESCRIPTIONS, MEMES_DATA_PATH)
            logger.info(f"创建默认类别描述文件: {MEMES_DATA_PATH}")

    def _load_descriptions(self) -> dict[str, str]:
        """加载类别描述配置"""
        return load_json(MEMES_DATA_PATH, DEFAULT_CATEGORY_DESCRIPTIONS)

    def reload_descriptions(self) -> dict[str, str]:
        """Reload category descriptions from disk."""
        self.descriptions = self._load_descriptions()
        return self.descriptions

    def get_local_categories(self) -> set[str]:
        """获取本地文件夹中的类别"""
        try:
            return {
                d
                for d in os.listdir(MEMES_DIR)
                if os.path.isdir(os.path.join(MEMES_DIR, d))
            }
        except Exception as e:
            logger.error(f"获取本地类别失败: {e}")
            return set()

    def get_sync_status(self) -> tuple[list[str], list[str]]:
        """获取同步状态
        返回: (missing_in_config, deleted_categories)
        """
        local_categories = self.get_local_categories()
        self.reload_descriptions()
        config_categories = set(self.descriptions.keys())

        return (
            list(local_categories - config_categories),  # 本地有但配置没有
            list(config_categories - local_categories),  # 配置有但本地没有
        )

    def update_description(self, category: str, description: str) -> bool:
        """更新类别描述"""
        try:
            self.reload_descriptions()
            self.descriptions[category] = description  # 更新内存中的 descriptions
            # 同步保存到文件
            return save_json(self.descriptions, MEMES_DATA_PATH)
        except Exception as e:
            logger.error(f"更新类别描述失败: {e}")
            return False

    def create_category(self, category: str, description: str = "请添加描述") -> bool:
        """创建类别目录并写入描述。"""
        try:
            category = category.strip()
            description = description.strip() or "请添加描述"
            if not is_safe_category_name(category):
                return False

            os.makedirs(os.path.join(MEMES_DIR, category), exist_ok=True)
            return self.update_description(category, description)
        except Exception as e:
            logger.error(f"创建类别失败: {e}")
            return False

    def rename_category(self, old_name: str, new_name: str) -> bool:
        """重命名类别"""
        try:
            self.reload_descriptions()
            if old_name not in self.descriptions:
                return False

            # 获取旧类别的描述
            description = self.descriptions[old_name]

            # 更新配置
            del self.descriptions[old_name]
            self.descriptions[new_name] = description

            # 更新文件夹名称
            old_path = os.path.join(MEMES_DIR, old_name)
            new_path = os.path.join(MEMES_DIR, new_name)
            if os.path.exists(old_path):
                os.rename(old_path, new_path)

            # 同步更新内存中的数据
            return save_json(self.descriptions, MEMES_DATA_PATH)
        except Exception as e:
            logger.error(f"重命名类别失败: {e}")
            return False

    def delete_category(self, category: str) -> bool:
        """删除类别"""
        try:
            self.reload_descriptions()
            # 从配置中删除
            if category in self.descriptions:
                del self.descriptions[category]
                save_json(self.descriptions, MEMES_DATA_PATH)

            # 删除文件夹
            category_path = os.path.join(MEMES_DIR, category)
            if os.path.exists(category_path):
                shutil.rmtree(category_path)

            return True
        except Exception as e:
            logger.error(f"删除类别失败: {e}")
            return False

    def remove_from_config(self, category: str) -> bool:
        """Remove a category from the description config only (keep directory on disk)."""
        try:
            self.reload_descriptions()
            if category not in self.descriptions:
                return False
            del self.descriptions[category]
            return save_json(self.descriptions, MEMES_DATA_PATH)
        except Exception as e:
            logger.error(f"从配置中移除类别失败: {e}")
            return False

    def get_descriptions(self) -> dict[str, str]:
        """获取所有类别描述"""
        self.reload_descriptions()
        return self.descriptions.copy()  # 返回字典的副本

    def sync_with_filesystem(self) -> bool:
        """同步文件系统和配置"""
        try:
            self.reload_descriptions()
            local_categories = self.get_local_categories()
            changed = False

            # 为新类别添加默认描述
            for category in local_categories:
                if category not in self.descriptions:
                    self.descriptions[category] = "请添加描述"
                    changed = True

            if changed:
                return save_json(self.descriptions, MEMES_DATA_PATH)
            return True
        except Exception as e:
            logger.error(f"同步文件系统失败: {e}")
            return False
