"""Exercise immediate model-driven sends without external model or platform calls."""

import asyncio
import json
from threading import Event
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_meme_manager.backend.packs import storage as pack_storage
from astrbot_plugin_meme_manager.backend.semantic import query as semantic_query
from astrbot_plugin_meme_manager.mixins.event_handlers import EventHandlerMixin
from test_meme_only_replies import reply_context as reply_context
from test_multi_pack import runtime as runtime
from test_multi_pack import semantic_runtime as semantic_runtime
from test_probability_gates import tool_reply as tool_reply

from astrbot.core.agent.tool import FunctionTool
from astrbot.core.message.components import Image, Plain
from astrbot.core.message.message_event_result import (
    MessageEventResult,
    ResultContentType,
)
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.provider.register import llm_tools


def test_registered_tool_exposes_one_target_and_describes_its_effect_and_result():
    tool = llm_tools.get_func("send_meme")
    assert tool is not None
    assert set(tool.parameters["properties"]) == {"target"}
    parameter = tool.parameters["properties"]["target"]
    assert parameter["type"] == "string"
    assert "Exact category key" in parameter["description"]
    assert "candidate ID" in parameter["description"]
    assert "explicitly asks" in tool.description
    assert "proactively" in tool.description
    assert "already used meme tags" in tool.description
    assert "ok=true" in tool.description and "status=sent" in tool.description
    assert "ok=false" in tool.description and "reason" in tool.description


@pytest.fixture
def direct_sender(reply_context):
    sender, event, state, root = reply_context
    sender.send_meme_tool_enabled = True
    sender.emotions_probability = 0
    sender.max_memes_per_message = 0
    event.unified_msg_origin = "test:group:123"
    state["meme_manager_send_tool_available"] = True
    sender.context = SimpleNamespace(send_message=AsyncMock(return_value=True))
    sender._send_meme_image = MethodType(EventHandlerMixin._send_meme_image, sender)
    return sender, event, state, root


@pytest.fixture
def semantic_sender(semantic_runtime):
    sender, event, state, context = semantic_runtime
    sender.send_meme_tool_enabled = True
    sender.emotions_probability = 0
    sender.max_memes_per_message = 0
    state["meme_manager_send_tool_available"] = True
    event.unified_msg_origin = "test:group:semantic"
    sender.context = SimpleNamespace(send_message=AsyncMock(return_value=True))
    sender._send_meme_image = MethodType(EventHandlerMixin._send_meme_image, sender)
    sender._get_category_meme_files = Mock(
        side_effect=AssertionError(
            "Semantic IDs must not fall back to random categories"
        )
    )
    return sender, event, state, context


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_index", [0, 1])
async def test_search_then_send_delivers_exact_candidate_from_its_bound_pack(
    semantic_sender, candidate_index
):
    sender, event, _, context = semantic_sender
    result = json.loads(await sender.search_memes_tool(event, "celebrate"))
    candidate = result["candidates"][candidate_index]
    response = json.loads(await sender.send_meme_tool(event, target=candidate["id"]))
    assert response == {"ok": True, "status": "sent", "sent_count": 1}
    chain = sender.context.send_message.await_args.args[1]
    expected = (
        context["pack_dirs"][candidate["_source_pack_id"]] / "memes/happy/meme.png"
    )
    assert expected.as_posix() in chain.chain[0].file.replace("\\", "/")
    record = sender._delivery_history.recent(event.unified_msg_origin)[0]
    assert record["candidate_id"] == candidate["id"]
    assert record["category"] == "happy"
    sender._get_category_meme_files.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation", ["unknown", "other_turn", "changed", "missing", "review", "unbound"]
)
async def test_semantic_tool_rejects_invalid_candidates_without_random_fallback(
    semantic_sender, mutation
):
    sender, event, state, context = semantic_sender
    result = json.loads(await sender.search_memes_tool(event, "celebrate"))
    candidate = result["candidates"][0]
    target = candidate["id"]
    pack = context["pack_dirs"][candidate["_source_pack_id"]]
    source = pack / "memes/happy/meme.png"
    if mutation == "unknown":
        target = "meme:" + "f" * 12
    elif mutation == "other_turn":
        state["meme_manager_semantic_candidates"] = {}
    elif mutation == "changed":
        source.write_bytes(b"changed")
    elif mutation == "missing":
        source.unlink()
    elif mutation == "review":
        record = next(iter(semantic_query.load_metadata(pack)["images"].values()))
        record["category"] = semantic_query.REVIEW_CATEGORY
    else:
        pack_storage.save_selection_rules(
            [{"id": "default", "scope": "default", "pack_ids": ["pack-a"]}]
        )
    response = json.loads(await sender.send_meme_tool(event, target=target))
    assert response == {
        "ok": False,
        "reason": "semantic_unavailable"
        if mutation == "unbound"
        else "invalid_candidate",
    }
    sender.context.send_message.assert_not_awaited()
    sender._get_category_meme_files.assert_not_called()


