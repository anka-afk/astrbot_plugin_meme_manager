import base64
import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_meme_manager.backend import auto_collect
from astrbot_plugin_meme_manager.mixins import web_api
from PIL import Image
from quart import Quart


@pytest.fixture
def review_api(tmp_path, monkeypatch):
    packs = tmp_path / "packs"
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for pack_id in ("first", "second"):
        pack = packs / pack_id
        (pack / "memes").mkdir(parents=True)
        (pack / "memes_data.json").write_text(
            json.dumps({"happy": "Joy", "sad": "Sadness"}), encoding="utf-8"
        )
    monkeypatch.setattr(auto_collect, "PACKS_DIR", packs)
    monkeypatch.setattr(web_api, "PACKS_DIR", packs)
    monkeypatch.setattr(auto_collect, "AUTO_COLLECT_INBOX_IMAGES_DIR", inbox)
    monkeypatch.setattr(
        auto_collect, "AUTO_COLLECT_INBOX_METADATA_PATH", inbox / "metadata.json"
    )
    monkeypatch.setattr(
        auto_collect, "AUTO_COLLECT_STATE_PATH", tmp_path / "state.json"
    )
    monkeypatch.setattr(
        auto_collect,
        "get_pack_paths",
        lambda pack_id: {"memes_dir": packs / pack_id / "memes"},
    )
    monkeypatch.setattr(
        auto_collect,
        "load_pack_category_mapping",
        lambda _: {"happy": "Joy", "sad": "Sadness"},
    )
    subject = web_api.WebAPIMixin()
    subject.semantic_task_manager = SimpleNamespace(
        begin_external_pack_operation=Mock(), end_external_pack_operation=Mock()
    )
    subject.reload_emotions = AsyncMock()
    subject.auto_collect_manager = auto_collect.AutoCollectManager(subject, {})
    output = io.BytesIO()
    Image.new("RGB", (8, 8), "orange").save(output, "PNG")
    content = output.getvalue()
    digest = hashlib.sha256(content).hexdigest()
    record_id = f"first:{digest}"
    (inbox / "candidate.png").write_bytes(content)
    (inbox / "metadata.json").write_text(
        json.dumps(
            {
                "items": {
                    record_id: {
                        "id": record_id,
                        "target_pack_id": "first",
                        "content_sha256": digest,
                        "filename": "candidate.png",
                        "suggested_category": "happy",
                        "classification": {
                            "meme_confidence": 0.96,
                            "category_confidence": 0.8,
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return Quart(__name__), subject, record_id, content


@pytest.mark.asyncio
async def test_review_list_and_preview_are_pack_scoped(review_api):
    app, subject, record_id, content = review_api
    async with app.test_request_context("/?pack_id=first"):
        response, status = await subject._api_auto_collect_inbox()
        assert status == 200
        assert (await response.get_json())["count"] == 1
    async with app.test_request_context(
        "/", query_string={"pack_id": "first", "id": record_id}
    ):
        response, status = await subject._api_auto_collect_image_data()
        assert status == 200
        value = (await response.get_json())["data_url"]
        assert value.startswith("data:image/png;base64,")
        assert base64.b64decode(value.split(",", 1)[1]) == content
    async with app.test_request_context(
        "/", query_string={"pack_id": "second", "id": record_id}
    ):
        response, status = await subject._api_auto_collect_image_data()
        assert status == 404


@pytest.mark.asyncio
async def test_accept_rejects_cross_pack_and_invalid_category(review_api):
    app, subject, record_id, _ = review_api
    for payload in (
        {"pack_id": "second", "items": [{"id": record_id}]},
        {"pack_id": "first", "items": [{"id": record_id, "category": "../escape"}]},
        {"pack_id": "first", "items": []},
    ):
        async with app.test_request_context("/", method="POST", json=payload):
            response, status = await subject._api_auto_collect_accept()
            assert status == 400
    assert (await subject.auto_collect_manager.pending_status("first"))["count"] == 1
    subject.reload_emotions.assert_not_awaited()


@pytest.mark.asyncio
async def test_accept_with_override_and_busy_retry(review_api):
    app, subject, record_id, _ = review_api
    payload = {"pack_id": "first", "items": [{"id": record_id, "category": "sad"}]}
    subject.semantic_task_manager.begin_external_pack_operation.side_effect = (
        RuntimeError("busy")
    )
    async with app.test_request_context("/", method="POST", json=payload):
        response, status = await subject._api_auto_collect_accept()
        assert status == 409
    assert (await subject.auto_collect_manager.pending_status("first"))["count"] == 1
    subject.semantic_task_manager.begin_external_pack_operation.side_effect = None
    async with app.test_request_context("/", method="POST", json=payload):
        response, status = await subject._api_auto_collect_accept()
        assert status == 200
        assert (await response.get_json())["imported"] == 1
    assert list((auto_collect.PACKS_DIR / "first" / "memes" / "sad").glob("*.png"))
    assert (await subject.auto_collect_manager.pending_status("first"))["count"] == 0


@pytest.mark.asyncio
async def test_discard_remembers_only_explicit_rejection(review_api):
    app, subject, record_id, _ = review_api
    async with app.test_request_context(
        "/",
        method="POST",
        json={
            "pack_id": "first",
            "ids": [record_id],
            "remember_rejection": "false",
        },
    ):
        response, status = await subject._api_auto_collect_discard()
        assert status == 400
    async with app.test_request_context(
        "/",
        method="POST",
        json={
            "pack_id": "first",
            "ids": [record_id],
            "remember_rejection": True,
        },
    ):
        response, status = await subject._api_auto_collect_discard()
        assert status == 200
        assert (await response.get_json())["discarded"] == 1
    state = json.loads(auto_collect.AUTO_COLLECT_STATE_PATH.read_text("utf-8"))
    assert record_id.split(":", 1)[1] in state["rejected"]["first"]
