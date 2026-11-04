"""适配已校验的内置消息发送，不修改共享 Agent 状态。"""

import asyncio
import copy
import random

from astrbot.api import FunctionTool
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain

from ..astrbot_compat import MessageSession


class OutgoingEvent:
    """隔离选图状态，同时保留原始请求的投递门控。

    Args:
        source: 模型轮次及概率决策所属的事件。
        session: 已校验的目标会话。
    """

    def __init__(self, source, session):
        self.source = source
        self.session = session
        self.session_id = (
            session.session_id if session else getattr(source, "session_id", "")
        )
        self.unified_msg_origin = (
            str(session) if session else getattr(source, "unified_msg_origin", "")
        )
        self.extras = {
            "meme_manager_semantic_response_processed": False,
            "meme_manager_tag_image_prepared": False,
            "meme_manager_delivery_registry": {},
            "found_emotions": [],
            "meme_manager_semantic_selected_ids": [],
            "meme_manager_temp_files": [],
            "meme_manager_outgoing_processing": True,
        }
        self.result = None

    def __getattr__(self, name):
        return getattr(self.source, name)

    def get_extra(self, key, default=None):
        """读取本地状态，将共享轮次状态的读取委托给其所有者。

        Args:
            key: 扩展属性名称。
            default: 属性不存在时使用的默认值。

        Returns:
            本地或继承的值。
        """
        if key in self.extras:
            return self.extras[key]
        value = self.source.get_extra(key)
        return default if value is None else value

    def set_extra(self, key, value):
        """将选图状态写入本地，将概率决策写入轮次状态。

        Args:
            key: 扩展属性名称。
            value: 新值。
        """
        if key == "meme_manager_send_probability_hit":
            self.source.set_extra(key, value)
        else:
            self.extras[key] = value

    def get_result(self):
        """返回隔离的输出，避免选图时发送 webchat 文本替换。

        Returns:
            本次准备工作的结果（如有）。
        """
        return self.result

    def get_session_id(self):
        """返回目标会话的规则键。

        Returns:
            目标会话 ID。
        """
        return self.session_id

    def get_message_type(self):
        """返回目标消息类型。

        Returns:
            目标会话的消息类型。
        """
        return (
            self.session.message_type
            if self.session
            else self.source.get_message_type()
        )

    def get_platform_name(self):
        """读取目标平台覆盖值，仅在此会话内使用回退值。

        Returns:
            目标平台类型（如已知）。
        """
        if "platform_name" in self.__dict__:
            return self.platform_name
        return self.source.get_platform_name()


class _SendContext:
    """委托上下文操作，仅适配本次调用的发送行为。

    Args:
        original: 未修改的插件上下文。
        send: 本次调用独立使用的发送实现。
    """

    def __init__(self, original, send):
        self.original = original
        self.send_message = send

    def __getattr__(self, name):
        return getattr(self.original, name)


