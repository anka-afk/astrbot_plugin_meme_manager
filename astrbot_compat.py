"""尚无共享公开 API 的 AstrBot 内部依赖。

可选依赖采用延迟导入，避免增强功能不可用时阻止插件加载。
"""

from importlib import import_module
from inspect import signature

# 先初始化公开 API，否则旧核心会通过 star_tools 发生循环导入。
import astrbot.api  # noqa: F401
from astrbot.core.agent.message import ImageURLPart, Message, TextPart, ToolCall
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.utils.astrbot_path import (
    get_astrbot_plugin_data_path,
    get_astrbot_temp_path,
)
from astrbot.core.utils.session_waiter import SessionFilter

__all__ = [
    "ContextWrapper",
    "ImageURLPart",
    "Message",
    "MessageSession",
    "CommandFilter",
    "CommandGroupFilter",
    "SessionFilter",
    "TextPart",
    "ToolCall",
    "get_astrbot_plugin_data_path",
    "get_astrbot_temp_path",
    "get_star_handlers_registry",
    "get_image_chat_routing",
    "is_aiocqhttp_message_event",
    "is_builtin_message_tool",
    "resolve_effective_persona_id",
]


def get_star_handlers_registry():
    """仅在应用旧版 Mixin 兼容处理时加载注册表。

    Returns:
        AstrBot 共享的处理器注册表。
    """
    from astrbot.core.star.star_handler import star_handlers_registry

    return star_handlers_registry


def get_image_chat_routing():
    """Resolve the main agent's image routing across supported layouts.

    Returns:
        The fallback-provider resolver and image-provider selector.
    """
    for module_name in (
        "astrbot.core.astr_agent_runtime.main_agent.providers",
        "astrbot.core.astr_main_agent.providers",
    ):
        try:
            providers = import_module(module_name)
        except ModuleNotFoundError as exc:
            # A missing parent also means this module layout is unavailable.
            if exc.name and (
                exc.name == module_name or module_name.startswith(f"{exc.name}.")
            ):
                continue
            raise
        return providers.resolve_fallback_providers, providers.select_image_provider

    from astrbot.core.astr_main_agent import (
        _get_fallback_chat_providers,
        _select_image_chat_provider,
    )

    return _get_fallback_chat_providers, _select_image_chat_provider


def is_builtin_message_tool(tool):
    """Identify the built-in messaging tool across supported module layouts.

    Args:
        tool: The messaging tool attached to the request.

    Returns:
        Whether the tool comes from AstrBot's built-in messaging module.
    """
    return type(tool).__module__ in (
        "astrbot.core.astr_agent_runtime.tools.message_tools",
        "astrbot.core.tools.message_tools",
    )


async def resolve_effective_persona_id(context, event, request=None) -> str:
    """Use the host's persona selection for the target session.

    Args:
        context: AstrBot's plugin context.
        event: The request or outgoing event that identifies the target session.
        request: Current model request, if available.

    Returns:
        The resolved persona ID, or an empty string if none was selected.
    """
    resolve_persona = context.persona_manager.resolve_selected_persona
    umo = event.unified_msg_origin
    options = {"umo": umo, "platform_name": event.get_platform_name()}
    if "conversation_persona_id" in signature(resolve_persona).parameters:
        # Older AstrBot stores the persona on the conversation as well as in session config.
        if request is None:
            manager = context.conversation_manager
            cid = await manager.get_curr_conversation_id(umo)
            conversation = await manager.get_conversation(umo, cid) if cid else None
        else:
            conversation = request.conversation
        options["conversation_persona_id"] = getattr(conversation, "persona_id", None)
    result = await resolve_persona(**options)
    return result[0] or ""


def is_aiocqhttp_message_event(event) -> bool:
    """Check that an event uses the OneBot serializer for outgoing messages.

    Args:
        event: The event that will send the message.

    Returns:
        Whether the event uses AstrBot's aiocqhttp adapter.
    """
    if event.get_platform_name() != "aiocqhttp":
        return False

    try:
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
            AiocqhttpMessageEvent,
        )
    except ImportError:
        return False
    return isinstance(event, AiocqhttpMessageEvent)
