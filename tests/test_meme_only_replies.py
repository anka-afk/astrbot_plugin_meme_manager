from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_meme_manager import main as plugin_main
from astrbot_plugin_meme_manager.main import MemeSender
from astrbot_plugin_meme_manager.mixins import event_handlers
from astrbot_plugin_meme_manager.mixins.event_handlers import (
    LLM_REQUEST_ORIGIN_EXTRA_KEY,
)
from PIL import Image as PillowImage

from astrbot.api.provider import LLMResponse
from astrbot.core.message.components import Image, Plain
from astrbot.core.message.message_event_result import (
    MessageChain,
    MessageEventResult,
    ResultContentType,
)
from astrbot.core.pipeline.respond.stage import RespondStage
from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.webchat.webchat_event import WebChatMessageEvent
from astrbot.core.platform.sources.webchat.webchat_queue_mgr import webchat_queue_mgr


@pytest.fixture
def reply_context(tmp_path):
    mapping = {"happy": "开心", "sad": "难过", "angry": "生气"}
    for tag in mapping:
        directory = tmp_path / tag
        directory.mkdir()
        PillowImage.new("RGB", (2, 2)).save(directory / "meme.png")
    sender = object.__new__(MemeSender)
    sender.config = {
        "generation": {
            "markup": {"enable_alternative": False, "enable_repeated_detection": False},
            "matching": {"enable_loose_matching": False},
        }
    }
    sender.category_mapping = mapping
    sender.trigger_scope = "only_chat_llm"
    sender.emotions_probability = 0
    sender.emotion_llm_enabled = False
    sender.remove_invalid_alternative_markup = False
    sender.convert_static_to_gif = False
    sender.enable_mixed_message = False
    sender.mixed_message_probability = 100
    sender.send_image_as_base64 = False
    sender.streaming_compatibility = True
    sender.content_cleanup_rule = r"&&[a-zA-Z]*&&"
    sender._semantic_mode_active = lambda event: False
    sender._resolve_runtime_pack_context = lambda **kwargs: {
        "category_mapping": mapping,
        "pack_dir": tmp_path,
    }
    sender._get_runtime_memes_dir_for_event = lambda event: tmp_path
    sender._send_meme_image = AsyncMock()
    state = {LLM_REQUEST_ORIGIN_EXTRA_KEY: "chat"}
    event = SimpleNamespace(
        get_extra=lambda key: state.get(key),
        set_extra=lambda key, value: state.update({key: value}),
        get_result=lambda: state.get("result"),
        get_platform_name=lambda: "test",
    )
    return sender, event, state, tmp_path


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("chain_response", [False, True])
async def test_marker_only_reply_delivers_all_images_without_blank_text(
    reply_context, streaming, mixed, chain_response
):
    sender, event, state, _ = reply_context
    sender.enable_mixed_message = mixed
    response = LLMResponse(
        role="assistant", completion_text="&&happy&&\n&&sad&&\n&&angry&&"
    )
    if chain_response:
        response.result_chain = MessageChain([Plain(response.completion_text)])
        sender.streaming_compatibility = False
    await sender._resp_impl(event, response)
    assert response.completion_text == ""
    assert response.result_chain
    assert state["found_emotions"] == ["happy", "sad", "angry"]
    state["result"] = MessageEventResult(
        chain=response.result_chain.chain,
        result_content_type=ResultContentType.STREAMING_FINISH
        if streaming
        else ResultContentType.LLM_RESULT,
    )
    await sender._on_decorating_result_impl(event)
    if streaming:
        assert sender._send_meme_image.await_count == 3
        assert not state["result"].chain
    else:
        assert len(state["result"].chain) == 3
        assert all(isinstance(component, Image) for component in state["result"].chain)
        assert not await RespondStage()._is_empty_message_chain(state["result"].chain)
        assert not state.get("meme_manager_pending_images")
        await sender._after_message_sent_impl(event)
        sender._send_meme_image.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_reply_still_respects_attachment_probability(reply_context):
    sender, event, state, _ = reply_context
    response = LLMResponse(role="assistant", completion_text="你好 &&happy&&")
    await sender._resp_impl(event, response)
    assert response.completion_text == "你好"
    assert not state.get("meme_manager_image_only_reply")
    state["result"] = MessageEventResult(
        chain=[Plain(response.completion_text)],
        result_content_type=ResultContentType.LLM_RESULT,
    )
    await sender._on_decorating_result_impl(event)
    assert len(state["result"].chain) == 1
    assert isinstance(state["result"].chain[0], Plain)
    assert not state.get("meme_manager_pending_images")


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_size", [1, 2, 5, 1000])
async def test_webchat_stream_cleans_tags_without_decorating_hook(
    reply_context, monkeypatch, chunk_size
):
    sender, fixture_event, state, _ = reply_context
    sender.emotions_probability = 100
    event = object.__new__(WebChatMessageEvent)
    event.__dict__.update(fixture_event.__dict__)
    event.message_obj = SimpleNamespace(message_id="tag-cleanup")
    event.session = SimpleNamespace(session_id="test")
    event.get_platform_name = lambda: "webchat"
    event.send = AsyncMock()
    monkeypatch.setattr(AstrMessageEvent, "send_streaming", AsyncMock())
    queue = AsyncMock(return_value=True)
    monkeypatch.setattr(webchat_queue_mgr, "put_back_queue", queue)
    text = "哈喽！晚上好呀。\n&&greeting&&\n&&happy&&\n`&&sad&&`"
    expected = "哈喽！晚上好呀。\n\n\n`&&sad&&`"
    response = LLMResponse(role="assistant", completion_text=text)

    async def source():
        # Request initialization happens when the platform starts the generator.
        state["meme_manager_stream_filtered"] = False
        for offset in range(0, len(text), chunk_size):
            yield MessageChain([Plain(text[offset : offset + chunk_size])])
        await sender._resp_impl(event, response)

    await sender._mark_llm_request_origin_impl(event)
    state["result"] = MessageEventResult(
        result_content_type=ResultContentType.STREAMING_RESULT,
        async_stream=source(),
    )
    # The real host stage intentionally skips the plugin's decorating hook.
    assert [item async for item in ResultDecorateStage().process(event)] == []
    await event.send_streaming(state["result"].async_stream, True)
    payloads = [call.args[1] for call in queue.await_args_list]
    assert "".join(p["data"] for p in payloads if p["type"] == "plain") == expected
    assert payloads[-1]["type"] == "complete"
    assert payloads[-1]["data"] == expected
    assert response.completion_text == expected
    event.send.assert_not_awaited()
    assert state["found_emotions"] == ["happy"]
    state["result"] = MessageEventResult(
        chain=[Plain(response.completion_text)],
        result_content_type=ResultContentType.STREAMING_FINISH,
    )
    await sender._on_decorating_result_impl(event)
    sender._send_meme_image.assert_awaited_once()


