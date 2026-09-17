import asyncio
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot_plugin_meme_manager.backend import auto_collect


class SnapshotLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for constant, name in (
            ("AUTO_COLLECT_TEMP_DIR", "queue"),
            ("AUTO_COLLECT_STATE_PATH", "state.json"),
            ("AUTO_COLLECT_INBOX_METADATA_PATH", "inbox.json"),
        ):
            self.stack.enter_context(
                patch.object(auto_collect, constant, self.root / name)
            )
        self.source = self.root / "source.png"
        self.source.write_bytes(b"snapshot-content")
        plugin = SimpleNamespace(
            _resolve_runtime_pack_context=lambda **_: {
                "pack_id": "pack-a",
                "category_mapping": {"happy": "positive"},
            }
        )
        self.manager = auto_collect.AutoCollectManager(
            plugin, {"enabled": True, "cooldown_seconds": 0}
        )
        self.manager._ready = True
        self.addAsyncCleanup(self.manager.close)
        self.image = auto_collect.Image(file="https://example.com/image.png")
        self.event = SimpleNamespace(
            is_private_chat=lambda: False,
            get_group_id=lambda: "group",
            message_obj=SimpleNamespace(message=[self.image]),
        )

    async def test_remote_submit_returns_before_download_and_close_cancels(self):
        started = asyncio.Event()

        async def download():
            started.set()
            await asyncio.Event().wait()

        with patch.object(
            auto_collect.Image, "convert_to_file_path", side_effect=download
        ):
            self.assertTrue(
                await asyncio.wait_for(self.manager.submit(self.event), 0.5)
            )
            await asyncio.wait_for(started.wait(), 0.5)
            self.assertEqual(len(self.manager._snapshot_tasks), 1)
            await self.manager.close()
        self.assertFalse(self.manager._snapshot_tasks)
        self.assertTrue(self.manager.queue.empty())

    async def test_remote_pending_tasks_are_bounded(self):
        async def download():
            await asyncio.Event().wait()

        with (
            patch.object(auto_collect, "QUEUE_SIZE", 2),
            patch.object(
                auto_collect.Image, "convert_to_file_path", side_effect=download
            ),
        ):
            self.assertTrue(await self.manager.submit(self.event))
            self.assertTrue(await self.manager.submit(self.event))
            self.assertFalse(await self.manager.submit(self.event))
            self.assertEqual(len(self.manager._snapshot_tasks), 2)
            await self.manager.close()

    async def test_concurrent_identical_candidates_reserve_only_one_digest(self):
        barrier = threading.Barrier(2)

        def lookup(*_):
            barrier.wait(timeout=2)
            return False

        with (
            patch.object(
                auto_collect.Image,
                "convert_to_file_path",
                new=AsyncMock(return_value=str(self.source)),
            ),
            patch.object(self.manager, "_pack_contains_digest", side_effect=lookup),
        ):
            results = await asyncio.gather(
                self.manager._snapshot_and_enqueue(
                    self.image, "pack-a", {}, "group", "a"
                ),
                self.manager._snapshot_and_enqueue(
                    self.image, "pack-a", {}, "group", "b"
                ),
            )
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(self.manager.queue.qsize(), 1)
        self.assertEqual(len(self.manager._inflight), 1)
        self.assertEqual(len(list((self.root / "queue").iterdir())), 1)
        await self.manager.close()
        self.assertFalse(self.manager._inflight)
        self.assertEqual(list((self.root / "queue").iterdir()), [])

    async def test_cancelled_copy_finishes_before_snapshot_cleanup(self):
        started = threading.Event()
        release = threading.Event()

        def copy(source, destination):
            started.set()
            if not release.wait(timeout=2):
                raise TimeoutError("Test copy was not released")
            destination.write_bytes(source.read_bytes())

        with (
            patch.object(
                auto_collect.Image,
                "convert_to_file_path",
                new=AsyncMock(return_value=str(self.source)),
            ),
            patch.object(auto_collect.shutil, "copyfile", side_effect=copy),
        ):
            task = asyncio.create_task(
                self.manager._snapshot_and_enqueue(
                    self.image, "pack-a", {}, "group", "a"
                )
            )
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(list((self.root / "queue").iterdir()), [])
        self.assertTrue(self.manager.queue.empty())
