"""Verify image numbering, original delivery, and preview context cleanup."""

import base64
import copy
import io
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from astrbot_plugin_meme_manager import main as plugin_main
from astrbot_plugin_meme_manager.backend.packs.preview import (
    PREVIEW_TOOL,
    build_meme_preview,
    discard_meme_tool_context,
)
from astrbot_plugin_meme_manager.mixins.event_handlers import (
    LLM_REQUEST_ORIGIN_EXTRA_KEY,
)
from mcp.types import CallToolResult, ImageContent, TextContent
from PIL import Image
from test_meme_only_replies import reply_context as reply_context
from test_probability_gates import tool_reply as tool_reply

from astrbot.core.agent import message as agent_message
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.message import (
    ImageURLPart,
    Message,
    TextPart,
    ToolCall,
)
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot.core.provider.register import llm_tools

if hasattr(agent_message, "dump_messages"):
    dump_history = agent_message.dump_messages
    load_history = agent_message.load_messages
else:
    dump_history = agent_message.dump_messages_with_checkpoints
    load_history = agent_message.bind_checkpoint_messages


def tool_call(name, **arguments):
    return ToolCall(
        id=name,
        function={"name": name, "arguments": json.dumps(arguments)},
    )


def test_numbering_survives_renames_replacements_and_simultaneous_previews(tmp_path):
    root = tmp_path / "pack"
    category = root / "memes" / "happy"
    category.mkdir(parents=True)
    first = category / "a.png"
    second = category / "b.png"
    Image.new("RGB", (32, 16), "red").save(first)
    Image.new("RGB", (32, 16), "blue").save(second)
    database = tmp_path / "preview_ids.sqlite3"
    packs = {"pack": root}

    with ThreadPoolExecutor(max_workers=2) as workers:
        jobs = [
            workers.submit(build_meme_preview, database, [second, first], packs, 0, 1)
            for _ in range(2)
        ]
        for job in jobs:
            jpeg, images, _ = job.result()
            assert {number: record[0] for number, record in images.items()} == {1: first}
            with Image.open(io.BytesIO(jpeg)) as sheet:
                assert sheet.size == (192, 208)

    renamed = first.rename(category / "z.png")
    second.unlink()
    Image.new("RGB", (32, 16), "green").save(second)
    _, images, next_after = build_meme_preview(database, [renamed, second], packs, 0, 9)
    assert next_after is None
    assert {number: record[0] for number, record in images.items()} == {
        1: renamed,
        3: second,
    }


def test_cleanup_preserves_other_tool_pairs_and_user_images():
    preview, search, send, other = map(
        tool_call, (PREVIEW_TOOL, "search_memes", "send_meme", "weather")
    )
    other_result = Message(role="tool", tool_call_id=other.id, content="Sunny")
    own_image = ImageURLPart(image_url={"url": "own.jpg", "id": "own.jpg"})
    preview_image = ImageURLPart(image_url={"url": "grid.jpg", "id": "grid.jpg"})
    messages = [
        Message(role="assistant", tool_calls=[preview, search, send, other]),
        Message(role="tool", tool_call_id=preview.id, content="preview result"),
        Message(role="tool", tool_call_id=search.id, content="semantic candidates"),
        Message(role="tool", tool_call_id=send.id, content="sent"),
        other_result,
        Message(
            role="user",
            content=[
                TextPart(text="[Image from tool 'preview_memes', path='grid.jpg']"),
                preview_image,
                TextPart(text="Unrelated image"),
                own_image,
            ],
        ),
    ]
    assert discard_meme_tool_context(messages, preview_only=True)
    assert messages[0].tool_calls == [search, send, other]
    assert [item.tool_call_id for item in messages[1:4]] == [search.id, send.id, other.id]
    assert discard_meme_tool_context(messages)
    assert messages[0].tool_calls == [other]
    assert messages[1] == other_result
    assert messages[2].content == [TextPart(text="Unrelated image"), own_image]


