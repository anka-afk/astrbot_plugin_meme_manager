"""Adapt validated builtin message sends without mutating shared agent state."""

import asyncio
import copy
import random

from astrbot.api import FunctionTool
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain

from ..astrbot_compat import MessageSession


class OutgoingEvent:
    """Isolate selection state while retaining the original request's delivery gates.

    Args:
        source: Event owning the model turn and probability decision.
        session: Already validated destination session.
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
        """Read local state, delegating shared turn state to its owner.

        Args:
            key: Extra name.
            default: Value when absent.

        Returns:
            The local or inherited value.
        """
        if key in self.extras:
            return self.extras[key]
        value = self.source.get_extra(key)
        return default if value is None else value

    def set_extra(self, key, value):
        """Write selection state locally and probability decisions to the turn.

        Args:
            key: Extra name.
            value: New value.
        """
        if key == "meme_manager_send_probability_hit":
            self.source.set_extra(key, value)
        else:
            self.extras[key] = value

    def get_result(self):
        """Return isolated output so selection cannot send webchat replacements.

        Returns:
            This preparation's result, if any.
        """
        return self.result

    def get_session_id(self):
        """Return the destination's rule key.

        Returns:
            The destination session ID.
        """
        return self.session_id

    def get_message_type(self):
        """Return the destination message type.

        Returns:
            The destination's message type.
        """
        return (
            self.session.message_type
            if self.session
            else self.source.get_message_type()
        )

    def get_platform_name(self):
        """Read a destination platform override, falling back only in this session.

        Returns:
            The destination platform type, if known.
        """
        if "platform_name" in self.__dict__:
            return self.platform_name
        return self.source.get_platform_name()


class _SendContext:
    """Delegate context operations while adapting only this invocation's send.

    Args:
        original: Unmodified plugin context.
        send: Invocation-local send implementation.
    """

    def __init__(self, original, send):
        self.original = original
        self.send_message = send

    def __getattr__(self, name):
        return getattr(self.original, name)


class MemeMessageTool(FunctionTool):
    """Delegate builtin validation and adapt its final message chain.

    Args:
        original: Request's original builtin tool, including any permission proxy.
        plugin: Meme manager instance.
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
        """Run original validation and keep all mutable state invocation-local.

        Args:
            context: Agent context with the original caller identity.
            **kwargs: Original tool arguments.

        Returns:
            The builtin result, or an explicit skipped/partial-delivery result.
        """
        plugin = self.plugin
        source = context.context.event
        real_context = context.context.context
        delivery = {"calls": 0, "sent": 0, "skipped": False}

        async def send(session, chain):
            """Adapt only messages already validated by the original tool.

            Args:
                session: Resolved destination accepted by the builtin.
                chain: Validated message components.

            Returns:
                Whether all prepared messages reached a matching adapter.
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
                    # Mutate this validated chain so the builtin records clean text.
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
                        # Returning false prevents the builtin marking a blank send.
                        return False
                    for message in sends:
                        delivery["calls"] += 1
                        # Adapters may replace components while sending. Capture
                        # provenance by identity before yielding to the adapter.
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
