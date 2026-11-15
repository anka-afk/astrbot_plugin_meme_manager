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
            jpeg, images, next_after = job.result()
            assert next_after == 1
            assert list(images) == [1]
            assert images[1][0] == first
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
    preview = ToolCall(
        type="function", id="preview", function={"name": PREVIEW_TOOL, "arguments": "{}"}
    )
    other = ToolCall(
        type="function", id="other", function={"name": "weather", "arguments": "{}"}
    )
    search = ToolCall(id="search", function={"name": "search_memes", "arguments": "{}"})
    send = ToolCall(id="send", function={"name": "send_meme", "arguments": "{}"})
    other_result = Message(role="tool", tool_call_id="other", content="Sunny")
    own_image = ImageURLPart(image_url={"url": "own.jpg", "id": "own.jpg"})
    preview_image = ImageURLPart(image_url={"url": "grid.jpg", "id": "grid.jpg"})
    messages = [
        Message(role="assistant", tool_calls=[preview, search, send, other]),
        Message(role="tool", tool_call_id="preview", content="preview result"),
        Message(role="tool", tool_call_id="search", content="semantic candidates"),
        Message(role="tool", tool_call_id="send", content="sent"),
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
    assert [item.tool_call_id for item in messages[1:4]] == ["search", "send", "other"]
    assert discard_meme_tool_context(messages)
    assert messages[0].tool_calls == [other]
    assert messages[1] == other_result
    assert messages[2].content == [TextPart(text="Unrelated image"), own_image]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "feature",
    ["enabled", "skip", "text_only", "disabled", "not_granted", "summary_off", "multiple"],
)
async def test_preview_exposure_and_numbered_original_delivery(
    tool_reply, monkeypatch, tmp_path, feature
):
    sender, event, state, request, shared, provider, _ = tool_reply
    sender.semantic_enabled = False
    sender._semantic_pack_ready = lambda **kwargs: False
    sender.preview_images_per_page = 0 if feature == "disabled" else 1
    sender.keep_meme_send_summary = feature != "summary_off"
    sender._send_meme_image.reset_mock()
    provider.provider_config["modalities"] = (
        ["tool_use"] if feature == "text_only" else ["tool_use", "image"]
    )
    context = sender._resolve_runtime_pack_context()
    context["memes_dir"] = context["pack_dir"] / "memes"
    category = context["memes_dir"] / "happy"
    category.mkdir(parents=True)
    path = category / "a.png"
    Image.new("RGB", (40, 30), "red").save(path)
    Image.new("RGB", (40, 30), "blue").save(category / "b.png")
    sender._resolve_runtime_pack_context = lambda **kwargs: context
    sender.prompt_tail = ""
    sender.prompt_examples = []
    monkeypatch.setattr(plugin_main, "PLUGIN_DATA_DIR", tmp_path / "preview_data")
    shared.add_tool(copy.copy(llm_tools.get_func("send_meme")))
    if feature != "not_granted":
        shared.add_tool(copy.copy(llm_tools.get_func(PREVIEW_TOOL)))
    await sender._inject_meme_prompt_impl(event, request)
    active = feature in {"enabled", "skip", "summary_off", "multiple"}
    assert state["meme_manager_preview_available"] is active
    assert (request.func_tool.get_tool(PREVIEW_TOOL) is not None) is active
    assert (PREVIEW_TOOL in request.system_prompt) is active
    assert request.func_tool.get_tool("send_meme") is not None

    run_context = SimpleNamespace(messages=[])
    await sender.track_meme_tool_context(event, run_context)
    result = await sender.preview_memes_tool(event, "happy")
    if not active:
        assert json.loads(result)["reason"] == "preview_unavailable"
        if feature == "disabled":
            assert json.loads(await sender.send_meme_tool(event, "happy"))["status"] == "sent"
            await sender.compact_meme_tool_history(event, run_context, None)
            assert run_context.messages == [
                Message(role="assistant", content="已发送 1 张表情（happy）。")
            ]
        return
    assert isinstance(result, CallToolResult)
    assert '"next_after":1' in result.content[0].text.replace(" ", "")
    assert isinstance(result.content[1], ImageContent)
    assert json.loads(await sender.preview_memes_tool(event, "happy", after=1))[
        "reason"
    ] == "preview_pending"
    for number in (1, 2):
        call_id = f"preview-{number}"
        run_context.messages.extend(
            [
                Message(
                    role="assistant",
                    tool_calls=[
                        ToolCall(id=call_id, function={"name": PREVIEW_TOOL, "arguments": "{}"})
                    ],
                ),
                Message(role="tool", content="preview result", tool_call_id=call_id),
                Message(
                    role="user",
                    content=[
                        TextPart(text=f"[Image from tool 'preview_memes', path='{call_id}']"),
                        ImageURLPart(image_url={"url": "grid.jpg", "id": call_id}),
                    ],
                ),
            ]
        )
        if number == 1:
            assert isinstance(
                await sender.preview_memes_tool(event, "happy", after=1), CallToolResult
            )
            assert not run_context.messages
    assert json.loads(await sender.preview_memes_tool(event, "happy", after=2))[
        "reason"
    ] == "preview_limit"
    if feature == "skip":
        run_context.messages.append(Message(role="assistant", content="No suitable meme"))
        await sender.compact_meme_tool_history(event, run_context, None)
        assert [message.content for message in run_context.messages] == ["No suitable meme"]
        assert state["meme_manager_preview_candidates"] is None
        sender._send_meme_image.assert_not_called()
        return
    assert json.loads(await sender.send_meme_tool(event, "preview:1"))["reason"] == (
        "invalid_preview_candidate"
    )
    original = (category / "b.png").read_bytes()
    (category / "b.png").write_bytes(path.read_bytes())
    assert json.loads(await sender.send_meme_tool(event, "preview:2"))["reason"] == (
        "candidate_changed"
    )
    (category / "b.png").write_bytes(original)
    assert json.loads(await sender.send_meme_tool(event, "preview:2"))["status"] == "sent"
    emitted = sender._send_meme_image.await_args.args[1]
    assert base64.b64decode(emitted.file.removeprefix("base64://")) == original
    assert not run_context.messages
    if feature == "multiple":
        assert json.loads(await sender.send_meme_tool(event, "happy"))["status"] == "sent"
    run_context.messages.extend(
        [
            Message(
                role="assistant",
                content=[TextPart(text="Temporary image selection notes")],
                tool_calls=[
                    ToolCall(id="send", function={"name": "send_meme", "arguments": "{}"})
                ],
            ),
            Message(role="tool", tool_call_id="send", content="sent"),
            Message(role="assistant", content=[TextPart(text="Done")]),
        ]
    )
    await sender.compact_meme_tool_history(event, run_context, None)
    assert state["meme_manager_tool_context"] is None
    assert state["meme_manager_send_summary"] is None
    text = ["Done"]
    if feature == "multiple":
        text.append("\n\n已发送 2 张表情。")
    elif feature != "summary_off":
        text.append("\n\n已发送 1 张表情（happy，#2）。")
    assert len(run_context.messages) == 1
    assert run_context.messages[0].content == [TextPart(text=item) for item in text]
    provider.text_chat.assert_not_awaited()
