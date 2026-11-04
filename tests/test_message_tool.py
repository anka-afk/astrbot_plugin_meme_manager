"""验证真实内置工具的校验及本次调用独立的发送适配器。"""

import asyncio
import copy
import json
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_meme_manager.backend.message_tool import MemeMessageTool
from astrbot_plugin_meme_manager.backend.packs import storage
from test_meme_only_replies import reply_context as reply_context
from test_multi_pack import runtime as runtime
from test_multi_pack import semantic_runtime as semantic_runtime
from test_probability_gates import tool_reply as tool_reply

from astrbot.api.provider import LLMResponse
from astrbot.core.message.components import At, Image, Plain
from astrbot.core.tools.message_tools import SendMessageToUserTool

pytestmark = pytest.mark.asyncio
ORIGIN = "test:GroupMessage:123"
REMOTE = "test:FriendMessage:456"


def wire(sender, event, state):
    """附加接近真实的内置上下文，不运行平台或模型。

    Args:
        sender: 测试插件实例。
        event: 测试事件。
        state: 测试事件扩展属性。

    Returns:
        包装后的工具、Agent 上下文及适配器捕获的发送记录。
    """
    event.unified_msg_origin = ORIGIN
    event.session_id = "123"
    event.role = "admin"
    event.get_sender_id = lambda: "admin"
    event._has_send_oper = False
    event.get_extra = lambda key, default=None: state.get(key, default)
    sender.emotions_probability = 100
    captured = []

    async def send(session, chain):
        captured.append((str(session), copy.deepcopy(chain)))
        return True

    sender.context = SimpleNamespace(
        send_message=AsyncMock(side_effect=send),
        get_config=lambda *args, **kwargs: {
            "provider_settings": {"computer_use_runtime": "local"}
        },
        conversation_manager=SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value="conv"),
            get_conversation=AsyncMock(
                return_value=SimpleNamespace(persona_id="remote-persona")
            ),
        ),
        persona_manager=SimpleNamespace(
            resolve_selected_persona=AsyncMock(
                return_value=("remote-persona", None, None, False)
            ),
        ),
        platform_manager=SimpleNamespace(
            platform_insts=[
                SimpleNamespace(meta=lambda: SimpleNamespace(id="test", name="test"))
            ]
        ),
    )
    context = SimpleNamespace(
        context=SimpleNamespace(context=sender.context, event=event)
    )
    return MemeMessageTool(SendMessageToUserTool(), sender), context, captured


@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("filter_all", [False, True])
async def test_builtin_category_send_preserves_media_and_deduplicates_final(
    reply_context, mixed, filter_all
):
    sender, event, state, _ = reply_context
    tool, context, sent = wire(sender, event, state)
    sender.enable_mixed_message = mixed
    sender.filter_all_tags = filter_all
    result = await tool.call(
        context,
        messages=[
            {"type": "plain", "text": "Hello &&happy&& :unknown: [unknown]"},
            {"type": "mention_user", "mention_user_id": "42"},
            {"type": "image", "url": "https://example.com/original.png"},
        ],
    )
    assert result.startswith("Message sent")
    assert len(sent) == (1 if mixed else 2)
    parts = [part for _, chain in sent for part in chain.chain]
    expected = "Hello" if filter_all else "Hello  :unknown: [unknown]"
    assert [part.text for part in parts if isinstance(part, Plain)] == [expected]
    assert sum(isinstance(part, At) for part in parts) == 1
    assert sum(isinstance(part, Image) for part in parts) == 2
    assert state["meme_manager_automatic_sent_count"] == 1
    assert expected in state["_send_message_to_user_current_session_plain_texts"]
    assert event._has_send_oper
    assert context.context.context is sender.context
    assert not state.get("meme_manager_semantic_response_processed")
    response = LLMResponse(role="assistant", completion_text="Hello &&happy&&")
    sender.emotion_llm_enabled = True
    sender._emotion_llm_generate = AsyncMock(
        side_effect=AssertionError("Already delivered")
    )
    await sender._resp_impl(event, response)
    assert response.completion_text == "Hello"
    assert not state.get("found_emotions")
    sender._emotion_llm_generate.assert_not_awaited()
    assert sender._delivery_history.recent(ORIGIN)[0]["category"] == "happy"


@pytest.mark.parametrize("probability", [0, 100])
async def test_marker_only_never_sends_blank_or_claims_skipped_delivery(
    reply_context, probability
):
    sender, event, state, _ = reply_context
    tool, context, sent = wire(sender, event, state)
    sender.emotions_probability = probability
    result = await tool.call(context, messages=[{"type": "plain", "text": "&&happy&&"}])
    assert len(sent) == int(probability > 0)
    assert event._has_send_oper is (probability > 0)
    assert ("Message not sent" in result) is (probability == 0)
    if sent:
        assert all(isinstance(c, Image) for c in sent[0][1].chain)


