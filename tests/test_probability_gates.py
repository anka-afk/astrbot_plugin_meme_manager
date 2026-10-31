"""Exercise independent delivery and Tool trigger decisions without model services."""

import asyncio
import json
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.message.components import Image, Plain
from astrbot.core.message.message_event_result import (
    MessageChain,
    MessageEventResult,
    ResultContentType,
)
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot_plugin_meme_manager import main as plugin_main
from astrbot_plugin_meme_manager.main import MemeSender
from astrbot_plugin_meme_manager.mixins import event_handlers
from astrbot_plugin_meme_manager.mixins.event_handlers import (
    is_explicit_meme_request,
)
from test_meme_only_replies import reply_context as reply_context


@pytest.fixture
def tool_reply(reply_context, monkeypatch):
    sender, event, state, root = reply_context
    sender.emotions_probability = 100
    sender.tool_probability_enabled = True
    sender.tool_call_probability = 100
    sender.semantic_enabled = True
    sender.sys_prompt_add = ""
    sender.prompt_head = ""
    sender.semantic_top_k = 4
    sender.semantic_min_score = 0.2
    sender._semantic_mode_active = MethodType(MemeSender._semantic_mode_active, sender)
    sender._semantic_pack_ready = lambda **kwargs: True
    sender._resolve_embedding_provider = lambda pack: None
    sender._resolve_runtime_pack_context = lambda **kwargs: {
        "pack_ids": ["fixture"],
        "pack_dir": root,
        "pack_dirs": {"fixture": root},
        "category_mapping": sender.category_mapping,
    }
    state["meme_manager_runtime_pack_ids"] = ["fixture"]
    event.unified_msg_origin = "test:session"
    candidate = {"id": "meme:0123456789ab", "caption": "Joy", "tags": []}
    search = AsyncMock(return_value={"ok": True, "candidates": [candidate]})
    monkeypatch.setattr(plugin_main, "search_memes_multi_pack", search)
    monkeypatch.setattr(plugin_main, "get_pack_paths", lambda pack: {"pack_dir": root})
    monkeypatch.setattr(
        plugin_main, "candidate_records_multi_pack", lambda packs, items: items
    )
    monkeypatch.setattr(
        event_handlers,
        "validate_selected_id",
        lambda ev, value, ctx: (
            root / "happy/meme.png"
            if value in (ev.get_extra("meme_manager_semantic_candidates") or {})
            else None
        ),
    )
    provider = SimpleNamespace(
        provider_config={"modalities": ["tool_use"]},
        text_chat=AsyncMock(
            return_value=LLMResponse(
                role="assistant",
                tools_call_name=["search_memes"],
                tools_call_args=[{"query": "celebrate with user"}],
            )
        ),
    )
    sender.context = SimpleNamespace(
        persona_manager=SimpleNamespace(
            resolve_selected_persona=AsyncMock(
                return_value=("persona", None, None, None)
            )
        ),
        get_config=lambda *args: {},
        get_provider_by_id=lambda provider_id: provider,
        get_using_provider=lambda umo: provider,
        get_using_provider_async=AsyncMock(return_value=provider),
    )
    tools = ToolSet(
        [
            FunctionTool(
                name="search_memes",
                description="Search",
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                handler=sender.search_memes_tool,
            ),
            FunctionTool(
                name="other_tool", description="Other", parameters={"type": "object"}
            ),
        ]
    )
    request = ProviderRequest(
        prompt="hello", system_prompt="Persona", func_tool=tools, model="override-model"
    )
    state["provider_request"] = request
    return sender, event, state, request, tools, provider, search


@pytest.mark.asyncio
@pytest.mark.parametrize("send_probability", [0, 100])
@pytest.mark.parametrize("call_probability", [0, 100])
async def test_independent_boundaries(tool_reply, send_probability, call_probability):
    sender, event, state, request, shared, provider, search = tool_reply
    sender.emotions_probability = send_probability
    sender.tool_call_probability = call_probability
    await sender._inject_meme_prompt_impl(event, request)
    assert provider.text_chat.await_count == (call_probability == 100)
    assert search.await_count == (call_probability == 100)
    assert request.func_tool.names() == ["other_tool"]
    assert shared.names() == ["search_memes", "other_tool"]
    response = LLMResponse(
        role="assistant", completion_text="Hello\n&&meme:0123456789ab&&"
    )
    await sender._resp_impl(event, response)
    assert response.completion_text == "Hello"
    state["result"] = MessageEventResult(
        chain=[Plain(response.completion_text)],
        result_content_type=ResultContentType.LLM_RESULT,
    )
    await sender._on_decorating_result_impl(event)
    assert bool(state.get("meme_manager_pending_images")) == (
        send_probability == 100 and call_probability == 100
    )


