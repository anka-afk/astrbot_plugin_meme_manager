import json
from unittest.mock import Mock

import pytest
from astrbot_plugin_meme_manager.backend.semantic import index as index_module
from astrbot_plugin_meme_manager.backend.semantic import storage
from astrbot_plugin_meme_manager.backend.semantic.index import (
    EmbeddingAdapter,
    build_index,
    index_is_ready,
    search_index,
)
from astrbot_plugin_meme_manager.backend.semantic.models import (
    PROMPT_VERSION,
    semantic_caption_is_complete,
)
from PIL import Image


class _Provider:
    provider_config = {"id": "embedding", "embedding_model": "test-model"}

    @staticmethod
    def get_dim():
        return 2

    async def get_embedding(self, text):
        return [1.0, 0.0]

    async def get_embeddings(self, texts):
        return [[1.0, 0.0] for _ in texts]


def _decision(caption):
    return {
        "caption": caption,
        "tags": ["joy"],
        "semantic_prompt_version": PROMPT_VERSION,
        "semantic_category": "happy",
    }


@pytest.mark.parametrize(
    ("version", "category", "complete"),
    [
        (PROMPT_VERSION, "happy", True),
        ("old", "happy", False),
        (PROMPT_VERSION, "sad", False),
    ],
)
def test_collection_analysis_reuse_requires_matching_semantic_contract(
    tmp_path, version, category, complete
):
    pack = tmp_path / "pack"
    source = pack / "memes" / "happy" / "new.png"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (8, 8), "red").save(source)
    (pack / "memes_data.json").write_text(
        json.dumps({"happy": "Express happiness"}), encoding="utf-8"
    )
    decision = {
        "caption": "A cheerful reaction to good news.",
        "tags": ["celebration", "good news"],
        "visible_text": "yay",
        "semantic_prompt_version": version,
        "semantic_category": category,
        "vision_model": "collection-vision",
    }
    storage.save_collected_image_semantic(pack, source, decision, manual_confirmed=True)
    metadata = storage.load_metadata(pack)
    item = next(iter(metadata["images"].values()))
    assert semantic_caption_is_complete(item) is complete
    assert item["auto_caption"] == decision["caption"]
    assert item["auto_tags"] == decision["tags"]
    assert item["category_review_status"] == "manual_confirmed"
    assert item["embedding_status"] == "pending"
    assert metadata["requires_local_index_rebuild"] is True
    reconciled = storage.reconcile_metadata(pack)
    assert (
        semantic_caption_is_complete(next(iter(reconciled["images"].values())))
        is complete
    )


def test_collection_analysis_rejects_image_outside_pack(tmp_path):
    pack = tmp_path / "pack"
    (pack / "memes").mkdir(parents=True)
    source = tmp_path / "outside.png"
    Image.new("RGB", (8, 8), "blue").save(source)
    with pytest.raises(ValueError):
        storage.save_collected_image_semantic(pack, source, {})


def test_collection_acceptance_does_not_rescan_existing_library(tmp_path, monkeypatch):
    pack = tmp_path / "pack"
    category = pack / "memes" / "happy"
    category.mkdir(parents=True)
    (pack / "memes_data.json").write_text('{"happy":"Joy"}', encoding="utf-8")
    scan = Mock(side_effect=AssertionError("Collection must not rehash the library"))
    monkeypatch.setattr(storage, "scan_images", scan)
    for name, color in (("first", "red"), ("second", "blue")):
        source = category / f"{name}.png"
        Image.new("RGB", (8, 8), color).save(source)
        storage.save_collected_image_semantic(pack, source, {})
    assert len(storage.load_metadata(pack)["images"]) == 2
    scan.assert_not_called()


