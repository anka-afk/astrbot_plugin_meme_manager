import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_meme_manager.backend.packs import transfer
from astrbot_plugin_meme_manager.mixins import web_api
from quart import Quart


@pytest.fixture
def packs(tmp_path):
    for pack_id, categories in (
        ("source", {"happy": "Joy", "sad": "Sadness", "new": "New"}),
        ("target", {"happy": " Joy\n", "sad": "Different", "other": "Other"}),
    ):
        pack = tmp_path / pack_id
        pack.mkdir()
        (pack / "memes_data.json").write_text(json.dumps(categories), encoding="utf-8")
        (pack / "manifest.json").write_text(
            json.dumps(
                {
                    "id": pack_id,
                    "categories": {
                        key: {"description": value} for key, value in categories.items()
                    },
                }
            ),
            encoding="utf-8",
        )
        for category in categories:
            (pack / "memes" / category).mkdir(parents=True)
    for category in ("happy", "sad", "new"):
        (tmp_path / "source" / "memes" / category / "test.png").write_bytes(
            category.encode()
        )
    return tmp_path


def payload(*categories, **kwargs):
    return {
        "source_pack_id": "source",
        "target_pack_id": "target",
        "mode": "preserve",
        "items": [
            {"category": category, "emoji": "test.png"} for category in categories
        ],
        **kwargs,
    }


def execute(packs, data):
    preview = transfer.transfer_pack_images(packs, data)
    return transfer.transfer_pack_images(
        packs, {**data, "preview": False, "plan_token": preview["plan_token"]}
    )


def test_preview_detects_all_category_cases_without_mutation(packs):
    result = transfer.transfer_pack_images(packs, payload("happy", "sad", "new"))
    actions = {row["source_category"]: row["action"] for row in result["plan"]}
    assert actions == {"happy": "existing", "sad": "choose", "new": "create"}
    assert not result["ready"]
    assert not (packs / "target/memes/new").exists()
    assert (packs / "source/memes/happy/test.png").exists()


def test_preserve_copies_description_and_keeps_source_category(packs):
    result = execute(packs, payload("new", "happy"))
    assert len(result["moved"]) == 2
    assert (packs / "source/memes/new").is_dir()
    assert not (packs / "source/memes/new/test.png").exists()
    assert (packs / "target/memes/new/test.png").read_bytes() == b"new"
    assert json.loads((packs / "target/memes_data.json").read_text())["new"] == "New"
    assert (
        json.loads((packs / "target/manifest.json").read_text())["categories"]["new"][
            "description"
        ]
        == "New"
    )


def test_explicit_target_ignores_source_category_and_never_overwrites(packs):
    result = execute(
        packs, payload("happy", "sad", mode="target", target_category="other")
    )
    assert len(result["moved"]) == len(result["failed"]) == 1
    assert (packs / "target/memes/other/test.png").read_bytes() == b"happy"
    assert (packs / "source/memes/sad/test.png").read_bytes() == b"sad"
    assert (
        json.loads((packs / "target/memes_data.json").read_text())["other"] == "Other"
    )


def test_conflict_requires_explicit_existing_category(packs):
    with pytest.raises(RuntimeError):
        execute(packs, payload("sad"))
    with pytest.raises(ValueError):
        execute(packs, payload("sad", category_mapping={"sad": "new-category"}))
    result = execute(packs, payload("sad", category_mapping={"sad": "sad"}))
    assert len(result["moved"]) == 1
    assert (
        json.loads((packs / "target/memes_data.json").read_text())["sad"] == "Different"
    )


def test_stale_plan_rejected_before_any_image_moves(packs):
    data = payload("happy", "new")
    plan = transfer.transfer_pack_images(packs, data)
    (packs / "target/memes_data.json").write_text(json.dumps({"happy": "Changed"}))
    with pytest.raises(RuntimeError):
        transfer.transfer_pack_images(
            packs, {**data, "preview": False, "plan_token": plan["plan_token"]}
        )
    assert (packs / "source/memes/new/test.png").exists()
    assert not (packs / "target/memes/new").exists()


@pytest.mark.parametrize(
    "override",
    [
        {"source_pack_id": "../source"},
        {"target_pack_id": "source"},
        {"mode": "invalid"},
        {"items": []},
        {"preview": "false"},
        {"items": [{"category": "../happy", "emoji": "test.png"}]},
        {"items": [{"category": "happy", "emoji": "../test.png"}]},
        {"items": [{"category": "happy", "emoji": "test.png:secret.png"}]},
        {"target_category": "missing", "mode": "target"},
    ],
)
def test_invalid_requests_do_not_modify_source(packs, override):
    with pytest.raises(ValueError):
        transfer.transfer_pack_images(packs, payload("happy", **override))
    assert (packs / "source/memes/happy/test.png").exists()


