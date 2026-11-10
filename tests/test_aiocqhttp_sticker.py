from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_meme_manager.main import MemeSender
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


def _make_sender(tmp_path):
    mapping = {"happy": "开心", "sad": "难过"}
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
    sender.context = SimpleNamespace(send_message=AsyncMock())
    sender.category_mapping = mapping
    sender.trigger_scope = "only_chat_llm"
    sender.emotions_probability = 100
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
    return sender


def _make_event(platform="aiocqhttp"):
    state = {LLM_REQUEST_ORIGIN_EXTRA_KEY: "chat"}
    raw_message = {"message_type": "group", "group_id": 123, "self_id": 1}
    event = SimpleNamespace(
        get_extra=lambda key: state.get(key),
        set_extra=lambda key, value: state.update({key: value}),
        get_result=lambda: state.get("result"),
        get_platform_name=lambda: platform,
        unified_msg_origin=f"{platform}:GroupMessage:123",
        message_obj=SimpleNamespace(raw_message=raw_message),
        bot=SimpleNamespace(send=AsyncMock()),
        _parse_onebot_json=AsyncMock(
            return_value=[{"type": "image", "data": {"file": "base64://AAAA"}}]
        ),
    )
    return event, state


@pytest.fixture
def sticker_runtime(tmp_path):
    sender = _make_sender(tmp_path)
    event, state = _make_event()
    image = Image.fromFileSystem(str(tmp_path / "happy" / "meme.png"))
    return sender, event, state, image


@pytest.mark.asyncio
async def test_aiocqhttp_image_sent_as_qq_sticker(sticker_runtime):
    sender, event, _, image = sticker_runtime

    await sender._send_meme_image(event, image)

    event._parse_onebot_json.assert_awaited_once()
    assert event._parse_onebot_json.await_args.args[0].chain == [image]
    event.bot.send.assert_awaited_once()
    raw, segments = event.bot.send.await_args.args
    assert raw is event.message_obj.raw_message
    assert segments[0]["data"]["sub_type"] == 1
    assert segments[0]["data"]["summary"] == "[动画表情]"
    assert segments[0]["data"]["file"] == "base64://AAAA"
    sender.context.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_aiocqhttp_sticker_failure_falls_back_to_plain_image(sticker_runtime):
    sender, event, _, image = sticker_runtime
    event.bot.send.side_effect = RuntimeError("napcat rejected")

    await sender._send_meme_image(event, image)

    sender.context.send_message.assert_awaited_once()
    origin, chain = sender.context.send_message.await_args.args
    assert origin == event.unified_msg_origin
    assert chain.chain == [image]


@pytest.mark.asyncio
async def test_other_platforms_do_not_use_sticker_path(tmp_path):
    sender = _make_sender(tmp_path)
    event, _ = _make_event(platform="telegram")
    image = Image.fromFileSystem(str(tmp_path / "happy" / "meme.png"))

    await sender._send_meme_image(event, image)

    event._parse_onebot_json.assert_not_awaited()
    event.bot.send.assert_not_awaited()
    sender.context.send_message.assert_awaited_once()


async def _decorate(sender, event, state, text):
    response = LLMResponse(role="assistant", completion_text=text)
    await sender._resp_impl(event, response)
    chain = (
        response.result_chain.chain
        if response.result_chain
        else [Plain(response.completion_text)]
    )
    state["result"] = MessageEventResult(
        chain=chain, result_content_type=ResultContentType.LLM_RESULT
    )
    await sender._on_decorating_result_impl(event)


@pytest.mark.asyncio
@pytest.mark.parametrize("mixed", [False, True])
async def test_aiocqhttp_meme_only_reply_sends_stickers_directly(
    sticker_runtime, mixed
):
    sender, event, state, _ = sticker_runtime
    sender.enable_mixed_message = mixed
    sender._send_meme_image = AsyncMock()

    await _decorate(sender, event, state, "&&happy&&\n&&sad&&")

    assert sender._send_meme_image.await_count == 2
    assert not state["result"].chain
    assert not state.get("meme_manager_pending_images")
    assert not state.get("meme_manager_temp_files")
    assert state.get("found_emotions") is None


@pytest.mark.asyncio
async def test_aiocqhttp_meme_only_send_error_still_clears_state(sticker_runtime):
    sender, event, state, _ = sticker_runtime
    sender._send_meme_image = AsyncMock(side_effect=RuntimeError("boom"))

    await _decorate(sender, event, state, "&&happy&&")

    assert not state["result"].chain
    assert state.get("found_emotions") is None
    assert state.get("meme_manager_semantic_selected_ids") is None


@pytest.mark.asyncio
async def test_aiocqhttp_text_reply_unmixed_sends_stickers_after_text(
    sticker_runtime,
):
    sender, event, state, _ = sticker_runtime
    sender.enable_mixed_message = False
    sender._send_meme_image = AsyncMock()

    await _decorate(sender, event, state, "你好 &&happy&&")

    assert [type(c) for c in state["result"].chain] == [Plain]
    assert len(state["meme_manager_pending_images"]) == 1
    sender._send_meme_image.assert_not_awaited()

    await sender._after_message_sent_impl(event)
    sender._send_meme_image.assert_awaited_once()


@pytest.mark.asyncio
async def test_aiocqhttp_text_reply_keeps_mixed_setting(sticker_runtime):
    sender, event, state, _ = sticker_runtime
    sender.enable_mixed_message = True
    sender.mixed_message_probability = 100
    sender._send_meme_image = AsyncMock()

    await _decorate(sender, event, state, "你好 &&happy&&")

    chain = state["result"].chain
    assert any(isinstance(c, Plain) for c in chain)
    assert any(isinstance(c, Image) for c in chain)
    assert not state.get("meme_manager_pending_images")
    sender._send_meme_image.assert_not_awaited()
