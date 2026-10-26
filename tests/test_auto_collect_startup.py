"""Cover provider availability during cold startup and plugin reloads."""

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from astrbot_plugin_meme_manager.backend import auto_collect
from astrbot_plugin_meme_manager.main import MemeSender

from astrbot.core.star.context import Context


class AutoCollectStartupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(
            patch.object(auto_collect, "AUTO_COLLECT_STATE_PATH", root / "state.json")
        )
        self.logger = Mock()
        self.stack.enter_context(patch.object(auto_collect, "logger", self.logger))
        self.stack.enter_context(patch("astrbot.core.star.context.logger", self.logger))
        context = SimpleNamespace(provider_manager=SimpleNamespace(inst_map={}))
        context.get_provider_by_id = Mock(
            side_effect=lambda provider_id: Context.get_provider_by_id(
                context, provider_id
            )
        )
        self.sender = object.__new__(MemeSender)
        self.sender.context = context
        self.sender.semantic_enabled = False
        self.manager = auto_collect.AutoCollectManager(
            self.sender, {"enabled": True, "vision_provider_id": "vision"}
        )
        self.sender.auto_collect_manager = self.manager

    async def asyncTearDown(self):
        await self.manager.close()

    async def test_cold_start_defers_missing_provider_without_warning(self):
        context = self.sender.context
        # Other providers do not imply that the selected vision provider is ready.
        context.provider_manager.inst_map["other"] = SimpleNamespace()

        await self.sender.initialize()

        context.get_provider_by_id.assert_not_called()
        self.logger.warning.assert_not_called()
        self.assertFalse(self.manager._ready)
        self.assertIsNone(self.manager._worker_task)

        context.provider_manager.inst_map["vision"] = SimpleNamespace(
            provider_config={"modalities": ["image"]}
        )
        await self.sender._schedule_semantic_initial_rebuild()

        self.assertTrue(self.manager._ready)
        self.assertIsNotNone(self.manager._worker_task)
        self.logger.warning.assert_not_called()

    async def test_reload_starts_worker_without_core_loaded_event(self):
        context = self.sender.context
        context.provider_manager.inst_map["vision"] = SimpleNamespace(
            provider_config={"modalities": ["image"]}
        )

        await self.sender.initialize()

        self.assertTrue(self.manager._ready)
        self.assertIsNotNone(self.manager._worker_task)
        worker = self.manager._worker_task
        await self.sender._schedule_semantic_initial_rebuild()
        await self.sender.initialize()
        self.assertIs(self.manager._worker_task, worker)
        context.get_provider_by_id.assert_called_once_with("vision")
        self.logger.warning.assert_not_called()

    async def test_missing_provider_still_warns_after_core_loaded(self):
        await self.sender.initialize()
        self.logger.warning.assert_not_called()

        await self.sender._schedule_semantic_initial_rebuild()

        self.sender.context.get_provider_by_id.assert_called_once_with("vision")
        self.logger.warning.assert_called_once()
        self.assertIn(
            "Provider vision was not found", self.logger.warning.call_args.args[0]
        )
        self.assertFalse(self.manager._ready)
        self.assertIsNone(self.manager._worker_task)

    async def test_reload_keeps_image_modality_validation(self):
        self.sender.context.provider_manager.inst_map["vision"] = SimpleNamespace(
            provider_config={"modalities": ["text"]}
        )

        await self.sender.initialize()

        self.sender.context.get_provider_by_id.assert_called_once_with("vision")
        self.logger.warning.assert_called_once()
        self.assertFalse(self.manager._ready)
        self.assertIsNone(self.manager._worker_task)

    async def test_inactive_configuration_does_not_resolve_providers(self):
        for enabled, provider_id in ((False, "vision"), (True, "")):
            with self.subTest(enabled=enabled, provider_id=provider_id):
                self.manager.enabled = enabled
                self.manager.vision_provider_id = provider_id
                self.logger.reset_mock()

                await self.sender.initialize()

                self.sender.context.get_provider_by_id.assert_not_called()
                self.assertEqual(self.logger.warning.call_count, int(enabled))
                self.assertFalse(self.manager._ready)
                self.assertIsNone(self.manager._worker_task)