class MemeMessageTool(FunctionTool):
    """委托内置校验，并适配最终消息链。

    Args:
        original: 当前请求的原始内置工具，包含可能存在的权限代理。
        plugin: 表情包管理器实例。
    """

    def __init__(self, original, plugin):
        super().__init__(
            name=original.name,
            description=original.description,
            parameters=copy.deepcopy(original.parameters),
            active=original.active,
        )
        self.original = original
        self.plugin = plugin

    async def call(self, context, **kwargs):
        """执行原始校验，将所有可变状态隔离到本次调用。

        Args:
            context: 包含原始调用者身份的 Agent 上下文。
            **kwargs: 原始工具参数。

        Returns:
            内置工具结果，或明确的跳过、部分投递结果。
        """
        plugin = self.plugin
        source = context.context.event
        real_context = context.context.context
        delivery = {"calls": 0, "sent": 0, "skipped": False}

        async def send(session, chain):
            """仅适配原始工具已经校验的消息。

            Args:
                session: 内置工具接受的已解析目标会话。
                chain: 已校验的消息组件。

            Returns:
                所有准备好的消息是否均已送达匹配的适配器。
            """
            if not plugin._scope_allows_llm_origin(source) or not any(
                isinstance(component, Plain) for component in chain.chain
            ):
                return await real_context.send_message(session, chain)
            lock = source.get_extra("meme_manager_outgoing_lock")
            if lock is None:
                lock = asyncio.Lock()
                source.set_extra("meme_manager_outgoing_lock", lock)
            async with lock:
                target = (
                    MessageSession.from_str(session)
                    if isinstance(session, str)
                    else session
                )
                event = OutgoingEvent(source, target)
                try:
                    await plugin._resolve_outgoing_event(event)
                    processed = set(
                        source.get_extra("meme_manager_tool_message_targets") or ()
                    )
                    processed.add(str(target))
                    source.set_extra("meme_manager_tool_message_targets", processed)
                    prepared = await plugin._prepare_outgoing_message(event, chain)
                    base = prepared["cleaned_chain"].chain
                    images = prepared["images"]
                    if prepared["emotions"] or prepared["selected_ids"]:
                        targets = source.get_extra("meme_manager_tool_tag_targets")
                        targets = set(targets or ())
                        targets.add(str(target))
                        source.set_extra("meme_manager_tool_tag_targets", targets)
                    mixed = not base or (
                        plugin.enable_mixed_message
                        and random.randint(1, 100) <= plugin.mixed_message_probability
                    )
                    # 修改这条已校验的消息链，使内置工具记录干净文本。
                    chain.chain = (
                        plugin._merge_components_with_images(base, images)
                        if mixed
                        else base
                    )
                    sends = [chain] if chain.chain else []
                    if not mixed:
                        sends.extend(MessageChain([image]) for image in images)
                    if not sends:
                        delivery["skipped"] = True
                        # 返回 false，防止内置工具将空白内容标记为已发送。
                        return False
                    for message in sends:
                        delivery["calls"] += 1
                        # 适配器可能在发送过程中替换组件，因此在让出执行权前
                        # 先按对象身份记录来源信息。
                        sent_images = [
                            image
                            for image in images
                            if any(image is part for part in message.chain)
                        ]
                        if not await real_context.send_message(target, message):
                            return False
                        delivery["sent"] += 1
                        if str(target) == str(source.unified_msg_origin):
                            source._has_send_oper = True
                            text = message.get_plain_text().strip()
                            if text:
                                texts = source.get_extra(
                                    "_send_message_to_user_current_session_plain_texts"
                                )
                                texts = list(texts) if isinstance(texts, list) else []
                                if text not in texts:
                                    texts.append(text)
                                source.set_extra(
                                    "_send_message_to_user_current_session_plain_texts",
                                    texts,
                                )
                        count = (
                            source.get_extra("meme_manager_automatic_sent_count") or 0
                        )
                        source.set_extra(
                            "meme_manager_automatic_sent_count",
                            count + len(sent_images),
                        )
                        choices = set(
                            source.get_extra("meme_manager_tool_delivered_choices")
                            or ()
                        )
                        registry = (
                            event.get_extra("meme_manager_delivery_registry") or {}
                        )
                        for image in sent_images:
                            record = registry.get(id(image)) or {}
                            choices.add(
                                (
                                    str(target),
                                    record.get("candidate_id")
                                    or record.get("category"),
                                )
                            )
                        source.set_extra("meme_manager_tool_delivered_choices", choices)
                        await plugin._confirm_meme_delivery(event, sent_images)
                    return True
                finally:
                    await plugin._cleanup_outgoing_files(event)

        local = copy.copy(context)
        local.context = copy.copy(context.context)
        local.context.context = _SendContext(real_context, send)
        result = await self.original.call(local, **kwargs)
        if delivery["skipped"]:
            return "Message not sent: no visible content or eligible meme after processing."
        if delivery["sent"] and delivery["sent"] < delivery["calls"]:
            return (
                "error: Message partially sent. Some components were delivered; "
                "do not resend the original message. " + str(result)
            )
        return result
