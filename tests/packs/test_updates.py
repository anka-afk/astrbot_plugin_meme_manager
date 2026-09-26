import asyncio
import base64
import json
import shutil
import time
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from astrbot_plugin_meme_manager.backend.packs import storage, updates
from astrbot_plugin_meme_manager.mixins import web_api
from quart import Quart
from PIL import Image


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    for key, relative in {
        "PACKS_DIR": "packs",
        "TEMP_DIR": "temp",
        "BACKUP_DIR": "backups",
        "PLUGIN_DATA_DIR": "data",
        "REGISTRY_PATH": "registry.json",
        "SELECTION_RULES_PATH": "rules.json",
    }.items():
        monkeypatch.setattr(storage, key, tmp_path / relative)
    source = {"type": "github", "repo": "owner/repo", "ref": "main", "subpath": "pack"}
    entry = {"id": "sample", "version": "2.0", "source": source}
    monkeypatch.setattr(
        storage,
        "find_cached_pack_entry",
        lambda pack_id: entry if pack_id == "sample" else None,
    )
    monkeypatch.setattr(
        storage, "load_cached_community_index", lambda: {"index": {"packs": [entry]}}
    )
    monkeypatch.setattr(storage, "_current_default_pack_id", lambda: "sample")
    local = storage.PACKS_DIR / "sample"
    local.mkdir(parents=True)
    (local / "memes/happy").mkdir(parents=True)
    manifest = {
        "schema_version": 1,
        "id": "sample",
        "name": "测试",
        "version": "1.0",
        "categories": {"happy": {"description": "Joy"}},
    }
    storage._save_json(local / "manifest.json", manifest)
    storage._save_json(local / "memes_data.json", {"happy": "Joy"})
    for name in ("change", "conflict", "delete", "move", "localdelete"):
        (local / f"memes/happy/{name}.png").write_bytes(name.encode())
    baseline = updates.snapshot(local)
    storage._save_json(local / updates.ORIGIN_FILE, {"source": source, **baseline})
    storage._save_registry(
        {
            "installed_packs": [
                {"id": "sample", "name": "测试", "version": "1.0", "enabled": True}
            ]
        }
    )
    token = "a" * 32
    session = storage.TEMP_DIR / "community_updates" / token
    remote = session / "candidate"
    shutil.copytree(local, remote)
    (remote / updates.ORIGIN_FILE).unlink()
    manifest["version"] = "2.0"
    storage._save_json(remote / "manifest.json", manifest)
    for name in ("change", "conflict", "new"):
        (remote / f"memes/happy/{name}.png").write_bytes(f"new-{name}".encode())
    (remote / "memes/happy/delete.png").unlink()
    (local / "memes/happy/conflict.png").write_bytes(b"user-edit")
    (local / "memes/happy/user.png").write_bytes(b"user-new")
    (local / "memes/happy/move.png").rename(local / "memes/happy/renamed.png")
    (local / "memes/happy/localdelete.png").unlink()
    record = {
        "pack_id": "sample",
        "source": source,
        "created_at": time.time(),
        "remote": updates.snapshot(remote),
    }
    storage._save_json(session / "session.json", record)
    return local, remote, {"session": token}, record


def execute(data):
    plan = updates.plan_update(data)
    return updates.apply_update({**data, "plan_token": plan["plan_token"]})


def test_merge_preserves_local_edits_moves_deletions_and_index(workspace):
    local, _, data, _ = workspace
    index = storage.PLUGIN_DATA_DIR / "semantic_indexes/sample"
    index.mkdir(parents=True)
    (index / "vector.bin").write_bytes(b"vectors")
    execute({**data, "strategy": "merge"})
    assert (local / "memes/happy/change.png").read_bytes() == b"new-change"
    assert (local / "memes/happy/conflict.png").read_bytes() == b"user-edit"
    assert (local / "memes/happy/user.png").is_file()
    assert (local / "memes/happy/new.png").is_file()
    assert (local / "memes/happy/delete.png").is_file()
    assert (local / "memes/happy/renamed.png").is_file()
    assert not (local / "memes/happy/move.png").exists()
    assert not (local / "memes/happy/localdelete.png").exists()
    assert (index / "vector.bin").read_bytes() == b"vectors"
    assert not (storage.BACKUP_DIR / "community_updates").exists()
    assert storage._load_registry()["installed_packs"][0]["version"] == "2.0"


