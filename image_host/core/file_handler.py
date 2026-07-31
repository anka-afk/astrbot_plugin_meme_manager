from pathlib import Path, PurePosixPath, PureWindowsPath


class FileHandler:
    """文件处理类"""

    SUPPORTED_FORMATS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def scan_local_images(self) -> list[dict[str, str]]:
        """扫描本地图片"""
        images = []
        for file_path in self.base_dir.rglob("*"):
            if (
                file_path.is_file()
                and file_path.suffix.lower() in self.SUPPORTED_FORMATS
            ):
                # 计算相对路径
                rel_path = file_path.relative_to(self.base_dir)
                category = str(rel_path.parent).replace("\\", "/")
                if category == ".":
                    category = ""

                # 构建文件信息
                filename = rel_path.name
                file_id = str(rel_path).replace("\\", "/")

                images.append(
                    {
                        "path": str(file_path),
                        "id": file_id,  # 使用文件名作为标识
                        "filename": filename,
                        "category": category,  # 保留分类信息
                    }
                )
        return images

    def get_file_path(self, category: str, filename: str) -> Path:
        """Return a safe local path for a remotely supplied image name.

        Args:
            category: Optional relative category path from the remote provider.
            filename: Image filename from the remote provider.

        Returns:
            A resolved path contained by the configured base directory.

        Raises:
            ValueError: If the category or filename can escape the base directory.
        """
        normalized_category = str(category or "").replace("\\", "/")
        normalized_filename = str(filename or "").replace("\\", "/")

        category_parts = (
            PurePosixPath(normalized_category).parts if normalized_category else ()
        )
        if (
            (normalized_category and normalized_category.startswith("/"))
            or PureWindowsPath(normalized_category).drive
            or any(part in {"", ".", ".."} for part in category_parts)
        ):
            raise ValueError(f"Unsafe remote category path: {category!r}")

        filename_parts = PurePosixPath(normalized_filename).parts
        if (
            not normalized_filename
            or normalized_filename.startswith("/")
            or PureWindowsPath(normalized_filename).drive
            or len(filename_parts) != 1
            or filename_parts[0] in {"", ".", ".."}
        ):
            raise ValueError(f"Unsafe remote filename: {filename!r}")

        base_dir = self.base_dir.resolve()
        target_path = base_dir.joinpath(*category_parts, filename_parts[0]).resolve()
        try:
            target_path.relative_to(base_dir)
        except ValueError as exc:
            raise ValueError(
                f"Remote image path escapes the local directory: {target_path}"
            ) from exc

        target_path.parent.mkdir(parents=True, exist_ok=True)
        return target_path
