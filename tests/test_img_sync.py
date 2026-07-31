from types import SimpleNamespace

import pytest

from astrbot_plugin_meme_manager.image_host import img_sync as sync_module
from astrbot_plugin_meme_manager.image_host.img_sync import ImageSync


@pytest.mark.parametrize(
    ("provider_type", "config", "expected"),
    [
        (
            "stardots",
            {"key": "key", "secret": "secret", "space": "space"},
            {
                "key": "key",
                "secret": "secret",
                "space": "space",
                "local_dir": "images",
            },
        ),
        ("cloudflare_r2", {"account_id": "account"}, {"account_id": "account"}),
        (
            "webdav",
            {"url": "https://dav.example"},
            {"url": "https://dav.example", "local_dir": "images"},
        ),
    ],
)
def test_init_selects_provider_and_wires_dependencies(
    monkeypatch, provider_type, config, expected
):
    provider_calls = []
    tracker_calls = []
    manager_calls = []

    def provider_factory(value):
        provider_calls.append(value)
        return "provider"

    monkeypatch.setattr(sync_module, "StarDotsProvider", provider_factory)
    monkeypatch.setattr(sync_module, "CloudflareR2Provider", provider_factory)
    monkeypatch.setattr(sync_module, "WebDAVProvider", provider_factory)
    monkeypatch.setattr(
        sync_module,
        "UploadTracker",
        lambda path: tracker_calls.append(path) or "tracker",
    )
    monkeypatch.setattr(
        sync_module,
        "SyncManager",
        lambda **kwargs: manager_calls.append(kwargs) or "manager",
    )

    client = ImageSync(config, "images", provider_type)
    assert provider_calls == [expected]
    assert tracker_calls[0].name == ".upload_tracker.json"
    assert manager_calls == [
        {
            "image_host": "provider",
            "local_dir": client.local_dir,
            "upload_tracker": "tracker",
        }
    ]
    assert client.sync_process is None
    assert client._sync_task is None


def test_init_rejects_unknown_provider(tmp_path):
    with pytest.raises(ValueError):
        ImageSync({}, tmp_path, "unknown")


def make_client(status=None):
    client = object.__new__(ImageSync)
    client.config = {"key": "key"}
    client.local_dir = sync_module.Path("images")
    client.provider = SimpleNamespace()
    client.sync_manager = SimpleNamespace(
        check_sync_status=lambda: status or {"to_upload": [], "to_download": []}
    )
    client.sync_process = None
    client._sync_task = None
    return client


def test_status_and_remote_operations_delegate_to_collaborators():
    status = {"to_upload": [{"filename": "a.png"}]}
    client = make_client(status)
    calls = []
    client.provider = SimpleNamespace(
        get_image_list=lambda: [{"filename": "remote.png"}],
        delete_image=lambda filename: calls.append(filename) or True,
    )
    assert client.check_status() == status
    assert client.get_remote_files() == [{"filename": "remote.png"}]
    assert client.delete_remote_file("remote.png")
    assert calls == ["remote.png"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task", "status"),
    [
        ("upload", {"to_upload": []}),
        ("download", {"to_download": []}),
        ("overwrite_to_remote", {"to_upload": [], "to_delete_remote": []}),
        (
            "overwrite_from_remote",
            {"to_download": [], "to_delete_local": []},
        ),
    ],
)
async def test_start_sync_skips_tasks_without_work(task, status):
    client = make_client(status)
    assert await client.start_sync(task)
    assert client.sync_process is None


@pytest.mark.asyncio
async def test_start_sync_rejects_concurrent_process():
    client = make_client()
    client.sync_process = SimpleNamespace(is_alive=lambda: True)
    with pytest.raises(RuntimeError):
        await client.start_sync("upload")


@pytest.mark.asyncio
@pytest.mark.parametrize(("exitcode", "expected"), [(0, True), (2, False)])
async def test_start_sync_waits_for_worker_exit(monkeypatch, exitcode, expected):
    client = make_client({"to_upload": [{"filename": "a.png"}]})

    class FakeProcess:
        def __init__(self, target, args):
            self.target = target
            self.args = args
            self.exitcode = exitcode
            self.started = False
            self.joined = False

        def start(self):
            self.started = True

        def join(self):
            self.joined = True

        def is_alive(self):
            return False

    monkeypatch.setattr(sync_module.multiprocessing, "Process", FakeProcess)
    assert await client.start_sync("upload") is expected
    assert client.sync_process.started
    assert client.sync_process.joined


