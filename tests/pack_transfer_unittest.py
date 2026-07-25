import asyncio
import hashlib
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
    from astrbot_plugin_meme_manager.backend import pack_storage
    from astrbot_plugin_meme_manager.backend.semantic_index import (
        EmbeddingAdapter,
        build_index,
    )
    from astrbot_plugin_meme_manager.backend.semantic_models import PROMPT_VERSION
    from astrbot_plugin_meme_manager.backend.semantic_storage import (
        load_metadata,
        reconcile_metadata,
        save_metadata,
    )
except ImportError as exc:  # pragma: no cover - 主机精简环境允许跳过
    pack_storage = None
    EmbeddingAdapter = None
    build_index = None
    PROMPT_VERSION = ""
    load_metadata = None
    reconcile_metadata = None
    save_metadata = None
    IMPORT_ERROR = str(exc)
else:
    IMPORT_ERROR = ""


class FakeEmbedding:
    async def get_embedding(self, text):
        return [1.0, 0.0] if "开心" in str(text) else [0.0, 1.0]

    async def get_embeddings(self, texts):
        return [await self.get_embedding(text) for text in texts]

    @staticmethod
    def get_dim():
        return 2

    @staticmethod
    def get_model():
        return "test-embedding-v1"

    @staticmethod
    def meta():
        return type("ProviderMeta", (), {"id": "test-embedding"})()