@pytest.mark.asyncio
async def test_nonstream_reply_cleans_known_and_unknown_tags(reply_context):
    sender, event, state, _ = reply_context
    sender.emotions_probability = 100
    response = LLMResponse(
        role="assistant", completion_text="哈喽！\n&&greeting&&\n&&happy&&"
    )
    await sender._resp_impl(event, response)
    assert response.completion_text == "哈喽！"
    state["result"] = MessageEventResult(
        chain=[Plain(response.completion_text)],
        result_content_type=ResultContentType.LLM_RESULT,
    )
    await sender._on_decorating_result_impl(event)
    assert [c.text for c in state["result"].chain if isinstance(c, Plain)] == ["哈喽！"]
    await sender._after_message_sent_impl(event)
    sender._send_meme_image.assert_awaited_once()


@pytest.mark.asyncio
async def test_stream_uses_pack_and_semantics_initialized_inside_source(reply_context):
    sender, event, state, _ = reply_context
    sender._semantic_mode_active = lambda event: bool(state.get("semantic"))

    async def source():
        state["semantic"] = True
        yield MessageChain([Plain("你好 meme:0123456789ab")])

    output = [chunk async for chunk in sender._filter_meme_stream(event, source())]
    assert "".join(c.get_plain_text() for c in output) == "你好 "