@pytest.fixture
def preview_request(tool_reply, monkeypatch, tmp_path):
    sender, event, _, request, tools, provider, _ = tool_reply
    sender.semantic_enabled = False
    sender._semantic_pack_ready = lambda **kwargs: False
    sender.preview_images_per_page = 1
    sender.keep_meme_send_summary = True
    provider.provider_config["modalities"] = ["tool_use", "image"]
    context = sender._resolve_runtime_pack_context()
    context["memes_dir"] = context["pack_dir"] / "memes"
    category = context["memes_dir"] / "happy"
    category.mkdir(parents=True)
    Image.new("RGB", (40, 30), "red").save(category / "a.png")
    Image.new("RGB", (40, 30), "blue").save(category / "b.png")
    sender._resolve_runtime_pack_context = lambda **kwargs: context
    sender.prompt_tail = ""
    sender.prompt_examples = []
    monkeypatch.setattr(plugin_main, "PLUGIN_DATA_DIR", tmp_path / "preview_data")
    for name in ("send_meme", PREVIEW_TOOL):
        tools.add_tool(copy.copy(llm_tools.get_func(name)))
    return sender, event, request, provider, category


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["text_only", "disabled", "not_granted"])
async def test_preview_requires_vision_setting_and_permission(preview_request, restriction):
    sender, event, request, provider, _ = preview_request
    if restriction == "text_only":
        provider.provider_config["modalities"] = ["tool_use"]
    elif restriction == "disabled":
        sender.preview_images_per_page = 0
    else:
        request.func_tool.remove_tool(PREVIEW_TOOL)
    await sender._inject_meme_prompt_impl(event, request)
    assert request.func_tool.get_tool(PREVIEW_TOOL) is None
    assert PREVIEW_TOOL not in request.system_prompt
    assert request.func_tool.get_tool("send_meme") is not None
    await sender.track_meme_tool_context(event, SimpleNamespace(messages=[]))
    assert json.loads(await sender.preview_memes_tool(event, "happy"))["reason"] == (
        "preview_unavailable"
    )


@pytest.mark.asyncio
async def test_numbered_preview_sends_the_current_original(preview_request):
    sender, event, request, _, category = preview_request
    await sender._inject_meme_prompt_impl(event, request)
    assert request.func_tool.get_tool(PREVIEW_TOOL) is not None
    assert PREVIEW_TOOL in request.system_prompt
    run_context = SimpleNamespace(messages=[])
    await sender.track_meme_tool_context(event, run_context)
    result = await sender.preview_memes_tool(event, "happy")
    assert isinstance(result, CallToolResult)
    assert json.loads(result.content[0].text.split("。", 1)[0])["next_after"] == 1
    assert isinstance(result.content[1], ImageContent)
    assert json.loads(await sender.preview_memes_tool(event, "happy", after=1))[
        "reason"
    ] == "preview_pending"
    preview_message = Message(role="assistant", tool_calls=[tool_call(PREVIEW_TOOL)])
    run_context.messages.append(preview_message)
    assert isinstance(
        await sender.preview_memes_tool(event, "happy", after=1), CallToolResult
    )
    assert not run_context.messages
    run_context.messages.append(preview_message)
    assert json.loads(await sender.preview_memes_tool(event, "happy", after=2))[
        "reason"
    ] == "preview_limit"
    assert json.loads(await sender.send_meme_tool(event, "preview:1"))["reason"] == (
        "invalid_preview_candidate"
    )
    path = category / "b.png"
    original = path.read_bytes()
    path.write_bytes((category / "a.png").read_bytes())
    assert json.loads(await sender.send_meme_tool(event, "preview:2"))["reason"] == (
        "candidate_changed"
    )
    path.write_bytes(original)
    receipt = await sender.send_meme_tool(event, "preview:2")
    assert json.loads(receipt) == {
        "ok": True,
        "status": "sent",
        "sent_count": 1,
        "category": "happy",
        "preview_number": 2,
    }
    emitted = sender._send_meme_image.await_args.args[1]
    assert base64.b64decode(emitted.file.removeprefix("base64://")) == original
    assert not run_context.messages
    send = tool_call("send_meme", target="preview:2")
    pair = [
        Message(role="assistant", tool_calls=[send]),
        Message(role="tool", tool_call_id=send.id, content=receipt),
    ]
    run_context.messages.extend(pair)
    await sender.compact_meme_tool_history(event, run_context, None)
    assert run_context.messages == pair


