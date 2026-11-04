"""在底层同步 HTTP 边界检查异步入口。"""

import asyncio
import threading

import pytest
import requests
from astrbot_plugin_meme_manager.backend.packs import storage
from astrbot_plugin_meme_manager.image_host.img_sync import ImageSync
from astrbot_plugin_meme_manager.mixins.web_api import WebAPIMixin
from quart import Quart


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation", ["catalog", "update", "lsky", "webdav", "stardots"]
)
async def test_slow_http_does_not_block_event_loop(operation, tmp_path, monkeypatch):
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    calls = []
    loop_thread = threading.get_ident()

    def slow_request(*args, **kwargs):
        loop.call_soon_threadsafe(started.set)
        released = release.wait(timeout=2)
        calls.append((threading.get_ident(), released))
        raise RuntimeError("Simulated network failure")

    monkeypatch.setattr(requests.Session, "request", slow_request)
    api = object.__new__(WebAPIMixin)
    api._get_github_accelerator_url = lambda: ""
    client = None
    if operation == "update":
        monkeypatch.setattr(storage, "PACKS_DIR", tmp_path / "packs")
        monkeypatch.setattr(storage, "TEMP_DIR", tmp_path / "temp")
        (storage.PACKS_DIR / "sample").mkdir(parents=True)
        monkeypatch.setattr(
            storage,
            "find_cached_pack_entry",
            lambda pack_id: {
                "id": pack_id,
                "version": "2.0",
                "source": {
                    "type": "github",
                    "repo": "owner/repo",
                    "ref": "main",
                    "subpath": "pack",
                },
            },
        )
        api._semantic_operation_guard = None
        api._community_update_jobs = {"job": {"state": "running"}}
        coroutine = api._run_community_update_job(
            "job", {"action": "prepare", "pack_id": "sample"}
        )
    elif operation == "catalog":
        coroutine = api._api_fetch_community_index()
    else:
        client = ImageSync(
            {
                "url": "https://example.test",
                "token": "test-token",
                "username": "test-user",
                "password": "test-password",
                "key": "test-key",
                "secret": "test-secret",
                "space": "test-space",
            },
            tmp_path,
            operation,
        )
        api._resolve_requested_sync_pack_id = lambda: "sample"
        api._ensure_img_sync_for_pack = lambda pack_id: client
        api._get_img_host_sync_task_status = lambda: {"running": False}
        api._get_img_host_status_cache_ttl = lambda: 0
        coroutine = api._api_img_host_sync_status()

    async def heartbeat():
        await started.wait()
        release.set()

    app = Quart(__name__)
    try:
        async with app.test_request_context("/"):
            result, _ = await asyncio.wait_for(
                asyncio.gather(coroutine, heartbeat()), timeout=5
            )
        assert calls, "The real call chain must reach the HTTP boundary"
        assert all(thread != loop_thread for thread, _ in calls)
        assert all(released for _, released in calls), (
            "The event loop must release HTTP while the request is still pending"
        )
        if operation == "update":
            assert api._community_update_jobs["job"]["state"] == "failed"
            assert (
                "Simulated network failure"
                in api._community_update_jobs["job"]["message"]
            )
        else:
            assert result[1] == 500
            assert "Simulated network failure" in str(await result[0].get_json())
    finally:
        release.set()
        if client is not None:
            client.close()
