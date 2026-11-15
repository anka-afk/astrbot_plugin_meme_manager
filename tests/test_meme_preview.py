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
from mcp.types import CallToolResult, ImageContent
from PIL import Image
from test_meme_only_replies import reply_context as reply_context
from test_probability_gates import tool_reply as tool_reply

from astrbot.core.agent.message import ImageURLPart, Message, TextPart, ToolCall
from astrbot.core.provider.register import llm_tools


def tool_call(name):
    return ToolCall(id=name, function={"name": name, "arguments": "{}"})


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
    assert json.loads(await sender.send_meme_tool(event, "preview:2"))["status"] == "sent"
    emitted = sender._send_meme_image.await_args.args[1]
    assert base64.b64decode(emitted.file.removeprefix("base64://")) == original
    assert not run_context.messages
    await sender.compact_meme_tool_history(event, run_context, None)
    assert run_context.messages == [
        Message(role="assistant", content="已发送 1 张表情（happy，#2）。")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("keep_summary", "sends", "summary"),
    [(True, 0, ""), (False, 1, ""), (True, 2, "已发送 2 张表情。")],
)
async def test_tool_history_preserves_only_success_summary(
    preview_request, keep_summary, sends, summary
):
    sender, event, request, provider, _ = preview_request
    sender.keep_meme_send_summary = keep_summary
    sender.preview_images_per_page = 0
    await sender._inject_meme_prompt_impl(event, request)
    run_context = SimpleNamespace(messages=[])
    await sender.track_meme_tool_context(event, run_context)
    for _ in range(sends):
        assert json.loads(await sender.send_meme_tool(event, "happy"))["status"] == "sent"
    tool_name = "send_meme" if sends else PREVIEW_TOOL
    run_context.messages.extend(
        [
            Message(
                role="assistant",
                content=[TextPart(text="Temporary image selection notes")],
                tool_calls=[tool_call(tool_name)],
            ),
            Message(role="tool", tool_call_id=tool_name, content="Temporary tool output"),
            Message(role="assistant", content=[TextPart(text="Done")]),
        ]
    )
    await sender.compact_meme_tool_history(event, run_context, None)
    assert len(run_context.messages) == 1
    expected_content = [TextPart(text="Done")]
    if summary:
        expected_content.append(TextPart(text=f"\n\n{summary}"))
    assert run_context.messages[0].content == expected_content
    provider.text_chat.assert_not_awaited()