def test_collection_saves_refresh_inventory_without_inflating_duplicates(tmp_path):
    pack = tmp_path / "pack"
    happy = pack / "memes" / "happy"
    sad = pack / "memes" / "sad"
    happy.mkdir(parents=True)
    sad.mkdir()
    shared_bytes = b"same-image-bytes"
    paths = []
    for path in (happy / "first.png", happy / "second.png", sad / "third.png"):
        path.write_bytes(shared_bytes)
        storage.save_collected_image_semantic(pack, path, {})
        paths.append(path)

    metadata = storage.load_metadata(pack)
    assert len(metadata["images"]) == 3
    assert metadata["file_total"] == 3
    assert metadata["unique_total"] == 3
    assert metadata["content_unique_total"] == 1
    assert metadata["reused_duplicate_files"] == 1
    assert metadata["cross_category_duplicate_entries"] == 1

    storage.save_collected_image_semantic(pack, paths[0], {})
    repeated = storage.load_metadata(pack)
    assert len(repeated["images"]) == 3
    assert repeated["file_total"] == 3
    assert repeated["unique_total"] == 3
    assert repeated["content_unique_total"] == 1


@pytest.mark.asyncio
async def test_complete_summary_after_collection_and_incremental_faiss_build(tmp_path):
    pack = tmp_path / "pack"
    category = pack / "memes" / "happy"
    category.mkdir(parents=True)
    (pack / "memes_data.json").write_text('{"happy":"Joy"}', encoding="utf-8")
    first = category / "first.png"
    Image.new("RGB", (8, 8), "red").save(first)
    storage.save_collected_image_semantic(pack, first, _decision("First joy"))
    assert storage.load_metadata(pack)["file_total"] == 1

    embedding = EmbeddingAdapter(_Provider())
    runtime = tmp_path / "runtime"
    await build_index(pack, runtime, "pack", embedding)

    second = category / "second.png"
    Image.new("RGB", (8, 8), "blue").save(second)
    storage.save_collected_image_semantic(pack, second, _decision("Second joy"))
    after_collection = storage.load_metadata(pack)
    assert len(after_collection["images"]) == 2
    assert after_collection["file_total"] == 2

    await build_index(pack, runtime, "pack", embedding, incremental_only=True)
    metadata = storage.load_metadata(pack)
    summary = storage.get_pack_semantic_summary(pack, verify_files=False)
    assert summary["semantic_status"] == "complete"
    assert summary["semantic_snapshot_matches"]
    assert not summary["semantic_files_changed"]
    assert summary["semantic_caption_total"] == 2
    strict_summary = storage.get_pack_semantic_summary(pack)
    assert strict_summary["semantic_status"] == "complete"
    assert strict_summary["semantic_snapshot_matches"]
    assert not strict_summary["semantic_files_changed"]
    assert index_is_ready(runtime, "pack", metadata)


@pytest.mark.asyncio
async def test_incremental_rebuild_repairs_historical_stale_inventory_counters(
    tmp_path,
):
    pack = tmp_path / "pack"
    category = pack / "memes" / "happy"
    category.mkdir(parents=True)
    (pack / "memes_data.json").write_text('{"happy":"Joy"}', encoding="utf-8")
    embedding = EmbeddingAdapter(_Provider())
    runtime = tmp_path / "runtime"
    for name, color, caption in (
        ("first.png", "red", "First joy"),
        ("second.png", "blue", "Second joy"),
    ):
        source = category / name
        Image.new("RGB", (8, 8), color).save(source)
        storage.save_collected_image_semantic(pack, source, _decision(caption))
    await build_index(pack, runtime, "pack", embedding)

    metadata_file = storage.metadata_path(pack)
    stored = json.loads(metadata_file.read_text(encoding="utf-8"))
    stored.update(
        {
            "file_total": 1,
            "unique_total": 1,
            "content_unique_total": 1,
            "reused_duplicate_files": 0,
            "cross_category_duplicate_entries": 0,
        }
    )
    metadata_file.write_text(json.dumps(stored), encoding="utf-8")
    stale = storage.get_pack_semantic_summary(pack, verify_files=False)
    assert stale["semantic_status"] == "partial"
    assert not stale["semantic_snapshot_matches"]
    assert stale["semantic_files_changed"]

    await build_index(pack, runtime, "pack", embedding, incremental_only=True)

    repaired = storage.load_metadata(pack)
    assert repaired["file_total"] == 2
    assert repaired["unique_total"] == 2
    assert repaired["content_unique_total"] == 2
    repaired_summary = storage.get_pack_semantic_summary(pack, verify_files=False)
    assert repaired_summary["semantic_status"] == "complete"
    assert repaired_summary["semantic_snapshot_matches"]
    assert not repaired_summary["semantic_files_changed"]