@pytest.mark.parametrize(
    "case", ["denied", "invalid_media", "invalid_session", "empty"]
)
async def test_builtin_validation_finishes_before_meme_selection(reply_context, case):
    sender, event, _, _ = reply_context
    tool, context, sent = wire(sender, event, {})
    sender._prepare_outgoing_message = AsyncMock(
        side_effect=AssertionError("Validation first")
    )
    messages = [{"type": "plain", "text": "Hello &&happy&&"}]
    session = ""
    if case == "denied":
        event.role = "member"
        session = REMOTE
    elif case == "invalid_media":
        messages.append({"type": "image"})
    elif case == "invalid_session":
        session = "bad:session"
    else:
        messages = []
    result = await tool.call(context, messages=messages, session=session)
    assert result.startswith("error:")
    assert not sent
    sender._prepare_outgoing_message.assert_not_awaited()


async def test_literals_are_preserved_and_adjacent_plain_markers_are_joined(
    reply_context,
):
    sender, event, state, _ = reply_context
    tool, context, sent = wire(sender, event, state)
    await tool.call(
        context,
        messages=[
            {"type": "plain", "text": "`&&sad&&`\n```\n&&angry&&\n```\nHello &&ha"},
            {"type": "plain", "text": "ppy&&"},
        ],
    )
    assert sent[0][1].get_plain_text() == "`&&sad&&`\n```\n&&angry&&\n```\nHello"
    assert state["meme_manager_automatic_sent_count"] == 1


async def test_multiple_calls_share_probability_budget_and_do_not_repeat_choices(
    reply_context,
):
    sender, event, state, _ = reply_context
    tool, context, sent = wire(sender, event, state)
    sender.max_memes_per_message = 2
    for tag in ("happy", "happy", "sad", "angry"):
        await tool.call(
            context, messages=[{"type": "plain", "text": f"Hello &&{tag}&&"}]
        )
    assert len(sent) == 6  # 四次文本发送，两次图片发送。
    assert state["meme_manager_automatic_sent_count"] == 2
    assert state["meme_manager_send_probability_hit"] is True


async def test_concurrent_calls_cannot_exceed_turn_budget(reply_context):
    sender, event, state, _ = reply_context
    tool, context, sent = wire(sender, event, state)
    sender.max_memes_per_message = 1
    await asyncio.gather(
        *(
            tool.call(context, messages=[{"type": "plain", "text": f"Hello &&{tag}&&"}])
            for tag in ("happy", "sad")
        )
    )
    assert sum(isinstance(c, Image) for _, chain in sent for c in chain.chain) == 1
    assert state["meme_manager_automatic_sent_count"] == 1


@pytest.mark.parametrize("failure", [False, RuntimeError("adapter failed")])
async def test_partial_failure_preserves_delivered_text_and_cleans_files(
    reply_context, failure
):
    sender, event, state, root = reply_context
    tool, context, _ = wire(sender, event, state)
    converted = root / "temporary.gif"

    def convert(path):
        converted.write_bytes(b"converted")
        return str(converted)

    sender._convert_to_gif = convert
    sender.context.send_message.side_effect = [True, failure]
    result = await tool.call(
        context, messages=[{"type": "plain", "text": "Hello &&happy&&"}]
    )
    assert "partially sent" in result
    assert not converted.exists()
    assert event._has_send_oper
    assert state["_send_message_to_user_current_session_plain_texts"] == ["Hello"]
    assert not state.get("meme_manager_automatic_sent_count")
    assert not sender._delivery_history.recent(ORIGIN)


async def test_cancelled_conversion_waits_for_thread_and_removes_output(reply_context):
    sender, event, state, root = reply_context
    tool, context, sent = wire(sender, event, state)
    started, release = Event(), Event()
    converted = root / "temporary.gif"

    def convert(path):
        started.set()
        release.wait(5)
        converted.write_bytes(b"converted")
        return str(converted)

    sender._convert_to_gif = convert
    task = asyncio.create_task(
        tool.call(context, messages=[{"type": "plain", "text": "Hello &&happy&&"}])
    )
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not sent
    assert not converted.exists()


