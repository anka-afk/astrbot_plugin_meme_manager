"""受支持的新旧 AstrBot 版本共用的协议约定。"""

import ast
import builtins
from pathlib import Path

import pytest
from astrbot_plugin_meme_manager import astrbot_compat
from astrbot_plugin_meme_manager.backend import manual_collect, message_tool
from astrbot_plugin_meme_manager.mixins import event_handlers

from astrbot.api import FunctionTool, ToolSet
from astrbot.api.event import MessageChain, ResultContentType
from astrbot.api.message_components import Image, Plain, Reply
from astrbot.api.platform import MessageType


def test_public_exports_preserve_runtime_type_identity():
    assert message_tool.FunctionTool is FunctionTool
    assert message_tool.Plain is Plain
    assert message_tool.MessageChain is MessageChain
    assert event_handlers.ToolSet is ToolSet
    assert event_handlers.Image is Image
    assert event_handlers.Plain is Plain
    assert event_handlers.ResultContentType is ResultContentType
    assert event_handlers.MessageType is MessageType
    assert Image(file="https://example.com/image.png").type == "Image"
    assert manual_collect.Reply is Reply


def test_session_and_context_contract():
    session = astrbot_compat.MessageSession.from_str("test:GroupMessage:123")
    assert str(session) == "test:GroupMessage:123"
    assert session.message_type is MessageType.GROUP_MESSAGE
    assert astrbot_compat.ContextWrapper(context=session).context is session
    assert astrbot_compat.get_star_handlers_registry() is not None


def test_image_routing_does_not_mask_missing_dependencies(monkeypatch):
    original_import = builtins.__import__

    def import_with_missing_dependency(name, *args, **kwargs):
        if name == "astrbot.core.astr_main_agent.providers":
            raise ModuleNotFoundError(
                "Missing routing dependency", name="routing_dependency"
            )
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_with_missing_dependency)
    with pytest.raises(ModuleNotFoundError) as error:
        astrbot_compat.get_image_chat_routing()
    assert error.value.name == "routing_dependency"


def test_internal_imports_stay_in_compat_module():
    root = Path(__file__).resolve().parents[1]
    paths = [root / "main.py", root / "config.py"]
    paths.extend((root / "backend").rglob("*.py"))
    paths.extend((root / "mixins").rglob("*.py"))
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("astrbot.core"), path
            elif isinstance(node, ast.Import):
                assert not any(
                    alias.name.startswith("astrbot.core") for alias in node.names
                ), path
