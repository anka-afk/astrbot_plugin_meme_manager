import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from astrbot_plugin_meme_manager.backend import auto_collect
from astrbot_plugin_meme_manager.backend.semantic.caption import prepare_visual_inputs
from PIL import Image as PILImage

PLUGIN_DIR = Path(__file__).resolve().parents[1]


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
        message_id: str = "",
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
            message_id=message_id,
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
        self.assertEqual(config["max_images_per_message"]["default"], 1)
        self.assertEqual(config["max_images_per_message"]["slider"]["max"], 10)
        self.assertEqual(config["cooldown_seconds"]["default"], 20)
        self.assertNotIn("allow_animated", config)

    def test_semantic_page_contains_conditional_auto_inbox(self):
        html = (PLUGIN_DIR / "pages/app/semantic/index.html").read_text("utf-8")
        script = (PLUGIN_DIR / "pages/app/semantic/script.js").read_text("utf-8")

        self.assertIn('id="auto-inbox-panel"', html)
        self.assertIn("auto-inbox-panel hidden", html)
        self.assertIn("data?.visible", script)
        self.assertNotIn("semantic/auto-inbox/import", script)


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


class AutoCollectSubmissionTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_submit_prioritizes_marked_images_and_observes_others(self):
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
                ("fallback", None, False),
                ("mismatched", [], False),
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
                patch.object(auto_collect.random, "random", return_value=1.0),
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

    async def test_submit_collects_tagged_images_up_to_message_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            packs_dir = root / "packs"
            (packs_dir / "pack-a" / "memes" / "happy").mkdir(parents=True)
            regular = root / "regular.png"
            meme = root / "meme.png"
            another_meme = root / "another-meme.png"
            extra_meme = root / "extra-meme.png"
            regular.write_bytes(png_bytes())
            meme_content = png_bytes((0, 0, 255))
            meme.write_bytes(meme_content)
            another_content = png_bytes((0, 255, 0))
            another_meme.write_bytes(another_content)
            extra_meme.write_bytes(png_bytes((255, 255, 0)))
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
                        "max_images_per_message": 2,
                        "cooldown_seconds": 20,
                    },
                )
                manager._ready = True
                event = DummyEvent(
                    "100",
                    image_paths=[regular, meme, another_meme, extra_meme],
                    raw_image_data=[
                        {"sub_type": 0, "summary": "[图片]"},
                        {"sub_type": 1, "summary": "[动画表情]"},
                        {"sub_type": 1, "summary": "[动画表情]"},
                        {"sub_type": 1, "summary": "[动画表情]"},
                    ],
                )

                self.assertTrue(await manager.submit(event))
                self.assertEqual(manager.queue.qsize(), 2)
                self.assertEqual(manager.counters["message_limit"], 2)
                self.assertEqual(
                    {path.read_bytes() for path in (root / "queue").glob("queued_*")},
                    {meme_content, another_content},
                )
                self.assertFalse(
                    await manager.submit(
                        DummyEvent(
                            "100",
                            image_path=regular,
                            raw_image_data=[{"sub_type": 1}],
                        )
                    )
                )
                self.assertEqual(manager.counters["cooldown"], 1)
                await manager.close()

    async def test_progressive_sampling_counts_messages_and_forces_eighth(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            packs_dir = root / "packs"
            (packs_dir / "pack-a" / "memes" / "happy").mkdir(parents=True)
            source = root / "ordinary.png"
            source.write_bytes(png_bytes())
            with (
                patch.object(auto_collect, "PACKS_DIR", packs_dir),
                patch.object(auto_collect, "AUTO_COLLECT_TEMP_DIR", root / "queue"),
                patch.object(auto_collect, "AUTO_COLLECT_STATE_PATH", root / "state.json"),
                patch.object(
                    auto_collect,
                    "load_pack_category_mapping",
                    return_value={"happy": "positive reaction"},
                ),
                patch.object(auto_collect.random, "random", return_value=1.0) as roll,
            ):
                manager = auto_collect.AutoCollectManager(
                    DummyPlugin(),
                    {
                        "enabled": True,
                        "vision_provider_id": "vision",
                        "target_pack_id": "pack-a",
                        "sampling_probability": 0,
                        "max_images_per_message": 2,
                        "cooldown_seconds": 0,
                    },
                )
                manager._ready = True
                first = DummyEvent(
                    "100",
                    image_paths=[source, source],
                    raw_image_data=[{"sub_type": 0}, {"sub_type": 0}],
                    message_id="message-1",
                )
                self.assertFalse(await manager.submit(first))
                self.assertEqual(manager.counters["same_message_duplicate"], 1)
                self.assertFalse(await manager.submit(first))
                self.assertEqual(manager.counters["redelivered"], 1)
                for count in range(2, 8):
                    self.assertFalse(
                        await manager.submit(
                            DummyEvent(
                                "100",
                                image_path=source,
                                raw_image_data=[{"sub_type": 0}],
                                message_id=f"message-{count}",
                            )
                        )
                    )
                self.assertTrue(
                    await manager.submit(
                        DummyEvent(
                            "100",
                            image_path=source,
                            raw_image_data=[{"sub_type": 0}],
                            message_id="message-8",
                        )
                    )
                )
                self.assertEqual(roll.call_count, 6)
                self.assertEqual(manager.queue.qsize(), 1)
                observation = next(iter(manager._state["observations"].values()))
                self.assertEqual(observation["count"], 8)
                self.assertTrue(observation["triggered"])
                await manager.close()

    async def test_progressive_second_observation_survives_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            packs_dir = root / "packs"
            (packs_dir / "pack-a" / "memes" / "happy").mkdir(parents=True)
            source = root / "ordinary.png"
            source.write_bytes(png_bytes())
            with (
                patch.object(auto_collect, "PACKS_DIR", packs_dir),
                patch.object(auto_collect, "AUTO_COLLECT_TEMP_DIR", root / "queue"),
                patch.object(auto_collect, "AUTO_COLLECT_STATE_PATH", root / "state.json"),
                patch.object(
                    auto_collect,
                    "load_pack_category_mapping",
                    return_value={"happy": "positive reaction"},
                ),
            ):
                config = {
                    "enabled": True,
                    "vision_provider_id": "vision",
                    "target_pack_id": "pack-a",
                    "sampling_probability": 0,
                    "cooldown_seconds": 0,
                }
                manager = auto_collect.AutoCollectManager(DummyPlugin(), config)
                manager._ready = True
                self.assertFalse(
                    await manager.submit(
                        DummyEvent("100", image_path=source, message_id="first")
                    )
                )
                await manager.close()
                manager = auto_collect.AutoCollectManager(DummyPlugin(), config)
                manager._ready = True
                self.assertFalse(
                    await manager.submit(
                        DummyEvent("100", image_path=source, message_id="first")
                    )
                )
                with patch.object(auto_collect.random, "random", return_value=0.0):
                    self.assertTrue(
                        await manager.submit(
                            DummyEvent("100", image_path=source, message_id="second")
                        )
                    )
                self.assertEqual(manager.queue.qsize(), 1)
                observation = next(iter(manager._state["observations"].values()))
                self.assertEqual(observation["count"], 2)
                self.assertTrue(observation["triggered"])
                await manager.close()
                manager = auto_collect.AutoCollectManager(DummyPlugin(), config)
                manager._ready = True
                with patch.object(auto_collect.random, "random") as roll:
                    self.assertTrue(
                        await manager.submit(
                            DummyEvent("100", image_path=source, message_id="third")
                        )
                    )
                    roll.assert_not_called()
                await manager.close()

    async def test_observations_expire_and_have_a_size_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            with (
                patch.object(auto_collect, "AUTO_COLLECT_STATE_PATH", state_path),
                patch.object(auto_collect, "MAX_OBSERVATIONS", 2),
                patch.object(auto_collect.time, "time", return_value=1000000),
            ):
                manager = auto_collect.AutoCollectManager(DummyPlugin(), {})
                self.assertFalse(manager._sample_observation("pack-a:first", "message-1"))
                self.assertFalse(manager._sample_observation("pack-a:second", "message-2"))
                self.assertFalse(manager._sample_observation("pack-a:third", "message-3"))
                self.assertEqual(len(manager._state["observations"]), 2)
                self.assertNotIn("pack-a:first", manager._state["observations"])
            with patch.object(
                auto_collect.time,
                "time",
                return_value=1000000 + auto_collect.OBSERVATION_TTL_SECONDS + 1,
            ):
                manager = auto_collect.AutoCollectManager(DummyPlugin(), {})
                self.assertFalse(manager._sample_observation("pack-a:second", "message-4"))
                self.assertEqual(manager._state["observations"]["pack-a:second"]["count"], 1)
                self.assertEqual(len(manager._state["observations"]), 1)

    async def test_progressive_probability_rises_after_second_observation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(
                auto_collect, "AUTO_COLLECT_STATE_PATH", Path(temp_dir) / "state.json"
            ):
                manager = auto_collect.AutoCollectManager(DummyPlugin(), {})
                with patch.object(
                    auto_collect.random, "random", side_effect=[0.46, 0.50]
                ) as roll:
                    self.assertFalse(manager._sample_observation("pack-a:digest", "first"))
                    self.assertFalse(manager._sample_observation("pack-a:digest", "second"))
                    self.assertTrue(manager._sample_observation("pack-a:digest", "third"))
                    self.assertTrue(manager._sample_observation("pack-a:digest", "fourth"))
                self.assertEqual(roll.call_count, 2)
                self.assertEqual(manager._state["observations"]["pack-a:digest"]["count"], 4)

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
                    await manager.submit(
                        DummyEvent(
                            "100", image_path=source, raw_image_data=[{"sub_type": 1}]
                        )
                    )
                )
                snapshot_path = next((root / "queue").glob("queued_*"))
                self.assertTrue(snapshot_path.exists())

                await manager.close()

                self.assertFalse(snapshot_path.exists())
                self.assertTrue(manager.queue.empty())

if __name__ == "__main__":
    unittest.main()
