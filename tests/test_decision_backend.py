import asyncio
import json
import uuid

import pytest
from aiohttp import web
from backend.decision import MAX_RESPONSE_BYTES, DecisionClient, DecisionError


class LocalService:
    def __init__(self, handler):
        self.handler = handler
        self.runner = None
        self.site = None
        self.url = ""

    async def __aenter__(self):
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self.handler)
        self.runner = web.AppRunner(app, shutdown_timeout=0.1)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        await self.runner.cleanup()


def client_config(service, **overrides):
    config = {
        "base_url": service.url + "/v1",
        "token": "test-token",
        "model": "test-model",
        "timeout": 2,
        "max_concurrency": 2,
    }
    config.update(overrides)
    return config


def questions():
    return {
        "selection": {
            "type": "choice",
            "instructions": "Select a suitable image or abstain.",
            "criteria": {"c0": "comfort", "c1": "celebrate", "skip": "no image"},
        }
    }


def response_payload(choice="c0", **answer):
    return {"answers": {"selection": {"type": "choice", "choice": choice, **answer}}}


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["/v1", "/v1/systemone/"])
async def test_public_catalog_preserves_ids_without_credentials_or_inference(suffix):
    observed = []

    async def handler(request):
        observed.append(
            (request.method, request.path, request.headers.get("Authorization"))
        )
        return web.json_response(
            {
                "models": [
                    {"name": "exact-model-id", "displayName": "Readable Model"},
                    {"name": "exact-model-id", "displayName": "Duplicate"},
                    {"name": "another"},
                    {"name": "extraction-only", "capabilities": {"questions": []}},
                    {"name": "bad\nmodel"},
                    {"name": ""},
                    "invalid",
                ]
            }
        )

    async with LocalService(handler) as service:
        client = DecisionClient(
            client_config(service, base_url=service.url + suffix, model="")
        )
        try:
            assert await client.list_models() == [
                {"id": "exact-model-id", "name": "Readable Model"},
                {"id": "another", "name": "another"},
            ]
            assert observed == [("GET", "/v1/models", None)]
            assert client.request_counts == {}
            session = client._session
        finally:
            await client.close()
        assert session.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload", [{"data": []}, {"models": None}, {"models": [{}] * 1001}, []]
)
async def test_catalog_rejects_invalid_shape(payload):
    async def handler(request):
        return web.json_response(payload)

    async with LocalService(handler) as service:
        client = DecisionClient(client_config(service))
        try:
            with pytest.raises(DecisionError, match="invalid_catalog"):
                await client.list_models()
        finally:
            await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, code", [(404, "not_found"), (302, "http_error"), (429, "rate_limited")]
)
async def test_catalog_errors_are_sanitized_and_redirects_are_not_followed(
    status, code
):
    paths = []

    async def handler(request):
        paths.append(request.path)
        return web.Response(
            status=status,
            headers={"Location": "/private"},
            text="private upstream details",
        )

    async with LocalService(handler) as service:
        client = DecisionClient(client_config(service))
        try:
            with pytest.raises(DecisionError) as error:
                await client.list_models()
            assert error.value.code == code
            assert "private" not in str(error.value)
            assert paths == ["/v1/models"]
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_catalog_timeout_and_invalid_url():
    async def handler(request):
        await asyncio.sleep(0.1)
        return web.json_response({"models": []})

    async with LocalService(handler) as service:
        client = DecisionClient(
            client_config(service, timeout=0.01, token="", model="")
        )
        try:
            with pytest.raises(DecisionError, match="timeout"):
                await client.list_models()
        finally:
            await client.close()
    client = DecisionClient({"base_url": "https://user:secret@example.test/v1"})
    with pytest.raises(DecisionError, match="invalid_base_url"):
        await client.list_models()
    assert client._session is None


