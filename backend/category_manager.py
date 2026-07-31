import json
import logging
import os
import shutil
from pathlib import Path

from ..utils import ensure_dir_exists, load_json, save_json
from .pack_resolver import resolve_pack_context
from .semantic_storage import invalidate_semantic_metadata

logger = logging.getLogger(__name__)


def is_safe_category_name(category: str) -> bool:
    """判断分类名称是否严格位于表情目录的单个路径段内。"""
    if not category or category != category.strip():
        return False
    if category in {".", ".."}:
        return False
    return (
        "/" not in category and "\\" not in category and Path(category).name == category
    )


def resolve_safe_category_directory(memes_root: str | Path, category: str) -> Path:
    """解析并校验表情分类目录不会逃逸资源包目录。

    Args:
        memes_root: 当前资源包的表情根目录。
        category: 待解析的单段分类名称。

    Returns:
        位于表情根目录内的已解析分类路径。

    Raises:
        ValueError: 分类名称非法或解析后的路径逃逸根目录时抛出。
    """
    if not is_safe_category_name(category):
        raise ValueError(f"非法分类名称: {category!r}")

    resolved_root = Path(memes_root).resolve()
    category_path = (resolved_root / category).resolve()
    try:
        category_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"分类目录逃逸表情根目录: {category!r}") from exc
    return category_path