@pytest.mark.asyncio
async def test_stream_wrapper_filters_chunks_once_and_preserves_metadata(reply_context):
    sender, event, state, _ = reply_context
    reasoning = MessageChain([Plain("&&sad&&")], type="reasoning")
    chunks = [
        MessageChain([Plain("你好 &&ha")], use_markdown_=True),
        MessageChain([Plain("ppy&&\n`&&sad&&`")], use_markdown_=True),
    ]
    closed = []

    async def source():
        try:
            yield None
            yield reasoning
            for chunk in chunks:
                yield chunk
        finally:
            closed.append(True)

    state["result"] = MessageEventResult(
        result_content_type=ResultContentType.STREAMING_RESULT,
        async_stream=source(),
    )
    output = []

    async def deliver(generator, use_fallback=False):
        assert use_fallback
        output.extend([chunk async for chunk in generator])

    event.send_streaming = deliver
    await sender._mark_llm_request_origin_impl(event)
    wrapped = event.send_streaming
    await sender._mark_llm_request_origin_impl(event)
    assert event.send_streaming is wrapped
    await event.send_streaming(state["result"].async_stream, use_fallback=True)
    assert output[0] is reasoning
    visible = "".join(
        component.text for chunk in output[1:] for component in chunk.chain
    )
    assert visible == "你好 \n`&&sad&&`"
    assert all(chunk.use_markdown_ for chunk in output[1:])
    assert chunks[0].chain[0].text == "你好 &&ha"
    assert closed == [True]
    sender._send_meme_image.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_cancellation_closes_original_generator(reply_context):
    sender, event, _, _ = reply_context
    closed = []

    async def source():
        try:
            yield MessageChain([Plain("正在回复")])
            yield MessageChain([Plain("&&happy&&")])
        finally:
            closed.append(True)

    wrapped = sender._filter_meme_stream(event, source())
    first = await anext(wrapped)
    assert first.chain[0].text == "正在回复"
    await wrapped.aclose()
    assert closed == [True]


@pytest.mark.asyncio
async def test_stream_final_response_selects_images_without_replacement_message(
    reply_context,
):
    sender, event, state, _ = reply_context
    event.get_platform_name = lambda: "webchat"
    event.send = AsyncMock()
    response = LLMResponse(role="assistant", completion_text="&&happy&&&&sad&&")

    async def source():
        for delta in ("&&ha", "ppy&&&", "&sad&&"):
            yield MessageChain([Plain(delta)])
        await sender._resp_impl(event, response)

    state["result"] = MessageEventResult(
        result_content_type=ResultContentType.STREAMING_RESULT,
        async_stream=source(),
    )

    async def deliver(generator, use_fallback=False):
        assert [chunk async for chunk in generator] == []

    event.send_streaming = deliver
    await sender._mark_llm_request_origin_impl(event)
    await event.send_streaming(state["result"].async_stream)
    assert state["found_emotions"] == ["happy", "sad"]
    event.send.assert_not_awaited()
    state["result"] = MessageEventResult(
        chain=response.result_chain.chain,
        result_content_type=ResultContentType.STREAMING_FINISH,
    )
    await sender._on_decorating_result_impl(event)
    assert sender._send_meme_image.await_count == 2


@pytest.mark.asyncio
async def test_stream_error_propagates_and_closes_source(reply_context):
    sender, event, _, _ = reply_context
    closed = []

    async def source():
        try:
            yield MessageChain([Plain("正常文本")])
            raise ValueError("source failure")
        finally:
            closed.append(True)

    with pytest.raises(ValueError, match="source failure"):
        async for _ in sender._filter_meme_stream(event, source()):
            pass
    assert closed == [True]


@pytest.mark.asyncio
async def test_adjacent_plain_components_share_literal_context(reply_context):
    sender, event, state, _ = reply_context
    original = [Plain("```\n"), Plain("&&happy&&\n"), Plain("```")]
    state["result"] = MessageEventResult(
        chain=original, result_content_type=ResultContentType.LLM_RESULT
    )
    await sender._on_decorating_result_impl(event)
    assert state["result"].chain[0].text == "```\n&&happy&&\n```"
    assert original[0].text == "```\n"
    sender._send_meme_image.assert_not_awaited()


