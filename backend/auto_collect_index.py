from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from PIL import Image

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


@dataclass(frozen=True, slots=True)
class ImageMatch:
    """描述字节完全相同的图片及可选的视觉复核提示。

    Args:
        exact_path: 已找到的 SHA-256 完全相同的现有图片。
        similar_path: dHash 接近的静态图片，仅供人工复核参考。
        distance: 视觉提示对应的 dHash 差异位数。
    """

    exact_path: Path | None = None
    similar_path: Path | None = None
    distance: int | None = None


class AutoCollectImageIndex:
    """按表情包缓存文件指纹，以最近最少使用策略限制缓存数量。"""

    def __init__(self, max_packs: int = 8):
        """初始化可供后台并发查询的索引。

        Args:
            max_packs: 内存中最多保留的表情包清单数量。
        """
        self.max_packs = max(1, max_packs)
        self._packs: OrderedDict[Path, dict[Path, tuple[int, int, str, int | None]]] = (
            OrderedDict()
        )
        self._lock = RLock()

    @staticmethod
    def _difference_hash(path: Path) -> int | None:
        """在大小限制内解码静态图片，生成参考用的视觉指纹。

        Args:
            path: 待检查的图片。

        Returns:
            64 位 dHash；动画、过大或无效图片返回 None。
        """
        try:
            with Image.open(path) as image:
                if (
                    getattr(image, "is_animated", False)
                    or image.width * image.height > 40_000_000
                ):
                    return None
                with image.convert("L") as grayscale:
                    with grayscale.resize((9, 8), Image.Resampling.LANCZOS) as small:
                        pixels = small.tobytes()
                fingerprint = 0
                for y in range(8):
                    for x in range(8):
                        fingerprint = (fingerprint << 1) | (
                            pixels[y * 9 + x] > pixels[y * 9 + x + 1]
                        )
                return fingerprint
        except (OSError, ValueError, Image.DecompressionBombError):
            return None

    def lookup(
        self, pack_root: Path, digest: str, candidate_path: Path | None = None
    ) -> ImageMatch:
        """刷新低开销的文件状态，查找完全匹配项或复核提示。

        本方法会读取磁盘并解码图片，异步调用方必须
        使用工作线程。仅重新计算变更文件的哈希。视觉哈希
        仅在提供候选图片时按需计算。近似匹配
        不能视为重复图片，因为文字差异可能具有实际意义。

        Args:
            pack_root: 目标表情包的图片目录。
            digest: 候选图片原始字节的 SHA-256。
            candidate_path: 可选的候选文件，用于提供视觉相似度提示。

        Returns:
            完全匹配项，或差异不超过五位的最接近静态图片提示。
        """
        root = Path(pack_root).resolve()
        with self._lock:
            previous = self._packs.pop(root, {})
            current = {}
            # 遍历前排除符号链接，包括 Windows 目录联接。
            directories = [root]
            visited = set()
            while directories:
                directory = directories.pop()
                try:
                    resolved = directory.resolve()
                    if not resolved.is_relative_to(root) or resolved in visited:
                        continue
                    visited.add(resolved)
                    children = list(directory.iterdir())
                except (OSError, RuntimeError):
                    continue
                for path in children:
                    try:
                        if path.is_symlink() or not path.resolve().is_relative_to(root):
                            continue
                        if path.is_dir():
                            directories.append(path)
                            continue
                        if (
                            path.suffix.lower() not in IMAGE_EXTENSIONS
                            or not path.is_file()
                        ):
                            continue
                        stat = path.stat()
                        signature = (stat.st_size, stat.st_mtime_ns)
                        cached = previous.get(path)
                        if cached is not None and cached[:2] == signature:
                            if candidate_path is not None and cached[3] == -1:
                                cached = (*cached[:3], self._difference_hash(path))
                            current[path] = cached
                            continue
                        hasher = hashlib.sha256()
                        with path.open("rb") as source:
                            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                                hasher.update(chunk)
                        # -1 表示尚未请求；None 表示无法进行视觉匹配，
                        # 例如图片是动图。
                        visual_hash = (
                            self._difference_hash(path)
                            if candidate_path is not None
                            else -1
                        )
                        # 文件若在并发操作中被替换，下次必须重新读取。
                        after = path.stat()
                        if signature != (after.st_size, after.st_mtime_ns):
                            continue
                        current[path] = (*signature, hasher.hexdigest(), visual_hash)
                    except (OSError, RuntimeError):
                        continue
            self._packs[root] = current
            while len(self._packs) > self.max_packs:
                self._packs.popitem(last=False)
            for path, (_, _, existing_digest, _) in current.items():
                if digest == existing_digest:
                    return ImageMatch(exact_path=path)
            if candidate_path is None:
                return ImageMatch()
            candidate_hash = self._difference_hash(candidate_path)
            if candidate_hash is None:
                return ImageMatch()
            closest = ImageMatch()
            for path, (_, _, _, visual_hash) in current.items():
                if visual_hash is None:
                    continue
                distance = (candidate_hash ^ visual_hash).bit_count()
                if distance <= 5 and (
                    closest.distance is None or distance < closest.distance
                ):
                    closest = ImageMatch(similar_path=path, distance=distance)
            return closest
