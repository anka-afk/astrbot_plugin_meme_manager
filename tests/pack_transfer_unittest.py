import asyncio
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
    from astrbot_plugin_meme_manager.backend.semantic_storage import (
        load_metadata,
        reconcile_metadata,
        save_metadata,
    )
except ImportError as exc:  # pragma: no cover - 主机精简环境允许跳过
    pack_storage = None
    EmbeddingAdapter = None
    build_index = None
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
            }
        )
        save_metadata(pack_dir, metadata)
        adapter = EmbeddingAdapter(FakeEmbedding())
        asyncio.run(build_index(pack_dir, self.plugin_data, pack_id, adapter))
        self._write_runtime_files([pack_id])
        return pack_dir

    def test_share_export_strips_vectors_and_remains_importable(self):
        pack_dir = self._create_semantic_pack()
        private_metadata = load_metadata(pack_dir)
        private_metadata["provider_api_key"] = "must-not-be-shared"
        private_item = next(iter(private_metadata["images"].values()))
        private_item["provider_config"] = {"token": "must-not-be-shared"}
        private_item["vision_model"] = "private-local-model"
        save_metadata(pack_dir, private_metadata)
        result = pack_storage.export_pack_archive("demo", export_mode="share")

        with zipfile.ZipFile(result["archive_path"]) as archive:
            names = set(archive.namelist())
            transfer = json.loads(archive.read("meme_pack_export.json"))
            semantic = json.loads(archive.read("semantic_metadata.json"))

        self.assertEqual(transfer["export_mode"], "share")
        self.assertFalse(transfer["features"]["vectors"])
        self.assertNotIn("semantic_index/index.faiss", names)
        self.assertNotIn("provider_api_key", semantic)
        self.assertTrue(
            all(
                item.get("embedding_status") == "pending"
                for item in semantic["images"].values()
            )
        )
        self.assertTrue(
            all(
                "provider_config" not in item and "vision_model" not in item
                for item in semantic["images"].values()
            )
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

        restored = pack_storage.import_pack_archive(Path(result["archive_path"]))
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
        (broken_root / "semantic_index" / "index.faiss").write_bytes(
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
        restored = pack_storage.import_pack_archive(broken_archive)

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
