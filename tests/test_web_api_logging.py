import logging
from types import SimpleNamespace

import pytest
from astrbot_plugin_meme_manager.mixins import web_api
from quart import Quart, Response


@pytest.fixture
def webui_api(monkeypatch):
    """创建 API 注册表并隔离日志捕获。

    Args:
        monkeypatch: 用于替换插件日志器的测试夹具。

    Returns:
        Mixin、Quart 应用及隔离的日志器。
    """
    app = Quart(__name__)
    app.config["TESTING"] = True
    subject = web_api.WebAPIMixin()
    subject.context = SimpleNamespace(
        register_web_api=lambda route, handler, methods, desc: app.add_url_rule(
            route, view_func=handler, methods=methods
        )
    )
    logger = logging.getLogger(__name__)
    monkeypatch.setattr(web_api, "logger", logger)
    return subject, app, logger


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["packs", "meme_image_data"])
@pytest.mark.parametrize("response_type", ["response", "tuple"])
@pytest.mark.parametrize(
    "status,level",
    [
        (200, logging.DEBUG),
        (304, logging.DEBUG),
        (400, logging.WARNING),
        (404, logging.WARNING),
        (409, logging.WARNING),
        (500, logging.ERROR),
        (503, logging.ERROR),
    ],
)
async def test_request_log_levels_and_response_preservation(
    webui_api, caplog, route, response_type, status, level
):
    subject, app, logger = webui_api

    async def handler():
        body = "" if status == 304 else "payload"
        if response_type == "tuple":
            return body, status, {"X-Test": "preserved"}
        return Response(body, status=status, headers={"X-Test": "preserved"})

    subject._register_webui_api(route, handler, ["GET"], "Test endpoint")
    with caplog.at_level(logging.DEBUG, logger=logger.name):
        response = await app.test_client().get(f"/meme_manager/{route}")

    assert response.status_code == status
    assert response.headers["X-Test"] == "preserved"
    assert await response.get_data() == (b"" if status == 304 else b"payload")
    if route == "meme_image_data" and status >= 400:
        assert response.headers["Cache-Control"] == "no-store"
    records = [record for record in caplog.records if record.name == logger.name]
    assert [record.levelno for record in records] == [logging.DEBUG, level]
    assert f"GET /meme_manager/{route} 开始" in records[0].message
    assert f"GET /meme_manager/{route} 完成 状态={status}" in records[1].message
    assert "耗时=" in records[1].message


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 304])
@pytest.mark.parametrize(
    "route",
    ["packs", "meme_image_data", "preview/manifest", "auto-collect/inbox/image_data"],
)
async def test_page_load_requests_are_silent_at_info(webui_api, caplog, route, status):
    subject, app, logger = webui_api

    async def handler():
        return "", status

    subject._register_webui_api(route, handler, ["GET"], "Test endpoint")
    client = app.test_client()
    with caplog.at_level(logging.INFO, logger=logger.name):
        for _ in range(153):
            response = await client.get(f"/meme_manager/{route}")
            assert response.status_code == status

    assert not [record for record in caplog.records if record.name == logger.name]


@pytest.mark.asyncio
async def test_request_exception_keeps_traceback_and_propagates(webui_api, caplog):
    subject, app, logger = webui_api
    error = RuntimeError("Handler failed")

    async def handler():
        raise error

    subject._register_webui_api("packs", handler, ["POST"], "Test endpoint")
    with caplog.at_level(logging.INFO, logger=logger.name):
        with pytest.raises(RuntimeError) as caught:
            await app.test_client().post("/meme_manager/packs")

    assert caught.value is error
    records = [record for record in caplog.records if record.name == logger.name]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert "POST /meme_manager/packs 失败 耗时=" in records[0].message
    assert records[0].exc_info[1] is error
    assert records[0].exc_info[2] is not None