@pytest.mark.asyncio
async def test_semantic_candidate_is_revalidated_after_conversion(semantic_sender):
    sender, event, _, context = semantic_sender
    result = json.loads(await sender.search_memes_tool(event, "celebrate"))
    candidate = result["candidates"][0]
    source = context["pack_dirs"][candidate["_source_pack_id"]] / "memes/happy/meme.png"
    converted = source.parent / "converted.gif"

    def convert(path):
        converted.write_bytes(b"converted")
        source.write_bytes(b"changed during conversion")
        return str(converted)

    sender._convert_to_gif = convert
    response = json.loads(await sender.send_meme_tool(event, candidate["id"]))
    assert response == {"ok": False, "reason": "candidate_changed"}
    assert not converted.exists()
    sender.context.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_multiple_semantic_candidates_can_be_sent_with_zero_attachment_limit(
    semantic_sender,
):
    sender, event, _, _ = semantic_sender
    result = json.loads(await sender.search_memes_tool(event, "celebrate"))
    for candidate in result["candidates"]:
        assert json.loads(await sender.send_meme_tool(event, candidate["id"]))["ok"]
    assert sender.context.send_message.await_count == len(result["candidates"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger_probability", [0, 100])
async def test_prefetched_candidate_sending_preserves_the_existing_search_strategy(
    tool_reply, trigger_probability
):
    sender, event, state, request, shared, provider, search = tool_reply
    sender.tool_call_probability = trigger_probability
    sender.emotions_probability = 0
    sender.max_memes_per_message = 0
    shared.add_tool(
        FunctionTool(
            name="send_meme", description="Send", parameters={"type": "object"}
        )
    )
    sender._get_category_meme_files = lambda event, category: ["fixture.png"]
    sender.context.send_message = AsyncMock(return_value=True)
    sender._send_meme_image = MethodType(EventHandlerMixin._send_meme_image, sender)
    await sender._inject_meme_prompt_impl(event, request)
    assert request.func_tool.get_tool("search_memes") is None
    assert (
        provider.text_chat.await_count
        == search.await_count
        == int(trigger_probability > 0)
    )
    result = json.loads(await sender.send_meme_tool(event, "meme:0123456789ab"))
    assert result["ok"] is (trigger_probability > 0)
    assert sender.context.send_message.await_count == int(trigger_probability > 0)
    if not trigger_probability:
        assert result["reason"] == "invalid_candidate"
    assert state["meme_manager_semantic_search_completed"]


@pytest.mark.asyncio
async def test_semantic_send_tool_does_not_require_a_category_catalog(tool_reply):
    sender, event, state, request, shared, _, _ = tool_reply
    sender.tool_probability_enabled = False
    shared.add_tool(
        FunctionTool(
            name="send_meme", description="Send", parameters={"type": "object"}
        )
    )
    sender._get_category_meme_files = lambda event, category: []
    await sender._inject_meme_prompt_impl(event, request)
    assert state["meme_manager_send_tool_available"]
    assert request.func_tool.get_tool("send_meme") is not None
    assert request.func_tool.get_tool("search_memes") is not None
    assert "本轮 search_memes 返回的完整 meme:... 候选 ID" in request.system_prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["chat", "decision"])
@pytest.mark.parametrize("search_permission", ["granted", "missing", "inactive"])
async def test_auxiliary_semantic_mode_keeps_only_granted_direct_search_tools(
    tool_reply, backend, search_permission
):
    sender, event, state, request, shared, _, search = tool_reply
    sender.emotion_llm_enabled = True
    sender.emotion_llm_provider_id = "selector"
    sender.selection_backend = backend
    state["selected_provider"] = "actual-reply"
    shared.add_tool(
        FunctionTool(
            name="send_meme", description="Send", parameters={"type": "object"}
        )
    )
    sender._get_category_meme_files = lambda event, category: ["fixture.png"]
    if search_permission == "missing":
        shared.remove_tool("search_memes")
    elif search_permission == "inactive":
        shared.get_tool("search_memes").active = False
    await sender._inject_meme_prompt_impl(event, request)
    granted = search_permission == "granted"
    assert state["meme_manager_direct_search_available"] is granted
    assert bool(request.func_tool.get_tool("search_memes")) is granted
    assert request.func_tool.get_tool("send_meme") is not None
    assert request.func_tool.get_tool("other_tool") is not None
    result = json.loads(await sender.search_memes_tool(event, "celebrate"))
    assert bool(result.get("ok")) is granted
    assert search.await_count == int(granted)
    if granted:
        assert not json.loads(await sender.search_memes_tool(event, "again"))["ok"]
        search.assert_awaited_once()


@pytest.mark.asyncio
async def test_tool_ignores_zero_probability_and_quantity_and_records_delivery(
    direct_sender,
):
    sender, event, state, _ = direct_sender
    state["meme_manager_send_probability_hit"] = False
    result = json.loads(await sender.send_meme_tool(event, "happy"))
    assert result == {"ok": True, "status": "sent", "sent_count": 1}
    sender.context.send_message.assert_awaited_once()
    origin, chain = sender.context.send_message.await_args.args
    assert origin == event.unified_msg_origin
    assert len(chain.chain) == 1 and isinstance(chain.chain[0], Image)
    record = sender._delivery_history.recent(origin)[0]
    assert record["category"] == "happy" and len(record["content_sha256"]) == 64
    assert state["meme_manager_direct_send_attempted"]


@pytest.mark.asyncio
async def test_multiple_deliberate_calls_are_allowed_despite_quantity_limit(
    direct_sender,
):
    sender, event, _, _ = direct_sender
    results = await asyncio.gather(
        sender.send_meme_tool(event, "happy"), sender.send_meme_tool(event, "sad")
    )
    assert all(json.loads(result)["ok"] for result in results)
    assert sender.context.send_message.await_count == 2
    assert len(sender._delivery_history.recent(event.unified_msg_origin)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["disabled", "unavailable", "origin"])
async def test_tool_respects_its_switch_and_request_permissions(direct_sender, reason):
    sender, event, state, _ = direct_sender
    if reason == "disabled":
        sender.send_meme_tool_enabled = False
    elif reason == "unavailable":
        state["meme_manager_send_tool_available"] = False
    else:
        state["meme_manager_llm_request_origin"] = "plugin"
    result = json.loads(await sender.send_meme_tool(event, "happy"))
    assert not result["ok"]
    assert result["reason"] == (
        "tool_disabled" if reason == "disabled" else "tool_unavailable"
    )
    sender.context.send_message.assert_not_awaited()
    assert not state.get("meme_manager_direct_send_attempted")


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["missing", "../happy", "", None, "待分类"])
async def test_unknown_or_unsafe_categories_never_send(direct_sender, category):
    sender, event, state, _ = direct_sender
    assert not json.loads(await sender.send_meme_tool(event, category))["ok"]
    sender.context.send_message.assert_not_awaited()
    assert state.get("meme_manager_direct_send_attempted")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        "invalid_target",
        "invalid_category",
        "no_images",
        "semantic_unavailable",
        "invalid_candidate",
    ],
)
async def test_rejected_targets_do_not_trigger_automatic_images_or_auxiliary_calls(
    direct_sender, reason
):
    sender, event, state, _ = direct_sender
    sender.emotions_probability = 100
    sender.max_memes_per_message = -1
    sender.emotion_llm_enabled = True
    sender._build_emotion_llm_injection_suffix = lambda event: ""
    sender._build_meme_quantity_prompt = lambda: ""
    sender._emotion_llm_generate = AsyncMock(
        return_value=LLMResponse(
            role="assistant", completion_text='{"selected_ids":["sad"]}'
        )
    )
    target = "deleted-category"
    if reason == "invalid_target":
        target = ""
    elif reason == "no_images":
        target = "happy"
        original_get_files = sender._get_category_meme_files
        sender._get_category_meme_files = lambda event, category: (
            [] if category == "happy" else original_get_files(event, category)
        )
    elif reason in {"semantic_unavailable", "invalid_candidate"}:
        target = "meme:ffffffffffff"
        sender._semantic_mode_active = lambda event: reason == "invalid_candidate"
    assert json.loads(await sender.send_meme_tool(event, target)) == {
        "ok": False,
        "reason": reason,
    }
    sender._semantic_mode_active = lambda event: False
    await sender._resp_impl(
        event, LLMResponse(role="assistant", completion_text="Hello")
    )
    state["result"] = MessageEventResult(
        chain=[Plain("Hello")], result_content_type=ResultContentType.LLM_RESULT
    )
    await sender._on_decorating_result_impl(event)
    await sender._after_message_sent_impl(event)
    await sender._send_memes_streaming(event, stream_completed=True)
    sender._emotion_llm_generate.assert_not_awaited()
    sender.context.send_message.assert_not_awaited()
    assert not state.get("meme_manager_pending_images")


