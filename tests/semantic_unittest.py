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
from backend.semantic_index import (
    EmbeddingAdapter,
    build_index,
    index_is_ready,
    search_index,
)
from backend.semantic_models import (
    PROMPT_VERSION,
    REVIEW_CATEGORY_DESCRIPTION,
    SemanticImage,
    build_semantic_text,
    compact_semantic_query,
    ensure_category_tag,
    extract_and_clean_semantic_meme_references,
    extract_visible_semantic_reply,
    normalize_vector,
    parse_caption_result,
    parse_caption_result_with_review,
    parse_semantic_query_result,
    semantic_entry_id,
)
from backend.semantic_query import (
    candidate_records,
    remember_candidates,
    search_memes,
    validate_selected_id,
)
from backend.semantic_storage import (
    apply_conflict_reclassifications,
    confirm_image_category,
    file_sha256,
    get_category_review_overview,
    get_image_semantic_detail,
    get_pack_semantic_summary,
    load_metadata,
    metadata_items,
    reconcile_metadata,
    reset_local_embedding_state,
    safe_relative_path,
    save_metadata,
    semantic_metadata_is_complete,
)
from backend.semantic_task import SemanticTaskManager
from image_host.img_sync import ImageSync
from PIL import Image
from utils import normalize_probability, probability_hit


def mark_category_reviewed(item, status="auto_match"):
    item["prompt_version"] = PROMPT_VERSION
    item["category_fit"] = "match"
    item["category_review_status"] = status
    item["category_review_context_hash"] = item.get("category_context_hash", "")
    return item


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


class ToolCaptionContext:
    def __init__(self):
        self.requests = []

    async def llm_generate(self, **kwargs):
        self.requests.append(kwargs)
        return type(
            "VisionResponse",
            (),
            {
                "completion_text": "",
                "tools_call_name": ["submit_meme_caption"],
                "tools_call_args": [
                    {
                        "caption": "假装镇定地承认自己心虚",
                        "tags": ["心虚", "装镇定", "自嘲", "聊天反应", "承认", "嘴硬"],
                        "visible_text": "我才不慌",
                    }
                ],
                "usage": {"prompt_tokens": 80, "completion_tokens": 20},
            },
        )()


class UnsupportedToolCaptionContext:
    def __init__(self, *, reject_response_format=False):
        self.requests = []
        self.reject_response_format = reject_response_format

    async def llm_generate(self, **kwargs):
        self.requests.append(kwargs)
        if "tools" in kwargs:
            raise RuntimeError("This model does not support function calling")
        if self.reject_response_format and "response_format" in kwargs:
            raise RuntimeError("response_format is unsupported")
        return type(
            "VisionResponse",
            (),
            {
                "completion_text": (
                    '{"caption":"无奈地摆手拒绝","tags":["无奈","拒绝",'
                    '"摆手","聊天反应","婉拒","退让"],"visible_text":"不了"}'
                )
            },
        )()


class FailedToolCaptionContext:
    def __init__(self):
        self.requests = []

    async def llm_generate(self, **kwargs):
        self.requests.append(kwargs)
        raise TimeoutError("vision provider request timed out")


class CategoryVisionContext(FakeContext):
    def __init__(self, payload):
        super().__init__(FakeEmbedding())
        self.payload = payload
        self.requests = []

    async def llm_generate(self, **kwargs):
        self.requests.append(kwargs)
        if "tools" in kwargs:
            return type(
                "VisionResponse",
                (),
                {
                    "completion_text": "",
                    "tools_call_name": ["submit_meme_caption"],
                    "tools_call_args": [self.payload],
                },
            )()
        return type(
            "VisionResponse",
            (),
            {"completion_text": json.dumps(self.payload, ensure_ascii=False)},
        )()