@unittest.skipIf(pack_storage is None, f"当前环境无法加载 AstrBot 插件: {IMPORT_ERROR}")
class PackTransferTests(unittest.TestCase):
    def test_transfer_integer_fields_reject_booleans_and_decimals(self):
        self.assertIsNone(pack_storage._safe_nonnegative_int(True))
        self.assertIsNone(pack_storage._safe_nonnegative_int(2.5))
        self.assertEqual(pack_storage._safe_nonnegative_int("2"), 2)

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.plugin_data = self.root / "runtime"
        self.packs_dir = self.plugin_data / "packs"
        self.backup_dir = self.plugin_data / "backup"
        self.temp_runtime_dir = self.plugin_data / "temp"
        self.registry_path = self.plugin_data / "registry.json"
        self.rules_path = self.plugin_data / "selection_rules.json"
        self.community_path = self.plugin_data / "community_cache.json"
        for directory in (
            self.packs_dir,
            self.backup_dir,
            self.temp_runtime_dir,
            self.plugin_data / "semantic_indexes",
        ):
            directory.mkdir(parents=True, exist_ok=True)

        self.patch = mock.patch.multiple(
            pack_storage,
            PLUGIN_DATA_DIR=self.plugin_data,
            PACKS_DIR=self.packs_dir,
            BACKUP_DIR=self.backup_dir,
            TEMP_DIR=self.temp_runtime_dir,
            REGISTRY_PATH=self.registry_path,
            SELECTION_RULES_PATH=self.rules_path,
            COMMUNITY_CACHE_PATH=self.community_path,
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.temp_dir.cleanup)
        self._write_runtime_files([])

    def _write_json(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _write_runtime_files(self, pack_ids: list[str]) -> None:
        self._write_json(
            self.registry_path,
            {
                "schema_version": 1,
                "installed_packs": [
                    {
                        "id": pack_id,
                        "name": pack_id,
                        "version": "1.0.0",
                        "enabled": True,
                    }
                    for pack_id in pack_ids
                ],
            },
        )
        default_pack_id = pack_ids[0] if pack_ids else "builtin-default"
        self._write_json(
            self.rules_path,
            {
                "schema_version": 1,
                "rules": [
                    {
                        "id": "default",
                        "scope": "default",
                        "pack_id": default_pack_id,
                    }
                ],
            },
        )

    @staticmethod
    def _embedding_kwargs() -> dict:
        return {
            "embedding_provider_id": "test-embedding",
            "embedding_model": "test-embedding-v1",
            "embedding_dimension": 2,
        }

    def _create_semantic_pack(self, pack_id: str = "demo") -> Path:
        pack_dir = self.packs_dir / pack_id
        image_dir = pack_dir / "memes" / "happy"
        image_dir.mkdir(parents=True)
        (image_dir / "one.png").write_bytes(b"test-image-content")
        descriptions = {"happy": "开心时使用"}
        self._write_json(pack_dir / "memes_data.json", descriptions)
        self._write_json(
            pack_dir / "manifest.json",
            {
                "schema_version": 1,
                "id": pack_id,
                "name": "测试表情包",
                "version": "1.0.0",
                "categories": {"happy": {"description": "开心时使用"}},
            },
        )
        metadata = reconcile_metadata(pack_dir)
        record = next(iter(metadata["images"].values()))
        record.update(
            {
                "caption": "开心地庆祝",
                "tags": ["开心", "庆祝"],
                "caption_status": "done",
                "embedding_status": "pending",
                "prompt_version": PROMPT_VERSION,
                "category_fit": "match",
                "category_review_status": "auto_match",
                "category_review_context_hash": record["category_context_hash"],
            }
        )
        save_metadata(pack_dir, metadata)
        adapter = EmbeddingAdapter(FakeEmbedding())
        asyncio.run(build_index(pack_dir, self.plugin_data, pack_id, adapter))
        self._write_runtime_files([pack_id])
        return pack_dir

    def test_pack_list_exposes_vector_rebuild_only_for_new_semantic_packs(self):
        new_pack_dir = self._create_semantic_pack("new-pack")
        legacy_pack_dir = self._create_semantic_pack("legacy-pack")
        legacy_manifest = json.loads(
            (legacy_pack_dir / "manifest.json").read_text(encoding="utf-8")
        )
        legacy_manifest["tags"] = ["legacy", "converted"]
        self._write_json(legacy_pack_dir / "manifest.json", legacy_manifest)
        migrated_pack_id = str(pack_storage.LEGACY_MIGRATED_PACK_ID)
        self._create_semantic_pack(migrated_pack_id)
        self._write_runtime_files(["new-pack", "legacy-pack", migrated_pack_id])

        packs = {item["id"]: item for item in pack_storage.list_installed_packs()}

        self.assertTrue((new_pack_dir / "semantic_metadata.json").is_file())
        self.assertTrue(packs["new-pack"]["supports_vector_rebuild"])
        self.assertFalse(packs["new-pack"]["is_legacy_pack"])
        self.assertTrue(packs["legacy-pack"]["has_semantic_metadata"])
        self.assertTrue(packs["legacy-pack"]["is_legacy_pack"])
        self.assertFalse(packs["legacy-pack"]["supports_vector_rebuild"])
        self.assertTrue(packs[migrated_pack_id]["is_legacy_pack"])
        self.assertFalse(packs[migrated_pack_id]["supports_vector_rebuild"])

    def test_share_export_strips_vectors_and_remains_importable(self):
        pack_dir = self._create_semantic_pack()
        private_metadata = load_metadata(pack_dir)
        private_metadata["provider_api_key"] = "must-not-be-shared"
        private_item = next(iter(private_metadata["images"].values()))
        private_item["vision_model"] = "private-local-model"
        private_item["error"] = "private task path: /tmp/private-source.png"
        save_metadata(pack_dir, private_metadata)
        raw_private = json.loads(
            (pack_dir / "semantic_metadata.json").read_text(encoding="utf-8")
        )
        next(iter(raw_private["images"].values()))["provider_config"] = {
            "token": "must-not-be-shared"
        }
        self._write_json(pack_dir / "semantic_metadata.json", raw_private)
        (pack_dir / pack_storage.LEGACY_METADATA_BACKUP_NAME).write_text(
            "must-not-be-shared", encoding="utf-8"
        )
        original_semantic_bytes = (pack_dir / "semantic_metadata.json").read_bytes()
        result = pack_storage.export_pack_archive("demo", export_mode="share")

        with zipfile.ZipFile(result["archive_path"]) as archive:
            names = set(archive.namelist())
            transfer = json.loads(archive.read("meme_pack_export.json"))
            semantic = json.loads(archive.read("semantic_metadata.json"))

        self.assertEqual(transfer["export_mode"], "share")
        self.assertFalse(transfer["features"]["vectors"])
        self.assertNotIn("semantic_index/index.faiss", names)
        self.assertNotIn(pack_storage.LEGACY_METADATA_BACKUP_NAME, names)
        self.assertNotIn("provider_api_key", semantic)
        self.assertTrue(
            all(
                item.get("embedding_status") == "pending"
                for item in semantic["images"].values()
            )
        )
        self.assertTrue(
            all("provider_config" not in item for item in semantic["images"].values())
        )
        self.assertTrue(
            all(
                item.get("vision_model") == "private-local-model"
                for item in semantic["images"].values()
            )
        )
        self.assertTrue(
            all(item.get("error") is None for item in semantic["images"].values())
        )
        self.assertEqual(
            (pack_dir / "semantic_metadata.json").read_bytes(), original_semantic_bytes
        )

        imported = pack_storage.import_pack_archive(Path(result["archive_path"]))
        self.assertEqual(imported["pack_id"], "demo-2")
        self.assertFalse(imported["vectors_restored"])
        self.assertTrue(
            (self.packs_dir / "demo-2" / "semantic_metadata.json").is_file()
        )

    def test_vector_backup_restores_a_ready_index(self):
        self._create_semantic_pack()
        result = pack_storage.export_pack_archive("demo", export_mode="backup")
        self.assertTrue(result["vectors_included"])

        shutil.rmtree(self.packs_dir / "demo")
        shutil.rmtree(self.plugin_data / "semantic_indexes" / "demo")
        self._write_runtime_files([])

        restored = pack_storage.import_pack_archive(
            Path(result["archive_path"]),
            **self._embedding_kwargs(),
        )
        self.assertEqual(restored["pack_id"], "demo")
        self.assertTrue(restored["vectors_restored"])
        capabilities = pack_storage.get_pack_export_capabilities("demo")
        self.assertTrue(capabilities["vector_backup_available"])
        self.assertTrue(
            all(
                item.get("embedding_status") == "done"
                for item in load_metadata(self.packs_dir / "demo")["images"].values()
            )
        )

    def test_broken_vector_backup_keeps_captions_and_requests_rebuild(self):
        self._create_semantic_pack()
        result = pack_storage.export_pack_archive("demo", export_mode="backup")
        broken_root = self.root / "broken_backup"
        with zipfile.ZipFile(result["archive_path"]) as archive:
            archive.extractall(broken_root)
        index_manifest = json.loads(
            (broken_root / "semantic_index" / "index_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        (broken_root / "semantic_index" / index_manifest["index_file"]).write_bytes(
            b"broken-faiss-index"
        )
        broken_archive = Path(
            shutil.make_archive(
                str(self.root / "broken-vector-backup"),
                "zip",
                root_dir=broken_root,
            )
        )

        shutil.rmtree(self.packs_dir / "demo")
        shutil.rmtree(self.plugin_data / "semantic_indexes" / "demo")
        self._write_runtime_files([])
        restored = pack_storage.import_pack_archive(
            broken_archive,
            **self._embedding_kwargs(),
        )

        self.assertFalse(restored["vectors_restored"])
        self.assertIn("校验未通过", restored["vector_warning"])
        self.assertFalse((self.plugin_data / "semantic_indexes" / "demo").exists())
        metadata = load_metadata(self.packs_dir / "demo")
        self.assertTrue(
            all(
                item.get("caption_status") == "done"
                and item.get("embedding_status") == "pending"
                for item in metadata["images"].values()
            )
        )

    def test_vector_backup_with_different_local_model_keeps_semantics_only(self):
        self._create_semantic_pack()
        result = pack_storage.export_pack_archive("demo", export_mode="backup")
        shutil.rmtree(self.packs_dir / "demo")
        shutil.rmtree(self.plugin_data / "semantic_indexes" / "demo")
        self._write_runtime_files([])

        restored = pack_storage.import_pack_archive(
            Path(result["archive_path"]),
            embedding_provider_id="different-provider",
            embedding_model="different-model",
            embedding_dimension=3,
        )

        self.assertFalse(restored["vectors_restored"])
        self.assertIn("不一致", restored["vector_warning"])
        self.assertFalse((self.plugin_data / "semantic_indexes" / "demo").exists())
        item = next(iter(load_metadata(self.packs_dir / "demo")["images"].values()))
        self.assertEqual(item["caption"], "开心地庆祝")
        self.assertEqual(item["embedding_status"], "pending")

    def test_v1_semantic_archive_migrates_and_round_trips_through_share(self):
        source = self.root / "v1_source"
        image = source / "memes" / "happy" / "one.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"legacy-semantic-image")
        digest = hashlib.sha256(image.read_bytes()).hexdigest()
        self._write_json(source / "memes_data.json", {"happy": "旧版开心分类"})
        self._write_json(
            source / "manifest.json",
            {
                "schema_version": 1,
                "id": "legacy-semantic",
                "name": "旧语义图包",
                "version": "1.0.0",
                "categories": {"happy": {"description": "旧版开心分类"}},
            },
        )
        self._write_json(
            source / "semantic_metadata.json",
            {
                "schema_version": "1.0",
                "pack_id": "legacy-semantic",
                "images": {
                    digest: {
                        "content_sha256": digest,
                        "relative_path": "memes/happy/one.png",
                        "category": "happy",
                        "caption": "旧版描述必须保留",
                        "tags": ["category:old", "旧标签"],
                        "visible_text": "旧文字",
                        "caption_status": "done",
                        "embedding_status": "done",
                        "vision_model": "legacy-vision",
                        "text_hash": "legacy-fingerprint",
                    }
                },
            },
        )
        archive_path = Path(
            shutil.make_archive(str(self.root / "v1-semantic"), "zip", root_dir=source)
        )

        inspection = pack_storage.inspect_pack_archive(archive_path)
        self.assertEqual(inspection["semantic_done"], 1)
        imported = pack_storage.import_pack_archive(archive_path)
        metadata = load_metadata(self.packs_dir / imported["pack_id"])
        item = next(iter(metadata["images"].values()))
        self.assertEqual(metadata["schema_version"], "2.0")
        self.assertEqual(item["caption"], "旧版描述必须保留")
        self.assertEqual(item["tags"], ["category:happy", "旧标签"])
        self.assertEqual(item["visible_text"], "旧文字")
        self.assertEqual(item["category_review_status"], "unchecked")
        self.assertEqual(item["embedding_status"], "pending")

        shared = pack_storage.export_pack_archive(
            imported["pack_id"], export_mode="share"
        )
        round_trip = pack_storage.import_pack_archive(Path(shared["archive_path"]))
        round_trip_item = next(
            iter(
                load_metadata(self.packs_dir / round_trip["pack_id"])["images"].values()
            )
        )
        self.assertEqual(round_trip_item["caption"], "旧版描述必须保留")
        self.assertEqual(round_trip_item["visible_text"], "旧文字")

    def test_overwrite_import_preserves_existing_manual_semantics_by_default(self):
        existing_pack = self._create_semantic_pack("demo")
        existing = load_metadata(existing_pack)
        existing_item = next(iter(existing["images"].values()))
        existing_item.update(
            {
                "caption": "人工描述优先",
                "tags": ["category:happy", "人工标签"],
                "visible_text": "人工文字",
                "manual_caption": "人工描述优先",
                "manual_tags": ["人工标签"],
                "manual_visible_text": "人工文字",
                "manual_override": True,
                "provenance": "manual",
                "category_review_status": "manual_confirmed",
                "manual_confirmation_context_hash": existing_item[
                    "category_context_hash"
                ],
            }
        )
        save_metadata(existing_pack, existing)

        incoming_pack = self._create_semantic_pack("incoming")
        incoming = load_metadata(incoming_pack)
        incoming_item = next(iter(incoming["images"].values()))
        incoming_item.update(
            {
                "caption": "导入包另一份人工描述",
                "tags": ["category:happy", "导入包人工标签"],
                "manual_caption": "导入包另一份人工描述",
                "manual_tags": ["导入包人工标签"],
                "manual_visible_text": "",
                "manual_override": True,
                "provenance": "manual",
            }
        )
        save_metadata(incoming_pack, incoming)
        shared = pack_storage.export_pack_archive("incoming", export_mode="share")
        extracted = self.root / "manual_overwrite_source"
        with zipfile.ZipFile(shared["archive_path"]) as archive:
            archive.extractall(extracted)
        manifest = json.loads((extracted / "manifest.json").read_text(encoding="utf-8"))
        manifest["id"] = "demo"
        self._write_json(extracted / "manifest.json", manifest)
        transfer = json.loads(
            (extracted / "meme_pack_export.json").read_text(encoding="utf-8")
        )
        transfer["pack"]["id"] = "demo"
        self._write_json(extracted / "meme_pack_export.json", transfer)
        overwrite_archive = Path(
            shutil.make_archive(
                str(self.root / "manual-overwrite"), "zip", root_dir=extracted
            )
        )
        self._write_runtime_files(["demo", "incoming"])

        result = pack_storage.import_pack_archive(overwrite_archive, overwrite=True)

        self.assertTrue(result["manual_data_preserved"])
        current = next(iter(load_metadata(self.packs_dir / "demo")["images"].values()))
        self.assertTrue(current["manual_override"])
        self.assertEqual(current["caption"], "人工描述优先")
        self.assertEqual(current["tags"], ["category:happy", "人工标签"])
        self.assertEqual(current["embedding_status"], "pending")

        overwritten = pack_storage.import_pack_archive(
            overwrite_archive,
            overwrite=True,
            preserve_existing_manual=False,
        )
        self.assertFalse(overwritten["manual_data_preserved"])
        current = next(iter(load_metadata(self.packs_dir / "demo")["images"].values()))
        self.assertTrue(current["manual_override"])
        self.assertEqual(current["caption"], "导入包另一份人工描述")

    def test_import_failure_rolls_back_pack_registry_rules_and_index(self):
        existing_pack = self._create_semantic_pack("demo")
        old_image = existing_pack / "memes" / "happy" / "one.png"
        old_image_bytes = old_image.read_bytes()
        registry_before = self.registry_path.read_bytes()
        rules_before = self.rules_path.read_bytes()
        old_index_files = sorted(
            path.name
            for path in (self.plugin_data / "semantic_indexes" / "demo").iterdir()
        )

        source = self.root / "rollback_source"
        new_image = source / "memes" / "happy" / "one.png"
        new_image.parent.mkdir(parents=True)
        new_image.write_bytes(b"replacement-image")
        self._write_json(source / "memes_data.json", {"happy": "新描述"})
        self._write_json(
            source / "manifest.json",
            {
                "schema_version": 1,
                "id": "demo",
                "name": "替换包",
                "version": "2.0.0",
                "categories": {"happy": {"description": "新描述"}},
            },
        )
        archive_path = Path(
            shutil.make_archive(str(self.root / "rollback"), "zip", root_dir=source)
        )

        with mock.patch.object(
            pack_storage,
            "_apply_post_install_policy",
            side_effect=RuntimeError("simulated post-install failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated post-install failure"):
                pack_storage.import_pack_archive(archive_path, overwrite=True)

        self.assertEqual(old_image.read_bytes(), old_image_bytes)
        self.assertEqual(self.registry_path.read_bytes(), registry_before)
        self.assertEqual(self.rules_path.read_bytes(), rules_before)
        self.assertEqual(
            sorted(
                path.name
                for path in (self.plugin_data / "semantic_indexes" / "demo").iterdir()
            ),
            old_index_files,
        )

    def test_import_snapshot_failure_does_not_move_existing_pack(self):
        existing_pack = self._create_semantic_pack("demo")
        old_image = existing_pack / "memes" / "happy" / "one.png"
        old_image_bytes = old_image.read_bytes()

        source = self.root / "snapshot_failure_source"
        new_image = source / "memes" / "happy" / "one.png"
        new_image.parent.mkdir(parents=True)
        new_image.write_bytes(b"replacement-image")
        self._write_json(source / "memes_data.json", {"happy": "新描述"})
        self._write_json(
            source / "manifest.json",
            {
                "schema_version": 1,
                "id": "demo",
                "name": "替换包",
                "version": "2.0.0",
                "categories": {"happy": {"description": "新描述"}},
            },
        )
        archive_path = Path(
            shutil.make_archive(
                str(self.root / "snapshot-failure"), "zip", root_dir=source
            )
        )
        original_read_bytes = Path.read_bytes

        def fail_registry_snapshot(path):
            if path == self.registry_path:
                raise OSError("simulated snapshot failure")
            return original_read_bytes(path)

        with mock.patch.object(Path, "read_bytes", new=fail_registry_snapshot):
            with self.assertRaisesRegex(OSError, "simulated snapshot failure"):
                pack_storage.import_pack_archive(archive_path, overwrite=True)

        self.assertTrue(existing_pack.is_dir())
        self.assertEqual(old_image.read_bytes(), old_image_bytes)
        self.assertTrue((self.plugin_data / "semantic_indexes" / "demo").is_dir())

    def test_corrupt_and_future_semantics_never_install_or_change_registry(self):
        for semantic_payload in (
            "{broken-json",
            json.dumps({"schema_version": "9.0", "images": {}}),
        ):
            with self.subTest(semantic_payload=semantic_payload):
                source = self.root / f"invalid_{len(semantic_payload)}"
                if source.exists():
                    shutil.rmtree(source)
                image = source / "memes" / "happy" / "one.png"
                image.parent.mkdir(parents=True)
                image.write_bytes(b"invalid-semantic")
                self._write_json(source / "memes_data.json", {"happy": "开心"})
                self._write_json(
                    source / "manifest.json",
                    {
                        "schema_version": 1,
                        "id": f"invalid-{len(semantic_payload)}",
                        "name": "无效语义包",
                        "version": "1.0.0",
                        "categories": {"happy": {"description": "开心"}},
                    },
                )
                (source / "semantic_metadata.json").write_text(
                    semantic_payload, encoding="utf-8"
                )
                archive_path = Path(
                    shutil.make_archive(
                        str(self.root / f"invalid-{len(semantic_payload)}"),
                        "zip",
                        root_dir=source,
                    )
                )
                registry_before = self.registry_path.read_bytes()

                with self.assertRaises(ValueError):
                    pack_storage.import_pack_archive(archive_path)

                self.assertEqual(self.registry_path.read_bytes(), registry_before)
                self.assertFalse(
                    (self.packs_dir / f"invalid-{len(semantic_payload)}").exists()
                )

    def test_explicit_manual_overwrite_cannot_replace_unsafe_existing_semantics(self):
        existing_pack = self._create_semantic_pack("demo")
        source = self.root / "unsafe_overwrite_source"
        image = source / "memes" / "happy" / "one.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"replacement-image")
        self._write_json(source / "memes_data.json", {"happy": "新描述"})
        self._write_json(
            source / "manifest.json",
            {
                "schema_version": 1,
                "id": "demo",
                "name": "替换包",
                "version": "2.0.0",
                "categories": {"happy": {"description": "新描述"}},
            },
        )
        archive_path = Path(
            shutil.make_archive(
                str(self.root / "unsafe-overwrite"), "zip", root_dir=source
            )
        )
        semantic_file = existing_pack / "semantic_metadata.json"
        registry_before = self.registry_path.read_bytes()
        for unsafe_payload in (
            b"{broken-json",
            json.dumps({"schema_version": "9.0", "images": {}}).encode(),
        ):
            with self.subTest(unsafe_payload=unsafe_payload):
                semantic_file.write_bytes(unsafe_payload)

                with self.assertRaisesRegex(ValueError, "拒绝覆盖"):
                    pack_storage.import_pack_archive(
                        archive_path,
                        overwrite=True,
                        preserve_existing_manual=False,
                    )

                self.assertEqual(semantic_file.read_bytes(), unsafe_payload)
                self.assertEqual(self.registry_path.read_bytes(), registry_before)
                self.assertEqual(
                    (existing_pack / "memes" / "happy" / "one.png").read_bytes(),
                    b"test-image-content",
                )

    def test_runtime_restore_failure_rolls_back_all_runtime_state(self):
        existing_pack = self._create_semantic_pack("demo")
        old_image = existing_pack / "memes" / "happy" / "one.png"
        old_bytes = old_image.read_bytes()
        registry_before = self.registry_path.read_bytes()
        rules_before = self.rules_path.read_bytes()

        backup_root = self.root / "runtime_restore_source" / "runtime_backup"
        backup_pack = backup_root / "packs" / "demo"
        shutil.copytree(existing_pack, backup_pack)
        (backup_pack / "semantic_metadata.json").unlink(missing_ok=True)
        (backup_pack / "memes" / "happy" / "one.png").write_bytes(b"restored-new-image")
        shutil.copy2(self.registry_path, backup_root / "registry.json")
        shutil.copy2(self.rules_path, backup_root / "selection_rules.json")
        runtime_archive = Path(
            shutil.make_archive(
                str(self.root / "runtime-rollback"),
                "zip",
                root_dir=backup_root,
            )
        )

        with mock.patch.object(
            pack_storage,
            "_save_selection_rules",
            side_effect=RuntimeError("simulated rules failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated rules failure"):
                pack_storage.import_runtime_backup(runtime_archive, overwrite=True)

        self.assertEqual(old_image.read_bytes(), old_bytes)
        self.assertEqual(self.registry_path.read_bytes(), registry_before)
        self.assertEqual(self.rules_path.read_bytes(), rules_before)

        restored = pack_storage.import_runtime_backup(runtime_archive, overwrite=True)
        self.assertEqual(restored["restored_packs"], 1)
        self.assertEqual(old_image.read_bytes(), b"restored-new-image")

    def test_runtime_restore_without_overwrite_keeps_local_registry_entry(self):
        existing_pack = self._create_semantic_pack("demo")
        old_image = existing_pack / "memes" / "happy" / "one.png"
        old_bytes = old_image.read_bytes()
        local_registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
        local_registry["installed_packs"][0]["name"] = "本机保留名称"
        local_registry["installed_packs"][0]["enabled"] = False
        self._write_json(self.registry_path, local_registry)

        backup_root = self.root / "runtime_merge_source" / "runtime_backup"
        backup_demo = backup_root / "packs" / "demo"
        shutil.copytree(existing_pack, backup_demo)
        (backup_demo / "semantic_metadata.json").unlink(missing_ok=True)
        (backup_demo / "memes" / "happy" / "one.png").write_bytes(
            b"must-not-overwrite-local"
        )
        backup_fresh = backup_root / "packs" / "fresh"
        shutil.copytree(existing_pack, backup_fresh)
        (backup_fresh / "semantic_metadata.json").unlink(missing_ok=True)
        fresh_manifest = json.loads(
            (backup_fresh / "manifest.json").read_text(encoding="utf-8")
        )
        fresh_manifest.update({"id": "fresh", "name": "备份新增包"})
        self._write_json(backup_fresh / "manifest.json", fresh_manifest)
        self._write_json(
            backup_root / "registry.json",
            {
                "schema_version": 1,
                "installed_packs": [
                    {
                        "id": "demo",
                        "name": "备份中的旧名称",
                        "version": "9.0.0",
                        "enabled": True,
                    },
                    {
                        "id": "fresh",
                        "name": "备份新增包",
                        "version": "1.0.0",
                        "enabled": True,
                    },
                ],
            },
        )
        runtime_archive = Path(
            shutil.make_archive(
                str(self.root / "runtime-merge"), "zip", root_dir=backup_root
            )
        )

        restored = pack_storage.import_runtime_backup(runtime_archive, overwrite=False)

        self.assertEqual(restored["restored_packs"], 1)
        self.assertEqual(old_image.read_bytes(), old_bytes)
        registry = {
            item["id"]: item
            for item in json.loads(self.registry_path.read_text(encoding="utf-8"))[
                "installed_packs"
            ]
        }
        self.assertEqual(registry["demo"]["name"], "本机保留名称")
        self.assertFalse(registry["demo"]["enabled"])
        self.assertEqual(registry["fresh"]["name"], "备份新增包")

    def test_legacy_nonsemantic_archive_is_converted(self):
        source = self.root / "legacy_source"
        (source / "memes" / "happy").mkdir(parents=True)
        (source / "memes" / "happy" / "old.gif").write_bytes(b"GIF89a")
        self._write_json(source / "memes_data.json", {"happy": "旧版开心分类"})
        archive_path = Path(
            shutil.make_archive(str(self.root / "My Old Pack"), "zip", root_dir=source)
        )

        inspection = pack_storage.inspect_pack_archive(
            archive_path, suggested_pack_id="My Old Pack"
        )
        self.assertEqual(inspection["detected_format"], "legacy")
        self.assertFalse(inspection["semantic_metadata"])

        imported = pack_storage.import_pack_archive(
            archive_path, suggested_pack_id="My Old Pack"
        )
        self.assertEqual(imported["pack_id"], "my-old-pack")
        self.assertTrue((self.packs_dir / "my-old-pack" / "manifest.json").is_file())
        self.assertFalse(
            (self.packs_dir / "my-old-pack" / "semantic_metadata.json").exists()
        )

    def test_archive_path_traversal_is_rejected(self):
        archive_path = self.root / "unsafe.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("../outside.txt", "blocked")
            archive.writestr("manifest.json", "{}")

        with self.assertRaisesRegex(ValueError, "非法路径"):
            pack_storage.inspect_pack_archive(archive_path)
        self.assertFalse((self.temp_runtime_dir / "outside.txt").exists())

    def test_import_refuses_to_consume_disk_safety_reserve(self):
        source = self.root / "disk_guard_source"
        (source / "memes" / "happy").mkdir(parents=True)
        (source / "memes" / "happy" / "one.png").write_bytes(b"image")
        self._write_json(source / "memes_data.json", {"happy": "开心"})
        archive_path = Path(
            shutil.make_archive(str(self.root / "disk-guard"), "zip", root_dir=source)
        )

        with mock.patch.object(
            pack_storage.shutil,
            "disk_usage",
            return_value=SimpleNamespace(free=1024),
        ):
            with self.assertRaisesRegex(ValueError, "剩余磁盘空间不足"):
                pack_storage.inspect_pack_archive(archive_path)

    def test_export_refuses_symlinks_that_could_leak_external_files(self):
        pack_dir = self._create_semantic_pack()
        (pack_dir / "memes" / "happy" / "external.txt").symlink_to("/etc/hosts")

        with self.assertRaisesRegex(ValueError, "符号链接"):
            pack_storage.export_pack_archive("demo", export_mode="share")

    def test_oversized_control_json_is_rejected_before_parsing(self):
        archive_path = self.root / "oversized-control.zip"
        with zipfile.ZipFile(
            archive_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr(
                "meme_pack_export.json",
                " "
                * (pack_storage.ARCHIVE_JSON_SIZE_LIMITS["meme_pack_export.json"] + 1),
            )
            archive.writestr("manifest.json", "{}")

        with self.assertRaisesRegex(ValueError, "体积异常"):
            pack_storage.inspect_pack_archive(archive_path)


if __name__ == "__main__":
    unittest.main()
