import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from aiohttp import web
from backend import decision as decision_module
from backend.decision import MAX_RESPONSE_BYTES, DecisionClient, DecisionError

pytestmark = pytest.mark.asyncio


@asynccontextmanager
async def client_for(handler, **overrides):
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = web.AppRunner(app, shutdown_timeout=0.1)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    original_base = decision_module.CLOUDFLARE_BASE_URL
    if overrides.get("provider") == "cloudflare":
        decision_module.CLOUDFLARE_BASE_URL = f"http://127.0.0.1:{port}/client/v4"
    client = DecisionClient(
        {
            "base_url": f"http://127.0.0.1:{port}/v1",
            "token": "test-token",
            "model": "test-model",
            "timeout": 2,
            "max_concurrency": 2,
            **overrides,
        }
    )
    try:
        yield client
    finally:
        await client.close()
        await runner.cleanup()
        decision_module.CLOUDFLARE_BASE_URL = original_base


def questions():
    return {
        "selection": {"type": "choice", "criteria": {"c0": "comfort", "skip": "none"}}
    }


def payload(**answer):
    return {"answers": {"selection": {"type": "choice", "choice": "c0", **answer}}}


async def test_catalog_filters_ids_without_credentials_or_inference():
    observed = []

    async def handler(request):
        observed.append((request.path, request.headers.get("Authorization")))
        return web.json_response(
            {
                "models": [
                    {"name": "exact", "displayName": "Readable"},
                    {"name": "exact"},
                    {"name": "unsupported", "capabilities": {"questions": []}},
                    {},
                ]
            }
        )

    async with client_for(handler) as client:
        client.base_url += "/systemone"
        assert await client.list_models() == [{"id": "exact", "name": "Readable"}]
        assert observed == [("/v1/models", None)]
        assert client.request_counts == {}


async def test_protocol_and_session_lifecycle():
    observed = []
    qs = {
        **questions(),
        "meme": {"type": "noul"},
        "fit": {"type": "score", "criteria": ["poor", "good"]},
    }

    async def handler(request):
        observed.append(await request.json())
        assert request.path == "/v1/systemone"
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.headers["Idempotency-Key"]
        body = payload()
        body["answers"].update(
            meme={"type": "noul", "noul": 0.96}, fit={"type": "score", "score": 0.8}
        )
        response = web.StreamResponse(headers={"Content-Type": "application/json"})
        await response.prepare(request)
        raw = json.dumps(body).encode()
        await response.write(raw[:10])
        await response.write(raw[10:])
        await response.write_eof()
        return response

    async with client_for(handler) as client:
        assert client._session is None
        answers = await client.evaluate({"text": "安慰对方"}, qs)
        assert answers["meme"]["noul"] == 0.96 and answers["fit"]["score"] == 0.8
        assert observed == [
            {"model": "test-model", "state": {"text": "安慰对方"}, "questions": qs}
        ]
        session = client._session
    assert session.closed
    await client.close()
    with pytest.raises(DecisionError, match="closed"):
        await client.evaluate({}, qs)


@pytest.mark.parametrize(
    "status,code",
    [
        (400, "bad_request"),
        (401, "unauthorized"),
        (403, "forbidden"),
        (404, "not_found"),
        (408, "request_timeout"),
        (429, "rate_limited"),
        (503, "server_error"),
        (307, "http_error"),
    ],
)
async def test_http_errors_do_not_retry_redirect_or_leak_details(status, code):
    paths = []

    async def handler(request):
        paths.append(request.path)
        return web.Response(
            status=status,
            text="private upstream detail",
            headers={"Location": "/capture"},
        )

    async with client_for(handler) as client:
        with pytest.raises(DecisionError) as error:
            await client.evaluate({}, questions())
        assert error.value.code == code and "private" not in str(error.value)
        assert paths == ["/v1/systemone"]


@pytest.mark.parametrize(
    "body,code",
    [
        ("not-json", "invalid_response"),
        ({"answers": {}}, "invalid_response"),
        (payload(choice="unknown"), "invalid_response"),
        (payload(probability=float("nan")), "invalid_probability"),
        (payload(confidence=True), "invalid_probability"),
        (payload(probability=1.1), "invalid_probability"),
    ],
)
async def test_invalid_answers(body, code):
    async def handler(request):
        return web.Response(text=body if isinstance(body, str) else json.dumps(body))

    async with client_for(handler) as client:
        with pytest.raises(DecisionError, match=code):
            await client.evaluate({}, questions())


async def test_partial_answers_and_size_limits():
    async def handler(request):
        return web.json_response(payload())

    async with client_for(handler) as client:
        with pytest.raises(DecisionError, match="invalid_response"):
            await client.evaluate({}, {**questions(), "meme": {"type": "noul"}})
        with pytest.raises(DecisionError, match="request_too_large"):
            await client.evaluate({"text": "表" * 30_000}, questions())

    async def oversized(request):
        return web.Response(body=b" " * (MAX_RESPONSE_BYTES + 1))

    async with client_for(oversized) as client:
        with pytest.raises(DecisionError, match="response_too_large"):
            await client.evaluate({}, questions())


