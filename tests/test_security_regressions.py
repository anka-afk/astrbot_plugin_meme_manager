import importlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR.parent))

TEST_DATA_DIR = tempfile.TemporaryDirectory()
astrbot_path_module = types.ModuleType("astrbot.core.utils.astrbot_path")
astrbot_path_module.get_astrbot_data_path = lambda: TEST_DATA_DIR.name
astrbot_path_module.get_astrbot_plugin_data_path = lambda: str(
    Path(TEST_DATA_DIR.name) / "plugin_data"
)
sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
sys.modules.setdefault("astrbot.core", types.ModuleType("astrbot.core"))
sys.modules.setdefault("astrbot.core.utils", types.ModuleType("astrbot.core.utils"))
sys.modules.setdefault("astrbot.core.utils.astrbot_path", astrbot_path_module)
models = importlib.import_module("astrbot_plugin_meme_manager.backend.models")
category_manager_module = importlib.import_module(
    "astrbot_plugin_meme_manager.backend.category_manager"
)
pack_protocol = importlib.import_module(
    "astrbot_plugin_meme_manager.backend.pack_protocol"
)
pack_storage = importlib.import_module(
    "astrbot_plugin_meme_manager.backend.pack_storage"
)
semantic_models = importlib.import_module(
    "astrbot_plugin_meme_manager.backend.semantic_models"
)
file_handler_module = importlib.import_module(
    "astrbot_plugin_meme_manager.image_host.core.file_handler"
)


class CategoryPathSafetyTests(unittest.TestCase):
    def test_safe_category_resolves_inside_memes_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memes_root = Path(temp_dir) / "memes"
            memes_root.mkdir()
            with patch.object(
                models, "resolve_pack_context", return_value={"memes_dir": memes_root}
            ):
                category_path = models._get_category_path("happy")

            self.assertEqual(category_path, (memes_root / "happy").resolve())

    def test_rejects_traversal_and_multi_segment_categories(self):
        unsafe_values = (
            "..",
            ".",
            "../outside",
            "..\\outside",
            "/absolute",
            "C:\\absolute",
            "nested/category",
            "nested\\category",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            memes_root = Path(temp_dir) / "memes"
            memes_root.mkdir()
            with patch.object(
                models, "resolve_pack_context", return_value={"memes_dir": memes_root}
            ):
                for category in unsafe_values:
                    with self.subTest(category=category):
                        with self.assertRaises(ValueError):
                            models._get_category_path(category)

    def test_upload_rejects_traversal_before_creating_files(self):
        class UploadedFile:
            filename = "payload.png"
            stream = io.BytesIO(b"not-an-image")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            memes_root = root / "memes"
            memes_root.mkdir()
            with patch.object(
                models, "resolve_pack_context", return_value={"memes_dir": memes_root}
            ):
                with self.assertRaises(ValueError):
                    models.add_emoji_to_category("..", UploadedFile())

            self.assertFalse((root / "payload.png").exists())

    def test_chinese_upload_filename_keeps_a_supported_extension(self):
        class UploadedFile:
            filename = "表情包.png"
            stream = io.BytesIO(b"image-content")

        with tempfile.TemporaryDirectory() as temp_dir:
            memes_root = Path(temp_dir) / "memes"
            memes_root.mkdir()
            with patch.object(
                models, "resolve_pack_context", return_value={"memes_dir": memes_root}
            ):
                result = models.add_emoji_to_category("happy", UploadedFile())
                listed_files = models.get_emoji_by_category("happy")

            self.assertTrue(result["filename"].startswith("image_"))
            self.assertTrue(result["filename"].endswith(".png"))
            self.assertEqual(listed_files, [result["filename"]])
            self.assertTrue(Path(result["path"]).is_file())

    def test_upload_rejects_unsupported_extension_before_writing(self):
        class UploadedFile:
            filename = "表情包.txt"
            stream = io.BytesIO(b"not-an-image")

        with tempfile.TemporaryDirectory() as temp_dir:
            memes_root = Path(temp_dir) / "memes"
            memes_root.mkdir()
            with patch.object(
                models, "resolve_pack_context", return_value={"memes_dir": memes_root}
            ):
                with self.assertRaises(ValueError):
                    models.add_emoji_to_category("happy", UploadedFile())

            self.assertFalse((memes_root / "happy").exists())


class RemoteSyncPathSafetyTests(unittest.TestCase):
    def test_safe_nested_remote_category_stays_inside_base_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir) / "memes"
            handler = file_handler_module.FileHandler(base_dir)

            target = handler.get_file_path("animals/cats", "happy.png")

            self.assertEqual(
                target, (base_dir / "animals" / "cats" / "happy.png").resolve()
            )
            self.assertTrue(target.parent.is_dir())

    def test_rejects_remote_category_path_traversal_without_creating_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base_dir = root / "memes"
            handler = file_handler_module.FileHandler(base_dir)

            for category in (
                "../escape",
                "..\\escape",
                "/absolute",
                "C:\\absolute",
                "safe/../escape",
            ):
                with self.subTest(category=category):
                    with self.assertRaises(ValueError):
                        handler.get_file_path(category, "proof.png")

            self.assertFalse((root / "escape").exists())

    def test_rejects_remote_filename_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            handler = file_handler_module.FileHandler(Path(temp_dir) / "memes")

            for filename in (
                "../proof.png",
                "..\\proof.png",
                "/proof.png",
                "C:\\proof.png",
                "nested/proof.png",
                "",
            ):
                with self.subTest(filename=filename):
                    with self.assertRaises(ValueError):
                        handler.get_file_path("safe", filename)


