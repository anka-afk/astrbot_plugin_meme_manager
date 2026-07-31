import asyncio
import hashlib
import logging
import os
import shutil
from pathlib import Path

from werkzeug.utils import secure_filename

from .category_manager import is_safe_category_name
from .pack_resolver import resolve_pack_context

logger = logging.getLogger(__name__)
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def _get_memes_root() -> Path:
    """动态解析当前默认资源包的表情目录。"""
    return Path(resolve_pack_context()["memes_dir"]).resolve()


class DuplicateEmojiError(ValueError):
    """上传的表情已存在于目标分类时抛出。"""

    def __init__(self, existing_filename: str):
        self.existing_filename = existing_filename
        super().__init__(f"同一分类中已存在相同文件：{existing_filename}")


def _is_supported_image(filename: str) -> bool:
    return filename.lower().endswith(IMAGE_EXTENSIONS)


def _get_category_path(category: str) -> Path:
    """解析分类目录，并禁止目录穿越。

    Args:
        category: 只能包含单个目录段的分类名称。

    Returns:
        位于当前表情目录内的分类绝对路径。

    Raises:
        ValueError: 分类名称非法或解析后的路径超出表情目录。
    """
    normalized = str(category or "").strip()
    if not is_safe_category_name(normalized):
        raise ValueError("分类名称非法")

    memes_root = _get_memes_root()
    category_path = (memes_root / normalized).resolve()
    try:
        category_path.relative_to(memes_root)
    except ValueError as exc:
        raise ValueError("分类路径超出表情目录") from exc
    return category_path


def _iter_category_image_paths(category_path: Path) -> list[Path]:
    if not category_path.is_dir():
        return []
    return [
        path
        for path in category_path.iterdir()
        if path.is_file() and _is_supported_image(path.name)
    ]


def _calculate_file_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _find_duplicate_image(category_path: Path, content_hash: str) -> Path | None:
    for existing_path in _iter_category_image_paths(category_path):
        try:
            if _calculate_file_hash(existing_path.read_bytes()) == content_hash:
                return existing_path
        except OSError as exc:
            logger.warning(
                "读取现有文件失败，跳过判重: %s, 错误: %s", existing_path, exc
            )
    return None


def _build_available_file_path(category_path: Path, filename: str) -> Path:
    candidate = category_path / filename
    if not candidate.exists():
        return candidate

    suffix = Path(filename).suffix
    stem = Path(filename).stem
    index = 1
    while True:
        candidate = category_path / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


async def scan_emoji_folder():
    """扫描表情包目录，返回所有分类及其表情文件。"""

    def scan() -> dict[str, list[str]]:
        emoji_data = {}
        memes_root = _get_memes_root()
        memes_root.mkdir(parents=True, exist_ok=True)
        for category in os.listdir(memes_root):
            category_path = _get_category_path(category)
            if not category_path.is_dir():
                continue
            emoji_data[category] = [
                path.name for path in _iter_category_image_paths(category_path)
            ]
        return emoji_data

    return await asyncio.to_thread(scan)


def get_emoji_by_category(category):
    """获取指定类别下的所有表情包"""
    category_path = _get_category_path(category)
    if not category_path.is_dir():
        return []
    return [path.name for path in _iter_category_image_paths(category_path)]


