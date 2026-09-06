"""Process lifecycle and durable progress for image synchronization."""

import asyncio
import hashlib
import json
import logging
import multiprocessing
import os
import threading
import uuid
from pathlib import Path

from .core.file_handler import write_json_atomic
from .core.sync_manager import SYNC_TASKS, SyncManager
from .core.upload_tracker import UploadTracker
from .providers import CloudflareR2Provider, StarDotsProvider, WebDAVProvider

logger = logging.getLogger(__name__)


class ImageSync:
    """Bind one local image library to one explicitly selected remote destination."""

    def __init__(
        self, config: dict, local_dir: str | Path, provider_type: str = "stardots"
    ):
        self.config = dict(config)
        self.local_dir = Path(local_dir).resolve()
        self.provider_type = provider_type
        providers = {
            "stardots": StarDotsProvider,
            "cloudflare_r2": CloudflareR2Provider,
            "webdav": WebDAVProvider,
        }
        if provider_type not in providers:
            raise ValueError(f"Unsupported image host provider: {provider_type}")
        self.provider = providers[provider_type](
            {**config, "local_dir": str(self.local_dir)}
        )
        if provider_type == "cloudflare_r2":
            identity = [
                provider_type,
                config.get("account_id"),
                config.get("bucket_name"),
                str(config.get("prefix") or "memes").strip("/"),
            ]
        elif provider_type == "webdav":
            identity = [
                provider_type,
                str(config.get("url") or "").rstrip("/"),
                config.get("username"),
                str(config.get("base_path", "memes")).strip("/"),
            ]
        else:
            identity = [provider_type, config.get("key"), config.get("space")]
        self.scope = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False).encode()
        ).hexdigest()[:24]
        self._state_dir = self.local_dir / ".sync-state"
        if (
            self._state_dir.is_symlink()
            or getattr(self._state_dir, "is_junction", lambda: False)()
        ):
            self.provider.close()
            raise ValueError("Sync state directory cannot be a filesystem link")
        self.upload_tracker = UploadTracker(self._state_dir / f"{self.scope}.json")
        self.sync_manager = SyncManager(
            self.provider, self.local_dir, self.upload_tracker
        )
        self.sync_process = None
        self._sync_task = None
        self._process_lock = threading.Lock()
        self._cancel_event = None
        self._progress_path = self._state_dir / f"{self.scope}.task.json"
        self._task_id = ""
        self._last_progress: dict = {}

    def check_status(self) -> dict:
        """Return a complete content comparison for the current destination.

        Returns:
            Planned incremental transfers, conflicts and mirror candidates.
        """
        return self.sync_manager.check_sync_status()

    async def start_sync(self, task: str) -> bool:
        """Start a guarded worker and await the captured process outside the event loop.

        Args:
            task: Sync operation.

        Returns:
            Whether the worker completed successfully.

        Raises:
            RuntimeError: Another operation is already running.
            asyncio.CancelledError: The caller cancelled the task.
        """
        process = self._start_sync_process(task)
        waiter = asyncio.create_task(asyncio.to_thread(process.join))
        self._sync_task = waiter
        try:
            await asyncio.shield(waiter)
            self.upload_tracker.load()
            return process.exitcode == 0
        except asyncio.CancelledError:
            await asyncio.to_thread(self.stop_sync, process)
            raise
        finally:
            if self._sync_task is waiter:
                self._sync_task = None

    def stop_sync(self, expected_process=None) -> None:
        """Request cooperative cancellation, then reap an unresponsive worker.

        Args:
            expected_process: Optional captured worker; an old waiter must not
                cancel a newer task that has already started.
        """
        with self._process_lock:
            process = self.sync_process
            if expected_process is not None and process is not expected_process:
                return
            if self._cancel_event:
                self._cancel_event.set()
            if process is not None and process.is_alive():
                process.join(timeout=2)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=2)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=2)
            if process is not None and not process.is_alive():
                self.get_task_status()
                self.sync_process = None

    def upload_to_remote(self) -> multiprocessing.Process:
        """Start an incremental upload task.

        Returns:
            The guarded worker process.
        """
        return self._start_sync_process("upload")

    def download_to_local(self) -> multiprocessing.Process:
        """Start an incremental download task.

        Returns:
            The guarded worker process.
        """
        return self._start_sync_process("download")

    def sync_all(self) -> bool:
        """Merge nonconflicting changes using one inventory and one worker.

        Returns:
            Whether the operation succeeded.
        """
        process = self._start_sync_process("sync_all")
        process.join()
        self.upload_tracker.load()
        return process.exitcode == 0

    def get_remote_files(self) -> list[dict]:
        """Return the complete remote image inventory."""
        return self.provider.get_image_list()

    def delete_remote_file(self, image_id: str) -> bool:
        """Delete an opaque remote ID when no sync worker is running.

        Args:
            image_id: Provider ID returned by the remote inventory.

        Returns:
            Whether deletion succeeded.
        """
        with self._process_lock:
            if self.sync_process and self.sync_process.is_alive():
                raise RuntimeError("已有同步任务正在运行，请等待完成后再试")
            return self.provider.delete_image(image_id)

    def _start_sync_process(self, task: str) -> multiprocessing.Process:
        """Serialize all worker entry points, including WebUI and chat commands.

        Args:
            task: Supported sync operation.

        Returns:
            Started worker process.

        Raises:
            ValueError: Unsupported operation.
            RuntimeError: A task is already running.
        """
        if task not in SYNC_TASKS:
            raise ValueError(f"Unsupported sync task: {task}")
        with self._process_lock:
            if self.sync_process and self.sync_process.is_alive():
                raise RuntimeError("已有同步任务正在运行，请等待完成后再试")
            if self.sync_process is not None:
                self.sync_process.join(timeout=0)
            self._task_id = uuid.uuid4().hex
            self._last_progress = {
                "task_id": self._task_id,
                "task": task,
                "phase": "starting",
                "total": 0,
                "processed": 0,
                "succeeded": 0,
                "failed": 0,
                "conflicts": 0,
                "errors": [],
                "current_file": "",
            }
            write_json_atomic(self._progress_path, self._last_progress)
            context = multiprocessing.get_context("spawn")
            self._cancel_event = context.Event()
            process = context.Process(
                target=run_sync_process,
                args=(
                    self.config,
                    str(self.local_dir),
                    task,
                    self.provider_type,
                    str(self._progress_path),
                    self._task_id,
                    self._cancel_event,
                ),
            )
            process.start()
            self.sync_process = process
            return process

    def get_task_status(self) -> dict:
        """Read bounded progress and derive completion from the real process.

        Returns:
            Progress compatible with the WebUI task-status and SSE endpoints.
        """
        if self._task_id and not (
            self.sync_process is None and self._last_progress.get("completed")
        ):
            try:
                data = json.loads(self._progress_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("task_id") == self._task_id:
                    self._last_progress = data
            except (ValueError, OSError):
                pass
        progress = dict(self._last_progress)
        process = self.sync_process
        running = process is not None and process.is_alive()
        exit_code = (
            process.exitcode if process is not None else progress.get("exit_code")
        )
        progress.update(
            available=True,
            running=running,
            completed=not running,
            pid=process.pid if process is not None else progress.get("pid"),
            exit_code=exit_code,
            success=None if running or exit_code is None else exit_code == 0,
        )
        if not running and process is not None:
            process.join(timeout=0)
            if exit_code != 0 and progress.get("phase") not in {"failed", "cancelled"}:
                progress.update(
                    phase="failed", message="Sync worker exited before completion"
                )
            if (
                exit_code != 0
                and self._cancel_event is not None
                and self._cancel_event.is_set()
            ):
                progress.update(phase="cancelled", message="Sync cancelled")
            self._last_progress = dict(progress)
        progress.setdefault(
            "message", "同步任务运行中" if running else "当前没有同步任务"
        )
        return progress

    def close(self) -> None:
        """Stop workers and release the provider's connection pools."""
        self.stop_sync()
        self.provider.close()


def run_sync_process(
    config: dict,
    local_dir: str,
    task: str,
    provider_type: str,
    progress_path: str = "",
    task_id: str = "",
    cancel_event=None,
):
    """Run one locked sync job and persist failures even when planning fails.

    Args:
        config: Provider configuration.
        local_dir: Local image library.
        task: Sync operation.
        provider_type: Explicit provider selection.
        progress_path: Durable task-progress file.
        task_id: Parent-generated task identifier.
        cancel_event: Cooperative cancellation signal.
    """
    sync = None
    lock_stream = None
    success = False
    try:
        if task not in SYNC_TASKS:
            raise ValueError(f"Unsupported sync task: {task}")
        sync = ImageSync(config, local_dir, provider_type)
        lock_path = sync._state_dir / "worker.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_stream = lock_path.open("a+b")
        if lock_path.stat().st_size == 0:
            lock_stream.write(b"0")
            lock_stream.flush()
        lock_stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        if progress_path:

            def publish(progress: dict) -> None:
                """Write worker progress for the parent's active task.

                Args:
                    progress: Current sync execution counters and errors.
                """
                write_json_atomic(
                    Path(progress_path), {**progress, "task_id": task_id, "task": task}
                )

            sync.sync_manager.progress_callback = publish
        if cancel_event is not None:
            sync.sync_manager.cancel_requested = cancel_event.is_set
        success = sync.sync_manager.run(task)
    except Exception as exc:
        logger.warning("Image sync worker failed: %s", exc)
        if progress_path:
            write_json_atomic(
                Path(progress_path),
                {
                    "task_id": task_id,
                    "task": task,
                    "phase": "failed",
                    "success": False,
                    "message": str(exc),
                },
            )
    finally:
        if lock_stream is not None:
            lock_stream.close()
        if sync is not None:
            sync.provider.close()
    raise SystemExit(0 if success else 1)