@pytest.mark.asyncio
async def test_evaluate_lazy_session_unicode_idempotency_and_close():
    observed = {}

    async def handler(request):
        observed["path"] = request.path
        observed["authorization"] = request.headers.get("Authorization")
        observed["idempotency"] = request.headers.get("Idempotency-Key")
        observed["body"] = await request.json()
        return web.json_response(response_payload(probability=0.75))

    async with LocalService(handler) as service:
        client = DecisionClient(client_config(service))
        assert client.ready
        assert client.status() == {
            "ready": True,
            "reason": "ready",
            "model": "test-model",
            "backend": "systemone",
            "requests_started": {},
        }
        assert client._session is None
        answers = await client.evaluate({"text": "安慰对方"}, questions())
        assert answers["selection"]["choice"] == "c0"
        assert observed["path"] == "/v1/systemone"
        assert observed["authorization"] == "Bearer test-token"
        assert uuid.UUID(observed["idempotency"])
        assert observed["body"]["state"] == {"text": "安慰对方"}
        assert observed["body"]["questions"] == questions()
        assert "options" not in observed["body"]["questions"]["selection"]
        assert client.status()["requests_started"] == {"other": 1}
        session = client._session
        await client.close()
        assert session.closed
        await client.close()
        assert not client.ready and client.status()["reason"] == "closed"
        with pytest.raises(DecisionError) as error:
            await client.evaluate({}, questions())
        assert error.value.code == "closed"


@pytest.mark.asyncio
async def test_request_counter_excludes_local_validation_and_separates_consumers():
    async def handler(request):
        return web.json_response(response_payload())

    async with LocalService(handler) as service:
        client = DecisionClient(client_config(service))
        try:
            with pytest.raises(DecisionError):
                await client.evaluate({"task": "auto_collect_judgment"}, {})
            assert client.request_counts == {}
            for task in ("auto_collect_judgment", "meme_selection"):
                await client.evaluate({"task": task}, questions())
            assert client.request_counts == {
                "auto_collect_judgment": 1,
                "meme_selection": 1,
            }
            await client.evaluate({"task": []}, questions())
            assert client.request_counts["other"] == 1
        finally:
            await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("http_status", "code"),
    [
        (400, "bad_request"),
        (401, "unauthorized"),
        (403, "forbidden"),
        (404, "not_found"),
        (408, "request_timeout"),
        (429, "rate_limited"),
        (500, "server_error"),
        (503, "server_error"),
    ],
)
async def test_http_errors_are_classified_without_retries(http_status, code):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return web.Response(status=http_status, text="secret upstream detail")

    async with LocalService(handler) as service:
        client = DecisionClient(client_config(service))
        with pytest.raises(DecisionError) as error:
            await client.evaluate({}, questions())
        assert error.value.code == code
        assert "secret" not in str(error.value)
        assert calls == 1
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("upstream_code", "expected"),
    [
        ("idempotency_payload_mismatch", "idempotency_conflict"),
        ("request_in_progress", "request_in_progress"),
        ("idempotency_failed", "idempotency_failed"),
        ("other_conflict", "conflict"),
    ],
)
async def test_conflict_types_are_distinct(upstream_code, expected):
    async def handler(request):
        return web.json_response(
            {"error": {"code": upstream_code, "message": "private detail"}},
            status=409,
        )

    async with LocalService(handler) as service:
        client = DecisionClient(client_config(service))
        with pytest.raises(DecisionError) as error:
            await client.evaluate({}, questions())
        assert error.value.code == expected
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "code"),
    [
        ("not-json", "invalid_response"),
        (json.dumps({"answers": {}}), "invalid_response"),
        (
            json.dumps(
                {
                    "answers": {
                        "selection": {"type": "choice", "choice": "c0"},
                        "unknown": {"type": "choice", "choice": "c0"},
                    }
                }
            ),
            "invalid_response",
        ),
        (json.dumps(response_payload("missing")), "invalid_response"),
        (
            json.dumps({"answers": {"selection": {"type": "choice"}}}),
            "invalid_response",
        ),
        (
            json.dumps(response_payload()).replace(
                '"choice": "c0"', '"choice": "c0", "probability": NaN'
            ),
            "invalid_probability",
        ),
        (
            json.dumps(response_payload()).replace(
                '"choice": "c0"', '"choice": "c0", "confidence": Infinity'
            ),
            "invalid_probability",
        ),
        (json.dumps(response_payload(probability=1.1)), "invalid_probability"),
        (json.dumps(response_payload(confidence=True)), "invalid_probability"),
        (
            json.dumps(response_payload(probabilities={"unknown": 0.5})),
            "invalid_response",
        ),
    ],
)
async def test_malformed_answers_and_non_finite_probabilities_are_rejected(body, code):
    async def handler(request):
        return web.Response(text=body, content_type="application/json")

    async with LocalService(handler) as service:
        client = DecisionClient(client_config(service))
        with pytest.raises(DecisionError) as error:
            await client.evaluate({}, questions())
        assert error.value.code == code
        await client.close()


