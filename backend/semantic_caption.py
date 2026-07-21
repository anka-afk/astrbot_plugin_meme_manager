"""离线视觉语义化：提示词、GIF 第一帧和模型结果校验。"""

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


def build_caption_prompt() -> str:
    return CAPTION_PROMPT


def prepare_visual_input(path: Path | str) -> tuple[str, str | None]:
    """返回视觉模型可读的本地路径；GIF 只生成临时第一帧。"""
    source = Path(path).resolve()
    if source.suffix.lower() != ".gif":
        return str(source), None
    try:
        from PIL import Image

        with Image.open(source) as image:
            image.seek(0)
            frame = image.convert("RGBA")
            output = tempfile.NamedTemporaryFile(
                prefix="meme_frame_", suffix=".png", delete=False
            )
            frame.save(output, format="PNG")
            output.close()
            return output.name, output.name
    except Exception as exc:
        raise ValueError(f"GIF 第一帧处理失败：{exc}") from exc


async def generate_caption(
    context: Any, image_path: Path | str, provider_id: str = ""
) -> dict[str, Any]:
    """调用 AstrBot 的视觉聊天模型；失败由任务层记录为单张 failed。"""
    if context is None or not callable(getattr(context, "llm_generate", None)):
        raise RuntimeError("当前没有可用的视觉模型上下文")
    visual_path, temp_path = prepare_visual_input(image_path)
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
            prompt=build_caption_prompt(),
            image_urls=[visual_path],
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
        if temp_path:
            try:
                Path(temp_path).unlink(missing_ok=True)
            except Exception:
                pass
