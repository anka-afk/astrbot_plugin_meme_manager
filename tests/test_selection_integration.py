import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_meme_manager.backend.delivery_history import DeliveryHistory
from astrbot_plugin_meme_manager.mixins.event_handlers import EventHandlerMixin
from test_meme_only_replies import reply_context as reply_context
from test_multi_pack import runtime as runtime
from test_multi_pack import semantic_runtime as semantic_runtime

from astrbot.api.provider import LLMResponse
from astrbot.core.message.components import Image
from astrbot.core.message.message_event_result import MessageChain


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["chat", "decision"])
async def test_post_reply_abstention_ignores_stray_reply_tags(reply_context, backend):
    sender, event, state, _ = reply_context
    sender.emotion_llm_enabled = True
    sender.selection_backend = backend
    sender._build_emotion_llm_injection_suffix = lambda event: ""
    sender._build_meme_quantity_prompt = lambda: ""
    sender._emotion_llm_generate = AsyncMock(
        return_value=LLMResponse(role="assistant", completion_text='{"emotions":[]}')
    )

    async def evaluate(state, questions):
        return {name: {"type": "choice", "choice": "skip"} for name in questions}

    sender.decision_client = SimpleNamespace(ready=True, evaluate=evaluate)
    sender.decision_client.model = "test-decision"
    sender.decision_client.status = lambda: {"model": "test-decision", "ready": True}
    response = LLMResponse(role="assistant", completion_text="Hello &&happy&&")
    await sender._resp_impl(event, response)
    assert state["found_emotions"] == []
    assert response.completion_text == "Hello"
    assert state["meme_manager_selection_result"].status == "abstained"
    if backend == "decision":
        sender._emotion_llm_generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_query_decision_backend_uses_actual_reply_provider_and_model(
    reply_context,
):
    sender, event, state, _ = reply_context
    sender.selection_backend = "decision"
    sender.semantic_query_source = "generated"
    sender.semantic_query_provider_id = ""
    sender.emotion_llm_provider_id = "old-image-provider"
    state["selected_provider"] = "actual-reply-provider"
    state["selected_model"] = "actual-model"
    sender.context = SimpleNamespace(
        get_provider_by_id=lambda provider_id: object(),
        llm_generate=AsyncMock(
            return_value=LLMResponse(
                role="assistant", completion_text='{"query":"安慰对方"}'
            )
        ),
    )
    sender._build_emotion_llm_injection_suffix = lambda event: ""
    assert await sender._build_emotion_semantic_query(event, "别担心") == "安慰对方"
    kwargs = sender.context.llm_generate.await_args.kwargs
    assert kwargs["chat_provider_id"] == "actual-reply-provider"
    assert kwargs["model"] == "actual-model"


@pytest.mark.asyncio
async def test_explicit_query_provider_does_not_inherit_reply_model(reply_context):
    sender, event, state, _ = reply_context
    sender.selection_backend = "decision"
    sender.semantic_query_source = "generated"
    sender.semantic_query_provider_id = "query-provider"
    sender.context = SimpleNamespace(
        llm_generate=AsyncMock(
            return_value=LLMResponse(
                role="assistant", completion_text='{"query":"开心"}'
            )
        )
    )
    sender._build_emotion_llm_injection_suffix = lambda event: ""
    await sender._build_emotion_semantic_query(event, "真好")
    assert (
        sender.context.llm_generate.await_args.kwargs["chat_provider_id"]
        == "query-provider"
    )
    assert "model" not in sender.context.llm_generate.await_args.kwargs


