import json

import pytest
from astrbot_plugin_meme_manager.backend.decision import DecisionClient
from astrbot_plugin_meme_manager.backend.plugin_settings import (
    describe_settings,
    validate_settings_changes,
)
from astrbot_plugin_meme_manager.mixins.web_api import WebAPIMixin
from quart import Quart
from test_plugin_settings import settings_api as settings_api
from test_plugin_settings import shared_config as shared_config


async def settings_call(api, endpoint, payload=None):
    async with Quart(__name__).test_request_context("/", method="POST", json=payload):
        response, status = await getattr(WebAPIMixin, "_api_settings_" + endpoint)(api)
    return status, await response.get_json()


@pytest.mark.asyncio
async def test_catalog_uses_only_draft_address_without_saving(
    settings_api, monkeypatch
):
    observed = []
    settings_api.config["decision_service"]["token"] = "private-saved-secret"
    revision = describe_settings(settings_api.config)[0]["revision"]

    async def list_models(client):
        observed.append((client.base_url, client.token, client.model))
        return [{"id": "exact-id", "name": "Readable"}]

    monkeypatch.setattr(DecisionClient, "list_models", list_models)
    status, body = await settings_call(
        settings_api, "decision_models", {"base_url": "https://draft.test/v1"}
    )
    assert status == 200 and body["models"][0]["id"] == "exact-id"
    assert observed == [("https://draft.test/v1", "", "")]
    assert describe_settings(settings_api.config)[0]["revision"] == revision
    settings_api.context._star_manager.reload.assert_not_called()


@pytest.mark.parametrize("consumer", ["selection", "collection"])
def test_service_required_only_for_active_consumers(shared_config, consumer):
    snapshot, values = describe_settings(shared_config)
    changes = {"generation.emotion.llm.backend": "decision"}
    updated = validate_settings_changes(
        values, snapshot["fields"], changes, shared_config.schema
    )
    assert not updated["generation"]["emotion"]["llm"]["enabled"]
    changes = (
        {"generation.emotion.llm.enabled": True, **changes}
        if consumer == "selection"
        else {"auto_collect.enabled": True, "auto_collect.judgment_backend": "decision"}
    )
    with pytest.raises(ValueError, match="密钥"):
        validate_settings_changes(
            values, snapshot["fields"], changes, shared_config.schema
        )
    changes.update(
        {"decision_service.token": "secret", "decision_service.model": "model"}
    )
    updated = validate_settings_changes(
        values, snapshot["fields"], changes, shared_config.schema
    )
    assert (
        updated["decision_service"]["model"] == "model"
        and values["decision_service"]["token"] == ""
    )


@pytest.mark.parametrize(
    "path,value",
    [
        ("base_url", "https://user:secret@example.test/v1"),
        ("base_url", "https://example.test/v1?token=secret"),
        ("timeout", 0),
        ("max_concurrency", 65),
        ("token", "value\nheader"),
    ],
)
def test_invalid_service_config_does_not_mutate_values(shared_config, path, value):
    snapshot, values = describe_settings(shared_config)
    before = json.dumps(values, sort_keys=True)
    with pytest.raises(ValueError):
        validate_settings_changes(
            values,
            snapshot["fields"],
            {"decision_service." + path: value},
            shared_config.schema,
        )
    assert json.dumps(values, sort_keys=True) == before


@pytest.mark.asyncio
async def test_save_redacts_secrets_and_survives_provider_catalog_failure(settings_api):
    def unavailable():
        raise RuntimeError("private provider detail")

    settings_api.context.get_all_providers = unavailable
    snapshot, _ = describe_settings(settings_api.config)
    status, body = await settings_call(
        settings_api,
        "config",
        {
            "revision": snapshot["revision"],
            "changes": {
                "decision_service.token": "test-secret",
                "decision_service.model": "model",
            },
        },
    )
    assert status == 200 and body["applied"] and body["fields"]
    assert body["provider_list_errors"] == ["chat"]
    assert body["decision_service_status"]["ready"]
    secret = next(
        item for item in body["fields"] if item["path"] == "decision_service.token"
    )
    assert secret["value"] == "" and secret["configured"]
    assert "test-secret" not in json.dumps(
        body
    ) and "private provider detail" not in json.dumps(body)