async def test_remote_session_uses_destination_persona_and_pack(reply_context, runtime):
    sender, event, state, _ = reply_context
    tool, context, sent = wire(sender, event, state)
    for pack_id in ("pack-a", "pack-b"):
        folder = runtime.packs / pack_id / "memes" / "happy"
        folder.mkdir(parents=True)
        (folder / "meme.png").write_bytes(b"image")
    storage.save_selection_rules(
        [
            {
                "id": "local",
                "scope": "session",
                "target": "123",
                "pack_ids": ["pack-a"],
            },
            {
                "id": "remote",
                "scope": "persona",
                "target": "remote-persona",
                "pack_ids": ["pack-b"],
            },
            {"id": "default", "scope": "default", "pack_ids": ["pack-a"]},
        ]
    )
    del sender._resolve_runtime_pack_context
    del sender._get_runtime_memes_dir_for_event
    await tool.call(
        context, session="456", messages=[{"type": "plain", "text": "Hi &&happy&&"}]
    )
    assert all(target == "test:GroupMessage:456" for target, _ in sent)
    image = sent[1][1].chain[0]
    assert "pack-b" in image.file
    assert not state.get("meme_manager_resolved_persona_id")
    assert not event._has_send_oper
    assert not state.get("_send_message_to_user_current_session_plain_texts")
    assert sender._delivery_history.recent("test:GroupMessage:456")
    assert not sender._delivery_history.recent(ORIGIN)


async def test_request_installation_keeps_registry_and_other_tools(tool_reply):
    sender, event, state, request, shared, _, _ = tool_reply
    original = SendMessageToUserTool()
    shared.add_tool(original)
    await sender._inject_meme_prompt_impl(event, request)
    wrapped = request.func_tool.get_tool("send_message_to_user")
    assert isinstance(wrapped, MemeMessageTool)
    assert wrapped.original is original
    assert shared.get_tool("send_message_to_user") is original
    assert request.func_tool.get_tool("other_tool") is shared.get_tool("other_tool")
    await sender._inject_meme_prompt_impl(event, request)
    assert request.func_tool.get_tool("send_message_to_user") is wrapped


async def test_code_context_survives_media_components(reply_context):
    sender, event, state, _ = reply_context
    tool, context, sent = wire(sender, event, state)
    await tool.call(
        context,
        messages=[
            {"type": "plain", "text": "```"},
            {"type": "mention_user", "mention_user_id": "42"},
            {"type": "plain", "text": "&&happy&&\n```"},
        ],
    )
    assert len(sent) == 1
    assert sent[0][1].chain[0].text == "```"
    assert isinstance(sent[0][1].chain[1], At)
    assert sent[0][1].chain[2].text == "&&happy&&\n```"
    assert not state.get("meme_manager_automatic_sent_count")


@pytest.mark.parametrize("allowed", [False, True])
async def test_cross_session_semantic_id_must_belong_to_destination(
    semantic_runtime, allowed
):
    sender, event, state, packs = semantic_runtime
    candidate = json.loads(await sender.search_memes_tool(event, "happy"))[
        "candidates"
    ][0]
    tool, context, sent = wire(sender, event, state)
    sender.emotion_llm_enabled = False
    sender._semantic_pack_ready = lambda **kwargs: True
    target_pack = candidate["_source_pack_id"] if allowed else "pack-a"
    storage.save_selection_rules(
        [
            {
                "id": "remote",
                "scope": "session",
                "target": "456",
                "pack_ids": [target_pack],
            },
            {"id": "default", "scope": "default", "pack_ids": ["pack-a", "pack-b"]},
        ]
    )
    before = list(state["meme_manager_runtime_pack_ids"])
    await tool.call(
        context,
        session=REMOTE,
        messages=[{"type": "plain", "text": f"Hi &&{candidate['id']}&&"}],
    )
    images = [c for _, chain in sent for c in chain.chain if isinstance(c, Image)]
    assert len(images) == int(allowed)
    assert state["meme_manager_runtime_pack_ids"] == before
    assert not event._has_send_oper


async def test_revalidation_after_conversion_rejects_changed_candidate(
    semantic_runtime,
):
    sender, event, state, packs = semantic_runtime
    candidate = json.loads(await sender.search_memes_tool(event, "happy"))[
        "candidates"
    ][0]
    source = packs["pack_dirs"][candidate["_source_pack_id"]] / "memes/happy/meme.png"
    temporary = source.parent / "temporary.gif"

    def convert(path):
        temporary.write_bytes(b"converted")
        source.write_bytes(b"changed")
        return str(temporary)

    tool, context, sent = wire(sender, event, state)
    sender._convert_to_gif = convert
    await tool.call(
        context, messages=[{"type": "plain", "text": f"Hi &&{candidate['id']}&&"}]
    )
    assert len(sent) == 1
    assert sent[0][1].get_plain_text() == "Hi"
    assert not temporary.exists()