@pytest.mark.asyncio
async def test_compatibility_interface_parses_split_plain_markers(reply_context):
    sender, event, _, _ = reply_context
    sender._build_emotion_images_for_event = AsyncMock(return_value=([], []))
    original = [Plain("你好 &&ha"), Plain("ppy&&")]
    prepared = await sender.compat_prepare_message(event, MessageChain(original))
    assert prepared["emotions"] == ["happy"]
    assert prepared["cleaned_chain"].chain[0].text == "你好"
    assert original[0].text == "你好 &&ha"


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["missing", "samplejoy", "samplecomfort"])
async def test_invalid_marker_does_not_create_an_image_reply(reply_context, tag):
    sender, event, state, _ = reply_context
    response = LLMResponse(role="assistant", completion_text=f"&&{tag}&&")
    await sender._resp_impl(event, response)
    assert response.completion_text == ""
    assert not response.result_chain
    assert not state.get("meme_manager_image_only_reply")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "enabled", "repeated", "expected", "remaining"),
    [
        ("happy", False, False, [], "happy"),
        ("happy", True, False, ["happy"], ""),
        ("sad\nhappy", True, False, ["sad", "happy"], ""),
        ("I am happy today.", True, True, [], "I am happy today."),
        ("你好happy", True, True, [], "你好happy"),
        ("`happy`", True, True, [], "`happy`"),
        ("    happy", True, True, [], "happy"),
        ("```\nhappy\n```", True, True, [], "```\nhappy\n```"),
        ("https://example.com/happy", True, True, [], "https://example.com/happy"),
        (
            "<thinking>\nhappy\n</thinking>",
            True,
            True,
            [],
            "<thinking>\nhappy\n</thinking>",
        ),
        ("happyhappy", False, True, ["happy"], ""),
        ("happyhappy", False, False, [], "happyhappy"),
        ("xhappyhappyx", True, True, [], "xhappyhappyx"),
        ("`happyhappy`\nhappyhappy", False, True, ["happy"], "`happyhappy`"),
        ("samplejoy", True, True, [], "samplejoy"),
    ],
)
async def test_category_fallback_preserves_normal_and_protected_text(
    reply_context, text, enabled, repeated, expected, remaining
):
    sender, event, state, _ = reply_context
    sender.config["generation"]["matching"]["enable_loose_matching"] = enabled
    sender.config["generation"]["markup"]["enable_repeated_detection"] = repeated
    response = LLMResponse(role="assistant", completion_text=text)
    await sender._resp_impl(event, response)
    assert state["found_emotions"] == expected
    assert response.completion_text == remaining


@pytest.mark.asyncio
async def test_category_fallback_uses_request_pack_instead_of_global_mapping(
    reply_context,
):
    sender, event, state, _ = reply_context
    sender.config["generation"]["matching"]["enable_loose_matching"] = True
    sender._resolve_runtime_pack_context = lambda **kwargs: {
        "category_mapping": {"自定义": "custom category"}
    }
    response = LLMResponse(role="assistant", completion_text="happy\n自定义")
    await sender._resp_impl(event, response)
    assert state["found_emotions"] == ["自定义"]
    assert response.completion_text == "happy"


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_semantic_image_only_reply_keeps_multiple_selections(
    reply_context, monkeypatch, streaming
):
    sender, event, state, root = reply_context
    sender._semantic_mode_active = lambda event: True
    paths = {
        f"meme:{index:012x}": root / tag / "meme.png"
        for index, tag in enumerate(("happy", "sad", "angry"), 1)
    }
    monkeypatch.setattr(
        event_handlers,
        "validate_selected_id",
        lambda event, value, pack: paths.get(value),
    )
    text = "\n".join(f"&&{value}&&" for value in paths)
    response = LLMResponse(role="assistant", completion_text=text)
    await sender._resp_semantic_impl(event, response, text)
    assert response.completion_text == ""
    assert response.result_chain
    state["result"] = MessageEventResult(
        chain=response.result_chain.chain,
        result_content_type=ResultContentType.STREAMING_FINISH
        if streaming
        else ResultContentType.LLM_RESULT,
    )
    await sender._on_decorating_result_impl(event)
    if streaming:
        assert sender._send_meme_image.await_count == 3
    else:
        assert len(state["result"].chain) == 3
        assert all(isinstance(component, Image) for component in state["result"].chain)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text", ["这次用文字说明。", "这次用文字说明。\n&&meme:ffffffffffff&&"]
)
async def test_semantic_abstention_does_not_send_first_candidate(
    reply_context, monkeypatch, text
):
    sender, event, state, root = reply_context
    sender._semantic_mode_active = lambda event: True
    state["meme_manager_semantic_mode"] = "tool"
    state["meme_manager_semantic_default_id"] = "meme:000000000001"
    monkeypatch.setattr(
        event_handlers,
        "validate_selected_id",
        lambda event, value, pack: (
            root / "happy" / "meme.png" if value == "meme:000000000001" else None
        ),
    )
    response = LLMResponse(role="assistant", completion_text=text)
    await sender._resp_semantic_impl(event, response, text)
    assert state["meme_manager_semantic_selected_ids"] == []
    assert response.completion_text == "这次用文字说明。"
    state["result"] = MessageEventResult(
        chain=[Plain(response.completion_text)],
        result_content_type=ResultContentType.LLM_RESULT,
    )
    await sender._on_decorating_result_impl(event)
    assert len(state["result"].chain) == 1
    assert isinstance(state["result"].chain[0], Plain)
    assert not state.get("meme_manager_pending_images")