@pytest.mark.asyncio
async def test_rejected_semantic_id_does_not_restart_post_reply_retrieval(
    semantic_sender,
):
    sender, event, state, _ = semantic_sender
    sender.emotion_llm_enabled = True
    state["meme_manager_semantic_mode"] = "llm"
    sender._build_emotion_semantic_query = AsyncMock(
        side_effect=AssertionError("No automatic retrieval after the tool failed")
    )
    result = json.loads(await sender.send_meme_tool(event, "meme:ffffffffffff"))
    assert result == {"ok": False, "reason": "invalid_candidate"}
    await sender._resp_impl(
        event, LLMResponse(role="assistant", completion_text="Hello")
    )
    sender._build_emotion_semantic_query.assert_not_awaited()
    sender.context.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_can_explicitly_retry_after_rejected_target(direct_sender):
    sender, event, state, _ = direct_sender
    assert not json.loads(await sender.send_meme_tool(event, "deleted-category"))["ok"]
    assert state.get("meme_manager_direct_send_attempted")
    assert json.loads(await sender.send_meme_tool(event, "happy"))["ok"]
    sender.context.send_message.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tag_state",
    [
        "found_emotions",
        "meme_manager_semantic_selected_ids",
        "meme_manager_tag_image_prepared",
    ],
)
async def test_using_tags_prevents_a_tool_send_in_the_same_reply(
    direct_sender, tag_state
):
    sender, event, state, _ = direct_sender
    state[tag_state] = ["happy"]
    result = json.loads(await sender.send_meme_tool(event, "happy"))
    assert result == {"ok": False, "reason": "tag_delivery_selected"}
    sender.context.send_message.assert_not_awaited()
    assert not state.get("meme_manager_direct_send_attempted")


