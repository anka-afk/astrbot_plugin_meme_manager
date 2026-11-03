"""Internal AstrBot dependencies without a shared public API.

Keep optional imports lazy so unavailable enhancements cannot block loading.
"""

# Initialize the public API first; old cores otherwise cycle through star_tools.
import astrbot.api  # noqa: F401
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.utils.astrbot_path import (
    get_astrbot_plugin_data_path,
    get_astrbot_temp_path,
)

__all__ = [
    "ContextWrapper",
    "MessageSession",
    "CommandFilter",
    "CommandGroupFilter",
    "get_astrbot_plugin_data_path",
    "get_astrbot_temp_path",
    "get_star_handlers_registry",
    "get_image_chat_routing",
    "is_builtin_message_tool",
]


def get_star_handlers_registry():
    """Load the registry only when applying the legacy Mixin workaround.

    Returns:
        AstrBot's shared handler registry.
    """
    from astrbot.core.star.star_handler import star_handlers_registry

    return star_handlers_registry


def get_image_chat_routing():
    """Load private image routing functions when the feature is needed.

    Returns:
        Fallback provider resolver and image provider selector.
    """
    from astrbot.core.astr_main_agent import (
        _get_fallback_chat_providers,
        _select_image_chat_provider,
    )

    return _get_fallback_chat_providers, _select_image_chat_provider


def is_builtin_message_tool(tool):
    """Preserve the current builtin tool check pending behavioral adaptation.

    Args:
        tool: Tool attached to the current request.

    Returns:
        Whether the tool matches the currently supported internal module.
    """
    return type(tool).__module__ == "astrbot.core.tools.message_tools"
