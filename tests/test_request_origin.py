import logging
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_meme_manager.mixins import event_handlers
from astrbot_plugin_meme_manager.mixins.event_handlers import (
    LLM_REQUEST_ORIGIN_EXTRA_KEY,
    TRIGGER_SCOPE_CHAT_AND_PLUGIN,
    TRIGGER_SCOPE_CHAT_ONLY,
)
from test_meme_only_replies import reply_context as reply_context
from test_probability_gates import tool_reply as tool_reply

from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import (
    MessageChain,
    MessageEventResult,
    ResultContentType,
)
from astrbot.core.provider.entities import LLMResponse

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "scope", [TRIGGER_SCOPE_CHAT_ONLY, TRIGGER_SCOPE_CHAT_AND_PLUGIN]
)
@pytest.mark.parametrize("origin", [None, ""])
@pytest.mark.parametrize("has_provider_request", [False, True])
async def test_missing_request_origin_respects_scope_and_reuses_observers(
    tool_reply, monkeypatch, caplog, scope, origin, has_provider_request
):
    sender, event, state, request, shared, provider, search = tool_reply
    sender.trigger_scope = scope
    state[LLM_REQUEST_ORIGIN_EXTRA_KEY] = origin
    if not has_provider_request:
        state.pop("provider_request")
    event.send = AsyncMock()
    event.send_streaming = AsyncMock()
    logger = logging.getLogger(__name__)
    monkeypatch.setattr(event_handlers, "logger", logger)
    caplog.set_level(logging.DEBUG, logger=logger.name)

    await sender._inject_meme_prompt_impl(event, request)
    assert state[LLM_REQUEST_ORIGIN_EXTRA_KEY] == "plugin"
    send, send_streaming = event.send, event.send_streaming
    await sender._inject_meme_prompt_impl(event, request)
    assert event.send is send
    assert event.send_streaming is send_streaming
    assert state["meme_manager_delivery_observer_installed"]
    assert state["meme_manager_stream_filter_installed"]
    assert shared.get_tool("search_memes") is not None
    allowed = scope == TRIGGER_SCOPE_CHAT_AND_PLUGIN
    assert provider.text_chat.await_count == search.await_count == int(allowed)
    assert state.get("meme_manager_semantic_active", False) is allowed
    assert request.system_prompt.startswith("Persona")

    text = "Hello\n&&meme:0123456789ab&&"
    response = LLMResponse(role="assistant", completion_text=text)
    await sender._resp_impl(event, response)
    assert response.completion_text == ("Hello" if allowed else text)
    state["result"] = MessageEventResult(
        chain=[Plain(response.completion_text)],
        result_content_type=ResultContentType.LLM_RESULT,
    )
    await sender._on_decorating_result_impl(event)
    assert bool(state.get("meme_manager_pending_images")) is allowed
    records = [record for record in caplog.records if record.name == logger.name]
    fallback = [record for record in records if "缺少来源标记" in record.message]
    assert len(fallback) == 1
    assert fallback[0].levelno == logging.INFO
    assert "按插件请求" in fallback[0].message
    assert scope in fallback[0].message
    if not allowed:
        assert request.func_tool.get_tool("search_memes") is None
        assert any("跳过请求" in record.message for record in records)
        assert any("跳过响应" in record.message for record in records)


@pytest.mark.parametrize(
    "scope", [TRIGGER_SCOPE_CHAT_ONLY, TRIGGER_SCOPE_CHAT_AND_PLUGIN]
)
@pytest.mark.parametrize("origin", ["chat", "plugin", "unknown"])
async def test_request_hook_preserves_existing_origin(tool_reply, scope, origin):
    sender, event, state, request, _, provider, search = tool_reply
    sender.trigger_scope = scope
    state[LLM_REQUEST_ORIGIN_EXTRA_KEY] = origin
    await sender._inject_meme_prompt_impl(event, request)
    assert state[LLM_REQUEST_ORIGIN_EXTRA_KEY] == origin
    allowed = origin == "chat" or (
        origin == "plugin" and scope == TRIGGER_SCOPE_CHAT_AND_PLUGIN
    )
    assert provider.text_chat.await_count == search.await_count == int(allowed)


@pytest.mark.parametrize(
    "scope", [TRIGGER_SCOPE_CHAT_ONLY, TRIGGER_SCOPE_CHAT_AND_PLUGIN]
)
async def test_missing_origin_stream_obeys_scope(tool_reply, scope):
    sender, event, state, request, _, _, _ = tool_reply
    sender.trigger_scope = scope
    state.pop(LLM_REQUEST_ORIGIN_EXTRA_KEY)
    state.pop("provider_request")
    text = "Hello\n&&meme:0123456789ab&&"
    response = LLMResponse(role="assistant", completion_text=text)
    output = []

    async def source():
        yield MessageChain([Plain(text)])
        await sender._resp_impl(event, response)

    async def deliver(generator):
        async for chunk in generator:
            output.append(chunk.get_plain_text())
        return "delivered"

    event.send_streaming = deliver
    await sender._inject_meme_prompt_impl(event, request)
    assert await event.send_streaming(source()) == "delivered"
    allowed = scope == TRIGGER_SCOPE_CHAT_AND_PLUGIN
    assert "".join(output).strip() == "Hello"
    assert sender._send_meme_image.await_count == int(allowed)


@pytest.mark.parametrize(
    "scope", [TRIGGER_SCOPE_CHAT_ONLY, TRIGGER_SCOPE_CHAT_AND_PLUGIN]
)
async def test_response_without_request_origin_stays_rejected(
    tool_reply, monkeypatch, caplog, scope
):
    sender, event, state, _, _, _, _ = tool_reply
    sender.trigger_scope = scope
    state.pop(LLM_REQUEST_ORIGIN_EXTRA_KEY)
    logger = logging.getLogger(__name__)
    monkeypatch.setattr(event_handlers, "logger", logger)
    response = LLMResponse(role="assistant", completion_text="Hello &&happy&&")
    with caplog.at_level(logging.DEBUG, logger=logger.name):
        await sender._resp_impl(event, response)
    assert state.get(LLM_REQUEST_ORIGIN_EXTRA_KEY) is None
    assert response.completion_text == "Hello &&happy&&"
    assert "跳过响应" in caplog.text
    assert "未标记" in caplog.text
