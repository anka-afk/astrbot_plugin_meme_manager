import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
from astrbot_plugin_meme_manager.backend import semantic_task as task_module
from astrbot_plugin_meme_manager.backend.semantic_task import (
    SemanticTaskManager,
    _revision_category_choice,
    _revision_original_category,
)


@pytest.fixture
def manager(tmp_path):
    return SemanticTaskManager(tmp_path, config={"concurrency": 4})


def test_revision_original_category_prefers_direct_then_history():
    item = SimpleNamespace(
        category="review",
        reclassified_from_category="happy",
        reclassification_history=[{"from_category": "sad"}],
    )
    assert _revision_original_category(item, {"happy", "sad"}) == "happy"
    item.reclassified_from_category = "missing"
    assert _revision_original_category(item, {"happy", "sad"}) == "sad"
    assert _revision_original_category(item, {"happy"}) == ""


@pytest.mark.parametrize(
    ("current", "original", "fit", "suggested", "expected"),
    [
        ("review", "happy", "conflict", "happy", ("happy", "return_original")),
        ("review", "happy", "conflict", "sad", ("sad", "move_to_other")),
        ("happy", "", "match", "", ("happy", "keep_current")),
        ("happy", "", "uncertain", "", ("happy", "keep_current")),
        ("review", "", "conflict", "missing", ("", "manual_required")),
    ],
)
def test_revision_category_choice(current, original, fit, suggested, expected):
    assert (
        _revision_category_choice(
            current_category=current,
            original_category=original,
            category_fit=fit,
            suggested_category=suggested,
            selectable_categories={"happy", "sad"},
        )
        == expected
    )


@pytest.mark.parametrize("value", ["", "a", "../pack", "中文", "a" * 65])
def test_validate_pack_id_rejects_unsafe_values(value):
    with pytest.raises(ValueError):
        SemanticTaskManager._validate_pack_id(value)


@pytest.mark.parametrize("value", ["ab", "pack-a", "pack_1.2"])
def test_validate_pack_id_accepts_safe_values(value):
    assert SemanticTaskManager._validate_pack_id(value) == value


def test_state_selection_and_pack_paths_are_scoped(manager, tmp_path):
    assert manager._state_path("pack-a") == (
        tmp_path.resolve() / "semantic_indexes" / "pack-a" / "task_state.json"
    )
    assert manager._selection_path("pack-a").name == "provider_selection.json"
    assert manager._pack_dir("pack-a") == tmp_path.resolve() / "packs" / "pack-a"


def test_state_and_selection_round_trip_and_corrupt_fallback(manager):
    manager._save_state("pack-a", {"task_status": "running"})
    assert manager._load_state("pack-a") == {"task_status": "running"}
    manager._save_json_atomic(
        manager._selection_path("pack-a"), {"id": "embedding"}, ".selection."
    )
    assert manager._load_selection("pack-a") == {"id": "embedding"}
    manager._state_path("pack-a").write_text("bad json", encoding="utf-8")
    manager._selection_path("pack-a").write_text("[]", encoding="utf-8")
    assert manager._load_state("pack-a") == {}
    assert manager._load_selection("pack-a") == {}


def test_external_operation_guards_mutations(manager, monkeypatch):
    monkeypatch.setattr(manager, "_load_state", lambda pack_id: {})
    manager.begin_external_pack_operation("pack-a", "同步")
    with pytest.raises(RuntimeError):
        manager.assert_pack_mutation_allowed("pack-a", "删除")
    manager.end_external_pack_operation("pack-a")
    manager.assert_pack_mutation_allowed("pack-a", "删除")


@pytest.mark.parametrize("status", ["running", "paused"])
def test_persisted_semantic_queue_guards_mutations(manager, monkeypatch, status):
    monkeypatch.setattr(manager, "_load_state", lambda pack_id: {"task_status": status})
    with pytest.raises(RuntimeError):
        manager.assert_pack_mutation_allowed("pack-a", "重命名")


def test_active_pack_tasks_reports_live_tasks(manager, monkeypatch):
    class FakeTask:
        def __init__(self, done):
            self._done = done

        def done(self):
            return self._done

    manager._tasks = {"pack-a": FakeTask(False), "pack-b": FakeTask(True)}
    manager._index_tasks = {"pack-c": FakeTask(False)}
    monkeypatch.setattr(
        manager,
        "_load_state",
        lambda pack_id: {
            "task_status": "running",
            "task_phase": "indexing" if pack_id == "pack-c" else "captioning",
            "concurrency": "3",
        },
    )
    assert manager.active_pack_tasks(exclude_pack_id="pack-b") == [
        {
            "pack_id": "pack-a",
            "task_status": "running",
            "task_phase": "captioning",
            "concurrency": 3,
        },
        {
            "pack_id": "pack-c",
            "task_status": "running",
            "task_phase": "indexing",
            "concurrency": 0,
        },
    ]


