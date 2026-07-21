"""离线视觉语义化：提示词、GIF 三帧采样和模型结果校验。"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from .semantic_models import PROMPT_VERSION, parse_caption_result

CAPTION_PROMPT = """请分析这张表情包，返回严格 JSON，不要解释。

字段：
- caption：一句自然、具体的中文描述，说明人物或画面的动作、情绪、态度和潜台词；
- tags：5 到 8 个细粒度中文标签，包含情绪、动作、场景或社交意图；
- visible_text：图片中清晰可见的文字，没有则返回空字符串。

不要只返回“开心、悲伤、生气”这类宽泛情绪。
优先描述它适合在什么对话场景中使用，例如：尴尬、装傻、嘴硬、心虚、无语地看着对方、发现事情不对、试图转移话题。
"""


def build_caption_prompt(frame_count: int = 1) -> str:
    if frame_count <= 1:
        return CAPTION_PROMPT
    return (
        CAPTION_PROMPT
        + f"\n你看到的 {frame_count} 张图片来自同一个 GIF，按开始、中间、结束的时间顺序排列。"
        "请结合动作变化理解完整含义，不要把它们当成互不相关的图片。\n"
    )


def prepare_visual_inputs(path: Path | str) -> tuple[list[str], list[str]]:
    """返回视觉模型输入；GIF 按首、中、尾位置生成最多三张临时 PNG。"""
    source = Path(path).resolve()
    if source.suffix.lower() != ".gif":
        return [str(source)], []
    temp_paths: list[str] = []
    try:
        from PIL import Image

        with Image.open(source) as image:
            frame_count = max(1, int(getattr(image, "n_frames", 1) or 1))
            frame_indexes = sorted({0, frame_count // 2, frame_count - 1})
            for frame_index in frame_indexes:
                image.seek(frame_index)
                frame = image.convert("RGBA")
                output = tempfile.NamedTemporaryFile(
                    prefix=f"meme_frame_{frame_index}_",
                    suffix=".png",
                    delete=False,
                )
                try:
                    frame.save(output, format="PNG")
                finally:
                    output.close()
                temp_paths.append(output.name)
            return list(temp_paths), temp_paths
    except Exception as exc:
        for temp_path in temp_paths:
            Path(temp_path).unlink(missing_ok=True)
        raise ValueError(f"GIF 首中尾帧处理失败：{exc}") from exc


def prepare_visual_input(path: Path | str) -> tuple[str, str | None]:
    """保留旧调用接口；新代码应使用 prepare_visual_inputs。"""
    visual_paths, temp_paths = prepare_visual_inputs(path)
    for extra_path in temp_paths[1:]:
        Path(extra_path).unlink(missing_ok=True)
    return visual_paths[0], temp_paths[0] if temp_paths else None


async def generate_caption(
    context: Any, image_path: Path | str, provider_id: str = ""
) -> dict[str, Any]:
    """调用 AstrBot 的视觉聊天模型；失败由任务层记录为单张 failed。"""
    if context is None or not callable(getattr(context, "llm_generate", None)):
        raise RuntimeError("当前没有可用的视觉模型上下文")
    visual_paths, temp_paths = prepare_visual_inputs(image_path)
    try:
        selected_provider = provider_id
        if not selected_provider and hasattr(context, "get_current_chat_provider_id"):
            try:
                selected_provider = await context.get_current_chat_provider_id(umo="")
            except Exception:
                selected_provider = ""
        if not selected_provider:
            provider_manager = getattr(context, "provider_manager", None)
            provider_map = getattr(provider_manager, "inst_map", None)
            if isinstance(provider_map, dict):
                selected_provider = next(iter(provider_map), "")
        if not selected_provider:
            raise RuntimeError("未配置视觉模型")
        response = await context.llm_generate(
            chat_provider_id=selected_provider,
            prompt=build_caption_prompt(len(visual_paths)),
            image_urls=visual_paths,
        )
        raw = (
            getattr(response, "completion_text", None)
            or getattr(response, "text", None)
            or response
        )
        caption, tags, visible_text = parse_caption_result(raw)
        return {
            "caption": caption,
            "tags": tags,
            "visible_text": visible_text,
            "vision_model": selected_provider,
            "prompt_version": PROMPT_VERSION,
        }
    finally:
        for temp_path in temp_paths:
            try:
                Path(temp_path).unlink(missing_ok=True)
            except Exception:
                pass
