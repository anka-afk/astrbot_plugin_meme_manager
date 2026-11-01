import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from aiohttp import web
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