@pytest.mark.asyncio
@pytest.mark.parametrize("keep_summary", [True, False])
async def test_tool_history_round_trip_preserves_only_successful_sends(
    preview_request, keep_summary
):
    sender, event, request, provider, category = preview_request
    sender.keep_meme_send_summary = keep_summary
    sender.preview_images_per_page = 0
    sad = category.with_name("sad")
    sad.mkdir()
    (sad / "a.png").write_bytes((category / "a.png").read_bytes())
    await sender._inject_meme_prompt_impl(event, request)
    run_context = SimpleNamespace(messages=[])
    await sender.track_meme_tool_context(event, run_context)
    first = tool_call("send_meme", target="happy")
    failed = tool_call("send_meme", target="missing")
    second = tool_call("send_meme", target="sad")
    other = tool_call("weather")
    first_result = Message(
        role="tool",
        tool_call_id=first.id,
        content=await sender.send_meme_tool(event, "happy"),
    )
    failed_result = Message(
        role="tool",
        tool_call_id=failed.id,
        content=await sender.send_meme_tool(event, "missing"),
    )
    second_result = Message(
        role="tool",
        tool_call_id=second.id,
        content=await sender.send_meme_tool(event, "sad"),
    )
    other_result = Message(role="tool", tool_call_id=other.id, content="Sunny")
    reply = Message(role="assistant", content=[TextPart(text="Done")])
    follow_up = Message(role="user", content="再发一张难过的")
    run_context.messages.extend(
        [
            Message(role="assistant", tool_calls=[first, failed, other]),
            first_result,
            failed_result,
            other_result,
            reply,
            follow_up,
            Message(role="assistant", tool_calls=[second]),
            second_result.model_copy(
                update={"content": f"{second_result.content}\n\n[Current turn budget]"}
            ),
        ]
    )
    assert json.loads(first_result.content)["category"] == "happy"
    assert json.loads(failed_result.content)["ok"] is False
    assert json.loads(second_result.content)["category"] == "sad"

    await sender.compact_meme_tool_history(event, run_context, None)
    saved = json.loads(json.dumps(dump_history(run_context.messages)))
    reloaded = load_history(saved)
    kept_calls = [first, other] if keep_summary else [other]
    kept_results = [first_result] if keep_summary else []
    follow_up_results = (
        [Message(role="assistant", tool_calls=[second]), second_result]
        if keep_summary
        else []
    )
    assert reloaded == [
        Message(role="assistant", tool_calls=kept_calls),
        *kept_results,
        other_result,
        reply,
        follow_up,
        *follow_up_results,
    ]
    assert sender._send_meme_image.await_count == 2
    provider.text_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_replays_send_receipt_and_sends_again(preview_request):
    sender, event, template, provider, _ = preview_request
    sender.preview_images_per_page = 0
    provider.get_model = lambda: "test-model"
    turns = [
        ("发一张开心的", 1, []),
        ("刚才发了什么类型？", 1, ["happy"]),
        ("再发一张", 2, ["happy"]),
    ]
    saved_history = []

    async def execute(*, tool, run_context, target):
        receipt = await sender.send_meme_tool(event, target)
        yield CallToolResult(content=[TextContent(type="text", text=receipt)])

    hooks = BaseAgentRunHooks()
    hooks.on_agent_begin = lambda context: sender.track_meme_tool_context(event, context)
    hooks.on_agent_done = lambda context, response: sender.compact_meme_tool_history(
        event, context, response
    )

    for prompt, delivered, expected_categories in turns:
        extras = {LLM_REQUEST_ORIGIN_EXTRA_KEY: "chat"}
        event.get_extra = extras.get
        event.set_extra = extras.__setitem__
        request = ProviderRequest(
            prompt=prompt,
            system_prompt=template.system_prompt,
            func_tool=template.func_tool,
            contexts=saved_history,
        )
        extras["provider_request"] = request
        await sender._inject_meme_prompt_impl(event, request)

        runner = ToolLoopAgentRunner()
        context = ContextWrapper(context=None)
        if delivered != sender._send_meme_image.await_count:
            provider.text_chat.side_effect = [
                LLMResponse(
                    role="tool",
                    tools_call_name=["send_meme"],
                    tools_call_args=[{"target": "happy"}],
                    tools_call_ids=[f"call_{delivered}"],
                ),
                LLMResponse(role="assistant", completion_text="给你一张开心的。"),
            ]
        else:
            provider.text_chat.side_effect = [
                LLMResponse(role="assistant", completion_text="开心。")
            ]
        provider.text_chat.reset_mock()
        await runner.reset(
            provider=provider,
            request=request,
            run_context=context,
            tool_executor=SimpleNamespace(execute=execute),
            agent_hooks=hooks,
        )
        async for _ in runner.step_until_done(4):
            pass
        assert runner.done()
        assert sender._send_meme_image.await_count == delivered
        first_request = [
            Message.model_validate(message)
            for message in provider.text_chat.await_args_list[0].kwargs["contexts"]
        ]
        historical_receipts = [
            json.loads(message.content)
            for message in first_request
            if message.role == "tool"
        ]
        assert [item["category"] for item in historical_receipts] == expected_categories
        saved_history = dump_history(
            [message for message in context.messages if message.role != "system"]
        )
        assert all(
            "已发送" not in str(message.get("content"))
            for message in saved_history
            if message["role"] == "assistant"
        )


