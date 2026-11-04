"""仅模拟网络投递，验证真实 QQ 群流式缓冲区。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_meme_only_replies import reply_context as reply_context
from test_send_meme_tool import direct_sender as direct_sender

from astrbot.core.message.components import Image, Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.message_type import MessageType


@pytest.fixture
def qq_stream(direct_sender, monkeypatch):
    sdk = pytest.importorskip("botpy.message")
    from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
        QQOfficialMessageEvent,
    )

    sender, _, state, _ = direct_sender
    raw = object.__new__(sdk.GroupMessage)
    raw.group_openid = "test-group"
    event = object.__new__(QQOfficialMessageEvent)
    event.message_obj = SimpleNamespace(
        type=MessageType.GROUP_MESSAGE,
        raw_message=raw,
        message_id="trigger-message-id",
    )
    event.platform_meta = SimpleNamespace(name="qq_official")
    event.unified_msg_origin = (
        f"qq_official:{MessageType.GROUP_MESSAGE.value}:test-group"
    )
    event._extras = state
    event.send_buffer = None
    delivered = []
    control = {"failure": None}

    async def post_one(chain, stream=None):
        await asyncio.sleep(0)
        if any(isinstance(component, Image) for component in chain.chain):
            if control["failure"] is not None:
                raise control["failure"]
        delivered.append(chain)
        return {"id": "mock-sent"}

    event._post_send_one = post_one
    monkeypatch.setattr(AstrMessageEvent, "send_streaming", AsyncMock())
    return sender, event, delivered, control


@pytest.mark.asyncio
@pytest.mark.parametrize("calls", [1, 2])
async def test_qq_group_stream_keeps_text_before_and_after_tool_images(
    qq_stream, calls
):
    sender, event, delivered, _ = qq_stream
    await sender._mark_llm_request_origin_impl(event)

    async def source():
        yield MessageChain([Plain("before-tool-text")])
        for _ in range(calls):
            result = json.loads(await sender.send_meme_tool(event, "happy"))
            assert result["ok"]
        yield MessageChain([Plain("after-tool-text")])

    await event.send_streaming(source())
    text = "".join(
        component.text
        for chain in delivered
        for component in chain.chain
        if isinstance(component, Plain)
    )
    images = [
        component
        for chain in delivered
        for component in chain.chain
        if isinstance(component, Image)
    ]
    assert text == "before-tool-textafter-tool-text"
    assert len(images) == calls
    assert len(sender._delivery_history.recent(event.unified_msg_origin)) == calls
    assert event.send_buffer is None
    sender.context.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_qq_tool_sends_preserve_the_original_buffer(qq_stream):
    sender, event, delivered, _ = qq_stream
    buffered = MessageChain([Plain("unsent-text")])
    event.send_buffer = buffered
    results = await asyncio.gather(
        sender.send_meme_tool(event, "happy"), sender.send_meme_tool(event, "sad")
    )
    assert all(json.loads(result)["ok"] for result in results)
    assert event.send_buffer is buffered
    assert len(delivered) == 2
    assert all(
        len(chain.chain) == 1 and isinstance(chain.chain[0], Image)
        for chain in delivered
    )
    assert len(sender._delivery_history.recent(event.unified_msg_origin)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("buffered", [False, True])
@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_qq_tool_send_restores_buffer_on_error_or_cancellation(
    qq_stream, buffered, failure
):
    sender, event, _, control = qq_stream
    original = MessageChain([Plain("unsent-text")]) if buffered else None
    event.send_buffer = original
    control["failure"] = (
        RuntimeError("adapter failed")
        if failure == "error"
        else asyncio.CancelledError()
    )
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await sender.send_meme_tool(event, "happy")
    else:
        assert json.loads(await sender.send_meme_tool(event, "happy")) == {
            "ok": False,
            "reason": "send_failed",
        }
    assert event.send_buffer is original
    assert not getattr(sender, "_delivery_history", None)
