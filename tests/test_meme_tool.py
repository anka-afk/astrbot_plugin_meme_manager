import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image as PillowImage

from astrbot_plugin_meme_manager.main import MEME_TOOL_PROMPT_HINT, MemeSender


def make_tool_context(tmp_path, *, enabled=True, limit=-1, mapping=None):
    mapping = mapping or {"happy": "开心", "sad": "难过"}
    for tag in mapping:
        directory = tmp_path / tag
        directory.mkdir()
        PillowImage.new("RGB", (2, 2)).save(directory / "meme.png")

    sender = object.__new__(MemeSender)
    sender.config = {}
    sender.meme_tool_enabled = enabled
    sender.max_memes_per_message = limit
    sender.emotions_probability = 100
    sender.convert_static_to_gif = False
    sender.category_mapping = mapping
    sender._resolve_runtime_pack_context = lambda **kwargs: {
        "category_mapping": mapping,
        "pack_dir": tmp_path,
    }
    sender._get_runtime_memes_dir_for_event = lambda event: tmp_path

    state = {}
    event = SimpleNamespace(
        get_extra=lambda key: state.get(key),
        set_extra=lambda key, value: state.update({key: value}),
    )
    return sender, event, state


def make_fake_tool_set():
    tool_set = SimpleNamespace(removed=[])
    tool_set.get_full_tool_set = lambda: tool_set
    tool_set.remove_tool = lambda name: tool_set.removed.append(name)
    return tool_set


@pytest.mark.asyncio
async def test_send_meme_tool_disabled_returns_error(tmp_path):
    sender, event, _ = make_tool_context(tmp_path, enabled=False)

    result = json_loads(await sender.send_meme_tool(event, "happy"))

    assert result["ok"] is False
    assert "未启用" in result["reason"]


@pytest.mark.asyncio
async def test_send_meme_tool_sends_image_and_records_state(tmp_path):
    sender, event, state = make_tool_context(tmp_path)

    result = json_loads(await sender.send_meme_tool(event, "happy"))

    assert result["ok"] is True
    tool_images = state.get("meme_manager_tool_images") or []
    assert len(tool_images) == 1
    assert state.get("meme_manager_tool_sent_emotions") == ["happy"]
    assert not state.get("meme_manager_pending_images")


@pytest.mark.asyncio
async def test_send_meme_tool_multiple_categories_accumulate_images(tmp_path):
    sender, event, state = make_tool_context(tmp_path)

    first = json_loads(await sender.send_meme_tool(event, "happy"))
    second = json_loads(await sender.send_meme_tool(event, "sad"))

    assert first["ok"] is True
    assert second["ok"] is True
    tool_images = state.get("meme_manager_tool_images") or []
    assert len(tool_images) == 2
    assert state.get("meme_manager_tool_sent_emotions") == ["happy", "sad"]


@pytest.mark.asyncio
async def test_send_meme_tool_sends_immediately_on_streaming_platform(tmp_path):
    sender, event, state = make_tool_context(tmp_path)
    sender.streaming_compatibility = True
    sender._send_meme_image = AsyncMock()

    result = json_loads(await sender.send_meme_tool(event, "happy"))

    assert result["ok"] is True
    assert sender._send_meme_image.await_count == 1
    assert not state.get("meme_manager_tool_images")
    assert state.get("meme_manager_tool_sent_emotions") == ["happy"]
    assert "已发出" in result["instruction"]


@pytest.mark.asyncio
async def test_send_meme_tool_returns_error_when_direct_send_fails(tmp_path):
    sender, event, state = make_tool_context(tmp_path)
    sender.streaming_compatibility = True
    sender._send_meme_image = AsyncMock(side_effect=RuntimeError("send failed"))

    result = json_loads(await sender.send_meme_tool(event, "happy"))

    assert result["ok"] is False
    assert "发送失败" in result["reason"]
    assert state.get("meme_manager_tool_sent_emotions") is None


@pytest.mark.asyncio
async def test_send_meme_tool_matches_category_case_insensitively(tmp_path):
    sender, event, state = make_tool_context(tmp_path)

    result = json_loads(await sender.send_meme_tool(event, "HAPPY"))

    assert result["ok"] is True
    assert state.get("meme_manager_tool_sent_emotions") == ["happy"]


@pytest.mark.asyncio
async def test_send_meme_tool_rejects_duplicate_emotion(tmp_path):
    sender, event, _ = make_tool_context(tmp_path)

    first = json_loads(await sender.send_meme_tool(event, "happy"))
    second = json_loads(await sender.send_meme_tool(event, "happy"))

    assert first["ok"] is True
    assert second["ok"] is False
    assert "已发送过" in second["reason"]


@pytest.mark.asyncio
async def test_send_meme_tool_respects_hard_limit(tmp_path):
    sender, event, _ = make_tool_context(tmp_path, limit=1)

    first = json_loads(await sender.send_meme_tool(event, "happy"))
    second = json_loads(await sender.send_meme_tool(event, "sad"))

    assert first["ok"] is True
    assert second["ok"] is False
    assert "上限" in second["reason"]