@pytest.mark.asyncio
async def test_response_and_request_sizes_are_bounded():
    async def large_response(request):
        return web.Response(body=b" " * (MAX_RESPONSE_BYTES + 1))

    async with LocalService(large_response) as service:
        client = DecisionClient(client_config(service))
        with pytest.raises(DecisionError) as error:
            await client.evaluate({}, questions())
        assert error.value.code == "response_too_large"
        with pytest.raises(DecisionError) as error:
            await client.evaluate({"text": "表" * 30_000}, questions())
        assert error.value.code == "request_too_large"
        await client.close()


@pytest.mark.asyncio
async def test_redirect_is_not_followed_and_authorization_is_not_forwarded():
    calls = []

    async def handler(request):
        calls.append((request.path, request.headers.get("Authorization")))
        if request.path == "/v1/systemone":
            return web.Response(status=307, headers={"Location": "/capture"})
        return web.json_response(response_payload())

    async with LocalService(handler) as service:
        client = DecisionClient(client_config(service))
        with pytest.raises(DecisionError) as error:
            await client.evaluate({}, questions())
        assert error.value.code == "http_error"
        assert calls == [("/v1/systemone", "Bearer test-token")]
        await client.close()


@pytest.mark.asyncio
async def test_queue_waiting_consumes_total_timeout_budget():
    entered = asyncio.Event()

    async def handler(request):
        entered.set()
        return web.json_response(response_payload())

    async with LocalService(handler) as service:
        client = DecisionClient(client_config(service, timeout=0.05, max_concurrency=1))
        await client._semaphore.acquire()
        with pytest.raises(DecisionError) as error:
            await client.evaluate({}, questions())
        client._semaphore.release()
        assert error.value.code == "timeout"
        assert not entered.is_set()
        await client.close()


@pytest.mark.asyncio
async def test_cancellation_propagates():
    started = asyncio.Event()

    async def handler(request):
        started.set()
        await asyncio.Event().wait()

    async with LocalService(handler) as service:
        client = DecisionClient(client_config(service))
        task = asyncio.create_task(client.evaluate({}, questions()))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await client.close()


@pytest.mark.asyncio
async def test_concurrency_is_bounded_and_sessions_are_reused():
    active = 0
    maximum = 0
    calls = 0

    async def handler(request):
        nonlocal active, maximum, calls
        active += 1
        maximum = max(maximum, active)
        calls += 1
        await asyncio.sleep(0.03)
        active -= 1
        return web.json_response(response_payload())

    async with LocalService(handler) as service:
        client = DecisionClient(client_config(service, max_concurrency=2))
        session = None
        results = await asyncio.gather(
            *(client.evaluate({}, questions()) for _ in range(6))
        )
        session = client._session
        assert len(results) == 6 and calls == 6
        assert maximum == 2
        assert session is not None and not session.closed
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"base_url": ""}, "missing_base_url"),
        ({"base_url": "https://user:secret@example.test/v1"}, "invalid_base_url"),
        ({"base_url": "https://example.test:invalid/v1"}, "invalid_base_url"),
        ({"token": ""}, "missing_token"),
        ({"token": "secret\ninjected"}, "invalid_token"),
        ({"model": ""}, "missing_model"),
        ({"model": "model\ninjected"}, "invalid_model"),
        ({"timeout": 0}, "invalid_timeout"),
        ({"timeout": float("inf")}, "invalid_timeout"),
        ({"max_concurrency": 0}, "invalid_max_concurrency"),
        ({"max_concurrency": 1.5}, "invalid_max_concurrency"),
    ],
)
async def test_readiness_rejects_incomplete_or_invalid_configuration(overrides, reason):
    config = {
        "base_url": "https://example.test/v1",
        "token": "test-token",
        "model": "test-model",
        **overrides,
    }
    client = DecisionClient(config)
    assert not client.ready
    assert client.status()["reason"] == reason
    with pytest.raises(DecisionError) as error:
        await client.evaluate({}, questions())
    assert error.value.code == reason
    await client.close()


