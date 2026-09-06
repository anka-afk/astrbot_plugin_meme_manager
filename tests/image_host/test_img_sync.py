import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest
from image_host import img_sync as sync_module
from image_host.core.file_handler import write_json_atomic
from image_host.img_sync import ImageSync


@pytest.fixture
def client(tmp_path, monkeypatch):
    provider = SimpleNamespace(
        close=lambda: None,
        get_image_list=lambda: [],
        delete_image=lambda image_id: True,
    )
    monkeypatch.setattr(sync_module, "WebDAVProvider", lambda config: provider)
    return ImageSync(
        {"url": "https://dav.example", "username": "user", "password": "secret"},
        tmp_path,
        "webdav",
    )


@pytest.fixture
def process_context(monkeypatch):
    processes = []

    class Process:
        def __init__(self, target, args):
            self.target, self.args = target, args
            self.pid = 123
            self.exitcode = None
            self.alive = False
            self.terminated = False
            self.killed = False
            self.join_delay = 0
            processes.append(self)

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def join(self, timeout=None):
            if timeout is None:
                time.sleep(self.join_delay)
                self.alive = False
                self.exitcode = 0

        def terminate(self):
            self.terminated = True
            self.alive = False
            self.exitcode = -15

        def kill(self):
            self.killed = True
            self.alive = False
            self.exitcode = -9

    context = SimpleNamespace(Event=threading.Event, Process=Process)
    monkeypatch.setattr(
        sync_module.multiprocessing, "get_context", lambda method: context
    )
    return processes


def test_all_providers_receive_local_root_without_network_probe(tmp_path, monkeypatch):
    calls = []

    def provider(config):
        calls.append(config)
        return SimpleNamespace(close=lambda: None)

    for name in ("StarDotsProvider", "CloudflareR2Provider", "WebDAVProvider"):
        monkeypatch.setattr(sync_module, name, provider)
    for name in ("stardots", "cloudflare_r2", "webdav"):
        sync = ImageSync({"list_cache_ttl": 33}, tmp_path, name)
        assert calls[-1]["local_dir"] == str(tmp_path.resolve())
        assert sync.sync_process is None
        assert sync._state_dir == tmp_path / ".sync-state"


def test_destination_scopes_change_for_storage_but_not_password_rotation(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sync_module, "WebDAVProvider", lambda config: None)
    base = {"url": "https://dav.example/root", "username": "user", "password": "one"}
    first = ImageSync(base, tmp_path, "webdav")
    rotated = ImageSync({**base, "password": "two", "timeout": 60}, tmp_path, "webdav")
    other = ImageSync({**base, "base_path": "other"}, tmp_path, "webdav")
    other_user = ImageSync({**base, "username": "someone"}, tmp_path, "webdav")
    assert first.upload_tracker.tracker_file == rotated.upload_tracker.tracker_file
    assert first.scope != other.scope != other_user.scope
    assert "one" not in str(first.upload_tracker.tracker_file)


@pytest.mark.parametrize(
    "task",
    ["upload", "download", "sync_all", "overwrite_to_remote", "overwrite_from_remote"],
)
def test_every_start_uses_guarded_worker(client, process_context, task):
    process = client._start_sync_process(task)
    assert process.target is sync_module.run_sync_process
    assert process.args[2:4] == (task, "webdav")
    assert process.is_alive()
    assert client.get_task_status()["phase"] == "starting"
    with pytest.raises(RuntimeError):
        client._start_sync_process("download")
    with pytest.raises(RuntimeError):
        client.upload_to_remote()
    with pytest.raises(RuntimeError):
        client.delete_remote_file("a.png")
    client.stop_sync()


def test_invalid_task_and_provider_fail_before_start(client, tmp_path, process_context):
    with pytest.raises(ValueError):
        client._start_sync_process("unknown")
    with pytest.raises(ValueError):
        ImageSync({}, tmp_path, "unknown")
    assert process_context == []


@pytest.mark.asyncio
async def test_async_wait_keeps_event_loop_responsive(
    client, process_context, monkeypatch
):
    original = client._start_sync_process

    def start(task):
        process = original(task)
        process.join_delay = 0.05
        return process

    monkeypatch.setattr(client, "_start_sync_process", start)
    progressed = False

    async def heartbeat():
        nonlocal progressed
        await asyncio.sleep(0.01)
        progressed = True

    success, _ = await asyncio.gather(client.start_sync("upload"), heartbeat())
    assert success
    assert progressed
    assert client._sync_task is None


@pytest.mark.asyncio
async def test_concurrent_async_starts_cannot_race_preflight(client, process_context):
    results = await asyncio.gather(
        client.start_sync("upload"),
        client.start_sync("download"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, RuntimeError) for result in results) == 1
    assert len(process_context) == 1


def test_progress_is_task_scoped_and_completion_uses_exit_code(client, process_context):
    process = client.upload_to_remote()
    write_json_atomic(client._progress_path, {"task_id": "old", "total": 999})
    assert client.get_task_status()["total"] == 0
    write_json_atomic(
        client._progress_path,
        {
            "task_id": client._task_id,
            "total": 3,
            "processed": 2,
            "phase": "upload",
            "success": True,
        },
    )
    assert client.get_task_status()["success"] is None
    process.alive = False
    process.exitcode = 1
    status = client.get_task_status()
    assert status["completed"]
    assert status["success"] is False
    assert status["processed"] == 2
    assert status["phase"] == "failed"


def test_stop_requests_cancellation_and_reaps_worker(client, process_context):
    process = client.upload_to_remote()
    client.stop_sync()
    assert client._cancel_event.is_set()
    assert process.terminated
    assert client.sync_process is None
    assert client.get_task_status()["phase"] == "cancelled"


def test_old_waiter_cannot_cancel_a_new_worker(client, process_context):
    old = client.upload_to_remote()
    old.alive = False
    old.exitcode = 0
    new = client.download_to_local()
    client.stop_sync(old)
    assert new.is_alive()
    assert not client._cancel_event.is_set()
    client.stop_sync()


def test_worker_dispatches_through_one_plan_and_closes_provider(tmp_path, monkeypatch):
    calls = []
    sync = SimpleNamespace(
        _state_dir=tmp_path / ".sync-state",
        provider=SimpleNamespace(close=lambda: calls.append("close")),
        sync_manager=SimpleNamespace(run=lambda task: calls.append(task) or True),
    )
    monkeypatch.setattr(sync_module, "ImageSync", lambda *args: sync)
    with pytest.raises(SystemExit) as result:
        sync_module.run_sync_process({}, str(tmp_path), "sync_all", "webdav")
    assert result.value.code == 0
    assert calls == ["sync_all", "close"]


def test_worker_persists_initialization_failure(tmp_path):
    progress_path = tmp_path / "progress.json"
    with pytest.raises(SystemExit) as result:
        sync_module.run_sync_process(
            {}, str(tmp_path), "upload", "unknown", str(progress_path), "task-id"
        )
    assert result.value.code == 1
    status = json.loads(progress_path.read_text())
    assert status["task_id"] == "task-id"
    assert status["phase"] == "failed"
