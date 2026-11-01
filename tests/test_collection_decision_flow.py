import asyncio
import hashlib

import pytest
import pytest_asyncio
import test_auto_collect_flow as flow_fixtures
from astrbot_plugin_meme_manager.backend import auto_collect
from astrbot_plugin_meme_manager.backend.decision import DecisionError
from astrbot_plugin_meme_manager.backend.semantic.storage import load_metadata


class FakeDecisionService:
    def __init__(self):
        self.ready = True
        self.base_url = "https://example.test/v1"
        self.model = "test-decision"
        self.timeout = 15
        self.max_concurrency = 2
        self.probability = 0.97
        self.category = "c0"
        self.error = None
        self.calls = []

    def status(self):
        return {"ready": self.ready, "reason": "ready", "model": self.model}

    async def evaluate(self, state, questions):
        self.calls.append((state, questions))
        if self.error:
            raise self.error
        probability = (
            self.probability.pop(0)
            if isinstance(self.probability, list)
            else self.probability
        )
        keys = list(questions["category"]["criteria"])
        answers = {
            "meme_usage": {"type": "noul", "noul": probability},
            "category": {
                "type": "choice",
                "choice": self.category,
                "probabilities": {
                    key: 0.96 if key == self.category else 0.04 / (len(keys) - 1)
                    for key in keys
                },
            },
        }
        if "content_rule" in questions:
            answers["content_rule"] = {"type": "choice", "choice": "allow"}
        return answers


@pytest_asyncio.fixture
async def decision_flow():
    harness = flow_fixtures.AutoCollectFlowTests(methodName="runTest")
    harness.setUp()
    harness.host.decision_client = FakeDecisionService()
    harness.decision.update(
        chat_use_evidence="Expressive illustration with reaction text"
    )
    manager = auto_collect.AutoCollectManager(
        harness.host,
        {**harness.config, "judgment_backend": "decision"},
    )
    harness.host.auto_collect_manager = manager
    await manager.start()
    try:
        yield harness
    finally:
        await manager.close()
        await harness.host.semantic_task_manager.close()
        harness.doCleanups()


@pytest.mark.asyncio
async def test_uncertain_decision_cannot_auto_import_but_explicit_review_can(
    decision_flow,
):
    harness = decision_flow
    manager = harness.host.auto_collect_manager
    manager.manual_review = False
    harness.host.decision_client.probability = 0.5
    assert await manager.submit(flow_fixtures.FlowEvent(harness.png))
    await asyncio.wait_for(manager.queue.join(), 5)
    response = await harness.client.get("/inbox", query_string={"pack_id": "pack-a"})
    pending = await response.get_json()
    assert pending["count"] == pending["review_required_count"] == 1
    item = pending["items"][0]
    assert item["review_required"] and item["judgment_backend"] == "decision"
    assert (await manager.import_pending("pack-a"))["skipped_review"] == 1
    record_id = item["id"]
    result = await harness.client.post(
        "/inbox/accept", json={"pack_id": "pack-a", "items": [{"id": record_id}]}
    )
    assert (await result.get_json())["imported"] == 1
    semantic = next(iter(load_metadata(harness.pack_dir)["images"].values()))
    assert semantic["caption_status"] == "pending"
    assert semantic["auto_caption"] == harness.decision["caption"]
    assert await manager.pending_status("pack-a") is not None


@pytest.mark.asyncio
async def test_failed_judgment_keeps_visual_evidence_without_caching_rejection(
    decision_flow,
):
    harness = decision_flow
    manager = harness.host.auto_collect_manager
    manager.manual_review = False
    harness.host.decision_client.error = DecisionError("timeout")
    assert await manager.submit(flow_fixtures.FlowEvent(harness.png))
    await asyncio.wait_for(manager.queue.join(), 5)
    pending = await manager.pending_status("pack-a")
    assert pending["count"] == 1
    item = pending["items"][0]
    assert item["decision_status"] == "failed" and item["review_required"]
    assert item["meme_confidence"] is None
    assert item["caption"] == harness.decision["caption"]
    assert manager._state.get("understandings") and not manager._state.get("decisions")
    assert manager.counters["decision_failed"] == 1


