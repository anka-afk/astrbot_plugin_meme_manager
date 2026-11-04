"""验证模型驱动的即时发送，不调用外部模型或平台。"""

import asyncio
import base64
import copy
import io
import json
from threading import Event
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_meme_manager.backend.packs import storage as pack_storage
from astrbot_plugin_meme_manager.backend.semantic import query as semantic_query
from astrbot_plugin_meme_manager.mixins import event_handlers
from astrbot_plugin_meme_manager.mixins.event_handlers import EventHandlerMixin
from PIL import Image as PillowImage
from test_meme_only_replies import reply_context as reply_context
from test_multi_pack import runtime as runtime
from test_multi_pack import semantic_runtime as semantic_runtime
from test_probability_gates import tool_reply as tool_reply

from astrbot.core.agent.tool import FunctionTool
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import (
    MessageEventResult,
    ResultContentType,
)
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.provider.register import llm_tools

pytestmark = pytest.mark.asyncio


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
    assert not response["ok"]
    assert response["reason"] == (
        "semantic_unavailable" if mutation == "unbound" else "invalid_candidate"
    )
    if mutation not in {"unbound", "other_turn"}:
        assert "本轮搜索结果" in response["instruction"]
    assert not state.get("meme_manager_direct_send_attempted")
    sender.context.send_message.assert_not_awaited()
    sender._get_category_meme_files.assert_not_called()


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
    assert request.system_prompt.count("标签与工具二选一") == 1
    assert (
        provider.text_chat.await_count
        == search.await_count
        == int(trigger_probability > 0)
    )
    candidates = state["meme_manager_semantic_candidates"].copy()
    sender.emotion_llm_provider_id = "selector"
    for auxiliary in (True, False):
        sender.emotion_llm_enabled = auxiliary
        for _ in range(2):
            await sender._inject_meme_prompt_impl(event, request)
            assert state["meme_manager_semantic_candidates"] == candidates
            assert state["meme_manager_semantic_search_completed"]
            assert request.system_prompt.count("标签与工具二选一") == int(not auxiliary)
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


@pytest.mark.parametrize("backend", ["chat", "decision"])
@pytest.mark.parametrize("search_permission", ["granted", "missing", "inactive"])
async def test_auxiliary_semantic_mode_keeps_only_granted_direct_search_tools(
    tool_reply, backend, search_permission
):
    sender, event, state, request, shared, _, search = tool_reply
    sender.tool_probability_enabled = False
    sender.emotion_llm_enabled = True
    sender.emotion_llm_provider_id = "selector"
    sender.selection_backend = backend
    state["selected_provider"] = "actual-reply"
    for name in ("search_memes", "send_meme"):
        shared.add_tool(copy.copy(llm_tools.get_func(name)))
    sender._get_category_meme_files = lambda event, category: ["fixture.png"]
    if search_permission == "missing":
        shared.remove_tool("search_memes")
    elif search_permission == "inactive":
        shared.get_tool("search_memes").active = False
    await sender._inject_meme_prompt_impl(event, request)
    guidance = request.system_prompt + json.dumps(request.func_tool.openai_schema())
    assert all(word not in guidance for word in ("&&", "标记", "二选一", "meme tags"))
    granted = search_permission == "granted"
    assert state["meme_manager_direct_search_available"] is granted
    assert bool(request.func_tool.get_tool("search_memes")) is granted
    assert request.func_tool.get_tool("send_meme") is not None
    assert request.func_tool.get_tool("other_tool") is not None
    rejected = json.loads(await sender.send_meme_tool(event, "meme:ffffffffffff"))
    assert rejected["reason"] == "invalid_candidate"
    assert ("search_memes" in rejected["instruction"]) is granted
    if not granted:
        assert "分类键" in rejected["instruction"]
    assert not state.get("meme_manager_direct_send_attempted")
    result = json.loads(await sender.search_memes_tool(event, "celebrate"))
    assert all(
        word not in json.dumps(result, ensure_ascii=False)
        for word in ("&&", "标记", "二选一")
    )
    assert bool(result.get("ok")) is granted
    assert search.await_count == int(granted)
    if granted:
        assert not json.loads(await sender.search_memes_tool(event, "again"))["ok"]
        for auxiliary in (False, True):
            sender.emotion_llm_enabled = auxiliary
            request.func_tool = shared
            await sender._inject_meme_prompt_impl(event, request)
            assert not json.loads(await sender.search_memes_tool(event, "again"))["ok"]
        search.assert_awaited_once()


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


