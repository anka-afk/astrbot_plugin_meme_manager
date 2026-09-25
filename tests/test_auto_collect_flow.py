import asyncio
import base64
import io
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot_plugin_meme_manager.backend import auto_collect
from astrbot_plugin_meme_manager.backend.packs import resolver
from astrbot_plugin_meme_manager.backend.semantic.storage import load_metadata
from astrbot_plugin_meme_manager.backend.semantic.task import SemanticTaskManager
from astrbot_plugin_meme_manager.mixins import web_api
from PIL import Image
from quart import Quart


class FlowEvent:
    """Provide the message interface consumed by the real collection manager."""

    def __init__(self, path: Path, sub_type: int = 1):
        """Create a local AstrBot image and its corresponding NapCat segment.

        Args:
            path: Real source image file.
            sub_type: NapCat image classification marker.
        """
        self.unified_msg_origin = "test:GroupMessage:100"
        self.message_obj = SimpleNamespace(
            message=[auto_collect.Image.fromFileSystem(path)],
            raw_message={
                "message": [{"type": "image", "data": {"sub_type": sub_type}}]
            },
        )

    def is_private_chat(self):
        return False

    def get_group_id(self):
        return "100"


class FlowHost(web_api.WebAPIMixin):
    """Host real API handlers and task locks without starting the AstrBot server."""

    def __init__(self, root: Path, context):
        """Initialize the temporary plugin host.

        Args:
            root: Isolated plugin data directory.
            context: Provider registry and mocked external vision transport.
        """
        self.context = context
        self.semantic_enabled = False
        self.semantic_task_manager = SemanticTaskManager(root, context=context)
        self.pack_dir = root / "packs" / "pack-a"
        self.reload_count = 0
        self.loaded_files = []

    async def reload_emotions(self):
        """Observe accepted files when the production manager requests a reload."""
        self.loaded_files = sorted(
            path for path in (self.pack_dir / "memes").rglob("*") if path.is_file()
        )
        self.reload_count += 1


class AutoCollectFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.pack_dir = self.root / "packs" / "pack-a"
        (self.pack_dir / "memes").mkdir(parents=True)
        (self.pack_dir / "memes_data.json").write_text(
            json.dumps({"happy": "positive reaction", "sad": "sad reaction"}),
            encoding="utf-8",
        )
        for name, path in {
            "PACKS_DIR": self.root / "packs",
            "AUTO_COLLECT_INBOX_DIR": self.root / "inbox",
            "AUTO_COLLECT_INBOX_IMAGES_DIR": self.root / "inbox" / "images",
            "AUTO_COLLECT_INBOX_METADATA_PATH": self.root / "inbox" / "metadata.json",
            "AUTO_COLLECT_STATE_PATH": self.root / "state.json",
            "AUTO_COLLECT_TEMP_DIR": self.root / "queue",
        }.items():
            self.stack.enter_context(patch.object(auto_collect, name, path))
        for module in (resolver, web_api):
            self.stack.enter_context(
                patch.object(module, "PACKS_DIR", self.root / "packs")
            )
        self.decision = {
            "is_meme": True,
            "meme_confidence": 0.97,
            "category": "happy",
            "category_confidence": 0.91,
            "reason": "Expresses a reaction",
            "caption": "A cheerful face celebrates good news in a conversation.",
            "tags": ["celebration", "good news"],
            "visible_text": "yay",
        }
        self.visual_requests = []
        self.vision = AsyncMock(side_effect=self.vision_response)
        provider = SimpleNamespace(
            provider_config={"model": "test", "modalities": ["image"]}
        )
        context = SimpleNamespace(
            get_provider_by_id=lambda provider_id: (
                provider if provider_id == "vision" else None
            ),
            llm_generate=self.vision,
        )
        self.host = FlowHost(self.root, context)
        self.config = {
            "enabled": True,
            "vision_provider_id": "vision",
            "target_pack_id": "pack-a",
            "sampling_probability": 100,
            "cooldown_seconds": 0,
            "manual_review": True,
        }
        self.host.auto_collect_manager = auto_collect.AutoCollectManager(
            self.host, self.config
        )
        self.app = Quart(__name__)
        self.app.testing = True
        for suffix, handler, methods in (
            ("", self.host._api_auto_collect_inbox, ["GET"]),
            ("/image", self.host._api_auto_collect_image, ["GET"]),
            ("/image_data", self.host._api_auto_collect_image_data, ["GET"]),
            ("/accept", self.host._api_auto_collect_accept, ["POST"]),
            ("/discard", self.host._api_auto_collect_discard, ["POST"]),
        ):
            self.app.add_url_rule("/inbox" + suffix, view_func=handler, methods=methods)
        self.client = self.app.test_client()
        self.png = self.root / "source.png"
        self.gif = self.root / "source.gif"
        Image.new("RGB", (24, 24), "red").save(self.png)
        Image.new("RGB", (24, 24), "red").save(
            self.gif,
            save_all=True,
            append_images=[Image.new("RGB", (24, 24), "blue")],
            duration=100,
            loop=0,
        )

    async def asyncSetUp(self):
        await self.host.auto_collect_manager.start()

    async def asyncTearDown(self):
        await self.host.auto_collect_manager.close()
        await self.host.semantic_task_manager.close()

    async def vision_response(self, **request):
        """Validate actual visual input before returning deterministic external output.

        Args:
            **request: Real classifier request including temporary frame paths.

        Returns:
            Provider-compatible structured response.
        """
        paths = [Path(path) for path in request["image_urls"]]
        for path in paths:
            with Image.open(path) as image:
                image.verify()
        self.visual_requests.append(paths)
        return SimpleNamespace(completion_text=json.dumps(self.decision))

    async def test_manual_review_previews_png_gif_and_accepts_category_override(self):
        manager = self.host.auto_collect_manager
        original_bytes = {self.png.read_bytes(), self.gif.read_bytes()}
        for source, confidence in ((self.png, 0.72), (self.gif, 0.91)):
            self.decision["category_confidence"] = confidence
            self.assertTrue(await manager.submit(FlowEvent(source)))
            # 模拟平台在提交后删除消息附件。
            source.unlink()
            await asyncio.wait_for(manager.queue.join(), 5)
        self.assertEqual(list((self.root / "queue").iterdir()), [])
        self.assertEqual(self.vision.await_count, 2)
        self.assertEqual([len(paths) for paths in self.visual_requests], [1, 2])
        self.assertTrue(
            all(not path.exists() for paths in self.visual_requests for path in paths)
        )
        self.assertEqual(list((self.pack_dir / "memes").rglob("*")), [])
        response = await self.client.get("/inbox", query_string={"pack_id": "pack-a"})
        self.assertEqual(response.status_code, 200)
        pending = await response.get_json()
        self.assertTrue(pending["visible"])
        self.assertEqual(pending["count"], 2)
        self.assertEqual(
            [item["category_confidence"] for item in pending["items"]], [0.91, 0.72]
        )
        self.assertEqual(pending["categories"]["sad"], "sad reaction")
        preview_mimes = set()
        for item in pending["items"]:
            self.assertEqual(item["suggested_category"], "happy")
            preview = await self.client.get(
                "/inbox/image_data",
                query_string={"pack_id": "pack-a", "id": item["id"]},
            )
            self.assertEqual(preview.status_code, 200)
            encoded = (await preview.get_json())["data_url"]
            preview_bytes = base64.b64decode(encoded.split(",", 1)[1])
            self.assertIn(preview_bytes, original_bytes)
            original = await self.client.get(
                "/inbox/image", query_string={"pack_id": "pack-a", "id": item["id"]}
            )
            self.assertEqual(original.status_code, 200)
            self.assertEqual(await original.get_data(), preview_bytes)
            preview_mimes.add(original.mimetype)
            with Image.open(io.BytesIO(await original.get_data())) as image:
                if original.mimetype == "image/gif":
                    self.assertEqual(image.n_frames, 2)
        self.assertEqual(preview_mimes, {"image/png", "image/gif"})
        accepted = await self.client.post(
            "/inbox/accept",
            json={
                "pack_id": "pack-a",
                "items": [
                    {"id": item["id"], "category": "sad"} for item in pending["items"]
                ],
            },
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(
            await accepted.get_json(), {"imported": 2, "duplicates": 0, "failed": 0}
        )
        self.assertEqual(self.host.reload_count, 1)
        self.assertEqual(
            {path.read_bytes() for path in self.host.loaded_files}, original_bytes
        )
        self.assertTrue(
            all(path.parent.name == "sad" for path in self.host.loaded_files)
        )
        metadata = load_metadata(self.pack_dir)
        self.assertEqual(len(metadata["images"]), 2)
        for item in metadata["images"].values():
            self.assertEqual(item["caption_status"], "pending")
            self.assertEqual(item["auto_caption"], self.decision["caption"])
            self.assertEqual(item["category_review_status"], "manual_confirmed")
            self.assertEqual(item["embedding_status"], "pending")
        after = await self.client.get("/inbox", query_string={"pack_id": "pack-a"})
        self.assertEqual((await after.get_json())["count"], 0)
        self.assertFalse(self.host.semantic_task_manager._index_tasks)

    async def test_duplicate_inflight_and_pending_preserves_cooldown_and_model_quota(
        self,
    ):
        manager = self.host.auto_collect_manager
        manager.cooldown_seconds = 20
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_vision(**request):
            entered.set()
            await release.wait()
            return await self.vision_response(**request)

        self.vision.side_effect = delayed_vision
        self.assertTrue(await manager.submit(FlowEvent(self.png)))
        await asyncio.wait_for(entered.wait(), 5)
        cooldown = dict(manager._cooldowns)
        with patch.object(auto_collect.logger, "info") as log_info:
            self.assertFalse(await manager.submit(FlowEvent(self.png)))
        self.assertTrue(
            any(
                len(call.args) > 1 and call.args[1] == "相同图片正在处理"
                for call in log_info.call_args_list
            )
        )
        self.assertEqual(manager._cooldowns, cooldown)
        release.set()
        await asyncio.wait_for(manager.queue.join(), 5)
        with patch.object(auto_collect.logger, "info") as log_info:
            self.assertFalse(await manager.submit(FlowEvent(self.png)))
        self.assertTrue(
            any(
                len(call.args) > 1 and call.args[1] == "相同图片已在待审箱"
                for call in log_info.call_args_list
            )
        )
        self.assertEqual(manager._cooldowns, cooldown)
        with patch.object(auto_collect.logger, "info") as log_info:
            self.assertFalse(await manager.submit(FlowEvent(self.gif)))
        self.assertTrue(
            any("冷却时间内" in call.args[0] for call in log_info.call_args_list)
        )
        manager._ready = False
        with patch.object(auto_collect.logger, "info") as log_info:
            self.assertFalse(await manager.submit(FlowEvent(self.png)))
        self.assertTrue(
            any("后台任务尚未就绪" in call.args[0] for call in log_info.call_args_list)
        )
        self.assertEqual(manager.counters["duplicate_or_rejected"], 2)
        self.assertEqual(manager.counters["cooldown"], 1)
        self.assertEqual(manager.counters["not_ready"], 1)
        self.assertEqual(self.vision.await_count, 1)
        response = await self.client.get("/inbox", query_string={"pack_id": "pack-a"})
        self.assertEqual((await response.get_json())["count"], 1)

    async def test_rejection_survives_manager_restart(self):
        manager = self.host.auto_collect_manager
        self.assertTrue(await manager.submit(FlowEvent(self.png)))
        await asyncio.wait_for(manager.queue.join(), 5)
        response = await self.client.get("/inbox", query_string={"pack_id": "pack-a"})
        item = (await response.get_json())["items"][0]
        discarded = await self.client.post(
            "/inbox/discard",
            json={"pack_id": "pack-a", "ids": [item["id"]], "remember_rejection": True},
        )
        self.assertEqual(discarded.status_code, 200)
        self.assertEqual(await discarded.get_json(), {"discarded": 1})
        await manager.close()
        manager = auto_collect.AutoCollectManager(self.host, self.config)
        self.host.auto_collect_manager = manager
        await manager.start()
        self.assertFalse(await manager.submit(FlowEvent(self.png)))
        await asyncio.wait_for(manager.queue.join(), 5)
        self.assertEqual(self.vision.await_count, 1)
        response = await self.client.get("/inbox", query_string={"pack_id": "pack-a"})
        self.assertEqual((await response.get_json())["count"], 0)
        self.assertFalse(manager._cooldowns)
        self.assertEqual(list((self.root / "inbox" / "images").iterdir()), [])

    async def test_disabled_manual_review_accepts_both_modes_without_embedding(self):
        manager = self.host.auto_collect_manager
        manager.manual_review = False
        for semantic_enabled, source in ((False, self.png), (True, self.gif)):
            with self.subTest(semantic_enabled=semantic_enabled):
                self.host.semantic_enabled = semantic_enabled
                self.assertTrue(await manager.submit(FlowEvent(source)))
                await asyncio.wait_for(manager.queue.join(), 5)
                pending = await self.client.get(
                    "/inbox", query_string={"pack_id": "pack-a"}
                )
                self.assertEqual((await pending.get_json())["count"], 0)
                self.assertIn(
                    source.read_bytes(),
                    [path.read_bytes() for path in self.host.loaded_files],
                )
                self.assertFalse(self.host.semantic_task_manager._tasks)
                self.assertFalse(self.host.semantic_task_manager._index_tasks)
        self.assertEqual(self.host.reload_count, 2)
        self.assertEqual(self.vision.await_count, 2)
        for item in load_metadata(self.pack_dir)["images"].values():
            self.assertEqual(item["caption_status"], "done")
            self.assertEqual(item["caption"], self.decision["caption"])
            self.assertEqual(item["category"], "happy")
            self.assertEqual(item["embedding_status"], "pending")

    async def test_ordinary_image_filtered_and_failed_vision_can_retry(self):
        manager = self.host.auto_collect_manager
        self.assertFalse(await manager.submit(FlowEvent(self.png, sub_type=0)))
        self.assertEqual(self.vision.await_count, 0)
        self.assertFalse(manager._cooldowns)
        self.vision.side_effect = RuntimeError("temporary provider outage")
        self.assertTrue(await manager.submit(FlowEvent(self.png)))
        await asyncio.wait_for(manager.queue.join(), 5)
        self.assertFalse(manager._state.get("decisions"))
        self.assertFalse(manager._inflight)
        self.assertFalse(manager._worker_task.done())
        self.vision.side_effect = self.vision_response
        self.assertTrue(await manager.submit(FlowEvent(self.png)))
        await asyncio.wait_for(manager.queue.join(), 5)
        self.assertEqual(self.vision.await_count, 2)
        pending = await self.client.get("/inbox", query_string={"pack_id": "pack-a"})
        self.assertEqual((await pending.get_json())["count"], 1)
        self.assertEqual(list((self.root / "queue").iterdir()), [])