@pytest.mark.asyncio
async def test_autonomous_strategy_keeps_search_optional(tool_reply):
    sender, event, state, request, shared, provider, search = tool_reply
    sender.tool_probability_enabled = False
    sender.emotions_probability = 0
    await sender._inject_meme_prompt_impl(event, request)
    provider.text_chat.assert_not_awaited()
    search.assert_not_awaited()
    assert request.func_tool is shared
    assert state.get("meme_manager_tool_probability_hit") is None
    await asyncio.gather(
        sender.search_memes_tool(event, "joy"),
        sender.search_memes_tool(event, "duplicate"),
    )
    search.assert_awaited_once()
    assert shared.get_tool("search_memes") is not None
    response = LLMResponse(role="assistant", completion_text="&&meme:0123456789ab&&")
    await sender._resp_impl(event, response)
    state["result"] = MessageEventResult(
        chain=response.result_chain.chain,
        result_content_type=ResultContentType.LLM_RESULT,
    )
    await sender._on_decorating_result_impl(event)
    assert not any(isinstance(c, Image) for c in state["result"].chain)
    assert not state.get("meme_manager_pending_images")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rolls, expected_call, expected_send",
    [([30, 80], True, False), ([80, 30], False, True)],
)
async def test_mid_probability_drawn_once_per_gate(
    tool_reply, monkeypatch, rolls, expected_call, expected_send
):
    sender, event, state, request, shared, provider, search = tool_reply
    sender.tool_call_probability = sender.emotions_probability = 50
    draws = iter(rolls)
    count = []

    def draw(probability):
        count.append(probability)
        return next(draws) <= probability

    monkeypatch.setattr(event_handlers, "probability_hit", draw)
    await asyncio.gather(
        sender._inject_meme_prompt_impl(event, request),
        sender._inject_meme_prompt_impl(event, request),
    )
    assert sender._meme_send_allowed(event) is expected_send
    assert sender._meme_send_allowed(event) is expected_send
    await sender._inject_meme_prompt_impl(event, request)
    await sender.search_memes_tool(event, "late duplicate")
    assert search.await_count == expected_call
    assert provider.text_chat.await_count == expected_call
    assert count == [50, 50]
    assert (
        request.system_prompt.count("<!-- meme_manager_semantic_prompt:start -->") == 1
    )
    assert bool(state["meme_manager_semantic_candidates"]) == expected_call
    assert shared.names() == ["search_memes", "other_tool"]