@pytest.mark.asyncio
async def test_stage_cache_reuses_visual_analysis_and_invalidates_routing_rules(
    decision_flow,
):
    harness = decision_flow
    manager = harness.host.auto_collect_manager
    content = harness.png.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    job = auto_collect.AutoCollectJob(
        harness.png, "pack-a", {"happy": "positive", "sad": "sad"}, "group", "100"
    )
    first = await manager._analyze_with_decision(content, ".png", job, digest)
    second = await manager._analyze_with_decision(content, ".png", job, digest)
    assert not first["review_required"] and not second["review_required"]
    assert harness.vision.await_count == 1
    assert len(harness.host.decision_client.calls) == 1
    manager.content_review_enabled = True
    manager.content_review_prompt = "Only suitable reaction images"
    await manager._analyze_with_decision(content, ".png", job, digest)
    assert harness.vision.await_count == 1
    assert len(harness.host.decision_client.calls) == 2
    manager.min_meme_confidence = 0.99
    result = await manager._analyze_with_decision(content, ".png", job, digest)
    assert result["review_required"]
    assert harness.vision.await_count == 1
    assert len(harness.host.decision_client.calls) == 3
    assert manager._state["recognition_count"] == 1


@pytest.mark.asyncio
async def test_conclusive_rejection_is_cached_with_unknown_category(decision_flow):
    harness = decision_flow
    manager = harness.host.auto_collect_manager
    harness.host.decision_client.probability = 0.01
    harness.host.decision_client.category = "unknown"
    await manager.submit(flow_fixtures.FlowEvent(harness.png))
    await asyncio.wait_for(manager.queue.join(), 5)
    assert (await manager.pending_status("pack-a"))["count"] == 0
    await manager.submit(flow_fixtures.FlowEvent(harness.png))
    await asyncio.wait_for(manager.queue.join(), 5)
    assert harness.vision.await_count == 1
    assert len(harness.host.decision_client.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("suggested_category", ["happy", "deleted-category"])
@pytest.mark.parametrize("review_flag", ["top_level", "classification"])
async def test_mixed_automatic_batch_skips_only_forced_review_records(
    decision_flow, suggested_category, review_flag
):
    harness = decision_flow
    manager = harness.host.auto_collect_manager
    harness.host.decision_client.probability = [0.5, 0.97]
    for index, source in enumerate((harness.png, harness.gif)):
        harness.decision["caption"] = f"Distinct reaction image {index}"
        assert await manager.submit(flow_fixtures.FlowEvent(source))
        await asyncio.wait_for(manager.queue.join(), 5)
    pending = await manager.pending_status("pack-a")
    assert pending["count"] == 2
    review_id = next(item["id"] for item in pending["items"] if item["review_required"])
    metadata = manager._load_json(auto_collect.AUTO_COLLECT_INBOX_METADATA_PATH, {})
    record = metadata["items"][review_id]
    record["suggested_category"] = suggested_category
    record["review_required"] = review_flag == "top_level"
    record["classification"]["review_required"] = review_flag == "classification"
    manager._save_json(auto_collect.AUTO_COLLECT_INBOX_METADATA_PATH, metadata)
    manager.manual_review = False
    result = await manager.import_pending("pack-a")
    assert result["imported"] == result["skipped_review"] == 1
    remaining = await manager.pending_status("pack-a")
    assert remaining["count"] == 1 and remaining["items"][0]["review_required"]
    assert remaining["items"][0]["id"] == review_id
    assert manager.pending_image_path("pack-a", review_id).is_file()


@pytest.mark.asyncio
async def test_stale_top_level_flag_cannot_override_classification_review(
    decision_flow,
):
    harness = decision_flow
    manager = harness.host.auto_collect_manager
    manager.manual_review = False
    harness.host.decision_client.probability = 0.5
    await manager.submit(flow_fixtures.FlowEvent(harness.png))
    await asyncio.wait_for(manager.queue.join(), 5)
    metadata = manager._load_json(auto_collect.AUTO_COLLECT_INBOX_METADATA_PATH, {})
    record = next(iter(metadata["items"].values()))
    record["review_required"] = False
    record["suggested_category"] = "deleted-category"
    manager._save_json(auto_collect.AUTO_COLLECT_INBOX_METADATA_PATH, metadata)
    assert (await manager.pending_status("pack-a"))["items"][0]["review_required"]
    assert (await manager.import_pending("pack-a"))["skipped_review"] == 1
    selection = {"id": record["id"]}
    result = await manager.accept_pending(
        "pack-a", [selection, selection], manual_confirmed=False
    )
    assert result == {"imported": 0, "duplicates": 0, "failed": 0, "skipped_review": 1}
    with pytest.raises(ValueError):
        await manager.accept_pending("pack-a", [selection], manual_confirmed=True)