def add_emoji_to_category(category, image_file):
    """
    添加表情包到指定类别

    Args:
        category: 类别名
        image_file: 上传的文件对象

    Returns:
        dict[str, str]: 保存后的文件路径和最终文件名
    """
    if not image_file:
        logger.error("没有接收到文件")
        raise ValueError("没有接收到文件")

    if not image_file.filename:
        logger.error("文件名为空")
        raise ValueError("文件名为空")

    filename = Path(str(image_file.filename).replace("\\", "/")).name
    original_suffix = Path(filename).suffix.lower()
    if original_suffix not in IMAGE_EXTENSIONS:
        raise ValueError("表情文件扩展名非法")

    # 确保类别目录存在
    category_path = _get_category_path(category)
    category_path.mkdir(parents=True, exist_ok=True)

    # 保存文件

    # 中文名称可能被 secure_filename 完全移除，因此需要单独保留合法扩展名。
    safe_filename = secure_filename(filename)
    if Path(safe_filename).suffix.lower() != original_suffix:
        safe_stem = secure_filename(Path(filename).stem)
        if not safe_stem:
            name_hash = hashlib.sha256(filename.encode("utf-8")).hexdigest()[:12]
            safe_stem = f"image_{name_hash}"
        safe_filename = f"{safe_stem}{original_suffix}"

    # 如果文件名被修改了，记录日志
    if safe_filename != filename:
        logger.info(f"文件名已从 {filename} 修改为安全的文件名 {safe_filename}")
        filename = safe_filename

    file_path = category_path / filename

    try:
        # 检查目录是否可写
        if not os.access(category_path, os.W_OK):
            logger.error(f"没有权限写入目录: {category_path}")
            raise OSError(f"没有权限写入目录: {category_path}")

        # 检查磁盘空间是否足够
        _, _, free = shutil.disk_usage(category_path)
        # 假设文件不会超过10MB，保险起见检查是否至少有10MB
        if free < 10 * 1024 * 1024:
            logger.error(f"磁盘空间不足: 只有 {free / 1024 / 1024:.2f}MB")
            raise OSError("磁盘空间不足")

        # 直接以二进制方式读取和写入文件，避免FileStorage.save可能存在的问题
        image_file.stream.seek(0)  # 确保从头开始读取
        content = image_file.stream.read()
        if not content:
            logger.error("文件内容为空")
            raise OSError("上传文件内容为空")

        content_hash = _calculate_file_hash(content)
        duplicate_path = _find_duplicate_image(category_path, content_hash)
        if duplicate_path is not None:
            logger.info(
                "跳过重复文件上传: 类别=%s, 上传名=%s, 已存在文件=%s",
                category,
                filename,
                duplicate_path.name,
            )
            raise DuplicateEmojiError(duplicate_path.name)

        file_path = _build_available_file_path(category_path, filename)

        # 记录日志，包括绝对路径
        logger.info(f"准备保存文件到: {file_path.absolute()}")

        # 以二进制写入模式保存文件
        with open(file_path, "wb") as f:
            f.write(content)

        # 验证文件是否成功保存
        if not file_path.exists():
            logger.error(f"文件保存失败，{file_path} 不存在")
            raise OSError(f"文件保存失败，{file_path} 不存在")

        file_size = file_path.stat().st_size
        if file_size == 0:
            logger.error(f"文件保存失败，{file_path} 大小为0")
            raise OSError(f"文件保存失败，{file_path} 大小为0")

        logger.info(f"文件成功保存到 {file_path}, 大小: {file_size} 字节")
        return {"path": str(file_path), "filename": file_path.name}

    except Exception as e:
        if isinstance(e, DuplicateEmojiError):
            raise
        logger.error(f"保存文件时出错: {str(e)}", exc_info=True)
        # 如果文件已部分创建，尝试删除
        if file_path.exists():
            try:
                file_path.unlink()  # 删除文件
                logger.info(f"已删除部分上传的文件: {file_path}")
            except Exception as del_e:
                logger.error(f"无法删除部分上传的文件: {del_e}")
        raise OSError(f"保存文件时出错: {str(e)}")


def delete_emoji_from_category(category, image_file):
    """删除指定类别下的表情包"""
    category_path = _get_category_path(category)
    if not category_path.is_dir():
        return False

    image_name = Path(image_file).name
    image_path = category_path / image_name
    if image_path.is_file() and _is_supported_image(image_path.name):
        image_path.unlink()
        return True
    return False


def batch_delete_emojis(category: str, image_files: list[str]) -> dict[str, object]:
    """批量删除指定类别下的表情包。"""
    category_path = _get_category_path(category)
    if not category_path.is_dir():
        return {
            "category_exists": False,
            "deleted_files": [],
            "missing_files": [],
        }

    deleted_files = []
    missing_files = []
    for image_file in dict.fromkeys(image_files):
        if delete_emoji_from_category(category, image_file):
            deleted_files.append(Path(image_file).name)
        else:
            missing_files.append(Path(image_file).name)

    return {
        "category_exists": True,
        "deleted_files": deleted_files,
        "missing_files": missing_files,
    }


def move_emoji_to_category(
    source_category: str, image_file: str, target_category: str
) -> dict[str, object]:
    """将单个表情包移动到另一个类别。"""
    source_category_path = _get_category_path(source_category)
    if not source_category_path.is_dir():
        return {
            "source_category_exists": False,
            "target_category": target_category,
            "filename": Path(image_file).name,
            "moved": False,
            "conflict": False,
            "missing": True,
        }

    target_category_path = _get_category_path(target_category)
    target_category_path.mkdir(parents=True, exist_ok=True)

    image_name = Path(image_file).name
    source_image_path = source_category_path / image_name
    target_image_path = target_category_path / image_name

    if not source_image_path.is_file() or not _is_supported_image(
        source_image_path.name
    ):
        return {
            "source_category_exists": True,
            "target_category": target_category,
            "filename": image_name,
            "moved": False,
            "conflict": False,
            "missing": True,
        }

    if target_image_path.exists():
        return {
            "source_category_exists": True,
            "target_category": target_category,
            "filename": image_name,
            "moved": False,
            "conflict": True,
            "missing": False,
        }

    shutil.move(str(source_image_path), str(target_image_path))
    return {
        "source_category_exists": True,
        "source_category": source_category,
        "target_category": target_category,
        "filename": image_name,
        "moved": True,
        "conflict": False,
        "missing": False,
    }