def test_copy_failure_preserves_source_and_removes_incomplete_destination(
    packs, monkeypatch
):
    def fail(incoming, outgoing):
        outgoing.write(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(transfer.shutil, "copyfileobj", fail)
    result = execute(packs, payload("happy"))
    assert len(result["failed"]) == 1
    assert (packs / "source/memes/happy/test.png").read_bytes() == b"happy"
    assert not (packs / "target/memes/happy/test.png").exists()


def test_missing_file_does_not_block_other_files(packs):
    (packs / "source/memes/happy/test.png").unlink()
    result = execute(packs, payload("happy", "new"))
    assert len(result["failed"]) == len(result["moved"]) == 1


def test_source_delete_failure_rolls_back_destination(packs, monkeypatch):
    original = Path.unlink

    def fail_source(path, *args, **kwargs):
        if path == packs / "source/memes/happy/test.png":
            raise PermissionError("source locked")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_source)
    result = execute(packs, payload("happy"))
    assert not result["moved"]
    assert len(result["failed"]) == 1
    assert not (packs / "target/memes/happy/test.png").exists()
    assert (packs / "source/memes/happy/test.png").exists()


def test_manifest_only_categories_are_available(packs):
    (packs / "target/memes_data.json").unlink()
    result = transfer.transfer_pack_images(packs, payload("happy"))
    assert result["ready"]
    assert result["plan"][0]["action"] == "existing"


@pytest.mark.asyncio
async def test_cancelled_request_retains_locks_until_worker_finishes(
    packs, monkeypatch
):
    monkeypatch.setattr(web_api, "PACKS_DIR", packs)
    monkeypatch.setattr(
        web_api, "list_installed_packs", lambda: [{"id": "source"}, {"id": "target"}]
    )
    started = asyncio.Event()
    finish = threading.Event()
    loop = asyncio.get_running_loop()

    def worker(*args):
        loop.call_soon_threadsafe(started.set)
        finish.wait(timeout=5)
        return {"moved": [], "failed": [], "warnings": []}

    monkeypatch.setattr(web_api, "transfer_pack_images", worker)
    subject = web_api.WebAPIMixin()
    subject.semantic_task_manager = SimpleNamespace(
        begin_external_pack_operation=Mock(), end_external_pack_operation=Mock()
    )
    app = Quart(__name__)
    async with app.test_request_context("/", method="POST", json=payload("happy")):
        task = asyncio.create_task(subject._api_transfer_pack_images())
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        await asyncio.sleep(0)
        subject.semantic_task_manager.end_external_pack_operation.assert_not_called()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert subject.semantic_task_manager.end_external_pack_operation.call_count == 2


def test_metadata_failure_happens_before_source_deletion(packs, monkeypatch):
    def fail(*args):
        raise OSError("read only")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError):
        execute(packs, payload("new"))
    assert (packs / "source/memes/new/test.png").exists()
    assert not (packs / "target/memes/new/test.png").exists()


@pytest.mark.asyncio
async def test_api_holds_both_locks_and_releases_after_failure(packs, monkeypatch):
    monkeypatch.setattr(web_api, "PACKS_DIR", packs)
    monkeypatch.setattr(
        web_api, "list_installed_packs", lambda: [{"id": "source"}, {"id": "target"}]
    )
    subject = web_api.WebAPIMixin()
    active = set()
    subject.semantic_task_manager = SimpleNamespace(
        begin_external_pack_operation=Mock(
            side_effect=lambda pack, _: active.add(pack)
        ),
        end_external_pack_operation=Mock(side_effect=lambda pack: active.remove(pack)),
    )
    subject.reload_emotions = AsyncMock()
    subject._resolve_runtime_pack_context = lambda: {"pack_id": "source"}
    original = web_api.transfer_pack_images

    def guarded(*args):
        assert active == {"source", "target"}
        return original(*args)

    monkeypatch.setattr(web_api, "transfer_pack_images", guarded)
    app = Quart(__name__)
    async with app.test_request_context("/", method="POST", json=payload("happy")):
        response, status = await subject._api_transfer_pack_images()
        assert status == 200
        plan = await response.get_json()
    assert not active
    async with app.test_request_context(
        "/",
        method="POST",
        json=payload("happy", preview=False, plan_token=plan["plan_token"]),
    ):
        _, status = await subject._api_transfer_pack_images()
        assert status == 200
    subject.reload_emotions.assert_awaited_once()
    assert not active
    subject.semantic_task_manager.begin_external_pack_operation.side_effect = [
        None,
        RuntimeError("busy"),
    ]
    subject.semantic_task_manager.end_external_pack_operation = Mock()
    async with app.test_request_context("/", method="POST", json=payload("happy")):
        _, status = await subject._api_transfer_pack_images()
        assert status == 409
    subject.semantic_task_manager.end_external_pack_operation.assert_called_once_with(
        "source"
    )
