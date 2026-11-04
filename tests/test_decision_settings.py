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
    # 未使用的地址无需校验，且结果不能依赖 provider 字段的提交顺序。
    updated = validate_settings_changes(
        values,
        snapshot["fields"],
        {
            "decision_service.base_url": "unused-url",
            **CLOUDFLARE_CHANGES,
        },
        shared_config.schema,
    )
    service = updated["decision_service"]
    assert service["provider"] == "cloudflare"
    assert service["account_id"] == "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    assert service["token"] == "cf-token"
    assert service["model"] == "clef-flash"
    assert DecisionClient(service).ready


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
    api = SimpleNamespace()  # 端点只读取请求体，不触碰插件实例状态。
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


@pytest.mark.parametrize(
    "draft,token,timeout",
    [
        ({"model": "clef"}, "saved-secret", 15),
        ({"token": "typed-secret", "timeout": 300}, "typed-secret", 20),
    ],
)
@pytest.mark.asyncio
async def test_decision_test_merges_saved_and_draft(
    settings_api, monkeypatch, draft, token, timeout
):
    settings_api.config["decision_service"].update(
        provider="cloudflare",
        account_id="a" * 32,
        token="saved-secret",
        model="clef-flash",
    )
    settings_api.config.save_config()
    before = describe_settings(settings_api.config)[0]["revision"]
    observed = []

    async def evaluate(client, state, questions):
        observed.append((client.ready, client.account_id, client.token, client.timeout))
        assert state["task"] == "connectivity_check"
        assert set(questions["ping"]["criteria"]) == {"pong", "unavailable"}
        return DecisionAnswers(
            {"ping": {"type": "choice", "choice": "pong"}},
            model=client.model,
            usage={"input_tokens": 12},
        )

    monkeypatch.setattr(DecisionClient, "evaluate", evaluate)
    status, body = await settings_call(settings_api, "decision_test", draft)
    assert status == 200 and body["ok"]
    assert observed == [(True, "a" * 32, token, timeout)]
    assert body["model"] == draft.get("model", "clef-flash")
    assert body["usage"] == {"input_tokens": 12} and body["latency_ms"] >= 0
    assert "saved-secret" not in json.dumps(body) and "typed-secret" not in json.dumps(
        body
    )
    assert describe_settings(settings_api.config)[0]["revision"] == before
    settings_api.context._star_manager.reload.assert_not_called()


@pytest.mark.parametrize(
    "draft,reason",
    [
        ({"token": ""}, "missing_token"),
        ({"account_id": ""}, "missing_account_id"),
        ({"model": ""}, "missing_model"),
        ({"provider": "systemone", "base_url": ""}, "missing_base_url"),
        ({"timeout": 0}, "invalid_timeout"),
        ({"provider": "unknown"}, "invalid_provider"),
    ],
)
@pytest.mark.asyncio
async def test_decision_test_rejects_invalid_draft_without_request(
    settings_api, monkeypatch, draft, reason
):
    settings_api.config["decision_service"].update(
        provider="cloudflare",
        account_id="a" * 32,
        token="saved-secret",
        model="clef-flash",
    )
    settings_api.config.save_config()

    async def unexpected_request(client):
        pytest.fail("无效草稿不应发起收费请求")

    monkeypatch.setattr(DecisionClient, "_get_session", unexpected_request)
    status, body = await settings_call(settings_api, "decision_test", draft)
    assert status == 200 and not body["ok"] and body["reason"] == reason


@pytest.mark.asyncio
async def test_decision_test_maps_upstream_failure_without_details(
    settings_api, monkeypatch
):
    async def evaluate(client, state, questions):
        raise DecisionError("insufficient_credits")

    monkeypatch.setattr(DecisionClient, "evaluate", evaluate)
    status, body = await settings_call(
        settings_api, "decision_test", {"token": "cf-token"}
    )
    assert status == 200 and not body["ok"]
    assert body["reason"] == "insufficient_credits" and "余额" in body["message"]
    assert "cf-token" not in json.dumps(body)