@pytest.mark.asyncio
async def test_inventory_refresh_failure_keeps_published_manifest(
    tmp_path, monkeypatch
):
    pack = tmp_path / "pack"
    category = pack / "memes" / "happy"
    category.mkdir(parents=True)
    (pack / "memes_data.json").write_text('{"happy":"Joy"}', encoding="utf-8")
    first = category / "first.png"
    Image.new("RGB", (8, 8), "red").save(first)
    storage.save_collected_image_semantic(pack, first, _decision("First joy"))

    embedding = EmbeddingAdapter(_Provider())
    runtime = tmp_path / "runtime"
    await build_index(pack, runtime, "pack", embedding)
    manifest_path = index_module.index_manifest_path(runtime, "pack")
    previous_manifest = manifest_path.read_bytes()

    second = category / "second.png"
    Image.new("RGB", (8, 8), "blue").save(second)
    storage.save_collected_image_semantic(pack, second, _decision("Second joy"))

    def fail_inventory_refresh(*_args, **_kwargs):
        raise OSError("inventory scan failed")

    monkeypatch.setattr(
        index_module, "refresh_metadata_inventory_snapshot", fail_inventory_refresh
    )
    with pytest.raises(OSError, match="inventory scan failed"):
        await build_index(pack, runtime, "pack", embedding)

    assert manifest_path.read_bytes() == previous_manifest
    metadata = storage.load_metadata(pack)
    results = await search_index(runtime, "pack", "joy", embedding, metadata)
    assert [result["caption"] for result in results] == ["First joy"]


