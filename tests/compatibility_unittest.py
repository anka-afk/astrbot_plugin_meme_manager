import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.semantic_index import EmbeddingAdapter, build_index, index_is_ready
from backend.semantic_models import PROMPT_VERSION, runtime_category_mapping
from backend.semantic_query import validate_selected_id
from backend.semantic_storage import (
    LEGACY_METADATA_BACKUP_NAME,
    SemanticMetadataCompatibilityError,
    file_sha256,
    load_metadata,
    migrate_legacy_metadata,
    reconcile_metadata,
    reset_local_embedding_state,
    save_metadata,
    scan_images,
    semantic_metadata_is_complete,
)
from backend.semantic_task import SemanticTaskManager


class FakeEmbedding:
    provider_config = {"id": "compat-embedding", "model": "compat-model"}

    def get_dim(self):
        return 2

    def get_model(self):
        return "compat-model"

    async def get_embedding(self, _text):
        return [1.0, 0.0]

    async def get_embeddings(self, texts):
        return [[1.0, 0.0] for _ in texts]


class CandidateEvent:
    def __init__(self, candidate_id, entry_id):
        self.extra = {
            "meme_manager_semantic_candidates": {
                candidate_id: {"id": candidate_id, "entry_id": entry_id}
            }
        }

    def get_extra(self, key):
        return self.extra.get(key)


def reviewed(item):
    item["prompt_version"] = PROMPT_VERSION
    item["category_fit"] = "match"
    item["category_review_status"] = "auto_match"
    item["category_review_context_hash"] = item["category_context_hash"]
    item["caption_status"] = "done"
    return item


