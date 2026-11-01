import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_meme_only_replies import reply_context as reply_context
from test_multi_pack import runtime as runtime
from test_multi_pack import semantic_runtime as semantic_runtime

from astrbot.api.provider import LLMResponse
from astrbot.core.message.components import Image
from astrbot.core.message.message_event_result import MessageChain


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
@pytest.mark.parametrize(
    "kind,backend",
    [("category", "chat"), ("category", "decision"), ("semantic", "decision")],
)
async def test_abstention_ignores_reply_markers(request, kind, backend):
    sender, event, state, _ = request.getfixturevalue(
        "semantic_runtime" if kind == "semantic" else "reply_context"
    )
    marker = "happy"
    if kind == "semantic":
        marker = json.loads(await sender.search_memes_tool(event, "happy"))[
            "candidates"
        ][0]["id"]
        state["meme_manager_semantic_mode"] = "llm"
        sender._build_emotion_semantic_query = AsyncMock(return_value="happy")
    sender.emotion_llm_enabled = True
    sender.selection_backend = backend
    sender._build_emotion_llm_injection_suffix = lambda event: ""
    sender._build_meme_quantity_prompt = lambda: ""
    sender._emotion_llm_generate = AsyncMock(
        return_value=LLMResponse(
            role="assistant", completion_text='{"selected_ids":[]}'
        )
    )
    sender.decision_client = SimpleNamespace(
        ready=True,
        model="test",
        status=lambda: {"model": "test", "ready": True},
        evaluate=AsyncMock(
            return_value={"selection": {"type": "choice", "choice": "skip"}}
        ),
    )
    response = LLMResponse(role="assistant", completion_text=f"Hello &&{marker}&&")
    if kind == "semantic":
        await sender._resp_semantic_llm_impl(event, response, response.completion_text)
    else:
        await sender._resp_impl(event, response)
    assert response.completion_text == "Hello"
    assert not state.get("found_emotions") and not state.get(
        "meme_manager_semantic_selected_ids"
    )
    assert state["meme_manager_selection_result"].status == "abstained"
    if backend == "decision":
        sender._emotion_llm_generate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_query_provider_preserves_reply_override_only_when_inherited(
    reply_context, explicit
):
    sender, event, state, _ = reply_context
    sender.selection_backend = "decision"
    sender.semantic_query_source = "generated"
    sender.semantic_query_provider_id = "query-provider" if explicit else ""
    sender.emotion_llm_provider_id = "old-image-provider"
    state.update(selected_provider="actual-reply", selected_model="actual-model")
    sender.context = SimpleNamespace(
        get_provider_by_id=lambda _: object(),
        llm_generate=AsyncMock(
            return_value=LLMResponse(
                role="assistant", completion_text='{"query":"安慰"}'
            )
        ),
    )
    sender._build_emotion_llm_injection_suffix = lambda event: ""
    assert await sender._build_emotion_semantic_query(event, "别担心") == "安慰"
    kwargs = sender.context.llm_generate.await_args.kwargs
    assert kwargs["chat_provider_id"] == (
        "query-provider" if explicit else "actual-reply"
    )
    assert kwargs.get("model") == (None if explicit else "actual-model")
