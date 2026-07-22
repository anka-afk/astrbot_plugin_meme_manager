import asyncio
import importlib.util
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

ASTRBOT_AVAILABLE = importlib.util.find_spec("astrbot") is not None
if ASTRBOT_AVAILABLE:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
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

    def get_result(self):
        return None

    def get_platform_name(self):
        return "telegram"


@unittest.skipUnless(ASTRBOT_AVAILABLE, "当前 Python 环境没有 AstrBot 运行库")
class RuntimeBehaviorTests(unittest.TestCase):
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

    def test_legacy_response_is_processed_only_once(self):
        class Plugin(EventHandlerMixin):
            remove_invalid_alternative_markup = True
            emotion_llm_enabled = False
            max_emotions_per_message = 1

            @staticmethod
            def _semantic_mode_active(event):
                return False

            @staticmethod
            def _resolve_runtime_pack_context(event=None):
                return {"category_mapping": {"happy": "开心"}}

            @staticmethod
            def _read_config_value(
                path,
                default=None,
                *,
                legacy_paths=(),
                legacy_keys=(),
            ):
                return default

        async def run():
            plugin = Plugin()
            event = FakeEvent()
            event.set_extra("meme_manager_semantic_response_processed", False)
            response = SimpleNamespace(completion_text="今天真不错 &&happy&&")

            await plugin._resp_impl(event, response)
            await plugin._resp_impl(event, response)

            self.assertEqual(event.get_extra("found_emotions"), ["happy"])
            self.assertEqual(response.completion_text, "今天真不错")

        asyncio.run(run())

    def test_empty_selected_pack_does_not_reuse_previous_pack_categories(self):
        class Plugin(EventHandlerMixin):
            remove_invalid_alternative_markup = True
            emotion_llm_enabled = False
            max_emotions_per_message = 1
            category_mapping = {"happy": "stale previous pack category"}

            @staticmethod
            def _semantic_mode_active(event):
                return False

            @staticmethod
            def _resolve_runtime_pack_context(event=None):
                return {"category_mapping": {}}

            @staticmethod
            def _read_config_value(
                path,
                default=None,
                *,
                legacy_paths=(),
                legacy_keys=(),
            ):
                return default

        async def run():
            plugin = Plugin()
            event = FakeEvent()
            event.set_extra("meme_manager_semantic_response_processed", False)
            response = SimpleNamespace(completion_text="must not send &&happy&&")

            await plugin._resp_impl(event, response)

            self.assertEqual(event.get_extra("found_emotions"), [])

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