async def test_queue_timeout_and_cancellation():
    started = asyncio.Event()

    async def handler(request):
        started.set()
        await asyncio.Event().wait()

    async with client_for(handler, timeout=0.05, max_concurrency=1) as client:
        await client._semaphore.acquire()
        try:
            with pytest.raises(DecisionError, match="timeout"):
                await client.evaluate({}, questions())
        finally:
            client._semaphore.release()
        assert not started.is_set()
        task = asyncio.create_task(client.evaluate({}, questions()))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_concurrency_and_per_request_usage():
    active = maximum = 0

    async def handler(request):
        nonlocal active, maximum
        index = (await request.json())["state"]["index"]
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return web.json_response(
            {
                **payload(),
                "model": f"resolved-{index}",
                "usage": {"input_tokens": index},
            },
            headers={"X-System-One-Credits": str(index)},
        )

    async with client_for(handler) as client:
        results = await asyncio.gather(
            *(client.evaluate({"index": i}, questions()) for i in range(4))
        )
        assert maximum == 2
        for index, result in enumerate(results):
            assert result.model == f"resolved-{index}"
            assert result.credits == index and result.usage == {"input_tokens": index}


ACCOUNT_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
CLOUDFLARE_CONFIG = {
    "provider": "cloudflare",
    "account_id": ACCOUNT_ID,
    "token": "cf-token",
    "model": "clef-flash",
}


@pytest.mark.parametrize("nested", [False, True])
async def test_cloudflare_endpoint_headers_and_envelope(nested):
    observed = {}

    async def handler(request):
        observed["path"] = request.path
        observed["headers"] = dict(request.headers)
        observed["body"] = await request.json()
        result = {
            **payload(confidence=0.9),
            "model": "clef-flash",
            "usage": {"input_tokens": 120, "output_tokens": 0},
        }
        return web.json_response(
            {"success": True, "result": {"result": result} if nested else result}
        )

    async with client_for(handler, **CLOUDFLARE_CONFIG) as client:
        answers = await client.evaluate({"task": "meme_selection"}, questions())
    assert observed["path"] == (
        f"/client/v4/accounts/{ACCOUNT_ID}/ai/run/@cf/cloudflare/clef-flash"
    )
    assert observed["headers"]["Authorization"] == "Bearer cf-token"
    assert "Idempotency-Key" not in observed["headers"]
    assert observed["body"]["model"] == "clef-flash"
    assert answers["selection"]["choice"] == "c0"
    assert answers.model == "clef-flash"
    assert answers.usage == {"input_tokens": 120, "output_tokens": 0}
    assert answers.credits is None


@pytest.mark.parametrize(
    "status,body,code",
    [
        (401, {"success": False, "errors": [{"code": 10000}]}, "unauthorized"),
        (429, {"success": False, "errors": [{"code": 2021}]}, "insufficient_credits"),
        (400, {"success": False, "errors": [{"code": 7000}]}, "bad_request"),
        (500, {"success": False, "errors": [{"code": 9999}]}, "server_error"),
        (500, "not-json", "server_error"),
        (200, {"success": False, "errors": []}, "invalid_response"),
        (200, {"result": {}, "success": True}, "invalid_response"),
    ],
)
async def test_cloudflare_error_envelope_maps_stable_codes(status, body, code):
    async def handler(request):
        return web.Response(
            status=status,
            text=body if isinstance(body, str) else json.dumps(body),
        )

    async with client_for(handler, **CLOUDFLARE_CONFIG) as client:
        with pytest.raises(DecisionError, match=code):
            await client.evaluate({}, questions())


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"token": ""}, "missing_token"),
        ({"account_id": ""}, "missing_account_id"),
        ({"account_id": "xyz"}, "invalid_account_id"),
        ({"model": ""}, "missing_model"),
        ({"model": "gpt-x"}, "unsupported_model"),
    ],
)
async def test_cloudflare_readiness_reasons(overrides, reason):
    config = {**CLOUDFLARE_CONFIG, **overrides}
    assert DecisionClient(config).status()["reason"] == reason


async def test_invalid_provider_falls_back_to_systemone_reasons():
    client = DecisionClient(
        {"provider": "azure", "token": "t", "model": "m", "base_url": ""}
    )
    status = client.status()
    assert status["provider"] == "systemone"
    assert status["reason"] == "invalid_provider"


async def test_cloudflare_catalog_is_local_and_needs_no_credentials():
    client = DecisionClient({"provider": "cloudflare"})
    try:
        models = await client.list_models()
        assert [model["id"] for model in models] == [
            "clef-flash",
            "clef",
            "clef-omni",
        ]
        assert client._session is None
        assert client.request_counts == {}
    finally:
        await client.close()


async def test_cloudflare_request_limits_differ_from_systemone():
    async def handler(request):
        body = await request.json()
        answers = {
            name: {"type": "choice", "choice": next(iter(question["criteria"]))}
            for name, question in body["questions"].items()
        }
        result = {"answers": answers}
        if "/accounts/" in request.path:
            result = {"success": True, "result": result}
        return web.json_response(result)

    wide_questions = {
        f"q{index}": {"type": "choice", "criteria": {"a": "x", "b": "y"}}
        for index in range(33)
    }
    async with client_for(handler, **CLOUDFLARE_CONFIG) as client:
        # 33 个问题超出 System One 的 32 问上限，但处于 clef 的 64 问内。
        await client.evaluate({}, wide_questions)
        # clef 的 choice 至少需要两个选项。
        with pytest.raises(DecisionError, match="invalid_request"):
            await client.evaluate(
                {}, {"solo": {"type": "choice", "criteria": {"a": "x"}}}
            )
        # 问题名超出 clef 允许的字符集。
        with pytest.raises(DecisionError, match="invalid_request"):
            await client.evaluate(
                {}, {"选图": {"type": "choice", "criteria": {"a": "x", "b": "y"}}}
            )

    async with client_for(handler) as systemone_client:
        # 同样的 33 个问题按 System One 规则被拒绝，单选项问题保持合法。
        with pytest.raises(DecisionError, match="invalid_request"):
            await systemone_client.evaluate({}, wide_questions)
        await systemone_client.evaluate(
            {}, {"solo": {"type": "choice", "criteria": {"a": "x"}}}
        )
