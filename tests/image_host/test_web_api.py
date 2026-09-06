import asyncio
import threading
from types import SimpleNamespace

import pytest
from astrbot_plugin_meme_manager.mixins import web_api as web_api_module
from astrbot_plugin_meme_manager.mixins.web_api import WebAPIMixin
from quart import Quart


@pytest.fixture
def api_harness(tmp_path, monkeypatch):
    ended = threading.Event()
    operations = []
    progress = {
        "available": True,
        "running": False,
        "completed": True,
        "success": None,
        "processed": 0,
        "total": 2,
    }
    process = SimpleNamespace(join=lambda: ended.wait(3))
    client = SimpleNamespace(
        local_dir=tmp_path / "memes",
        sync_process=None,
        get_task_status=lambda: dict(progress),
    )

    def start(task):
        progress.update(
            running=True, completed=False, task_id="test-task", phase="upload"
        )
        return process

    def stop():
        operations.append(("stop", threading.get_ident()))
        progress.update(running=False, completed=True, success=False, phase="cancelled")
        ended.set()

    client._start_sync_process = start
    client.stop_sync = stop
    harness = object.__new__(WebAPIMixin)
    harness.img_sync = client
    harness._img_sync_pack_id = "test-pack"
    harness._last_img_host_sync_task_status = None
    harness._img_host_local_operation = None
    harness._ensure_img_sync_for_pack = lambda pack_id=None: client
    harness._img_host_sync_status_cache = {"old": {"payload": {"is_synced": True}}}
    harness.semantic_task_manager = SimpleNamespace(
        begin_external_pack_operation=lambda pack, operation: operations.append(
            ("begin", pack)
        ),
        end_external_pack_operation=lambda pack: operations.append(("end", pack)),
    )
    monkeypatch.setattr(web_api_module, "PACKS_DIR", tmp_path)
    yield harness, progress, operations, ended
    ended.set()


@pytest.mark.asyncio
async def test_webui_worker_completion_releases_pack_without_polling(api_harness):
    api, progress, operations, ended = api_harness
    status = api._start_img_host_sync_task("download", "test-pack")
    assert status["running"]
    assert status["total"] == 2
    assert ("begin", "test-pack") in operations
    with pytest.raises(RuntimeError):
        api._start_img_host_sync_task("download", "test-pack")
    progress.update(running=False, completed=True, success=True, processed=2)
    ended.set()
    await asyncio.wait_for(api._img_host_sync_monitor, 1)
    assert ("end", "test-pack") in operations
    assert api._img_host_local_operation is None
    assert api._last_img_host_sync_task_status["processed"] == 2
    assert api._img_host_sync_status_cache == {}


@pytest.mark.asyncio
async def test_cancel_endpoint_rejects_other_pack_and_stops_off_loop(api_harness):
    api, progress, operations, ended = api_harness
    app = Quart(__name__)
    async with app.test_request_context(
        "/", method="POST", json={"managed_pack_id": "other"}
    ):
        _, code = await api._api_img_host_sync_cancel()
    assert code == 409
    assert operations == []
    event_thread = threading.get_ident()
    async with app.test_request_context(
        "/", method="POST", json={"managed_pack_id": "test-pack"}
    ):
        response = await api._api_img_host_sync_cancel()
    assert (await response.get_json())["phase"] == "cancelled"
    assert operations[0][0] == "stop"
    assert operations[0][1] != event_thread