@pytest.mark.asyncio
async def test_preflight_preserves_provider_model_and_request(tool_reply):
    sender, event, state, request, _shared, provider, _search = tool_reply
    state["selected_provider"] = "selected-provider"
    sender.context.get_using_provider_async.side_effect = AssertionError(
        "Must honor override"
    )
    request.contexts = [{"role": "user", "content": "context"}]
    request.image_urls = ["fixture.png"]
    request.extra_user_content_parts = [{"type": "text", "text": "extra"}]
    await sender._inject_meme_prompt_impl(event, request)
    kwargs = provider.text_chat.call_args.kwargs
    assert kwargs["tool_choice"] == "required"
    assert kwargs["request_max_retries"] == 0
    assert kwargs["model"] == "override-model"
    assert kwargs["func_tool"].names() == ["search_memes"]
    assert (
        kwargs["contexts"] == request.contexts
        and kwargs["contexts"] is not request.contexts
    )
    assert kwargs["image_urls"] == ["fixture.png"]
    assert kwargs["extra_user_content_parts"] == request.extra_user_content_parts
    assert "meme:0123456789ab" in request.system_prompt
    assert request.prompt == "hello"
    assert not hasattr(request, "tool_choice")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "primary_tools,fallback_tools", [(True, True), (False, True), (True, False)]
)
async def test_preflight_uses_core_image_fallback_provider(
    tool_reply, primary_tools, fallback_tools
):
    from astrbot.core.astr_main_agent import (
        _get_fallback_chat_providers,
        _select_image_chat_provider,
    )
    from astrbot.core.provider.provider import Provider

    sender, event, state, request, _, primary, search = tool_reply
    primary.provider_config = {
        "id": "primary",
        "modalities": ["text"] + (["tool_use"] if primary_tools else []),
    }
    fallback = Mock(spec=Provider)
    fallback.provider_config = {
        "id": "vision",
        "modalities": ["text", "image"] + (["tool_use"] if fallback_tools else []),
    }
    fallback.text_chat = AsyncMock(return_value=primary.text_chat.return_value)
    state["selected_provider"] = "primary"
    sender.context.get_provider_by_id = lambda value: {
        "primary": primary,
        "vision": fallback,
    }[value]
    sender.context.get_config = lambda *args: {
        "agent_runner": {"config": {"model": {"fallback_provider_ids": ["vision"]}}}
    }
    request.image_urls = ["fixture.png"]
    # AstrBot has already selected the image fallback before invoking our hook,
    # and clears a model override that belonged to the original provider.
    actual = _select_image_chat_provider(
        primary,
        request,
        _get_fallback_chat_providers(primary, sender.context, ["vision"]),
    )
    assert actual is fallback
    request.model = None
    await sender._inject_meme_prompt_impl(event, request)
    primary.text_chat.assert_not_awaited()
    if fallback_tools:
        fallback.text_chat.assert_awaited_once()
        assert fallback.text_chat.call_args.kwargs["model"] is None
        search.assert_awaited_once()
    else:
        fallback.text_chat.assert_not_awaited()
        search.assert_not_awaited()


@pytest.mark.asyncio
async def test_required_duplicates_execute_once(tool_reply):
    sender, event, _, request, _, provider, search = tool_reply
    provider.text_chat.return_value = LLMResponse(
        role="assistant",
        tools_call_name=["search_memes", "search_memes"],
        tools_call_args=[{"query": "first"}, {"query": "second"}],
    )
    await sender._inject_meme_prompt_impl(event, request)
    search.assert_awaited_once()
    assert search.call_args.args[2] == "first"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "provider",
        "no_call",
        "bad_args",
        "wrong_tool",
        "search",
        "empty",
        "unsupported",
        "missing_tool",
    ],
)
async def test_failures_preserve_text_and_do_not_fallback(tool_reply, failure):
    sender, event, state, request, _, provider, search = tool_reply
    if failure == "provider":
        provider.text_chat.side_effect = TypeError("required unsupported")
    elif failure == "no_call":
        provider.text_chat.return_value = LLMResponse(
            role="assistant", completion_text="Ignored required"
        )
    elif failure == "bad_args":
        provider.text_chat.return_value.tools_call_args = [{"query": 123}]
    elif failure == "wrong_tool":
        provider.text_chat.return_value.tools_call_name = ["other_tool"]
    elif failure == "search":
        search.side_effect = RuntimeError("Index failed")
    elif failure == "empty":
        search.return_value = {"ok": True, "candidates": []}
    elif failure == "unsupported":
        provider.provider_config["modalities"] = ["text"]
    elif failure == "missing_tool":
        request.func_tool = ToolSet()
    await sender._inject_meme_prompt_impl(event, request)
    await sender._inject_meme_prompt_impl(event, request)
    response = LLMResponse(role="assistant", completion_text="Normal reply &&happy&&")
    await sender._resp_impl(event, response)
    assert "Normal reply" in response.completion_text
    assert state["meme_manager_semantic_mode"] == "tool"
    assert not state.get("meme_manager_semantic_candidates")
    assert not state.get("found_emotions")
    assert provider.text_chat.await_count <= 1 and search.await_count <= 1


