import asyncio
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image as PILImage

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR.parent))

from astrbot_plugin_meme_manager.backend import auto_collect
from astrbot_plugin_meme_manager.backend.semantic_caption import prepare_visual_inputs


def png_bytes(color=(255, 0, 0)) -> bytes:
    output = io.BytesIO()
    PILImage.new("RGB", (8, 8), color=color).save(output, format="PNG")
    return output.getvalue()


class DummyMutationManager:
    def begin_external_pack_operation(self, _pack_id, _operation):
        return None

    def end_external_pack_operation(self, _pack_id):
        return None


class DummyPlugin:
    semantic_enabled = True

    def __init__(self):
        self.semantic_task_manager = DummyMutationManager()
        self.reload_count = 0

    async def reload_emotions(self):
        self.reload_count += 1


class DummyEvent:
    def __init__(
        self,
        source_id: str,
        *,
        private: bool = False,
        image_path: Path | None = None,
        image_paths: list[Path] | None = None,
        raw_image_data: list[dict] | None = None,
    ):
        self.source_id = source_id
        self.private = private
        self.unified_msg_origin = (
            f"test:FriendMessage:{source_id}"
            if private
            else f"test:GroupMessage:{source_id}"
        )
        paths = image_paths or [image_path]
        self.message_obj = SimpleNamespace(
            message=[
                auto_collect.Image(
                    file=(
                        path.resolve().as_uri()
                        if path is not None
                        else "https://example.com/meme.png"
                    )
                )
                for path in paths
            ]
        )
        if raw_image_data is not None:
            self.message_obj.raw_message = {
                "message": [{"type": "image", "data": data} for data in raw_image_data]
            }

    def is_private_chat(self):
        return self.private

    def get_sender_id(self):
        return self.source_id if self.private else "member"

    def get_group_id(self):
        return "" if self.private else self.source_id

    def get_session_id(self):
        return self.source_id


class AutoCollectConfigurationTests(unittest.TestCase):
    def test_schema_exposes_provider_dropdown_and_requested_defaults(self):
        schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text("utf-8"))
        config = schema["auto_collect"]["items"]

        self.assertEqual(config["vision_provider_id"]["_special"], "select_provider")
        self.assertEqual(config["scope"]["type"], "list")
        self.assertEqual(config["scope"]["default"], [])
        self.assertEqual(config["sampling_probability"]["default"], 100)
        self.assertEqual(config["cooldown_seconds"]["default"], 20)
        self.assertNotIn("allow_animated", config)

    def test_semantic_page_contains_conditional_auto_inbox(self):
        html = (PLUGIN_DIR / "pages/semantic/index.html").read_text("utf-8")
        script = (PLUGIN_DIR / "pages/semantic/script.js").read_text("utf-8")

        self.assertIn('id="auto-inbox-panel"', html)
        self.assertIn("auto-inbox-panel hidden", html)
        self.assertIn("data?.visible", script)
        self.assertIn("semantic/auto-inbox/import", script)
        self.assertIn("semantic/start", script)