@pytest.mark.parametrize(
    ("configured", "expected"),
    [(0, 1), ("bad", 1), (5, 5), (99, 16)],
)
def test_configured_concurrency_is_bounded(tmp_path, configured, expected):
    manager = SemanticTaskManager(tmp_path, config={"concurrency": configured})
    assert manager._configured_concurrency() == expected


def test_elapsed_seconds_handles_timezones_and_invalid_values():
    assert (
        SemanticTaskManager._elapsed_seconds(
            "2026-01-01T00:00:00Z", "2026-01-01T00:00:03+00:00"
        )
        == 3
    )
    assert (
        SemanticTaskManager._elapsed_seconds(
            datetime(2026, 1, 1), datetime(2026, 1, 1, 0, 0, 2)
        )
        == 2
    )
    assert SemanticTaskManager._elapsed_seconds("bad", "bad") == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, {"input": 0, "output": 0, "total": 0, "calls": 0}),
        (
            {"input": "2", "output": 3, "total": 0, "calls": -1},
            {"input": 2, "output": 3, "total": 5, "calls": 0},
        ),
        (
            {"input": "bad", "output": -3, "total": 8, "calls": 1},
            {"input": 0, "output": 0, "total": 8, "calls": 1},
        ),
    ],
)
def test_token_usage_normalization(value, expected):
    assert SemanticTaskManager._normalize_token_usage(value) == expected


def test_record_vision_usage_accumulates_and_saves(manager, monkeypatch):
    saved = []
    monkeypatch.setattr(
        manager,
        "_load_state",
        lambda pack_id: {"token_usage": {"input": 2, "total": 2, "calls": 1}},
    )
    monkeypatch.setattr(
        manager, "_save_state", lambda pack_id, state: saved.append(state)
    )
    manager._record_vision_usage("pack-a", {"output": 3})
    assert saved[0]["token_usage"] == {
        "input": 2,
        "output": 3,
        "total": 5,
        "calls": 2,
    }
    assert saved[0]["vision_calls"] == 2


def test_safe_error_redacts_local_paths_and_limits_length(manager):
    error = f"failed at {manager._pack_dir('pack-a')} " + "x" * 600
    result = manager._safe_error(error, "pack-a")
    assert str(manager.plugin_data_dir) not in result
    assert len(result) == 500


def test_vision_provider_details_requires_image_capability(tmp_path):
    provider = SimpleNamespace(
        provider_config={"modalities": ["text"], "model": "vision-model"}
    )
    context = SimpleNamespace(
        get_provider_by_id=lambda provider_id: provider,
        llm_generate=lambda **kwargs: None,
    )
    manager = SemanticTaskManager(
        tmp_path,
        context=context,
        config={"vision_provider_id": "vision"},
    )
    assert manager._vision_provider_details() == {
        "id": "vision",
        "model": "",
        "ready": False,
    }
    provider.provider_config["modalities"] = ["text", "image"]
    assert manager._vision_provider_details() == {
        "id": "vision",
        "model": "vision-model",
        "ready": True,
    }


def test_persist_provider_selection_preserves_matching_verification(manager):
    manager._save_json_atomic(
        manager._selection_path("pack-a"),
        {
            "effective_provider_id": "provider",
            "embedding_model": "model",
            "configured_dimension": 8,
            "dimension_verified": True,
            "verified_dimension": 8,
            "verified_at": "earlier",
        },
        ".selection.",
    )
    embedding = SimpleNamespace(
        ready=True,
        provider_id="provider",
        model_name="model",
        dimension=8,
    )
    result = manager._persist_provider_selection("pack-a", embedding, "configured")
    assert result["dimension_verified"] is True
    assert result["verified_dimension"] == 8
    assert result["selection_mode"] == "configured"


@pytest.mark.asyncio
async def test_close_cancels_all_live_tasks(manager):
    started = asyncio.Event()

    async def wait_forever():
        started.set()
        await asyncio.Event().wait()

    first = asyncio.create_task(wait_forever())
    second = asyncio.create_task(wait_forever())
    manager._tasks["pack-a"] = first
    manager._index_tasks["pack-b"] = second
    await started.wait()
    await manager.close()
    assert first.cancelled()
    assert second.cancelled()


