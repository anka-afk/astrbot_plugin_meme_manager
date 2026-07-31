import asyncio
import threading
import time
import unittest
from pathlib import Path

from backend.semantic_task import SemanticTaskManager
from image_host.img_sync import ImageSync


class EventLoopBlockingTests(unittest.IsolatedAsyncioTestCase):
    async def test_pack_mutation_runs_outside_event_loop_thread(self):
        manager = object.__new__(SemanticTaskManager)
        manager._lock = lambda _pack_id: asyncio.Lock()
        manager.assert_pack_mutation_allowed = lambda _pack_id, _operation: None
        event_loop_thread = threading.get_ident()
        mutation_thread = None
        loop_progressed = False

        def mutation():
            nonlocal mutation_thread
            mutation_thread = threading.get_ident()
            time.sleep(0.05)
            return "完成"

        async def heartbeat():
            nonlocal loop_progressed
            await asyncio.sleep(0.01)
            loop_progressed = True

        result, _ = await asyncio.gather(
            manager.run_locked_pack_mutation("pack-a", "测试变更", mutation),
            heartbeat(),
        )

        self.assertEqual(result, "完成")
        self.assertTrue(loop_progressed)
        self.assertNotEqual(mutation_thread, event_loop_thread)

    async def test_cancelled_pack_mutation_keeps_lock_until_thread_finishes(self):
        manager = object.__new__(SemanticTaskManager)
        lock = asyncio.Lock()
        manager._lock = lambda _pack_id: lock
        manager.assert_pack_mutation_allowed = lambda _pack_id, _operation: None
        started = threading.Event()
        release = threading.Event()

        def mutation():
            started.set()
            release.wait(timeout=1)

        task = asyncio.create_task(
            manager.run_locked_pack_mutation("pack-a", "测试取消", mutation)
        )
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)

        self.assertTrue(lock.locked())
        self.assertFalse(task.done())

        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(lock.locked())

    async def test_sync_status_check_runs_outside_event_loop_thread(self):
        client = object.__new__(ImageSync)
        client.sync_process = None
        client._sync_task = None
        event_loop_thread = threading.get_ident()
        status_thread = None
        loop_progressed = False

        def check_status():
            nonlocal status_thread
            status_thread = threading.get_ident()
            time.sleep(0.05)
            return {"to_upload": [], "to_download": []}

        client.check_status = check_status

        async def heartbeat():
            nonlocal loop_progressed
            await asyncio.sleep(0.01)
            loop_progressed = True

        result, _ = await asyncio.gather(client.start_sync("upload"), heartbeat())

        self.assertTrue(result)
        self.assertTrue(loop_progressed)
        self.assertNotEqual(status_thread, event_loop_thread)


class CommandMutationRegressionTests(unittest.TestCase):
    def test_chat_commands_use_locked_pack_mutations(self):
        source = (
            Path(__file__).parents[1] / "mixins" / "commands.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn("_assert_default_pack_mutation_allowed", source)
        self.assertGreaterEqual(source.count("self._run_default_pack_mutation("), 4)

    def test_clear_commands_invalidate_semantic_metadata(self):
        source = (
            Path(__file__).parents[1] / "mixins" / "commands.py"
        ).read_text(encoding="utf-8")

        self.assertEqual(source.count("self._invalidate_default_pack_semantics()"), 2)


class FullSyncSequenceTests(unittest.TestCase):
    def test_sync_all_uses_one_sequential_worker_and_returns_success(self):
        class FakeProcess:
            exitcode = 0

            def __init__(self):
                self.joined = False

            def join(self):
                self.joined = True

        client = object.__new__(ImageSync)
        process = FakeProcess()
        started_tasks = []

        def start_sync_process(task):
            started_tasks.append(task)
            return process

        client._start_sync_process = start_sync_process

        self.assertTrue(client.sync_all())
        self.assertEqual(started_tasks, ["sync_all"])
        self.assertTrue(process.joined)

    def test_sync_all_returns_failure_from_worker_exit_code(self):
        class FakeProcess:
            exitcode = 1

            @staticmethod
            def join():
                return None

        client = object.__new__(ImageSync)
        client._start_sync_process = lambda task: FakeProcess()

        self.assertFalse(client.sync_all())


if __name__ == "__main__":
    unittest.main()
