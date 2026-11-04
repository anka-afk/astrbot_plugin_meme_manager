"""存储适配器共用的跨平台路径、指纹及原子写入。"""

import hashlib
import json
import ntpath
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path, PureWindowsPath

from PIL import Image

SUPPORTED_FORMATS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
MAX_IMAGE_BYTES = 1024 * 1024 * 1024


def normalize_relative_path(value: str) -> str:
    """校验路径，不悄然改变其含义。

    Args:
        value: 可使用任一平台分隔符的相对图片路径。

    Returns:
        可跨平台使用的 POSIX 路径。

    Raises:
        ValueError: 路径超出命名空间或形成 Windows 文件名别名。
    """
    value = str(value).replace("\\", "/")
    parts = value.split("/")
    is_reserved = getattr(ntpath, "isreserved", None)
    if (
        not value
        or PureWindowsPath(value).drive
        or any(
            part.casefold() in {"", ".", "..", ".sync-state"}
            or part.rstrip(" .") != part
            or any(ord(char) < 32 or char in '<>:"|?*' for char in part)
            or (
                is_reserved(part)
                if is_reserved is not None
                else PureWindowsPath(part).is_reserved()
            )
            for part in parts
        )
    ):
        raise ValueError(f"Unsafe image path: {value!r}")
    return "/".join(parts)


def file_fingerprint(file_path: Path) -> dict:
    """使用有限内存计算稳定文件的哈希。

    Args:
        file_path: 待读取的文件。

    Returns:
        文件大小、SHA-256 摘要及纳秒级修改时间戳。

    Raises:
        OSError: 读取期间文件变化或无法读取。
    """
    before = file_path.stat()
    digest = hashlib.sha256()
    with file_path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    after = file_path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise OSError(f"Image changed while reading: {file_path.name}")
    return {
        "size": after.st_size,
        "sha256": digest.hexdigest(),
        "mtime_ns": after.st_mtime_ns,
    }


def write_json_atomic(target: Path, data: dict) -> None:
    """持久保存状态，避免暴露未完整写入的 JSON。

    Args:
        target: 待替换的状态文件。
        data: 可序列化的状态。

    Raises:
        OSError: 无法将新状态持久写入。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_image_stream(
    chunks: Iterable[bytes],
    save_path: Path,
    expected_size: int | None = None,
    expected_sha256: str = "",
) -> None:
    """校验流式下载的图片后，再原子发布。

    Args:
        chunks: 下载的字节分块。
        save_path: 最终目标路径。
        expected_size: 提供方给出的确切字节长度（如有）。
        expected_sha256: 提供方给出的内容校验和（如有）。

    Raises:
        ValueError: 传输被截断、文件过大或内容不是图片。
        OSError: 读取或保存失败，现有目标保持完整。
    """
    if expected_size is not None and not 0 < expected_size <= MAX_IMAGE_BYTES:
        raise ValueError("Remote image size is outside the supported range")
    save_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    digest = hashlib.sha256()
    size = 0
    try:
        with tempfile.NamedTemporaryFile(
            dir=save_path.parent, prefix=".image-download-", suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            for chunk in chunks:
                size += len(chunk)
                if size > MAX_IMAGE_BYTES or (
                    expected_size is not None and size > expected_size
                ):
                    raise ValueError("Downloaded image exceeds the expected size")
                digest.update(chunk)
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if not size or (expected_size is not None and size != expected_size):
            raise ValueError("Incomplete image download")
        if expected_sha256 and digest.hexdigest() != expected_sha256:
            raise ValueError("Downloaded image checksum mismatch")
        with Image.open(temporary) as image:
            if image.format not in {"JPEG", "PNG", "GIF", "WEBP"}:
                raise ValueError("Unsupported image content")
            image.verify()
        temporary.replace(save_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class FileHandler:
    """扫描完整本地清单，并解析安全的图片目标路径。"""

    SUPPORTED_FORMATS = SUPPORTED_FORMATS

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def scan_local_images(self) -> list[dict]:
        """返回完整、确定且包含内容指纹的文件清单。

        Returns:
            本地图片及其路径、分类、大小和校验和。

        Raises:
            OSError: 任一目录或图片无法读取。
            ValueError: 符号链接或存在歧义的路径导致镜像操作不安全。
        """
        images = []
        directories = [self.base_dir]
        seen = set()
        while directories:
            for path in sorted(directories.pop().iterdir()):
                if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                    raise ValueError(f"Image directory contains a link: {path.name}")
                if path.name == ".sync-state":
                    continue
                if path.is_dir():
                    directories.append(path)
                elif path.is_file() and path.suffix.lower() in SUPPORTED_FORMATS:
                    relative = normalize_relative_path(
                        path.relative_to(self.base_dir).as_posix()
                    )
                    if relative.casefold() in seen:
                        raise ValueError(f"Ambiguous image path: {relative}")
                    seen.add(relative.casefold())
                    category, _, filename = relative.rpartition("/")
                    images.append(
                        {
                            "path": str(path),
                            "id": relative,
                            "filename": filename,
                            "category": category,
                            **file_fingerprint(path),
                        }
                    )
        return sorted(images, key=lambda item: item["id"])

    def get_file_path(
        self, category: str, filename: str, *, create_parent: bool = True
    ) -> Path:
        """在本地根目录内解析远端名称。

        Args:
            category: 相对分类，可包含嵌套层级。
            filename: 单个文件名。
            create_parent: 是否创建目标目录。

        Returns:
            安全的绝对目标路径。

        Raises:
            ValueError: 名称或现有文件系统链接超出根目录。
        """
        filename = normalize_relative_path(filename)
        if "/" in filename:
            raise ValueError(f"Unsafe remote filename: {filename!r}")
        relative = normalize_relative_path(
            f"{category}/{filename}" if category else filename
        )
        target = self.base_dir.joinpath(*relative.split("/"))
        current = target
        while current != self.base_dir:
            if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
                raise ValueError(f"Image path contains a link: {relative}")
            current = current.parent
        target.resolve().relative_to(self.base_dir)
        if create_parent:
            target.parent.mkdir(parents=True, exist_ok=True)
        return target