async def test_model_can_explicitly_retry_after_rejected_target(direct_sender):
    sender, event, state, _ = direct_sender
    assert not json.loads(await sender.send_meme_tool(event, "deleted-category"))["ok"]
    assert not state.get("meme_manager_direct_send_attempted")
    assert json.loads(await sender.send_meme_tool(event, "happy"))["ok"]
    sender.context.send_message.assert_awaited_once()


@pytest.mark.parametrize(
    "target", ["", None, "../happy", "待分类", "deleted", "meme:ffffffffffff"]
)
async def test_rejected_target_allows_a_later_category_tag(direct_sender, target):
    sender, event, state, _ = direct_sender
    sender.emotions_probability = 100
    sender.max_memes_per_message = -1
    result = json.loads(await sender.send_meme_tool(event, target))
    assert not result["ok"]
    assert not state.get("meme_manager_direct_send_attempted")
    sender.context.send_message.assert_not_awaited()
    response = LLMResponse(role="assistant", completion_text="Hello\n&&happy&&")
    await sender._resp_impl(event, response)
    assert response.completion_text == "Hello"
    assert state["found_emotions"] == ["happy"]
    state["result"] = MessageEventResult(
        chain=[Plain(response.completion_text)],
        result_content_type=ResultContentType.LLM_RESULT,
    )
    await sender._on_decorating_result_impl(event)
    assert len(state["meme_manager_pending_images"]) == 1
    await sender._after_message_sent_impl(event)
    sender.context.send_message.assert_awaited_once()


@pytest.mark.parametrize("already_searched", [False, True])
@pytest.mark.parametrize("delivery", ["tool", "tag"])
async def test_invalid_id_recovers_with_a_current_candidate(
    semantic_sender, already_searched, delivery
):
    sender, event, state, _ = semantic_sender
    sender.emotion_llm_enabled = False
    sender.emotions_probability = 100
    sender.max_memes_per_message = -1
    if already_searched:
        search = json.loads(await sender.search_memes_tool(event, "celebrate"))
    result = json.loads(await sender.send_meme_tool(event, "meme:ffffffffffff"))
    assert not result["ok"] and result["reason"] == "invalid_candidate"
    assert not state.get("meme_manager_direct_send_attempted")
    sender.context.send_message.assert_not_awaited()
    if already_searched:
        assert "本轮搜索结果" in result["instruction"]
    else:
        assert "请调用 search_memes" in result["instruction"]
        search = json.loads(await sender.search_memes_tool(event, "celebrate"))
    candidate_id = search["candidates"][0]["id"]
    if delivery == "tool":
        assert json.loads(await sender.send_meme_tool(event, candidate_id))["ok"]
    else:
        response = LLMResponse(
            role="assistant", completion_text=f"Hello\n&&{candidate_id}&&"
        )
        await sender._resp_impl(event, response)
        assert state["meme_manager_semantic_selected_ids"] == [candidate_id]
        state["result"] = MessageEventResult(
            chain=[Plain(response.completion_text)],
            result_content_type=ResultContentType.LLM_RESULT,
        )
        await sender._on_decorating_result_impl(event)
        await sender._after_message_sent_impl(event)
    sender.context.send_message.assert_awaited_once()
    sender._get_category_meme_files.assert_not_called()


async def test_completed_empty_search_offers_text_recovery(semantic_sender):
    sender, event, state, _ = semantic_sender
    state["meme_manager_semantic_search_completed"] = True
    state["meme_manager_semantic_candidates"] = {}
    result = json.loads(await sender.send_meme_tool(event, "meme:ffffffffffff"))
    assert result["reason"] == "invalid_candidate"
    assert "纯文字回复" in result["instruction"]
    assert "search_memes" not in result["instruction"]
    assert not state.get("meme_manager_direct_send_attempted")
    sender.context.send_message.assert_not_awaited()


