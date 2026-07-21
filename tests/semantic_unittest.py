import asyncio
import tempfile
import unittest
from pathlib import Path

from backend.semantic_caption import prepare_visual_inputs
from backend.semantic_index import EmbeddingAdapter, build_index
from backend.semantic_models import parse_caption_result
from backend.semantic_query import search_memes, validate_selected_id
from backend.semantic_storage import reconcile_metadata, save_metadata
from PIL import Image


class FakeEmbedding:
    async def get_embeddings_async(self, texts):
        return [[1.0, 0.0] if "心虚" in text else [0.0, 1.0] for text in texts]

    async def get_embedding_async(self, text):
        return (await self.get_embeddings_async([text]))[0]


class FakeEvent:
    def __init__(self):
        self.extra = {}

    def get_extra(self, key):
        return self.extra.get(key)

    def set_extra(self, key, value):
        self.extra[key] = value


class SemanticMvpTest(unittest.TestCase):
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
                metadata = reconcile_metadata(pack)
                item = next(iter(metadata["images"].values()))
                item.update(
                    {
                        "caption": "我有点心虚想装傻",
                        "tags": ["心虚", "装傻"],
                        "caption_status": "done",
                    }
                )
                save_metadata(pack, metadata)
                await build_index(pack, root, "demo", EmbeddingAdapter(FakeEmbedding()))
                result = await search_memes(
                    pack, root, "demo", "我有点心虚", FakeEmbedding()
                )
                self.assertTrue(result["ok"])
                self.assertTrue(result["candidates"])
                self.assertNotIn("content_sha256", result["candidates"][0])

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