def test_stop_sync_terminates_stubborn_process_and_cancels_waiter():
    class FakeProcess:
        alive = True
        terminated = False
        killed = False

        def is_alive(self):
            return self.alive

        def terminate(self):
            self.terminated = True

        def join(self, timeout=None):
            return None

        def kill(self):
            self.killed = True
            self.alive = False

    class FakeTask:
        cancelled = False

        def done(self):
            return False

        def cancel(self):
            self.cancelled = True

    client = make_client()
    process = FakeProcess()
    task = FakeTask()
    client.sync_process = process
    client._sync_task = task
    client.stop_sync()
    assert process.terminated
    assert process.killed
    assert task.cancelled
    assert client.sync_process is None
    assert client._sync_task is None


def test_upload_and_download_start_expected_worker():
    client = make_client()
    calls = []
    client._start_sync_process = lambda task: calls.append(task) or f"process-{task}"
    assert client.upload_to_remote() == "process-upload"
    assert client.download_to_local() == "process-download"
    assert calls == ["upload", "download"]


def test_start_sync_process_constructs_and_starts_process(monkeypatch):
    client = make_client()

    class FakeProcess:
        def __init__(self, target, args):
            self.target = target
            self.args = args
            self.started = False

        def start(self):
            self.started = True

    monkeypatch.setattr(sync_module.multiprocessing, "Process", FakeProcess)
    process = client._start_sync_process("download")
    assert process.target is sync_module.run_sync_process
    assert process.args == (client.config, "images", "download")
    assert process.started


@pytest.mark.parametrize(
    ("config", "expected_type", "expected_config"),
    [
        ({"cloudflare_r2": {"account_id": "a"}}, "cloudflare_r2", {"account_id": "a"}),
        ({"stardots": {"key": "k"}}, "stardots", {"key": "k"}),
        ({"webdav": {"url": "u"}}, "webdav", {"url": "u"}),
        ({"account_id": "a"}, "cloudflare_r2", {"account_id": "a"}),
        ({"key": "k"}, "stardots", {"key": "k"}),
        (
            {"provider": "webdav", "url": "u"},
            "webdav",
            {"provider": "webdav", "url": "u"},
        ),
    ],
)
def test_run_sync_process_detects_provider(
    monkeypatch, config, expected_type, expected_config
):
    calls = []
    sync = SimpleNamespace(
        sync_manager=SimpleNamespace(sync_to_remote=lambda: calls.append("upload") or True)
    )
    monkeypatch.setattr(
        sync_module,
        "ImageSync",
        lambda provider_config, local_dir, provider_type: (
            calls.append((provider_config, local_dir, provider_type)) or sync
        ),
    )
    with pytest.raises(SystemExit) as exc_info:
        sync_module.run_sync_process(config, "images", "upload")
    assert exc_info.value.code == 0
    assert calls == [(expected_config, "images", expected_type), "upload"]


@pytest.mark.parametrize(
    ("task", "method", "success", "exitcode"),
    [
        ("upload", "sync_to_remote", False, 1),
        ("download", "sync_from_remote", True, 0),
        ("overwrite_to_remote", "overwrite_to_remote", True, 0),
        ("overwrite_from_remote", "overwrite_from_remote", False, 1),
    ],
)
def test_run_sync_process_dispatches_task(
    monkeypatch, task, method, success, exitcode
):
    calls = []
    manager = SimpleNamespace(**{method: lambda: calls.append(method) or success})
    monkeypatch.setattr(
        sync_module,
        "ImageSync",
        lambda *args: SimpleNamespace(sync_manager=manager),
    )
    with pytest.raises(SystemExit) as exc_info:
        sync_module.run_sync_process({"key": "key"}, "images", task)
    assert exc_info.value.code == exitcode
    assert calls == [method]


def test_run_sync_process_runs_full_sync_in_order(monkeypatch):
    calls = []
    manager = SimpleNamespace(
        sync_to_remote=lambda: calls.append("upload") or True,
        sync_from_remote=lambda: calls.append("download") or True,
    )
    monkeypatch.setattr(
        sync_module,
        "ImageSync",
        lambda *args: SimpleNamespace(sync_manager=manager),
    )
    with pytest.raises(SystemExit) as exc_info:
        sync_module.run_sync_process({"key": "key"}, "images", "sync_all")
    assert exc_info.value.code == 0
    assert calls == ["upload", "download"]


@pytest.mark.parametrize(
    ("config", "task"),
    [({}, "upload"), ({"key": "key"}, "unknown")],
)
def test_run_sync_process_rejects_unknown_config_or_task(monkeypatch, config, task):
    monkeypatch.setattr(
        sync_module,
        "ImageSync",
        lambda *args: SimpleNamespace(sync_manager=SimpleNamespace()),
    )
    with pytest.raises(SystemExit) as exc_info:
        sync_module.run_sync_process(config, "images", task)
    assert exc_info.value.code == 1