def test_discard_meme_tool_context_preserves_assistant_visible_text():
    call = tool_call("send_meme", target="happy")
    messages = [
        Message(
            role="assistant",
            content=[TextPart(text="给你发一张图！")],
            tool_calls=[call],
        ),
        Message(role="tool", tool_call_id=call.id, content='{"ok": false}'),
    ]
    assert discard_meme_tool_context(messages) is True
    assert len(messages) == 1
    assert messages[0].role == "assistant"
    assert messages[0].tool_calls is None
    assert messages[0].content == [TextPart(text="给你发一张图！")]


@pytest.mark.asyncio
async def test_preview_accepts_integral_float_cursor_and_converts_static_to_gif(
    preview_request,
):
    sender, event, request, _, _ = preview_request
    sender.convert_static_to_gif = True
    await sender._inject_meme_prompt_impl(event, request)
    await sender.track_meme_tool_context(event, SimpleNamespace(messages=[]))

    for bad_after in (True, False, 1.5, -1, -1.0, float("nan"), float("inf"), "0"):
        assert (
            json.loads(await sender.preview_memes_tool(event, "happy", after=bad_after))[
                "reason"
            ]
            == "invalid_preview_arguments"
        )

    assert isinstance(
        await sender.preview_memes_tool(event, "happy", after=0.0), CallToolResult
    )
    receipt = json.loads(await sender.send_meme_tool(event, "preview:1"))
    assert receipt["ok"] is True
    emitted = sender._send_meme_image.await_args.args[1]
    raw = base64.b64decode(emitted.file.removeprefix("base64://"))
    assert raw[:6] in (b"GIF87a", b"GIF89a")


def test_build_meme_preview_handles_non_positive_limit_and_relative_paths(
    tmp_path, monkeypatch
):
    from pathlib import Path

    pack_dir = tmp_path / "pack"
    image_path = pack_dir / "memes" / "happy" / "a.png"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (32, 32), (10, 20, 30)).save(image_path, "PNG")
    monkeypatch.chdir(tmp_path)
    rel_file = Path("pack/memes/happy/a.png")
    assert build_meme_preview(
        tmp_path / "preview.sqlite3", [rel_file], {"pack": pack_dir}, 0, limit=0
    ) == (None, {}, None)
    jpeg, candidates, next_after = build_meme_preview(
        tmp_path / "preview.sqlite3", [rel_file], {"pack": pack_dir}, 0, limit=1
    )
    assert jpeg is not None
    assert 1 in candidates
    assert next_after is None