@pytest.mark.parametrize("category_available", [False, True])
async def test_prompt_exposes_available_paths_without_example_ids(
    tool_reply, category_available
):
    sender, event, _, request, shared, _, _ = tool_reply
    sender.tool_probability_enabled = False
    shared.add_tool(copy.copy(llm_tools.get_func("send_meme")))
    sender._get_category_meme_files = lambda event, category: (
        ["fixture.png"] if category_available else []
    )
    await sender._inject_meme_prompt_impl(event, request)
    assert "先调用 search_memes" in request.system_prompt
    assert ("按分类选图" in request.system_prompt) is category_available
    assert ("[可用分类]" in request.system_prompt) is category_available
    assert "meme:123456789abc" not in request.system_prompt
    for name in ("search_memes", "send_meme"):
        tool = llm_tools.get_func(name)
        schema = json.dumps(tool.parameters)
        assert "meme:" not in tool.description + schema
        assert "&&" not in tool.description + schema


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


async def test_exact_semantic_candidates_ignore_attachment_limits(semantic_sender):
    sender, event, _, context = semantic_sender
    candidates = json.loads(await sender.search_memes_tool(event, "celebrate"))[
        "candidates"
    ]
    for candidate in candidates:
        assert json.loads(await sender.send_meme_tool(event, candidate["id"])) == {
            "ok": True,
            "status": "sent",
            "sent_count": 1,
        }
        chain = sender.context.send_message.await_args.args[1]
        expected = (
            context["pack_dirs"][candidate["_source_pack_id"]] / "memes/happy/meme.png"
        )
        assert expected.as_posix() in chain.chain[0].file.replace(chr(92), "/")
    assert sender.context.send_message.await_count == len(candidates) == 2
    assert {
        r["candidate_id"]
        for r in sender._delivery_history.recent(event.unified_msg_origin)
    } == {c["id"] for c in candidates}
    sender._get_category_meme_files.assert_not_called()


async def test_explicit_category_calls_ignore_probability_and_quantity(direct_sender):
    sender, event, state, _ = direct_sender
    state["meme_manager_send_probability_hit"] = False
    results = await asyncio.gather(
        *(sender.send_meme_tool(event, category) for category in ("happy", "sad"))
    )
    assert all(json.loads(result)["ok"] for result in results)
    assert sender.context.send_message.await_count == 2
    records = sender._delivery_history.recent(event.unified_msg_origin)
    assert {r["category"] for r in records} == {"happy", "sad"}
    assert all(len(r["content_sha256"]) == 64 for r in records)
    chain = sender.context.send_message.await_args.args[1]
    await sender._confirm_meme_delivery(event, chain.chain)
    assert len(sender._delivery_history.recent(event.unified_msg_origin)) == 2


async def test_valid_tool_attempt_blocks_automatic_images_and_auxiliary_calls(
    direct_sender,
):
    sender, event, state, _ = direct_sender
    sender.emotions_probability = 100
    sender.max_memes_per_message = -1
    sender.emotion_llm_enabled = True
    sender._emotion_llm_generate = AsyncMock(
        side_effect=AssertionError("No auxiliary call")
    )
    result = json.loads(await sender.send_meme_tool(event, "happy"))
    assert result["ok"]
    response = LLMResponse(role="assistant", completion_text="Hello &&happy&&")
    await sender._resp_impl(event, response)
    assert response.completion_text == "Hello" and not state["found_emotions"]
    state["result"] = MessageEventResult(
        chain=[Plain("Hello")], result_content_type=ResultContentType.LLM_RESULT
    )
    await sender._on_decorating_result_impl(event)
    await sender._after_message_sent_impl(event)
    await sender._send_memes_streaming(event, stream_completed=True)
    assert sender.context.send_message.await_count == int(result["ok"])
    sender._emotion_llm_generate.assert_not_awaited()
    assert not state.get("meme_manager_pending_images")