class PackManifestCategorySafetyTests(unittest.TestCase):
    def test_rejects_manifest_categories_that_are_not_single_path_segments(self):
        for category in (
            "..",
            ".",
            "../private",
            "..\\private",
            "/absolute",
            "nested/category",
            "nested\\category",
        ):
            manifest = {
                "id": "unsafe-pack",
                "name": "不安全资源包",
                "version": "1.0.0",
                "categories": {category: {"description": "测试"}},
            }
            with self.subTest(category=category):
                with self.assertRaises(ValueError):
                    pack_protocol.validate_pack_manifest(manifest)

    def test_runtime_mapping_filters_unsafe_legacy_categories(self):
        mapping = semantic_models.runtime_category_mapping(
            {
                "happy": "开心",
                "../../private": "私有目录",
                "nested/category": "嵌套目录",
                semantic_models.REVIEW_CATEGORY: "待审核",
            }
        )

        self.assertEqual(mapping, {"happy": "开心"})

    def test_resolved_category_directory_stays_inside_memes_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memes_root = Path(temp_dir) / "memes"
            memes_root.mkdir()

            safe_path = category_manager_module.resolve_safe_category_directory(
                memes_root, "happy"
            )
            self.assertEqual(safe_path, (memes_root / "happy").resolve())

            with self.assertRaises(ValueError):
                category_manager_module.resolve_safe_category_directory(
                    memes_root, "../private"
                )


class DomXssRegressionTests(unittest.TestCase):
    def test_catalog_metadata_uses_text_content(self):
        source = (Path(__file__).parents[1] / "pages/catalog/script.js").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("meta.innerHTML", source)
        self.assertIn("maintainerMeta.textContent", source)
        self.assertIn("sourceMeta.textContent", source)

    def test_catalog_install_supports_unknown_size_cancel_and_reconnect(self):
        root = Path(__file__).parents[1] / "pages/catalog"
        source = (root / "script.js").read_text(encoding="utf-8")
        html = (root / "index.html").read_text(encoding="utf-8")

        self.assertIn("status?.progress !== null", source)
        self.assertIn('"community/install/cancel"', source)
        self.assertIn('apiGet("community/install/status")', source)
        self.assertIn("restoreActiveInstall", source)
        self.assertIn('id="install-progress-cancel"', html)

    def test_settings_dynamic_values_are_not_html_templates(self):
        source = (Path(__file__).parents[1] / "pages/settings/script.js").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("getPackOptions", source)
        self.assertNotIn('${rule.target || ""}', source)
        self.assertNotIn("errors.map((item) => `<li>${item}</li>`)", source)
        self.assertIn("option.textContent", source)
        self.assertIn("targetInputElement.value", source)


