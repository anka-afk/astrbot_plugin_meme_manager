import json
import logging
import os
import shutil
from pathlib import Path

from ..config import (
    ACTIVE_PACK_MANIFEST_PATH,
    MEMES_DATA_PATH,
    MEMES_DIR,
    sync_active_pack_metadata,
)
from ..utils import ensure_dir_exists, load_json, save_json
from .semantic_storage import invalidate_semantic_metadata

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
        """确保 memes_data.json 文件存在，不存在时基于当前包内容初始化。"""
        if not os.path.exists(MEMES_DATA_PATH):
            initial_descriptions = self._build_initial_descriptions()
            save_json(initial_descriptions, MEMES_DATA_PATH)
            logger.info(f"初始化类别描述文件: {MEMES_DATA_PATH}")
            sync_active_pack_metadata(initial_descriptions)

    def _build_initial_descriptions(self) -> dict[str, str]:
        """在缺失 memes_data.json 时，从目录与 manifest 构建初始描述。"""
        descriptions: dict[str, str] = {}
        local_categories = self.get_local_categories()

        # 1) 优先读取当前包 manifest 的分类描述（官方包通常只带 manifest）
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
                        if not key or key not in local_categories:
                            continue
                        if isinstance(meta, dict):
                            descriptions[key] = str(
                                meta.get("description") or "请添加描述"
                            )
                        else:
                            descriptions[key] = str(meta or "请添加描述")
        except Exception as exc:
            logger.warning(f"从 manifest 初始化类别描述失败: {exc}")

        # 2) 补齐实际目录存在但 manifest 未声明的分类
        for category in local_categories:
            descriptions.setdefault(category, "请添加描述")

        return descriptions

    def _load_descriptions(self) -> dict[str, str]:
        """加载类别描述配置"""
        if not os.path.exists(MEMES_DATA_PATH):
            self._ensure_data_file()
        return load_json(MEMES_DATA_PATH, {})

    def reload_descriptions(self) -> dict[str, str]:
        """Reload category descriptions from disk."""
        self.descriptions = self._load_descriptions()
        return self.descriptions

    @staticmethod
    def _invalidate_semantic_if_present() -> None:
        pack_dir = Path(MEMES_DIR).resolve().parent
        if not (pack_dir / "semantic_metadata.json").is_file():
            return
        try:
            invalidate_semantic_metadata(pack_dir)
        except Exception as exc:
            logger.error(f"分类变更后刷新语义元数据失败: {exc}", exc_info=True)

    def get_local_categories(self) -> set[str]:
        """获取本地文件夹中的类别"""
        try:
            ensure_dir_exists(MEMES_DIR)
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
            category = str(category or "").strip()
            if not is_safe_category_name(category):
                return False
            self.reload_descriptions()
            old_description = str(self.descriptions.get(category) or "")
            self.descriptions[category] = description  # 更新内存中的 descriptions
            saved = save_json(self.descriptions, MEMES_DATA_PATH)
            if saved:
                sync_active_pack_metadata(self.descriptions)
                if " ".join(old_description.split()) != " ".join(
                    str(description).split()
                ):
                    self._invalidate_semantic_if_present()
            return saved
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
            old_name = str(old_name or "").strip()
            new_name = str(new_name or "").strip()
            if (
                not is_safe_category_name(old_name)
                or old_name not in self.descriptions
                or not is_safe_category_name(new_name)
                or (new_name != old_name and new_name in self.descriptions)
            ):
                return False

            old_path = Path(MEMES_DIR) / old_name
            new_path = Path(MEMES_DIR) / new_name
            if new_name != old_name and new_path.exists():
                return False

            # 获取旧类别的描述
            description = self.descriptions[old_name]

            # 更新配置
            del self.descriptions[old_name]
            self.descriptions[new_name] = description

            # 更新文件夹名称
            if os.path.exists(old_path):
                os.rename(old_path, new_path)

            saved = save_json(self.descriptions, MEMES_DATA_PATH)
            if saved:
                sync_active_pack_metadata(self.descriptions)
                self._invalidate_semantic_if_present()
            return saved
        except Exception as e:
            logger.error(f"重命名类别失败: {e}")
            return False

    def delete_category(self, category: str) -> bool:
        """删除类别"""
        try:
            category = str(category or "").strip()
            if not is_safe_category_name(category):
                return False
            self.reload_descriptions()
            # 从配置中删除
            if category in self.descriptions:
                del self.descriptions[category]
                save_json(self.descriptions, MEMES_DATA_PATH)

            # 删除文件夹
            category_path = os.path.join(MEMES_DIR, category)
            if os.path.exists(category_path):
                shutil.rmtree(category_path)

            sync_active_pack_metadata(self.descriptions)
            self._invalidate_semantic_if_present()
            return True
        except Exception as e:
            logger.error(f"删除类别失败: {e}")
            return False

    def remove_from_config(self, category: str) -> bool:
        """Remove a category from the description config only (keep directory on disk)."""
        try:
            category = str(category or "").strip()
            if not is_safe_category_name(category):
                return False
            self.reload_descriptions()
            if category not in self.descriptions:
                return False
            del self.descriptions[category]
            saved = save_json(self.descriptions, MEMES_DATA_PATH)
            if saved:
                sync_active_pack_metadata(self.descriptions)
                self._invalidate_semantic_if_present()
            return saved
        except Exception as e:
            logger.error(f"从配置中移除类别失败: {e}")
            return False

    def get_descriptions(self) -> dict[str, str]:
        """获取所有类别描述"""
        self.reload_descriptions()
        return self.descriptions.copy()  # 返回字典的副本

    def sync_with_filesystem(self) -> bool:
        """同步文件系统和配置：将配置强制对齐为实际文件夹结构"""
        try:
            self.reload_descriptions()
            local_categories = self.get_local_categories()
            changed = False

            # 为新类别添加默认描述
            for category in local_categories:
                if category not in self.descriptions:
                    self.descriptions[category] = "请添加描述"
                    changed = True

            # 删除配置中不存在对应文件夹的条目
            stale = [c for c in list(self.descriptions) if c not in local_categories]
            for category in stale:
                del self.descriptions[category]
                changed = True

            if changed:
                saved = save_json(self.descriptions, MEMES_DATA_PATH)
                if saved:
                    sync_active_pack_metadata(self.descriptions)
                    self._invalidate_semantic_if_present()
                return saved
            sync_active_pack_metadata(self.descriptions)
            return True
        except Exception as e:
            logger.error(f"同步文件系统失败: {e}")
            return False