@pytest.mark.asyncio
async def test_semantic_search_exposes_candidates_without_preselecting(
    reply_context, monkeypatch
):
    import json

    sender, event, state, root = reply_context
    sender._semantic_mode_active = lambda event: True
    sender._resolve_runtime_pack_context = lambda **kwargs: {
        "pack_id": "pack",
        "pack_dir": root,
    }
    sender._resolve_embedding_provider = lambda pack: object()
    sender.semantic_top_k = 3
    sender.semantic_min_score = 0
    state["meme_manager_semantic_mode"] = "tool"
    state["meme_manager_semantic_verified_pack_id"] = "pack"
    candidate = {"id": "meme:000000000001", "caption": "安慰对方"}
    search = AsyncMock(return_value={"ok": True, "candidates": [candidate]})
    monkeypatch.setattr(plugin_main, "search_memes", search)
    monkeypatch.setattr(plugin_main, "candidate_records", lambda root, items: items)
    first = json.loads(await sender.search_memes_tool(event, "安慰对方"))
    assert first["candidates"] == [candidate]
    assert state["meme_manager_semantic_candidates"][candidate["id"]] == candidate
    assert not state.get("meme_manager_semantic_selected_ids")
    assert not state.get("meme_manager_semantic_default_id")
    second = json.loads(await sender.search_memes_tool(event, "换一个关键词"))
    assert second["ok"] is False
    search.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limit,expected", [(-10, 3), (-1, 3), (0, 0), (1, 1), (2, 2), (5, 3)]
)
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("semantic", [False, True])
@pytest.mark.parametrize("text", ["", "Hello"])
async def test_reply_meme_limit_preserves_text_and_selection_order(
    reply_context, monkeypatch, limit, expected, streaming, mixed, semantic, text
):
    sender, event, state, root = reply_context
    sender.max_memes_per_message = limit
    sender.emotions_probability = 100
    sender.enable_mixed_message = mixed
    tags = ["happy", "sad", "angry"]
    if semantic:
        sender._semantic_mode_active = lambda event: True
        paths = {
            f"meme:{index:012x}": root / tag / "meme.png"
            for index, tag in enumerate(tags, 1)
        }
        monkeypatch.setattr(
            event_handlers,
            "validate_selected_id",
            lambda event, value, pack: paths.get(value),
        )
        markers = " ".join(f"&&{value}&&" for value in paths)
    else:
        markers = " ".join(f"&&{tag}&&" for tag in tags)
    response = LLMResponse(role="assistant", completion_text=f"{text} {markers}")
    if semantic:
        await sender._resp_semantic_impl(event, response, response.completion_text)
    else:
        await sender._resp_impl(event, response)
    assert response.completion_text == text
    state["result"] = MessageEventResult(
        chain=[Plain(text)],
        result_content_type=ResultContentType.STREAMING_FINISH
        if streaming
        else ResultContentType.LLM_RESULT,
    )
    await sender._on_decorating_result_impl(event)
    await sender._after_message_sent_impl(event)
    images = [part for part in state["result"].chain if isinstance(part, Image)]
    images.extend(call.args[1] for call in sender._send_meme_image.await_args_list)
    assert len(images) == expected
    assert [
        Path(image.file.removeprefix("file:///")).parent.name for image in images
    ] == tags[:expected]
    assert (
        "".join(part.text for part in state["result"].chain if isinstance(part, Plain))
        == text
    )
    await sender._on_decorating_result_impl(event)
    await sender._after_message_sent_impl(event)
    assert sender._send_meme_image.await_count <= expected


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,expected", [(-2, 3), (0, 0), (1, 1)])
async def test_compat_preparation_respects_meme_limit(reply_context, limit, expected):
    sender, event, _, _ = reply_context
    sender.max_memes_per_message = limit
    sender.emotions_probability = 100
    prepared = await sender.compat_prepare_message(
        event, "Hello &&happy&& &&sad&& &&angry&&"
    )
    assert len(prepared["images"]) == expected
    assert prepared["cleaned_chain"].chain[0].text == "Hello"
