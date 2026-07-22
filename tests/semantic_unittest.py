import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import faiss
from backend.semantic_caption import (
    build_caption_prompt,
    generate_caption,
    prepare_visual_inputs,
)
from backend.semantic_index import EmbeddingAdapter, build_index, index_is_ready
from backend.semantic_models import (
    PROMPT_VERSION,
    SemanticImage,
    normalize_vector,
    parse_caption_result,
)
from backend.semantic_query import (
    candidate_records,
    remember_candidates,
    search_memes,
    validate_selected_id,
)
from backend.semantic_storage import (
    get_image_semantic_detail,
    get_pack_semantic_summary,
    load_metadata,
    reconcile_metadata,
    reset_local_embedding_state,
    safe_relative_path,
    save_metadata,
    semantic_metadata_is_complete,
)
from backend.semantic_task import SemanticTaskManager
from image_host.img_sync import ImageSync
from PIL import Image


class FakeEmbedding:
    def __init__(self):
        self.single_calls = 0
        self.batch_calls = 0

    async def get_embeddings(self, texts):
        self.batch_calls += 1
        return [[1.0, 0.0] if "心虚" in text else [0.0, 1.0] for text in texts]

    async def get_embedding(self, text):
        self.single_calls += 1
        return [1.0, 0.0] if "心虚" in text else [0.0, 1.0]

    def get_dim(self):
        return 2

    def get_model(self):
        return "fake-semantic-v1"

    def meta(self):
        return type("ProviderMeta", (), {"id": "fake-embedding"})()


class WrongDimensionEmbedding(FakeEmbedding):
    def get_dim(self):
        return 3

    async def get_embedding(self, text):
        self.single_calls += 1
        return [1.0, 0.0]


class FakeEvent:
    def __init__(self):
        self.extra = {}

    def get_extra(self, key):
        return self.extra.get(key)

    def set_extra(self, key, value):
        self.extra[key] = value


class FakeContext:
    def __init__(self, provider):
        self.provider = provider

    def get_provider_by_id(self, provider_id):
        return (
            self.provider if provider_id in {"fake-embedding", "fake-vision"} else None
        )

    def get_all_embedding_providers(self):
        return [self.provider]

    async def llm_generate(self, **kwargs):
        return type(
            "VisionResponse",
            (),
            {
                "completion_text": (
                    '{"caption":"我有点心虚想装傻",'
                    '"tags":["心虚","装傻"],"visible_text":""}'
                )
            },
        )()


class BlockingVisionContext(FakeContext):
    def __init__(self, provider, expected_started):
        super().__init__(provider)
        self.expected_started = expected_started
        self.started_calls = 0
        self.expected_calls_started = asyncio.Event()
        self.release_calls = asyncio.Event()

    async def llm_generate(self, **kwargs):
        self.started_calls += 1
        if self.started_calls >= self.expected_started:
            self.expected_calls_started.set()
        await self.release_calls.wait()
        return await super().llm_generate(**kwargs)


class BlockingEmbedding(FakeEmbedding):
    def __init__(self):
        super().__init__()
        self.batch_started = asyncio.Event()
        self.release_batch = asyncio.Event()

    async def get_embeddings(self, texts):
        self.batch_calls += 1
        self.batch_started.set()
        await self.release_batch.wait()
        return [[1.0, 0.0] for _ in texts]


class RetryVisionContext:
    def __init__(self):
        self.requests = []

    async def llm_generate(self, **kwargs):
        self.requests.append(kwargs)
        if len(self.requests) == 1:
            return type(
                "VisionResponse",
                (),
                {
                    "completion_text": (
                        "我先确认角色。tool request web_search with query is 动漫角色"
                    ),
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "total_tokens": 120,
                    },
                },
            )()
        return type(
            "VisionResponse",
            (),
            {
                "completion_text": (
                    '{"caption":"惊慌地摆手求饶","tags":["惊慌","求饶",'
                    '"摆手","认怂","聊天反应","拒绝"],"visible_text":""}'
                ),
                "usage": {
                    "prompt_tokens": 40,
                    "completion_tokens": 15,
                    "total_tokens": 55,
                },
            },
        )()