@pytest.mark.asyncio
@pytest.mark.parametrize("auxiliary", [False, True])
async def test_tool_send_prevents_automatic_attachment_and_auxiliary_calls(
    direct_sender, auxiliary
):
    sender, event, state, _ = direct_sender
    sender.emotions_probability = 100
    sender.max_memes_per_message = -1
    state["meme_manager_send_probability_hit"] = True
    sender.emotion_llm_enabled = auxiliary
    sender._emotion_llm_generate = AsyncMock(
        side_effect=AssertionError("No extra call")
    )
    assert json.loads(await sender.send_meme_tool(event, "happy"))["ok"]
    response = LLMResponse(role="assistant", completion_text="Hello &&happy&&")
    await sender._resp_impl(event, response)
    assert response.completion_text == "Hello"
    assert state["found_emotions"] == []
    state["result"] = MessageEventResult(
        chain=[Plain(response.completion_text)],
        result_content_type=ResultContentType.LLM_RESULT,
    )
    await sender._on_decorating_result_impl(event)
    await sender._send_memes_streaming(event, stream_completed=True)
    sender.context.send_message.assert_awaited_once()
    sender._emotion_llm_generate.assert_not_awaited()
    assert not state.get("meme_manager_pending_images")


@pytest.mark.asyncio
async def test_tool_send_skips_post_reply_semantic_retrieval(direct_sender):
    sender, event, _, _ = direct_sender
    assert json.loads(await sender.send_meme_tool(event, "happy"))["ok"]
    sender._build_emotion_semantic_query = AsyncMock(
        side_effect=AssertionError("No additional retrieval")
    )
    response = LLMResponse(
        role="assistant", completion_text="Hello &&meme:0123456789ab&&"
    )
    await sender._resp_semantic_llm_impl(event, response, response.completion_text)
    assert response.completion_text == "Hello"
    sender._build_emotion_semantic_query.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, RuntimeError("adapter failed")])