async def test_valid_semantic_attempt_does_not_restart_retrieval(semantic_sender):
    sender, event, state, _ = semantic_sender
    candidates = json.loads(await sender.search_memes_tool(event, "happy"))[
        "candidates"
    ]
    sender.emotion_llm_enabled = True
    state["meme_manager_semantic_mode"] = "llm"
    sender._build_emotion_semantic_query = AsyncMock(
        side_effect=AssertionError("No automatic retrieval")
    )
    target = candidates[0]["id"]
    assert json.loads(await sender.send_meme_tool(event, target))["ok"]
    response = LLMResponse(role="assistant", completion_text=f"Hello &&{target}&&")
    await sender._resp_impl(event, response)
    assert response.completion_text == "Hello"
    sender._build_emotion_semantic_query.assert_not_awaited()


@pytest.mark.parametrize(
    "failure", [False, RuntimeError("adapter failed"), asyncio.CancelledError()]
)
async def test_failed_delivery_cleans_conversion_without_recording_success(
    direct_sender, failure
):
    sender, event, state, root = direct_sender
    converted = root / "converted.gif"
    converted.write_bytes(b"converted")
    sender._convert_to_gif = lambda path: str(converted)
    if failure is False:
        sender.context.send_message.return_value = False
    else:
        sender.context.send_message.side_effect = failure
    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await sender.send_meme_tool(event, "happy")
    else:
        assert json.loads(await sender.send_meme_tool(event, "happy")) == {
            "ok": False,
            "reason": "send_failed",
        }
    assert not converted.exists() and (root / "happy/meme.png").is_file()
    assert not getattr(sender, "_delivery_history", None)
    assert state["meme_manager_direct_send_attempted"]


@pytest.mark.parametrize(
    "restriction",
    ["none", "disabled", "missing", "inactive", "modality", "empty", "origin"],
)
async def test_request_tool_permissions_preserve_shared_tools(tool_reply, restriction):
    sender, event, state, request, shared, provider, _ = tool_reply
    sender.tool_probability_enabled = False
    sender.send_meme_tool_enabled = restriction != "disabled"
    tool = FunctionTool(
        name="send_meme", description="Send", parameters={"type": "object"}
    )
    if restriction != "missing":
        shared.add_tool(tool)
    sender._get_category_meme_files = lambda event, category: ["fixture.png"]
    if restriction == "inactive":
        tool.active = False
    elif restriction == "modality":
        provider.provider_config["modalities"] = ["text"]
    elif restriction == "empty":
        sender._get_category_meme_files = lambda event, category: []
        sender._semantic_pack_ready = lambda **kwargs: False
    elif restriction == "origin":
        state["meme_manager_llm_request_origin"] = "plugin"
    await sender._inject_meme_prompt_impl(event, request)
    allowed = restriction == "none"
    assert state["meme_manager_send_tool_available"] is allowed
    assert bool(request.func_tool.get_tool("send_meme")) is allowed
    assert bool(shared.get_tool("send_meme")) is (restriction != "missing")
    assert request.func_tool.get_tool("other_tool") is not None
    await sender._inject_meme_prompt_impl(event, request)
    assert request.system_prompt.count("[主动发送表情工具]") == int(allowed)
    assert request.system_prompt.count("标签与工具二选一") == int(allowed)
    if not allowed:
        assert "send_meme" not in request.system_prompt


@pytest.mark.parametrize("semantic", [False, True])
async def test_prompt_mode_switch_preserves_user_text_and_shared_tool_schemas(
    tool_reply, semantic
):
    sender, event, state, request, shared, _, _ = tool_reply
    sender.tool_probability_enabled = False
    sender._semantic_pack_ready = lambda **kwargs: semantic
    sender.emotion_llm_provider_id = "selector"
    sender.prompt_head, sender.prompt_tail = "User head", "User tail\n&&happy&&"
    sender.prompt_examples = []
    context = sender._resolve_runtime_pack_context()
    context.update(memes_dir=context["pack_dir"], pack_dirs={})
    sender._resolve_runtime_pack_context = lambda **kwargs: context
    for name in ("search_memes", "send_meme"):
        shared.add_tool(copy.copy(llm_tools.get_func(name)))
    original_schema = shared.openai_schema()
    user_prompt = "User head: my instructions. User tail\n\n"
    request.system_prompt = user_prompt
    for auxiliary in (False, True, False):
        sender.emotion_llm_enabled = auxiliary
        request.func_tool = shared
        for _ in range(2):
            await sender._inject_meme_prompt_impl(event, request)
            assert request.system_prompt.count("标签与工具二选一") == int(not auxiliary)
            assert ("&&" in request.system_prompt) is (not auxiliary)
            assert sender._strip_meme_prompt(request.system_prompt) == user_prompt
            assert shared.openai_schema() == original_schema
    original_prompt = request.system_prompt
    other_state = {"meme_manager_llm_request_origin": "chat"}
    other_event, other_request = copy.copy(event), copy.copy(request)
    other_event.get_extra = other_state.get
    other_event.set_extra = lambda key, value: other_state.update({key: value})
    other_request.system_prompt = "Another session"
    sender.emotion_llm_enabled = True
    await sender._inject_meme_prompt_impl(other_event, other_request)
    assert "&&" not in other_request.system_prompt
    assert request.system_prompt == original_prompt
    assert state["meme_manager_selection_timing"] == "during_reply"


