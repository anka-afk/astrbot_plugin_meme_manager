"""按目标持久保存同步基线，以便识别冲突。"""

import json
import threading
import time
from pathlib import Path

from astrbot.api import logger

from .file_handler import file_fingerprint, normalize_relative_path, write_json_atomic


class UploadTracker:
    """记录已确认的传输；旧版仅有文件名的记录不能作为依据。"""

    def __init__(self, tracker_file: Path):
        self.tracker_file = Path(tracker_file)
        self.uploaded_files: dict[str, dict] = {}
        self._lock = threading.RLock()
        self.load()

    def load(self) -> None:
        """重新加载完整快照，包含工作进程写入的内容。"""
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
                logger.warning("无法加载同步基线，需要重新比对现有文件")
                self.uploaded_files = {}

    def save(self) -> None:
        """原子保存快照，并向上传播持久化失败。"""
        with self._lock:
            write_json_atomic(self.tracker_file, self.uploaded_files)

    def is_uploaded(self, file_path: Path, category: str = "") -> bool:
        """检查当前内容是否与已确认的传输一致。

        Args:
            file_path: 当前本地图片。
            category: 可跨平台使用的分类路径。

        Returns:
            已保存的 SHA-256 是否与当前内容一致。
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
        """在确认上传或校验下载后保存基线。

        Args:
            file_path: 已传输的本地图片。
            category: 相对分类。
            remote_url: 公开 URL（如有）。
            remote_info: 已确认的远端 ID 与版本令牌。
            fingerprint: 已传输字节的指纹。
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
        """返回已确认的文件基线数量。"""
        return len(self.uploaded_files)

    def get_uploaded_files(self) -> dict[str, dict]:
        """返回供状态命令使用的快照。"""
        with self._lock:
            return {key: dict(value) for key, value in self.uploaded_files.items()}

    def remove_record(self, file_path: Path, category: str = "") -> None:
        """从基线中移除已删除图片。

        Args:
            file_path: 已删除的本地图片。
            category: 相对分类。
        """
        relative = normalize_relative_path(
            f"{category}/{file_path.name}" if category else file_path.name
        )
        with self._lock:
            self.uploaded_files.pop(relative, None)
            self.save()

    def clear_record(self) -> None:
        """清空基线，不删除任何本地或远端图片。"""
        with self._lock:
            self.uploaded_files = {}
            self.save()
