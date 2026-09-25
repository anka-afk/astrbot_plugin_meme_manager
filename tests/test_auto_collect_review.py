import hashlib
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot_plugin_meme_manager.backend import auto_collect
from test_auto_collect import DummyPlugin, png_bytes


class AutoCollectReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.packs = self.root / "packs"
        for pack_id in ("pack-a", "pack-b"):
            (self.packs / pack_id / "memes" / "happy").mkdir(parents=True)
        for name, value in {
            "PACKS_DIR": self.packs,
            "AUTO_COLLECT_INBOX_IMAGES_DIR": self.root / "inbox" / "images",
            "AUTO_COLLECT_INBOX_METADATA_PATH": self.root / "inbox" / "metadata.json",
            "AUTO_COLLECT_STATE_PATH": self.root / "state.json",
        }.items():
            self.stack.enter_context(patch.object(auto_collect, name, value))
        self.stack.enter_context(
            patch.object(
                auto_collect,
                "get_pack_paths",
                side_effect=lambda pack_id: {
                    "memes_dir": self.packs / pack_id / "memes"
                },
            )
        )
        self.stack.enter_context(
            patch.object(
                auto_collect,
                "load_pack_category_mapping",
                return_value={"happy": "happy", "sad": "sad"},
            )
        )
        self.plugin = DummyPlugin()
        self.plugin.semantic_enabled = False
        self.manager = auto_collect.AutoCollectManager(self.plugin, {"enabled": True})

    async def add_candidate(self, color=(255, 0, 0), confidence=0.9):
        content = png_bytes(color)
        digest = hashlib.sha256(content).hexdigest()
        source = self.root / f"{digest}.png"
        source.write_bytes(content)
        job = auto_collect.AutoCollectJob(
            source, "pack-a", {"happy": "happy"}, "group", "1"
        )
        decision = {
            "is_meme": True,
            "meme_confidence": 0.95,
            "category": "happy",
            "category_confidence": confidence,
        }
        self.manager._classify = AsyncMock(return_value=decision)
        await self.manager._process_job(job)
        return f"pack-a:{digest}", job

    async def test_override_category_and_failed_semantic_write_retains_pending(self):
        record_id, _ = await self.add_candidate()
        with patch.object(
            auto_collect,
            "save_collected_image_semantic",
            side_effect=OSError("disk unavailable"),
        ):
            result = await self.manager.accept_pending(
                "pack-a", [{"id": record_id, "category": "sad"}]
            )
        self.assertEqual(result["failed"], 1)
        self.assertTrue(self.manager.pending_image_path("pack-a", record_id).exists())
        self.assertFalse(list((self.packs / "pack-a" / "memes").rglob("*.png")))
        result = await self.manager.accept_pending(
            "pack-a", [{"id": record_id, "category": "sad"}]
        )
        self.assertEqual(result["imported"], 1)
        self.assertEqual(
            len(list((self.packs / "pack-a" / "memes" / "sad").glob("*.png"))), 1
        )

    async def test_regular_discard_can_be_collected_again(self):
        record_id, job = await self.add_candidate()
        await self.manager.discard_pending("pack-a", [record_id], False)
        await self.manager._process_job(job)
        self.assertEqual((await self.manager.pending_status("pack-a"))["count"], 1)

    async def test_capacity_and_existing_pending_skip_recognition(self):
        self.manager.pending_limit = 1
        record_id, job = await self.add_candidate()
        self.manager._classify.reset_mock()
        await self.manager._process_job(job)
        self.manager._classify.assert_not_awaited()
        await self.add_candidate((0, 255, 0))
        self.assertEqual((await self.manager.pending_status("pack-a"))["count"], 1)
        self.assertEqual(self.manager.counters["inbox_full"], 1)

    async def test_cross_pack_and_traversal_rejected_before_mutation(self):
        record_id, _ = await self.add_candidate()
        with self.assertRaises(ValueError):
            await self.manager.accept_pending("pack-b", [{"id": record_id}])
        with self.assertRaises(ValueError):
            await self.manager.discard_pending("pack-b", [record_id], True)
        metadata = json.loads(auto_collect.AUTO_COLLECT_INBOX_METADATA_PATH.read_text())
        metadata["items"][record_id]["filename"] = "../../outside.png"
        auto_collect.AUTO_COLLECT_INBOX_METADATA_PATH.write_text(json.dumps(metadata))
        with self.assertRaises(ValueError):
            self.manager.pending_image_path("pack-a", record_id)

    async def test_auto_accept_does_not_start_embedding(self):
        self.manager.manual_review = False
        await self.add_candidate()
        self.assertEqual((await self.manager.pending_status("pack-a"))["count"], 0)
        self.assertEqual(len(list((self.packs / "pack-a" / "memes").rglob("*.png"))), 1)

    async def test_legacy_bulk_import_cannot_bypass_manual_review(self):
        await self.add_candidate()
        with self.assertRaises(RuntimeError):
            await self.manager.import_pending("pack-a")
        self.assertEqual((await self.manager.pending_status("pack-a"))["count"], 1)

    async def test_uncertain_category_kept_for_human_but_auto_accept_uses_review(self):
        record_id, _ = await self.add_candidate(confidence=0.2)
        item = (await self.manager.pending_status("pack-a"))["items"][0]
        self.assertEqual(item["suggested_category"], "happy")
        self.assertEqual(item["category_confidence"], 0.2)
        await self.manager.discard_pending("pack-a", [record_id])
        self.manager.manual_review = False
        await self.add_candidate(confidence=0.2)
        review_dir = self.packs / "pack-a" / "memes" / auto_collect.REVIEW_CATEGORY
        self.assertEqual(len(list(review_dir.glob("*.png"))), 1)

    async def test_selection_limit_matches_maximum_pending_configuration(self):
        record_id, _ = await self.add_candidate()
        result = await self.manager.accept_pending("pack-a", [{"id": record_id}] * 2000)
        self.assertEqual(result["imported"], 1)
        record_id, _ = await self.add_candidate((0, 255, 0))
        result = await self.manager.discard_pending("pack-a", [record_id] * 2000)
        self.assertEqual(result["discarded"], 1)
        with self.assertRaises(ValueError):
            await self.manager.discard_pending("pack-a", [record_id] * 2001)

    async def test_batch_validation_reads_inbox_metadata_once(self):
        for action in ("accept", "discard"):
            with self.subTest(action=action):
                ids = []
                for index in range(3):
                    color = (index * 50, 10 if action == "accept" else 20, 255)
                    record_id, _ = await self.add_candidate(color)
                    ids.append(record_id)
                with patch.object(
                    self.manager, "_load_json", wraps=self.manager._load_json
                ) as loader:
                    if action == "accept":
                        result = await self.manager.accept_pending(
                            "pack-a", [{"id": record_id} for record_id in ids]
                        )
                        self.assertEqual(result["imported"], 3)
                    else:
                        result = await self.manager.discard_pending("pack-a", ids)
                        self.assertEqual(result["discarded"], 3)
                loader.assert_called_once_with(
                    auto_collect.AUTO_COLLECT_INBOX_METADATA_PATH, {"items": {}}
                )

    async def test_nonfinite_model_confidences_never_become_high_confidence(self):
        self.plugin.context = SimpleNamespace(llm_generate=AsyncMock())
        with patch.object(auto_collect, "AUTO_COLLECT_TEMP_DIR", self.root):
            for value in ("NaN", "Infinity", "-Infinity"):
                with self.subTest(value=value):
                    self.plugin.context.llm_generate.return_value = SimpleNamespace(
                        completion_text=json.dumps(
                            {
                                "is_meme": True,
                                "category": "happy",
                                "meme_confidence": value,
                                "category_confidence": value,
                            }
                        )
                    )
                    result = await self.manager._classify(
                        png_bytes(), ".png", {"happy": "happy"}
                    )
                    self.assertEqual(result["meme_confidence"], 0.0)
                    self.assertEqual(result["category_confidence"], 0.0)

    async def test_plain_text_messages_do_not_increment_collection_skip_counters(self):
        self.manager._ready = True
        self.manager.scope = {"unmatched"}
        event = SimpleNamespace(message_obj=SimpleNamespace(message=[]))
        self.assertFalse(await self.manager.submit(event))
        self.assertEqual(self.manager.counters, {})