def batch_move_emojis(
    source_category: str, image_files: list[str], target_category: str
) -> dict[str, object]:
    """批量将表情包移动到另一个类别。"""
    source_category_path = _get_category_path(source_category)
    if not source_category_path.is_dir():
        return {
            "source_category_exists": False,
            "moved_files": [],
            "missing_files": [],
            "conflicting_files": [],
        }

    moved_files = []
    missing_files = []
    conflicting_files = []

    for image_file in dict.fromkeys(image_files):
        result = move_emoji_to_category(source_category, image_file, target_category)
        if result["moved"]:
            moved_files.append(result["filename"])
        elif result["conflict"]:
            conflicting_files.append(result["filename"])
        elif result["missing"]:
            missing_files.append(result["filename"])

    return {
        "source_category_exists": True,
        "source_category": source_category,
        "target_category": target_category,
        "moved_files": moved_files,
        "missing_files": missing_files,
        "conflicting_files": conflicting_files,
    }


def copy_emoji_to_category(
    source_category: str, image_file: str, target_category: str
) -> dict[str, object]:
    """将单个表情包复制到另一个类别。"""
    source_category_path = _get_category_path(source_category)
    if not source_category_path.is_dir():
        return {
            "source_category_exists": False,
            "target_category": target_category,
            "filename": Path(image_file).name,
            "copied": False,
            "conflict": False,
            "missing": True,
        }

    target_category_path = _get_category_path(target_category)
    target_category_path.mkdir(parents=True, exist_ok=True)

    image_name = Path(image_file).name
    source_image_path = source_category_path / image_name
    target_image_path = target_category_path / image_name

    if not source_image_path.is_file() or not _is_supported_image(
        source_image_path.name
    ):
        return {
            "source_category_exists": True,
            "target_category": target_category,
            "filename": image_name,
            "copied": False,
            "conflict": False,
            "missing": True,
        }

    if target_image_path.exists():
        return {
            "source_category_exists": True,
            "target_category": target_category,
            "filename": image_name,
            "copied": False,
            "conflict": True,
            "missing": False,
        }

    shutil.copy2(source_image_path, target_image_path)
    return {
        "source_category_exists": True,
        "source_category": source_category,
        "target_category": target_category,
        "filename": image_name,
        "copied": True,
        "conflict": False,
        "missing": False,
    }


def batch_copy_emojis(
    source_category: str, image_files: list[str], target_category: str
) -> dict[str, object]:
    """批量将表情包复制到另一个类别。"""
    source_category_path = _get_category_path(source_category)
    if not source_category_path.is_dir():
        return {
            "source_category_exists": False,
            "copied_files": [],
            "missing_files": [],
            "conflicting_files": [],
        }

    copied_files = []
    missing_files = []
    conflicting_files = []

    for image_file in dict.fromkeys(image_files):
        result = copy_emoji_to_category(source_category, image_file, target_category)
        if result["copied"]:
            copied_files.append(result["filename"])
        elif result["conflict"]:
            conflicting_files.append(result["filename"])
        elif result["missing"]:
            missing_files.append(result["filename"])

    return {
        "source_category_exists": True,
        "source_category": source_category,
        "target_category": target_category,
        "copied_files": copied_files,
        "missing_files": missing_files,
        "conflicting_files": conflicting_files,
    }


def clear_category_emojis(category: str) -> dict[str, object]:
    """清空指定类别下的所有表情包，但保留类别目录和配置。"""
    category_path = _get_category_path(category)
    if not category_path.is_dir():
        return {
            "category_exists": False,
            "deleted_files": [],
        }

    deleted_files = []
    for image_path in _iter_category_image_paths(category_path):
        image_path.unlink()
        deleted_files.append(image_path.name)

    return {
        "category_exists": True,
        "deleted_files": deleted_files,
    }


def clear_all_emojis() -> dict[str, object]:
    """清空所有类别中的表情包，但保留目录和配置。"""
    deleted_by_category = {}
    memes_root = _get_memes_root()
    if not memes_root.exists():
        return {"deleted_by_category": deleted_by_category}

    for category_path in memes_root.iterdir():
        if not category_path.is_dir():
            continue
        result = clear_category_emojis(category_path.name)
        deleted_files = result["deleted_files"]
        if deleted_files:
            deleted_by_category[category_path.name] = len(deleted_files)

    return {"deleted_by_category": deleted_by_category}


def update_emoji_in_category(category, old_image_file, new_image_file):
    """更新（替换）表情包文件"""
    category_path = _get_category_path(category)

    if not category_path.is_dir():
        return False
    old_image_name = Path(str(old_image_file or "")).name
    old_image_path = category_path / old_image_name
    if old_image_path.is_file() and _is_supported_image(old_image_name):
        filename = secure_filename(str(new_image_file.filename or ""))
        if not filename or not _is_supported_image(filename):
            raise ValueError("表情文件名或扩展名非法")
        old_image_path.unlink()
        new_image_file.save(str(category_path / filename))
        return True
    return False
