import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_meme_manager import main as plugin_main
from astrbot_plugin_meme_manager.backend.plugin_settings import (
    describe_settings,
    validate_settings_changes,
)
from astrbot_plugin_meme_manager.mixins.web_api import WebAPIMixin
from quart import Quart

from astrbot.core.config.astrbot_config import AstrBotConfig


@pytest.fixture
def shared_config(tmp_path):
    schema = json.loads(
        (Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8")
    )
    config = AstrBotConfig(str(tmp_path / "plugin_config.json"), schema=schema)
    config["storage"]["providers"]["cloudflare_r2"]["secret_access_key"] = (
        "private-fixture-secret"
    )
    config["image_host_config"] = {"webdav": {"password": "obsolete-secret"}}
    config["emotions_probability"] = 99
    config["storage"]["providers"]["webdav"]["token"] = "obsolete-token"
    config.save_config()
    return config


@pytest.fixture
def settings_api(shared_config):
    subject = SimpleNamespace(
        config=shared_config,
        context=SimpleNamespace(
            get_all_providers=lambda: [],
            get_all_embedding_providers=lambda: [],
            _star_manager=SimpleNamespace(reload=AsyncMock(return_value=(True, None))),
        ),
    )
    subject.context.get_registered_star = lambda name: SimpleNamespace(star_cls=subject)
    return subject


@pytest.fixture
def start_plugin(monkeypatch):
    for name in ("SemanticTaskManager", "CategoryManager", "AutoCollectManager"):
        monkeypatch.setattr(
            plugin_main, name, lambda *args, **kwargs: SimpleNamespace()
        )
    for name in (
        "_normalize_mixin_handler_module_paths",
        "_reload_personas",
        "_register_web_apis",
        "_ensure_img_sync_for_pack",
    ):
        monkeypatch.setattr(plugin_main.MemeSender, name, lambda self: None)
    return lambda config: plugin_main.MemeSender(SimpleNamespace(), config)


def test_startup_cleans_persisted_config_without_changing_current_values(
    shared_config, start_plugin, monkeypatch
):
    shared_config["generation"]["emotion"]["probability"] = 0
    shared_config["generation"]["message"]["streaming_compatibility"] = False
    shared_config["community"]["github_accelerator_url"] = ""
    shared_config["generation"]["trigger"]["scope"] = "all_messages"
    shared_config.save_config()
    save = Mock(wraps=shared_config.save_config)
    monkeypatch.setattr(AstrBotConfig, "save_config", lambda self: save())

    sender = start_plugin(shared_config)
    saved = json.loads(Path(shared_config.config_path).read_text(encoding="utf-8-sig"))
    assert sender.config is shared_config
    assert saved == shared_config
    assert saved.keys() == shared_config.schema.keys()
    assert "token" not in saved["storage"]["providers"]["webdav"]
    assert sender.emotions_probability == 0
    assert sender.streaming_compatibility is False
    assert saved["community"]["github_accelerator_url"] == ""
    assert saved["generation"]["trigger"]["scope"] == "only_chat_llm"
    assert (
        saved["storage"]["providers"]["cloudflare_r2"]["secret_access_key"]
        == "private-fixture-secret"
    )
    save.assert_called_once_with()
    start_plugin(shared_config)
    save.assert_called_once_with()


def test_old_only_config_uses_current_defaults_without_migrating_values(
    shared_config, start_plugin
):
    Path(shared_config.config_path).write_text(
        json.dumps(
            {
                "emotions_probability": 99,
                "semantic_enabled": True,
                "image_host": "webdav",
                "webdav_password": "obsolete-secret",
            }
        ),
        encoding="utf-8",
    )
    reloaded = AstrBotConfig(shared_config.config_path, schema=shared_config.schema)
    sender = start_plugin(reloaded)
    assert (
        sender.emotions_probability
        == reloaded.schema["generation"]["items"]["emotion"]["items"]["probability"][
            "default"
        ]
    )
    assert sender.semantic_enabled is False
    assert (
        sender._get_image_host_type()
        == reloaded.schema["storage"]["items"]["provider"]["default"]
    )
    assert sender._get_provider_config("webdav")["password"] == ""
    assert reloaded.keys() == reloaded.schema.keys()
    assert "obsolete-secret" not in Path(reloaded.config_path).read_text(
        encoding="utf-8"
    )


def test_startup_cleanup_failure_restores_config(
    shared_config, start_plugin, monkeypatch
):
    before = Path(shared_config.config_path).read_bytes()
    monkeypatch.setattr(
        AstrBotConfig, "save_config", Mock(side_effect=OSError("disk failure"))
    )
    with pytest.raises(OSError, match="disk failure"):
        start_plugin(shared_config)
    assert dict(shared_config) == json.loads(before)
    assert Path(shared_config.config_path).read_bytes() == before


@pytest.mark.parametrize("provider", ["stardots", "cloudflare_r2", "webdav"])
def test_current_provider_selection_is_not_guessed_from_credentials(
    shared_config, start_plugin, provider
):
    storage = shared_config["storage"]
    storage["provider"] = provider
    storage["providers"]["stardots"].update(key="key", secret="secret")
    storage["providers"]["cloudflare_r2"].update(
        account_id="account", access_key_id="key", bucket_name="memes"
    )
    storage["providers"]["webdav"].update(
        url="https://dav.example", username="user", password="password"
    )
    sender = start_plugin(shared_config)
    assert sender.img_sync_provider_type == provider


def test_snapshot_covers_schema_and_redacts_secrets(shared_config):
    snapshot, values = describe_settings(shared_config)
    assert "private-fixture-secret" not in json.dumps(snapshot)
    assert (
        values["storage"]["providers"]["cloudflare_r2"]["secret_access_key"]
        == "private-fixture-secret"
    )
    fields = {field["path"]: field for field in snapshot["fields"]}
    assert "settings_center" not in fields
    assert fields["storage.providers.cloudflare_r2.secret_access_key"]["configured"]
    assert fields["storage.providers.webdav.password"]["secret"]
    assert not any("legacy" in field for field in fields.values())
    assert not any(path.startswith("image_host_config.") for path in fields)
    assert "obsolete-secret" not in json.dumps(snapshot)
    pending = [("", shared_config.schema)]
    expected = set()
    while pending:
        prefix, items = pending.pop()
        for key, spec in items.items():
            path = f"{prefix}.{key}" if prefix else key
            if spec["type"] == "object":
                pending.append((path, spec["items"]))
            else:
                expected.add(path)
    assert fields.keys() == expected


def test_partial_edit_cleans_old_fields_and_preserves_current_values(shared_config):
    snapshot, values = describe_settings(shared_config)
    updated = validate_settings_changes(
        values,
        snapshot["fields"],
        {
            "semantic.min_score": 0,
            "auto_collect.daily_recognition_limit": 0,
            "generation.message.streaming_compatibility": False,
            "community.github_accelerator_url": "",
            "auto_collect.scope": ["group:123", "user:456"],
        },
        shared_config.schema,
    )
    assert updated.keys() == shared_config.schema.keys()
    assert "token" not in updated["storage"]["providers"]["webdav"]
    assert (
        updated["storage"]["providers"]["cloudflare_r2"]
        == values["storage"]["providers"]["cloudflare_r2"]
    )
    assert updated["semantic"]["min_score"] == 0
    assert updated["generation"]["message"]["streaming_compatibility"] is False
    assert updated["community"]["github_accelerator_url"] == ""
    assert values["semantic"]["min_score"] == 0.25


@pytest.mark.parametrize(
    "changes",
    [
        {"unknown.path": 1},
        {"emotions_probability": 60},
        {"image_host_config.webdav.password": "old"},
        {"storage.providers.webdav.token": "old"},
        {"generation.trigger.scope": "all_llm"},
        {"semantic.enabled": "true"},
        {"semantic.top_k": True},
        {"semantic.top_k": 0},
        {"semantic.min_score": float("nan")},
        {"semantic.min_score": 1.1},
        {"auto_collect.scope": "group:1"},
        {"auto_collect.scope": [2]},
        {"generation.emotion.probability": 101},
        {"generation.message.content_cleanup_rule": "["},
        {"storage.provider": "missing"},
        {"storage.providers.webdav.url": "file:///tmp"},
    ],
)
def test_invalid_config_is_not_written(shared_config, changes):
    before = Path(shared_config.config_path).read_bytes()
    snapshot, values = describe_settings(shared_config)
    with pytest.raises(ValueError):
        validate_settings_changes(
            values, snapshot["fields"], changes, shared_config.schema
        )
    assert Path(shared_config.config_path).read_bytes() == before


@pytest.mark.asyncio
async def test_both_editors_share_the_file_and_stale_edits_are_rejected(settings_api):
    app = Quart(__name__)
    snapshot, _ = describe_settings(settings_api.config)
    # Simulate a save from AstrBot's existing editor while this page stays open.
    settings_api.config["generation"]["emotion"]["probability"] = 70
    settings_api.config.save_config()
    async with app.test_request_context(
        "/",
        method="POST",
        json={"revision": snapshot["revision"], "changes": {"semantic.enabled": True}},
    ):
        response, status = await WebAPIMixin._api_settings_config(settings_api)
    assert status == 409
    assert "其他页面" in (await response.get_json())["message"]
    settings_api.context._star_manager.reload.assert_not_awaited()

    async with app.test_request_context("/", method="GET"):
        response, status = await WebAPIMixin._api_settings_config(settings_api)
    current = await response.get_json()
    assert status == 200
    assert (
        next(
            field["value"]
            for field in current["fields"]
            if field["path"] == "generation.emotion.probability"
        )
        == 70
    )
    async with app.test_request_context(
        "/",
        method="POST",
        json={"revision": current["revision"], "changes": {"semantic.enabled": True}},
    ):
        response, status = await WebAPIMixin._api_settings_config(settings_api)
    assert status == 200
    assert (await response.get_json())["applied"] is True
    settings_api.context._star_manager.reload.assert_awaited_once_with("meme_manager")
    # A fresh AstrBot config instance sees the settings saved by the plugin UI.
    reloaded = AstrBotConfig(
        settings_api.config.config_path, schema=settings_api.config.schema
    )
    assert next(iter(reloaded)) == "settings_center"
    assert reloaded["settings_center"] == {}
    assert (
        describe_settings(reloaded)[0]["revision"]
        == (await response.get_json())["revision"]
    )
    assert reloaded["semantic"]["enabled"] is True
    assert reloaded["generation"]["emotion"]["probability"] == 70
    assert "emotions_probability" not in reloaded
    assert "image_host_config" not in reloaded
    assert "token" not in reloaded["storage"]["providers"]["webdav"]
    assert (
        reloaded["storage"]["providers"]["cloudflare_r2"]["secret_access_key"]
        == "private-fixture-secret"
    )


@pytest.mark.asyncio
async def test_secret_clear_and_reload_failure_are_reported(settings_api):
    settings_api.context._star_manager.reload.return_value = (False, "failure")
    snapshot, _ = describe_settings(settings_api.config)
    app = Quart(__name__)
    async with app.test_request_context(
        "/",
        method="POST",
        json={
            "revision": snapshot["revision"],
            "changes": {"storage.providers.cloudflare_r2.secret_access_key": ""},
        },
    ):
        response, status = await WebAPIMixin._api_settings_config(settings_api)
    assert status == 200
    assert (await response.get_json())["applied"] is False
    assert (
        settings_api.config["storage"]["providers"]["cloudflare_r2"][
            "secret_access_key"
        ]
        == ""
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["install", "semantic", "sync"])
async def test_running_task_prevents_reload_and_config_write(settings_api, operation):
    if operation == "install":
        settings_api._community_install_tasks = [SimpleNamespace(done=lambda: False)]
    elif operation == "semantic":
        settings_api.semantic_task_manager = SimpleNamespace(
            active_pack_tasks=lambda: ["running"], _external_pack_operations={}
        )
    else:
        settings_api.img_sync = SimpleNamespace(
            sync_process=SimpleNamespace(is_alive=lambda: True)
        )
    snapshot, _ = describe_settings(settings_api.config)
    app = Quart(__name__)
    async with app.test_request_context(
        "/",
        method="POST",
        json={"revision": snapshot["revision"], "changes": {"semantic.enabled": True}},
    ):
        _, status = await WebAPIMixin._api_settings_config(settings_api)
    assert status == 409
    assert describe_settings(settings_api.config)[0]["revision"] == snapshot["revision"]
    settings_api.context._star_manager.reload.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_failure_restores_shared_config_in_memory(settings_api, monkeypatch):
    def fail_write(self, values):
        self.update(values)
        raise OSError("fixture disk failure")

    monkeypatch.setattr(AstrBotConfig, "save_config", fail_write)
    snapshot, _ = describe_settings(settings_api.config)
    app = Quart(__name__)
    async with app.test_request_context(
        "/",
        method="POST",
        json={"revision": snapshot["revision"], "changes": {"semantic.enabled": True}},
    ):
        _, status = await WebAPIMixin._api_settings_config(settings_api)
    assert status == 500
    assert settings_api.config["semantic"]["enabled"] is False
    assert describe_settings(settings_api.config)[0]["revision"] == snapshot["revision"]
    settings_api.context._star_manager.reload.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_plugin_instance_never_triggers_a_reload(settings_api):
    settings_api.context.get_registered_star = lambda name: None
    snapshot, _ = describe_settings(settings_api.config)
    app = Quart(__name__)
    async with app.test_request_context(
        "/",
        method="POST",
        json={"revision": snapshot["revision"], "changes": {"semantic.enabled": True}},
    ):
        _, status = await WebAPIMixin._api_settings_config(settings_api)
    assert status == 409
    settings_api.context._star_manager.reload.assert_not_awaited()
