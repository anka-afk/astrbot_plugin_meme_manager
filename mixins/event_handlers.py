import asyncio
import os
import random
import re
import ssl
import tempfile
import time
import traceback
import io
import json
from typing import Any

import aiohttp
from PIL import Image as PILImage
from astrbot.api import logger
from astrbot.api.all import *
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import *
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core.message.components import Plain, Image
from astrbot.core.message.message_event_result import MessageChain, ResultContentType

from ..config import MEMES_DIR
from ..backend.semantic_query import validate_selected_id


class EventHandlerMixin:
    """处理图片上传、LLM 响应解析、消息装饰等事件"""

    def _normalize_outgoing_message_components(self, message: Any) -> list:
        """将外部传入消息统一为组件列表。"""
        if isinstance(message, MessageChain):
            return list(message.chain or [])
        if isinstance(message, list):
            return list(message)
        if isinstance(message, str):
            return [Plain(message)]
        raise TypeError("message 必须是 str、list 或 MessageChain")

    def _extract_marked_emotions_from_text(
        self,
        text: str,
        valid_emoticons: set[str],
    ) -> tuple[str, list[str]]:
        """提取文本中的表情标记并返回清理后的文本。"""
        clean_text = text or ""
        found_emotions: list[str] = []

        # 严格标记：&&emotion&&
        strict_pattern = re.compile(r"&&([^&]+?)&&")

        def _replace_strict(match: re.Match) -> str:
            emotion = match.group(1).strip()
            if emotion in valid_emoticons:
                found_emotions.append(emotion)
            return ""

        clean_text = strict_pattern.sub(_replace_strict, clean_text)

        # 可选替代标记：[emotion] 与 (emotion)
        if self._read_config_value(
            ("generation", "markup", "enable_alternative"),
            default=True,
            legacy_keys=("enable_alternative_markup",),
        ):
            bracket_pattern = re.compile(r"\[([^\[\]]+)\]")

            def _replace_bracket(match: re.Match) -> str:
                emotion = match.group(1).strip()
                if emotion in valid_emoticons:
                    found_emotions.append(emotion)
                    return ""
                if self.remove_invalid_alternative_markup:
                    return ""
                return match.group(0)

            clean_text = bracket_pattern.sub(_replace_bracket, clean_text)

            paren_pattern = re.compile(r"\(([^()]+)\)")

            def _replace_paren(match: re.Match) -> str:
                emotion = match.group(1).strip()
                markup = match.group(0)
                if emotion in valid_emoticons and self._is_likely_emotion_markup(
                    markup, clean_text, match.start()
                ):
                    found_emotions.append(emotion)
                    return ""
                if self.remove_invalid_alternative_markup:
                    return ""
                return markup

            clean_text = paren_pattern.sub(_replace_paren, clean_text)

        # 防御性清理残留符号
        clean_text = re.sub(r"&&+", "", clean_text)
        return clean_text, found_emotions

    def _build_emotion_images_for_event(
        self,
        event: AstrMessageEvent,
        emotions: list[str],
    ) -> tuple[list[Image], list[str]]:
        """根据表情列表构建待发送图片组件，并返回临时文件列表。"""
        if not emotions:
            return [], []

        random_value = random.randint(1, 100)
        if random_value > self.emotions_probability:
            return [], []

        memes_root = self._get_runtime_memes_dir_for_event(event)
        emotion_images: list[Image] = []
        temp_files: list[str] = []

        for emotion in emotions:
            if not emotion:
                continue

            emotion_path = os.path.join(memes_root, emotion)
            if not os.path.exists(emotion_path):
                continue

            memes = [
                f
                for f in os.listdir(emotion_path)
                if f.endswith((".jpg", ".png", ".gif"))
            ]
            if not memes:
                continue

            meme = random.choice(memes)
            meme_file = os.path.join(emotion_path, meme)

            try:
                final_meme_file = self._convert_to_gif(meme_file)
                if final_meme_file != meme_file:
                    temp_files.append(final_meme_file)
                emotion_images.append(Image.fromFileSystem(final_meme_file))
            except Exception as e:
                logger.error(f"[meme_manager] 构建表情图片失败: {e}")

        return emotion_images, temp_files

    async def compat_prepare_message(
        self,
        event: AstrMessageEvent,
        message: str | list | MessageChain,
    ) -> dict:
        """对外兼容接口：清理消息中的表情标记并准备待发送表情图片。"""
        pack_context = self._resolve_runtime_pack_context(event=event)
        runtime_category_mapping = (
            pack_context.get("category_mapping") or self.category_mapping
        )
        valid_emoticons = set(runtime_category_mapping.keys())

        raw_components = self._normalize_outgoing_message_components(message)
        cleaned_components = []
        found_emotions: list[str] = []

        for component in raw_components:
            if isinstance(component, Plain):
                cleaned_text, extracted = self._extract_marked_emotions_from_text(
                    component.text,
                    valid_emoticons,
                )
                found_emotions.extend(extracted)
                if cleaned_text.strip():
                    cleaned_components.append(Plain(cleaned_text.strip()))
            else:
                cleaned_components.append(component)

        # 去重并应用数量限制
        seen = set()
        filtered_emotions: list[str] = []
        for emotion in found_emotions:
            if emotion in seen:
                continue
            seen.add(emotion)
            filtered_emotions.append(emotion)
            if len(filtered_emotions) >= self.max_emotions_per_message:
                break

        emotion_images, temp_files = self._build_emotion_images_for_event(
            event,
            filtered_emotions,
        )

        return {
            "cleaned_chain": MessageChain(cleaned_components),
            "emotions": filtered_emotions,
            "images": emotion_images,
            "temp_files": temp_files,
        }

    async def compat_send_message(
        self,
        event: AstrMessageEvent,
        message: str | list | MessageChain,
        *,
        send_images: bool = True,
    ) -> dict:
        """对外兼容接口：使用本插件逻辑清理后发送消息，并可附带发送表情图片。"""
        prepared = await self.compat_prepare_message(event, message)

        return await self.compat_send_prepared_message(
            event,
            prepared,
            send_images=send_images,
        )

    async def compat_send_prepared_message(
        self,
        event: AstrMessageEvent,
        prepared: dict,
        *,
        send_text: bool = True,
        send_images: bool = True,
    ) -> dict:
        """对外兼容接口：发送由 compat_prepare_message 生成的处理结果。"""
        cleaned_chain: MessageChain = prepared.get("cleaned_chain") or MessageChain([])
        emotion_images: list[Image] = prepared.get("images") or []
        temp_files: list[str] = prepared.get("temp_files") or []

        try:
            if send_text and cleaned_chain.chain:
                await event.send(cleaned_chain)

            if send_images and emotion_images:
                for image in emotion_images:
                    await self._send_meme_image(event, image)
        finally:
            for temp_file in temp_files:
                try:
                    if os.path.exists(temp_file):
                        os.remove(temp_file)
                except Exception as e:
                    logger.error(f"[meme_manager] 清理兼容接口临时文件失败: {e}")

        return {
            "sent_text": bool(send_text and cleaned_chain.chain),
            "sent_images_count": len(emotion_images) if send_images else 0,
            "detected_emotions": prepared.get("emotions") or [],
        }

    async def _handle_upload_image_impl(self, event: AstrMessageEvent):
        user_key = f"{event.session_id}_{event.get_sender_id()}"
        upload_state = self.upload_states.get(user_key)
        if not upload_state or time.time() > upload_state["expire_time"]:
            if user_key in self.upload_states:
                del self.upload_states[user_key]
            return
        images = [c for c in event.message_obj.message if isinstance(c, Image)]
        if not images:
            yield event.plain_result("请发送图片文件来进行上传哦。")
            return
        category = upload_state["category"]
        pack_id = str(MEMES_DIR.parent.name or "").strip()
        try:
            self.semantic_task_manager.begin_external_pack_operation(
                pack_id, "接收并保存表情图片"
            )
        except RuntimeError as exc:
            yield event.plain_result(f"⚠️ {exc}")
            return
        save_dir = os.path.join(MEMES_DIR, category)
        try:
            os.makedirs(save_dir, exist_ok=True)
            saved_files = []
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            for idx, img in enumerate(images, 1):
                timestamp = int(time.time())
                try:
                    if "multimedia.nt.qq.com.cn" in img.url:
                        insecure_url = img.url.replace("https://", "http://", 1)
                        async with aiohttp.ClientSession() as session:
                            async with session.get(insecure_url) as resp:
                                content = await resp.read()
                    else:
                        async with aiohttp.ClientSession(
                            connector=aiohttp.TCPConnector(ssl=ssl_context)
                        ) as session:
                            async with session.get(img.url) as resp:
                                content = await resp.read()
                    try:
                        with PILImage.open(io.BytesIO(content)) as pil_img:
                            file_type = pil_img.format.lower()
                    except Exception:
                        file_type = "unknown"
                    ext_mapping = {
                        "jpeg": ".jpg",
                        "png": ".png",
                        "gif": ".gif",
                        "webp": ".webp",
                    }
                    ext = ext_mapping.get(file_type, ".bin")
                    filename = f"{timestamp}_{idx}{ext}"
                    save_path = os.path.join(save_dir, filename)
                    with open(save_path, "wb") as f:
                        f.write(content)
                    saved_files.append(filename)
                except Exception as e:
                    logger.error(f"下载图片失败: {str(e)}")
                    yield event.plain_result(f"文件 {img.url} 下载失败啦: {str(e)}")
                    continue
            del self.upload_states[user_key]
            result_msg = [
                Plain(
                    f"✅ 已经成功收录了 {len(saved_files)} 张新表情到「{category}」图库！"
                )
            ]
            if self.img_sync:
                result_msg.append(
                    Plain("\n☁️ 检测到已配置图床，如需同步到云端请使用命令：同步到云端")
                )
            yield event.chain_result(result_msg)
            await self.reload_emotions()
        except Exception as e:
            yield event.plain_result(f"保存失败了：{str(e)}")
        finally:
            self.semantic_task_manager.end_external_pack_operation(pack_id)

    async def _inject_meme_prompt_impl(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        self._apply_request_prompt(req, event)

    async def _resp_impl(self, event: AstrMessageEvent, response: LLMResponse):
        """处理 LLM 响应，识别表情"""

        if not response or not response.completion_text:
            return

        text = response.completion_text

        # 语义模式只接受本轮 search_memes 返回的候选 ID，不再运行旧分类猜测逻辑。
        if self._semantic_mode_active(event):
            return await self._resp_semantic_impl(event, response, text)

        pack_context = self._resolve_runtime_pack_context(event=event)
        runtime_category_mapping = (
            pack_context.get("category_mapping") or self.category_mapping
        )

        found_emotions: list[str] = []
        valid_emoticons = set(runtime_category_mapping.keys())

        clean_text = text

        # 第一阶段：严格匹配符号包裹的表情
        hex_pattern = r"&&([^&&]+)&&"
        matches = re.finditer(hex_pattern, clean_text)

        # 严格模式处理
        temp_replacements = []
        strict_emotions = []
        for match in matches:
            original = match.group(0)
            emotion = match.group(1).strip()

            # 合法性验证
            if emotion in valid_emoticons:
                temp_replacements.append((original, emotion))
                strict_emotions.append(emotion)
            else:
                temp_replacements.append((original, ""))  # 非法表情静默移除

        # 保持原始顺序替换
        for original, emotion in temp_replacements:
            clean_text = clean_text.replace(original, "", 1)  # 每次替换第一个匹配项
            if emotion:
                found_emotions.append(emotion)

        # 第二阶段：替代标记处理（如[emotion]、(emotion)等）
        if self._read_config_value(
            ("generation", "markup", "enable_alternative"),
            default=True,
            legacy_keys=("enable_alternative_markup",),
        ):
            remove_invalid_markup = self.remove_invalid_alternative_markup
            # 处理[emotion]格式
            bracket_pattern = r"\[([^\[\]]+)\]"
            matches = re.finditer(bracket_pattern, clean_text)
            bracket_replacements = []
            invalid_brackets = [] if remove_invalid_markup else None

            for match in matches:
                original = match.group(0)
                emotion = match.group(1).strip()

                if emotion in valid_emoticons:
                    bracket_replacements.append((original, emotion))
                elif remove_invalid_markup:
                    invalid_brackets.append(original)

            if remove_invalid_markup:
                for invalid in invalid_brackets:
                    clean_text = clean_text.replace(invalid, "", 1)

            for original, emotion in bracket_replacements:
                clean_text = clean_text.replace(original, "", 1)
                found_emotions.append(emotion)

            # 处理(emotion)格式
            paren_pattern = r"\(([^()]+)\)"
            matches = re.finditer(paren_pattern, clean_text)
            paren_replacements = []
            invalid_parens = [] if remove_invalid_markup else None

            for match in matches:
                original = match.group(0)
                emotion = match.group(1).strip()

                if emotion in valid_emoticons:
                    # 需要额外验证，确保不是普通句子的一部分
                    if self._is_likely_emotion_markup(
                        original, clean_text, match.start()
                    ):
                        paren_replacements.append((original, emotion))
                elif remove_invalid_markup:
                    invalid_parens.append(original)

            if remove_invalid_markup:
                for invalid in invalid_parens:
                    clean_text = clean_text.replace(invalid, "", 1)

            for original, emotion in paren_replacements:
                clean_text = clean_text.replace(original, "", 1)
                found_emotions.append(emotion)

        # 第三阶段：处理重复表情模式（如angryangryangry）
        repeated_emotions = []
        if self._read_config_value(
            ("generation", "markup", "enable_repeated_detection"),
            default=True,
            legacy_keys=("enable_repeated_emotion_detection",),
        ):
            high_confidence_emotions = self._read_config_value(
                ("generation", "matching", "high_confidence_emotions"),
                default=[],
                legacy_keys=("high_confidence_emotions",),
            )

            for emotion in valid_emoticons:
                # 跳过太短的表情词，避免误判
                if len(emotion) < 3:
                    continue

                # 对高置信度表情，重复两次即可识别
                if emotion in high_confidence_emotions:
                    # 检测重复两次的模式，如 happyhappy
                    repeat_pattern = f"({re.escape(emotion)})\\1{{1,}}"
                    matches = re.finditer(repeat_pattern, clean_text)
                    for match in matches:
                        # 跳过thinking标签内的内容
                        if self._is_position_in_thinking_tags(
                            clean_text, match.start()
                        ):
                            continue
                        original = match.group(0)
                        clean_text = clean_text.replace(original, "", 1)
                        found_emotions.append(emotion)
                        repeated_emotions.append(emotion)
                else:
                    # 普通表情词需要重复至少3次才识别
                    # 只检查长度>=4的表情，以减少误判
                    if len(emotion) >= 4:
                        # 查找表情词重复3次以上的模式
                        repeat_pattern = f"({re.escape(emotion)})\\1{{2,}}"
                        matches = re.finditer(repeat_pattern, clean_text)
                        for match in matches:
                            # 跳过thinking标签内的内容
                            if self._is_position_in_thinking_tags(
                                clean_text, match.start()
                            ):
                                continue
                            original = match.group(0)
                            clean_text = clean_text.replace(original, "", 1)
                            found_emotions.append(emotion)
                            repeated_emotions.append(emotion)

        logger.debug(f"[meme_manager] 重复检测阶段找到的表情: {repeated_emotions}")

        # 第四阶段：智能识别可能的表情（松散模式）
        loose_emotions = []
        if self._read_config_value(
            ("generation", "matching", "enable_loose_matching"),
            default=True,
            legacy_keys=("enable_loose_emotion_matching",),
        ):
            # 查找所有可能的表情词
            for emotion in valid_emoticons:
                # 使用单词边界确保不是其他单词的一部分
                pattern = r"\b(" + re.escape(emotion) + r")\b"
                for match in re.finditer(pattern, clean_text):
                    word = match.group(1)
                    position = match.start()

                    # 跳过thinking标签内的内容
                    if self._is_position_in_thinking_tags(clean_text, position):
                        continue

                    # 判断是否可能是表情而非英文单词
                    if self._is_likely_emotion(
                        word, clean_text, position, valid_emoticons
                    ):
                        # 添加到表情列表
                        found_emotions.append(word)
                        loose_emotions.append(word)
                        # 替换文本中的表情词
                        clean_text = (
                            clean_text[:position] + clean_text[position + len(word) :]
                        )

        logger.debug(f"[meme_manager] 松散匹配阶段找到的表情: {loose_emotions}")

        if self.emotion_llm_enabled:
            try:
                provider_id = self.emotion_llm_provider_id
                if not provider_id:
                    provider_id = await self.context.get_current_chat_provider_id(
                        umo=event.unified_msg_origin
                    )
                if provider_id:
                    valid_list = sorted(valid_emoticons)
                    prompt = (
                        "你是表情标签选择器，只能从给定标签中选择。\n"
                        "请基于文本语义判断需要的表情，返回JSON格式："
                        '{"emotions":["tag1","tag2"]}。\n'
                        "只输出JSON，不要解释。\n"
                        f"可用标签: {', '.join(valid_list)}\n"
                        f"文本: {clean_text}"
                    )
                    llm_resp = await self.context.llm_generate(
                        chat_provider_id=provider_id, prompt=prompt
                    )
                    if llm_resp and llm_resp.completion_text:
                        raw_text = llm_resp.completion_text.strip()
                        data = None
                        try:
                            data = json.loads(raw_text)
                        except Exception:
                            match = re.search(r"\{[\s\S]*\}", raw_text)
                            if match:
                                try:
                                    data = json.loads(match.group(0))
                                except Exception:
                                    data = None
                        if isinstance(data, dict):
                            emotions = data.get("emotions")
                            if isinstance(emotions, list):
                                for emo in emotions:
                                    if isinstance(emo, str) and emo in valid_emoticons:
                                        found_emotions.append(emo)
                            elif (
                                isinstance(emotions, str)
                                and emotions in valid_emoticons
                            ):
                                found_emotions.append(emotions)
            except Exception as e:
                logger.error(f"[meme_manager] 情感模型调用失败: {e}")

        # 去重并应用数量限制
        seen = set()
        filtered_emotions = []
        for emo in found_emotions:
            if emo not in seen:
                seen.add(emo)
                filtered_emotions.append(emo)
            if len(filtered_emotions) >= self.max_emotions_per_message:
                break

        event.set_extra("found_emotions", filtered_emotions)
        logger.info(f"[meme_manager] 去重后的最终表情列表: {filtered_emotions}")

        # 防御性清理残留符号
        clean_text = re.sub(r"&&+", "", clean_text)  # 清除未成对的&&符号
        response.completion_text = clean_text.strip()
        logger.debug(
            f"[meme_manager] 清理后的最终文本内容长度: {len(response.completion_text)}"
        )

        # webchat 流式场景：在 "complete" 入队前发送干净文本，替换客户端已显示的含标记脏文本
        result = event.get_result()
        if (
            event.get_platform_name() == "webchat"
            and result is not None
            and result.result_content_type == ResultContentType.STREAMING_RESULT
        ):
            try:
                await event.send(MessageChain([Plain(response.completion_text)]))
                logger.debug("[meme_manager] webchat 流式文本已替换为干净版本")
            except Exception as e:
                logger.error(f"[meme_manager] webchat 流式文本替换失败: {e}")

    async def _resp_semantic_impl(
        self, event: AstrMessageEvent, response: LLMResponse, text: str
    ):
        """清理语义图片标记并记录本轮经过候选校验的精确 ID。"""
        pack_context = self._resolve_runtime_pack_context(event=event)
        marker_pattern = re.compile(r"&&\s*(meme:[0-9a-fA-F]{12,64})\s*&&")
        selected_ids: list[str] = []

        def replace(match: re.Match) -> str:
            value = match.group(1).strip()
            if validate_selected_id(event, value, pack_context.get("pack_dir")):
                if value not in selected_ids:
                    selected_ids.append(value)
            else:
                logger.warning("忽略不在本轮候选中的语义图片 ID: %s", value)
            return ""

        clean_text = marker_pattern.sub(replace, text or "")
        # 不让模型写出的畸形标记泄漏到用户消息中。
        clean_text = re.sub(r"&&\s*meme:[^&]+&&", "", clean_text, flags=re.IGNORECASE)
        event.set_extra("meme_manager_semantic_selected_ids", selected_ids)
        event.set_extra("found_emotions", None)
        response.completion_text = clean_text.strip()
        result = event.get_result()
        if (
            event.get_platform_name() == "webchat"
            and result is not None
            and result.result_content_type == ResultContentType.STREAMING_RESULT
        ):
            try:
                await event.send(MessageChain([Plain(response.completion_text)]))
            except Exception as exc:
                logger.error("webchat 语义文本替换失败: %s", exc)

    async def _on_decorating_result_impl(self, event: AstrMessageEvent):
        """在消息发送前清理文本中的表情标签，并添加表情图片"""
        logger.debug("[meme_manager] on_decorating_result 开始处理")

        result = event.get_result()
        if not result:
            return

        # 流式传输兼容处理
        if result.result_content_type == ResultContentType.STREAMING_FINISH:
            if self.streaming_compatibility or event.get_platform_name() == "webchat":
                await self._send_memes_streaming(event)
            return

        try:
            # 第一步：获取并清理原始消息链中的文本
            original_chain = result.chain
            cleaned_components = []

            if original_chain:
                # 处理不同类型的消息链
                if isinstance(original_chain, str):
                    # 字符串类型：清理后转为 Plain 组件
                    cleaned = (
                        re.sub(self.content_cleanup_rule, "", original_chain)
                        if self.content_cleanup_rule
                        else original_chain
                    )
                    if cleaned.strip():
                        cleaned_components.append(Plain(cleaned.strip()))

                elif isinstance(original_chain, MessageChain):
                    # MessageChain 类型：遍历清理 Plain 组件
                    for component in original_chain.chain:
                        if isinstance(component, Plain):
                            cleaned = (
                                re.sub(self.content_cleanup_rule, "", component.text)
                                if self.content_cleanup_rule
                                else component.text
                            )
                            if cleaned.strip():
                                cleaned_components.append(Plain(cleaned.strip()))
                        else:
                            # 保留非文本组件（如已有的图片等）
                            cleaned_components.append(component)

                elif isinstance(original_chain, list):
                    # 列表类型：遍历清理 Plain 组件
                    for component in original_chain:
                        if isinstance(component, Plain):
                            cleaned = (
                                re.sub(self.content_cleanup_rule, "", component.text)
                                if self.content_cleanup_rule
                                else component.text
                            )
                            if cleaned.strip():
                                cleaned_components.append(Plain(cleaned.strip()))
                        else:
                            cleaned_components.append(component)

            # 第二步：语义模式按候选 ID 精确取图，不走概率、分类目录和 random.choice。
            semantic_selected_ids = (
                event.get_extra("meme_manager_semantic_selected_ids") or []
            )
            if semantic_selected_ids and self._semantic_mode_active(event):
                memes_root = self._get_runtime_memes_dir_for_event(event)
                pack_context = self._resolve_runtime_pack_context(event=event)
                semantic_images = []
                semantic_temp_files = []
                for selected_id in semantic_selected_ids[:1]:
                    image_path = validate_selected_id(
                        event, selected_id, pack_context.get("pack_dir")
                    )
                    if image_path is None:
                        continue
                    try:
                        final_path = self._convert_to_gif(str(image_path))
                        if final_path != str(image_path):
                            semantic_temp_files.append(final_path)
                        semantic_images.append(Image.fromFileSystem(final_path))
                    except Exception as exc:
                        logger.error("构建语义表情图片失败: %s", exc)
                if semantic_temp_files:
                    event.set_extra("meme_manager_temp_files", semantic_temp_files)
                if semantic_images:
                    # 语义模式不再随机丢弃模型已经选择的候选。
                    event.set_extra("meme_manager_pending_images", semantic_images)
            # 第三步：旧模式添加表情图片（如果有找到的表情）
            found_emotions = event.get_extra("found_emotions") or []
            if found_emotions and not semantic_selected_ids:
                memes_root = self._get_runtime_memes_dir_for_event(event)
                # 检查概率（注意：概率判断是"小于等于"才发送）
                random_value = random.randint(1, 100)
                threshold = self.emotions_probability

                if random_value <= threshold:
                    # 创建表情图片列表
                    emotion_images = []
                    temp_files = []  # 记录临时文件路径
                    for emotion in found_emotions:
                        if not emotion:
                            continue

                        emotion_path = os.path.join(memes_root, emotion)
                        path_exists = os.path.exists(emotion_path)

                        if not path_exists:
                            continue

                        memes = [
                            f
                            for f in os.listdir(emotion_path)
                            if f.endswith((".jpg", ".png", ".gif"))
                        ]

                        if not memes:
                            continue

                        meme = random.choice(memes)
                        meme_file = os.path.join(emotion_path, meme)

                        try:
                            # 转换静态图为 GIF（如果配置开启）
                            final_meme_file = self._convert_to_gif(meme_file)
                            if final_meme_file != meme_file:
                                temp_files.append(final_meme_file)
                            emotion_images.append(Image.fromFileSystem(final_meme_file))
                        except Exception as e:
                            logger.error(f"添加表情图片失败: {e}")

                    if emotion_images:
                        # 记录临时文件到 event extra
                        if temp_files:
                            existing_temp_files = (
                                event.get_extra("meme_manager_temp_files") or []
                            )
                            event.set_extra(
                                "meme_manager_temp_files",
                                existing_temp_files + temp_files,
                            )

                        use_mixed_message = False
                        if self.enable_mixed_message:
                            use_mixed_message = (
                                random.randint(1, 100) <= self.mixed_message_probability
                            )

                        if use_mixed_message and self.send_image_as_base64:
                            normalized_images = []
                            for image in emotion_images:
                                normalized_images.append(
                                    await self._ensure_image_send_format(image)
                                )
                            emotion_images = normalized_images

                        if use_mixed_message:
                            cleaned_components = self._merge_components_with_images(
                                cleaned_components, emotion_images
                            )
                        else:
                            event.set_extra(
                                "meme_manager_pending_images", emotion_images
                            )
                    else:
                        pass

            # 清空当前事件已处理的表情列表
            event.set_extra("found_emotions", None)
            event.set_extra("meme_manager_semantic_selected_ids", None)

            # 第三步：更新消息链
            if cleaned_components:
                # 直接使用组件列表，不要包装在 MessageChain 中
                result.chain = cleaned_components
            elif original_chain:
                # 如果原本有内容但清理后为空，也要更新（避免发送带标签的空消息）
                # 进行最后的防御性清理
                if isinstance(original_chain, str):
                    final_cleaned = re.sub(
                        r"&&+", "", original_chain
                    )  # 清除残留的&&符号
                    if final_cleaned.strip():
                        result.chain = [Plain(final_cleaned.strip())]
                elif isinstance(original_chain, MessageChain):
                    # 对 MessageChain 中的每个 Plain 组件进行最后清理
                    final_components = []
                    for component in original_chain.chain:
                        if isinstance(component, Plain):
                            final_cleaned = re.sub(r"&&+", "", component.text)
                            if final_cleaned.strip():
                                final_components.append(Plain(final_cleaned.strip()))
                        else:
                            final_components.append(component)
                    if final_components:
                        result.chain = final_components

            logger.debug("[meme_manager] on_decorating_result 处理完成")

        except Exception as e:
            logger.error(f"处理消息装饰失败: {str(e)}")
            logger.error(traceback.format_exc())

    @filter.after_message_sent()
    async def _after_message_sent_impl(self, event: AstrMessageEvent):
        """消息发送后处理。用于发送未混合的表情图片。"""
        pending_images = event.get_extra("meme_manager_pending_images")

        try:
            if pending_images:
                for image in pending_images:
                    await self._send_meme_image(event, image)
        except Exception as e:
            logger.error(f"发送表情图片失败: {str(e)}")
            logger.error(traceback.format_exc())
        finally:
            event.set_extra("meme_manager_pending_images", None)

            # 清理临时文件
            temp_files = event.get_extra("meme_manager_temp_files")
            if temp_files:
                for temp_file in temp_files:
                    try:
                        if os.path.exists(temp_file):
                            os.remove(temp_file)
                            logger.debug(f"[meme_manager] 已清理临时文件: {temp_file}")
                    except Exception as e:
                        logger.error(f"[meme_manager] 清理临时文件失败: {e}")
                event.set_extra("meme_manager_temp_files", None)

    # 辅助方法
    def _is_position_in_thinking_tags(self, text: str, position: int) -> bool:
        thinking_pattern = re.compile(
            r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE
        )
        for match in thinking_pattern.finditer(text):
            if match.start() <= position < match.end():
                return True
        return False

    def _is_likely_emotion_markup(self, markup, text, position):
        """判断一个标记是否可能是表情而非普通文本的一部分"""
        # 获取标记前后的文本
        before_text = text[:position].strip()
        after_text = text[position + len(markup) :].strip()

        # 如果是在中文上下文中，更可能是表情
        has_chinese_before = bool(
            re.search(r"[\u4e00-\u9fff]", before_text[-1:] if before_text else "")
        )
        has_chinese_after = bool(
            re.search(r"[\u4e00-\u9fff]", after_text[:1] if after_text else "")
        )
        if has_chinese_before or has_chinese_after:
            return True

        # 如果在数字标记中，可能是引用标记如[1]，不是表情
        if re.match(r"\[\d+\]", markup):
            return False

        # 如果标记内有空格，可能是普通句子，不是表情
        if " " in markup[1:-1]:
            return False

        # 如果标记前后是完整的英文句子，可能不是表情
        english_context_before = bool(re.search(r"[a-zA-Z]\s+$", before_text))
        english_context_after = bool(re.search(r"^\s+[a-zA-Z]", after_text))
        if english_context_before and english_context_after:
            return False

        # 默认情况下认为可能是表情
        return True

    def _is_likely_emotion(self, word, text, position, valid_emotions):
        """判断一个单词是否可能是表情而非普通英文单词"""

        # 先获取上下文
        before_text = text[:position].strip()
        after_text = text[position + len(word) :].strip()

        # 规则1：检查是否在英文上下文中
        # 如果前面有英文单词+空格，或后面有空格+英文单词，可能是英文上下文
        english_context_before = bool(re.search(r"[a-zA-Z]\s+$", before_text))
        english_context_after = bool(re.search(r"^\s+[a-zA-Z]", after_text))

        # 在英文上下文中，不太可能是表情
        if english_context_before or english_context_after:
            return False

        # 规则2：前后有中文字符，更可能是表情
        has_chinese_before = bool(
            re.search(r"[\u4e00-\u9fff]", before_text[-1:] if before_text else "")
        )
        has_chinese_after = bool(
            re.search(r"[\u4e00-\u9fff]", after_text[:1] if after_text else "")
        )

        if has_chinese_before or has_chinese_after:
            return True

        # 规则3：如果是句子开头或结尾，可能是表情
        if not before_text or before_text.endswith(
            ("。", "，", "！", "？", ".", ",", ":", ";", "!", "?", "\n")
        ):
            return True

        # 规则4：如果前后都是标点或空格，可能是表情
        if (not before_text or before_text[-1] in " \t\n.,!?;:'\"()[]{}") and (
            not after_text or after_text[0] in " \t\n.,!?;:'\"()[]{}"
        ):
            return True

        # 规则5：如果是已知的表情占比很高(>=70%)的单词，即使在英文上下文中也可能是表情
        if word in self._read_config_value(
            ("generation", "matching", "high_confidence_emotions"),
            default=[],
            legacy_keys=("high_confidence_emotions",),
        ):
            return True

        return False

    def _convert_to_gif(self, image_path: str) -> str:
        """
        将静态图片转换为 GIF 格式。
        如果图片已经是 GIF，则返回原路径。
        如果转换成功，返回临时 GIF 文件的路径。
        """
        if not self.convert_static_to_gif:
            return image_path

        if image_path.lower().endswith(".gif"):
            return image_path

        try:
            with PILImage.open(image_path) as img:
                # 检查是否已经是 GIF (虽然后缀不是 .gif，但内容可能是)
                if img.format == "GIF":
                    return image_path

                # 创建临时文件
                temp_dir = tempfile.gettempdir()
                temp_filename = os.path.join(
                    temp_dir,
                    f"meme_{int(time.time())}_{random.randint(1000, 9999)}.gif",
                )

                # 转换为 RGB (如果是 RGBA 需要处理透明度)
                if img.mode in ("RGBA", "LA") or (
                    img.mode == "P" and "transparency" in img.info
                ):
                    # 创建白色背景
                    background = PILImage.new("RGB", img.size, (255, 255, 255))
                    if img.mode == "P":
                        img = img.convert("RGBA")
                    background.paste(img, mask=img.split()[3])  # 3 is the alpha channel
                    img = background
                else:
                    img = img.convert("RGB")

                # 保存为 GIF
                img.save(temp_filename, "GIF")
                logger.debug(f"[meme_manager] 已将静态图转换为 GIF: {temp_filename}")
                return temp_filename
        except Exception as e:
            logger.error(f"[meme_manager] 转换图片为 GIF 失败: {e}")
            return image_path

    async def _send_memes_streaming(self, event: AstrMessageEvent):
        """流式传输兼容模式：在流式消息发送完成后，主动发送表情图片作为独立消息。"""
        semantic_selected_ids = (
            event.get_extra("meme_manager_semantic_selected_ids") or []
        )
        if semantic_selected_ids and self._semantic_mode_active(event):
            pack_context = self._resolve_runtime_pack_context(event=event)
            try:
                for selected_id in semantic_selected_ids[:1]:
                    image_path = validate_selected_id(
                        event, selected_id, pack_context.get("pack_dir")
                    )
                    if image_path is None:
                        continue
                    final_path = self._convert_to_gif(str(image_path))
                    try:
                        await self._send_meme_image(
                            event, Image.fromFileSystem(final_path)
                        )
                    finally:
                        if final_path != str(image_path) and os.path.exists(final_path):
                            os.remove(final_path)
            except Exception as exc:
                logger.error("流式语义表情发送失败: %s", exc, exc_info=True)
            finally:
                event.set_extra("meme_manager_semantic_selected_ids", None)
            return
        found_emotions = event.get_extra("found_emotions") or []
        if not found_emotions:
            return

        memes_root = self._get_runtime_memes_dir_for_event(event)

        try:
            random_value = random.randint(1, 100)
            if random_value > self.emotions_probability:
                return

            for emotion in found_emotions:
                if not emotion:
                    continue

                emotion_path = os.path.join(memes_root, emotion)
                if not os.path.exists(emotion_path):
                    continue

                memes = [
                    f
                    for f in os.listdir(emotion_path)
                    if f.endswith((".jpg", ".png", ".gif"))
                ]
                if not memes:
                    continue

                meme = random.choice(memes)
                meme_file = os.path.join(emotion_path, meme)
                final_meme_file = self._convert_to_gif(meme_file)

                try:
                    await self._send_meme_image(
                        event, Image.fromFileSystem(final_meme_file)
                    )
                except Exception as e:
                    logger.error(f"[meme_manager] 流式模式发送表情失败: {e}")
                finally:
                    # 清理临时文件
                    if final_meme_file != meme_file and os.path.exists(final_meme_file):
                        try:
                            os.remove(final_meme_file)
                        except Exception:
                            pass
        except Exception as e:
            logger.error(f"[meme_manager] 流式模式处理表情失败: {e}")
            logger.error(traceback.format_exc())
        finally:
            event.set_extra("found_emotions", None)

    async def _send_meme_image(self, event: AstrMessageEvent, image: Image) -> None:
        image = await self._ensure_image_send_format(image)
        if event.get_platform_name() in {"gewechat", "webchat"}:
            await event.send(MessageChain([image]))
            return
        await self.context.send_message(event.unified_msg_origin, MessageChain([image]))

    async def _ensure_image_send_format(self, image: Image) -> Image:
        """根据配置规范图片发送格式。"""
        if not self.send_image_as_base64:
            return image

        image_ref = image.file or image.url or ""
        if isinstance(image_ref, str) and image_ref.startswith("base64://"):
            return image

        try:
            base64_data = await image.convert_to_base64()
            if not base64_data:
                return image
            return Image.fromBase64(base64_data)
        except Exception as e:
            logger.error(f"[meme_manager] 转换图片为 base64 失败: {e}")
            return image

    def _merge_components_with_images(self, components, images):
        """将表情图片与文本组件智能配对，支持分段回复

        Args:
            components: 清理后的消息组件列表
            images: 表情图片列表

        Returns:
            合并后的消息组件列表，图片会合理地分布在文本中
        """
        logger.debug(
            f"[meme_manager] _merge_components_with_images 输入: 组件总数={len(components)}, 图片总数={len(images)}"
        )

        if not images:
            return components

        if not components:
            # 没有文本组件，只发送图片
            return images

        # 找到所有 Plain 组件的索引
        plain_indices = [
            i for i, comp in enumerate(components) if isinstance(comp, Plain)
        ]
        logger.debug(f"[meme_manager] Plain 组件的索引位置列表: {plain_indices}")

        if not plain_indices:
            # 没有 Plain 组件，直接添加图片到末尾
            return components + images

        # 策略：将图片均匀分布在文本组件中，优先在文本后添加图片
        # 这样在分段回复时，图片更容易和对应的文本一起发送
        merged_components = components.copy()
        images_per_text = max(
            1, len(images) // len(plain_indices)
        )  # 每个文本至少配一张图片
        image_index = 0
        images_inserted_so_far = 0  # 跟踪已插入的图片数量

        for idx, plain_idx in enumerate(plain_indices):
            if image_index >= len(images):
                break

            # 计算这个文本应该配多少张图片
            if idx == len(plain_indices) - 1:
                # 最后一个文本组件，分配所有剩余图片
                images_for_this_text = len(images) - image_index
            else:
                images_for_this_text = min(images_per_text, len(images) - image_index)

            logger.debug(
                f"[meme_manager] Plain 组件 {idx} (索引={plain_idx}) 分配的图片数量: {images_for_this_text}"
            )

            # 在这个文本组件后插入图片
            # 注意：plain_idx 是在原始 components 中的位置，但由于我们已经插入了一些图片，
            # 需要考虑已插入图片对当前位置的影响
            insert_pos = plain_idx + 1 + images_inserted_so_far

            for _ in range(images_for_this_text):
                if image_index < len(images):
                    merged_components.insert(insert_pos, images[image_index])
                    image_index += 1
                    insert_pos += 1
                    images_inserted_so_far += 1

        logger.debug(
            f"[meme_manager] 合并前组件总数: {len(components)}, 合并后组件总数: {len(merged_components)}"
        )

        return merged_components
