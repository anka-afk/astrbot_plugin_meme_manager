import hashlib
import json

import pytest
from astrbot_plugin_meme_manager.backend import semantic_storage as storage
from astrbot_plugin_meme_manager.backend.semantic_models import (
    SemanticImage,
    category_analysis_is_current,
)


def create_pack(tmp_path):
    pack_dir = tmp_path / "pack"
    memes_dir = pack_dir / "memes"
    memes_dir.mkdir(parents=True)
    return pack_dir, memes_dir


def test_metadata_path_and_safe_relative_path(tmp_path):
    pack_dir, _ = create_pack(tmp_path)
    assert storage.metadata_path(pack_dir) == pack_dir / "semantic_metadata.json"
    assert storage.safe_relative_path(pack_dir, "memes/happy.png") == (
        pack_dir / "memes" / "happy.png"
    )
    assert storage.safe_relative_path(pack_dir, "../secret") is None
    assert storage.safe_relative_path(pack_dir, str(tmp_path.resolve())) is None
    assert storage.safe_relative_path(pack_dir, "") is None


def test_file_sha256_reads_in_chunks(tmp_path):
    path = tmp_path / "data.bin"
    path.write_bytes(b"abcdef")
    assert (
        storage.file_sha256(path, chunk_size=2) == hashlib.sha256(b"abcdef").hexdigest()
    )