def test_upstream_merge_applies_conflicts_deletions_and_descriptions(workspace):
    local, _, data, _ = workspace
    assert updates.plan_update(data)["strategy"] == "merge_upstream"
    (local / "memes/happy/delete.png").write_bytes(b"user-edited-deleted")
    storage._save_json(local / "memes_data.json", {"happy": "My description"})
    plan = updates.plan_update({**data, "strategy": "merge_upstream"})
    assert plan["ready"]
    assert next(
        row for row in plan["rows"] if row["path"] == "memes/happy/conflict.png"
    )["action"] == "update"
    execute({**data, "strategy": "merge_upstream"})
    assert (local / "memes/happy/conflict.png").read_bytes() == b"new-conflict"
    assert (local / "memes/happy/change.png").read_bytes() == b"new-change"
    assert not (local / "memes/happy/delete.png").exists()
    assert (local / "memes/happy/user.png").is_file()
    assert (local / "memes/happy/renamed.png").is_file()
    assert storage._load_json(local / "memes_data.json", {})["happy"] == "Joy"


def test_upstream_merge_can_keep_deleted_file_and_local_conflict(workspace):
    local, _, data, _ = workspace
    execute(
        {
            **data,
            "strategy": "merge_upstream",
            "keep_deleted": ["memes/happy/delete.png"],
            "choices": {"memes/happy/conflict.png": "local"},
        }
    )
    assert (local / "memes/happy/delete.png").is_file()
    assert (local / "memes/happy/conflict.png").read_bytes() == b"user-edit"


def test_skipping_individual_upstream_images_keeps_existing_and_omits_new(workspace):
    local, _, data, _ = workspace
    skipped = ["memes/happy/conflict.png", "memes/happy/new.png"]
    plan = updates.plan_update({**data, "skip": skipped})
    assert all(
        row["action"] == "keep"
        for row in plan["rows"]
        if row["path"] in skipped
    )
    execute({**data, "skip": skipped})
    assert (local / "memes/happy/conflict.png").read_bytes() == b"user-edit"
    assert not (local / "memes/happy/new.png").exists()
    assert (local / "memes/happy/change.png").read_bytes() == b"new-change"


def test_replace_can_skip_individual_upstream_images(workspace):
    local, _, data, _ = workspace
    plan = updates.plan_update(
        {
            **data,
            "strategy": "replace",
            "skip": ["memes/happy/conflict.png", "memes/happy/new.png"],
        }
    )
    assert next(row for row in plan["rows"] if row["path"].endswith("new.png"))[
        "kind"
    ] == "added"
    assert next(row for row in plan["rows"] if row["path"].endswith("conflict.png"))[
        "kind"
    ] == "changed"
    execute(
        {
            **data,
            "strategy": "replace",
            "skip": ["memes/happy/conflict.png", "memes/happy/new.png"],
            "acknowledge_replace": True,
        }
    )
    assert (local / "memes/happy/conflict.png").read_bytes() == b"user-edit"
    assert not (local / "memes/happy/new.png").exists()
    assert not (local / "memes/happy/user.png").exists()


def test_replace_previews_local_only_deletions_and_can_keep_one(workspace):
    local, _, data, _ = workspace
    request = {
        **data,
        "strategy": "replace",
        "keep_deleted": ["memes/happy/user.png"],
        "acknowledge_replace": True,
    }
    plan = updates.plan_update(request)
    row = next(row for row in plan["rows"] if row["path"].endswith("user.png"))
    assert row["kind"] == "upstream_deleted"
    assert row["local_exists"]
    assert row["action"] == "keep"
    execute(request)
    assert (local / "memes/happy/user.png").is_file()
    assert not (local / "memes/happy/delete.png").exists()


