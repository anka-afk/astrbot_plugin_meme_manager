"""Plan and execute conservative image synchronization across storage adapters."""

import logging
import tempfile
from collections.abc import Callable
from pathlib import Path

from PIL import Image

from ..interfaces.image_host import ImageHostInterface
from .file_handler import FileHandler, file_fingerprint, normalize_relative_path
from .upload_tracker import UploadTracker

logger = logging.getLogger(__name__)
SYNC_TASKS = {
    "upload",
    "download",
    "sync_all",
    "overwrite_to_remote",
    "overwrite_from_remote",
}


class SyncManager:
    """Separate inventory comparison, transfer execution and mirror cleanup."""

    def __init__(
        self,
        image_host: ImageHostInterface,
        local_dir: Path,
        upload_tracker: UploadTracker | None = None,
    ):
        self.image_host = image_host
        self.file_handler = FileHandler(local_dir)
        self.upload_tracker = upload_tracker
        self.progress_callback: Callable[[dict], None] | None = None
        self.cancel_requested: Callable[[], bool] = lambda: False
        self.progress: dict = {}

    @staticmethod
    def _extract_remote_size(image_info: dict) -> int | None:
        """Read an optional byte count without treating missing sizes as zero.

        Args:
            image_info: Provider metadata.

        Returns:
            A nonnegative size, or None when the provider does not expose one.
        """
        for key in ("size", "file_size", "fileSize", "bytes", "length"):
            value = image_info.get(key)
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and value >= 0
            ):
                return int(value)
            if isinstance(value, str) and value.isdigit():
                return int(value)
        return None

    def check_sync_status(self) -> dict:
        """Build one complete comparison without modifying either image library.

        Returns:
            Incremental transfers, conflicts and directional mirror candidates.

        Raises:
            ValueError: A remote path is unsafe or maps ambiguously to a local file.
            Exception: Local scanning or any remote listing page fails.
        """
        local_images = self.file_handler.scan_local_images()
        remote_images = []
        remote_by_path = {}
        case_paths = {}
        for source in self.image_host.get_image_list():
            image = dict(source)
            category = str(image.get("category") or "")
            filename = str(image.get("filename") or "")
            relative = normalize_relative_path(
                image.get("relative_path")
                or (f"{category}/{filename}" if category else filename)
            )
            if not image.get("id"):
                raise ValueError("Remote image is missing its storage identifier")
            category, _, filename = relative.rpartition("/")
            self.file_handler.get_file_path(category, filename, create_parent=False)
            if Path(filename).suffix.lower() not in FileHandler.SUPPORTED_FORMATS:
                continue
            if relative.casefold() in case_paths:
                raise ValueError(f"Duplicate or ambiguous remote image: {relative}")
            case_paths[relative.casefold()] = relative
            image.update(
                relative_path=relative,
                filename=filename,
                category=category,
                size=self._extract_remote_size(image),
            )
            remote_by_path[relative] = image
            remote_images.append(image)
        remote_images.sort(key=lambda item: item["relative_path"])
        local_by_path = {image["id"]: image for image in local_images}
        for relative in local_by_path:
            if (
                relative.casefold() in case_paths
                and case_paths[relative.casefold()] != relative
            ):
                raise ValueError(
                    f"Local and remote filename casing differs: {relative}"
                )

        if self.upload_tracker:
            self.upload_tracker.load()
        records = self.upload_tracker.uploaded_files if self.upload_tracker else {}
        local_only = [
            image for image in local_images if image["id"] not in remote_by_path
        ]
        remote_only = [
            image
            for image in remote_images
            if image["relative_path"] not in local_by_path
        ]
        to_upload = list(local_only)
        to_download = list(remote_only)
        overwrite_remote = list(local_only)
        overwrite_local = list(remote_only)
        conflicts = []
        history_compared = 0

        for relative in sorted(local_by_path.keys() & remote_by_path.keys()):
            local, remote = local_by_path[relative], remote_by_path[relative]
            record = records.get(relative, {})
            if remote.get("sha256") == local["sha256"]:
                continue
            if record.get("sha256"):
                local_changed = local["sha256"] != record["sha256"]
                remote_changed = bool(
                    (remote.get("sha256") and remote["sha256"] != record["sha256"])
                    or (
                        remote["size"] is not None
                        and remote["size"] != record.get("file_size")
                    )
                    or any(
                        remote.get(key)
                        and record.get(key)
                        and remote[key] != record[key]
                        for key in ("etag", "modified")
                    )
                )
                if not local_changed and not remote_changed:
                    history_compared += 1
                    continue
                if local_changed and not remote_changed:
                    to_upload.append(local)
                elif remote_changed and not local_changed:
                    to_download.append(remote)
                else:
                    conflicts.append(
                        {"relative_path": relative, "reason": "both_changed"}
                    )
            else:
                conflicts.append({"relative_path": relative, "reason": "unverified"})
            overwrite_remote.append(local)
            overwrite_local.append(remote)

        sizes = [image["size"] for image in remote_images if image["size"] is not None]
        complete = len(sizes) == len(remote_images)
        local_bytes = sum(image["size"] for image in local_images)
        average_size = local_bytes / len(local_images) if local_images else 0
        estimated = (
            sum(
                image["size"]
                if image["size"] is not None
                else local_by_path.get(image["relative_path"], {}).get(
                    "size", average_size
                )
                for image in remote_images
            )
            if local_images or sizes
            else None
        )
        return {
            "to_upload": to_upload,
            "to_download": to_download,
            "to_delete_local": local_only,
            "to_delete_remote": remote_only,
            "to_overwrite_remote": overwrite_remote,
            "to_overwrite_local": overwrite_local,
            "conflicts": conflicts,
            "conflict_count": len(conflicts),
            "is_synced": not (to_upload or to_download or conflicts),
            "local_images": local_images,
            "remote_images": remote_images,
            "remote_image_count": len(remote_images),
            "local_image_count": len(local_images),
            "remote_total_bytes": sum(sizes) if complete else None,
            "remote_total_bytes_estimated": sum(sizes)
            if complete
            else (int(estimated) if estimated is not None else None),
            "remote_size_source": "exact"
            if complete
            else ("local_estimate" if estimated is not None else "unknown"),
            "remote_size_complete": complete,
            "local_total_bytes": local_bytes,
            "history_compared_count": history_compared,
            "remote_exists": getattr(self.image_host, "listing_exists", True),
        }

    def _publish_progress(self, **changes) -> None:
        """Publish a bounded snapshot after each operation.

        Args:
            **changes: Updated task fields.
        """
        self.progress.update(changes)
        if self.progress_callback:
            self.progress_callback(dict(self.progress))

    def _transfer_image(self, direction: str, image: dict, local: dict | None) -> None:
        """Transfer a planned file while protecting edits made after planning.

        Args:
            direction: Upload or download.
            image: Planned source metadata.
            local: Local metadata captured when planning, if the file existed.

        Raises:
            Exception: Content changed, a transfer failed, or state could not be saved.
        """
        category, filename = image.get("category", ""), image["filename"]
        path = self.file_handler.get_file_path(category, filename)
        before = file_fingerprint(path) if path.exists() else None
        if (local is None and before is not None) or (
            local is not None
            and (before is None or before["sha256"] != local["sha256"])
        ):
            raise ValueError("Local image changed after the sync plan was built")
        if direction == "upload":
            with Image.open(path) as content:
                content.verify()
            remote = self.image_host.upload_image(path)
            if not isinstance(remote, dict) or not remote.get("id"):
                raise ValueError("Provider did not confirm an uploaded object")
            after = file_fingerprint(path)
            if after["sha256"] != before["sha256"]:
                raise ValueError("Local image changed during upload")
            if remote.get("sha256") and remote["sha256"] != after["sha256"]:
                raise ValueError("Provider uploaded different image content")
        else:
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=path.parent,
                    prefix=".sync-download-",
                    suffix=".tmp",
                    delete=False,
                ) as stream:
                    temporary = Path(stream.name)
                if not self.image_host.download_image(image, temporary):
                    raise OSError("Provider did not complete the download")
                after = file_fingerprint(temporary)
                if image.get("size") is not None and after["size"] != image["size"]:
                    raise ValueError("Downloaded image size does not match the listing")
                if image.get("sha256") and after["sha256"] != image["sha256"]:
                    raise ValueError(
                        "Downloaded image checksum does not match the listing"
                    )
                with Image.open(temporary) as content:
                    content.verify()
                current = file_fingerprint(path) if path.exists() else None
                if current != before:
                    raise ValueError("Local image changed during download")
                if self.cancel_requested():
                    raise InterruptedError(
                        "Sync cancelled before replacing the local image"
                    )
                temporary.replace(path)
                remote = image
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        if self.upload_tracker:
            self.upload_tracker.mark_uploaded(
                path,
                category,
                remote.get("url", ""),
                remote_info=remote,
                fingerprint=after,
            )

    def run(self, task: str) -> bool:
        """Execute a single plan and delete extras only after successful transfers.

        Args:
            task: Incremental upload, download, union sync, or directional mirror.

        Returns:
            Whether every operation succeeded without unresolved conflicts.

        Raises:
            ValueError: The task is unsupported.
        """
        if task not in SYNC_TASKS:
            raise ValueError(f"Unsupported sync task: {task}")
        self.progress = {
            "phase": "planning",
            "total": 0,
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "conflicts": 0,
            "errors": [],
            "current_file": "",
        }
        self._publish_progress()
        try:
            status = self.check_sync_status()
            if task == "overwrite_from_remote" and not status["remote_exists"]:
                raise ValueError(
                    "Remote directory does not exist; local cleanup was cancelled"
                )
            local_by_path = {image["id"]: image for image in status["local_images"]}
            mirror = task.startswith("overwrite_")
            uploads = (
                status["to_overwrite_remote"]
                if task == "overwrite_to_remote"
                else (status["to_upload"] if task in {"upload", "sync_all"} else [])
            )
            downloads = (
                status["to_overwrite_local"]
                if task == "overwrite_from_remote"
                else (status["to_download"] if task in {"download", "sync_all"} else [])
            )
            deletions = (
                status["to_delete_remote"]
                if task == "overwrite_to_remote"
                else (
                    status["to_delete_local"] if task == "overwrite_from_remote" else []
                )
            )
            conflicts = [] if mirror else status["conflicts"]
            self._publish_progress(
                total=len(uploads) + len(downloads) + len(deletions),
                conflicts=len(conflicts),
            )
            for direction, images in (("upload", uploads), ("download", downloads)):
                for image in images:
                    if self.cancel_requested():
                        raise InterruptedError("Sync cancelled")
                    relative = image.get("relative_path") or image["id"]
                    self._publish_progress(phase=direction, current_file=relative)
                    try:
                        self._transfer_image(
                            direction, image, local_by_path.get(relative)
                        )
                        self.progress["succeeded"] += 1
                    except InterruptedError:
                        raise
                    except Exception as exc:
                        self.progress["failed"] += 1
                        if len(self.progress["errors"]) < 20:
                            self.progress["errors"].append(
                                {
                                    "path": relative,
                                    "operation": direction,
                                    "message": str(exc),
                                }
                            )
                        logger.warning(
                            "Image %s failed for %s: %s", direction, relative, exc
                        )
                    self._publish_progress(processed=self.progress["processed"] + 1)

            if deletions and not self.progress["failed"]:
                if self.cancel_requested():
                    raise InterruptedError("Sync cancelled before cleanup")
                # Re-list before deleting: a changed or incomplete source must never
                # turn into permission to clean the destination.
                self._publish_progress(phase="verifying", current_file="")
                fresh = self.check_sync_status()
                if task == "overwrite_from_remote" and not fresh["remote_exists"]:
                    raise ValueError(
                        "Remote directory disappeared; local cleanup was cancelled"
                    )
                if task == "overwrite_to_remote":
                    before_source = {
                        item["id"]: item["sha256"] for item in status["local_images"]
                    }
                    after_source = {
                        item["id"]: item["sha256"] for item in fresh["local_images"]
                    }
                else:
                    before_source = {
                        item["relative_path"]: {
                            key: item.get(key)
                            for key in ("id", "size", "etag", "modified", "sha256")
                        }
                        for item in status["remote_images"]
                    }
                    after_source = {
                        item["relative_path"]: {
                            key: item.get(key)
                            for key in ("id", "size", "etag", "modified", "sha256")
                        }
                        for item in fresh["remote_images"]
                    }
                if before_source != after_source:
                    raise ValueError(
                        "Source changed during sync; cleanup was cancelled"
                    )
                if task == "overwrite_to_remote":
                    fresh_remote = {
                        item["id"]: item for item in fresh["to_delete_remote"]
                    }
                    for item in deletions:
                        current = fresh_remote.get(item["id"])
                        if current is None or any(
                            item.get(key) != current.get(key)
                            for key in ("size", "etag", "modified", "sha256")
                        ):
                            raise ValueError(
                                "Remote deletion candidate changed; cleanup was cancelled"
                            )
                for image in deletions:
                    if self.cancel_requested():
                        raise InterruptedError("Sync cancelled during cleanup")
                    relative = image.get("relative_path") or image["id"]
                    self._publish_progress(phase="delete", current_file=relative)
                    try:
                        if task == "overwrite_to_remote":
                            if not self.image_host.delete_image(image["id"]):
                                raise OSError("Provider did not confirm deletion")
                        else:
                            path = self.file_handler.get_file_path(
                                image["category"],
                                image["filename"],
                                create_parent=False,
                            )
                            if file_fingerprint(path)["sha256"] != image["sha256"]:
                                raise ValueError("Local deletion candidate changed")
                            path.unlink()
                            if self.upload_tracker:
                                self.upload_tracker.remove_record(
                                    path, image["category"]
                                )
                        self.progress["succeeded"] += 1
                    except Exception as exc:
                        self.progress["failed"] += 1
                        if len(self.progress["errors"]) < 20:
                            self.progress["errors"].append(
                                {
                                    "path": relative,
                                    "operation": "delete",
                                    "message": str(exc),
                                }
                            )
                        logger.warning("Image cleanup failed for %s: %s", relative, exc)
                    self._publish_progress(processed=self.progress["processed"] + 1)
            success = not (self.progress["failed"] or conflicts)
            self._publish_progress(
                phase="completed" if success else "failed",
                success=success,
                current_file="",
                message=(
                    "Sync completed"
                    if success
                    else "Resolve same-name conflicts using a directional overwrite"
                    if conflicts
                    else "Transfer failed; cleanup was skipped"
                ),
            )
            return success
        except Exception as exc:
            self._publish_progress(
                phase="cancelled" if isinstance(exc, InterruptedError) else "failed",
                success=False,
                message=str(exc),
            )
            logger.warning("Image sync stopped: %s", exc)
            return False

    def sync_to_remote(self) -> bool:
        """Upload new and locally changed images; retain conflicts and remote extras."""
        return self.run("upload")

    def sync_from_remote(self) -> bool:
        """Download new and remotely changed images; retain conflicts and local extras."""
        return self.run("download")

    def overwrite_to_remote(self) -> bool:
        """Resolve differences in favor of local images, then remove remote extras."""
        return self.run("overwrite_to_remote")

    def overwrite_from_remote(self) -> bool:
        """Resolve differences in favor of remote images, then remove local extras."""
        return self.run("overwrite_from_remote")