def test_load_category_descriptions_merges_metadata_and_manifest(tmp_path):
    pack_dir, _ = create_pack(tmp_path)
    (pack_dir / "memes_data.json").write_text(
        json.dumps({"happy": "可编辑描述", "": "忽略"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (pack_dir / "manifest.json").write_text(
        json.dumps(
            {
                "categories": {
                    "happy": {"description": "清单描述"},
                    "sad": {"description": "伤心"},
                    "empty": {},
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert storage.load_category_descriptions(pack_dir) == {
        "happy": "可编辑描述",
        "sad": "伤心",
        "empty": "请添加描述",
    }


def test_load_category_descriptions_ignores_corrupt_files(tmp_path):
    pack_dir, _ = create_pack(tmp_path)
    (pack_dir / "memes_data.json").write_text("not-json", encoding="utf-8")
    (pack_dir / "manifest.json").write_text("not-json", encoding="utf-8")
    assert storage.load_category_descriptions(pack_dir) == {}


def test_scan_images_keeps_duplicate_content_as_distinct_entries(tmp_path):
    pack_dir, memes_dir = create_pack(tmp_path)
    for category in ("happy", "sad"):
        category_dir = memes_dir / category
        category_dir.mkdir()
        (category_dir / "same.png").write_bytes(b"same")
        (category_dir / "ignore.txt").write_bytes(b"same")
    images = storage.scan_images(pack_dir)
    assert len(images) == 2
    assert images[0]["content_sha256"] == images[1]["content_sha256"]
    assert images[0]["entry_id"] != images[1]["entry_id"]
    assert {item["category"] for item in images} == {"happy", "sad"}


def test_scan_images_returns_empty_without_memes_directory(tmp_path):
    assert storage.scan_images(tmp_path / "missing") == []


def test_save_and_load_empty_metadata_round_trip(tmp_path):
    pack_dir, _ = create_pack(tmp_path)
    target = storage.save_metadata(pack_dir, {"images": {}})
    assert target.is_file()
    loaded = storage.load_metadata(pack_dir)
    assert loaded["schema_version"] == storage.SCHEMA_VERSION
    assert loaded["pack_id"] == "pack"
    assert loaded["images"] == {}


def test_load_metadata_returns_new_payload_when_file_missing(tmp_path):
    pack_dir, _ = create_pack(tmp_path)
    loaded = storage.load_metadata(pack_dir)
    assert loaded["pack_id"] == "pack"
    assert loaded["images"] == {}


def test_load_metadata_keeps_corrupt_or_future_file_read_only(tmp_path):
    pack_dir, _ = create_pack(tmp_path)
    metadata_file = pack_dir / "semantic_metadata.json"
    metadata_file.write_text("not-json", encoding="utf-8")
    corrupt = storage.load_metadata(pack_dir)
    assert corrupt["metadata_read_only"]
    assert "无法解析" in corrupt["metadata_error"]

    metadata_file.write_text(
        json.dumps({"schema_version": "99", "images": {}}), encoding="utf-8"
    )
    future = storage.load_metadata(pack_dir)
    assert future["metadata_read_only"]
    assert future["source_schema_version"] == "99"


def test_save_metadata_refuses_read_only_and_corrupt_existing_files(tmp_path):
    pack_dir, _ = create_pack(tmp_path)
    with pytest.raises(storage.SemanticMetadataCompatibilityError):
        storage.save_metadata(
            pack_dir,
            {"metadata_read_only": True, "metadata_error": "future version"},
        )
    (pack_dir / "semantic_metadata.json").write_text("not-json", encoding="utf-8")
    with pytest.raises(storage.SemanticMetadataCompatibilityError):
        storage.save_metadata(pack_dir, {"images": {}})


def test_semantic_metadata_complete_requires_current_snapshot(tmp_path):
    pack_dir, memes_dir = create_pack(tmp_path)
    category_dir = memes_dir / "happy"
    category_dir.mkdir()
    image_path = category_dir / "meme.png"
    image_path.write_bytes(b"image")
    scanned = storage.scan_images(pack_dir)[0]
    image = SemanticImage(
        content_sha256=scanned["content_sha256"],
        relative_path=scanned["relative_path"],
        category="happy",
        caption="猫在笑",
        tags=["可爱"],
        caption_status="done",
        embedding_status="done",
    )
    image.category_review_status = "auto_match"
    image.category_review_context_hash = image.category_context_hash
    metadata = {
        "file_total": 1,
        "unique_total": 1,
        "images": {scanned["entry_id"]: image.to_dict()},
    }
    storage.save_metadata(pack_dir, metadata)
    loaded = storage.load_metadata(pack_dir)
    assert storage.semantic_metadata_is_complete(pack_dir, loaded)
    assert storage.semantic_metadata_is_complete(
        pack_dir, loaded, require_embeddings=True
    )
    loaded["file_total"] = 2
    assert not storage.semantic_metadata_is_complete(pack_dir, loaded)


def test_reconcile_preserves_existing_caption_when_prompt_or_context_changes(tmp_path):
    pack_dir, memes_dir = create_pack(tmp_path)
    category_dir = memes_dir / "happy"
    category_dir.mkdir()
    (category_dir / "meme.png").write_bytes(b"image")
    (pack_dir / "memes_data.json").write_text(
        json.dumps({"happy": "旧分类描述"}, ensure_ascii=False), encoding="utf-8"
    )
    scanned = storage.scan_images(pack_dir)[0]
    image = SemanticImage(
        content_sha256=scanned["content_sha256"],
        relative_path=scanned["relative_path"],
        category="happy",
        category_description="旧分类描述",
        caption="已经付费生成的描述",
        tags=["已有标签"],
        caption_status="done",
        embedding_status="done",
        prompt_version="old-prompt",
        category_review_status="auto_match",
    )
    storage.save_metadata(
        pack_dir,
        {
            "file_total": 1,
            "unique_total": 1,
            "images": {scanned["entry_id"]: image.to_dict()},
        },
    )
    (pack_dir / "memes_data.json").write_text(
        json.dumps({"happy": "新分类描述"}, ensure_ascii=False), encoding="utf-8"
    )

    reconciled = storage.reconcile_metadata(pack_dir)
    item = reconciled["images"][scanned["entry_id"]]

    assert item["caption"] == "已经付费生成的描述"
    assert item["caption_status"] == "done"
    assert item["embedding_status"] == "pending"
    assert item["category_review_status"] == "unchecked"
    assert not category_analysis_is_current(item)
    assert storage.semantic_metadata_is_complete(pack_dir, reconciled)


def test_semantic_summary_reports_none_and_complete(tmp_path):
    pack_dir, memes_dir = create_pack(tmp_path)
    assert storage.get_pack_semantic_summary(pack_dir)["semantic_status"] == "none"
    category_dir = memes_dir / "happy"
    category_dir.mkdir()
    (category_dir / "meme.png").write_bytes(b"image")
    scanned = storage.scan_images(pack_dir)[0]
    image = SemanticImage(
        content_sha256=scanned["content_sha256"],
        relative_path=scanned["relative_path"],
        category="happy",
        caption="猫",
        tags=["猫"],
        caption_status="done",
        embedding_status="done",
    )
    image.category_review_status = "auto_match"
    image.category_review_context_hash = image.category_context_hash
    storage.save_metadata(
        pack_dir,
        {
            "file_total": 1,
            "unique_total": 1,
            "images": {scanned["entry_id"]: image.to_dict()},
        },
    )
    summary = storage.get_pack_semantic_summary(pack_dir)
    assert summary["semantic_status"] == "complete"
    assert summary["semantic_caption_done"] == 1
    assert summary["semantic_snapshot_matches"]


def test_import_metadata_file_validates_shape(tmp_path):
    path = tmp_path / "metadata.json"
    with pytest.raises(FileNotFoundError):
        storage.import_metadata_file(path)
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        storage.import_metadata_file(path)
    path.write_text(json.dumps({"images": {}}), encoding="utf-8")
    assert storage.import_metadata_file(path) == {"images": {}}


def test_metadata_items_filters_and_sorts(monkeypatch):
    data = {
        "images": {
            "done": {
                "relative_path": "z.png",
                "caption_status": "done",
                "embedding_status": "done",
                "updated_at": "3",
            },
            "failed": {
                "relative_path": "b.png",
                "caption_status": "failed",
                "embedding_status": "pending",
                "updated_at": "2",
            },
            "running": {
                "relative_path": "a.png",
                "caption_status": "running",
                "embedding_status": "pending",
                "updated_at": "1",
                "reclassification_status": "auto_reclassified",
            },
        }
    }
    monkeypatch.setattr(storage, "load_metadata", lambda pack_dir: data)
    assert [item["relative_path"] for item in storage.metadata_items("pack")] == [
        "a.png",
        "b.png",
        "z.png",
    ]
    assert len(storage.metadata_items("pack", "completed")) == 1
    assert len(storage.metadata_items("pack", "failed")) == 1
    assert len(storage.metadata_items("pack", "reclassified")) == 1
    assert storage.metadata_items("pack", "unknown") == []


def test_category_review_overview_is_unavailable_before_semantic_work(
    tmp_path, monkeypatch
):
    pack_dir, _ = create_pack(tmp_path)
    monkeypatch.setattr(
        storage,
        "get_pack_semantic_summary",
        lambda pack_dir: {"semantic_status": "none"},
    )
    overview = storage.get_category_review_overview(pack_dir)
    assert not overview["available"]
    assert overview["statistics"]["total"] == 0
