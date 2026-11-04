import json
from types import SimpleNamespace

import pytest
from astrbot_plugin_meme_manager.backend.decision import (
    DecisionAnswers,
    DecisionClient,
    DecisionError,
)
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


CLOUDFLARE_CHANGES = {
    "generation.emotion.llm.enabled": True,
    "generation.emotion.llm.backend": "decision",
    "decision_service.provider": "cloudflare",
    "decision_service.account_id": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
    "decision_service.token": "cf-token",
    "decision_service.model": "clef-flash",
}


def test_cloudflare_provider_accepts_clef_without_base_url(shared_config):
    snapshot, values = describe_settings(shared_config)
    updated = validate_settings_changes(
        values, snapshot["fields"], CLOUDFLARE_CHANGES, shared_config.schema
    )
    service = updated["decision_service"]
    assert service["provider"] == "cloudflare"
    assert service["account_id"] == "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    assert service["token"] == "cf-token"
    assert service["model"] == "clef-flash"
    # Cloudflare ignores the unused System One URL, including an empty value.
    updated = validate_settings_changes(
        values,
        snapshot["fields"],
        {**CLOUDFLARE_CHANGES, "decision_service.base_url": ""},
        shared_config.schema,
    )
    assert not updated["decision_service"]["base_url"]


@pytest.mark.parametrize(
    "override,message",
    [
        ({"decision_service.account_id": ""}, "账户 ID"),
        ({"decision_service.account_id": "xyz"}, "32 位"),
        ({"decision_service.model": "gpt-4"}, "clef"),
    ],
)
def test_cloudflare_provider_rejects_invalid_service_config(
    shared_config, override, message
):
    snapshot, values = describe_settings(shared_config)
    before = json.dumps(values, sort_keys=True)
    with pytest.raises(ValueError, match=message):
        validate_settings_changes(
            values,
            snapshot["fields"],
            {**CLOUDFLARE_CHANGES, **override},
            shared_config.schema,
        )
    assert json.dumps(values, sort_keys=True) == before


@pytest.mark.asyncio
async def test_decision_models_endpoint_returns_cloudflare_catalog():
    api = SimpleNamespace()  # Catalog discovery does not access plugin state.
    status, body = await settings_call(
        api, "decision_models", {"provider": "cloudflare"}
    )
    assert status == 200
    assert [model["id"] for model in body["models"]] == [
        "clef-flash",
        "clef",
        "clef-omni",
    ]


@pytest.mark.asyncio
async def test_decision_models_endpoint_still_requires_url_for_systemone():
    api = SimpleNamespace()
    status, body = await settings_call(api, "decision_models", {"base_url": " "})
    assert status == 400 and "基础地址" in body["message"]


@pytest.mark.asyncio
async def test_decision_test_uses_draft_values_and_saved_key(settings_api, monkeypatch):
    observed = []

    async def evaluate(client, state, questions):
        observed.append(
            (
                client.provider,
                client.account_id,
                client.model,
                client.token,
                client.timeout,
                state["task"],
                questions["ping"]["type"],
                sorted(questions["ping"]["criteria"]),
            )
        )
        return DecisionAnswers(
            {"ping": {"type": "choice", "choice": "pong", "confidence": 0.91}},
            model="clef-flash",
            usage={"input_tokens": 12},
            credits=None,
        )

    monkeypatch.setattr(DecisionClient, "evaluate", evaluate)
    settings_api.config["decision_service"].update(
        {
            "provider": "cloudflare",
            "account_id": "a" * 32,
            "token": "saved-secret",
            "model": "clef-flash",
        }
    )
    settings_api.config.save_config()
    before = describe_settings(settings_api.config)[0]["revision"]
    status, body = await settings_call(
        settings_api,
        "decision_test",
        {"provider": "cloudflare", "model": "clef-flash"},
    )
    assert status == 200 and body["ok"] is True
    assert observed == [
        (
            "cloudflare",
            "a" * 32,
            "clef-flash",
            "saved-secret",
            15.0,
            "connectivity_check",
            "choice",
            ["pong", "unavailable"],
        )
    ]
    assert "clef-flash" in body["message"] and "ms" in body["message"]
    assert body["choice"] == "pong" and body["confidence"] == 0.91
    assert body["latency_ms"] >= 0
    assert "saved-secret" not in json.dumps(body)
    # The probe must not persist its draft or reload the plugin.
    assert describe_settings(settings_api.config)[0]["revision"] == before
    assert settings_api.config["decision_service"]["token"] == "saved-secret"
    settings_api.context._star_manager.reload.assert_not_called()


