"""Portable paths, fingerprints and atomic writes shared by storage adapters."""

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path, PureWindowsPath

from PIL import Image

SUPPORTED_FORMATS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
MAX_IMAGE_BYTES = 1024 * 1024 * 1024


def normalize_relative_path(value: str) -> str:
    """Validate a path without silently changing its meaning.

    Args:
        value: Relative image path using either platform's separators.

    Returns:
        A portable POSIX path.

    Raises:
        ValueError: The path escapes its namespace or aliases a Windows filename.
    """
    value = str(value).replace("\\", "/")
    parts = value.split("/")
    if (
        not value
        or PureWindowsPath(value).drive
        or any(
            part.casefold() in {"", ".", "..", ".sync-state"}
            or part.rstrip(" .") != part
            or any(ord(char) < 32 or char in '<>:"|?*' for char in part)
            or PureWindowsPath(part).is_reserved()
            for part in parts
        )
    ):
        raise ValueError(f"Unsafe image path: {value!r}")
    return "/".join(parts)


def file_fingerprint(file_path: Path) -> dict:
    """Hash a stable file using bounded memory.

    Args:
        file_path: File to read.

    Returns:
        Its size, SHA-256 digest and nanosecond modification timestamp.

    Raises:
        OSError: The file changed during reading or could not be read.
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
    """Persist state without exposing partially written JSON.

    Args:
        target: State file to replace.
        data: Serializable state.

    Raises:
        OSError: The new state could not be durably written.
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
    """Validate a streamed image before atomically publishing it.

    Args:
        chunks: Downloaded byte chunks.
        save_path: Final destination.
        expected_size: Exact byte length, if supplied by the provider.
        expected_sha256: Content checksum, if supplied by the provider.

    Raises:
        ValueError: The transfer is truncated, too large or not an image.
        OSError: Reading or saving fails; the existing target remains intact.
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
    """Scan complete local inventories and resolve safe image destinations."""

    SUPPORTED_FORMATS = SUPPORTED_FORMATS

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def scan_local_images(self) -> list[dict]:
        """Return a complete, deterministic inventory with content fingerprints.

        Returns:
            Local images and their paths, categories, sizes and checksums.

        Raises:
            OSError: Any directory or image cannot be read.
            ValueError: A symlink or ambiguous path makes mirroring unsafe.
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
        """Resolve a remote name inside the local root.

        Args:
            category: Relative category, optionally nested.
            filename: Single filename.
            create_parent: Whether to create the destination directory.

        Returns:
            A safe absolute destination.

        Raises:
            ValueError: A name or existing filesystem link escapes the root.
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