@pytest.mark.asyncio
async def test_cancelled_attempt_not_retried(tool_reply):
    sender, event, state, request, _, provider, search = tool_reply
    started = asyncio.Event()

    async def wait(**kwargs):
        started.set()
        await asyncio.Event().wait()

    provider.text_chat.side_effect = wait
    task = asyncio.create_task(sender._inject_meme_prompt_impl(event, request))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await sender._inject_meme_prompt_impl(event, request)
    provider.text_chat.assert_awaited_once()
    search.assert_not_awaited()
    assert (
        json.loads(state["meme_manager_tool_probability_result"])["reason"]
        == "tool_search_cancelled"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("semantic", [False, True])
@pytest.mark.parametrize("probability", [0, 100])
@pytest.mark.parametrize("streaming", [False, True])
async def test_all_delivery_paths_use_gate(
    reply_context, monkeypatch, semantic, probability, streaming
):
    sender, event, state, root = reply_context
    sender.emotions_probability = probability
    sender._semantic_mode_active = lambda ev: semantic
    selected = "meme:0123456789ab" if semantic else "happy"
    state["meme_manager_semantic_mode"] = "tool" if semantic else ""
    monkeypatch.setattr(
        event_handlers, "validate_selected_id", lambda *args: root / "happy/meme.png"
    )
    response = LLMResponse(role="assistant", completion_text=f"&&{selected}&&")
    await sender._resp_impl(event, response)
    state["result"] = MessageEventResult(
        chain=response.result_chain.chain,
        result_content_type=ResultContentType.STREAMING_FINISH
        if streaming
        else ResultContentType.LLM_RESULT,
    )
    await sender._on_decorating_result_impl(event)
    if streaming:
        assert sender._send_meme_image.await_count == (probability == 100)
        await sender._send_memes_streaming(event)
        assert sender._send_meme_image.await_count == (probability == 100)
    else:
        assert any(isinstance(c, Image) for c in state["result"].chain) == (
            probability == 100
        )


def test_events_and_sessions_do_not_share_decisions(reply_context, monkeypatch):
    sender, event, _, _ = reply_context
    sender.emotions_probability = 50
    draws = iter([True, False, True])
    monkeypatch.setattr(event_handlers, "probability_hit", lambda _: next(draws))
    assert sender._meme_send_allowed(event)
    for expected in (False, True):
        state = {}
        other = SimpleNamespace(
            get_extra=state.get,
            set_extra=lambda key, value, state=state: state.update({key: value}),
        )
        assert sender._meme_send_allowed(other) is expected
    assert sender._meme_send_allowed(event)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("给我发个表情包", True),
        ("来张梗图看看", True),
        ("send me a meme", True),
        ("不要发表情包", False),
        ("先不用发梗图", False),
        ("别给我发个表情包", False),
        ("不要给我发个梗图", False),
        ("为什么发表情包？", False),
        ("为什么给我发个表情包？", False),
        ("表情包发出去了吗", False),
        ("这个表情包挺好笑的", False),
        ("我没让你发表情包", False),
        ("不知道怎么发表情包", False),
    ],
)
def test_explicit_meme_request_detection(reply_context, message, expected):
    _, event, _, _ = reply_context
    event.message_str = message
    assert is_explicit_meme_request(event) is expected


@pytest.mark.parametrize(
    ("message", "bypass", "probability", "expected", "source"),
    [
        ("给我发个表情包", True, 5, True, "explicit_request"),
        ("来个梗图", True, 5, True, "explicit_request"),
        ("给我发个表情包", True, 0, False, "probability"),
        ("给我发个表情包", False, 5, False, "probability"),
        ("不要发表情包", True, 5, False, "probability"),
        ("别给我发个表情包", True, 5, False, "probability"),
        ("为什么发表情包", True, 5, False, "probability"),
        ("为什么给我发个表情包", True, 5, False, "probability"),
    ],
)
def test_explicit_request_bypass_probability(
    reply_context,
    monkeypatch,
    message,
    bypass,
    probability,
    expected,
    source,
):
    sender, event, state, _ = reply_context
    event.message_str = message
    sender.emotions_probability = probability
    sender.explicit_request_bypass_probability = bypass
    draws = []

    def miss(configured_probability):
        draws.append(configured_probability)
        return False

    monkeypatch.setattr(event_handlers, "probability_hit", miss)
    assert sender._meme_send_allowed(event) is expected
    assert state["meme_manager_send_probability_source"] == source
    assert draws == ([] if source == "explicit_request" else [probability])


