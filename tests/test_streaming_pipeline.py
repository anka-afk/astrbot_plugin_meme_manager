"""Regress plugin-initiated streams that lose their final decoration phase."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import (
    MessageChain,
    MessageEventResult,
    ResultContentType,
)
from astrbot.core.pipeline.process_stage.method import star_request
from astrbot.core.pipeline.process_stage.method.agent_sub_stages import internal
from astrbot.core.pipeline.process_stage.stage import ProcessStage
from astrbot.core.pipeline.respond import stage as respond
from astrbot.core.pipeline.result_decorate import stage as decorate
from astrbot.core.pipeline.scheduler import PipelineScheduler
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot.core.star.star_handler import EventType
from astrbot_plugin_meme_manager.mixins import event_handlers
from test_meme_only_replies import reply_context as reply_context


@pytest.mark.asyncio
@pytest.mark.parametrize("semantic", [False, True])
async def test_plugin_stream_delivers_without_final_decoration(
    reply_context, monkeypatch, semantic
):
    sender, _, _, root = reply_context
    sender.trigger_scope = "chat_and_plugin_llm"
    sender.emotions_probability = 100
    sender._semantic_mode_active = lambda event: semantic
    selected_id = "meme:0123456789ab"
    monkeypatch.setattr(
        event_handlers,
        "validate_selected_id",
        lambda event, value, pack: (
            root / "happy" / "meme.png" if value == selected_id else None
        ),
    )

    message = AstrBotMessage()
    message.message = [Plain("hello")]
    message.message_str = "hello"
    message.message_id = "trigger-message"
    message.type = MessageType.GROUP_MESSAGE
    message.sender = MessageMember(user_id="user", nickname="User")
    message.self_id = "bot"
    message.group_id = "group"
    event = AstrMessageEvent(
        "hello", message, PlatformMetadata("qq_official", "test", "test"), "group"
    )
    event.is_at_or_wake_command = True
    event.send = AsyncMock()
    event.send_typing = AsyncMock()
    event.stop_typing = AsyncMock()
    event.set_extra("meme_manager_semantic_mode", "tool" if semantic else "")
    output = []

    async def deliver(generator, *_args, **_kwargs):
        async for chunk in generator:
            output.append(chunk.get_plain_text())
        # The platform may buffer text until its stream consumer returns.
        sender._send_meme_image.assert_not_awaited()
        event._has_send_oper = True

    event.send_streaming = deliver
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["provider_settings"].update(
        streaming_response=True,
        unsupported_streaming_strategy="realtime_segmenting",
    )
    context = SimpleNamespace(
        get_config=lambda **kwargs: config,
        conversation_manager=AsyncMock(),
    )
    ctx = SimpleNamespace(
        astrbot_config=config, plugin_manager=SimpleNamespace(context=context)
    )
    text = f"Hello &&{selected_id if semantic else 'happy'}&&"
    response = LLMResponse(role="assistant", completion_text=text)
    request = ProviderRequest(prompt="hello")
    runner = MagicMock()
    runner.done.return_value = True
    runner.get_final_llm_resp.return_value = response
    runner.stats.to_dict.return_value = {}
    provider = MagicMock()
    provider.provider_config = {"id": "test", "api_base": ""}
    provider.get_model.return_value = "test"
    runner.provider = provider
    monkeypatch.setattr(
        internal,
        "build_main_agent",
        AsyncMock(
            return_value=SimpleNamespace(
                agent_runner=runner,
                provider_request=request,
                provider=provider,
                reset_coro=None,
            )
        ),
    )
    monkeypatch.setattr(internal, "try_capture_follow_up", lambda event: None)
    monkeypatch.setattr(internal, "register_active_runner", lambda *args: None)
    monkeypatch.setattr(internal, "unregister_active_runner", lambda *args: None)
    monkeypatch.setattr(internal, "_record_internal_agent_stats", AsyncMock())
    monkeypatch.setattr(internal.Metric, "upload", AsyncMock())
    monkeypatch.setattr(internal.InternalAgentSubStage, "_save_to_history", AsyncMock())
    monkeypatch.setattr(
        internal.SessionServiceManager,
        "should_process_llm_request",
        AsyncMock(return_value=True),
    )

    async def hook(ev, kind, *args):
        if kind == EventType.OnWaitingLLMRequestEvent:
            await sender._mark_llm_request_origin_impl(ev)
        return False

    monkeypatch.setattr(internal, "call_event_hook", hook)

    async def run(agent, *_args, **_kwargs):
        yield MessageChain([Plain(text)])
        await sender._resp_impl(event, response)

    monkeypatch.setattr(internal, "run_agent", run)

    async def plugin_handler(ev):
        ev.call_llm = True
        yield request

    handler = SimpleNamespace(
        handler=plugin_handler,
        handler_name="request",
        handler_full_name="stream_probe.request",
        handler_module_path="stream_probe",
        event_filters=[],
        event_type=EventType.AdapterMessageEvent,
        extras_configs={},
    )
    decorator = SimpleNamespace(
        handler=sender._on_decorating_result_impl,
        handler_name="decorate",
        handler_module_path="stream_probe",
    )
    monkeypatch.setitem(
        star_request.star_map,
        "stream_probe",
        SimpleNamespace(name="stream_probe", display_name="stream_probe"),
    )
    monkeypatch.setattr(
        decorate.star_handlers_registry,
        "get_handlers_by_event_type",
        lambda kind, **kwargs: (
            [decorator] if kind == EventType.OnDecoratingResultEvent else []
        ),
    )
    event.set_extra("activated_handlers", [handler])
    stages = [ProcessStage(), decorate.ResultDecorateStage(), respond.RespondStage()]
    for stage in stages:
        await stage.initialize(ctx)
    scheduler = object.__new__(PipelineScheduler)
    scheduler.stages = stages
    await scheduler._process_stages(event)

    sender._send_meme_image.assert_awaited_once()
    event.send.assert_not_awaited()
    assert not event.get_extra("found_emotions")
    assert not event.get_extra("meme_manager_semantic_selected_ids")
    assert "".join(output) == "Hello "


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["source", "early_return", "flush", "cancel"])
@pytest.mark.parametrize("semantic", [False, True])
async def test_incomplete_stream_never_sends_images(
    reply_context, monkeypatch, interruption, semantic
):
    sender, event, state, root = reply_context
    sender.emotions_probability = 100
    sender._semantic_mode_active = lambda event: semantic
    selected_id = "meme:0123456789ab"
    monkeypatch.setattr(
        event_handlers,
        "validate_selected_id",
        lambda event, value, pack: (
            root / "happy" / "meme.png" if value == selected_id else None
        ),
    )
    state["meme_manager_semantic_mode"] = "tool" if semantic else ""
    closed = []
    text = f"Hello &&{selected_id if semantic else 'happy'}&&"
    response = LLMResponse(role="assistant", completion_text=text)
    failed = True

    async def source():
        try:
            await sender._resp_impl(event, response)
            yield MessageChain([Plain(text)])
            if interruption == "source":
                raise ValueError("source failed")
        finally:
            closed.append(True)

    async def deliver(generator):
        if failed and interruption == "early_return":
            await anext(generator)
            return
        async for _ in generator:
            pass
        if failed and interruption == "flush":
            raise ValueError("flush failed")
        if failed and interruption == "cancel":
            raise asyncio.CancelledError

    event.send_streaming = deliver
    await sender._mark_llm_request_origin_impl(event)
    # A previous stream's success must not authorize delivery for this one.
    state["meme_manager_stream_exhausted"] = True
    if interruption == "early_return":
        await event.send_streaming(source())
    else:
        with pytest.raises(
            asyncio.CancelledError if interruption == "cancel" else ValueError
        ):
            await event.send_streaming(source())
    sender._send_meme_image.assert_not_awaited()
    assert closed == [True]
    assert not state["meme_manager_stream_exhausted"]
    assert state["found_emotions"] is None
    assert state["meme_manager_semantic_selected_ids"] is None
    assert not state["meme_manager_image_only_reply"]
    assert state["meme_manager_legacy_probability_hit"] is None

    failed = False

    async def error_notice():
        yield MessageChain([Plain("The previous reply failed. Please try again.")])

    await event.send_streaming(error_notice())
    state["result"] = MessageEventResult(
        chain=[], result_content_type=ResultContentType.STREAMING_FINISH
    )
    await sender._on_decorating_result_impl(event)
    sender._send_meme_image.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "compatibility,probability,image_only,expected",
    [
        (False, 100, False, 0),
        (True, 0, False, 0),
        (False, 0, True, 0),
    ],
)
async def test_stream_completion_respects_attachment_settings(
    reply_context, compatibility, probability, image_only, expected
):
    sender, event, _, _ = reply_context
    sender.streaming_compatibility = compatibility
    sender.emotions_probability = probability
    text = "&&happy&&" if image_only else "Hello &&happy&&"
    response = LLMResponse(role="assistant", completion_text=text)

    async def source():
        yield MessageChain([Plain(text)])
        await sender._resp_impl(event, response)

    async def deliver(generator):
        async for _ in generator:
            pass
        return "delivered"

    event.send_streaming = deliver
    await sender._mark_llm_request_origin_impl(event)
    assert await event.send_streaming(source()) == "delivered"
    assert sender._send_meme_image.await_count == expected


@pytest.mark.asyncio
async def test_stream_completion_retains_request_origin_gate(reply_context):
    sender, event, state, _ = reply_context
    state["provider_request"] = ProviderRequest(prompt="hello")

    async def source():
        yield MessageChain([Plain("Hello")])
        state["found_emotions"] = ["happy"]

    async def deliver(generator):
        async for _ in generator:
            pass

    event.send_streaming = deliver
    await sender._mark_llm_request_origin_impl(event)
    await event.send_streaming(source())
    sender._send_meme_image.assert_not_awaited()
    assert state["found_emotions"] is None


@pytest.mark.asyncio
async def test_supplementary_send_failure_preserves_delivered_text(reply_context):
    sender, event, _, _ = reply_context
    sender.emotions_probability = 100
    sender._send_memes_streaming = AsyncMock(side_effect=ValueError("pack unavailable"))
    response = LLMResponse(role="assistant", completion_text="Hello &&happy&&")

    async def source():
        yield MessageChain([Plain(response.completion_text)])
        await sender._resp_impl(event, response)

    async def deliver(generator):
        async for _ in generator:
            pass
        return "delivered"

    event.send_streaming = deliver
    await sender._mark_llm_request_origin_impl(event)
    assert await event.send_streaming(source()) == "delivered"
    sender._send_memes_streaming.assert_awaited_once()