def test_inventory_snapshot_keeps_incomplete_or_failed_captions_partial(tmp_path):
    pack = tmp_path / "pack"
    category = pack / "memes" / "happy"
    category.mkdir(parents=True)
    (pack / "memes_data.json").write_text('{"happy":"Joy"}', encoding="utf-8")
    complete = category / "complete.png"
    Image.new("RGB", (8, 8), "red").save(complete)
    storage.save_collected_image_semantic(pack, complete, _decision("Complete joy"))
    incomplete = category / "incomplete.png"
    Image.new("RGB", (8, 8), "blue").save(incomplete)
    storage.save_collected_image_semantic(pack, incomplete, {})

    metadata = storage.load_metadata(pack)
    assert metadata["file_total"] == 2
    pending_summary = storage.get_pack_semantic_summary(pack, verify_files=False)
    assert pending_summary["semantic_status"] == "partial"
    assert pending_summary["semantic_snapshot_matches"]
    assert pending_summary["semantic_caption_done"] == 1
    assert not pending_summary["semantic_files_changed"]

    for item in metadata["images"].values():
        if item.get("caption_status") != "done":
            item["caption_status"] = "failed"
            item["error"] = "vision failed"
    storage.refresh_metadata_inventory_snapshot(pack, metadata)
    assert metadata["file_total"] == 2
    storage.save_metadata(pack, metadata)

    failed_summary = storage.get_pack_semantic_summary(pack, verify_files=False)
    assert failed_summary["semantic_status"] == "partial"
    assert failed_summary["semantic_caption_failed"] == 1
    assert failed_summary["semantic_snapshot_matches"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broken_index",
    ["unreadable", "missing_manifest", "invalid_manifest", "unreadable_vector"],
)
async def test_incremental_collection_reuses_vectors_and_keeps_existing_search(
    tmp_path, monkeypatch, broken_index
):
    from unittest.mock import AsyncMock

    from astrbot_plugin_meme_manager.backend.semantic.index import (
        EmbeddingAdapter,
        build_index,
        index_is_ready,
        search_index,
    )
    from astrbot_plugin_meme_manager.backend.semantic.task import SemanticTaskManager

    class Provider:
        provider_config = {"id": "embedding", "embedding_model": "test-model"}
        get_dim = staticmethod(lambda: 2)
        get_embedding = AsyncMock(return_value=[1.0, 0.0])
        get_embeddings = AsyncMock(
            side_effect=lambda texts: [[1.0, 0.0] for _ in texts]
        )

    pack = tmp_path / "pack"
    category = pack / "memes" / "happy"
    category.mkdir(parents=True)
    (pack / "memes_data.json").write_text('{"happy":"Joy"}', encoding="utf-8")
    decision = {
        "caption": "A happy reaction",
        "tags": ["joy"],
        "semantic_prompt_version": PROMPT_VERSION,
        "semantic_category": "happy",
    }
    first = category / "first.png"
    Image.new("RGB", (8, 8), "red").save(first)
    storage.save_collected_image_semantic(pack, first, decision)
    provider = Provider()
    embedding = EmbeddingAdapter(provider)
    runtime = tmp_path / "runtime"
    await build_index(pack, runtime, "pack", embedding)
    provider.get_embeddings.reset_mock()

    second = category / "second.png"
    Image.new("RGB", (8, 8), "blue").save(second)
    storage.save_collected_image_semantic(
        pack, second, {**decision, "caption": "New joy"}
    )
    unfinished = category / "unfinished.png"
    Image.new("RGB", (8, 8), "green").save(unfinished)
    storage.save_collected_image_semantic(pack, unfinished, {})
    metadata = storage.load_metadata(pack)
    assert index_is_ready(runtime, "pack", metadata, allow_partial=True)
    assert not index_is_ready(runtime, "pack", metadata)
    before = await search_index(runtime, "pack", "joy", embedding, metadata)
    assert len(before) == 1
    assert before[0]["caption"] == decision["caption"]

    manager = SemanticTaskManager(runtime)
    manager._pack_dir = lambda pack_id: pack
    manager._require_embedding_provider = lambda *args: embedding
    manager._verify_embedding_dimension = AsyncMock()
    await manager.rebuild_index("pack", force=False)
    provider.get_embeddings.assert_awaited_once()
    assert len(provider.get_embeddings.await_args.args[0]) == 1
    metadata = storage.load_metadata(pack)
    after = await search_index(runtime, "pack", "joy", embedding, metadata)
    assert len(after) == 2
    assert index_is_ready(runtime, "pack", metadata, allow_partial=True)
    with pytest.raises(RuntimeError, match="描述未完成"):
        await manager.rebuild_index("pack", force=True)
    provider.provider_config = {"id": "other", "embedding_model": "test-model"}
    embedding = EmbeddingAdapter(provider)
    with pytest.raises(RuntimeError, match="全量重建"):
        await manager.rebuild_index("pack", force=False)
    provider.provider_config = {"id": "embedding", "embedding_model": "test-model"}
    embedding = EmbeddingAdapter(provider)
    next(item for item in metadata["images"].values() if item.get("caption"))[
        "caption"
    ] = "Stale entry"
    # top_k=1 时，排名靠前的过时向量不能遮蔽剩余有效条目。
    remaining = await search_index(runtime, "pack", "joy", embedding, metadata, top_k=1)
    assert len(remaining) == 1
    assert remaining[0]["caption"] != "Stale entry"
    # 语义文本变化后，过时向量不得再返回该条目。
    for item in metadata["images"].values():
        if item.get("caption"):
            item["caption"] = "Changed text"
    assert not index_is_ready(runtime, "pack", metadata, allow_partial=True)
    provider.provider_config = {"id": "embedding", "embedding_model": "test-model"}
    embedding = EmbeddingAdapter(provider)
    assert await search_index(runtime, "pack", "joy", embedding, metadata) == []

    from astrbot_plugin_meme_manager.backend.semantic import index as index_module

    provider.get_embeddings.reset_mock()
    if broken_index == "unreadable":
        monkeypatch.setattr(index_module, "_read_faiss_index", lambda *args: None)
    elif broken_index == "missing_manifest":
        index_module.index_manifest_path(runtime, "pack").unlink()
    elif broken_index == "invalid_manifest":
        index_module.index_manifest_path(runtime, "pack").write_text(
            "invalid", encoding="utf-8"
        )
    else:
        monkeypatch.setattr(
            index_module, "_reconstruct_reusable_vectors", lambda *args: {}
        )
    with pytest.raises(RuntimeError, match="全量重建"):
        await manager.rebuild_index("pack", force=False)
    provider.get_embeddings.assert_not_awaited()
