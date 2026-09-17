import json
from unittest.mock import Mock

import pytest
from astrbot_plugin_meme_manager.backend.semantic import storage
from astrbot_plugin_meme_manager.backend.semantic.models import (
    PROMPT_VERSION,
    semantic_caption_is_complete,
)
from PIL import Image


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
