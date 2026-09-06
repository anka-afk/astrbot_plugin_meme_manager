"""Durable per-destination baselines for conflict-aware synchronization."""

import json
import logging
import threading
import time
from pathlib import Path

from .file_handler import file_fingerprint, normalize_relative_path, write_json_atomic

logger = logging.getLogger(__name__)


class UploadTracker:
    """Remember confirmed transfers; legacy filename-only records are not proof."""

    def __init__(self, tracker_file: Path):
        self.tracker_file = Path(tracker_file)
        self.uploaded_files: dict[str, dict] = {}
        self._lock = threading.RLock()
        self.load()

    def load(self) -> None:
        """Reload a complete snapshot, including writes made by a worker."""
        with self._lock:
            try:
                data = json.loads(self.tracker_file.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or any(
                    not isinstance(value, dict) for value in data.values()
                ):
                    raise ValueError("Invalid sync baseline format")
                self.uploaded_files = {
                    normalize_relative_path(key): value for key, value in data.items()
                }
            except FileNotFoundError:
                self.uploaded_files = {}
            except (ValueError, OSError):
                logger.warning(
                    "Could not load sync baseline; existing files require comparison"
                )
                self.uploaded_files = {}

    def save(self) -> None:
        """Atomically persist the snapshot, propagating persistence failures."""
        with self._lock:
            write_json_atomic(self.tracker_file, self.uploaded_files)

    def is_uploaded(self, file_path: Path, category: str = "") -> bool:
        """Check that current content matches a confirmed transfer.

        Args:
            file_path: Current local image.
            category: Portable category path.

        Returns:
            Whether the saved SHA-256 matches the current content.
        """
        relative = normalize_relative_path(
            f"{category}/{file_path.name}" if category else file_path.name
        )
        record = self.uploaded_files.get(relative, {})
        return bool(
            record.get("sha256")
            and file_path.is_file()
            and record["sha256"] == file_fingerprint(file_path)["sha256"]
        )

    def mark_uploaded(
        self,
        file_path: Path,
        category: str = "",
        remote_url: str = "",
        *,
        remote_info: dict | None = None,
        fingerprint: dict | None = None,
    ) -> None:
        """Save a baseline after a confirmed upload or validated download.

        Args:
            file_path: Transferred local image.
            category: Relative category.
            remote_url: Public URL, when available.
            remote_info: Confirmed remote ID and version tokens.
            fingerprint: Fingerprint of the transferred bytes.
        """
        relative = normalize_relative_path(
            f"{category}/{file_path.name}" if category else file_path.name
        )
        fingerprint = fingerprint or file_fingerprint(file_path)
        remote_info = remote_info or {}
        with self._lock:
            self.uploaded_files[relative] = {
                "filename": file_path.name,
                "category": category,
                "remote_url": remote_url,
                "upload_time": time.time(),
                "file_size": fingerprint["size"],
                "sha256": fingerprint["sha256"],
                "remote_id": remote_info.get("id", ""),
                "etag": remote_info.get("etag", ""),
                "modified": remote_info.get("modified", ""),
            }
            self.save()

    def get_uploaded_count(self) -> int:
        """Return the number of confirmed file baselines."""
        return len(self.uploaded_files)

    def get_uploaded_files(self) -> dict[str, dict]:
        """Return a snapshot for status commands."""
        with self._lock:
            return {key: dict(value) for key, value in self.uploaded_files.items()}

    def remove_record(self, file_path: Path, category: str = "") -> None:
        """Remove a deleted image from the baseline.

        Args:
            file_path: Deleted local image.
            category: Relative category.
        """
        relative = normalize_relative_path(
            f"{category}/{file_path.name}" if category else file_path.name
        )
        with self._lock:
            self.uploaded_files.pop(relative, None)
            self.save()

    def clear_record(self) -> None:
        """Clear baselines without deleting any local or remote images."""
        with self._lock:
            self.uploaded_files = {}
            self.save()
