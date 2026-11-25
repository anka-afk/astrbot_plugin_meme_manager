import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_auto_collect_flow as flow
from astrbot_plugin_meme_manager.backend import manual_collect
from astrbot_plugin_meme_manager.main import MemeSender
from astrbot_plugin_meme_manager.mixins.commands import CommandMixin

from astrbot.core.message.components import Image, Plain, Reply


class Event(flow.FlowEvent):
    """为真实收集流程提供命令、身份与回复方法。"""

    def __init__(self, components=(), sender="admin", chat="100"):
        self.session_id = chat
        self.unified_msg_origin = f"test:GroupMessage:{chat}"
        self.sender = sender
        self.message_obj = SimpleNamespace(message=list(components))
        self.extra = {}
        self.send = AsyncMock()

    def get_sender_id(self):
        return self.sender

    def get_extra(self, key):
        return self.extra.get(key)

    def set_extra(self, key, value):
        self.extra[key] = value

    def plain_result(self, text):
        return text


class ManualCollectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.flow = flow.AutoCollectFlowTests()
        self.flow.setUp()
        self.addCleanup(self.flow.stack.close)
        self.host = self.flow.host
        self.pipeline = self.host.auto_collect_manager
        self.manager = manual_collect.ManualCollectManager(self.pipeline)
        self.host.manual_collect_manager = self.manager
        self.host.upload_states = {}

    async def asyncSetUp(self):
        await self.flow.asyncSetUp()

    async def asyncTearDown(self):
        await self.manager.close()
        await self.flow.asyncTearDown()

    async def test_quote_snapshots_all_images_without_passive_gates_or_double_submission(
        self,
    ):
        await self.pipeline.close()
        self.pipeline.enabled = False
        self.pipeline.scope = {"another-chat"}
        self.pipeline.sampling_probability = 0
        self.pipeline._cooldowns["group:100"] = time.monotonic()
        image = Image.fromFileSystem(self.flow.png)
        event = Event(
            [
                Reply(
                    id="quoted",
                    chain=[
                        Plain("text"),
                        image,
                        image,
                        Image.fromFileSystem(self.flow.gif),
                    ],
                ),
                Image.fromFileSystem(self.flow.root / "must-not-read.png"),
            ]
        )
        event.set_extra(
            "activated_handlers", [SimpleNamespace(handler_name="collect_memes")]
        )
        self.assertEqual(
            [
                result
                async for result in MemeSender.handle_upload_image(self.host, event)
            ],
            [],
        )
        replies = [
            result async for result in CommandMixin.collect_memes(self.host, event)
        ]
        self.assertEqual(replies, [])
        self.assertIn("已提交 3 张", event.send.call_args_list[0].args[0])
        self.assertEqual(
            [
                result
                async for result in MemeSender.handle_upload_image(self.host, event)
            ],
            [],
        )
        self.flow.png.unlink()
        self.flow.gif.unlink()
        await self.pipeline.queue.join()
        await asyncio.gather(*self.manager.tasks)
        self.assertEqual((await self.pipeline.pending_status("pack-a"))["count"], 2)
        self.assertEqual(self.flow.vision.await_count, 2)
        self.assertIn("重复 1 张", event.send.call_args.args[0])
        self.assertFalse(self.pipeline.enabled)

    async def test_continuous_scope_fixed_target_and_end_with_pending_work(self):
        event = Event()
        self.assertIn("已开始", await self.manager.start(event))
        session = self.manager.sessions[self.manager.key(event)]
        self.assertIn("已在收集", await self.manager.start(event))
        self.assertIs(session, self.manager.sessions[self.manager.key(event)])
        self.assertIsNone(await self.manager.consume(Event(sender="other")))
        self.assertIsNone(await self.manager.consume(Event(chat="other")))
        self.assertEqual(await self.manager.consume(Event([Plain("hello")])), "")
        release = asyncio.Event()
        original = self.pipeline._classify

        async def classify(*args):
            await release.wait()
            return await original(*args)

        self.pipeline._classify = classify
        self.pipeline.manual_review = False
        self.pipeline.target_pack_id = "changed-after-start"
        image_event = Event([Image.fromFileSystem(self.flow.png)])
        self.assertEqual(await self.manager.consume(image_event), "")
        self.assertIn("pack-a", image_event.send.call_args.args[0])
        self.assertIn("仍在处理 1 张", self.manager.stop(event))
        self.assertIsNone(await self.manager.consume(image_event))
        release.set()
        await self.pipeline.queue.join()
        await asyncio.gather(*self.manager.tasks)
        self.assertIn("已入包 1 张", event.send.call_args.args[0])
        self.assertEqual(
            len(list((self.flow.pack_dir / "memes" / "happy").glob("*.png"))), 1
        )

    async def test_forced_review_and_quota_are_reported_without_false_success(self):
        self.pipeline.manual_review = False
        self.pipeline.judgment_backend = "decision"
        self.pipeline._analyze_with_decision = AsyncMock(
            return_value={
                **self.flow.decision,
                "review_required": True,
                "review_reason": "uncertain",
            }
        )
        event = Event([Image.fromFileSystem(self.flow.png)])
        await self.manager.quick(event)
        await self.pipeline.queue.join()
        await asyncio.gather(*self.manager.tasks)
        self.assertEqual((await self.pipeline.pending_status("pack-a"))["count"], 1)
        self.assertFalse(list((self.flow.pack_dir / "memes").rglob("*.png")))
        self.pipeline.judgment_backend = "vision"
        self.pipeline._daily_call_available = lambda: False
        event = Event([Image.fromFileSystem(self.flow.gif)])
        await self.manager.quick(event)
        await self.pipeline.queue.join()
        await asyncio.gather(*self.manager.tasks)
        self.assertIn("识别额度不足 1 张", event.send.call_args.args[0])

    async def test_missing_quote_and_model_fail_before_creating_receiver(self):
        for components, expected in (
            ([], "请引用"),
            ([Reply(id="missing", chain=[])], "无法读取"),
            ([Reply(id="text", chain=[Plain("text")])], "没有可收集"),
        ):
            self.assertIn(expected, await self.manager.quick(Event(components)))
        await self.pipeline.close()
        self.pipeline.vision_provider_id = ""
        self.assertIn("无法开始", await self.manager.start(Event()))
        self.assertEqual(self.manager.sessions, {})

    async def test_expiry_and_unload_clear_receivers_without_late_notifications(self):
        event = Event()
        await self.manager.start(event)
        session = self.manager.sessions[self.manager.key(event)]
        session.last_activity -= manual_collect.IDLE_TIMEOUT_SECONDS + 1
        await session.expiry_task
        self.assertFalse(self.manager.sessions)
        self.assertIn("空闲", event.send.call_args.args[0])
        await self.manager.start(event)
        downloading = asyncio.Event()

        async def download(_image):
            downloading.set()
            await asyncio.Future()

        with patch.object(Image, "convert_to_file_path", download):
            submission = asyncio.create_task(
                self.manager.consume(Event([Image.fromFileSystem(self.flow.png)]))
            )
            await downloading.wait()
            await self.manager.close()
            await self.pipeline.close()
            with self.assertRaises(asyncio.CancelledError):
                await submission
        self.assertFalse(self.manager.tasks)
        self.assertFalse(self.manager.sessions)
        self.assertFalse(self.pipeline._inflight)
        self.assertFalse(list((self.flow.root / "queue").glob("*")))
        event.send.assert_awaited_once()
