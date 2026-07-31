import json

import pytest
from astrbot_plugin_meme_manager.backend import pack_resolver as resolver


@pytest.fixture
def resolver_paths(tmp_path, monkeypatch):
    packs_dir = tmp_path / "packs"
    packs_dir.mkdir()
    registry_path = tmp_path / "registry.json"
    rules_path = tmp_path / "selection_rules.json"
    monkeypatch.setattr(resolver, "PACKS_DIR", packs_dir)
    monkeypatch.setattr(resolver, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(resolver, "SELECTION_RULES_PATH", rules_path)
    monkeypatch.setattr(resolver, "DEFAULT_PACK_ID", "builtin-default")
    monkeypatch.setattr(resolver, "LEGACY_MIGRATED_PACK_ID", "legacy-migrated")
    monkeypatch.setattr(resolver, "DEFAULT_CATEGORY_DESCRIPTIONS", {"happy": "开心"})
    return packs_dir, registry_path, rules_path


def write_runtime_data(paths, packs, rules):
    packs_dir, registry_path, rules_path = paths
    for pack in packs:
        (packs_dir / pack["id"]).mkdir(exist_ok=True)
    registry_path.write_text(json.dumps({"installed_packs": packs}), encoding="utf-8")
    rules_path.write_text(json.dumps({"rules": rules}), encoding="utf-8")


def test_resolve_pack_id_prefers_matching_session_then_persona(resolver_paths):
    packs = [
        {"id": "session-pack", "enabled": True},
        {"id": "persona-pack", "enabled": True},
        {"id": "default-pack", "enabled": True},
    ]
    rules = [
        {"scope": "default", "pack_id": "default-pack"},
        {"scope": "session", "target": "session-1", "pack_id": "session-pack"},
        {"scope": "persona", "target": "persona-1", "pack_id": "persona-pack"},
    ]
    write_runtime_data(resolver_paths, packs, rules)
    assert resolver.resolve_pack_id("session-1", "persona-1") == "session-pack"
    assert resolver.resolve_pack_id("other", "persona-1") == "persona-pack"
    assert resolver.resolve_pack_id("other", "other") == "default-pack"


def test_resolve_pack_id_skips_disabled_and_missing_rule_targets(resolver_paths):
    packs = [
        {"id": "disabled-pack", "enabled": False},
        {"id": "enabled-pack", "enabled": True},
    ]
    rules = [
        {"scope": "session", "target": "session-1", "pack_id": "disabled-pack"},
        {"scope": "persona", "target": "persona-1", "pack_id": "missing-pack"},
    ]
    write_runtime_data(resolver_paths, packs, rules)
    assert resolver.resolve_pack_id("session-1", "persona-1") == "enabled-pack"


@pytest.mark.parametrize("fallback_id", ["legacy-migrated", "builtin-default"])
def test_resolve_pack_id_allows_builtin_fallback_without_registry(
    resolver_paths, fallback_id
):
    packs_dir, registry_path, rules_path = resolver_paths
    (packs_dir / fallback_id).mkdir()
    registry_path.write_text("{}", encoding="utf-8")
    rules_path.write_text("{}", encoding="utf-8")
    assert resolver.resolve_pack_id() == fallback_id


def test_resolve_pack_id_returns_builtin_when_runtime_files_are_corrupt(
    resolver_paths,
):
    _, registry_path, rules_path = resolver_paths
    registry_path.write_text("not-json", encoding="utf-8")
    rules_path.write_text("not-json", encoding="utf-8")
    assert resolver.resolve_pack_id() == "builtin-default"


def test_get_pack_paths_uses_pack_root(resolver_paths):
    paths = resolver.get_pack_paths("demo")
    assert paths["pack_dir"] == resolver_paths[0] / "demo"
    assert paths["memes_dir"] == resolver_paths[0] / "demo" / "memes"
    assert paths["metadata_path"].name == "memes_data.json"
    assert paths["manifest_path"].name == "manifest.json"


def test_load_pack_category_mapping_prefers_metadata(resolver_paths):
    pack_dir = resolver_paths[0] / "demo"
    pack_dir.mkdir()
    (pack_dir / "memes_data.json").write_text(
        json.dumps({"happy": "元数据", "needs_review": "隐藏"}), encoding="utf-8"
    )
    (pack_dir / "manifest.json").write_text(
        json.dumps({"categories": {"sad": "清单"}}), encoding="utf-8"
    )
    assert resolver.load_pack_category_mapping("demo") == {"happy": "元数据"}


def test_load_pack_category_mapping_falls_back_to_manifest(resolver_paths):
    pack_dir = resolver_paths[0] / "demo"
    pack_dir.mkdir()
    (pack_dir / "manifest.json").write_text(
        json.dumps(
            {
                "categories": {
                    "happy": {"description": "开心"},
                    "sad": "伤心",
                    "": "忽略",
                    "../bad": "忽略",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert resolver.load_pack_category_mapping("demo") == {
        "happy": "开心",
        "sad": "伤心",
    }


def test_load_pack_category_mapping_uses_builtin_defaults(resolver_paths):
    (resolver_paths[0] / "builtin-default").mkdir()
    assert resolver.load_pack_category_mapping("builtin-default") == {"happy": "开心"}
    assert resolver.load_pack_category_mapping("other") == {}


def test_resolve_pack_context_combines_paths_and_mapping(resolver_paths):
    packs = [{"id": "demo", "enabled": True}]
    rules = [{"scope": "default", "pack_id": "demo"}]
    write_runtime_data(resolver_paths, packs, rules)
    pack_dir = resolver_paths[0] / "demo"
    (pack_dir / "memes_data.json").write_text(
        json.dumps({"happy": "开心"}), encoding="utf-8"
    )
    context = resolver.resolve_pack_context()
    assert context["pack_id"] == "demo"
    assert context["pack_dir"] == pack_dir
    assert context["category_mapping"] == {"happy": "开心"}