async def test_failed_adapter_does_not_report_success_or_record_history(
    direct_sender, failure
):
    sender, event, _, _ = direct_sender
    if failure is False:
        sender.context.send_message.return_value = False
    else:
        sender.context.send_message.side_effect = failure
    result = json.loads(await sender.send_meme_tool(event, "happy"))
    assert result == {"ok": False, "reason": "send_failed"}
    assert not getattr(sender, "_delivery_history", None)
    assert not sender._meme_send_allowed(event)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_converted_file_is_cleaned_after_send_failure_or_cancellation(
    direct_sender, cancelled
):
    sender, event, _, root = direct_sender
    converted = root / "converted.gif"
    converted.write_bytes(b"converted")
    sender._convert_to_gif = lambda path: str(converted)
    sender.context.send_message.side_effect = (
        asyncio.CancelledError() if cancelled else RuntimeError("adapter failed")
    )
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await sender.send_meme_tool(event, "happy")
    else:
        assert not json.loads(await sender.send_meme_tool(event, "happy"))["ok"]
    assert not converted.exists()
    assert (root / "happy/meme.png").is_file()


@pytest.mark.asyncio
async def test_pack_changes_during_conversion_do_not_send(direct_sender):
    sender, event, _, root = direct_sender
    converted = root / "converted.gif"

    def convert(path):
        converted.write_bytes(b"converted")
        sender._get_category_meme_files = lambda event, category: []
        return str(converted)

    sender._convert_to_gif = convert
    result = json.loads(await sender.send_meme_tool(event, "happy"))
    assert result == {"ok": False, "reason": "candidate_changed"}
    assert not converted.exists()
    sender.context.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelling_conversion_waits_for_worker_and_removes_its_output(
    direct_sender,
):
    sender, event, _, root = direct_sender
    started = asyncio.Event()
    release = Event()
    loop = asyncio.get_running_loop()
    converted = root / "converted.gif"

    def convert(path):
        loop.call_soon_threadsafe(started.set)
        release.wait(timeout=3)
        converted.write_bytes(b"converted")
        return str(converted)

    sender._convert_to_gif = convert
    task = asyncio.create_task(sender.send_meme_tool(event, "happy"))
    try:
        await asyncio.wait_for(started.wait(), 3)
        task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not converted.exists()
    sender.context.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_request_filtering_preserves_shared_tools_and_explains_sending_policy(
    tool_reply, enabled
):
    sender, event, state, request, shared, _, _ = tool_reply
    sender.tool_probability_enabled = False
    sender.send_meme_tool_enabled = enabled
    shared.add_tool(
        FunctionTool(
            name="send_meme", description="Send", parameters={"type": "object"}
        )
    )
    sender._get_category_meme_files = lambda event, category: ["fixture.png"]
    await sender._inject_meme_prompt_impl(event, request)
    assert state["meme_manager_send_tool_available"] is enabled
    assert bool(request.func_tool.get_tool("send_meme")) is enabled
    assert shared.get_tool("send_meme") is not None
    assert request.func_tool.get_tool("other_tool") is not None
    if enabled:
        assert "用户明确要求发表情时，应调用 send_meme" in request.system_prompt
        assert "平时也可以" in request.system_prompt
        assert "标签与工具二选一" in request.system_prompt
        assert "不受自动配图的概率和数量上限约束" in request.system_prompt
        await sender._inject_meme_prompt_impl(event, request)
        assert request.system_prompt.count("[主动发送表情工具]") == 1
    else:
        assert "[主动发送表情工具]" not in request.system_prompt


@pytest.mark.asyncio
async def test_tool_is_not_injected_when_not_granted_by_the_host(tool_reply):
    sender, event, state, request, _, _, _ = tool_reply
    sender.tool_probability_enabled = False
    await sender._inject_meme_prompt_impl(event, request)
    assert state["meme_manager_send_tool_available"] is False
    assert "[主动发送表情工具]" not in request.system_prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["inactive", "modality", "empty", "origin"])
async def test_restricted_send_tool_is_removed_from_only_the_current_request(
    tool_reply, restriction
):
    sender, event, state, request, shared, provider, _ = tool_reply
    sender.tool_probability_enabled = False
    tool = FunctionTool(
        name="send_meme", description="Send", parameters={"type": "object"}
    )
    shared.add_tool(tool)
    sender._get_category_meme_files = lambda event, category: ["fixture.png"]
    if restriction == "inactive":
        tool.active = False
    elif restriction == "modality":
        provider.provider_config["modalities"] = ["text"]
    elif restriction == "empty":
        sender._get_category_meme_files = lambda event, category: []
        sender._semantic_pack_ready = lambda **kwargs: False
    else:
        state["meme_manager_llm_request_origin"] = "plugin"
    await sender._inject_meme_prompt_impl(event, request)
    assert state["meme_manager_send_tool_available"] is False
    assert request.func_tool.get_tool("send_meme") is None
    assert shared.get_tool("send_meme") is tool
    assert request.func_tool.get_tool("other_tool") is not None
