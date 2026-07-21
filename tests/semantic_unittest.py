import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import faiss
from backend.semantic_caption import prepare_visual_inputs
from backend.semantic_index import EmbeddingAdapter, build_index, index_is_ready
from backend.semantic_models import parse_caption_result
from backend.semantic_query import search_memes, validate_selected_id
from backend.semantic_storage import load_metadata, reconcile_metadata, save_metadata
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
        return self.provider if provider_id == "fake-embedding" else None

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

    def test_caption_result_requires_fine_grained_fields(self):
        caption, tags, visible_text = parse_caption_result(
            '{"caption":"心虚装傻","tags":["心虚","尴尬"],"visible_text":""}'
        )
        self.assertEqual(caption, "心虚装傻")
        self.assertEqual(tags, ["心虚", "尴尬"])
        with self.assertRaises(ValueError):
            parse_caption_result('{"caption":"只有情绪"}')

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


if __name__ == "__main__":
    unittest.main()