@pytest.mark.asyncio
async def test_choice_noul_and_score_share_one_real_protocol_request():
    questions_payload = {
        **questions(),
        "is_meme": {"type": "noul", "instructions": "Is it a chat reaction image?"},
        "fit": {
            "type": "score",
            "instructions": "Rate how well it fits.",
            "criteria": ["poor", "good", "excellent"],
        },
    }

    async def handler(request):
        incoming = await request.json()
        assert incoming["questions"] == questions_payload
        payload = response_payload()
        payload["answers"].update(
            is_meme={"type": "noul", "noul": 0.96},
            fit={"type": "score", "score": 1.6},
        )
        return web.json_response(payload)

    async with LocalService(handler) as service:
        client = DecisionClient(client_config(service))
        answers = await client.evaluate({"caption": "celebrating"}, questions_payload)
        assert answers["is_meme"]["noul"] == 0.96
        assert answers["fit"]["score"] == 1.6
        await client.close()


@pytest.mark.asyncio
async def test_chunked_response_is_read_to_completion():
    async def handler(request):
        response = web.StreamResponse(headers={"Content-Type": "application/json"})
        await response.prepare(request)
        body = json.dumps(response_payload()).encode()
        await response.write(body[:12])
        await asyncio.sleep(0.01)
        await response.write(body[12:])
        await response.write_eof()
        return response

    async with LocalService(handler) as service:
        client = DecisionClient(client_config(service))
        assert (await client.evaluate({}, questions()))["selection"]["choice"] == "c0"
        await client.close()


@pytest.mark.asyncio
async def test_partial_answer_set_is_rejected_before_returning_any_decision():
    async def handler(request):
        return web.json_response(
            {"answers": {"is_meme": {"type": "noul", "noul": 0.01}}}
        )

    async with LocalService(handler) as service:
        client = DecisionClient(client_config(service))
        with pytest.raises(DecisionError, match="invalid_response"):
            await client.evaluate({}, {**questions(), "is_meme": {"type": "noul"}})
        await client.close()


@pytest.mark.asyncio
async def test_waiting_chat_request_is_not_overtaken_by_following_collection():
    entered = asyncio.Event()
    release = asyncio.Event()
    order = []

    async def handler(request):
        payload = await request.json()
        order.append(payload["state"]["name"])
        if len(order) == 1:
            entered.set()
            await release.wait()
        return web.json_response(response_payload())

    async with LocalService(handler) as service:
        client = DecisionClient(client_config(service, max_concurrency=1))
        initial = asyncio.create_task(
            client.evaluate({"name": "collection0"}, questions())
        )
        await entered.wait()
        waiting = asyncio.create_task(client.evaluate({"name": "chat"}, questions()))
        await asyncio.sleep(0)
        later = asyncio.create_task(
            client.evaluate({"name": "collection1"}, questions())
        )
        release.set()
        await asyncio.gather(initial, waiting, later)
        assert order == ["collection0", "chat", "collection1"]
        await client.close()


@pytest.mark.asyncio
async def test_choice_limit_counts_all_255_options():
    payload = {
        "selection": {
            "type": "choice",
            "criteria": {f"c{index}": str(index) for index in range(255)},
        }
    }

    async def handler(request):
        incoming = await request.json()
        assert len(incoming["questions"]["selection"]["criteria"]) == 255
        return web.json_response(response_payload())

    async with LocalService(handler) as service:
        client = DecisionClient(client_config(service))
        assert (await client.evaluate({}, payload))["selection"]["choice"] == "c0"
        payload["selection"]["criteria"]["overflow"] = "extra"
        with pytest.raises(DecisionError, match="invalid_request"):
            await client.evaluate({}, payload)
        await client.close()


@pytest.mark.asyncio
async def test_usage_and_header_cost_remain_attached_to_the_owning_request():
    async def handler(request):
        index = (await request.json())["state"]["index"]
        payload = response_payload()
        payload.update(model=f"resolved-{index}", usage={"input_tokens": index})
        return web.json_response(payload, headers={"X-System-One-Credits": str(index)})

    async with LocalService(handler) as service:
        client = DecisionClient(client_config(service))
        first, second = await asyncio.gather(
            client.evaluate({"index": 1}, questions()),
            client.evaluate({"index": 2}, questions()),
        )
        assert first.model == "resolved-1" and second.model == "resolved-2"
        assert first.credits == 1 and second.credits == 2
        assert first.usage == {"input_tokens": 1}
        assert "output_tokens" not in first.usage
        assert set(first) == {"selection"}
        await client.close()


def test_partial_native_service_configuration_uses_schema_base_url_default():
    client = DecisionClient({"token": "test-only-credential", "model": "jev"})
    assert client.ready
    assert client.base_url == "https://system-one.dev/v1"
    assert client._session is None