@pytest.mark.asyncio
async def test_update_image_preview_serves_session_image_and_rejects_traversal(
    workspace,
):
    _, remote, data, _ = workspace
    Image.new("RGB", (2, 2), "red").save(remote / "memes/happy/new.png")
    record = storage._load_json(remote.parent / "session.json", {})
    record["remote"] = updates.snapshot(remote)
    storage._save_json(remote.parent / "session.json", record)
    subject = object.__new__(web_api.WebAPIMixin)
    app = Quart(__name__)
    async with app.test_request_context(
        "/?session=" + data["session"] + "&side=upstream&path=memes/happy/new.png"
    ):
        response = await subject._api_community_update_image_data()
    body = await response.get_json()
    assert body["data_url"].startswith("data:image/webp;base64,")
    assert base64.b64decode(body["data_url"].split(",", 1)[1])
    async with app.test_request_context(
        "/?session=" + data["session"] + "&side=upstream&path=memes/../secret.png"
    ):
        _, status = await subject._api_community_update_image_data()
    assert status == 400


def test_add_does_not_replace_and_legacy_defaults_to_upstream_merge(workspace):
    local, _, data, _ = workspace
    (local / updates.ORIGIN_FILE).unlink()
    assert updates.plan_update(data)["strategy"] == "merge_upstream"
    execute({**data, "strategy": "add"})
    assert (local / "memes/happy/change.png").read_bytes() == b"change"
    assert (local / "memes/happy/new.png").is_file()


def test_legacy_upstream_default_replaces_conflicts_and_keeps_local_only(workspace):
    local, _, data, _ = workspace
    (local / updates.ORIGIN_FILE).unlink()
    execute(data)
    assert (local / "memes/happy/conflict.png").read_bytes() == b"new-conflict"
    assert (local / "memes/happy/user.png").is_file()


def test_prepare_reuses_download_for_four_hours_and_refreshes_expired_cache(
    workspace, monkeypatch
):
    _, _, data, record = workspace

    def fail_download(*args, **kwargs):
        raise RuntimeError("download requested")

    monkeypatch.setattr(storage, "_download_github_archive", fail_download)
    assert updates.prepare_update("sample")["session"] == data["session"]
    record["created_at"] = time.time() - 14401
    storage._save_json(
        storage.TEMP_DIR / "community_updates" / data["session"] / "session.json",
        record,
    )
    with pytest.raises(RuntimeError, match="download requested"):
        updates.prepare_update("sample")


@pytest.mark.parametrize("strategy", ["merge", "replace"])
def test_update_keeps_unchanged_embedding_and_marks_new_content_pending(
    workspace, strategy
):
    local, remote, data, record = workspace
    for pack in (local, remote):
        (pack / "memes/happy/same.png").write_bytes(b"unchanged")
    record["remote"] = updates.snapshot(remote)
    storage._save_json(remote.parent / "session.json", record)
    metadata = updates.reconcile_metadata(local)
    for item in metadata["images"].values():
        item.update(caption="A caption", tags=["sample"], caption_status="done")
    updates.save_metadata(local, metadata)
    metadata = updates.reconcile_metadata(local)
    for item in metadata["images"].values():
        item["embedding_status"] = "done"
    updates.save_metadata(local, metadata)
    execute({**data, "strategy": strategy, "acknowledge_replace": True})
    records = {
        item["relative_path"]: item
        for item in updates.load_metadata(local)["images"].values()
    }
    assert records["memes/happy/same.png"]["embedding_status"] == "done"
    assert records["memes/happy/new.png"]["embedding_status"] == "pending"
    assert records["memes/happy/change.png"]["embedding_status"] == "pending"


def test_replace_requires_confirmation_and_removes_local_additions(workspace):
    local, _, data, _ = workspace
    data["strategy"] = "replace"
    with pytest.raises(ValueError, match="完整替换"):
        execute(data)
    assert (local / "memes/happy/user.png").is_file()
    execute({**data, "acknowledge_replace": True})
    assert not (local / "memes/happy/user.png").exists()
    assert (local / "memes/happy/conflict.png").read_bytes() == b"new-conflict"