@pytest.mark.asyncio
@pytest.mark.parametrize("semantic", [False, True])
async def test_auxiliary_selection_is_independent_of_zero_send_probability(
    tool_reply, monkeypatch, semantic
):
    sender, event, state, request, _, provider, search = tool_reply
    sender.emotions_probability = 0
    sender.emotion_llm_enabled = True
    sender._emotion_llm_generate = AsyncMock(
        return_value=LLMResponse(
            role="assistant",
            completion_text=json.dumps(
                {"meme_id": "meme:0123456789ab"}
                if semantic
                else {"emotions": ["happy"]}
            ),
        )
    )
    sender._build_emotion_llm_injection_suffix = lambda event: ""
    sender._build_emotion_semantic_query = AsyncMock(return_value="joy")
    if semantic:
        state.update(
            meme_manager_semantic_active=True,
            meme_manager_semantic_mode="llm",
            meme_manager_semantic_verified_pack_ids="fixture",
        )
        monkeypatch.setattr(event_handlers, "search_memes_multi_pack", search)
        monkeypatch.setattr(
            event_handlers, "get_pack_paths", lambda pack: {"pack_dir": request.prompt}
        )
        monkeypatch.setattr(
            event_handlers, "candidate_records_multi_pack", lambda packs, items: items
        )
    response = LLMResponse(role="assistant", completion_text="Hello")
    await sender._resp_impl(event, response)
    sender._emotion_llm_generate.assert_awaited_once()
    assert search.await_count == semantic
    state["result"] = MessageEventResult(
        chain=[Plain(response.completion_text)],
        result_content_type=ResultContentType.LLM_RESULT,
    )
    await sender._on_decorating_result_impl(event)
    assert not state.get("meme_manager_pending_images")


@pytest.mark.asyncio
async def test_preflight_preserves_tool_permission_proxy(tool_reply):
    sender, event, _, request, _, provider, search = tool_reply
    calls = []

    class GuardedSearch(FunctionTool):
        async def call(self, context, **kwargs):
            calls.append((context.context.event, kwargs))
            return "error: Permission denied"

    request.func_tool = ToolSet(
        [
            GuardedSearch(
                name="search_memes",
                description="Guarded",
                parameters={"type": "object"},
            )
        ]
    )
    await sender._inject_meme_prompt_impl(event, request)
    assert calls == [(event, {"query": "celebrate with user"})]
    provider.text_chat.assert_awaited_once()
    search.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_interruption_keeps_both_gate_decisions(tool_reply, monkeypatch):
    sender, event, state, request, _, provider, search = tool_reply
    sender.trigger_scope = "chat_and_plugin_llm"
    sender.tool_call_probability = sender.emotions_probability = 50
    decisions = []
    monkeypatch.setattr(
        event_handlers,
        "probability_hit",
        lambda probability: decisions.append(probability) or True,
    )

    async def consume(source):
        async for _ in source:
            raise ValueError("stream interrupted")

    event.send_streaming = consume
    await sender._mark_llm_request_origin_impl(event)
    await sender._inject_meme_prompt_impl(event, request)
    assert sender._meme_send_allowed(event)
    state["meme_manager_semantic_selected_ids"] = ["meme:0123456789ab"]

    async def source():
        yield MessageChain([Plain("Partial reply")])

    with pytest.raises(ValueError, match="interrupted"):
        await event.send_streaming(source())
    assert state["meme_manager_semantic_selected_ids"] is None
    assert sender._meme_send_allowed(event)
    await sender._inject_meme_prompt_impl(event, request)
    assert decisions == [50, 50]
    provider.text_chat.assert_awaited_once()
    search.assert_awaited_once()
    sender._send_meme_image.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_events_have_independent_tool_decisions(tool_reply):
    sender, event, _, request, shared, provider, search = tool_reply
    await sender._inject_meme_prompt_impl(event, request)
    for origin, probability, expected_calls in [
        (event.unified_msg_origin, 0, 1),
        ("test:other-session", 100, 2),
    ]:
        state = {
            "meme_manager_llm_request_origin": "chat",
            "meme_manager_runtime_pack_ids": ["fixture"],
        }
        other = SimpleNamespace(**vars(event))
        other.unified_msg_origin = origin
        other.get_extra = state.get
        other.set_extra = lambda key, value, state=state: state.update({key: value})
        next_request = ProviderRequest(prompt="next message", func_tool=shared)
        sender.tool_call_probability = probability
        await sender._inject_meme_prompt_impl(other, next_request)
        assert search.await_count == expected_calls
        assert provider.text_chat.await_count == expected_calls
        assert state["meme_manager_tool_probability_hit"] == bool(probability)
    assert shared.names() == ["search_memes", "other_tool"]
