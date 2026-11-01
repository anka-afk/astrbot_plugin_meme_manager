import json
from types import SimpleNamespace

import pytest
from astrbot_plugin_meme_manager.backend.plugin_settings import (
    describe_settings,
    validate_settings_changes,
)
from astrbot_plugin_meme_manager.mixins.web_api import WebAPIMixin
from quart import Quart
from test_plugin_settings import settings_api as settings_api
from test_plugin_settings import shared_config as shared_config
from test_plugin_settings import start_plugin as start_plugin


@pytest.mark.asyncio
async def test_catalog_uses_draft_address_without_saving_or_forwarding_credentials(
    settings_api, monkeypatch
):
    from astrbot_plugin_meme_manager.backend.decision import DecisionClient

    observed = {}
    settings_api.config["decision_service"]["token"] = "private-saved-credential"
    before = describe_settings(settings_api.config)[0]["revision"]

    async def list_models(client):
        observed.update(
            base_url=client.base_url, token=client.token, model=client.model
        )
        return [{"id": "catalog-id", "name": "Catalog Name"}]

    monkeypatch.setattr(DecisionClient, "list_models", list_models)
    app = Quart(__name__)
    async with app.test_request_context(
        "/", method="POST", json={"base_url": "https://draft.example.test/v1"}
    ):
        response, status = await WebAPIMixin._api_settings_decision_models(settings_api)
    body = await response.get_json()
    assert status == 200
    assert body == {"models": [{"id": "catalog-id", "name": "Catalog Name"}]}
    assert observed == {
        "base_url": "https://draft.example.test/v1",
        "token": "",
        "model": "",
    }
    assert describe_settings(settings_api.config)[0]["revision"] == before
    settings_api.context._star_manager.reload.assert_not_called()
    assert "private-saved-credential" not in json.dumps(body)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, [], {}, {"base_url": ""}, {"base_url": 123}])
async def test_catalog_rejects_invalid_input_without_network(settings_api, payload):
    app = Quart(__name__)
    async with app.test_request_context("/", method="POST", json=payload):
        response, status = await WebAPIMixin._api_settings_decision_models(settings_api)
    assert status == 400


@pytest.mark.asyncio
async def test_catalog_error_does_not_return_upstream_details(
    settings_api, monkeypatch
):
    from astrbot_plugin_meme_manager.backend.decision import (
        DecisionClient,
        DecisionError,
    )

    async def list_models(client):
        raise DecisionError("not_found")

    monkeypatch.setattr(DecisionClient, "list_models", list_models)
    app = Quart(__name__)
    async with app.test_request_context(
        "/", method="POST", json={"base_url": "https://example.test/v1"}
    ):
        response, status = await WebAPIMixin._api_settings_decision_models(settings_api)
    assert status == 502
    assert "可直接填写模型 ID" in (await response.get_json())["message"]


@pytest.mark.asyncio
async def test_provider_list_failure_keeps_settings_editable(settings_api):
    def unavailable():
        raise RuntimeError("private provider detail")

    settings_api.context.get_all_providers = unavailable
    app = Quart(__name__)
    async with app.test_request_context("/", method="GET"):
        response, status = await WebAPIMixin._api_settings_config(settings_api)
    body = await response.get_json()
    assert status == 200 and body["fields"]
    assert body["provider_list_errors"] == ["chat"]
    assert "private provider detail" not in json.dumps(body)


def test_unused_decision_backend_retains_draft_configuration(shared_config):
    snapshot, values = describe_settings(shared_config)
    updated = validate_settings_changes(
        values,
        snapshot["fields"],
        {"generation.emotion.llm.backend": "decision"},
        shared_config.schema,
    )
    assert updated["generation"]["emotion"]["llm"]["enabled"] is False
    assert updated["generation"]["emotion"]["llm"]["backend"] == "decision"
    assert updated["decision_service"]["token"] == ""


@pytest.mark.parametrize("consumer", ["selection", "collection"])
def test_enabling_a_consumer_requires_valid_service_configuration(
    shared_config, consumer
):
    snapshot, values = describe_settings(shared_config)
    changes = (
        {
            "generation.emotion.llm.enabled": True,
            "generation.emotion.llm.backend": "decision",
        }
        if consumer == "selection"
        else {"auto_collect.enabled": True, "auto_collect.judgment_backend": "decision"}
    )
    with pytest.raises(ValueError, match="密钥"):
        validate_settings_changes(
            values, snapshot["fields"], changes, shared_config.schema
        )
    changes.update(
        {
            "decision_service.token": "test-only-credential",
            "decision_service.model": "jev",
        }
    )
    updated = validate_settings_changes(
        values, snapshot["fields"], changes, shared_config.schema
    )
    assert updated["decision_service"]["model"] == "jev"
    assert values["decision_service"]["token"] == ""


@pytest.mark.parametrize(
    "changes",
    [
        {"decision_service.base_url": "https://user:password@example.test/v1"},
        {"decision_service.base_url": "https://example.test/v1?token=private"},
        {"decision_service.base_url": "https://example.test:bad/v1"},
        {"decision_service.timeout": 0},
        {"decision_service.timeout": 301},
        {"decision_service.max_concurrency": 65},
        {"decision_service.model": "x" * 129},
        {"decision_service.token": "value\nheader"},
    ],
)
def test_invalid_service_values_are_rejected_before_writing(shared_config, changes):
    snapshot, values = describe_settings(shared_config)
    before = json.dumps(values, sort_keys=True)
    with pytest.raises(ValueError):
        validate_settings_changes(
            values, snapshot["fields"], changes, shared_config.schema
        )
    assert json.dumps(values, sort_keys=True) == before


def test_native_configuration_wires_independent_model_roles(
    shared_config, start_plugin
):
    shared_config["generation"]["emotion"]["llm"].update(
        enabled=True, backend="decision", context_turns=20
    )
    shared_config["semantic"]["query"].update(
        source="reply_text", provider_id="query-chat"
    )
    shared_config["decision_service"].update(token="test-only-credential", model="jev")
    sender = start_plugin(shared_config)
    assert sender.selection_backend == "decision"
    assert sender.semantic_query_source == "reply_text"
    assert sender.semantic_query_provider_id == "query-chat"
    assert sender.emotion_llm_context_turns == 20
    assert sender.decision_client.ready
    assert sender.decision_client._session is None


@pytest.mark.asyncio
async def test_save_returns_redacted_service_status_and_provider_choices(settings_api):
    provider = SimpleNamespace(
        meta=lambda: SimpleNamespace(id="chat-id", model="chat-model")
    )
    settings_api.context.get_all_providers = lambda: [provider]
    snapshot, _ = describe_settings(settings_api.config)
    app = Quart(__name__)
    async with app.test_request_context(
        "/",
        method="POST",
        json={
            "revision": snapshot["revision"],
            "changes": {
                "decision_service.token": "test-only-credential",
                "decision_service.model": "jev",
            },
        },
    ):
        response, status = await WebAPIMixin._api_settings_config(settings_api)
    body = await response.get_json()
    assert status == 200 and body["applied"]
    assert body["providers"]["chat"] == [{"id": "chat-id", "model": "chat-model"}]
    assert body["decision_service_status"]["ready"] is True
    secret = next(
        item for item in body["fields"] if item["path"] == "decision_service.token"
    )
    assert secret["value"] == "" and secret["configured"] is True
    assert "test-only-credential" not in json.dumps(body)
