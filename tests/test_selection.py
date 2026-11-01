import asyncio
import json
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from backend.decision import DecisionError
from backend.selection import SelectionTask, select_candidates
from backend.semantic.models import ensure_category_tag
from backend.semantic.storage import _normalize_manual_semantic_inputs

pytestmark = pytest.mark.asyncio


@pytest.fixture(params=["category", "semantic"])
def selection_case(request):
    ids = (
        ["comfort", "self"]
        if request.param == "category"
        else ["meme:000000000001", "meme:000000000002"]
    )
    task = SelectionTask(
        kind=request.param,
        reply="别难过，我在陪你。",
        candidates=[
            {"id": value, "description": "安慰", "caption": "安慰", "tags": ["安慰"]}
            for value in ids
        ],
    )
    generate = AsyncMock(
        return_value=SimpleNamespace(
            completion_text=json.dumps({"selected_ids": [ids[1]]})
        )
    )
    client = SimpleNamespace(
        ready=True,
        model="decision-model",
        status=lambda: {"ready": True, "model": "decision-model"},
        evaluate=AsyncMock(
            return_value={"selection": {"type": "choice", "choice": "c1"}}
        ),
    )
    select = partial(select_candidates, task, generate=generate, decision_client=client)
    return task, generate, client, select


@pytest.mark.parametrize("backend", ["chat", "decision"])
async def test_selection_maps_candidates_without_exposing_paths(
    selection_case, backend
):
    task, generate, client, select = selection_case
    task.candidates[0]["pack_dir"] = "C:/private/pack"
    task.reference = "x" * 16_000
    result = await select(backend=backend)
    assert result.status == "selected" and result.selected_ids == [
        task.candidates[1]["id"]
    ]
    if backend == "chat":
        prompt = generate.await_args.args[0]
        assert task.reference in prompt and "C:/private/pack" not in prompt
        client.evaluate.assert_not_awaited()
    else:
        state, questions = client.evaluate.await_args.args
        assert list(questions["selection"]["criteria"]) == ["c0", "c1", "skip"]
        assert "C:/private/pack" not in json.dumps([state, questions])
        generate.assert_not_awaited()
    task.candidates.clear()
    assert (await select(backend=backend)).status == "abstained"
    assert generate.await_count + client.evaluate.await_count == 1


async def test_chat_category_keeps_legacy_multiselect():
    task = SelectionTask(
        kind="category", reply="hello", candidates=[{"id": "comfort"}, {"id": "self"}]
    )
    generate = AsyncMock(
        return_value=SimpleNamespace(
            completion_text='{"emotions":["&&COMFORT&&","missing","self","self"]}'
        )
    )
    result = await select_candidates(
        task, backend="chat", generate=generate, decision_client=None
    )
    assert result.selected_ids == ["comfort", "self"]


@pytest.mark.parametrize("backend", ["chat", "decision"])
async def test_legal_metadata_limits_do_not_block_other_candidates(
    selection_case, backend
):
    task, generate, client, select = selection_case
    if task.kind == "semantic":
        _, tags, _ = _normalize_manual_semantic_inputs(
            "caption", [f"tag-{i}" for i in range(50)], ""
        )
        task.candidates[0]["tags"] = ensure_category_tag(tags, "happy")
    else:
        task.candidates[0]["description"] = "表" * 20_000
    result = await select(backend=backend)
    assert result.status == "selected" and result.selected_ids == [
        task.candidates[1]["id"]
    ]
    if backend == "decision":
        metadata = client.evaluate.await_args.args[1]["selection"]["criteria"]["c0"]
        if task.kind == "semantic":
            assert metadata["tags"] == task.candidates[0]["tags"]
        else:
            assert metadata["description"] == "表" * 4_000
    else:
        prompt = generate.await_args.args[0]
        if task.kind == "semantic":
            assert all(tag in prompt for tag in task.candidates[0]["tags"])
        else:
            assert "表" * 4_000 in prompt and "表" * 4_001 not in prompt


@pytest.mark.parametrize(
    "backend,answer,reason",
    [
        ("chat", "not-json", "invalid_response"),
        ("chat", '{"selected_ids":["unknown"]}', "unknown_candidate"),
        ("decision", {}, "invalid_response"),
        (
            "decision",
            {"selection": {"type": "choice", "choice": "unknown"}},
            "unknown_candidate",
        ),
    ],
)
async def test_invalid_selection_is_failure(selection_case, backend, answer, reason):
    task, generate, client, select = selection_case
    generate.return_value.completion_text = answer
    client.evaluate.return_value = answer
    result = await select(backend=backend)
    assert (
        result.status == "failed"
        and result.reason == reason
        and not result.selected_ids
    )


@pytest.mark.parametrize("selection_case", ["semantic"], indirect=True)
async def test_semantic_chat_rejects_multiple_candidates(selection_case):
    task, generate, client, select = selection_case
    generate.return_value.completion_text = json.dumps(
        {"selected_ids": [c["id"] for c in task.candidates]}
    )
    result = await select(backend="chat")
    assert result.status == "failed" and result.reason == "multiple_candidates"


@pytest.mark.parametrize("backend", ["chat", "decision"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_backend_failure_and_cancellation(selection_case, backend, cancel):
    task, generate, client, select = selection_case
    error = (
        asyncio.CancelledError()
        if cancel
        else (
            RuntimeError("unavailable")
            if backend == "chat"
            else DecisionError("closed")
        )
    )
    generate.side_effect = client.evaluate.side_effect = error
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await select(backend=backend)
    else:
        result = await select(backend=backend)
        assert result.status == "failed"
        assert result.reason == ("generation_failed" if backend == "chat" else "closed")