class ArchiveDownloadProgressTests(unittest.TestCase):
    def test_github_archive_streams_chunks_and_reports_progress(self):
        class DownloadResponse:
            status_code = 200
            headers = {"content-length": "6"}
            closed = False

            def iter_content(self, chunk_size):
                self.chunk_size = chunk_size
                yield b"abc"
                yield b"def"

            def close(self):
                self.closed = True

        response = DownloadResponse()
        progress = []
        with tempfile.TemporaryDirectory() as temp_dir:
            target_path = Path(temp_dir) / "archive.zip"
            with patch.object(
                pack_storage.requests,
                "get",
                return_value=response,
            ) as requests_get:
                pack_storage._download_github_archive(
                    "owner/repo",
                    "main",
                    target_path,
                    progress_callback=lambda phase, downloaded, total: progress.append(
                        (phase, downloaded, total)
                    ),
                )

            self.assertEqual(target_path.read_bytes(), b"abcdef")

        requests_get.assert_called_once_with(
            "https://github.com/owner/repo/archive/main.zip",
            timeout=(
                pack_storage.ARCHIVE_DOWNLOAD_CONNECT_TIMEOUT_SECONDS,
                pack_storage.ARCHIVE_DOWNLOAD_READ_TIMEOUT_SECONDS,
            ),
            stream=True,
        )
        self.assertEqual(
            progress,
            [
                ("downloading", 0, 6),
                ("downloading", 3, 6),
                ("downloading", 6, 6),
            ],
        )
        self.assertEqual(response.chunk_size, 1024 * 1024)
        self.assertTrue(response.closed)


class RuntimePackSwitchTests(unittest.TestCase):
    def test_category_manager_targets_new_default_pack_without_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            packs_root = Path(temp_dir) / "packs"
            contexts = {}
            for pack_id in ("pack-a", "pack-b"):
                pack_dir = packs_root / pack_id
                memes_dir = pack_dir / "memes"
                memes_dir.mkdir(parents=True)
                metadata_path = pack_dir / "memes_data.json"
                manifest_path = pack_dir / "manifest.json"
                metadata_path.write_text("{}", encoding="utf-8")
                manifest_path.write_text(
                    '{"id":"' + pack_id + '","categories":{}}',
                    encoding="utf-8",
                )
                contexts[pack_id] = {
                    "pack_id": pack_id,
                    "pack_dir": pack_dir,
                    "memes_dir": memes_dir,
                    "metadata_path": metadata_path,
                    "manifest_path": manifest_path,
                    "category_mapping": {},
                }

            active_pack = {"id": "pack-a"}

            def resolve_context():
                return contexts[active_pack["id"]]

            with patch.object(
                category_manager_module,
                "resolve_pack_context",
                side_effect=resolve_context,
            ):
                manager = category_manager_module.CategoryManager()
                self.assertTrue(manager.create_category("before-switch", "鏃у寘鍒嗙被"))
                active_pack["id"] = "pack-b"
                self.assertTrue(manager.create_category("after-switch", "鏂板寘鍒嗙被"))

            self.assertTrue(
                (contexts["pack-a"]["memes_dir"] / "before-switch").is_dir()
            )
            self.assertFalse(
                (contexts["pack-a"]["memes_dir"] / "after-switch").exists()
            )
            self.assertTrue((contexts["pack-b"]["memes_dir"] / "after-switch").is_dir())
            self.assertFalse(
                (contexts["pack-b"]["memes_dir"] / "before-switch").exists()
            )