class SemanticMvpTest(unittest.TestCase):
    def test_homepage_semantic_summary_and_duplicate_image_detail(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first_category = root / "memes" / "a"
            second_category = root / "memes" / "b"
            first_category.mkdir(parents=True)
            second_category.mkdir(parents=True)
            first_image = first_category / "one.png"
            duplicate_image = second_category / "copy.png"
            first_image.write_bytes(b"same-image")
            duplicate_image.write_bytes(b"same-image")

            metadata = reconcile_metadata(root)
            semantic_item = next(iter(metadata["images"].values()))
            semantic_item.update(
                {
                    "caption": "无奈地摊手表示没办法",
                    "tags": ["无奈", "摊手"],
                    "visible_text": "",
                    "caption_status": "done",
                    "embedding_status": "done",
                }
            )
            save_metadata(root, metadata)

            summary = get_pack_semantic_summary(root)
            self.assertEqual(summary["semantic_status"], "complete")
            self.assertTrue(semantic_metadata_is_complete(root))
            self.assertEqual(summary["semantic_file_total"], 2)
            self.assertEqual(summary["semantic_caption_total"], 1)
            self.assertEqual(summary["semantic_caption_done"], 1)

            duplicate_detail = get_image_semantic_detail(root, duplicate_image)
            self.assertEqual(duplicate_detail["status"], "complete")
            self.assertEqual(duplicate_detail["caption"], "无奈地摊手表示没办法")
            self.assertEqual(duplicate_detail["tags"], ["无奈", "摊手"])

            duplicate_image.write_bytes(b"new-content")
            replaced_summary = get_pack_semantic_summary(root)
            self.assertEqual(replaced_summary["semantic_status"], "partial")
            self.assertTrue(replaced_summary["semantic_snapshot_matches"])
            self.assertTrue(replaced_summary["semantic_files_changed"])
            self.assertFalse(semantic_metadata_is_complete(root))
            duplicate_image.write_bytes(b"same-image")
            self.assertTrue(semantic_metadata_is_complete(root))

            first_image.write_bytes(b"replaced-image")
            replaced_summary = get_pack_semantic_summary(root)
            self.assertEqual(replaced_summary["semantic_status"], "partial")
            self.assertTrue(replaced_summary["semantic_snapshot_matches"])

            first_image.write_bytes(b"same-image")
            self.assertEqual(
                get_pack_semantic_summary(root)["semantic_status"], "complete"
            )

            (first_category / "new.jpg").write_bytes(b"new-image")
            changed_summary = get_pack_semantic_summary(root)
            self.assertEqual(changed_summary["semantic_status"], "partial")
            self.assertFalse(changed_summary["semantic_snapshot_matches"])
            self.assertTrue(changed_summary["semantic_files_changed"])

            missing_detail = get_image_semantic_detail(
                root, first_category / "new.jpg"
            )
            self.assertEqual(missing_detail["status"], "none")

    def test_new_image_sync_does_not_stop_existing_task(self):
        class RunningProcess:
            @staticmethod
            def is_alive():
                return True

        async def run():
            sync_client = ImageSync.__new__(ImageSync)
            sync_client.sync_process = RunningProcess()
            stopped = False

            def stop_sync():
                nonlocal stopped
                stopped = True

            sync_client.stop_sync = stop_sync
            with self.assertRaisesRegex(RuntimeError, "已有同步任务"):
                await sync_client.start_sync("download")
            self.assertFalse(stopped)

        asyncio.run(run())

    def test_semantic_config_uses_astrbot_supported_types(self):
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(schema["semantic"]["items"]["min_score"]["type"], "float")
        self.assertEqual(
            schema["semantic"]["items"]["vision_provider_id"]["_special"],
            "select_provider",
        )
        self.assertEqual(
            schema["semantic"]["items"]["embedding_provider_id"]["_special"],
            "select_provider_embedding",
        )
        self.assertNotIn(
            "可选", schema["semantic"]["items"]["embedding_provider_id"]["description"]
        )

    def test_full_task_embeds_once_and_builds_faiss(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                pack = root / "packs" / "demo"
                (pack / "memes" / "a").mkdir(parents=True)
                (pack / "memes" / "a" / "one.png").write_bytes(b"one")
                provider = FakeEmbedding()
                manager = SemanticTaskManager(
                    root,
                    context=FakeContext(provider),
                    config={
                        "embedding_provider_id": "fake-embedding",
                        "vision_provider_id": "fake-vision",
                    },
                )
                await manager.start("demo")
                await manager._tasks["demo"]
                self.assertEqual(manager.status("demo")["task_status"], "completed")
                self.assertEqual(provider.batch_calls, 1)
                self.assertEqual(provider.single_calls, 1)
                task_state = json.loads(
                    (root / "semantic_indexes" / "demo" / "task_state.json").read_text()
                )
                self.assertEqual(task_state["embedding_provider_id"], "fake-embedding")
                self.assertEqual(task_state["embedding_dimension"], 2)
                index_path = root / "semantic_indexes" / "demo" / "index.faiss"
                self.assertEqual(faiss.read_index(str(index_path)).ntotal, 1)

        asyncio.run(run())

    def test_task_manager_automatically_selects_and_persists_embedding_provider(self):
        provider = FakeEmbedding()
        with tempfile.TemporaryDirectory() as temp:
            configured = SemanticTaskManager(
                temp,
                context=FakeContext(provider),
                config={"embedding_provider_id": "fake-embedding"},
            )
            self.assertIs(configured._resolve_embedding_provider(), provider)

            automatic = SemanticTaskManager(temp, context=FakeContext(provider))
            self.assertIs(automatic._resolve_embedding_provider("demo"), provider)
            selection = json.loads(
                (
                    Path(temp)
                    / "semantic_indexes"
                    / "demo"
                    / "provider_selection.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(selection["selection_mode"], "automatic")
            self.assertEqual(selection["effective_provider_id"], "fake-embedding")
            self.assertEqual(selection["configured_dimension"], 2)

    def test_invalid_explicit_provider_does_not_fallback_to_automatic(self):
        provider = FakeEmbedding()
        with tempfile.TemporaryDirectory() as temp:
            manager = SemanticTaskManager(
                temp,
                context=FakeContext(provider),
                config={"embedding_provider_id": "missing-provider"},
            )
            self.assertIsNone(manager._resolve_embedding_provider("demo"))

    def test_full_task_is_blocked_before_queue_without_embedding_provider(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                pack = root / "packs" / "demo"
                (pack / "memes" / "a").mkdir(parents=True)
                (pack / "memes" / "a" / "one.png").write_bytes(b"one")
                empty_context = type(
                    "EmptyContext",
                    (),
                    {
                        "llm_generate": FakeContext(FakeEmbedding()).llm_generate,
                        "get_all_embedding_providers": lambda self: [],
                    },
                )()
                manager = SemanticTaskManager(
                    root,
                    context=empty_context,
                    config={"vision_provider_id": "fake-vision"},
                )
                with self.assertRaisesRegex(RuntimeError, "没有可自动选择"):
                    await manager.start("demo")
                self.assertNotIn("demo", manager._tasks)
                self.assertFalse(
                    (root / "semantic_indexes" / "demo" / "task_state.json").exists()
                )

        asyncio.run(run())

    def test_caption_only_task_does_not_require_embedding_provider(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                pack = root / "packs" / "demo"
                (pack / "memes" / "a").mkdir(parents=True)
                (pack / "memes" / "a" / "one.png").write_bytes(b"one")
                manager = SemanticTaskManager(
                    root,
                    context=FakeContext(FakeEmbedding()),
                    config={"vision_provider_id": "fake-vision"},
                )
                await manager.start("demo", mode="caption_only")
                await manager._tasks["demo"]
                self.assertEqual(manager.status("demo")["task_status"], "completed")
                self.assertEqual(manager.status("demo")["caption_done"], 1)
                self.assertEqual(manager.status("demo")["embedding_done"], 0)

        asyncio.run(run())

    def test_full_task_requires_explicit_visual_provider(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                pack = root / "packs" / "demo"
                (pack / "memes" / "a").mkdir(parents=True)
                (pack / "memes" / "a" / "one.png").write_bytes(b"one")
                manager = SemanticTaskManager(
                    root,
                    context=FakeContext(FakeEmbedding()),
                    config={"embedding_provider_id": "fake-embedding"},
                )
                with self.assertRaisesRegex(RuntimeError, "未配置视觉模型"):
                    await manager.start("demo")
                self.assertNotIn("demo", manager._tasks)
                self.assertFalse(
                    (root / "semantic_indexes" / "demo" / "task_state.json").exists()
                )

        asyncio.run(run())

    def test_dimension_probe_blocks_queue_and_persists_error(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                pack = root / "packs" / "demo"
                (pack / "memes" / "a").mkdir(parents=True)
                (pack / "memes" / "a" / "one.png").write_bytes(b"one")
                manager = SemanticTaskManager(
                    root,
                    context=FakeContext(WrongDimensionEmbedding()),
                    config={"embedding_provider_id": "fake-embedding", "vision_provider_id": "fake-vision"},
                )
                with self.assertRaisesRegex(RuntimeError, "维度校验失败"):
                    await manager.start("demo")
                self.assertNotIn("demo", manager._tasks)
                self.assertFalse((root / "semantic_indexes" / "demo" / "task_state.json").exists())
                selection = json.loads(
                    (root / "semantic_indexes" / "demo" / "provider_selection.json").read_text()
                )
                self.assertFalse(selection["dimension_verified"])
                self.assertIn("维度不一致", selection["verification_error"])

        asyncio.run(run())

    def test_clear_local_state_preserves_caption_and_removes_vectors(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                pack = root / "packs" / "demo"
                (pack / "memes" / "a").mkdir(parents=True)
                (pack / "memes" / "a" / "one.png").write_bytes(b"one")
                manager = SemanticTaskManager(
                    root,
                    context=FakeContext(FakeEmbedding()),
                    config={"embedding_provider_id": "fake-embedding", "vision_provider_id": "fake-vision"},
                )
                await manager.start("demo")
                await manager._tasks["demo"]
                metadata = load_metadata(pack)
                digest = next(iter(metadata["images"]))
                metadata["images"][digest]["caption_status"] = "done"
                metadata["images"][digest]["embedding_status"] = "done"
                save_metadata(pack, metadata)
                (root / "semantic_indexes" / "demo" / "task_state.json").write_text("{}")
                result = await manager.clear_local_semantic_state("demo")
                current = load_metadata(pack)
                self.assertEqual(current["images"][digest]["caption_status"], "done")
                self.assertEqual(current["images"][digest]["embedding_status"], "cleared")
                self.assertFalse((root / "semantic_indexes" / "demo" / "index.faiss").exists())
                self.assertEqual(result["task_status"], "idle")
                self.assertEqual(result["pending"], 0)
                self.assertTrue(result["queue_cleared"])

        asyncio.run(run())

    def test_public_semantic_state_removes_provider_and_dimension(self):
        portable = reset_local_embedding_state(
            {
                "embedding_provider_id": "private-provider",
                "embedding_dimension": 4096,
                "images": {
                    "a" * 64: {
                        "caption": "描述",
                        "tags": ["测试"],
                        "caption_status": "done",
                        "embedding_status": "done",
                        "embedding_dimension": 4096,
                    }
                },
            }
        )
        self.assertNotIn("embedding_provider_id", portable)
        self.assertNotIn("embedding_dimension", portable)
        item = portable["images"]["a" * 64]
        self.assertEqual(item["embedding_status"], "pending")
        self.assertNotIn("embedding_dimension", item)
        self.assertTrue(portable["requires_local_index_rebuild"])

    def test_rebuild_is_blocked_while_task_is_running(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / "packs" / "demo").mkdir(parents=True)
                manager = SemanticTaskManager(
                    root,
                    context=FakeContext(FakeEmbedding()),
                    config={"embedding_provider_id": "fake-embedding"},
                )
                blocker = asyncio.Event()

                async def wait_forever():
                    await blocker.wait()

                running_task = asyncio.create_task(wait_forever())
                manager._tasks["demo"] = running_task
                try:
                    with self.assertRaisesRegex(RuntimeError, "语义化任务尚未结束"):
                        await manager.rebuild_index("demo")
                finally:
                    running_task.cancel()
                    await asyncio.gather(running_task, return_exceptions=True)

        asyncio.run(run())

    def test_resume_and_rebuild_require_embedding_provider(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / "packs" / "demo").mkdir(parents=True)
                empty_context = type(
                    "EmptyContext",
                    (),
                    {"get_all_embedding_providers": lambda self: []},
                )()
                manager = SemanticTaskManager(root, context=empty_context)
                manager._save_state("demo", {"task_status": "paused"})
                with self.assertRaisesRegex(RuntimeError, "没有可自动选择"):
                    await manager.resume("demo")
                with self.assertRaisesRegex(RuntimeError, "没有可自动选择"):
                    await manager.rebuild_index("demo")

        asyncio.run(run())

    def test_resume_restarts_stale_running_task(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                manager = SemanticTaskManager(
                    temp,
                    context=FakeContext(FakeEmbedding()),
                    config={"embedding_provider_id": "fake-embedding"},
                )
                manager._save_state("demo", {"task_status": "running"})
                resumed = asyncio.Event()

                async def fake_run(pack_id, *, mode, force):
                    self.assertEqual((pack_id, mode, force), ("demo", "full", False))
                    resumed.set()

                manager._run = fake_run
                await manager.resume("demo")
                await manager._tasks["demo"]
                self.assertTrue(resumed.is_set())

        asyncio.run(run())

    def test_pause_with_five_concurrency_cancels_requests_and_restores_queue(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                pack = root / "packs" / "demo" / "memes" / "queue"
                pack.mkdir(parents=True)
                for index in range(8):
                    (pack / f"{index}.png").write_bytes(f"image-{index}".encode())

                context = BlockingVisionContext(FakeEmbedding(), expected_started=5)
                manager = SemanticTaskManager(
                    root,
                    context=context,
                    config={"vision_provider_id": "fake-vision", "concurrency": 5},
                )
                await manager.start("demo", mode="caption_only", concurrency=5)
                await asyncio.wait_for(context.expected_calls_started.wait(), timeout=1)

                paused = await manager.pause("demo")
                self.assertEqual(paused["task_status"], "paused")
                self.assertEqual(paused["active_request_count"], 0)
                self.assertEqual(paused["queued_caption_tasks"], 8)
                self.assertEqual(paused["running_tasks"], 0)
                self.assertIn("已中断 5 个模型请求", paused["message"])

                stopped = manager.status("demo")
                self.assertEqual(context.started_calls, 5)
                self.assertEqual(stopped["task_status"], "paused")
                self.assertEqual(stopped["queue_status"], "paused")
                self.assertEqual(stopped["queued_caption_tasks"], 8)
                self.assertFalse(stopped["can_pause"])
                self.assertTrue(stopped["can_resume"])

                context.release_calls.set()
                resumed = await manager.resume("demo", concurrency=5)
                self.assertIn("队列已继续", resumed["message"])
                await asyncio.wait_for(manager._tasks["demo"], timeout=1)
                finished = manager.status("demo")
                self.assertEqual(context.started_calls, 13)
                self.assertEqual(finished["task_status"], "completed")
                self.assertEqual(finished["queue_status"], "waiting")

        asyncio.run(run())

    def test_stale_paused_requests_are_restored_as_waiting_queue(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                pack = root / "packs" / "demo"
                (pack / "memes" / "queue").mkdir(parents=True)
                for index in range(2):
                    (pack / "memes" / "queue" / f"{index}.png").write_bytes(
                        f"stale-image-{index}".encode()
                    )
                metadata = reconcile_metadata(pack)
                first_item = next(iter(metadata["images"].values()))
                first_item["caption_status"] = "running"
                save_metadata(pack, metadata)

                context = BlockingVisionContext(FakeEmbedding(), expected_started=2)
                context.release_calls.set()
                manager = SemanticTaskManager(
                    root,
                    context=context,
                    config={"vision_provider_id": "fake-vision", "concurrency": 5},
                )
                manager._save_state(
                    "demo",
                    {
                        "task_status": "paused",
                        "task_phase": "captioning",
                        "mode": "caption_only",
                        "concurrency": 5,
                        "active_items": [first_item["relative_path"]],
                        "started_at": "2026-01-01T00:00:00+00:00",
                        "paused_at": "2026-01-01T00:00:10+00:00",
                        "paused_seconds": 3,
                    },
                )

                stale = manager.status("demo")
                self.assertEqual(stale["active_request_count"], 0)
                self.assertEqual(stale["queued_caption_tasks"], 2)
                self.assertEqual(stale["elapsed_seconds"], 7)
                recovered = load_metadata(pack)
                self.assertTrue(
                    all(
                        item.get("caption_status") == "pending"
                        for item in recovered["images"].values()
                    )
                )

                await manager.resume("demo", concurrency=3)
                await asyncio.wait_for(manager._tasks["demo"], timeout=1)
                finished = manager.status("demo")
                self.assertEqual(context.started_calls, 2)
                self.assertEqual(finished["task_status"], "completed")
                self.assertEqual(finished["active_request_count"], 0)

        asyncio.run(run())

    def test_unexpected_worker_error_cancels_and_awaits_sibling_workers(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                image_dir = root / "packs" / "demo" / "memes" / "queue"
                image_dir.mkdir(parents=True)
                for index in range(4):
                    (image_dir / f"{index}.png").write_bytes(
                        f"worker-{index}".encode()
                    )
                manager = SemanticTaskManager(
                    root,
                    context=FakeContext(FakeEmbedding()),
                    config={"vision_provider_id": "fake-vision", "concurrency": 4},
                )
                started = 0
                cancelled = 0
                completed = 0
                wait_forever = asyncio.Event()

                async def fake_process(
                    pack_id,
                    pack_dir,
                    metadata,
                    digest,
                    raw_item,
                    vision_provider,
                    force,
                    semaphore,
                ):
                    nonlocal started, cancelled, completed
                    started += 1
                    if started == 1:
                        await asyncio.sleep(0)
                        raise OSError("模拟持久化失败")
                    try:
                        await wait_forever.wait()
                        completed += 1
                    except asyncio.CancelledError:
                        cancelled += 1
                        raise

                manager._process_caption_item = fake_process
                await manager.start("demo", mode="caption_only", concurrency=4)
                await asyncio.wait_for(manager._tasks["demo"], timeout=1)
                self.assertEqual(started, 4)
                self.assertEqual(cancelled, 3)
                self.assertEqual(completed, 0)
                self.assertEqual(manager.status("demo")["task_status"], "failed")

        asyncio.run(run())

    def test_persisted_pause_requires_resume_and_blocks_pack_mutation(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / "packs" / "demo" / "memes" / "queue").mkdir(
                    parents=True
                )
                manager = SemanticTaskManager(
                    root,
                    context=FakeContext(FakeEmbedding()),
                    config={"vision_provider_id": "fake-vision"},
                )
                manager._save_state(
                    "demo", {"task_status": "paused", "mode": "caption_only"}
                )
                with self.assertRaisesRegex(RuntimeError, "继续队列"):
                    await manager.start("demo", mode="caption_only")
                with self.assertRaisesRegex(RuntimeError, "暂停或中断"):
                    manager.assert_pack_mutation_allowed("demo", "卸载资源包")

        asyncio.run(run())

    def test_standalone_index_build_is_visible_in_task_status(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                image_dir = root / "packs" / "demo" / "memes" / "queue"
                image_dir.mkdir(parents=True)
                for index in range(2):
                    (image_dir / f"{index}.png").write_bytes(
                        f"index-{index}".encode()
                    )
                provider = BlockingEmbedding()
                manager = SemanticTaskManager(
                    root,
                    context=FakeContext(provider),
                    config={
                        "vision_provider_id": "fake-vision",
                        "embedding_provider_id": "fake-embedding",
                    },
                )
                await manager.start("demo", mode="caption_only")
                await manager._tasks["demo"]

                rebuilding = asyncio.create_task(
                    manager.rebuild_index("demo", force=True)
                )
                await asyncio.wait_for(provider.batch_started.wait(), timeout=1)
                status = manager.status("demo")
                self.assertEqual(status["task_status"], "running")
                self.assertEqual(status["task_phase"], "indexing")
                self.assertTrue(status["worker_alive"])
                self.assertFalse(status["can_start"])
                with self.assertRaisesRegex(RuntimeError, "语义任务尚未结束"):
                    manager.assert_pack_mutation_allowed("demo", "卸载资源包")

                provider.release_batch.set()
                await asyncio.wait_for(rebuilding, timeout=1)
                finished = manager.status("demo")
                self.assertEqual(finished["task_status"], "completed")
                self.assertFalse(finished["worker_alive"])

        asyncio.run(run())

    def test_clear_queue_cancels_standalone_index_build(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                image_dir = root / "packs" / "demo" / "memes" / "queue"
                image_dir.mkdir(parents=True)
                (image_dir / "one.png").write_bytes(b"cancel-index")
                provider = BlockingEmbedding()
                manager = SemanticTaskManager(
                    root,
                    context=FakeContext(provider),
                    config={
                        "vision_provider_id": "fake-vision",
                        "embedding_provider_id": "fake-embedding",
                    },
                )
                await manager.start("demo", mode="caption_only")
                await manager._tasks["demo"]

                rebuilding = asyncio.create_task(
                    manager.rebuild_index("demo", force=True)
                )
                await asyncio.wait_for(provider.batch_started.wait(), timeout=1)
                with self.assertRaisesRegex(RuntimeError, "收尾阶段不能暂停"):
                    await asyncio.wait_for(manager.pause("demo"), timeout=0.2)

                cleared = await asyncio.wait_for(
                    manager.clear_local_semantic_state("demo"), timeout=1
                )
                self.assertTrue(rebuilding.cancelled())
                self.assertEqual(cleared["task_status"], "idle")
                self.assertTrue(cleared["queue_cleared"])
                self.assertNotIn("demo", manager._index_tasks)

        asyncio.run(run())

    def test_other_pack_concurrency_is_reported_without_global_limiting(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                for pack_id in ("pack-a", "pack-b"):
                    image_dir = root / "packs" / pack_id / "memes" / "queue"
                    image_dir.mkdir(parents=True)
                    for index in range(6):
                        (image_dir / f"{index}.png").write_bytes(
                            f"{pack_id}-{index}".encode()
                        )
                context = BlockingVisionContext(FakeEmbedding(), expected_started=5)
                manager = SemanticTaskManager(
                    root,
                    context=context,
                    config={"vision_provider_id": "fake-vision", "concurrency": 5},
                )
                await manager.start("pack-a", mode="caption_only", concurrency=5)
                await asyncio.wait_for(context.expected_calls_started.wait(), timeout=1)
                started_b = await manager.start(
                    "pack-b", mode="caption_only", concurrency=3
                )
                self.assertEqual(
                    started_b["other_active_tasks"][0]["pack_id"], "pack-a"
                )
                self.assertEqual(
                    started_b["other_active_tasks"][0]["concurrency"], 5
                )
                self.assertIn("并发会叠加", started_b["message"])

                await manager.clear_local_semantic_state("pack-a")
                await manager.clear_local_semantic_state("pack-b")

        asyncio.run(run())

    def test_external_file_operation_blocks_semantic_start(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / "packs" / "demo" / "memes" / "queue").mkdir(
                    parents=True
                )
                manager = SemanticTaskManager(
                    root,
                    context=FakeContext(FakeEmbedding()),
                    config={"vision_provider_id": "fake-vision"},
                )
                manager.begin_external_pack_operation("demo", "从远端覆盖本地表情包")
                status = manager.status("demo")
                self.assertEqual(status["queue_status"], "external_operation")
                self.assertFalse(status["can_start"])
                with self.assertRaisesRegex(RuntimeError, "从远端覆盖"):
                    await manager.start("demo", mode="caption_only")
                manager.end_external_pack_operation("demo")
                self.assertTrue(manager.status("demo")["can_start"])

        asyncio.run(run())

    def test_caption_prompt_uses_complete_meme_pragmatics_workflow(self):
        prompt = build_caption_prompt(5)
        self.assertIn("不是给图片写普通图注", prompt)
        self.assertIn("区分原图内容与后期叠加", prompt)
        self.assertIn("梗的构成方式", prompt)
        self.assertIn("严格保留原文的语气和标点", prompt)
        self.assertIn("确定说话视角和行为归属", prompt)
        self.assertIn("发送表情包的人、聊天对象、图中人物", prompt)
        self.assertIn("不能默认所有句子都在质问聊天对象", prompt)
        self.assertIn("己方自嘲、承认后装傻", prompt)
        self.assertIn("不提供联网搜索或其他外部工具", prompt)
        self.assertIn("禁止调用 web_search", prompt)
        self.assertNotIn("必须尝试检索", prompt)
        self.assertIn("触发发送这张图", prompt)
        self.assertIn("言语功能", prompt)
        self.assertIn("复合语气", prompt)
        self.assertIn("表情不等于梗义", prompt)
        self.assertIn("同一个 GIF", prompt)
        self.assertIn("不要把它们当成互不相关的图片", prompt)
        self.assertNotIn("小B崽子", prompt)
        self.assertNotIn("《我的世界》", prompt)
        self.assertNotIn("不觉得羞愧", prompt)

    def test_caption_retry_recovers_tool_text_and_counts_both_calls(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                image_path = Path(temp) / "one.jpg"
                image_path.write_bytes(b"fake-image")
                context = RetryVisionContext()
                result = await generate_caption(context, image_path, "fake-vision")
                self.assertEqual(result["caption"], "惊慌地摆手求饶")
                self.assertEqual(result["token_usage"]["calls"], 2)
                self.assertEqual(result["token_usage"]["total"], 175)
                self.assertEqual(len(context.requests), 2)
                self.assertEqual(
                    context.requests[0]["response_format"], {"type": "json_object"}
                )
                self.assertEqual(context.requests[0]["temperature"], 0)
                self.assertIn("禁止联网", context.requests[0]["system_prompt"])
                self.assertIn("上一次输出不是可用的 JSON", context.requests[1]["prompt"])

        asyncio.run(run())

    def test_normal_start_preserves_existing_caption_until_force_is_requested(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                pack = root / "packs" / "demo"
                image_dir = pack / "memes" / "angry"
                image_dir.mkdir(parents=True)
                (image_dir / "one.png").write_bytes(b"old-caption")
                metadata = reconcile_metadata(pack)
                item = next(iter(metadata["images"].values()))
                item.update(
                    {
                        "caption": "旧提示词生成的错误描述",
                        "tags": ["旧标签"],
                        "auto_tags": ["旧标签"],
                        "caption_status": "done",
                        "embedding_status": "cleared",
                        "prompt_version": "meme-semantic-v4",
                        "vision_model": "old-vision-provider",
                    }
                )
                save_metadata(pack, metadata)
                provider = FakeEmbedding()
                manager = SemanticTaskManager(
                    root,
                    context=FakeContext(provider),
                    config={
                        "vision_provider_id": "fake-vision",
                        "embedding_provider_id": "fake-embedding",
                    },
                )

                await manager.start("demo")
                await manager._tasks["demo"]

                current = next(iter(load_metadata(pack)["images"].values()))
                self.assertEqual(current["caption"], "旧提示词生成的错误描述")
                self.assertEqual(current["prompt_version"], "meme-semantic-v4")
                self.assertEqual(current["vision_model"], "old-vision-provider")
                state = manager.status("demo")
                self.assertEqual(state["vision_calls"], 0)
                self.assertTrue(state["index_ready"])
                self.assertEqual(provider.batch_calls, 1)

                await manager.start("demo", force=True)
                await manager._tasks["demo"]
                regenerated = next(iter(load_metadata(pack)["images"].values()))
                self.assertEqual(regenerated["caption"], "我有点心虚想装傻")
                self.assertEqual(regenerated["prompt_version"], PROMPT_VERSION)

        asyncio.run(run())

    def test_gif_uses_up_to_five_evenly_spaced_frames(self):
        with tempfile.TemporaryDirectory() as temp:
            gif_path = Path(temp) / "animated.gif"
            colors = [(index * 20, 0, 0) for index in range(11)]
            frames = [Image.new("RGB", (4, 4), color) for color in colors]
            frames[0].save(
                gif_path,
                save_all=True,
                append_images=frames[1:],
                duration=100,
                loop=0,
                disposal=2,
            )

            visual_paths, temp_paths = prepare_visual_inputs(gif_path)
            try:
                self.assertEqual(len(visual_paths), 5)
                sampled_colors = []
                for path in visual_paths:
                    with Image.open(path) as sampled:
                        sampled_colors.append(sampled.convert("RGB").getpixel((0, 0)))
                self.assertEqual(
                    sampled_colors,
                    [colors[index] for index in (0, 2, 5, 8, 10)],
                )
            finally:
                for path in temp_paths:
                    Path(path).unlink(missing_ok=True)

    def test_misnamed_gif_is_detected_from_file_content(self):
        with tempfile.TemporaryDirectory() as temp:
            disguised_path = Path(temp) / "animated.jpg"
            colors = [(index * 30, 0, 0) for index in range(7)]
            frames = [Image.new("RGB", (4, 4), color) for color in colors]
            frames[0].save(
                disguised_path,
                format="GIF",
                save_all=True,
                append_images=frames[1:],
                duration=100,
                loop=0,
                disposal=2,
            )

            visual_paths, temp_paths = prepare_visual_inputs(disguised_path)
            try:
                self.assertEqual(len(visual_paths), 5)
                self.assertTrue(all(Path(path).suffix == ".png" for path in visual_paths))
            finally:
                for path in temp_paths:
                    Path(path).unlink(missing_ok=True)

    def test_scan_deduplicates_and_reuses_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "memes" / "a").mkdir(parents=True)
            (root / "memes" / "b").mkdir(parents=True)
            (root / "memes" / "a" / "one.png").write_bytes(b"same")
            (root / "memes" / "b" / "copy.png").write_bytes(b"same")
            (root / "memes" / "b" / "two.jpg").write_bytes(b"other")
            metadata = reconcile_metadata(root)
            self.assertEqual(
                (
                    metadata["file_total"],
                    metadata["unique_total"],
                    metadata["reused_duplicate_files"],
                ),
                (3, 2, 1),
            )
            save_metadata(root, metadata)
            (root / "memes" / "a" / "one.png").rename(
                root / "memes" / "a" / "moved.png"
            )
            moved = reconcile_metadata(root)
            self.assertEqual(len(moved["images"]), 2)
            self.assertIn(
                "moved.png",
                {
                    item["relative_path"].split("/")[-1]
                    for item in moved["images"].values()
                },
            )

            moved_digest = next(
                digest
                for digest, item in moved["images"].items()
                if item["relative_path"].endswith("moved.png")
            )
            moved["images"][moved_digest]["caption_status"] = "failed"
            moved["images"][moved_digest]["caption"] = ""
            moved["images"][moved_digest]["tags"] = []
            save_metadata(root, moved)
            imported = reconcile_metadata(
                root,
                external_data={
                    "images": {
                        moved_digest.upper(): {
                            "content_sha256": moved_digest.upper(),
                            "caption": "从外部记录复用",
                            "tags": ["外部语义"],
                        }
                    }
                },
            )
            self.assertEqual(
                imported["images"][moved_digest]["caption"], "从外部记录复用"
            )
            self.assertEqual(imported["images"][moved_digest]["caption_status"], "done")

    def test_paths_reject_absolute_traversal_and_external_symlink(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            tempfile.TemporaryDirectory() as outside,
        ):
            root = Path(temp)
            memes = root / "memes" / "a"
            memes.mkdir(parents=True)
            outside_image = Path(outside) / "outside.png"
            outside_image.write_bytes(b"outside")
            (memes / "linked.png").symlink_to(outside_image)

            self.assertIsNone(safe_relative_path(root, str(outside_image)))
            self.assertIsNone(safe_relative_path(root, "memes/a/../a/image.png"))

            digest = "f" * 64
            metadata = reconcile_metadata(
                root,
                external_data={
                    "images": {
                        digest: {
                            "content_sha256": digest,
                            "relative_path": str(outside_image),
                            "caption": "外部记录",
                            "tags": ["测试"],
                        }
                    }
                },
            )
            self.assertEqual(metadata["file_total"], 1)
            self.assertEqual(metadata["unique_total"], 0)
            self.assertEqual(metadata["images"][digest]["relative_path"], "")

    def test_caption_result_requires_fine_grained_fields(self):
        caption, tags, visible_text = parse_caption_result(
            '{"caption":"心虚装傻","tags":["心虚","尴尬"],"visible_text":""}'
        )
        self.assertEqual(caption, "心虚装傻")
        self.assertEqual(tags, ["心虚", "尴尬"])
        with self.assertRaises(ValueError):
            parse_caption_result('{"caption":"只有情绪"}')
        mixed = parse_caption_result(
            '工具调用：{"name":"web_search","parameters":{"query":"角色"}}\n'
            '最终结果：{"caption":"无奈地装傻","tags":["无奈","装傻"],'
            '"visible_text":""}'
        )
        self.assertEqual(mixed[0], "无奈地装傻")
        with self.assertRaisesRegex(ValueError, "无效数值"):
            normalize_vector([float("nan"), 1.0])

    def test_index_search_returns_top_candidate_without_path(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                pack = root / "packs" / "demo"
                (pack / "memes" / "a").mkdir(parents=True)
                (pack / "memes" / "a" / "one.png").write_bytes(b"one")
                (pack / "memes" / "a" / "two.png").write_bytes(b"two")
                metadata = reconcile_metadata(pack)
                for item in metadata["images"].values():
                    is_target = item["relative_path"].endswith("one.png")
                    item.update(
                        {
                            "caption": "我有点心虚想装傻"
                            if is_target
                            else "开心庆祝成功",
                            "tags": ["心虚", "装傻"] if is_target else ["开心", "庆祝"],
                            "caption_status": "done",
                        }
                    )
                save_metadata(pack, metadata)
                fake_embedding = FakeEmbedding()
                adapter = EmbeddingAdapter(fake_embedding)
                manifest = await build_index(pack, root, "demo", adapter)
                index_path = root / "semantic_indexes" / "demo" / "index.faiss"
                self.assertEqual(manifest["index_format"], "faiss-indexflatip-v1")
                self.assertEqual(faiss.read_index(str(index_path)).ntotal, 2)
                with self.assertRaises((UnicodeDecodeError, json.JSONDecodeError)):
                    json.loads(index_path.read_text(encoding="utf-8"))

                await build_index(pack, root, "demo", adapter)
                self.assertEqual(fake_embedding.batch_calls, 1)
                indexed_metadata = load_metadata(pack)
                self.assertTrue(
                    index_is_ready(
                        root,
                        "demo",
                        indexed_metadata,
                        adapter.provider_id,
                        adapter.model_name,
                        adapter.dimension,
                    )
                )
                self.assertFalse(
                    index_is_ready(
                        root,
                        "demo",
                        indexed_metadata,
                        "another-provider",
                        adapter.model_name,
                        adapter.dimension,
                    )
                )
                result = await search_memes(
                    pack, root, "demo", "我有点心虚", fake_embedding
                )
                self.assertTrue(result["ok"])
                self.assertTrue(result["candidates"])
                self.assertEqual(result["candidates"][0]["caption"], "我有点心虚想装傻")
                self.assertNotIn("content_sha256", result["candidates"][0])
                self.assertEqual(fake_embedding.single_calls, 1)
                await search_memes(pack, root, "demo", "我有点心虚", fake_embedding)
                self.assertEqual(fake_embedding.single_calls, 1)

        asyncio.run(run())

    def test_index_allows_failed_items_and_expands_colliding_ids(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                pack = root / "packs" / "demo"
                pack.mkdir(parents=True)
                first_digest = "a" * 12 + "1" * 52
                second_digest = "a" * 12 + "2" * 52
                metadata = {
                    "schema_version": "1.0",
                    "pack_id": "demo",
                    "images": {
                        first_digest: SemanticImage(
                            first_digest,
                            "memes/a/one.png",
                            caption="心虚装傻一号",
                            tags=["心虚"],
                            caption_status="done",
                        ).to_dict(),
                        second_digest: SemanticImage(
                            second_digest,
                            "memes/a/two.png",
                            caption="心虚装傻二号",
                            tags=["心虚"],
                            caption_status="done",
                        ).to_dict(),
                    },
                }
                save_metadata(pack, metadata)
                provider = FakeEmbedding()
                adapter = EmbeddingAdapter(provider)
                await build_index(pack, root, "demo", adapter)

                result = await search_memes(
                    pack,
                    root,
                    "demo",
                    "我有点心虚，碰撞测试",
                    provider,
                    top_k=1,
                    min_score=-1,
                )
                self.assertEqual(
                    len(result["candidates"][0]["id"].removeprefix("meme:")), 16
                )

                current = load_metadata(pack)
                failed_digest = "b" * 64
                current["images"][failed_digest] = SemanticImage(
                    failed_digest,
                    "",
                    caption="已经生成描述但向量失败",
                    tags=["失败项"],
                    caption_status="done",
                    embedding_status="failed",
                    error="向量生成失败",
                ).to_dict()
                save_metadata(pack, current)
                self.assertTrue(
                    index_is_ready(
                        root,
                        "demo",
                        current,
                        adapter.provider_id,
                        adapter.model_name,
                        adapter.dimension,
                    )
                )

                current["images"][first_digest]["caption"] = "语义已经改变"
                save_metadata(pack, current)
                self.assertFalse(
                    index_is_ready(
                        root,
                        "demo",
                        current,
                        adapter.provider_id,
                        adapter.model_name,
                        adapter.dimension,
                    )
                )

        asyncio.run(run())

    def test_candidates_accumulate_and_invalid_ids_are_ignored(self):
        event = FakeEvent()
        remember_candidates(event, [{"id": "meme:" + "1" * 12, "caption": "一"}])
        remember_candidates(event, [{"id": "meme:" + "2" * 12, "caption": "二"}])
        self.assertEqual(len(event.extra["meme_manager_semantic_candidates"]), 2)
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(candidate_records(temp, [{"id": "invalid"}]), [])

    def test_selected_id_must_be_from_current_candidates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "memes" / "a").mkdir(parents=True)
            image = root / "memes" / "a" / "one.png"
            image.write_bytes(b"one")
            metadata = reconcile_metadata(root)
            digest = next(iter(metadata["images"]))
            save_metadata(root, metadata)
            event = FakeEvent()
            event.set_extra(
                "meme_manager_semantic_candidates",
                {f"meme:{digest[:12]}": {"content_sha256": digest}},
            )
            self.assertEqual(
                validate_selected_id(event, f"meme:{digest[:12]}", root),
                image.resolve(),
            )
            self.assertIsNone(validate_selected_id(event, "meme:" + "0" * 12, root))
            image.write_bytes(b"replaced")
            self.assertIsNone(validate_selected_id(event, f"meme:{digest[:12]}", root))


if __name__ == "__main__":
    unittest.main()