@pytest.mark.asyncio
async def test_successful_deliveries_are_recorded_once_and_failures_are_not(
    reply_context,
):
    sender, event, state, root = reply_context
    event.unified_msg_origin = "test:group:123"
    sender._delivery_history = DeliveryHistory()
    sender._ensure_image_send_format = AsyncMock(side_effect=lambda image: image)
    sender.context = SimpleNamespace(send_message=AsyncMock())
    image = Image.fromFileSystem(str(root / "happy" / "meme.png"))
    sender._track_prepared_meme(
        event, image, root / "happy" / "meme.png", category="happy"
    )
    await EventHandlerMixin._send_meme_image(sender, event, image)
    await sender._confirm_meme_delivery(event, [image])
    records = sender._delivery_history.recent(event.unified_msg_origin)
    assert len(records) == 1 and records[0]["category"] == "happy"
    assert len(records[0]["content_sha256"]) == 64
    assert "source" not in records[0]
    another = Image.fromFileSystem(str(root / "sad" / "meme.png"))
    sender._track_prepared_meme(
        event, another, root / "sad" / "meme.png", category="sad"
    )
    sender.context.send_message.side_effect = RuntimeError("adapter failed")
    with pytest.raises(RuntimeError):
        await EventHandlerMixin._send_meme_image(sender, event, another)
    assert len(sender._delivery_history.recent(event.unified_msg_origin)) == 1


@pytest.mark.asyncio
async def test_send_observer_confirms_mixed_images_only_when_send_returns(
    reply_context,
):
    sender, event, state, root = reply_context
    event.unified_msg_origin = "test:group:123"
    event.send = AsyncMock()
    await sender._mark_llm_request_origin_impl(event)
    image = Image.fromFileSystem(str(root / "happy" / "meme.png"))
    sender._track_prepared_meme(
        event, image, root / "happy" / "meme.png", category="happy"
    )
    await event.send(MessageChain([image]))
    assert (
        sender._delivery_history.recent(event.unified_msg_origin)[0]["category"]
        == "happy"
    )


@pytest.mark.asyncio
async def test_cancelled_selector_propagates_and_does_not_repeat(reply_context):
    sender, event, state, _ = reply_context
    sender.emotion_llm_enabled = True
    sender.selection_backend = "chat"
    sender._build_emotion_llm_injection_suffix = lambda event: ""
    sender._build_meme_quantity_prompt = lambda: ""
    sender._emotion_llm_generate = AsyncMock(side_effect=asyncio.CancelledError())
    response = LLMResponse(role="assistant", completion_text="Hello")
    with pytest.raises(asyncio.CancelledError):
        await sender._resp_impl(event, response)
    await sender._resp_impl(event, response)
    sender._emotion_llm_generate.assert_awaited_once()


def test_twenty_context_turns_include_forty_visible_messages(reply_context):
    sender, _, _, _ = reply_context
    sender.emotion_llm_context_turns = 20
    request = SimpleNamespace(
        contexts=[
            {"role": "user" if index % 2 == 0 else "assistant", "content": str(index)}
            for index in range(60)
        ]
    )
    lines = sender._collect_emotion_context_lines_from_request(request)
    assert len(lines) == 40
    assert lines[0] == "用户: 20"
    assert lines[-1] == "助手: 59"


@pytest.mark.asyncio
async def test_semantic_abstention_cannot_be_overridden_by_reply_marker(
    semantic_runtime,
):
    sender, event, state, _ = semantic_runtime
    result = json.loads(await sender.search_memes_tool(event, "happy"))
    candidate_id = result["candidates"][0]["id"]
    state["meme_manager_semantic_mode"] = "llm"
    sender.selection_backend = "decision"
    sender._build_emotion_semantic_query = AsyncMock(return_value="happy")
    sender._build_emotion_llm_injection_suffix = lambda event: ""
    sender._build_meme_quantity_prompt = lambda: ""
    sender._emotion_llm_generate = AsyncMock(
        side_effect=AssertionError("No chat selection")
    )

    async def evaluate(state, questions):
        return {name: {"type": "choice", "choice": "skip"} for name in questions}

    sender.decision_client = SimpleNamespace(ready=True, evaluate=evaluate)
    sender.decision_client.model = "test-decision"
    sender.decision_client.status = lambda: {"model": "test-decision", "ready": True}
    response = LLMResponse(
        role="assistant", completion_text=f"Hello &&{candidate_id}&&"
    )
    await sender._resp_semantic_llm_impl(event, response, response.completion_text)
    assert state["meme_manager_semantic_selected_ids"] == []
    assert response.completion_text == "Hello"
    assert state["meme_manager_selection_result"].status == "abstained"


