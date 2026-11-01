from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_meme_manager.mixins.event_handlers import EventHandlerMixin

from astrbot.core.message.components import Image
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.message_type import MessageType


@pytest.mark.asyncio
async def test_real_qq_group_adapter_preserves_trigger_message_id(monkeypatch):
    message_sdk = pytest.importorskip("botpy.message")
    from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
        QQOfficialMessageEvent,
    )

    raw_message = object.__new__(message_sdk.GroupMessage)
    raw_message.group_openid = "test-group"
    event = object.__new__(QQOfficialMessageEvent)
    event.message_obj = SimpleNamespace(
        type=MessageType.GROUP_MESSAGE,
        raw_message=raw_message,
        message_id="trigger-message-id",
    )
    event.platform_meta = SimpleNamespace(name="qq_official")
    event._extras = {}
    event.send_buffer = None
    post_group_message = AsyncMock(return_value={"id": "sent-image-id"})
    event.bot = SimpleNamespace(
        api=SimpleNamespace(post_group_message=post_group_message)
    )
    monkeypatch.setattr(
        QQOfficialMessageEvent,
        "_parse_to_qqofficial",
        AsyncMock(return_value=("", "image-data", None, None, None, None, None)),
    )
    monkeypatch.setattr(
        QQOfficialMessageEvent,
        "upload_group_and_c2c_image",
        AsyncMock(return_value={"file_info": "uploaded-image"}),
    )
    monkeypatch.setattr(AstrMessageEvent, "send", AsyncMock())
    sender = object.__new__(EventHandlerMixin)
    sender.send_image_as_base64 = False
    sender.context = SimpleNamespace(send_message=AsyncMock())

    await sender._send_meme_image(event, Image.fromBase64("image-data"))

    sender.context.send_message.assert_not_awaited()
    post_group_message.assert_awaited_once()
    payload = post_group_message.await_args.kwargs
    assert payload["group_openid"] == "test-group"
    assert payload["msg_id"] == "trigger-message-id"
    assert payload["msg_type"] == 7
    assert payload["media"] == {"file_info": "uploaded-image"}
    assert isinstance(payload["msg_seq"], int)
    assert event.send_buffer is None