def test_conflict_both_and_explicit_upstream_deletion(workspace):
    local, _, data, _ = workspace
    execute(
        {
            **data,
            "choices": {"memes/happy/conflict.png": "both"},
            "remove": ["memes/happy/delete.png"],
        }
    )
    assert (local / "memes/happy/conflict.png").read_bytes() == b"user-edit"
    assert (
        local / "memes/happy/conflict_upstream_1.png"
    ).read_bytes() == b"new-conflict"
    assert not (local / "memes/happy/delete.png").exists()


def test_category_conflict_defaults_to_same_category_and_allows_override(workspace):
    local, _, data, _ = workspace
    storage._save_json(local / "memes_data.json", {"happy": "My description"})
    plan = updates.plan_update(data)
    assert plan["ready"]
    assert plan["categories"][0]["target"] == "happy"
    assert not updates.plan_update({**data, "categories": {"happy": ""}})["ready"]
    execute({**data, "strategy": "merge", "categories": {"happy": "new:上游"}})
    assert (local / "memes/上游/new.png").is_file()
    descriptions = json.loads((local / "memes_data.json").read_text(encoding="utf-8"))
    assert descriptions == {"happy": "My description", "上游": "Joy"}


def test_default_category_mapping_keeps_local_description(workspace):
    local, _, data, _ = workspace
    storage._save_json(local / "memes_data.json", {"happy": "My description"})
    execute({**data, "strategy": "merge"})
    assert (local / "memes/happy/new.png").is_file()
    assert (
        storage._load_json(local / "memes_data.json", {})["happy"] == "My description"
    )


def test_stale_preview_and_candidate_change_are_rejected(workspace):
    local, remote, data, _ = workspace
    plan = updates.plan_update(data)
    (local / "memes/happy/user.png").write_bytes(b"changed after preview")
    with pytest.raises(RuntimeError, match="已变化"):
        updates.apply_update({**data, "plan_token": plan["plan_token"]})
    (remote / "memes/happy/new.png").write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="下载内容"):
        updates.plan_update(data)


def test_restore_existing_backup_does_not_create_another_backup(workspace):
    local, _, data, _ = workspace
    backup_id = "b" * 32
    backup = storage.BACKUP_DIR / "community_updates" / backup_id
    shutil.copytree(local, backup / "pack")
    storage._save_json(backup / "backup.json", {"pack_id": "sample", "complete": True})
    execute(data)
    restore = {"backup_id": backup_id}
    plan = updates.restore_update(restore)
    updates.restore_update(
        {
            **restore,
            "preview": False,
            "plan_token": plan["plan_token"],
            "acknowledge_restore": True,
        }
    )
    assert (local / "memes/happy/change.png").read_bytes() == b"change"
    assert len(list(backup.parent.iterdir())) == 1


def test_space_failure_leaves_original_untouched(workspace, monkeypatch):
    local, _, data, _ = workspace
    before = updates.snapshot(local)

    def fail(*args, **kwargs):
        raise OSError("not enough disk space")

    monkeypatch.setattr(storage, "_require_free_space", fail)
    with pytest.raises(OSError):
        execute(data)
    assert updates.snapshot(local) == before


def test_registry_failure_rolls_back_pack_and_vectors(workspace, monkeypatch):
    local, _, data, _ = workspace
    before = updates.snapshot(local)
    index = storage.PLUGIN_DATA_DIR / "semantic_indexes/sample"
    index.mkdir(parents=True)
    (index / "vector.bin").write_bytes(b"index")
    original = storage._save_registry
    calls = []

    def fail_once(registry):
        calls.append(True)
        if len(calls) == 1:
            raise OSError("registry locked")
        original(registry)

    monkeypatch.setattr(storage, "_save_registry", fail_once)
    with pytest.raises(OSError):
        execute(data)
    assert updates.snapshot(local) == before
    assert (index / "vector.bin").read_bytes() == b"index"
    assert storage._load_registry()["installed_packs"][0]["version"] == "1.0"


def test_deleted_directory_never_reports_installed(workspace):
    local, _, _, _ = workspace
    assert updates.community_update_status()["sample"]["available"]
    shutil.rmtree(local)
    assert updates.community_update_status()["sample"] == {"installed": False}
    with pytest.raises(FileNotFoundError):
        updates.prepare_update("sample")