class SemanticMvpTest(unittest.TestCase):
    def test_semantic_reference_cleanup_accepts_wrapped_and_bare_model_output(self):
        meme_id = "meme:" + "7" * 12
        wrapped_text, wrapped_ids = extract_and_clean_semantic_meme_references(
            f"我会陪着你。\n&&{meme_id}&&"
        )
        self.assertEqual(wrapped_text, "我会陪着你。")
        self.assertEqual(wrapped_ids, [meme_id])

        leaked_text, leaked_ids = extract_and_clean_semantic_meme_references(
            f"`{meme_id}`，通过闭眼晃头的动作来表达撒娇式安抚。"
            "我知道你只是对自己要求很高。"
        )
        self.assertEqual(leaked_text, "我知道你只是对自己要求很高。")
        self.assertEqual(leaked_ids, [meme_id])

        inline_text, inline_ids = extract_and_clean_semantic_meme_references(
            f"我会陪着你，{meme_id}"
        )
        self.assertEqual(inline_text, "我会陪着你，")
        self.assertEqual(inline_ids, [meme_id])

    def test_emotion_query_removes_reasoning_and_machine_markers(self):
        raw = (
            "<thinking>这里有非常长的分析、用户历史和回复策略。</thinking>"
            "先别急着否定自己的努力，我是真的替你骄傲。\n"
            "&&meow&&"
        )
        visible = extract_visible_semantic_reply(raw)
        self.assertEqual(
            visible,
            "先别急着否定自己的努力，我是真的替你骄傲。",
        )
        self.assertNotIn("thinking", visible)
        self.assertNotIn("meow", visible)

    def test_emotion_query_parser_enforces_short_vector_input(self):
        raw = (
            '<think>内部分析可能出现 {"other":"value"}</think>\n'
            '{"query":"温柔安慰 肯定努力 陪伴鼓励"}'
        )
        self.assertEqual(
            parse_semantic_query_result(raw, "备用文本"),
            "温柔安慰 肯定努力 陪伴鼓励",
        )
        long_query = compact_semantic_query("安慰 " * 40)
        self.assertLessEqual(len(long_query), 48)
        self.assertEqual(
            parse_semantic_query_result('{"query":""}', "这是备用可见回复"),
            "这是备用可见回复",
        )

    def test_probability_gate_supports_zero_middle_and_full_percent(self):
        self.assertFalse(probability_hit(0, roll=1))
        self.assertFalse(probability_hit("invalid", roll=1))
        self.assertTrue(probability_hit(50, roll=50))
        self.assertFalse(probability_hit(50, roll=51))
        self.assertTrue(probability_hit(100, roll=100))
        self.assertEqual(normalize_probability(-1), 0)
        self.assertEqual(normalize_probability(101), 100)

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
            for semantic_item in metadata["images"].values():
                semantic_item.update(
                    {
                        "caption": "无奈地摊手表示没办法",
                        "tags": ["无奈", "摊手"],
                        "visible_text": "",
                        "caption_status": "done",
                        "embedding_status": "done",
                    }
                )
                mark_category_reviewed(semantic_item)
            save_metadata(root, metadata)

            summary = get_pack_semantic_summary(root)
            self.assertEqual(summary["semantic_status"], "complete")
            self.assertTrue(semantic_metadata_is_complete(root))
            self.assertEqual(summary["semantic_file_total"], 2)
            self.assertEqual(summary["semantic_caption_total"], 2)
            self.assertEqual(summary["semantic_caption_done"], 2)

            duplicate_detail = get_image_semantic_detail(root, duplicate_image)
            self.assertEqual(duplicate_detail["status"], "complete")
            self.assertEqual(duplicate_detail["caption"], "无奈地摊手表示没办法")
            self.assertEqual(duplicate_detail["tags"], ["category:b", "无奈", "摊手"])

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

            missing_detail = get_image_semantic_detail(root, first_category / "new.jpg")
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
        self.assertLess(
            list(schema).index("semantic"), list(schema).index("generation")
        )
        semantic_items = schema["semantic"]["items"]
        self.assertEqual(semantic_items["min_score"]["type"], "float")
        self.assertEqual(
            semantic_items["vision_provider_id"]["_special"],
            "select_provider",
        )
        self.assertEqual(
            semantic_items["embedding_provider_id"]["_special"],
            "select_provider_embedding",
        )
        self.assertNotIn("可选", semantic_items["embedding_provider_id"]["description"])
        self.assertEqual(list(semantic_items)[:3], ["enabled", "top_k", "min_score"])
        self.assertNotIn("concurrency", semantic_items)
        generation_items = schema["generation"]["items"]
        self.assertEqual(next(iter(generation_items)), "emotion")
        emotion_items = generation_items["emotion"]["items"]
        self.assertEqual(next(iter(emotion_items)), "llm")
        self.assertIn("接管 Tool", emotion_items["llm"]["description"])
        provider_item = emotion_items["llm"]["items"]["provider_id"]
        self.assertIn("留空使用回复模型", provider_item["description"])
        self.assertIn("自动复用", provider_item["hint"])

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
                    Path(temp) / "semantic_indexes" / "demo" / "provider_selection.json"
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
                    config={
                        "embedding_provider_id": "fake-embedding",
                        "vision_provider_id": "fake-vision",
                    },
                )
                with self.assertRaisesRegex(RuntimeError, "维度校验失败"):
                    await manager.start("demo")
                self.assertNotIn("demo", manager._tasks)
                self.assertFalse(
                    (root / "semantic_indexes" / "demo" / "task_state.json").exists()
                )
                selection = json.loads(
                    (
                        root / "semantic_indexes" / "demo" / "provider_selection.json"
                    ).read_text()
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
                    config={
                        "embedding_provider_id": "fake-embedding",
                        "vision_provider_id": "fake-vision",
                    },
                )
                await manager.start("demo")
                await manager._tasks["demo"]
                metadata = load_metadata(pack)
                digest = next(iter(metadata["images"]))
                metadata["images"][digest]["caption_status"] = "done"
                metadata["images"][digest]["embedding_status"] = "done"
                save_metadata(pack, metadata)
                (root / "semantic_indexes" / "demo" / "task_state.json").write_text(
                    "{}"
                )
                result = await manager.clear_local_semantic_state("demo")
                current = load_metadata(pack)
                self.assertEqual(current["images"][digest]["caption_status"], "done")
                self.assertEqual(
                    current["images"][digest]["embedding_status"], "cleared"
                )
                self.assertFalse(
                    (root / "semantic_indexes" / "demo" / "index.faiss").exists()
                )
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
                    (image_dir / f"{index}.png").write_bytes(f"worker-{index}".encode())
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
                    available_categories,
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
                (root / "packs" / "demo" / "memes" / "queue").mkdir(parents=True)
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
                    (image_dir / f"{index}.png").write_bytes(f"index-{index}".encode())
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
                self.assertEqual(started_b["other_active_tasks"][0]["concurrency"], 5)
                self.assertIn("并发会叠加", started_b["message"])

                await manager.clear_local_semantic_state("pack-a")
                await manager.clear_local_semantic_state("pack-b")

        asyncio.run(run())

    def test_external_file_operation_blocks_semantic_start(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / "packs" / "demo" / "memes" / "queue").mkdir(parents=True)
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
                self.assertNotIn("response_format", context.requests[0])
                self.assertEqual(context.requests[0]["tool_choice"], "required")
                self.assertEqual(
                    context.requests[0]["tools"].tools[0].name,
                    "submit_meme_caption",
                )
                self.assertEqual(context.requests[0]["temperature"], 0)
                self.assertIn("禁止联网", context.requests[0]["system_prompt"])
                self.assertNotIn("tools", context.requests[1])
                self.assertEqual(
                    context.requests[1]["response_format"], {"type": "json_object"}
                )
                self.assertIn(
                    "上一次输出不是可用的 JSON", context.requests[1]["prompt"]
                )

        asyncio.run(run())

    def test_caption_prefers_astrbot_generic_tool_call(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                image_path = Path(temp) / "one.jpg"
                image_path.write_bytes(b"fake-image")
                context = ToolCaptionContext()
                result = await generate_caption(context, image_path, "fake-vision")
                self.assertEqual(result["caption"], "假装镇定地承认自己心虚")
                self.assertEqual(result["visible_text"], "我才不慌")
                self.assertEqual(result["token_usage"]["calls"], 1)
                request = context.requests[0]
                self.assertEqual(request["tool_choice"], "required")
                self.assertNotIn("response_format", request)
                tool = request["tools"].tools[0]
                self.assertEqual(tool.name, "submit_meme_caption")
                self.assertEqual(
                    tool.parameters["required"],
                    [
                        "caption",
                        "tags",
                        "visible_text",
                        "category_fit",
                        "category_review_reason",
                        "suggested_category",
                    ],
                )

        asyncio.run(run())

    def test_caption_falls_back_to_structured_json_when_tools_are_unsupported(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                image_path = Path(temp) / "one.jpg"
                image_path.write_bytes(b"fake-image")
                context = UnsupportedToolCaptionContext()
                result = await generate_caption(context, image_path, "fake-vision")
                self.assertEqual(result["caption"], "无奈地摆手拒绝")
                self.assertEqual(len(context.requests), 2)
                self.assertIn("tools", context.requests[0])
                self.assertEqual(
                    context.requests[1]["response_format"], {"type": "json_object"}
                )
                self.assertNotIn("tools", context.requests[1])

                await generate_caption(context, image_path, "fake-vision")
                self.assertEqual(len(context.requests), 3)
                self.assertNotIn("tools", context.requests[2])
                self.assertEqual(
                    context.requests[2]["response_format"], {"type": "json_object"}
                )

        asyncio.run(run())

    def test_caption_falls_back_to_plain_json_when_both_features_are_unsupported(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                image_path = Path(temp) / "one.jpg"
                image_path.write_bytes(b"fake-image")
                context = UnsupportedToolCaptionContext(reject_response_format=True)
                result = await generate_caption(context, image_path, "fake-vision")
                self.assertEqual(result["caption"], "无奈地摆手拒绝")
                self.assertEqual(len(context.requests), 3)
                self.assertIn("tools", context.requests[0])
                self.assertIn("response_format", context.requests[1])
                self.assertNotIn("tools", context.requests[2])
                self.assertNotIn("response_format", context.requests[2])

        asyncio.run(run())

    def test_caption_does_not_hide_unrelated_provider_errors(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                image_path = Path(temp) / "one.jpg"
                image_path.write_bytes(b"fake-image")
                context = FailedToolCaptionContext()
                with self.assertRaisesRegex(TimeoutError, "timed out"):
                    await generate_caption(context, image_path, "fake-vision")
                self.assertEqual(len(context.requests), 1)
                self.assertIn("tools", context.requests[0])

        asyncio.run(run())

    def test_normal_start_refreshes_unchecked_ai_caption(self):
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
                self.assertTrue(current["caption"].endswith("我有点心虚想装傻"))
                self.assertEqual(current["prompt_version"], PROMPT_VERSION)
                self.assertEqual(current["vision_model"], "fake-vision")
                self.assertEqual(current["category_review_status"], "needs_review")
                state = manager.status("demo")
                self.assertEqual(state["vision_calls"], 1)
                self.assertTrue(state["index_ready"])
                self.assertEqual(provider.batch_calls, 1)

                await manager.start("demo", force=True)
                await manager._tasks["demo"]
                regenerated = next(iter(load_metadata(pack)["images"].values()))
                self.assertTrue(regenerated["caption"].endswith("我有点心虚想装傻"))
                self.assertIn("angry", regenerated["caption"])
                self.assertEqual(regenerated["tags"][0], "category:angry")
                self.assertEqual(regenerated["prompt_version"], PROMPT_VERSION)

        asyncio.run(run())

    def test_category_context_is_sent_and_controls_ambiguous_caption(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                image_path = Path(temp) / "one.png"
                image_path.write_bytes(b"ambiguous")
                context = CategoryVisionContext(
                    {
                        "caption": "人物表情比较模糊，看不出明显倾向",
                        "tags": ["表情模糊", "聊天反应"],
                        "visible_text": "",
                        "category_fit": "uncertain",
                        "category_review_reason": "画面线索较弱，无法可靠判断",
                    }
                )
                result = await generate_caption(
                    context,
                    image_path,
                    "fake-vision",
                    category="sad",
                    category_description="悲伤、失落、委屈或难过时使用",
                )
                request_prompt = context.requests[0]["prompt"]
                self.assertIn('当前分类名称："sad"', request_prompt)
                self.assertIn("悲伤、失落、委屈或难过时使用", request_prompt)
                self.assertIn("这张图片目前由用户归入上述分类", request_prompt)
                self.assertIn("悲伤、失落、委屈或难过", result["caption"])
                self.assertEqual(result["category_fit"], "uncertain")
                self.assertTrue(result["category_review_reason"])

        asyncio.run(run())

    def test_clear_category_conflict_keeps_real_meaning_and_requests_review(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                image_path = Path(temp) / "one.png"
                image_path.write_bytes(b"conflict")
                context = CategoryVisionContext(
                    {
                        "caption": "举杯欢呼庆祝胜利，明显是在表达开心",
                        "tags": ["开心", "庆祝", "欢呼"],
                        "visible_text": "赢了",
                        "category_fit": "conflict",
                        "category_review_reason": "文字和举杯动作都明确表示庆祝",
                    }
                )
                result = await generate_caption(
                    context,
                    image_path,
                    "fake-vision",
                    category="sad",
                    category_description="悲伤和失落",
                )
                self.assertEqual(
                    result["caption"], "举杯欢呼庆祝胜利，明显是在表达开心"
                )
                self.assertEqual(result["category_fit"], "conflict")
                self.assertIn("庆祝", result["category_review_reason"])

        asyncio.run(run())

    def test_conflict_prompt_lists_categories_and_rejects_invented_suggestion(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                image_path = Path(temp) / "one.png"
                image_path.write_bytes(b"catalog")
                context = CategoryVisionContext(
                    {
                        "caption": "冒汗尴尬，不知道如何回应",
                        "tags": ["冒汗", "尴尬", "无奈"],
                        "visible_text": "",
                        "category_fit": "conflict",
                        "category_review_reason": "没有愤怒证据，主要是尴尬无奈",
                        "suggested_category": "sigh",
                    }
                )
                result = await generate_caption(
                    context,
                    image_path,
                    "fake-vision",
                    category="angry",
                    category_description="抱怨、批评或激烈反对",
                    available_categories={
                        "angry": "抱怨、批评或激烈反对",
                        "sigh": "表达无奈、无语或感慨",
                        "confused": "表达困惑或理解障碍",
                    },
                )
                prompt = context.requests[0]["prompt"]
                self.assertIn('"sigh": "表达无奈、无语或感慨"', prompt)
                self.assertIn("不能代替画面证据", prompt)
                self.assertIn("冒汗、慌张、尴尬笑", prompt)
                self.assertEqual(result["suggested_category"], "sigh")
                tool = context.requests[0]["tools"].tools[0]
                self.assertEqual(
                    tool.parameters["properties"]["suggested_category"]["enum"],
                    ["", "confused", "sigh"],
                )

                invalid_context = CategoryVisionContext(
                    {
                        "caption": "明显是在害怕",
                        "tags": ["害怕", "退缩"],
                        "visible_text": "",
                        "category_fit": "conflict",
                        "category_review_reason": "与愤怒明显不符",
                        "suggested_category": "model-invented-category",
                    }
                )
                invalid = await generate_caption(
                    invalid_context,
                    image_path,
                    "fake-vision",
                    category="angry",
                    available_categories={"sigh": "无奈"},
                )
                self.assertEqual(invalid["suggested_category"], "")

        asyncio.run(run())

    def test_conflict_is_moved_to_existing_category_and_marked(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                pack = root / "packs" / "demo"
                angry_dir = pack / "memes" / "angry"
                sigh_dir = pack / "memes" / "sigh"
                angry_dir.mkdir(parents=True)
                sigh_dir.mkdir()
                source = angry_dir / "sweat.jpg"
                source.write_bytes(b"obvious-sweat")
                descriptions = {
                    "angry": "抱怨、批评或激烈反对",
                    "sigh": "表达无奈、无语或感慨",
                }
                (pack / "memes_data.json").write_text(
                    json.dumps(descriptions, ensure_ascii=False), encoding="utf-8"
                )
                context = CategoryVisionContext(
                    {
                        "caption": "额头冒汗，尴尬无奈地不知道如何接话",
                        "tags": ["冒汗", "尴尬", "无奈"],
                        "visible_text": "",
                        "category_fit": "conflict",
                        "category_review_reason": "没有愤怒线索，主要表达尴尬无奈",
                        "suggested_category": "sigh",
                    }
                )
                manager = SemanticTaskManager(
                    root,
                    context=context,
                    config={"vision_provider_id": "fake-vision"},
                )
                await manager.start("demo", mode="caption_only")
                await manager._tasks["demo"]

                moved_path = sigh_dir / "sweat.jpg"
                self.assertFalse(source.exists())
                self.assertTrue(moved_path.is_file())
                current = load_metadata(pack)
                self.assertEqual(len(current["images"]), 1)
                item = next(iter(current["images"].values()))
                self.assertEqual(item["category"], "sigh")
                self.assertEqual(item["tags"][0], "category:sigh")
                self.assertNotIn("category:angry", item["tags"])
                self.assertEqual(item["category_review_status"], "needs_review")
                self.assertEqual(item["reclassification_status"], "auto_reclassified")
                self.assertEqual(item["reclassified_from_category"], "angry")
                self.assertEqual(item["reclassified_to_category"], "sigh")
                self.assertIn("尴尬无奈", item["reclassification_reason"])
                self.assertEqual(len(item["reclassification_history"]), 1)
                self.assertEqual(manager.status("demo")["reclassified_items"], 1)

                overview = get_category_review_overview(pack)
                self.assertEqual(overview["statistics"]["reclassified"], 1)
                self.assertEqual(len(metadata_items(pack, "reclassified")), 1)
                self.assertEqual(
                    overview["items"][0]["reclassification_status"],
                    "auto_reclassified",
                )

                context.payload = {
                    "caption": "当前分类下确实表达无奈",
                    "tags": ["无奈", "叹气"],
                    "visible_text": "",
                    "category_fit": "match",
                    "category_review_reason": "",
                    "suggested_category": "",
                }
                await manager.start("demo", mode="caption_only", force=True)
                await manager._tasks["demo"]
                refreshed = next(iter(load_metadata(pack)["images"].values()))
                self.assertEqual(refreshed["category"], "sigh")
                self.assertEqual(refreshed["category_review_status"], "needs_review")
                self.assertEqual(
                    refreshed["reclassification_status"], "auto_reclassified"
                )
                confirmed = confirm_image_category(pack, moved_path)
                self.assertEqual(
                    confirmed["category_review_status"], "manual_confirmed"
                )
                self.assertEqual(
                    confirmed["reclassification_status"], "auto_reclassified"
                )

        asyncio.run(run())

    def test_reclassification_avoids_filename_overwrite_and_skips_manual_item(self):
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            angry_dir = pack / "memes" / "angry"
            sigh_dir = pack / "memes" / "sigh"
            angry_dir.mkdir(parents=True)
            sigh_dir.mkdir()
            (angry_dir / "same.jpg").write_bytes(b"move-me")
            (angry_dir / "manual.jpg").write_bytes(b"manual-content")
            (sigh_dir / "same.jpg").write_bytes(b"keep-target")
            (pack / "memes_data.json").write_text(
                json.dumps({"angry": "愤怒", "sigh": "无奈"}), encoding="utf-8"
            )
            metadata = reconcile_metadata(pack)
            for item in metadata["images"].values():
                if item["category"] != "angry":
                    continue
                item.update(
                    {
                        "caption": "真实含义是无奈",
                        "tags": ["无奈"],
                        "caption_status": "done",
                        "prompt_version": PROMPT_VERSION,
                        "category_fit": "conflict",
                        "category_review_status": "needs_review",
                        "category_review_reason": "没有愤怒证据",
                        "category_review_context_hash": item["category_context_hash"],
                        "suggested_category": "sigh",
                    }
                )
                if item["relative_path"].endswith("manual.jpg"):
                    item["manual_override"] = True
                    item["manual_tags"] = ["用户标签"]
                    item["provenance"] = "manual"
            save_metadata(pack, metadata)
            current = load_metadata(pack)
            result = apply_conflict_reclassifications(pack, current)

            self.assertEqual(result["moved"], 1)
            self.assertEqual(result["skipped"], 1)
            self.assertEqual((sigh_dir / "same.jpg").read_bytes(), b"keep-target")
            self.assertEqual((sigh_dir / "same_1.jpg").read_bytes(), b"move-me")
            self.assertTrue((angry_dir / "manual.jpg").is_file())
            records = load_metadata(pack)["images"].values()
            manual = next(
                item for item in records if item["relative_path"].endswith("manual.jpg")
            )
            self.assertEqual(manual["category"], "angry")
            self.assertTrue(manual["manual_override"])

    def test_reclassification_merges_same_content_target_without_stale_entry(self):
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            angry_dir = pack / "memes" / "angry"
            sigh_dir = pack / "memes" / "sigh"
            angry_dir.mkdir(parents=True)
            sigh_dir.mkdir()
            (angry_dir / "source.png").write_bytes(b"same-content")
            (sigh_dir / "existing.png").write_bytes(b"same-content")
            (pack / "memes_data.json").write_text(
                json.dumps({"angry": "愤怒", "sigh": "无奈"}), encoding="utf-8"
            )
            metadata = reconcile_metadata(pack)
            self.assertEqual(metadata["unique_total"], 2)
            angry_item = next(
                item
                for item in metadata["images"].values()
                if item["category"] == "angry"
            )
            angry_item.update(
                {
                    "caption": "冒汗无奈",
                    "tags": ["无奈"],
                    "caption_status": "done",
                    "prompt_version": PROMPT_VERSION,
                    "category_fit": "conflict",
                    "category_review_status": "needs_review",
                    "category_review_reason": "没有愤怒证据",
                    "category_review_context_hash": angry_item["category_context_hash"],
                    "suggested_category": "sigh",
                }
            )
            save_metadata(pack, metadata)
            current = load_metadata(pack)
            result = apply_conflict_reclassifications(pack, current)

            self.assertEqual(result["moved"], 1)
            merged = load_metadata(pack)
            self.assertEqual(len(merged["images"]), 1)
            self.assertEqual(merged["unique_total"], 1)
            self.assertEqual(merged["file_total"], 2)
            self.assertEqual(merged["reused_duplicate_files"], 1)
            item = next(iter(merged["images"].values()))
            self.assertEqual(item["category"], "sigh")
            self.assertEqual(item["reclassification_status"], "auto_reclassified")
            overview = get_category_review_overview(pack)
            self.assertEqual(overview["statistics"]["reclassified"], 2)

    def test_task_persists_conflict_review_reason_and_fixed_tag(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                pack = root / "packs" / "demo"
                image_dir = pack / "memes" / "foo"
                image_dir.mkdir(parents=True)
                (image_dir / "one.png").write_bytes(b"conflict-task")
                (pack / "memes_data.json").write_text(
                    json.dumps({"foo": "自嘲或缓和气氛"}), encoding="utf-8"
                )
                context = CategoryVisionContext(
                    {
                        "caption": "严肃指责对方，语气非常强硬",
                        "tags": ["指责", "强硬"],
                        "visible_text": "别装了",
                        "category_fit": "conflict",
                        "category_review_reason": "文字是在明确指责对方",
                    }
                )
                manager = SemanticTaskManager(
                    root,
                    context=context,
                    config={"vision_provider_id": "fake-vision"},
                )
                await manager.start("demo", mode="caption_only")
                await manager._tasks["demo"]
                item = next(iter(load_metadata(pack)["images"].values()))
                self.assertEqual(item["category_review_status"], "needs_review")
                self.assertIn("文字是在明确指责对方", item["category_review_reason"])
                self.assertEqual(item["tags"][0], "category:needs_review")
                self.assertNotIn("category:foo", item["tags"])
                self.assertEqual(item["caption"], "严肃指责对方，语气非常强硬")
                self.assertEqual(item["reclassification_status"], "moved_to_review")
                self.assertEqual(item["reclassified_from_category"], "foo")
                self.assertEqual(item["reclassified_to_category"], "needs_review")
                self.assertFalse((image_dir / "one.png").exists())
                self.assertTrue((pack / "memes" / "needs_review" / "one.png").is_file())
                descriptions = json.loads(
                    (pack / "memes_data.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    descriptions["needs_review"], REVIEW_CATEGORY_DESCRIPTION
                )
                manifest = json.loads(
                    (pack / "manifest.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    manifest["categories"]["needs_review"]["description"],
                    REVIEW_CATEGORY_DESCRIPTION,
                )
                self.assertIn("自嘲或缓和气氛", context.requests[0]["prompt"])

        asyncio.run(run())

    def test_fixed_category_tag_is_first_and_model_tags_are_preserved(self):
        tags = ensure_category_tag(
            ["category:happy", "开心", "分类:庆祝", "category:sad"], "sad"
        )
        self.assertEqual(tags[0], "category:sad")
        self.assertIn("category:happy", tags[1:])
        self.assertIn("分类:庆祝", tags[1:])
        self.assertEqual(tags.count("category:sad"), 1)
        vector_text = build_semantic_text("难过", tags, "", "sad", "悲伤和失落")
        self.assertEqual(vector_text.count("category:sad"), 1)
        with self.assertRaisesRegex(ValueError, "category_fit 无效"):
            parse_caption_result_with_review(
                {
                    "caption": "模型不能自行确认",
                    "tags": ["越权"],
                    "visible_text": "",
                    "category_fit": "manual_confirmed",
                    "category_review_reason": "",
                }
            )

    def test_move_rename_and_description_change_invalidate_confirmation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_dir = root / "memes" / "foo"
            source_dir.mkdir(parents=True)
            image = source_dir / "one.png"
            image.write_bytes(b"same")
            (root / "memes_data.json").write_text(
                json.dumps({"foo": "自嘲或缓和气氛", "sleep": "困倦想睡"}),
                encoding="utf-8",
            )
            metadata = reconcile_metadata(root)
            first_item = next(iter(metadata["images"].values()))
            first_item.update(
                {
                    "caption": "旧描述",
                    "tags": ["旧标签"],
                    "caption_status": "done",
                    "embedding_status": "done",
                }
            )
            save_metadata(root, metadata)
            confirmed = confirm_image_category(root, image)
            self.assertEqual(confirmed["category_review_status"], "manual_confirmed")

            target_dir = root / "memes" / "sleep"
            target_dir.mkdir()
            moved_image = target_dir / image.name
            image.rename(moved_image)
            moved = reconcile_metadata(root)
            self.assertEqual(len(moved["images"]), 1)
            moved_item = next(iter(moved["images"].values()))
            self.assertEqual(moved_item["tags"][0], "category:sleep")
            self.assertNotIn("category:foo", moved_item["tags"])
            self.assertEqual(moved_item["category_review_status"], "unchecked")
            self.assertEqual(moved_item["caption_status"], "pending")
            self.assertEqual(moved_item["embedding_status"], "pending")
            self.assertFalse(moved_item["manual_confirmation_context_hash"])
            save_metadata(root, moved)

            renamed_dir = root / "memes" / "rest"
            target_dir.rename(renamed_dir)
            (root / "memes_data.json").write_text(
                json.dumps({"rest": "困倦想睡"}), encoding="utf-8"
            )
            renamed = reconcile_metadata(root)
            renamed_item = next(iter(renamed["images"].values()))
            self.assertEqual(renamed_item["tags"][0], "category:rest")
            self.assertEqual(renamed_item["embedding_status"], "pending")

            renamed_item.update(
                {
                    "caption": "保留的描述",
                    "tags": ["困倦"],
                    "caption_status": "done",
                    "embedding_status": "done",
                }
            )
            save_metadata(root, renamed)
            confirm_image_category(root, renamed_dir / "one.png")
            (root / "memes_data.json").write_text(
                json.dumps({"rest": "休息、暂停工作和恢复精力"}), encoding="utf-8"
            )
            changed = reconcile_metadata(root)
            changed_item = next(iter(changed["images"].values()))
            self.assertEqual(changed_item["category_review_status"], "unchecked")
            self.assertEqual(changed_item["caption_status"], "pending")
            self.assertEqual(changed_item["embedding_status"], "pending")

    def test_same_content_in_different_legacy_categories_has_independent_entries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for category in ("foo", "givemoney"):
                directory = root / "memes" / category
                directory.mkdir(parents=True)
                (directory / f"{category}.png").write_bytes(b"duplicate")
            metadata = reconcile_metadata(root)
            self.assertEqual(len(metadata["images"]), 2)
            self.assertEqual(metadata["content_unique_total"], 1)
            tags = {item["tags"][0] for item in metadata["images"].values()}
            self.assertEqual(tags, {"category:foo", "category:givemoney"})
            self.assertEqual(len(set(metadata["images"])), 2)

    def test_new_cross_category_duplicate_does_not_inherit_manual_protection(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                pack = root / "packs" / "demo"
                foo_dir = pack / "memes" / "foo"
                foo_dir.mkdir(parents=True)
                foo_image = foo_dir / "foo.png"
                foo_image.write_bytes(b"duplicate-manual")
                metadata = reconcile_metadata(pack)
                foo_item = next(iter(metadata["images"].values()))
                foo_item.update(
                    {
                        "caption": "foo 分类的人工描述",
                        "tags": ["人工标签"],
                        "manual_tags": ["人工标签"],
                        "manual_override": True,
                        "provenance": "manual",
                        "caption_status": "done",
                    }
                )
                save_metadata(pack, metadata)
                confirm_image_category(pack, foo_image)

                give_dir = pack / "memes" / "givemoney"
                give_dir.mkdir()
                (give_dir / "give.png").write_bytes(b"duplicate-manual")
                reconciled = reconcile_metadata(pack)
                give_item = next(
                    item
                    for item in reconciled["images"].values()
                    if item["category"] == "givemoney"
                )
                self.assertFalse(give_item["manual_override"])
                self.assertEqual(give_item["manual_tags"], [])
                self.assertEqual(give_item["provenance"], "ai")
                self.assertEqual(give_item["category_review_status"], "unchecked")
                save_metadata(pack, reconciled)

                context = CategoryVisionContext(
                    {
                        "caption": "按新分类生成的描述",
                        "tags": ["新分类标签"],
                        "visible_text": "",
                        "category_fit": "match",
                        "category_review_reason": "",
                    }
                )
                manager = SemanticTaskManager(
                    root,
                    context=context,
                    config={"vision_provider_id": "fake-vision"},
                )
                await manager.start("demo", mode="caption_only")
                await manager._tasks["demo"]
                current = load_metadata(pack)
                by_category = {
                    item["category"]: item for item in current["images"].values()
                }
                self.assertEqual(by_category["foo"]["caption"], "foo 分类的人工描述")
                self.assertEqual(
                    by_category["foo"]["category_review_status"],
                    "manual_confirmed",
                )
                self.assertTrue(
                    by_category["givemoney"]["caption"].endswith("按新分类生成的描述")
                )
                self.assertEqual(
                    by_category["givemoney"]["tags"],
                    ["category:givemoney", "新分类标签"],
                )

        asyncio.run(run())

    def test_webp_image_receives_category_review_record(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image_dir = root / "memes" / "foo"
            image_dir.mkdir(parents=True)
            (image_dir / "one.webp").write_bytes(b"webp-content")
            metadata = reconcile_metadata(root)
            self.assertEqual(len(metadata["images"]), 1)
            item = next(iter(metadata["images"].values()))
            self.assertEqual(item["tags"][0], "category:foo")
            save_metadata(root, metadata)
            overview = get_category_review_overview(root)
            self.assertTrue(overview["available"])
            self.assertEqual(overview["statistics"]["unchecked"], 1)

    def test_unsemanticized_pack_has_no_category_review_entries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image_dir = root / "memes" / "foo"
            image_dir.mkdir(parents=True)
            (image_dir / "one.png").write_bytes(b"legacy-image")
            (root / "semantic_metadata.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "pack_id": "legacy",
                        "images": {},
                    }
                ),
                encoding="utf-8",
            )

            overview = get_category_review_overview(root)

            self.assertFalse(overview["available"])
            self.assertEqual(overview["semantic_status"], "none")
            self.assertEqual(overview["items"], [])
            self.assertEqual(overview["statistics"]["total"], 0)
            self.assertEqual(overview["statistics"]["unchecked"], 0)

    def test_manual_confirmation_is_saved_and_homepage_assets_expose_review_controls(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image_dir = root / "memes" / "foo"
            image_dir.mkdir(parents=True)
            image = image_dir / "one.png"
            image.write_bytes(b"one")
            metadata = reconcile_metadata(root)
            item = next(iter(metadata["images"].values()))
            item.update(
                {
                    "caption": "自嘲缓和气氛",
                    "tags": ["自嘲"],
                    "caption_status": "done",
                }
            )
            save_metadata(root, metadata)
            confirm_image_category(root, image)
            overview = get_category_review_overview(root)
            self.assertEqual(overview["statistics"]["manual_confirmed"], 1)
            self.assertEqual(
                overview["items"][0]["category_review_status"], "manual_confirmed"
            )

        page = Path("pages/a_manage/index.html").read_text(encoding="utf-8")
        script = Path("pages/a_manage/script.js").read_text(encoding="utf-8")
        semantic_page = Path("pages/semantic/index.html").read_text(encoding="utf-8")
        semantic_script = Path("pages/semantic/script.js").read_text(encoding="utf-8")
        self.assertIn('data-review-filter="needs_review"', page)
        self.assertIn('data-review-filter="reclassified"', page)
        self.assertIn('id="semantic-review-toolbar"', page)
        self.assertIn('class="semantic-review-toolbar hidden"', page)
        self.assertIn("image-preview-category-confirm-btn", page)
        self.assertIn("image-preview-reclassification", page)
        self.assertIn("semantic/confirm_category", script)
        self.assertIn("semanticReviewAvailable", script)
        self.assertIn("if (semanticReviewAvailable && review)", script)
        self.assertIn("reclassification_status", script)
        self.assertIn('value="reclassified"', semantic_page)
        self.assertIn("自动重分类：", semantic_script)

    def test_v1_metadata_is_discarded_for_clean_rebuild(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image_dir = root / "memes" / "foo"
            image_dir.mkdir(parents=True)
            image = image_dir / "one.png"
            image.write_bytes(b"legacy")
            digest = file_sha256(image)
            (root / "semantic_metadata.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "images": {
                            digest: {
                                "content_sha256": digest,
                                "relative_path": "memes/foo/one.png",
                                "category": "foo",
                                "caption": "旧版人工描述仍需保留",
                                "tags": ["旧标签"],
                                "caption_status": "done",
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            discarded = load_metadata(root)
            self.assertEqual(discarded["schema_version"], "2.0")
            self.assertEqual(discarded["images"], {})
            self.assertTrue(discarded["legacy_semantic_data_discarded"])
            self.assertTrue(discarded["requires_local_index_rebuild"])

            rebuilt = reconcile_metadata(root)
            self.assertEqual(len(rebuilt["images"]), 1)
            item = next(iter(rebuilt["images"].values()))
            self.assertEqual(item["caption"], "")
            self.assertEqual(item["tags"][0], "category:foo")
            self.assertEqual(item["category_review_status"], "unchecked")
            self.assertEqual(item["caption_status"], "pending")

    def test_v2_portable_semantics_keep_category_review_but_reset_vectors(self):
        with (
            tempfile.TemporaryDirectory() as source_temp,
            tempfile.TemporaryDirectory() as target_temp,
        ):
            source = Path(source_temp)
            target = Path(target_temp)
            for root in (source, target):
                image_dir = root / "memes" / "foo"
                image_dir.mkdir(parents=True)
                (image_dir / "one.png").write_bytes(b"portable-v2")
                (root / "memes_data.json").write_text(
                    json.dumps({"foo": "自嘲并缓和气氛"}), encoding="utf-8"
                )

            metadata = reconcile_metadata(source)
            item = next(iter(metadata["images"].values()))
            item.update(
                {
                    "caption": "以自嘲缓和聊天气氛",
                    "tags": ["自嘲", "缓和气氛"],
                    "caption_status": "done",
                    "embedding_status": "done",
                }
            )
            save_metadata(source, metadata)
            confirm_image_category(source, source / "memes" / "foo" / "one.png")
            confirmed_metadata = load_metadata(source)
            confirmed_item = next(iter(confirmed_metadata["images"].values()))
            confirmed_item.update(
                {
                    "reclassification_status": "auto_reclassified",
                    "reclassified_from_category": "angry",
                    "reclassified_to_category": "foo",
                    "reclassification_reason": "原分类与画面明显不符",
                    "reclassified_at": "2026-07-23T00:00:00+00:00",
                    "reclassification_history": [
                        {
                            "from_category": "angry",
                            "to_category": "foo",
                            "reason": "原分类与画面明显不符",
                            "status": "auto_reclassified",
                            "at": "2026-07-23T00:00:00+00:00",
                        }
                    ],
                }
            )
            save_metadata(source, confirmed_metadata)

            portable = reset_local_embedding_state(load_metadata(source))
            imported = reconcile_metadata(target, external_data=portable)
            imported_item = next(iter(imported["images"].values()))
            self.assertEqual(imported["schema_version"], "2.0")
            self.assertEqual(imported_item["caption"], "以自嘲缓和聊天气氛")
            self.assertEqual(
                imported_item["tags"],
                ["category:foo", "自嘲", "缓和气氛"],
            )
            self.assertEqual(
                imported_item["category_review_status"], "manual_confirmed"
            )
            self.assertEqual(imported_item["embedding_status"], "pending")
            self.assertEqual(
                imported_item["reclassification_status"], "auto_reclassified"
            )
            self.assertEqual(len(imported_item["reclassification_history"]), 1)
            self.assertTrue(imported["requires_local_index_rebuild"])

    def test_one_click_classification_does_not_overwrite_manual_content(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                pack = root / "packs" / "demo"
                image_dir = pack / "memes" / "foo"
                image_dir.mkdir(parents=True)
                (image_dir / "one.png").write_bytes(b"manual")
                metadata = reconcile_metadata(pack)
                item = next(iter(metadata["images"].values()))
                item.update(
                    {
                        "caption": "用户亲自修正的描述",
                        "tags": ["用户标签"],
                        "manual_tags": ["用户标签"],
                        "manual_override": True,
                        "provenance": "manual",
                        "caption_status": "pending",
                    }
                )
                save_metadata(pack, metadata)
                context = CategoryVisionContext(
                    {
                        "caption": "模型想覆盖的描述",
                        "tags": ["模型标签"],
                        "visible_text": "",
                        "category_fit": "match",
                        "category_review_reason": "",
                    }
                )
                manager = SemanticTaskManager(
                    root,
                    context=context,
                    config={"vision_provider_id": "fake-vision"},
                )
                await manager.start("demo", mode="caption_only")
                await manager._tasks["demo"]
                current = next(iter(load_metadata(pack)["images"].values()))
                self.assertEqual(current["caption"], "用户亲自修正的描述")
                self.assertEqual(current["tags"], ["category:foo", "用户标签"])
                self.assertEqual(current["category_review_status"], "auto_match")

        asyncio.run(run())

    def test_one_click_refresh_keeps_manual_confirmation(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                pack = root / "packs" / "demo"
                image_dir = pack / "memes" / "foo"
                image_dir.mkdir(parents=True)
                image = image_dir / "one.png"
                image.write_bytes(b"old-ai")
                metadata = reconcile_metadata(pack)
                item = next(iter(metadata["images"].values()))
                item.update(
                    {
                        "caption": "旧版 AI 描述",
                        "tags": ["旧标签"],
                        "caption_status": "done",
                        "prompt_version": "meme-semantic-v6",
                    }
                )
                save_metadata(pack, metadata)
                confirm_image_category(pack, image)
                context = CategoryVisionContext(
                    {
                        "caption": "新版分类感知描述",
                        "tags": ["新版标签"],
                        "visible_text": "",
                        "category_fit": "match",
                        "category_review_reason": "",
                    }
                )
                manager = SemanticTaskManager(
                    root,
                    context=context,
                    config={"vision_provider_id": "fake-vision"},
                )
                await manager.start("demo", mode="caption_only")
                await manager._tasks["demo"]
                current = next(iter(load_metadata(pack)["images"].values()))
                self.assertEqual(current["category_review_status"], "manual_confirmed")
                self.assertTrue(current["caption"].endswith("新版分类感知描述"))
                self.assertEqual(current["prompt_version"], PROMPT_VERSION)
                self.assertEqual(len(context.requests), 1)

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
                self.assertTrue(
                    all(Path(path).suffix == ".png" for path in visual_paths)
                )
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
                (3, 3, 0),
            )
            save_metadata(root, metadata)
            (root / "memes" / "a" / "one.png").rename(
                root / "memes" / "a" / "moved.png"
            )
            moved = reconcile_metadata(root)
            self.assertEqual(len(moved["images"]), 3)
            self.assertIn(
                "moved.png",
                {
                    item["relative_path"].split("/")[-1]
                    for item in moved["images"].values()
                },
            )

            moved_entry_id = next(
                entry_id
                for entry_id, item in moved["images"].items()
                if item["relative_path"].endswith("moved.png")
            )
            moved_digest = moved["images"][moved_entry_id]["content_sha256"]
            moved["images"][moved_entry_id]["caption_status"] = "failed"
            moved["images"][moved_entry_id]["caption"] = ""
            moved["images"][moved_entry_id]["tags"] = []
            save_metadata(root, moved)
            imported = reconcile_metadata(
                root,
                external_data={
                    "schema_version": "2.0",
                    "images": {
                        moved_entry_id.upper(): {
                            "content_sha256": moved_digest.upper(),
                            "category": "a",
                            "caption": "从外部记录复用",
                            "tags": ["外部语义"],
                        }
                    },
                },
            )
            self.assertEqual(
                imported["images"][moved_entry_id]["caption"], "从外部记录复用"
            )
            self.assertEqual(
                imported["images"][moved_entry_id]["caption_status"], "done"
            )

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
                    "schema_version": "2.0",
                    "images": {
                        digest: {
                            "content_sha256": digest,
                            "relative_path": str(outside_image),
                            "caption": "外部记录",
                            "tags": ["测试"],
                        }
                    },
                },
            )
            self.assertEqual(metadata["file_total"], 1)
            self.assertEqual(metadata["unique_total"], 0)
            missing_item = next(
                item
                for item in metadata["images"].values()
                if item.get("content_sha256") == digest
            )
            self.assertEqual(missing_item["relative_path"], "")

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
                    mark_category_reviewed(item)
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

                incomplete_vectors = load_metadata(pack)
                first_digest = next(iter(incomplete_vectors["images"]))
                incomplete_vectors["images"][first_digest]["embedding_status"] = (
                    "failed"
                )
                save_metadata(pack, incomplete_vectors)
                blocked = await search_memes(
                    pack, root, "demo", "我有点心虚", fake_embedding
                )
                self.assertEqual(blocked["candidates"], [])
                self.assertIn("尚未完成100%语义化", blocked["reason"])
                incomplete_vectors["images"][first_digest]["embedding_status"] = "done"
                save_metadata(pack, incomplete_vectors)

                # Replacing content without changing the file count must still
                # disable semantic search immediately.
                (pack / "memes" / "a" / "one.png").write_bytes(b"new")
                blocked = await search_memes(
                    pack, root, "demo", "我有点心虚", fake_embedding
                )
                self.assertEqual(blocked["candidates"], [])
                self.assertIn("尚未完成100%语义化", blocked["reason"])
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
                        first_digest: mark_category_reviewed(
                            SemanticImage(
                                first_digest,
                                "memes/a/one.png",
                                caption="心虚装傻一号",
                                tags=["心虚"],
                                caption_status="done",
                            ).to_dict()
                        ),
                        second_digest: mark_category_reviewed(
                            SemanticImage(
                                second_digest,
                                "memes/a/two.png",
                                caption="心虚装傻二号",
                                tags=["心虚"],
                                caption_status="done",
                            ).to_dict()
                        ),
                    },
                }
                save_metadata(pack, metadata)
                provider = FakeEmbedding()
                adapter = EmbeddingAdapter(provider)
                await build_index(pack, root, "demo", adapter)

                result = await search_index(
                    root,
                    "demo",
                    "我有点心虚，碰撞测试",
                    adapter,
                    load_metadata(pack),
                    top_k=1,
                    min_score=-1,
                )
                self.assertEqual(len(result[0]["id"].removeprefix("meme:")), 12)

                current = load_metadata(pack)
                failed_digest = "b" * 64
                failed_entry_id = semantic_entry_id(failed_digest, "")
                current["images"][failed_entry_id] = mark_category_reviewed(
                    SemanticImage(
                        failed_digest,
                        "",
                        caption="已经生成描述但向量失败",
                        tags=["失败项"],
                        caption_status="done",
                        embedding_status="failed",
                        error="向量生成失败",
                    ).to_dict()
                )
                save_metadata(pack, current)
                current = load_metadata(pack)
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

                first_entry_id = next(
                    entry_id
                    for entry_id, item in current["images"].items()
                    if item.get("content_sha256") == first_digest
                )
                current["images"][first_entry_id]["caption"] = "语义已经改变"
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
            entry_id = next(iter(metadata["images"]))
            digest = metadata["images"][entry_id]["content_sha256"]
            save_metadata(root, metadata)
            event = FakeEvent()
            event.set_extra(
                "meme_manager_semantic_candidates",
                {
                    f"meme:{entry_id[:12]}": {
                        "entry_id": entry_id,
                        "content_sha256": digest,
                    }
                },
            )
            self.assertEqual(
                validate_selected_id(event, f"meme:{entry_id[:12]}", root),
                image.resolve(),
            )
            self.assertIsNone(validate_selected_id(event, "meme:" + "0" * 12, root))
            image.write_bytes(b"replaced")
            self.assertIsNone(
                validate_selected_id(event, f"meme:{entry_id[:12]}", root)
            )


if __name__ == "__main__":
    unittest.main()