@pytest.mark.parametrize("as_base64", [False, True])
async def test_concurrent_gifs_survive_delayed_reads_and_clean_only_owned_files(
    direct_sender, monkeypatch, as_base64
):
    sender, first, state, root = direct_sender
    sender.convert_static_to_gif = True
    sender.send_image_as_base64 = as_base64
    temp_dir = root / "temp"
    monkeypatch.setattr(event_handlers, "get_astrbot_temp_path", lambda: str(temp_dir))
    monkeypatch.setattr(event_handlers.time, "time", lambda: 1234567890)
    monkeypatch.setattr(event_handlers.random, "randint", lambda *args: 1000)
    second = copy.copy(first)
    second_state = state.copy()
    second.get_extra = second_state.get
    second.set_extra = lambda key, value: second_state.update({key: value})
    second.unified_msg_origin += "-second"
    for category, color in (("happy", "red"), ("sad", "blue")):
        PillowImage.new("RGB", (2, 2), color).save(root / category / "meme.png")
    arrived = asyncio.Queue()
    gates = {event.unified_msg_origin: asyncio.Event() for event in (first, second)}
    received = {}

    async def delayed_send(umo, chain):
        image = chain.chain[0]
        received[umo] = image
        await arrived.put(umo)
        await gates[umo].wait()
        data = base64.b64decode(await image.convert_to_base64())
        with PillowImage.open(io.BytesIO(data)) as gif:
            expected = (255, 0, 0) if umo == first.unified_msg_origin else (0, 0, 255)
            assert gif.convert("RGB").getpixel((0, 0)) == expected
        if umo == first.unified_msg_origin:
            raise OSError("simulated adapter failure")
        return True

    sender.context.send_message.side_effect = delayed_send
    tasks = [
        asyncio.create_task(sender.send_meme_tool(first, "happy")),
        asyncio.create_task(sender.send_meme_tool(second, "sad")),
    ]
    try:
        for _ in tasks:
            await asyncio.wait_for(arrived.get(), timeout=3)
        owned = set(temp_dir.glob("*.gif"))
        assert len(owned) == 2

        def fail_save(image, output, *args, **kwargs):
            output.write(b"partial GIF")
            raise OSError("simulated conversion failure")

        with monkeypatch.context() as failing:
            failing.setattr(PillowImage.Image, "save", fail_save)
            source = str(root / "happy/meme.png")
            assert await asyncio.to_thread(sender._convert_to_gif, source) == source
        assert set(temp_dir.glob("*.gif")) == owned
        gates[first.unified_msg_origin].set()
        assert json.loads(await tasks[0])["reason"] == "send_failed"
        assert len(list(temp_dir.glob("*.gif"))) == 1
        gates[second.unified_msg_origin].set()
        assert json.loads(await tasks[1])["status"] == "sent"
        assert not list(temp_dir.glob("*.gif"))
        if as_base64:
            assert all(
                image.file.startswith("base64://") for image in received.values()
            )
            for image in received.values():
                assert await image.convert_to_base64()
    finally:
        for gate in gates.values():
            gate.set()
        await asyncio.gather(*tasks, return_exceptions=True)