class CategoryManager:
    def __init__(self):
        """初始化类别管理器"""
        ensure_dir_exists(self._paths()["memes_dir"])
        self._ensure_data_file()
        self.descriptions = self._load_descriptions()

    @staticmethod
    def _paths() -> dict[str, Path]:
        """动态返回当前默认资源包的管理路径。"""
        context = resolve_pack_context()
        return {
            "pack_dir": Path(context["pack_dir"]),
            "memes_dir": Path(context["memes_dir"]),
            "metadata_path": Path(context["metadata_path"]),
            "manifest_path": Path(context["manifest_path"]),
        }

    def _sync_manifest(self) -> bool:
        """将当前分类描述同步到当前默认资源包清单。"""
        paths = self._paths()
        manifest = load_json(paths["manifest_path"], {})
        if not isinstance(manifest, dict):
            manifest = {}
        pack_id = paths["pack_dir"].name
        manifest.setdefault("schema_version", 1)
        manifest.setdefault("id", pack_id)
        manifest.setdefault("name", f"Meme Pack {pack_id}")
        manifest.setdefault("version", "1.0.0")
        manifest.setdefault("description", "Runtime-managed meme pack")
        manifest["categories"] = {
            category: {"description": description}
            for category, description in sorted(self.descriptions.items())
        }
        return save_json(manifest, paths["manifest_path"])

    def _ensure_data_file(self) -> None:
        """确保 memes_data.json 文件存在，不存在时基于当前包内容初始化。"""
        metadata_path = self._paths()["metadata_path"]
        if not metadata_path.exists():
            initial_descriptions = self._build_initial_descriptions()
            save_json(initial_descriptions, metadata_path)
            self.descriptions = initial_descriptions
            logger.info(f"初始化类别描述文件: {metadata_path}")
            self._sync_manifest()

    def _build_initial_descriptions(self) -> dict[str, str]:
        """在缺失 memes_data.json 时，从目录与 manifest 构建初始描述。"""
        descriptions: dict[str, str] = {}
        local_categories = self.get_local_categories()

        # 1) 优先读取当前包 manifest 的分类描述（官方包通常只带 manifest）
        try:
            manifest_path = self._paths()["manifest_path"]
            if manifest_path.is_file():
                with manifest_path.open(encoding="utf-8-sig") as file_obj:
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
        metadata_path = self._paths()["metadata_path"]
        if not metadata_path.exists():
            self._ensure_data_file()
        return load_json(metadata_path, {})

    def reload_descriptions(self) -> dict[str, str]:
        """从磁盘重新加载分类描述。"""
        self.descriptions = self._load_descriptions()
        return self.descriptions

    def _invalidate_semantic_if_present(self) -> None:
        pack_dir = self._paths()["pack_dir"].resolve()
        if not (pack_dir / "semantic_metadata.json").is_file():
            return
        try:
            invalidate_semantic_metadata(pack_dir)
        except Exception as exc:
            logger.error(f"分类变更后刷新语义元数据失败: {exc}", exc_info=True)

    def get_local_categories(self) -> set[str]:
        """获取本地文件夹中的类别"""
        try:
            memes_dir = self._paths()["memes_dir"]
            ensure_dir_exists(memes_dir)
            return {d for d in os.listdir(memes_dir) if os.path.isdir(memes_dir / d)}
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
            original_descriptions = self.descriptions.copy()
            old_description = str(self.descriptions.get(category) or "")
            self.descriptions[category] = description  # 更新内存中的描述映射
            saved = save_json(self.descriptions, self._paths()["metadata_path"])
            if not saved:
                self.descriptions = original_descriptions
                return False
            if not self._sync_manifest():
                self.descriptions = original_descriptions
                save_json(self.descriptions, self._paths()["metadata_path"])
                self._sync_manifest()
                return False
            if " ".join(old_description.split()) != " ".join(str(description).split()):
                self._invalidate_semantic_if_present()
            return True
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

            category_path = self._paths()["memes_dir"] / category
            directory_existed = category_path.exists()
            category_path.mkdir(parents=True, exist_ok=True)
            created = self.update_description(category, description)
            if not created and not directory_existed:
                try:
                    category_path.rmdir()
                except OSError:
                    logger.warning(f"创建分类回滚时无法移除目录: {category_path}")
            return created
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

            memes_dir = self._paths()["memes_dir"]
            old_path = memes_dir / old_name
            new_path = memes_dir / new_name
            if new_name != old_name and new_path.exists():
                return False

            original_descriptions = self.descriptions.copy()
            # 获取旧类别的描述
            description = self.descriptions[old_name]

            # 更新配置
            del self.descriptions[old_name]
            self.descriptions[new_name] = description

            # 更新文件夹名称
            directory_renamed = new_name != old_name and old_path.exists()
            if directory_renamed:
                os.rename(old_path, new_path)

            saved = save_json(self.descriptions, self._paths()["metadata_path"])
            if saved and self._sync_manifest():
                self._invalidate_semantic_if_present()
                return True

            if directory_renamed and new_path.exists() and not old_path.exists():
                os.rename(new_path, old_path)
            self.descriptions = original_descriptions
            save_json(self.descriptions, self._paths()["metadata_path"])
            self._sync_manifest()
            return False
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
            original_descriptions = self.descriptions.copy()
            # 从配置中删除
            if category in self.descriptions:
                del self.descriptions[category]
                if not save_json(self.descriptions, self._paths()["metadata_path"]):
                    self.descriptions = original_descriptions
                    save_json(self.descriptions, self._paths()["metadata_path"])
                    self._sync_manifest()
                    return False
            if not self._sync_manifest():
                self.descriptions = original_descriptions
                save_json(self.descriptions, self._paths()["metadata_path"])
                self._sync_manifest()
                return False

            # 删除文件夹
            category_path = self._paths()["memes_dir"] / category
            if os.path.exists(category_path):
                try:
                    shutil.rmtree(category_path)
                except Exception:
                    self.descriptions = original_descriptions
                    save_json(self.descriptions, self._paths()["metadata_path"])
                    self._sync_manifest()
                    raise

            self._invalidate_semantic_if_present()
            return True
        except Exception as e:
            logger.error(f"删除类别失败: {e}")
            return False

    def remove_from_config(self, category: str) -> bool:
        """仅从描述配置中移除分类，并保留磁盘上的分类目录。"""
        try:
            category = str(category or "").strip()
            if not is_safe_category_name(category):
                return False
            self.reload_descriptions()
            if category not in self.descriptions:
                return False
            del self.descriptions[category]
            saved = save_json(self.descriptions, self._paths()["metadata_path"])
            if saved:
                self._sync_manifest()
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
                saved = save_json(self.descriptions, self._paths()["metadata_path"])
                if saved:
                    self._sync_manifest()
                    self._invalidate_semantic_if_present()
                return saved
            self._sync_manifest()
            return True
        except Exception as e:
            logger.error(f"同步文件系统失败: {e}")
            return False
