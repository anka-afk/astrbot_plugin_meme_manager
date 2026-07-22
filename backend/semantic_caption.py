"""离线视觉语义化：提示词、GIF 多帧采样和模型结果校验。"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from .semantic_models import PROMPT_VERSION, parse_caption_result

CAPTION_PROMPT = """你是中文互联网表情包语义分析员。你的任务不是给图片写普通图注，而是还原这张图作为聊天回复时真正传达的意思，让人能够按对话情境准确搜索到它。

请在内部完成以下分析，不要输出分析过程：

一、分离画面证据
- 识别主体的表情、视线、姿势、动作、物品、特效、标点和原始文字。
- 区分原图内容与后期叠加的文字、emoji、符号、裁切和夸张特效。后期元素通常是在替图片“配语气”，不能误当成人物原本的表情或物品。
- 如果是 GIF，结合全部输入帧判断动作的起点、变化、方向和结果，不要用某一帧概括整段动作。

二、判断梗的构成方式
- 判断它主要依靠动作反应、文字与画面的配合或反差、夸张符号、谐音/错字/拆字、游戏或作品元素、角色二创、经典模板等哪种机制表达意思。
- 图片文字既要按原字读取，也要结合整句话判断网络黑话、同音替换、拼音、数字或英文字母代称。还原含义时保留原文，不要回避粗口或攻击性用语，也不要在证据不足时强行解梗。
- 严格保留原文的语气和标点。不能擅自添加问号、感叹号或否定词，从而把陈述改成质问、把自嘲改成指责。

三、确定说话视角和行为归属
- 分清三个角色：发送表情包的人、聊天对象、图中人物。图中人物经常是在替发送者表演某种反应，并不天然代表被评价的对方。
- 对省略主语或宾语的短句，必须比较至少三种解释：发送者在说自己或己方、发送者在评价对方、发送者在吐槽第三方。不能默认所有句子都在质问聊天对象。
- 结合文字的陈述/疑问形式、人物表情、动作方向和图文反差选择指向。如果人物用开心、得意、点赞、卖萌等方式主动认领一种本应尴尬或负面的状态，应优先考虑己方自嘲、承认后装傻、厚脸皮调侃等用法，而不是自动解释成批评对方。
- 明确蠢事、失误、越界行为或尴尬处境究竟是发送者一方、对方还是第三方造成的，并在 caption 和 tags 中保持一致。

四、按条件核实外部知识
- 如果当前请求环境确实提供联网搜索能力，只要画面主体疑似有明确身份、作品出处或经典模板来源，就必须尝试检索核实；不能因为不确定搜索词而直接跳过。
- 搜索时应使用稳定的外观特征、服饰/道具、画面文字和可能的作品线索交叉验证，不能仅凭发色、瞳色或印象认人。
- 有可靠结果时，将人物名、作品名或模板来源自然写入 caption 或 tags，方便按身份搜索；但角色身份和原作人设只能作为辅助证据，不能覆盖当前图片经过二创后的实际语气。
- 如果没有联网能力或没有可靠结果，就省略身份，不得声称已经检索，也不能把搜索猜测写成事实。

五、还原聊天中的真实用法
- 先推断“什么样的上一句话或行为会触发发送这张图”，再判断发送者是在质问、反驳、拒绝、催促、吐槽、嘲讽、敷衍、求饶、炫耀还是表达其他反应。
- 情绪必须写成贴近口语的复合语气，例如惊讶中带戒备、恼火中带疑惑、无奈中带嫌弃，而不是只贴单一的情绪类别。
- 给出最符合全部证据的一种核心解读，并补充一到两个相近使用场景。优先使用聊天中真的会说的话来概括潜台词，不要写成文学化的人像观察。
- 表情不等于梗义：人物面无表情不一定只是冷漠，愤怒符号也不一定代表暴怒。要综合文字、符号、构图、动作和常见聊天习惯判断强度与语气。

六、输出前自检
- 描述是否回答了“这图在回复什么、为什么此时发、语气有多重”，而不只是“画了什么”。
- 每个关键判断是否有画面、文字、动作或可靠外部知识支撑。
- 是否误把后期贴图当成原图内容，误把角色身份当成表情含义，或套用了与图片无关的固定场景。
- 是否明确了事情是谁做的、谁在装傻或被调侃；有没有凭空改变原文标点，导致说话方向反转。

最后只返回严格 JSON，不要使用 Markdown，不要增加字段：
- caption：一到两句自然中文；先概括核心梗义和复合语气，再说明典型触发语境或用法；身份仅在已可靠核实时提及。
- tags：6 到 10 个细粒度中文标签，覆盖核心梗义、说话视角、行为归属、言语功能、复合语气、触发场景及关键视觉/文字线索。
- visible_text：图片中清晰可见的原始文字，没有则为空字符串。

