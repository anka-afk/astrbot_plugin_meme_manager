from pathlib import Path
from types import SimpleNamespace

import pytest
from backend import semantic_caption as caption
from PIL import Image


def test_build_caption_tool_set_allows_empty_suggested_category():
    tool_set = caption._build_caption_tool_set(["happy", "sad"])
    tools = getattr(tool_set, "tools", [])
    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == caption.CAPTION_TOOL_NAME
    suggested_category = tool.parameters["properties"]["suggested_category"]
    assert suggested_category["type"] == "string"
    assert "enum" not in suggested_category


def test_build_caption_prompt_contains_category_catalog_frames_and_review():
    prompt = caption.build_caption_prompt(
        frame_count=3,
        category="happy",
        category_description="开心回应",
        available_categories={"happy": "开心回应", "sad": "伤心"},
        review_instruction="这是自嘲，不是嘲笑别人",
        current_semantic={
            "caption": "旧描述",
            "tags": ["旧标签"],
            "current_category": "happy",
            "original_category": "sad",
        },
    )
    assert '当前分类名称："happy"' in prompt
    assert '"sad": "伤心"' in prompt
    assert '"happy": "开心回应"' not in prompt
    assert "人工复审纠错" in prompt
    assert "这是自嘲，不是嘲笑别人" in prompt
    assert "3 张图片来自同一个 GIF" in prompt


def test_prepare_visual_inputs_keeps_static_image(tmp_path):
    image_path = tmp_path / "static.png"
    Image.new("RGB", (2, 2), "red").save(image_path)
    visual_paths, temp_paths = caption.prepare_visual_inputs(image_path)
    assert visual_paths == [str(image_path.resolve())]
    assert temp_paths == []


def test_prepare_visual_inputs_samples_animated_gif_and_legacy_wrapper(tmp_path):
    gif_path = tmp_path / "animated.gif"
    frames = [Image.new("RGB", (2, 2), color) for color in ("red", "green", "blue")]
    frames[0].save(
        gif_path,
        save_all=True,
        append_images=frames[1:],
        duration=50,
        loop=0,
    )
    visual_paths, temp_paths = caption.prepare_visual_inputs(gif_path)
    try:
        assert len(visual_paths) == 3
        assert visual_paths == temp_paths
        assert all(Path(path).is_file() for path in temp_paths)
    finally:
        for path in temp_paths:
            Path(path).unlink(missing_ok=True)

    first_path, temporary_path = caption.prepare_visual_input(gif_path)
    try:
        assert first_path == temporary_path
        assert Path(first_path).is_file()
    finally:
        Path(first_path).unlink(missing_ok=True)


def test_prepare_visual_inputs_handles_unknown_static_and_broken_animation(tmp_path):
    unknown = tmp_path / "image.bin"
    unknown.write_bytes(b"not-an-image")
    assert caption.prepare_visual_inputs(unknown) == ([str(unknown.resolve())], [])
    broken_gif = tmp_path / "broken.gif"
    broken_gif.write_bytes(b"not-a-gif")
    with pytest.raises(ValueError, match="动图多帧处理失败"):
        caption.prepare_visual_inputs(broken_gif)


@pytest.mark.parametrize(
    ("usage", "names", "expected"),
    [
        ({"input_tokens": "12"}, ("input_tokens",), 12),
        (SimpleNamespace(prompt_tokens=8), ("prompt_tokens",), 8),
        ({"value": -1}, ("value",), 0),
        ({"value": "bad"}, ("value",), 0),
        (None, ("value",), 0),
    ],
)
def test_read_usage_number(usage, names, expected):
    assert caption._read_usage_number(usage, *names) == expected


def test_extract_token_usage_supports_new_old_and_nested_shapes():
    assert caption.extract_token_usage(
        {"usage": {"input": 10, "output": 5, "total": 15}}
    ) == {"input": 10, "output": 5, "total": 15, "calls": 1}
    assert caption.extract_token_usage(
        SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=10, cached_tokens=2, completion_tokens=3
            )
        )
    ) == {"input": 12, "output": 3, "total": 15, "calls": 1}
    assert caption.extract_token_usage(
        SimpleNamespace(
            usage=None,
            raw_completion=SimpleNamespace(
                usage=SimpleNamespace(input_tokens=4, output_tokens=2)
            ),
        )
    ) == {"input": 4, "output": 2, "total": 6, "calls": 1}


def test_merge_token_usage_ignores_invalid_and_negative_values():
    assert caption._merge_token_usage(
        [
            {"input": 3, "output": 2, "total": 5, "calls": 1},
            {"input": -1, "output": "bad", "total": 4, "calls": 1},
        ]
    ) == {"input": 3, "output": 2, "total": 9, "calls": 2}


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("response_format is unsupported", True),
        ("结构化输出不支持", True),
        ("network unavailable", False),
    ],
)
def test_structured_output_unsupported_detection(message, expected):
    assert caption._structured_output_is_unsupported(RuntimeError(message)) is expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("tool_choice is unsupported", True),
        ("工具调用未启用", True),
        ("request timed out", False),
        ("tools request timed out", False),
    ],
)
def test_tool_call_unsupported_detection(message, expected):
    assert caption._tool_call_is_unsupported(RuntimeError(message)) is expected


def test_caption_output_mode_cache():
    context = SimpleNamespace()
    assert caption._caption_output_mode(context, "provider") == ""
    caption._remember_caption_output_mode(context, "provider", "tool")
    assert caption._caption_output_mode(context, "provider") == "tool"


def test_caption_tool_and_response_payloads():
    response = {
        "tools_call_name": ["other", caption.CAPTION_TOOL_NAME],
        "tools_call_args": [{"ignored": True}, {"caption": "猫"}],
        "completion_text": "fallback",
    }
    assert caption._caption_tool_call_payload(response) == (
        True,
        {"caption": "猫"},
    )
    assert caption._caption_response_payload(response) == {"caption": "猫"}
    assert caption._caption_tool_call_payload({"tools_call_name": "other"}) == (
        False,
        None,
    )
    assert caption._caption_response_payload({"completion_text": "text"}) == "text"
    assert caption._caption_response_payload(SimpleNamespace(text="object text")) == (
        "object text"
    )
