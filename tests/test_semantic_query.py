import hashlib
import json
from unittest.mock import AsyncMock

import pytest
from backend import semantic_query as query

ENTRY_ID = "123456789abc" + "0" * 52
CONTENT_HASH = hashlib.sha256(b"image").hexdigest()


class FakeEvent:
    def __init__(self):
        self.extra = {}

    def get_extra(self, key):
        return self.extra.get(key)

    def set_extra(self, key, value):
        self.extra[key] = value


@pytest.mark.asyncio
async def test_search_memes_returns_early_for_empty_query(monkeypatch):
    search_index = AsyncMock()
    monkeypatch.setattr(query, "search_index", search_index)
    result = await query.search_memes("pack", "data", "pack-id", " ", object())
    assert result == {"ok": True, "candidates": [], "reason": "查询词不能为空"}
    search_index.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_memes_requires_metadata_and_complete_index(monkeypatch):
    monkeypatch.setattr(query, "load_metadata", lambda pack_dir: {"images": {}})
    result = await query.search_memes("pack", "data", "pack-id", "cat", object())
    assert result["reason"] == "资源包没有语义元数据"

    monkeypatch.setattr(query, "load_metadata", lambda pack_dir: {"images": {"a": {}}})
    monkeypatch.setattr(
        query, "semantic_metadata_is_complete", lambda *args, **kwargs: False
    )
    result = await query.search_memes("pack", "data", "pack-id", "cat", object())
    assert "尚未完成100%语义化" in result["reason"]


@pytest.mark.asyncio
async def test_search_memes_strips_internal_candidate_fields(monkeypatch):
    metadata = {"images": {ENTRY_ID: {"caption": "猫"}}}
    monkeypatch.setattr(query, "load_metadata", lambda pack_dir: metadata)
    monkeypatch.setattr(
        query, "semantic_metadata_is_complete", lambda *args, **kwargs: True
    )
    search_index = AsyncMock(
        return_value=[
            {
                "id": "meme:123456789abc",
                "content_sha256": CONTENT_HASH,
                "entry_id": ENTRY_ID,
                "score": 0.9,
                "caption": "猫",
            }
        ]
    )
    monkeypatch.setattr(query, "search_index", search_index)

    result = await query.search_memes(
        "pack",
        "data",
        "pack-id",
        "cat",
        object(),
        top_k=3,
        min_score=0.5,
    )

    assert result == {
        "ok": True,
        "candidates": [{"id": "meme:123456789abc", "caption": "猫"}],
        "max_selectable": 1,
    }
    assert search_index.await_args.kwargs == {"top_k": 3, "min_score": 0.5}


@pytest.mark.asyncio
async def test_search_memes_reports_no_matches(monkeypatch):
    monkeypatch.setattr(query, "load_metadata", lambda pack_dir: {"images": {"a": {}}})
    monkeypatch.setattr(
        query, "semantic_metadata_is_complete", lambda *args, **kwargs: True
    )
    monkeypatch.setattr(query, "search_index", AsyncMock(return_value=[]))
    result = await query.search_memes("pack", "data", "pack-id", "cat", object())
    assert result["reason"] == "没有找到足够匹配的表情包"


def test_candidate_records_adds_private_fields_for_unique_prefix(monkeypatch):
    metadata = {
        "images": {
            ENTRY_ID: {
                "content_sha256": CONTENT_HASH,
                "caption": "猫在笑",
                "tags": ["猫"],
            }
        }
    }
    monkeypatch.setattr(query, "load_metadata", lambda pack_dir: metadata)
    records = query.candidate_records(
        "pack", [{"id": "meme:123456789abc", "caption": "公开描述"}]
    )
    assert records[0]["entry_id"] == ENTRY_ID
    assert records[0]["content_sha256"] == CONTENT_HASH
    assert records[0]["caption"] == "猫在笑"


def test_candidate_records_rejects_invalid_or_ambiguous_ids(monkeypatch):
    metadata = {
        "images": {
            ENTRY_ID: {},
            "123456789abc" + "1" * 52: {},
        }
    }
    monkeypatch.setattr(query, "load_metadata", lambda pack_dir: metadata)
    assert (
        query.candidate_records("pack", [{"id": "bad"}, {"id": "meme:123456789abc"}])
        == []
    )


def test_remember_candidates_merges_existing_event_state():
    event = FakeEvent()
    event.extra["meme_manager_semantic_candidates"] = {
        "meme:oldoldoldold": {"id": "meme:oldoldoldold"}
    }
    query.remember_candidates(
        event,
        [
            {"id": "meme:123456789abc", "caption": "猫"},
            {"caption": "无标识"},
        ],
    )
    assert set(event.extra["meme_manager_semantic_candidates"]) == {
        "meme:oldoldoldold",
        "meme:123456789abc",
    }
    query.remember_candidates(object(), [])


def test_validate_selected_id_accepts_matching_unchanged_file(tmp_path, monkeypatch):
    image_path = tmp_path / "memes" / "happy" / "meme.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"image")
    metadata = {
        "images": {
            ENTRY_ID: {
                "relative_path": "memes/happy/meme.png",
                "category": "happy",
                "content_sha256": CONTENT_HASH,
            }
        }
    }
    monkeypatch.setattr(query, "load_metadata", lambda pack_dir: metadata)
    event = FakeEvent()
    event.extra["meme_manager_semantic_candidates"] = {
        "meme:123456789abc": {"entry_id": ENTRY_ID}
    }

    assert (
        query.validate_selected_id(event, "meme:123456789abc", tmp_path) == image_path
    )


@pytest.mark.parametrize(
    "mutation", ["missing-candidate", "wrong-entry", "review", "hash"]
)
def test_validate_selected_id_rejects_stale_or_forbidden_selection(
    tmp_path, monkeypatch, mutation
):
    image_path = tmp_path / "memes" / "happy" / "meme.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"image")
    record = {
        "relative_path": "memes/happy/meme.png",
        "category": "happy",
        "content_sha256": CONTENT_HASH,
    }
    metadata = {"images": {ENTRY_ID: record}}
    monkeypatch.setattr(query, "load_metadata", lambda pack_dir: metadata)
    event = FakeEvent()
    if mutation != "missing-candidate":
        event.extra["meme_manager_semantic_candidates"] = {
            "meme:123456789abc": {
                "entry_id": "abcdef123456" + "0" * 52
                if mutation == "wrong-entry"
                else ENTRY_ID
            }
        }
    if mutation == "review":
        record["category"] = query.REVIEW_CATEGORY
    if mutation == "hash":
        record["content_sha256"] = "0" * 64

    assert query.validate_selected_id(event, "meme:123456789abc", tmp_path) is None


def test_validate_selected_id_rejects_invalid_id_and_missing_file(
    tmp_path, monkeypatch
):
    assert query.validate_selected_id(FakeEvent(), "invalid", tmp_path) is None
    monkeypatch.setattr(
        query,
        "load_metadata",
        lambda pack_dir: {
            "images": {
                ENTRY_ID: {
                    "relative_path": "memes/missing.png",
                    "category": "happy",
                    "content_sha256": CONTENT_HASH,
                }
            }
        },
    )
    event = FakeEvent()
    event.extra["meme_manager_semantic_candidates"] = {
        "meme:123456789abc": {"entry_id": ENTRY_ID}
    }
    assert query.validate_selected_id(event, "meme:123456789abc", tmp_path) is None


def test_dumps_result_preserves_unicode():
    dumped = query.dumps_result({"caption": "开心"})
    assert json.loads(dumped) == {"caption": "开心"}
    assert "开心" in dumped