@pytest.mark.asyncio
async def test_empty_categories_do_not_call_either_backend(reply_context):
    sender, event, state, root = reply_context
    sender.emotion_llm_enabled = True
    sender.selection_backend = "decision"
    sender._build_emotion_llm_injection_suffix = lambda event: ""
    sender._build_meme_quantity_prompt = lambda: ""
    sender._get_category_meme_files = lambda event, category: []
    sender._emotion_llm_generate = AsyncMock(
        side_effect=AssertionError("No candidates")
    )
    evaluate = AsyncMock(side_effect=AssertionError("No candidates"))
    sender.decision_client = SimpleNamespace(ready=True, evaluate=evaluate)
    response = LLMResponse(role="assistant", completion_text="Hello")
    await sender._resp_impl(event, response)
    assert state["found_emotions"] == []
    evaluate.assert_not_awaited()
    sender._emotion_llm_generate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["err", "tool", "chunk", "tools"])
async def test_intermediate_or_error_response_does_not_claim_final_selection(
    reply_context, kind
):
    sender, event, state, _ = reply_context
    sender.emotion_llm_enabled = True
    sender._emotion_llm_generate = AsyncMock()
    response = LLMResponse(
        role=kind if kind in {"err", "tool"} else "assistant",
        completion_text="Intermediate",
        is_chunk=kind == "chunk",
        tools_call_name=["search"] if kind == "tools" else [],
    )
    await sender._resp_impl(event, response)
    assert not state.get("meme_manager_semantic_response_processed")
    sender._emotion_llm_generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_request_hook_keeps_original_user_preferences(reply_context):
    sender, event, state, _ = reply_context
    event.unified_msg_origin = "test:group:123"
    sender.emotion_llm_enabled = True
    sender.emotion_llm_provider_id = "image-chat"
    sender.emotion_llm_context_turns = 1
    sender.context = SimpleNamespace(
        persona_manager=SimpleNamespace(
            resolve_selected_persona=AsyncMock(
                return_value=("p", None, None, None, None)
            )
        ),
        get_config=lambda *args: {},
    )
    sender._apply_request_prompt = lambda *args: None
    request = SimpleNamespace(
        prompt="请只用文字回复",
        conversation=None,
        contexts=[{"role": "user", "content": "不要图片"}],
        model="reply-model",
    )
    await sender._inject_meme_prompt_impl(event, request)
    request.prompt = "Tool intermediate message"
    request.contexts = [{"role": "assistant", "content": "processing"}]
    await sender._inject_meme_prompt_impl(event, request)
    assert state["meme_manager_selection_user_message"] == "请只用文字回复"
    assert state["meme_manager_emotion_context_lines"] == ["用户: 不要图片"]


@pytest.mark.asyncio
async def test_cancelled_optional_hash_keeps_confirmed_delivery(
    reply_context, monkeypatch
):
    sender, event, _, root = reply_context
    event.unified_msg_origin = "test:group:123"
    image = Image.fromFileSystem(str(root / "happy" / "meme.png"))
    sender._track_prepared_meme(
        event, image, root / "happy" / "meme.png", category="happy"
    )
    monkeypatch.setattr(
        asyncio, "to_thread", AsyncMock(side_effect=asyncio.CancelledError())
    )
    with pytest.raises(asyncio.CancelledError):
        await sender._confirm_meme_delivery(event, [image])
    assert sender._delivery_history.recent(event.unified_msg_origin) == [
        {"category": "happy", "candidate_id": "", "content_sha256": ""}
    ]