格式必须为：
{"caption":"……","tags":["……","……"],"visible_text":"……"}
"""

MAX_GIF_FRAMES = 5


def build_caption_prompt(frame_count: int = 1) -> str:
    if frame_count <= 1:
        return CAPTION_PROMPT
    return (
        CAPTION_PROMPT
        + f"\n你看到的 {frame_count} 张图片来自同一个 GIF，按从开始到结束的时间顺序等间隔排列。"
        "请结合动作变化理解完整含义，不要把它们当成互不相关的图片。\n"
    )


def prepare_visual_inputs(path: Path | str) -> tuple[list[str], list[str]]:
    """返回视觉模型输入；GIF 等间隔生成最多五张临时 PNG。"""
    source = Path(path).resolve()
    if source.suffix.lower() != ".gif":
        return [str(source)], []
    temp_paths: list[str] = []
    try:
        from PIL import Image

        with Image.open(source) as image:
            frame_count = max(1, int(getattr(image, "n_frames", 1) or 1))
            sample_count = min(frame_count, MAX_GIF_FRAMES)
            if sample_count <= 1:
                frame_indexes = [0]
            else:
                frame_indexes = [
                    round(position * (frame_count - 1) / (sample_count - 1))
                    for position in range(sample_count)
                ]
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
        raise ValueError(f"GIF 多帧处理失败：{exc}") from exc


def prepare_visual_input(path: Path | str) -> tuple[str, str | None]:
    """保留旧调用接口；新代码应使用 prepare_visual_inputs。"""
    visual_paths, temp_paths = prepare_visual_inputs(path)
    for extra_path in temp_paths[1:]:
        Path(extra_path).unlink(missing_ok=True)
    return visual_paths[0], temp_paths[0] if temp_paths else None


def _read_usage_number(usage: Any, *names: str) -> int:
    """兼容 AstrBot TokenUsage 和不同模型返回的 usage 字段。"""
    if usage is None:
        return 0
    for name in names:
        value = (
            usage.get(name)
            if isinstance(usage, dict)
            else getattr(usage, name, None)
        )
        if value is None:
            continue
        try:
            number = int(value or 0)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            return number
    return 0


def extract_token_usage(response: Any) -> dict[str, int]:
    """从视觉模型响应中提取输入、输出和总 token；没有返回时保持为 0。"""
    usage = (
        response.get("usage")
        if isinstance(response, dict)
        else getattr(response, "usage", None)
    )
    if usage is None:
        raw_completion = getattr(response, "raw_completion", None)
        usage = getattr(raw_completion, "usage", None)
    raw_input = (
        usage.get("input")
        if isinstance(usage, dict)
        else getattr(usage, "input", None)
    )
    if raw_input is not None:
        input_tokens = _read_usage_number(usage, "input")
        cached_tokens = 0
    else:
        input_tokens = _read_usage_number(
            usage,
            "input_tokens",
            "prompt_tokens",
            "input_other",
        )
        cached_tokens = _read_usage_number(usage, "input_cached", "cached_tokens")
    output_tokens = _read_usage_number(
        usage,
        "output",
        "output_tokens",
        "completion_tokens",
    )
    total = _read_usage_number(usage, "total", "total_tokens")
    if total <= 0:
        total = input_tokens + cached_tokens + output_tokens
    return {
        "input": input_tokens + cached_tokens,
        "output": output_tokens,
        "total": total,
    }


async def generate_caption(
    context: Any, image_path: Path | str, provider_id: str = ""
) -> dict[str, Any]:
    """调用 AstrBot 的视觉聊天模型；失败由任务层记录为单张 failed。"""
    if context is None or not callable(getattr(context, "llm_generate", None)):
        raise RuntimeError("当前没有可用的视觉模型上下文")
    visual_paths, temp_paths = prepare_visual_inputs(image_path)
    try:
        selected_provider = provider_id
        if not selected_provider:
            raise RuntimeError("未配置视觉模型，请先选择支持图片输入的视觉模型 Provider")
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
        token_usage = extract_token_usage(response)
        try:
            caption, tags, visible_text = parse_caption_result(raw)
        except Exception as exc:
            # 即使返回内容无法解析，也保留本次已发生的调用用量。
            setattr(exc, "token_usage", token_usage)
            setattr(exc, "result_preview", str(raw or "")[:1000])
            raise
        return {
            "caption": caption,
            "tags": tags,
            "visible_text": visible_text,
            "vision_model": selected_provider,
            "prompt_version": PROMPT_VERSION,
            "token_usage": token_usage,
        }
    finally:
        for temp_path in temp_paths:
            try:
                Path(temp_path).unlink(missing_ok=True)
            except Exception:
                pass
