"""Regression coverage for multi-pack rules, retrieval and delivery."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api.provider import LLMResponse
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import (
    MessageEventResult,
    ResultContentType,
)
from astrbot_plugin_meme_manager import main as plugin_main
from astrbot_plugin_meme_manager.backend.packs import resolver, storage
from astrbot_plugin_meme_manager.backend.semantic import index as semantic_index
from astrbot_plugin_meme_manager.backend.semantic import query
from astrbot_plugin_meme_manager.backend.semantic import storage as semantic_storage
from astrbot_plugin_meme_manager.backend.semantic.models import SemanticImage
from astrbot_plugin_meme_manager.main import MemeSender
from astrbot_plugin_meme_manager.mixins.event_handlers import (
    LLM_REQUEST_ORIGIN_EXTRA_KEY,
)
from astrbot_plugin_meme_manager.mixins.web_api import WebAPIMixin
from PIL import Image as PillowImage
from quart import Quart


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    packs = tmp_path / "packs"
    packs.mkdir()
    registry = tmp_path / "registry.json"
    rules = tmp_path / "selection_rules.json"
    for module in (resolver, storage):
        monkeypatch.setattr(module, "PACKS_DIR", packs)
        monkeypatch.setattr(module, "REGISTRY_PATH", registry)
        monkeypatch.setattr(module, "SELECTION_RULES_PATH", rules)
    monkeypatch.setattr(storage, "PLUGIN_DATA_DIR", tmp_path)
    registry.write_text(
        json.dumps(
            {
                "installed_packs": [
                    {"id": "pack-a", "enabled": True},
                    {"id": "pack-b", "enabled": True},
                ]
            }
        ),
        encoding="utf-8",
    )
    for pack_id, categories in (
        ("pack-a", {"happy": "Happy"}),
        ("pack-b", {"happy": "Joyful", "sad": "Sad"}),
    ):
        directory = packs / pack_id
        directory.mkdir()
        (directory / "memes_data.json").write_text(
            json.dumps(categories), encoding="utf-8"
        )
    return SimpleNamespace(packs=packs, rules=rules)


@pytest.mark.parametrize("scope", ["default", "session", "persona"])
def test_rules_round_trip_and_resolve_all_packs(runtime, scope):
    rule = {"id": "binding", "scope": scope, "pack_ids": ["pack-a", "pack-b", "pack-a"]}
    rules = [rule]
    if scope != "default":
        rule["target"] = "target"
        rules.append({"id": "default", "scope": "default", "pack_id": "pack-b"})
    saved = storage.save_selection_rules(rules)
    assert saved["rules"][0]["pack_ids"] == ["pack-a", "pack-b"]
    context = resolver.resolve_pack_context(session_id="target", persona_id="target")
    assert context["pack_ids"] == ["pack-a", "pack-b"]
    assert (
        context["pack_id"] == resolver.resolve_pack_id("target", "target") == "pack-a"
    )
    assert context["pack_dirs"]["pack-b"] == runtime.packs / "pack-b"
    assert context["category_mapping"]["happy"] == "Happy"
    assert context["category_mapping"]["sad"] == "Sad"
    assert storage.get_selection_rules()["rules"] == saved["rules"]
    assert (
        storage._validate_and_normalize_rules(saved["rules"], {"pack-a", "pack-b"})
        == saved["rules"]
    )


def test_default_selection_skips_unavailable_packs(runtime):
    runtime.rules.write_text(
        json.dumps(
            {
                "rules": [
                    {"scope": "default", "pack_ids": ["missing", "pack-a", "pack-b"]}
                ]
            }
        ),
        encoding="utf-8",
    )
    assert resolver.resolve_pack_ids() == ["pack-a", "pack-b"]
    (runtime.packs / "pack-a").rename(runtime.packs / "disabled")
    assert resolver.resolve_pack_ids() == ["pack-b"]


def test_invalid_secondary_pack_is_rejected(runtime):
    with pytest.raises(ValueError, match="missing"):
        storage.save_selection_rules(
            [
                {
                    "id": "default",
                    "scope": "default",
                    "pack_id": "pack-a",
                    "pack_ids": ["pack-a", "missing"],
                }
            ]
        )


def test_uninstall_preserves_remaining_multi_pack_bindings(runtime):
    storage.save_selection_rules(
        [
            {
                "id": "session",
                "scope": "session",
                "target": "chat",
                "pack_ids": ["pack-a", "pack-b"],
            },
            {"id": "default", "scope": "default", "pack_ids": ["pack-a", "pack-b"]},
        ]
    )
    result = storage.uninstall_pack("pack-a")
    rules = storage.get_selection_rules()["rules"]
    assert len(rules) == 2
    assert all(
        rule["pack_ids"] == ["pack-b"] and rule["pack_id"] == "pack-b" for rule in rules
    )
    assert result["switched_default_to"] == "pack-b"
    assert resolver.resolve_pack_ids("chat") == ["pack-b"]


@pytest.fixture
def semantic_runtime(runtime, monkeypatch):
    sender = object.__new__(MemeSender)
    sender.emotions_probability = 100
    sender.config = {}
    sender.semantic_top_k = 5
    sender.semantic_min_score = 0
    sender._resolve_embedding_provider = lambda pack_id: SimpleNamespace(
        provider_config={"id": pack_id, "model": "embedding-model"}
    )
    sender.convert_static_to_gif = False
    sender.trigger_scope = "only_chat_llm"
    sender.streaming_compatibility = True
    sender.enable_mixed_message = False
    sender.send_image_as_base64 = False
    sender.remove_invalid_alternative_markup = False
    sender.content_cleanup_rule = r"&&[a-zA-Z]*&&"
    sender._send_meme_image = AsyncMock()
    storage.save_selection_rules(
        [{"id": "default", "scope": "default", "pack_ids": ["pack-a", "pack-b"]}]
    )
    context = resolver.resolve_pack_context()
    metadata = {}
    for index, pack_id in enumerate(context["pack_ids"]):
        path = context["pack_dirs"][pack_id] / "memes" / "happy" / "meme.png"
        path.parent.mkdir(parents=True)
        PillowImage.new("RGB", (2, 2), color=(index * 100, 0, 0)).save(path)
        entry_id = "123456789abc" + str(index) * 52
        metadata[context["pack_dirs"][pack_id]] = {
            "images": {
                entry_id: {
                    "relative_path": "memes/happy/meme.png",
                    "category": "happy",
                    "caption": pack_id,
                    "content_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            }
        }
    monkeypatch.setattr(query, "load_metadata", lambda directory: metadata[directory])
    monkeypatch.setattr(query, "index_is_ready", lambda *a, **k: True)

    async def search_index(data_dir, pack_id, text, embedding, data, **kwargs):
        assert embedding.provider_id == pack_id
        entry_id, record = next(iter(data["images"].items()))
        return [
            {
                **record,
                "entry_id": entry_id,
                "id": "meme:" + entry_id[:12],
                "score": 0.9 if pack_id == "pack-b" else 0.5,
            }
        ]

    monkeypatch.setattr(query, "search_index", search_index)
    state = {
        "meme_manager_semantic_active": True,
        "meme_manager_semantic_mode": "tool",
        "meme_manager_runtime_pack_ids": context["pack_ids"],
        "meme_manager_semantic_verified_pack_ids": ",".join(context["pack_ids"]),
        LLM_REQUEST_ORIGIN_EXTRA_KEY: "chat",
    }
    event = SimpleNamespace(
        get_extra=lambda key: state.get(key),
        set_extra=lambda key, value: state.update({key: value}),
        get_result=lambda: state.get("result"),
        get_platform_name=lambda: "test",
    )
    return sender, event, state, context


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_secondary_search_candidate_is_delivered(semantic_runtime, streaming):
    sender, event, state, context = semantic_runtime
    result = json.loads(await sender.search_memes_tool(event, "happy"))
    candidates = result["candidates"]
    assert len(candidates) == 2
    assert len({item["id"] for item in candidates}) == 2
    assert candidates[0]["_source_pack_id"] == "pack-b"
    assert "entry_id" not in candidates[0] and "score" not in candidates[0]
    selected = candidates[0]["id"]
    response = LLMResponse(role="assistant", completion_text=f"Hello &&{selected}&&")
    await sender._resp_semantic_impl(event, response, response.completion_text)
    assert state["meme_manager_semantic_selected_ids"] == [selected]
    state["result"] = MessageEventResult(
        chain=[Plain(response.completion_text)],
        result_content_type=ResultContentType.LLM_RESULT,
    )
    if streaming:
        await sender._send_memes_streaming(event)
        sender._send_meme_image.assert_awaited_once()
        image = sender._send_meme_image.await_args.args[1]
    else:
        await sender._on_decorating_result_impl(event)
        (image,) = state["meme_manager_pending_images"]
    assert "pack-b" in image.file
    changed_context = {**context, "pack_ids": ["pack-a"]}
    assert query.validate_selected_id(event, selected, changed_context) is None


@pytest.mark.asyncio
async def test_emotion_model_uses_secondary_pack(semantic_runtime):
    sender, event, state, context = semantic_runtime
    state["meme_manager_semantic_mode"] = "llm"
    sender._build_emotion_semantic_query = AsyncMock(return_value="happy")
    sender._build_meme_quantity_prompt = lambda: ""
    sender._build_emotion_llm_injection_suffix = lambda event: ""
    sender._emotion_llm_generate = AsyncMock(
        return_value=SimpleNamespace(
            completion_text='{"meme_id":"meme:123456789abc1111"}'
        )
    )
    response = LLMResponse(role="assistant", completion_text="Hello")
    await sender._resp_semantic_llm_impl(event, response, "Hello")
    assert state["meme_manager_semantic_selected_ids"] == ["meme:123456789abc1111"]
    assert query.validate_selected_id(
        event, "meme:123456789abc1111", context
    ).is_relative_to(context["pack_dirs"]["pack-b"])


@pytest.mark.asyncio
async def test_failed_primary_search_does_not_hide_secondary(semantic_runtime):
    sender, event, state, context = semantic_runtime
    resolve = sender._resolve_embedding_provider

    def provider_for_pack(pack_id):
        if pack_id == "pack-a":
            raise RuntimeError("Provider unavailable")
        return resolve(pack_id)

    sender._resolve_embedding_provider = provider_for_pack
    result = json.loads(await sender.search_memes_tool(event, "happy"))
    assert [item["_source_pack_id"] for item in result["candidates"]] == ["pack-b"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["load_metadata", "index_is_ready"])
async def test_primary_file_errors_do_not_abort_secondary_search(
    semantic_runtime, monkeypatch, stage
):
    sender, event, state, context = semantic_runtime
    original = getattr(query, stage)

    def fail_primary(directory, *args, **kwargs):
        pack_id = args[0] if stage == "index_is_ready" else directory.name
        if pack_id == "pack-a":
            raise PermissionError("Image is temporarily inaccessible")
        return original(directory, *args, **kwargs)

    monkeypatch.setattr(query, stage, fail_primary)
    result = json.loads(await sender.search_memes_tool(event, "happy"))
    assert [item["_source_pack_id"] for item in result["candidates"]] == ["pack-b"]


@pytest.mark.parametrize("stage", ["load_metadata", "index_is_ready"])
def test_primary_file_errors_do_not_abort_secondary_readiness(
    semantic_runtime, monkeypatch, stage
):
    sender, event, state, context = semantic_runtime
    sender.semantic_enabled = True
    sender.semantic_embedding_provider_id = ""
    sender._resolve_embedding_provider = lambda pack_id: SimpleNamespace(
        provider_config={"id": pack_id, "model": "embedding-model"},
        get_dim=lambda: 2,
        get_embedding=AsyncMock(return_value=[1, 0]),
    )
    monkeypatch.setattr(
        plugin_main, "load_metadata", lambda directory: {"images": {"entry": {}}}
    )
    monkeypatch.setattr(
        plugin_main, "semantic_metadata_is_complete", lambda *a, **k: True
    )
    monkeypatch.setattr(plugin_main, "index_is_ready", lambda *a, **k: True)
    original = getattr(plugin_main, stage)

    def fail_primary(*args, **kwargs):
        pack_id = args[1] if stage == "index_is_ready" else args[0].name
        if pack_id == "pack-a":
            raise PermissionError("Image is temporarily inaccessible")
        return original(*args, **kwargs)

    monkeypatch.setattr(plugin_main, stage, fail_primary)
    assert sender._semantic_pack_ready(event)


def test_legacy_category_images_include_secondary_pack(semantic_runtime):
    sender, event, state, context = semantic_runtime
    paths = sender._get_category_meme_files(event, "happy")
    assert len(paths) == 2
    assert {path.parents[2].name for path in paths} == {"pack-a", "pack-b"}
    assert sender._get_category_meme_files(event, "../happy") == []


@pytest.mark.asyncio
async def test_rules_api_checks_new_secondary_pack_index(runtime):
    storage.save_selection_rules(
        [{"id": "default", "scope": "default", "pack_id": "pack-a"}]
    )
    api = object.__new__(WebAPIMixin)
    api._reload_personas = lambda: None
    api.semantic_enabled = True
    checked = []

    def status(pack_id):
        checked.append(pack_id)
        return {"dimension_rebuild_required": True, "semantic_caption_complete": True}

    api.semantic_task_manager = SimpleNamespace(status=status)
    app = Quart(__name__)
    async with app.test_request_context(
        "/",
        method="POST",
        json={
            "rules": [
                {"id": "default", "scope": "default", "pack_ids": ["pack-a", "pack-b"]}
            ]
        },
    ):
        response, code = await api._api_settings_rules()
    assert code == 200
    assert (await response.get_json())["semantic_rebuild_packs"] == ["pack-b"]
    assert checked == ["pack-b"]


@pytest.mark.asyncio
async def test_real_indexes_use_each_packs_model_and_dimension(runtime, tmp_path):
    pytest.importorskip("faiss")
    providers = {}
    contexts = []

    class Provider:
        def __init__(self, pack_id, dimension):
            self.provider_config = {"id": pack_id, "embedding_model": pack_id}
            self.dimension = dimension

        def get_dim(self):
            return self.dimension

        async def get_embedding(self, text):
            return [1.0] + [0.0] * (self.dimension - 1)

    for dimension, pack_id in enumerate(("pack-a", "pack-b"), 2):
        directory = runtime.packs / pack_id
        image_path = directory / "memes" / "happy" / "meme.png"
        image_path.parent.mkdir(parents=True)
        PillowImage.new("RGB", (2, 2), color=(dimension * 40, 0, 0)).save(image_path)
        (scanned,) = semantic_storage.scan_images(directory)
        image = SemanticImage(
            content_sha256=scanned["content_sha256"],
            relative_path=scanned["relative_path"],
            category="happy",
            caption=pack_id,
            tags=["happy"],
            caption_status="done",
        )
        semantic_storage.save_metadata(
            directory,
            {
                "file_total": 1,
                "unique_total": 1,
                "images": {scanned["entry_id"]: image.to_dict()},
            },
        )
        provider = Provider(pack_id, dimension)
        providers[pack_id] = provider
        await semantic_index.build_index(
            directory, tmp_path, pack_id, semantic_index.EmbeddingAdapter(provider)
        )
        contexts.append({"pack_id": pack_id, "pack_dir": directory})
    result = await query.search_memes_multi_pack(
        contexts, tmp_path, "happy", providers.__getitem__
    )
    assert {item["_source_pack_id"] for item in result["candidates"]} == {
        "pack-a",
        "pack-b",
    }