@pytest.mark.parametrize(
    "remote_version,expected",
    [("1.0", False), ("0.9", True), ("1.0.0", True), ("release-x", True), ("", False)],
)
def test_update_availability_only_compares_version_strings(
    workspace, monkeypatch, remote_version, expected
):
    entry = storage.find_cached_pack_entry("sample")
    entry["version"] = remote_version

    def no_download(*args, **kwargs):
        raise AssertionError("Version checks must not download archives")

    monkeypatch.setattr(storage, "_download_github_archive", no_download)
    assert updates.community_update_status()["sample"]["available"] is expected


def test_catalog_fetch_reads_only_manifest_when_version_is_missing(
    tmp_path, monkeypatch
):
    source = {"type": "github", "repo": "owner/repo", "ref": "main", "subpath": "pack"}
    entries = [
        {"id": "known", "version": "3.0", "source": source},
        {"id": "missing", "source": source},
    ]
    calls = []
    monkeypatch.setattr(storage, "validate_community_index", lambda value: value)
    monkeypatch.setattr(storage, "COMMUNITY_CACHE_PATH", tmp_path / "cache.json")

    def fetch(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(
            status_code=200,
            close=lambda: None,
            json=lambda: (
                {"packs": entries}
                if len(calls) == 1
                else {"id": "missing", "version": "2.0"}
            ),
        )

    monkeypatch.setattr(storage, "_http_get_with_optional_acceleration", fetch)
    result = storage.fetch_and_cache_community_index("https://example.test/index.json")
    assert [item["version"] for item in result["index"]["packs"]] == ["3.0", "2.0"]
    assert calls == [
        "https://example.test/index.json",
        "https://raw.githubusercontent.com/owner/repo/main/pack/manifest.json",
    ]


def test_create_named_pack_and_rollback_registration(workspace, monkeypatch):
    result = storage.create_local_pack("我的表情包", "个人收藏")
    manifest = storage._load_json(
        storage.PACKS_DIR / result["pack_id"] / "manifest.json", {}
    )
    assert manifest["name"] == "我的表情包"
    assert manifest["description"] == "个人收藏"
    assert len(storage.list_installed_packs()) == 2
    before = set(storage.PACKS_DIR.iterdir())

    def fail(*args):
        raise OSError("registry locked")

    monkeypatch.setattr(storage, "_save_registry", fail)
    with pytest.raises(OSError):
        storage.create_local_pack("不能写入")
    assert set(storage.PACKS_DIR.iterdir()) == before
    with pytest.raises(ValueError):
        storage.create_local_pack("  ")


def test_prepare_download_validates_and_keeps_local_unchanged(workspace, monkeypatch):
    local, remote, _, _ = workspace
    before = updates.snapshot(local)

    def download(repo, ref, destination, **kwargs):
        with zipfile.ZipFile(destination, "w") as archive:
            for path in remote.rglob("*"):
                if path.is_file():
                    archive.write(
                        path, "repository/pack/" + path.relative_to(remote).as_posix()
                    )

    monkeypatch.setattr(storage, "_download_github_archive", download)
    prepared = updates.prepare_update("sample")
    assert prepared["has_baseline"]
    assert (
        updates.plan_update({"session": prepared["session"]})["latest_version"] == "2.0"
    )
    assert updates.snapshot(local) == before
    installed = storage.install_pack_from_github_source(
        workspace[3]["source"], overwrite=True
    )
    origin = storage._load_json(local / updates.ORIGIN_FILE, {})
    assert installed["pack_id"] == "sample"
    assert origin["source"] == workspace[3]["source"]
    assert origin["files"] == updates.snapshot(remote)["files"]


def test_user_created_empty_pack_is_not_an_install_placeholder(workspace):
    local, _, _, _ = workspace
    shutil.rmtree(local)
    storage.create_local_pack("待整理的收藏")
    assert storage._snapshot_single_empty_pack() is None


def test_cancel_check_cleans_staging_without_modifying_local(workspace, monkeypatch):
    local, _, _, _ = workspace
    before = updates.snapshot(local)

    def download(*args, cancel_check=None, **kwargs):
        assert cancel_check()
        raise storage.InstallCancelledError("cancelled")

    monkeypatch.setattr(storage, "_download_github_archive", download)
    with pytest.raises(storage.InstallCancelledError):
        updates.prepare_update("sample", cancel_check=lambda: True)
    assert updates.snapshot(local) == before
    assert len(list((storage.TEMP_DIR / "community_updates").iterdir())) == 1


@pytest.mark.asyncio
async def test_cancel_api_only_allows_checking_jobs():
    subject = web_api.WebAPIMixin()
    subject._community_update_jobs = {
        "check": {"action": "prepare", "state": "running"},
        "write": {"action": "apply", "state": "running"},
    }
    app = Quart(__name__)
    async with app.test_request_context(
        "/", method="POST", json={"action": "cancel", "job_id": "check"}
    ):
        _, status = await subject._api_community_update()
        assert status == 200
    assert subject._community_update_jobs["check"]["cancel_requested"]
    async with app.test_request_context(
        "/", method="POST", json={"action": "cancel", "job_id": "write"}
    ):
        _, status = await subject._api_community_update()
        assert status == 409
    assert "cancel_requested" not in subject._community_update_jobs["write"]


def test_merge_preserves_manual_semantic_content(workspace):
    local, _, data, _ = workspace
    metadata = updates.reconcile_metadata(local)
    for record in metadata["images"].values():
        if record["relative_path"] == "memes/happy/user.png":
            record.update(
                caption="我的人工描述",
                manual_caption="我的人工描述",
                tags=["个人标签"],
                manual_tags=["个人标签"],
                manual_override=True,
                provenance="manual",
                caption_status="done",
            )
    updates.save_metadata(local, metadata)
    execute(data)
    record = next(
        item
        for item in updates.load_metadata(local)["images"].values()
        if item["relative_path"] == "memes/happy/user.png"
    )
    assert record["manual_caption"] == "我的人工描述"
    assert record["manual_tags"] == ["个人标签"]


@pytest.mark.asyncio
async def test_update_job_applies_under_locks_and_reports_completion(
    workspace, monkeypatch
):
    local, _, data, _ = workspace
    monkeypatch.setattr(web_api, "PACKS_DIR", storage.PACKS_DIR)
    active = set()
    subject = web_api.WebAPIMixin()
    subject.semantic_task_manager = SimpleNamespace(
        begin_external_pack_operation=lambda pack, _: active.add(pack),
        end_external_pack_operation=lambda pack: active.remove(pack),
    )
    subject._reload_personas = Mock()
    original = updates.apply_update

    def guarded(payload, **kwargs):
        assert active == {"sample"}
        return original(payload, **kwargs)

    monkeypatch.setattr(web_api, "apply_update", guarded)
    plan = updates.plan_update(data)
    app = Quart(__name__)
    async with app.test_request_context(
        "/",
        method="POST",
        json={**data, "action": "apply", "plan_token": plan["plan_token"]},
    ):
        response, code = await subject._api_community_update()
        assert code == 202
        job_id = (await response.get_json())["job_id"]
    await asyncio.gather(*subject._community_update_tasks)
    assert not active
    assert subject._community_update_jobs[job_id]["state"] == "completed"
    assert updates.snapshot(local)["version"] == "2.0"
    subject._reload_personas.assert_called_once()


@pytest.mark.asyncio
async def test_runtime_operations_serialize_when_no_pack_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(web_api, "PACKS_DIR", tmp_path)
    subject = web_api.WebAPIMixin()
    subject._semantic_operation_guard = lambda *_: None
    registry = []

    def create(name, operation_guard=None):
        snapshot = registry.copy()
        time.sleep(0.03)
        registry[:] = snapshot + [name]

    await asyncio.gather(
        *[
            subject._run_guarded_runtime_file_operation("create", create, name)
            for name in ("first", "second")
        ]
    )
    assert registry == ["first", "second"]
