import importlib
import io
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


class CategoryPathSafetyTests(unittest.TestCase):
    def test_safe_category_resolves_inside_memes_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memes_root = Path(temp_dir) / "memes"
            memes_root.mkdir()
            with patch.object(models, "MEMES_DIR", memes_root):
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
            with patch.object(models, "MEMES_DIR", memes_root):
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
            with patch.object(models, "MEMES_DIR", memes_root):
                with self.assertRaises(ValueError):
                    models.add_emoji_to_category("..", UploadedFile())

            self.assertFalse((root / "payload.png").exists())


class DomXssRegressionTests(unittest.TestCase):
    def test_catalog_metadata_uses_text_content(self):
        source = (Path(__file__).parents[1] / "pages/catalog/script.js").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("meta.innerHTML", source)
        self.assertIn("maintainerMeta.textContent", source)
        self.assertIn("sourceMeta.textContent", source)

    def test_settings_dynamic_values_are_not_html_templates(self):
        source = (Path(__file__).parents[1] / "pages/settings/script.js").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("getPackOptions", source)
        self.assertNotIn('${rule.target || ""}', source)
        self.assertNotIn("errors.map((item) => `<li>${item}</li>`)", source)
        self.assertIn("option.textContent", source)
        self.assertIn("targetInputElement.value", source)


if __name__ == "__main__":
    unittest.main()
