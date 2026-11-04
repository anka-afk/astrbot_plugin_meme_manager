"""尚无共享公开 API 的 AstrBot 内部依赖。

可选依赖采用延迟导入，避免增强功能不可用时阻止插件加载。
"""

# 先初始化公开 API，否则旧核心会通过 star_tools 发生循环导入。
import astrbot.api  # noqa: F401
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
    "MessageSession",
    "CommandFilter",
    "CommandGroupFilter",
    "SessionFilter",
    "get_astrbot_plugin_data_path",
    "get_astrbot_temp_path",
    "get_star_handlers_registry",
    "get_image_chat_routing",
    "is_builtin_message_tool",
]


def get_star_handlers_registry():
    """仅在应用旧版 Mixin 兼容处理时加载注册表。

    Returns:
        AstrBot 共享的处理器注册表。
    """
    from astrbot.core.star.star_handler import star_handlers_registry

    return star_handlers_registry


def get_image_chat_routing():
    """兼容主 Agent 单体模块与拆分模块的图片路由。

    Returns:
        备用提供方解析器及图片提供方选择器。
    """
    try:
        from astrbot.core.astr_main_agent.providers import (
            resolve_fallback_providers,
            select_image_provider,
        )
    except ModuleNotFoundError as exc:
        # 只有拆分模块本身缺失，才表示属于受支持的旧版核心。
        # 新模块内部依赖缺失时，必须保留原始错误。
        if exc.name != "astrbot.core.astr_main_agent.providers":
            raise
        from astrbot.core.astr_main_agent import (
            _get_fallback_chat_providers,
            _select_image_chat_provider,
        )

        return _get_fallback_chat_providers, _select_image_chat_provider
    return resolve_fallback_providers, select_image_provider


def is_builtin_message_tool(tool):
    """在行为适配完成前保留当前内置工具的检查方式。

    Args:
        tool: 当前请求附带的工具。

    Returns:
        该工具是否属于当前支持的内部模块。
    """
    return type(tool).__module__ == "astrbot.core.tools.message_tools"