@pytest.mark.parametrize(
    ("state", "metadata", "external", "expected"),
    [
        ({}, {"images": {}}, "", "empty"),
        ({"queue_cleared": True}, {"images": {}}, "", "cleared"),
        ({}, {"images": {}}, "同步", "external_operation"),
        (
            {},
            {"images": {}, "metadata_read_only": True, "metadata_error": "broken"},
            "",
            "metadata_error",
        ),
    ],
)
def test_status_reports_primary_queue_states(
    manager, monkeypatch, state, metadata, external, expected
):
    adapter = SimpleNamespace(
        ready=False,
        provider_id="",
        model_name="",
        dimension=0,
        provider=None,
    )
    monkeypatch.setattr(task_module, "load_metadata", lambda pack_dir: metadata)
    monkeypatch.setattr(task_module, "load_index_manifest", lambda *args: {})
    monkeypatch.setattr(manager, "_load_state", lambda pack_id: state)
    monkeypatch.setattr(manager, "_embedding_adapter", lambda pack_id: adapter)
    monkeypatch.setattr(manager, "_load_selection", lambda pack_id: {})
    monkeypatch.setattr(
        manager,
        "_vision_provider_details",
        lambda: {"id": "", "model": "", "ready": False},
    )
    monkeypatch.setattr(manager, "capabilities", lambda *args, **kwargs: {})
    if external:
        manager._external_pack_operations["pack-a"] = external

    assert manager.status("pack-a")["queue_status"] == expected


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        (None, False),
        ({"caption_status": "pending"}, True),
        ({"embedding_status": "failed"}, True),
        (
            {
                "caption_status": "done",
                "caption": "已有描述",
                "tags": ["标签"],
                "embedding_status": "done",
                "prompt_version": "old",
            },
            False,
        ),
    ],
)
def test_item_queue_detection(item, expected):
    assert SemanticTaskManager._item_is_queued(item) is expected


def test_reset_running_items_returns_them_to_pending(manager, monkeypatch):
    metadata = {
        "images": {
            "a": {"caption_status": "running", "embedding_status": "done"},
            "b": {"caption_status": "done", "embedding_status": "running"},
            "c": {"caption_status": "done", "embedding_status": "done"},
        }
    }
    saved = []
    monkeypatch.setattr(
        task_module, "save_metadata", lambda path, data: saved.append(data)
    )
    result, recovered = manager._reset_running_items("pack-a", metadata)
    assert result["images"]["a"]["caption_status"] == "pending"
    assert result["images"]["b"]["embedding_status"] == "pending"
    assert recovered == 2
    assert saved == [metadata]


@pytest.mark.asyncio
async def test_caption_worker_skips_completed_caption_from_old_prompt(
    manager, monkeypatch
):
    raw_item = {
        "content_sha256": "a" * 64,
        "relative_path": "memes/happy/meme.png",
        "category": "happy",
        "caption": "已有描述",
        "tags": ["已有标签"],
        "caption_status": "done",
        "embedding_status": "pending",
        "category_review_status": "unchecked",
        "prompt_version": "old-prompt",
    }

    async def unexpected_generation(*args, **kwargs):
        raise AssertionError("completed captions must not call the vision provider")

    monkeypatch.setattr(task_module, "generate_caption", unexpected_generation)
    await manager._process_caption_item(
        "pack-a",
        manager._pack_dir("pack-a"),
        {"images": {"entry": raw_item}},
        "entry",
        raw_item,
        "vision",
        {"happy": "开心"},
        False,
        asyncio.Semaphore(1),
    )
    assert raw_item["caption"] == "已有描述"


def test_status_allows_index_rebuild_for_completed_old_prompt_caption(
    manager, monkeypatch
):
    metadata = {
        "images": {
            "entry": {
                "caption_status": "done",
                "caption": "已有描述",
                "tags": ["已有标签"],
                "embedding_status": "pending",
                "category_review_status": "unchecked",
                "prompt_version": "old-prompt",
            }
        }
    }
    adapter = SimpleNamespace(
        ready=True,
        provider_id="embedding",
        model_name="model",
        dimension=8,
        provider=object(),
    )
    monkeypatch.setattr(task_module, "load_metadata", lambda pack_dir: metadata)
    monkeypatch.setattr(task_module, "load_index_manifest", lambda *args: {})
    monkeypatch.setattr(task_module, "index_is_ready", lambda *args, **kwargs: False)
    monkeypatch.setattr(manager, "_load_state", lambda pack_id: {})
    monkeypatch.setattr(manager, "_embedding_adapter", lambda pack_id: adapter)
    monkeypatch.setattr(manager, "_load_selection", lambda pack_id: {})
    monkeypatch.setattr(
        manager,
        "_vision_provider_details",
        lambda: {"id": "", "model": "", "ready": False},
    )
    monkeypatch.setattr(manager, "capabilities", lambda *args, **kwargs: {})

    status = manager.status("pack-a")

    assert status["can_rebuild_index"] is True
    assert status["queued_caption_tasks"] == 0
    assert status["queued_embedding_tasks"] == 1
