import asyncio
import json
import threading
from pathlib import Path

import pytest
from astrbot_plugin_meme_manager.backend.packs import storage as packs
from astrbot_plugin_meme_manager.backend.semantic import storage
from astrbot_plugin_meme_manager.backend.semantic import task as task_module
from astrbot_plugin_meme_manager.backend.semantic.task import SemanticTaskManager


@pytest.mark.parametrize("metadata_kind", ["missing", "current", "legacy"])
def test_page_queries_do_not_read_images_or_create_metadata(
    tmp_path, monkeypatch, metadata_kind
):
    manager = SemanticTaskManager(tmp_path)
    pack_dir = tmp_path / "packs" / "pack-a"
    category = pack_dir / "memes" / "happy"
    category.mkdir(parents=True)
    image_path = category / "meme.png"
    image_path.write_bytes(b"image")
    metadata_path = pack_dir / "semantic_metadata.json"
    original = None
    if metadata_kind != "missing":
        data = storage.reconcile_metadata(pack_dir)
        for item in data["images"].values():
            item.update(caption="Happy cat", tags=["happy"], caption_status="done")
        storage.save_metadata(pack_dir, data)
        if metadata_kind == "legacy":
            data["schema_version"] = "1"
            metadata_path.write_text(json.dumps(data), encoding="utf-8")
        original = metadata_path.read_bytes()

    monkeypatch.setattr(packs, "PACKS_DIR", tmp_path / "packs")
    monkeypatch.setattr(
        packs, "_load_registry", lambda: {"installed_packs": [{"id": "pack-a"}]}
    )
    monkeypatch.setattr(packs, "_current_default_pack_id", lambda: "pack-a")
    monkeypatch.setattr(packs, "_load_manifest", lambda pack_id: {})
    original_open = Path.open

    def guard_image_open(path, *args, **kwargs):
        if path.suffix == ".png":
            pytest.fail("Page queries must not read image contents")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guard_image_open)
    for _ in range(2):
        listed = packs.list_installed_packs()
        assert listed[0]["image_count"] == 1
        detail = packs.get_pack_detail("pack-a")
        assert detail["total_images"] == 1
        status = manager.status("pack-a")
        items = storage.metadata_items(pack_dir)
        assert status["can_start"]
        if metadata_kind == "current":
            assert listed[0]["semantic_status"] == "complete"
            assert len(items) == 1
        elif metadata_kind == "legacy":
            assert status["queue_status"] == "migration_required"
            assert not items
        else:
            assert status["queue_status"] == "empty"
            assert not items
    assert (metadata_path.read_bytes() if metadata_path.exists() else None) == original
    assert not (pack_dir / storage.LEGACY_METADATA_BACKUP_NAME).exists()


def test_saved_summary_does_not_weaken_runtime_content_verification(tmp_path):
    category = tmp_path / "memes" / "happy"
    category.mkdir(parents=True)
    image_path = category / "meme.png"
    image_path.write_bytes(b"first")
    data = storage.reconcile_metadata(tmp_path)
    for item in data["images"].values():
        item.update(caption="Happy cat", tags=["happy"], caption_status="done")
    storage.save_metadata(tmp_path, data)
    assert storage.semantic_metadata_is_complete(tmp_path)
    image_path.write_bytes(b"other")
    assert (
        storage.get_pack_semantic_summary(tmp_path, verify_files=False)[
            "semantic_status"
        ]
        == "complete"
    )
    assert not storage.semantic_metadata_is_complete(tmp_path)
    assert storage.get_pack_semantic_summary(tmp_path)["semantic_files_changed"]
    (category / "new.png").write_bytes(b"new")
    assert storage.get_pack_semantic_summary(tmp_path, verify_files=False)[
        "semantic_files_changed"
    ]


@pytest.mark.asyncio
async def test_explicit_start_scans_off_loop_and_preserves_existing_captions(
    tmp_path, monkeypatch
):
    manager = SemanticTaskManager(tmp_path)
    pack_dir = tmp_path / "packs" / "pack-a"
    category = pack_dir / "memes" / "happy"
    category.mkdir(parents=True)
    (category / "meme.png").write_bytes(b"image")
    data = storage.reconcile_metadata(pack_dir)
    for item in data["images"].values():
        item.update(caption="Existing caption", tags=["happy"], caption_status="done")
    storage.save_metadata(pack_dir, data)
    (category / "new.png").write_bytes(b"new")
    loop_thread = threading.get_ident()
    scanned = threading.Event()
    release = threading.Event()
    reconcile = storage.reconcile_metadata

    def blocking_scan(*args, **kwargs):
        assert threading.get_ident() != loop_thread
        scanned.set()
        assert release.wait(5)
        return reconcile(*args, **kwargs)

    async def no_model_calls(*args, **kwargs):
        return None

    monkeypatch.setattr(task_module, "reconcile_metadata", blocking_scan)
    monkeypatch.setattr(manager, "_run", no_model_calls)
    monkeypatch.setattr(manager, "_vision_provider_ready", lambda: True)
    started = asyncio.create_task(manager.start("pack-a", mode="caption_only"))
    try:
        assert await asyncio.to_thread(scanned.wait, 5)
        assert not started.done()
        assert manager._lock("pack-a").locked()
    finally:
        release.set()
    await started
    metadata = storage.load_metadata(pack_dir)
    assert len(metadata["images"]) == 2
    assert any(
        item["caption"] == "Existing caption" for item in metadata["images"].values()
    )
    await manager.close()