@pytest.mark.asyncio
async def test_decision_test_prefers_typed_key_and_caps_timeout(
    settings_api, monkeypatch
):
    observed = []

    async def evaluate(client, state, questions):
        observed.append((client.token, client.timeout, client.max_concurrency))
        return DecisionAnswers({"ping": {"type": "choice", "choice": "pong"}})

    monkeypatch.setattr(DecisionClient, "evaluate", evaluate)
    settings_api.config["decision_service"].update({"token": "saved-secret"})
    settings_api.config["decision_service"]["timeout"] = 300
    settings_api.config.save_config()
    status, body = await settings_call(
        settings_api,
        "decision_test",
        {
            "provider": "systemone",
            "base_url": "https://service.test/v1",
            "model": "some-model",
            "token": "typed-key",
        },
    )
    assert status == 200 and body["ok"] is True
    assert observed == [("typed-key", 20.0, 1)]
    assert "typed-key" not in json.dumps(body)


@pytest.mark.asyncio
async def test_decision_test_does_not_fall_back_when_key_cleared(settings_api):
    settings_api.config["decision_service"]["token"] = "saved-secret"
    settings_api.config.save_config()
    status, body = await settings_call(
        settings_api,
        "decision_test",
        {
            "provider": "systemone",
            "base_url": "https://service.test/v1",
            "model": "some-model",
            "token": "",
        },
    )
    # An explicit empty key must fail before any billable request starts.
    assert status == 200 and body["ok"] is False
    assert body["reason"] == "missing_token" and "密钥" in body["message"]


@pytest.mark.asyncio
async def test_decision_test_maps_upstream_failure_without_details(
    settings_api, monkeypatch
):
    async def evaluate(client, state, questions):
        raise DecisionError("insufficient_credits")

    monkeypatch.setattr(DecisionClient, "evaluate", evaluate)
    status, body = await settings_call(
        settings_api,
        "decision_test",
        {
            "provider": "cloudflare",
            "account_id": "b" * 32,
            "model": "clef-flash",
            "token": "cf-token",
        },
    )
    assert status == 200 and body["ok"] is False
    assert body["reason"] == "insufficient_credits"
    assert "余额" in body["message"]
    assert "cf-token" not in json.dumps(body)


@pytest.mark.parametrize(
    "provider,field,reason",
    [
        ("cloudflare", "account_id", "missing_account_id"),
        ("cloudflare", "model", "missing_model"),
        ("cloudflare", "token", "missing_token"),
        ("systemone", "base_url", "missing_base_url"),
        ("systemone", "model", "missing_model"),
    ],
)
@pytest.mark.asyncio
async def test_decision_test_preserves_explicit_empty_drafts(
    settings_api, monkeypatch, provider, field, reason
):
    settings_api.config["decision_service"].update(
        provider=provider,
        account_id="a" * 32,
        base_url="https://service.test/v1",
        token="saved-secret",
        model="clef-flash" if provider == "cloudflare" else "some-model",
    )
    settings_api.config.save_config()

    async def unexpected_request(client):
        pytest.fail("Invalid drafts must not start a network request")

    monkeypatch.setattr(DecisionClient, "_get_session", unexpected_request)
    status, body = await settings_call(settings_api, "decision_test", {field: ""})
    assert status == 200 and body["ok"] is False
    assert body["reason"] == reason
    assert settings_api.config["decision_service"][field]


@pytest.mark.parametrize("value", [None, False, 123, {}, []])
@pytest.mark.asyncio
async def test_decision_test_rejects_non_text_fields(settings_api, value):
    status, body = await settings_call(
        settings_api, "decision_test", {"token": value}
    )
    assert status == 400 and body["ok"] is False


@pytest.mark.parametrize("timeout,expected", [(3, 3.0), (60, 20.0)])
@pytest.mark.asyncio
async def test_decision_test_uses_draft_timeout(
    settings_api, monkeypatch, timeout, expected
):
    observed = []

    async def evaluate(client, state, questions):
        observed.append(client.timeout)
        return DecisionAnswers({"ping": {"type": "choice", "choice": "pong"}})

    monkeypatch.setattr(DecisionClient, "evaluate", evaluate)
    status, body = await settings_call(
        settings_api, "decision_test", {"timeout": timeout}
    )
    assert status == 200 and body["ok"] is True
    assert observed == [expected]


@pytest.mark.parametrize("timeout", [0, -1, True, None, "invalid", 301])
@pytest.mark.asyncio
async def test_decision_test_does_not_mask_invalid_timeouts(settings_api, timeout):
    status, body = await settings_call(
        settings_api,
        "decision_test",
        {
            "base_url": "https://service.test/v1",
            "token": "draft-secret",
            "model": "some-model",
            "timeout": timeout,
        },
    )
    assert status == 200 and body["ok"] is False
    assert body["reason"] == "invalid_timeout"


@pytest.mark.parametrize("provider", ["unknown", None, 123])
@pytest.mark.asyncio
async def test_decision_models_rejects_invalid_providers(provider):
    status, _ = await settings_call(
        SimpleNamespace(), "decision_models", {"provider": provider}
    )
    assert status == 400
