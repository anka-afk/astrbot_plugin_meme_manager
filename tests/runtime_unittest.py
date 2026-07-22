import asyncio
import importlib.util
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ASTRBOT_AVAILABLE = importlib.util.find_spec("astrbot") is not None
if ASTRBOT_AVAILABLE:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from astrbot_plugin_meme_manager.mixins import web_api as web_api_module
    from astrbot_plugin_meme_manager.mixins.event_handlers import EventHandlerMixin
    from astrbot_plugin_meme_manager.mixins.web_api import WebAPIMixin


class FakeEvent:
    def __init__(self):
        self.extra = {}
        self.unified_msg_origin = "telegram:test"

    def get_extra(self, key):
        return self.extra.get(key)

    def set_extra(self, key, value):
        self.extra[key] = value


@unittest.skipUnless(ASTRBOT_AVAILABLE, "当前 Python 环境没有 AstrBot 运行库")
class RuntimeBehaviorTests(unittest.TestCase):
    def test_vector_rebuild_guidance_requires_complete_captions_without_index(self):
        class Manager:
            def status(self, pack_id):
                self.pack_id = pack_id
                return {
                    "task_status": "idle",
                    "dimension_rebuild_required": True,
                    "semantic_caption_complete": True,
                    "index_ready": False,
                    "embedding_provider_id": "embedding-provider",
                    "embedding_model": "embedding-model",
                    "embedding_configured_dimension": 4096,
                }

        class Plugin(WebAPIMixin):
            def __init__(self):
                self.semantic_enabled = True
                self.semantic_task_manager = Manager()

        plugin = Plugin()
        guidance = plugin._semantic_rebuild_guidance("shared-pack")

        self.assertEqual(plugin.semantic_task_manager.pack_id, "shared-pack")
        self.assertTrue(guidance["semantic_rebuild_required"])
        self.assertTrue(guidance["semantic_caption_complete"])
        self.assertFalse(guidance["semantic_index_ready"])
        self.assertEqual(guidance["semantic_embedding_model"], "embedding-model")
        self.assertEqual(guidance["semantic_embedding_dimension"], 4096)

    def test_vector_rebuild_guidance_stays_off_while_task_is_running(self):
        class Manager:
            @staticmethod
            def status(pack_id):
                return {
                    "task_status": "running",
                    "dimension_rebuild_required": True,
                    "semantic_caption_complete": True,
                    "index_ready": False,
                }

        class Plugin(WebAPIMixin):
            semantic_enabled = True
            semantic_task_manager = Manager()

        guidance = Plugin()._semantic_rebuild_guidance("shared-pack")

        self.assertFalse(guidance["semantic_rebuild_required"])
        self.assertEqual(guidance["semantic_task_status"], "running")

    def test_vector_rebuild_guidance_stays_off_when_semantic_is_disabled(self):
        class Manager:
            def status(self, pack_id):
                raise AssertionError("语义功能关闭时不应读取任务状态")

        class Plugin(WebAPIMixin):
            semantic_enabled = False
            semantic_task_manager = Manager()

        guidance = Plugin()._semantic_rebuild_guidance("shared-pack")

        self.assertFalse(guidance["semantic_rebuild_required"])
        self.assertEqual(guidance["semantic_rebuild_pack_id"], "shared-pack")

    def test_vector_rebuild_guidance_failure_does_not_break_pack_operation(self):
        class Manager:
            @staticmethod
            def status(pack_id):
                raise RuntimeError(f"无法读取 {pack_id}")

        class Plugin(WebAPIMixin):
            semantic_enabled = True
            semantic_task_manager = Manager()

        guidance = Plugin()._semantic_rebuild_guidance("shared-pack")

        self.assertFalse(guidance["semantic_rebuild_required"])
        self.assertEqual(guidance["semantic_rebuild_pack_id"], "shared-pack")

    def test_setting_default_pack_returns_vector_rebuild_guidance(self):
        from quart import Quart

        class Manager:
            @staticmethod
            def status(pack_id):
                return {
                    "task_status": "idle",
                    "dimension_rebuild_required": pack_id == "shared-pack",
                    "semantic_caption_complete": True,
                    "index_ready": False,
                    "embedding_provider_id": "embedding-provider",
                    "embedding_model": "embedding-model",
                    "embedding_configured_dimension": 4096,
                }

        class Plugin(WebAPIMixin):
            semantic_enabled = True
            semantic_task_manager = Manager()

            @staticmethod
            def _reload_personas():
                return None

        async def run():
            app = Quart(__name__)
            with patch.object(
                web_api_module,
                "set_default_pack",
                return_value={"default_pack_id": "shared-pack"},
            ) as mocked_set_default:
                async with app.test_request_context(
                    "/default",
                    method="POST",
                    json={"pack_id": "shared-pack"},
                ):
                    response, status = await Plugin()._api_set_default_pack()
                    payload = await response.get_json()

            self.assertEqual(status, 200)
            mocked_set_default.assert_called_once_with("shared-pack")
            self.assertEqual(payload["default_pack_id"], "shared-pack")
            self.assertTrue(payload["semantic_rebuild_required"])
            self.assertEqual(payload["semantic_embedding_dimension"], 4096)

        asyncio.run(run())

    def test_pack_import_stage_accepts_archive_larger_than_quart_default(self):
        from quart import Quart

        captured = {}

        class Plugin(WebAPIMixin):
            @staticmethod
            def _cleanup_pack_import_sessions():
                return None

        def fake_inspection(path, *, suggested_pack_id):
            captured["size"] = Path(path).stat().st_size
            captured["suggested_pack_id"] = suggested_pack_id
            return {
                "pack_id": "large-demo",
                "name": "大体积测试包",
                "image_count": 1,
                "category_count": 1,
                "semantic_metadata": True,
                "vectors_present": False,
            }

        async def run():
            app = Quart(__name__)
            old_quart_limit = int(app.config["MAX_CONTENT_LENGTH"])
            upload_size = old_quart_limit + 1024 * 1024
            boundary = "meme-manager-large-upload"
            body = (
                (
                    f"--{boundary}\r\n"
                    'Content-Disposition: form-data; name="file"; '
                    'filename="large-demo.zip"\r\n'
                    "Content-Type: application/zip\r\n\r\n"
                ).encode()
                + (b"x" * upload_size)
                + f"\r\n--{boundary}--\r\n".encode()
            )
            headers = {
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            }
            with tempfile.TemporaryDirectory() as temp_dir:
                with (
                    patch.object(web_api_module, "TEMP_DIR", Path(temp_dir)),
                    patch.object(
                        web_api_module,
                        "inspect_pack_archive",
                        fake_inspection,
                    ),
                ):
                    async with app.test_request_context(
                        "/stage",
                        method="POST",
                        headers=headers,
                        data=body,
                    ):
                        response, status = await Plugin()._api_stage_pack_import()
                        payload = await response.get_json()

                self.assertEqual(status, 200)
                self.assertEqual(payload["pack_id"], "large-demo")
                self.assertTrue(payload["import_token"])
                self.assertEqual(captured["size"], upload_size)
                self.assertEqual(captured["suggested_pack_id"], "large-demo")

        asyncio.run(run())

    def test_pack_import_apply_returns_vector_rebuild_guidance(self):
        from quart import Quart

        token = "a" * 32

        class Manager:
            @staticmethod
            def status(pack_id):
                return {
                    "task_status": "idle",
                    "dimension_rebuild_required": pack_id == "shared-pack",
                    "semantic_caption_complete": True,
                    "index_ready": False,
                    "embedding_provider_id": "embedding-provider",
                    "embedding_model": "embedding-model",
                    "embedding_configured_dimension": 4096,
                }

        class Plugin(WebAPIMixin):
            semantic_enabled = True
            semantic_task_manager = Manager()

            @staticmethod
            def _cleanup_pack_import_sessions():
                return None

            @staticmethod
            def _reload_personas():
                return None

        def fake_import(path, **kwargs):
            self.assertTrue(Path(path).is_file())
            self.assertTrue(kwargs["set_as_default"])
            return {
                "pack_id": "shared-pack",
                "name": "分享包",
                "vectors_restored": False,
            }

        async def run():
            app = Quart(__name__)
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                session_dir = temp_path / "pack_import_sessions"
                session_dir.mkdir()
                (session_dir / f"{token}.zip").write_bytes(b"test archive")
                (session_dir / f"{token}.json").write_text(
                    '{"pack_id":"shared-pack","suggested_pack_id":"shared-pack"}',
                    encoding="utf-8",
                )
                with (
                    patch.object(web_api_module, "TEMP_DIR", temp_path),
                    patch.object(
                        web_api_module,
                        "import_pack_archive",
                        fake_import,
                    ),
                ):
                    async with app.test_request_context(
                        "/apply",
                        method="POST",
                        json={
                            "import_token": token,
                            "overwrite": False,
                            "set_as_default": True,
                        },
                    ):
                        response, status = await Plugin()._api_apply_pack_import()
                        payload = await response.get_json()

                self.assertEqual(status, 200)
                self.assertEqual(payload["pack_id"], "shared-pack")
                self.assertTrue(payload["semantic_rebuild_required"])
                self.assertEqual(payload["semantic_rebuild_pack_id"], "shared-pack")

        asyncio.run(run())

    def test_pack_download_uses_quart_compatible_attachment_name(self):
        captured = {}

        class Plugin(WebAPIMixin):
            async def _run_guarded_pack_file_operation(self, *args, **kwargs):
                return {
                    "archive_path": "/tmp/demo_share.zip",
                    "archive_filename": "demo_share.zip",
                }

        async def fake_send_file(path, **kwargs):
            captured["path"] = path
            captured.update(kwargs)
            return SimpleNamespace(status_code=200)

        async def run():
            fake_request = SimpleNamespace(args={"pack_id": "demo", "mode": "share"})
            with (
                patch.object(web_api_module, "request", fake_request),
                patch.object(web_api_module, "send_file", fake_send_file),
            ):
                response = await Plugin()._api_download_pack()

            self.assertEqual(response.status_code, 200)
            self.assertEqual(captured["path"], "/tmp/demo_share.zip")
            self.assertEqual(captured["mimetype"], "application/zip")
            self.assertTrue(captured["as_attachment"])
            self.assertEqual(captured["attachment_filename"], "demo_share.zip")
            self.assertNotIn("download_name", captured)

        asyncio.run(run())

    def test_cancelled_file_request_holds_lock_until_worker_finishes(self):
        class Manager:
            def __init__(self):
                self.active = set()

            def begin_external_pack_operation(self, pack_id, operation):
                self.active.add(pack_id)

            def end_external_pack_operation(self, pack_id):
                self.active.discard(pack_id)

        class Plugin(WebAPIMixin):
            def __init__(self):
                self.semantic_task_manager = Manager()

        async def run():
            plugin = Plugin()
            worker_started = threading.Event()
            worker_release = threading.Event()

            def blocking_file_operation(*, operation_guard=None):
                self.assertIsNone(operation_guard)
                worker_started.set()
                worker_release.wait(timeout=2)

            task = asyncio.create_task(
                plugin._run_guarded_pack_file_operation(
                    "demo",
                    "测试文件操作",
                    blocking_file_operation,
                )
            )
            self.assertTrue(await asyncio.to_thread(worker_started.wait, 1))
            task.cancel()
            await asyncio.sleep(0.05)
            self.assertIn("demo", plugin.semantic_task_manager.active)
            worker_release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertNotIn("demo", plugin.semantic_task_manager.active)

        asyncio.run(run())

    def test_semantic_response_is_processed_only_once(self):
        class Plugin(EventHandlerMixin):
            def __init__(self):
                self.selection_calls = 0

            @staticmethod
            def _semantic_mode_active(event):
                return True

            async def _resp_semantic_llm_impl(self, event, response, text):
                self.selection_calls += 1
                event.set_extra(
                    "meme_manager_semantic_selected_ids",
                    ["meme:123456789abc"],
                )

        async def run():
            plugin = Plugin()
            event = FakeEvent()
            event.set_extra("meme_manager_semantic_mode", "llm")
            event.set_extra("meme_manager_semantic_response_processed", False)
            response = SimpleNamespace(completion_text="正常回复")

            await plugin._resp_impl(event, response)
            await plugin._resp_impl(event, response)

            self.assertEqual(plugin.selection_calls, 1)
            self.assertEqual(
                event.get_extra("meme_manager_semantic_selected_ids"),
                ["meme:123456789abc"],
            )

        asyncio.run(run())

    def test_blank_emotion_provider_reuses_and_caches_reply_provider(self):
        class Context:
            def __init__(self):
                self.calls = 0

            async def get_current_chat_provider_id(self, *, umo):
                self.calls += 1
                self.last_umo = umo
                return "reply-provider"

        class Plugin(EventHandlerMixin):
            def __init__(self):
                self.emotion_llm_provider_id = ""
                self.context = Context()

        async def run():
            plugin = Plugin()
            event = FakeEvent()

            first = await plugin._resolve_emotion_llm_provider_id(event)
            second = await plugin._resolve_emotion_llm_provider_id(event)

            self.assertEqual(first, "reply-provider")
            self.assertEqual(second, "reply-provider")
            self.assertEqual(plugin.context.calls, 1)
            self.assertEqual(plugin.context.last_umo, "telegram:test")
            self.assertEqual(
                event.get_extra("meme_manager_reply_provider_id"),
                "reply-provider",
            )

        asyncio.run(run())

    def test_configured_emotion_provider_stays_independent(self):
        class Context:
            async def get_current_chat_provider_id(self, *, umo):
                raise AssertionError("配置了独立模型时不应查询回复模型")

        class Plugin(EventHandlerMixin):
            emotion_llm_provider_id = "emotion-provider"
            context = Context()

        async def run():
            provider_id = await Plugin()._resolve_emotion_llm_provider_id(FakeEvent())
            self.assertEqual(provider_id, "emotion-provider")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