@pytest.mark.parametrize("probability", [0, 100])
async def test_marker_tool_attempt_blocks_direct_tool_even_if_probability_misses(
    reply_context, probability
):
    sender, event, state, _ = reply_context
    tool, context, _ = wire(sender, event, state)
    sender.emotions_probability = probability
    sender.send_meme_tool_enabled = True
    state["meme_manager_send_tool_available"] = True
    await tool.call(context, messages=[{"type": "plain", "text": "Hello &&happy&&"}])
    result = json.loads(await sender._send_meme_tool_impl(event, "happy"))
    assert result == {"ok": False, "reason": "tag_delivery_selected"}


async def test_real_agent_context_and_executor_keep_original_identity(reply_context):
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.astr_agent_context import AstrAgentContext
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.star.context import Context

    sender, _, state, _ = reply_context
    msg = AstrBotMessage()
    msg.message = [Plain("hello")]
    msg.message_str = "hello"
    msg.message_id = "trigger"
    msg.sender = MessageMember(user_id="admin", nickname="Admin")
    msg.type = MessageType.GROUP_MESSAGE
    msg.self_id = "bot"
    msg.group_id = "123"
    event = AstrMessageEvent(
        "hello", msg, PlatformMetadata("test", "test", "test"), "123"
    )
    for key, value in state.items():
        event.set_extra(key, value)
    # 连接平台依赖时保留真实事件的扩展属性存储。
    real_get, real_set = event.get_extra, event.set_extra
    tool, _, sent = wire(sender, event, state)
    event.get_extra, event.set_extra = real_get, real_set
    real_context = object.__new__(Context)
    real_context.__dict__.update(vars(sender.context))
    sender.context = real_context
    context = ContextWrapper(
        context=AstrAgentContext(context=real_context, event=event)
    )
    results = [
        r
        async for r in FunctionToolExecutor.execute(
            tool, context, messages=[{"type": "plain", "text": "Hello &&happy&&"}]
        )
    ]
    assert "Message sent" in results[0].content[0].text
    assert len(sent) == 2
    assert context.context.context is real_context
    assert context.context.event is event


async def test_local_file_permissions_are_not_relaxed_by_marker_support(reply_context):
    sender, event, state, root = reply_context
    tool, context, sent = wire(sender, event, state)
    event.role = "member"
    result = await tool.call(
        context,
        messages=[
            {"type": "plain", "text": "Hello &&happy&&"},
            {"type": "image", "path": str(root / "happy/meme.png")},
        ],
    )
    assert "restricted" in result.lower()
    assert not sent


async def test_identical_base64_images_only_confirm_the_component_actually_sent(
    reply_context,
):
    sender, event, state, _ = reply_context
    tool, context, _ = wire(sender, event, state)
    sender.send_image_as_base64 = True
    # 测试分类文件的字节相同，因此生成的 Image 值也相同。
    sender.context.send_message.side_effect = [True, True, False]
    result = await tool.call(
        context, messages=[{"type": "plain", "text": "Hi &&happy&& &&sad&&"}]
    )
    assert "partially sent" in result
    assert state["meme_manager_automatic_sent_count"] == 1
    assert len(state["meme_manager_tool_delivered_choices"]) == 1
    assert len(sender._delivery_history.recent(ORIGIN)) == 1


async def test_adapter_component_replacement_preserves_delivery_accounting(
    reply_context,
):
    sender, event, state, _ = reply_context
    tool, context, _ = wire(sender, event, state)

    async def send(session, chain):
        chain.chain = [Plain("adapter transformed the wire representation")]
        return True

    sender.context.send_message.side_effect = send
    await tool.call(context, messages=[{"type": "plain", "text": "&&happy&&"}])
    assert state["meme_manager_automatic_sent_count"] == 1
    assert len(sender._delivery_history.recent(ORIGIN)) == 1


async def test_public_compatibility_send_reuses_selection_and_cleanup(reply_context):
    sender, event, state, root = reply_context
    wire(sender, event, state)
    temporary = root / "compat.gif"

    def convert(path):
        temporary.write_bytes(b"converted")
        return str(temporary)

    sender._convert_to_gif = convert
    event.send = AsyncMock()
    result = await sender.compat_send_message(event, "Hello &&happy&&")
    assert result["sent_images_count"] == 1
    assert event.send.await_args.args[0].get_plain_text() == "Hello"
    assert not temporary.exists()
    assert sender._delivery_history.recent(ORIGIN)[0]["category"] == "happy"
