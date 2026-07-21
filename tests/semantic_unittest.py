import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import faiss
from backend.semantic_caption import prepare_visual_inputs
from backend.semantic_index import EmbeddingAdapter, build_index, index_is_ready
from backend.semantic_models import (
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
    load_metadata,
    reconcile_metadata,
    safe_relative_path,
    save_metadata,
)
from backend.semantic_task import SemanticTaskManager
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


class SemanticMvpTest(unittest.TestCase):
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
                self.assertEqual(provider.single_calls, 0)
                index_path = root / "semantic_indexes" / "demo" / "index.faiss"
                self.assertEqual(faiss.read_index(str(index_path)).ntotal, 1)

        asyncio.run(run())

    def test_task_manager_uses_core_embedding_provider(self):
        provider = FakeEmbedding()
        with tempfile.TemporaryDirectory() as temp:
            configured = SemanticTaskManager(
                temp,
                context=FakeContext(provider),
                config={"embedding_provider_id": "fake-embedding"},
            )
            self.assertIs(configured._resolve_embedding_provider(), provider)

            automatic = SemanticTaskManager(temp, context=FakeContext(provider))
            self.assertIs(automatic._resolve_embedding_provider(), provider)

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

    def test_resume_restarts_stale_running_task(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                manager = SemanticTaskManager(temp)
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

    def test_gif_uses_first_middle_and_last_frames(self):
        with tempfile.TemporaryDirectory() as temp:
            gif_path = Path(temp) / "animated.gif"
            colors = [
                (255, 0, 0),
                (0, 255, 0),
                (0, 0, 255),
                (255, 255, 255),
                (0, 0, 0),
            ]
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
                self.assertEqual(len(visual_paths), 3)
                sampled_colors = []
                for path in visual_paths:
                    with Image.open(path) as sampled:
                        sampled_colors.append(sampled.convert("RGB").getpixel((0, 0)))
                self.assertEqual(sampled_colors, [colors[0], colors[2], colors[4]])
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