class SemanticCompatibilityTests(unittest.TestCase):
    def _pack(self, root, categories):
        pack = Path(root) / "demo"
        for category, files in categories.items():
            category_dir = pack / "memes" / category
            category_dir.mkdir(parents=True, exist_ok=True)
            for filename, content in files.items():
                (category_dir / filename).write_bytes(content)
        (pack / "memes_data.json").write_text(
            json.dumps(
                {category: f"{category} description" for category in categories}
            ),
            encoding="utf-8",
        )
        return pack

    def _legacy_record(self, image, **overrides):
        record = {
            "content_sha256": file_sha256(image),
            "relative_path": image.relative_to(image.parents[2]).as_posix(),
            "category": image.parent.name,
            "caption": "legacy caption",
            "tags": ["category:old", "分类:旧分类", "legacy tag"],
            "visible_text": "legacy visible text",
            "caption_status": "done",
            "embedding_status": "done",
            "provenance": "ai",
            "vision_model": "legacy-vision",
            "text_hash": "legacy-text-fingerprint",
            "updated_at": "2026-01-02T03:04:05+00:00",
        }
        record.update(overrides)
        return record

    def test_pure_v1_migration_preserves_semantics_and_invalidates_local_vectors(self):
        with tempfile.TemporaryDirectory() as temp:
            pack = self._pack(temp, {"happy": {"one.png": b"legacy-one"}})
            image = pack / "memes" / "happy" / "one.png"
            record = self._legacy_record(
                image,
                caption_status="failed",
                error="temporary legacy failure",
            )
            source = {
                "schema_version": "1.0",
                "pack_id": "old-pack",
                "embedding_provider_id": "private-provider",
                "images": {record["content_sha256"]: record},
            }

            migrated = migrate_legacy_metadata(
                source,
                scan_images(pack),
                {"happy": "happy description"},
                "demo",
            )

            self.assertEqual(source["schema_version"], "1.0")
            self.assertNotIn("metadata_migration_required", source)
            self.assertEqual(migrated["schema_version"], "2.0")
            self.assertNotIn("embedding_provider_id", migrated)
            item = next(iter(migrated["images"].values()))
            self.assertEqual(item["caption"], "legacy caption")
            self.assertEqual(item["visible_text"], "legacy visible text")
            self.assertEqual(item["tags"], ["category:happy", "legacy tag"])
            self.assertEqual(item["vision_model"], "legacy-vision")
            self.assertEqual(item["legacy_text_hash"], "legacy-text-fingerprint")
            self.assertEqual(item["category_review_status"], "unchecked")
            self.assertEqual(item["embedding_status"], "pending")
            self.assertEqual(item["caption_status"], "failed")
            self.assertEqual(item["error"], "temporary legacy failure")
            self.assertTrue(migrated["requires_local_index_rebuild"])
            self.assertFalse(migrated["legacy_index_compatible"])

    def test_manual_v1_protection_stays_on_exact_path_only(self):
        with tempfile.TemporaryDirectory() as temp:
            pack = self._pack(
                temp,
                {
                    "first": {"original.png": b"same-content"},
                    "second": {"copy.png": b"same-content"},
                },
            )
            original = pack / "memes" / "first" / "original.png"
            record = self._legacy_record(
                original,
                caption="manual caption",
                tags=["category:first", "manual tag"],
                provenance="manual",
                manual_override=True,
                manual_tags=["分类:first", "manual tag"],
                auto_caption="automatic starting point",
                auto_tags=["category:first", "automatic tag"],
                auto_visible_text="automatic text",
            )
            source = {
                "schema_version": "1.0",
                "images": {record["content_sha256"]: record},
            }

            migrated = migrate_legacy_metadata(
                source,
                scan_images(pack),
                {"first": "first description", "second": "second description"},
                "demo",
            )
            by_path = {
                item["relative_path"]: item for item in migrated["images"].values()
            }
            exact = by_path["memes/first/original.png"]
            duplicate = by_path["memes/second/copy.png"]
            self.assertTrue(exact["manual_override"])
            self.assertEqual(exact["manual_caption"], "manual caption")
            self.assertEqual(exact["tags"], ["category:first", "manual tag"])
            self.assertFalse(duplicate["manual_override"])
            self.assertEqual(duplicate["provenance"], "ai")
            self.assertEqual(duplicate["caption"], "automatic starting point")
            self.assertEqual(duplicate["tags"], ["category:second", "automatic tag"])
            self.assertNotEqual(exact["entry_id"], duplicate["entry_id"])

    def test_versionless_partial_metadata_migrates_in_memory_without_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            pack = self._pack(
                temp,
                {"happy": {"done.png": b"done", "pending.png": b"pending"}},
            )
            done = pack / "memes" / "happy" / "done.png"
            record = self._legacy_record(done)
            source = {"images": {record["content_sha256"]: record}}
            metadata_path = pack / "semantic_metadata.json"
            original = json.dumps(source, ensure_ascii=False).encode()
            metadata_path.write_bytes(original)

            migrated = load_metadata(pack)

            self.assertEqual(metadata_path.read_bytes(), original)
            self.assertTrue(migrated["metadata_migration_required"])
            self.assertEqual(migrated["migrated_from_schema_version"], "missing")
            self.assertEqual(len(migrated["images"]), 2)
            by_name = {
                Path(item["relative_path"]).name: item
                for item in migrated["images"].values()
            }
            self.assertEqual(by_name["done.png"]["caption"], "legacy caption")
            self.assertEqual(by_name["pending.png"]["caption"], "")
            self.assertEqual(by_name["pending.png"]["caption_status"], "pending")

    def test_first_migration_write_creates_stable_backup_and_is_atomic(self):
        with tempfile.TemporaryDirectory() as temp:
            pack = self._pack(temp, {"happy": {"one.png": b"legacy"}})
            image = pack / "memes" / "happy" / "one.png"
            record = self._legacy_record(image)
            raw = json.dumps(
                {
                    "schema_version": "1.0",
                    "images": {record["content_sha256"]: record},
                },
                ensure_ascii=False,
            ).encode()
            metadata_path = pack / "semantic_metadata.json"
            metadata_path.write_bytes(raw)
            migrated = load_metadata(pack)

            with mock.patch(
                "backend.semantic_storage.os.replace",
                side_effect=OSError("simulated replace failure"),
            ):
                with self.assertRaisesRegex(OSError, "simulated replace failure"):
                    save_metadata(pack, migrated)

            backup_path = pack / LEGACY_METADATA_BACKUP_NAME
            self.assertEqual(metadata_path.read_bytes(), raw)
            self.assertEqual(backup_path.read_bytes(), raw)
            save_metadata(pack, migrated)
            self.assertEqual(load_metadata(pack)["schema_version"], "2.0")
            self.assertEqual(backup_path.read_bytes(), raw)
            self.assertEqual(
                [path.name for path in pack.glob("semantic_metadata*.backup.json")],
                [LEGACY_METADATA_BACKUP_NAME],
            )

    def test_corrupt_wrong_type_and_future_metadata_remain_read_only(self):
        cases = [
            (b'{"schema_version":"1.0","images":', "无法解析"),
            (
                json.dumps(
                    {
                        "schema_version": "2.0",
                        "images": {"a" * 64: {"tags": {"bad": True}}},
                    }
                ).encode(),
                "字段类型错误",
            ),
            (
                json.dumps({"schema_version": "9.0", "images": {}}).encode(),
                "不支持",
            ),
        ]
        for raw, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temp:
                pack = self._pack(temp, {"happy": {"one.png": b"one"}})
                metadata_path = pack / "semantic_metadata.json"
                metadata_path.write_bytes(raw)
                loaded = load_metadata(pack)
                self.assertTrue(loaded["metadata_read_only"])
                self.assertIn(message, loaded["metadata_error"])
                with self.assertRaises(SemanticMetadataCompatibilityError):
                    save_metadata(pack, loaded)
                self.assertEqual(metadata_path.read_bytes(), raw)
                self.assertFalse((pack / LEGACY_METADATA_BACKUP_NAME).exists())

    def test_reset_and_external_reconcile_migrate_v1_then_round_trip_v2(self):
        with (
            tempfile.TemporaryDirectory() as first_temp,
            tempfile.TemporaryDirectory() as second_temp,
        ):
            first = self._pack(first_temp, {"happy": {"one.png": b"portable"}})
            image = first / "memes" / "happy" / "one.png"
            record = self._legacy_record(
                image,
                embedding_status="done",
                error="private provider detail",
            )
            legacy = {
                "schema_version": "1.0",
                "api_key": "secret",
                "images": {record["content_sha256"]: record},
            }
            portable = reset_local_embedding_state(legacy, first)
            self.assertEqual(portable["schema_version"], "2.0")
            self.assertNotIn("api_key", portable)
            first_item = next(iter(portable["images"].values()))
            self.assertEqual(first_item["caption"], "legacy caption")
            self.assertEqual(first_item["embedding_status"], "pending")
            self.assertIsNone(first_item["error"])

            second = self._pack(second_temp, {"happy": {"one.png": b"portable"}})
            imported = reconcile_metadata(second, external_data=legacy)
            self.assertEqual(
                next(iter(imported["images"].values()))["caption"],
                "legacy caption",
            )
            portable_v2 = reset_local_embedding_state(imported)
            round_trip = reconcile_metadata(second, external_data=portable_v2)
            self.assertEqual(
                next(iter(round_trip["images"].values()))["caption"],
                "legacy caption",
            )

    def test_legacy_index_manifest_is_never_considered_current(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            index_dir = root / "semantic_indexes" / "demo"
            index_dir.mkdir(parents=True)
            (index_dir / "index_manifest.json").write_text(
                json.dumps(
                    {
                        "index_format": "faiss-indexflatip-v1",
                        "metadata_schema_version": "1.0",
                        "item_count": 1,
                        "embedding_provider_id": "compat-embedding",
                        "embedding_model": "compat-model",
                        "embedding_dimension": 2,
                    }
                ),
                encoding="utf-8",
            )
            self.assertFalse(
                index_is_ready(
                    root,
                    "demo",
                    {"images": {}},
                    "compat-embedding",
                    "compat-model",
                    2,
                )
            )
            (index_dir / "index_manifest.json").write_text(
                json.dumps(
                    {
                        "index_format": "faiss-indexflatip-v1",
                        "metadata_schema_version": "2.0",
                        "item_count": "not-an-integer",
                        "embedding_dimension": {"bad": True},
                    }
                ),
                encoding="utf-8",
            )
            self.assertFalse(index_is_ready(root, "demo", {"images": {}}))

    def test_needs_review_is_excluded_from_legacy_and_semantic_runtime(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                pack = self._pack(
                    temp,
                    {
                        "happy": {"one.png": b"normal"},
                        "needs_review": {"review.png": b"review"},
                    },
                )
                metadata = reconcile_metadata(pack)
                review_entry_id = ""
                for entry_id, item in metadata["images"].items():
                    item["caption"] = f"caption {item['category']}"
                    item["tags"] = [f"category:{item['category']}", "tag"]
                    reviewed(item)
                    item["embedding_status"] = "pending"
                    if item["category"] == "needs_review":
                        review_entry_id = entry_id
                save_metadata(pack, metadata)

                adapter = EmbeddingAdapter(FakeEmbedding())
                manifest = await build_index(pack, root, "demo", adapter)
                self.assertEqual(manifest["item_count"], 1)
                self.assertNotIn(review_entry_id, manifest["items"])
                current = load_metadata(pack)
                self.assertTrue(
                    semantic_metadata_is_complete(
                        pack, current, require_embeddings=True
                    )
                )

                candidate_id = f"meme:{review_entry_id[:12]}"
                event = CandidateEvent(candidate_id, review_entry_id)
                self.assertIsNone(validate_selected_id(event, candidate_id, pack))

                mapping = runtime_category_mapping(
                    {"happy": "happy description", "needs_review": "review"}
                )
                self.assertIn("happy", mapping)
                self.assertNotIn("needs_review", mapping)

        asyncio.run(run())

    def test_status_get_does_not_write_migrated_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pack = self._pack(root / "packs", {"happy": {"one.png": b"legacy"}})
            image = pack / "memes" / "happy" / "one.png"
            record = self._legacy_record(image)
            raw = json.dumps(
                {
                    "schema_version": "1.0",
                    "images": {record["content_sha256"]: record},
                }
            ).encode()
            metadata_path = pack / "semantic_metadata.json"
            metadata_path.write_bytes(raw)
            state_dir = root / "semantic_indexes" / "demo"
            state_dir.mkdir(parents=True)
            (state_dir / "task_state.json").write_text(
                json.dumps({"task_status": "running", "active_items": ["old"]}),
                encoding="utf-8",
            )
            manager = SemanticTaskManager(root)

            with mock.patch(
                "backend.semantic_task.save_metadata",
                side_effect=AssertionError("GET status must not write metadata"),
            ):
                status = manager.status("demo")

            self.assertEqual(status["queue_status"], "migration_required")
            self.assertTrue(status["metadata_migration_required"])
            self.assertEqual(metadata_path.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