class CategoryTransactionTests(unittest.TestCase):
    def test_create_rolls_back_new_directory_when_metadata_save_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            context = self._create_pack_context(Path(temp_dir), {"existing": "已有"})
            with patch.object(
                category_manager_module, "resolve_pack_context", return_value=context
            ):
                manager = category_manager_module.CategoryManager()
                with patch.object(
                    category_manager_module, "save_json", return_value=False
                ):
                    created = manager.create_category("new-category", "新分类")

            self.assertFalse(created)
            self.assertFalse((context["memes_dir"] / "new-category").exists())
            self.assertEqual(manager.descriptions, {"existing": "已有"})

    def test_rename_restores_directory_when_metadata_save_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            context = self._create_pack_context(Path(temp_dir), {"old": "旧分类"})
            old_path = context["memes_dir"] / "old"
            old_path.mkdir()
            (old_path / "meme.png").write_bytes(b"image")
            with patch.object(
                category_manager_module, "resolve_pack_context", return_value=context
            ):
                manager = category_manager_module.CategoryManager()
                with patch.object(
                    category_manager_module, "save_json", return_value=False
                ):
                    renamed = manager.rename_category("old", "new")

            self.assertFalse(renamed)
            self.assertTrue((old_path / "meme.png").is_file())
            self.assertFalse((context["memes_dir"] / "new").exists())
            self.assertEqual(manager.descriptions, {"old": "旧分类"})

    def test_delete_keeps_directory_when_metadata_save_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            context = self._create_pack_context(Path(temp_dir), {"keep": "保留"})
            category_path = context["memes_dir"] / "keep"
            category_path.mkdir()
            (category_path / "meme.png").write_bytes(b"image")
            with patch.object(
                category_manager_module, "resolve_pack_context", return_value=context
            ):
                manager = category_manager_module.CategoryManager()
                with patch.object(
                    category_manager_module, "save_json", return_value=False
                ):
                    deleted = manager.delete_category("keep")

            self.assertFalse(deleted)
            self.assertTrue((category_path / "meme.png").is_file())
            self.assertEqual(manager.descriptions, {"keep": "保留"})

    @staticmethod
    def _create_pack_context(root: Path, descriptions: dict[str, str]) -> dict:
        """创建分类事务测试所需的最小资源包目录。

        Args:
            root: 测试资源包根目录。
            descriptions: 初始分类描述。

        Returns:
            可供 ``resolve_pack_context`` 返回的资源包上下文。
        """
        pack_dir = root / "pack"
        memes_dir = pack_dir / "memes"
        memes_dir.mkdir(parents=True)
        metadata_path = pack_dir / "memes_data.json"
        manifest_path = pack_dir / "manifest.json"
        metadata_path.write_text(
            json.dumps(descriptions, ensure_ascii=False),
            encoding="utf-8",
        )
        manifest_path.write_text(
            json.dumps(
                {
                    "id": "test-pack",
                    "name": "测试资源包",
                    "version": "1.0.0",
                    "categories": {
                        category: {"description": description}
                        for category, description in descriptions.items()
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return {
            "pack_id": "test-pack",
            "pack_dir": pack_dir,
            "memes_dir": memes_dir,
            "metadata_path": metadata_path,
            "manifest_path": manifest_path,
            "category_mapping": descriptions,
        }


class RuntimePackModelSwitchTests(unittest.TestCase):
    def test_model_operations_follow_runtime_default_pack(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pack_a_memes = Path(temp_dir) / "pack-a" / "memes"
            pack_b_memes = Path(temp_dir) / "pack-b" / "memes"
            (pack_a_memes / "happy").mkdir(parents=True)
            (pack_b_memes / "happy").mkdir(parents=True)
            active_memes = {"path": pack_a_memes}

            with patch.object(
                models,
                "resolve_pack_context",
                side_effect=lambda: {"memes_dir": active_memes["path"]},
            ):
                self.assertEqual(
                    models._get_category_path("happy"),
                    (pack_a_memes / "happy").resolve(),
                )
                active_memes["path"] = pack_b_memes
                self.assertEqual(
                    models._get_category_path("happy"),
                    (pack_b_memes / "happy").resolve(),
                )


if __name__ == "__main__":
    unittest.main()