class AutoCollectImageTests(unittest.TestCase):
    def test_validates_static_png_without_animation_setting(self):
        self.assertEqual(
            auto_collect.AutoCollectManager._validate_image(png_bytes()), ".png"
        )

    def test_samples_all_frames_from_animated_gif(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "animated.gif"
            frames = [
                PILImage.new("RGB", (8, 8), color=color)
                for color in ("red", "green", "blue")
            ]
            frames[0].save(
                source,
                format="GIF",
                save_all=True,
                append_images=frames[1:],
                duration=50,
                loop=0,
            )
            visual_paths, temporary_paths = prepare_visual_inputs(source)
            try:
                self.assertEqual(len(visual_paths), 3)
                self.assertEqual(visual_paths, temporary_paths)
                self.assertTrue(
                    all(Path(path).suffix == ".png" for path in visual_paths)
                )
            finally:
                for path in temporary_paths:
                    Path(path).unlink(missing_ok=True)


class AutoCollectScopeAndCooldownTests(unittest.IsolatedAsyncioTestCase):
    async def test_scope_accepts_prefixed_group_and_user_ids(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(
                auto_collect, "AUTO_COLLECT_STATE_PATH", Path(temp_dir) / "state.json"
            ):
                manager = auto_collect.AutoCollectManager(
                    DummyPlugin(),
                    {
                        "enabled": True,
                        "vision_provider_id": "vision",
                        "scope": ["group:100", "user:200"],
                    },
                )

            self.assertTrue(manager._source_allowed(DummyEvent("100"))[0])
            self.assertTrue(manager._source_allowed(DummyEvent("200", private=True))[0])
            self.assertFalse(manager._source_allowed(DummyEvent("300"))[0])

    async def test_submit_uses_100_percent_sampling_and_20_second_cooldown(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            packs_dir = root / "packs"
            (packs_dir / "pack-a" / "memes" / "happy").mkdir(parents=True)
            source = root / "source.png"
            source.write_bytes(png_bytes())
            with (
                patch.object(auto_collect, "PACKS_DIR", packs_dir),
                patch.object(auto_collect, "AUTO_COLLECT_TEMP_DIR", root / "queue"),
                patch.object(
                    auto_collect, "AUTO_COLLECT_STATE_PATH", root / "state.json"
                ),
                patch.object(
                    auto_collect,
                    "load_pack_category_mapping",
                    return_value={"happy": "positive reaction"},
                ),
            ):
                manager = auto_collect.AutoCollectManager(
                    DummyPlugin(),
                    {
                        "enabled": True,
                        "vision_provider_id": "vision",
                        "target_pack_id": "pack-a",
                        "sampling_probability": 100,
                        "cooldown_seconds": 20,
                    },
                )
                manager._ready = True
                event = DummyEvent("100", image_path=source)

                self.assertTrue(await manager.submit(event))
                self.assertFalse(await manager.submit(event))
                self.assertEqual(manager.queue.qsize(), 1)
                await manager.close()

    async def test_submit_prefilters_images_with_napcat_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            packs_dir = root / "packs"
            (packs_dir / "pack-a" / "memes" / "happy").mkdir(parents=True)
            regular = root / "regular.png"
            meme = root / "meme.png"
            regular.write_bytes(png_bytes())
            meme.write_bytes(png_bytes((0, 0, 255)))
            cases = [
                ("regular", [{"sub_type": 0, "summary": "[图片]"}], False),
                ("custom", [{"sub_type": "1", "summary": "[动画表情]"}], True),
                ("legacy", [{"sub_type": 11, "summary": "[动画表情]"}], True),
                ("market", [{"emoji_id": "123", "summary": "商城表情"}], True),
                ("fallback", None, True),
                ("mismatched", [], True),
            ]
            with (
                patch.object(auto_collect, "PACKS_DIR", packs_dir),
                patch.object(auto_collect, "AUTO_COLLECT_TEMP_DIR", root / "queue"),
                patch.object(
                    auto_collect, "AUTO_COLLECT_STATE_PATH", root / "state.json"
                ),
                patch.object(
                    auto_collect,
                    "load_pack_category_mapping",
                    return_value={"happy": "positive reaction"},
                ),
            ):
                for name, raw_image_data, expected in cases:
                    with self.subTest(name=name):
                        manager = auto_collect.AutoCollectManager(
                            DummyPlugin(),
                            {
                                "enabled": True,
                                "vision_provider_id": "vision",
                                "target_pack_id": "pack-a",
                                "cooldown_seconds": 0,
                            },
                        )
                        manager._ready = True
                        event = DummyEvent(
                            "100",
                            image_path=meme,
                            raw_image_data=raw_image_data,
                        )

                        self.assertEqual(await manager.submit(event), expected)
                        self.assertEqual(manager.queue.qsize(), int(expected))
                        await manager.close()

    async def test_submit_selects_tagged_image_from_multi_image_message(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            packs_dir = root / "packs"
            (packs_dir / "pack-a" / "memes" / "happy").mkdir(parents=True)
            regular = root / "regular.png"
            meme = root / "meme.png"
            regular.write_bytes(png_bytes())
            meme_content = png_bytes((0, 0, 255))
            meme.write_bytes(meme_content)
            with (
                patch.object(auto_collect, "PACKS_DIR", packs_dir),
                patch.object(auto_collect, "AUTO_COLLECT_TEMP_DIR", root / "queue"),
                patch.object(
                    auto_collect, "AUTO_COLLECT_STATE_PATH", root / "state.json"
                ),
                patch.object(
                    auto_collect,
                    "load_pack_category_mapping",
                    return_value={"happy": "positive reaction"},
                ),
            ):
                manager = auto_collect.AutoCollectManager(
                    DummyPlugin(),
                    {
                        "enabled": True,
                        "vision_provider_id": "vision",
                        "target_pack_id": "pack-a",
                    },
                )
                manager._ready = True
                event = DummyEvent(
                    "100",
                    image_paths=[regular, meme],
                    raw_image_data=[
                        {"sub_type": 0, "summary": "[图片]"},
                        {"sub_type": 1, "summary": "[动画表情]"},
                    ],
                )

                self.assertTrue(await manager.submit(event))
                snapshot_path = next((root / "queue").glob("queued_*"))
                self.assertEqual(snapshot_path.read_bytes(), meme_content)
                await manager.close()

    async def test_worker_uses_snapshot_after_event_file_is_deleted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            packs_dir = root / "packs"
            (packs_dir / "pack-a" / "memes" / "happy").mkdir(parents=True)
            source = root / "event-image.png"
            source.write_bytes(png_bytes())
            with (
                patch.object(auto_collect, "PACKS_DIR", packs_dir),
                patch.object(auto_collect, "AUTO_COLLECT_TEMP_DIR", root / "queue"),
                patch.object(
                    auto_collect, "AUTO_COLLECT_STATE_PATH", root / "state.json"
                ),
                patch.object(
                    auto_collect,
                    "load_pack_category_mapping",
                    return_value={"happy": "positive reaction"},
                ),
            ):
                manager = auto_collect.AutoCollectManager(
                    DummyPlugin(),
                    {
                        "enabled": True,
                        "vision_provider_id": "vision",
                        "target_pack_id": "pack-a",
                    },
                )
                manager._ready = True

                self.assertTrue(
                    await manager.submit(DummyEvent("100", image_path=source))
                )
                snapshot_path = next((root / "queue").glob("queued_*"))
                source.unlink()
                with patch.object(manager, "_pack_contains_digest", return_value=True):
                    manager._worker_task = asyncio.create_task(manager._worker())
                    await manager.queue.join()
                await manager.close()

                self.assertFalse(snapshot_path.exists())

    async def test_close_removes_unprocessed_snapshots(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            packs_dir = root / "packs"
            (packs_dir / "pack-a" / "memes" / "happy").mkdir(parents=True)
            source = root / "event-image.png"
            source.write_bytes(png_bytes())
            with (
                patch.object(auto_collect, "PACKS_DIR", packs_dir),
                patch.object(auto_collect, "AUTO_COLLECT_TEMP_DIR", root / "queue"),
                patch.object(
                    auto_collect, "AUTO_COLLECT_STATE_PATH", root / "state.json"
                ),
                patch.object(
                    auto_collect,
                    "load_pack_category_mapping",
                    return_value={"happy": "positive reaction"},
                ),
            ):
                manager = auto_collect.AutoCollectManager(
                    DummyPlugin(),
                    {
                        "enabled": True,
                        "vision_provider_id": "vision",
                        "target_pack_id": "pack-a",
                    },
                )
                manager._ready = True

                self.assertTrue(
                    await manager.submit(DummyEvent("100", image_path=source))
                )
                snapshot_path = next((root / "queue").glob("queued_*"))
                self.assertTrue(snapshot_path.exists())

                await manager.close()

                self.assertFalse(snapshot_path.exists())
                self.assertTrue(manager.queue.empty())


class AutoCollectInboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_semantic_inbox_stays_separate_until_manual_import(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            packs_dir = root / "packs"
            pack_dir = packs_dir / "pack-a"
            (pack_dir / "memes" / "happy").mkdir(parents=True)
            inbox_dir = root / "inbox"
            inbox_images = inbox_dir / "images"
            inbox_metadata = inbox_dir / "metadata.json"
            state_path = root / "state.json"
            plugin = DummyPlugin()
            content = png_bytes()
            digest = auto_collect.hashlib.sha256(content).hexdigest()
            with (
                patch.object(auto_collect, "PACKS_DIR", packs_dir),
                patch.object(auto_collect, "AUTO_COLLECT_INBOX_DIR", inbox_dir),
                patch.object(
                    auto_collect, "AUTO_COLLECT_INBOX_IMAGES_DIR", inbox_images
                ),
                patch.object(
                    auto_collect,
                    "AUTO_COLLECT_INBOX_METADATA_PATH",
                    inbox_metadata,
                ),
                patch.object(auto_collect, "AUTO_COLLECT_STATE_PATH", state_path),
                patch.object(
                    auto_collect,
                    "get_pack_paths",
                    side_effect=lambda pack_id: {
                        "pack_dir": packs_dir / pack_id,
                        "memes_dir": packs_dir / pack_id / "memes",
                    },
                ),
                patch.object(
                    auto_collect,
                    "load_pack_category_mapping",
                    return_value={"happy": "positive reaction"},
                ),
                patch.object(auto_collect, "invalidate_semantic_metadata"),
            ):
                manager = auto_collect.AutoCollectManager(
                    plugin,
                    {"enabled": True, "vision_provider_id": "vision"},
                )
                job = auto_collect.AutoCollectJob(
                    snapshot_path=root / "unused.png",
                    target_pack_id="pack-a",
                    categories={"happy": "positive reaction"},
                    source_kind="group",
                    source_id="100",
                )
                await manager._save_to_inbox(
                    job,
                    content,
                    digest,
                    ".png",
                    "happy",
                    {"is_meme": True, "meme_confidence": 1.0},
                )

                self.assertFalse(any((pack_dir / "memes" / "happy").iterdir()))
                pending = await manager.pending_status("pack-a")
                self.assertTrue(pending["visible"])
                self.assertEqual(pending["count"], 1)

                result = await manager.import_pending("pack-a")

                self.assertEqual(result["imported"], 1)
                self.assertEqual(plugin.reload_count, 1)
                self.assertEqual(
                    len(list((pack_dir / "memes" / "happy").glob("*.png"))), 1
                )
                self.assertEqual((await manager.pending_status("pack-a"))["count"], 0)


if __name__ == "__main__":
    unittest.main()