@pytest.mark.asyncio
async def test_send_meme_tool_rejects_unknown_category(tmp_path):
    sender, event, _ = make_tool_context(tmp_path)

    result = json_loads(await sender.send_meme_tool(event, "angry"))

    assert result["ok"] is False
    assert "未知表情分类" in result["reason"]
    assert "happy" in result["available"]


@pytest.mark.asyncio
async def test_send_meme_tool_rejects_empty_category_directory(tmp_path):
    mapping = {"happy": "开心"}
    sender, event, _ = make_tool_context(tmp_path, mapping=mapping)
    (tmp_path / "happy").mkdir(exist_ok=True)
    for existing in (tmp_path / "happy").iterdir():
        existing.unlink()

    result = json_loads(await sender.send_meme_tool(event, "happy"))

    assert result["ok"] is False
    assert "没有可发送的表情图片" in result["reason"]


@pytest.mark.asyncio
async def test_send_meme_tool_rejects_semantic_mode(tmp_path):
    sender, event, state = make_tool_context(tmp_path)
    state["meme_manager_semantic_active"] = True

    result = json_loads(await sender.send_meme_tool(event, "happy"))

    assert result["ok"] is False
    assert "语义模式" in result["reason"]


def test_remove_meme_tool_removes_tool_from_request():
    tool_set = make_fake_tool_set()
    req = SimpleNamespace(system_prompt="", func_tool=tool_set)

    MemeSender._remove_meme_tool(req)

    assert tool_set.removed == ["send_meme"]


def test_remove_meme_tool_handles_missing_tool_set():
    req = SimpleNamespace(system_prompt="", func_tool=None)

    MemeSender._remove_meme_tool(req)


def make_prompt_sender(mapping=None, *, enabled=True, supports_tools=True):
    mapping = mapping or {"happy": "开心", "sad": "难过"}
    sender = object.__new__(MemeSender)
    sender.config = {}
    sender.emotion_llm_enabled = False
    sender.semantic_enabled = False
    sender.meme_tool_enabled = enabled
    sender.category_mapping = mapping
    sender._semantic_pack_ready = lambda **kwargs: False
    sender._reply_model_supports_tools = lambda event: supports_tools
    sender._resolve_runtime_pack_context = lambda **kwargs: {
        "category_mapping": mapping
    }
    sender._build_meme_prompt = lambda mapping_string=None: "PROMPT"
    sender._wrap_meme_prompt = lambda prompt: f"<wrap>{prompt}</wrap>"
    sender._strip_meme_prompt = lambda prompt: prompt or ""
    sender._remove_meme_tool = MagicMock()
    sender._remove_semantic_tool = MagicMock()
    return sender


def test_apply_request_prompt_appends_tool_hint_when_enabled():
    sender = make_prompt_sender()
    req = SimpleNamespace(system_prompt="", func_tool=None)

    sender._apply_request_prompt(req, event=None)

    assert MEME_TOOL_PROMPT_HINT in req.system_prompt
    sender._remove_meme_tool.assert_not_called()


def test_apply_request_prompt_removes_tool_when_disabled():
    sender = make_prompt_sender(enabled=False)
    req = SimpleNamespace(system_prompt="", func_tool=None)

    sender._apply_request_prompt(req, event=None)

    assert MEME_TOOL_PROMPT_HINT not in req.system_prompt
    sender._remove_meme_tool.assert_called_once_with(req)


def test_apply_request_prompt_removes_tool_when_model_unsupported():
    sender = make_prompt_sender(supports_tools=False)
    req = SimpleNamespace(system_prompt="", func_tool=None)

    sender._apply_request_prompt(req, event=None)

    sender._remove_meme_tool.assert_called_once_with(req)


def test_apply_request_prompt_removes_tool_in_emotion_llm_mode():
    sender = make_prompt_sender(enabled=False)
    sender.emotion_llm_enabled = True
    sender._semantic_pack_ready = lambda **kwargs: False
    req = SimpleNamespace(system_prompt="", func_tool=None)

    sender._apply_request_prompt(req, event=None)

    sender._remove_meme_tool.assert_called_once_with(req)


def test_apply_request_prompt_meme_tool_mode_is_exclusive():
    sender = make_prompt_sender()
    sender.meme_tool_enabled = True
    sender.emotion_llm_enabled = True
    sender._semantic_pack_ready = MagicMock(return_value=True)
    req = SimpleNamespace(system_prompt="", func_tool=None)

    sender._apply_request_prompt(req, event=None)

    sender._semantic_pack_ready.assert_not_called()
    sender._remove_semantic_tool.assert_called_once_with(req)
    assert MEME_TOOL_PROMPT_HINT in req.system_prompt
    sender._remove_meme_tool.assert_not_called()


@pytest.mark.asyncio
async def test_build_emotion_images_skips_probability_when_tool_requests(tmp_path):
    sender, event, _ = make_tool_context(tmp_path)
    sender.emotions_probability = 0

    without_respect, _ = await sender._build_emotion_images_for_event(
        event, ["happy"], respect_probability=False
    )
    with_respect, _ = await sender._build_emotion_images_for_event(
        event, ["happy"], respect_probability=True
    )

    assert len(without_respect) == 1
    assert with_respect == []


def json_loads(text):
    import json

    return json.loads(text)


def test_event_loop_not_required_for_helpers():
    assert asyncio.iscoroutinefunction(MemeSender.send_meme_tool)
