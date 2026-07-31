import json

from astrbot_plugin_meme_manager import config


def test_resolve_plugin_name_and_data_directory(monkeypatch, tmp_path):
    assert config.resolve_plugin_name(None) == config.DEFAULT_PLUGIN_NAME
    assert config.resolve_plugin_name(" custom ") == "custom"
    assert config.resolve_plugin_name(" ") == config.DEFAULT_PLUGIN_NAME
    monkeypatch.setattr(config, "get_astrbot_plugin_data_path", lambda: str(tmp_path))
    assert config.get_plugin_data_dir("demo") == (tmp_path / "demo").resolve()


def test_get_plugin_data_dir_falls_back_when_astrbot_path_fails(monkeypatch):
    monkeypatch.setattr(
        config,
        "get_astrbot_plugin_data_path",
        lambda: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    expected = (config.PLUGIN_DIR / "data" / "plugin_data" / "demo").resolve()
    assert config.get_plugin_data_dir("demo") == expected


def test_json_file_helpers_and_content_detection(tmp_path):
    path = tmp_path / "nested" / "data.json"
    config._save_json_file(path, {"text": "开心"})
    assert config._load_json_file(path, {}) == {"text": "开心"}
    path.write_text("not-json", encoding="utf-8")
    assert config._load_json_file(path, {"fallback": True}) == {"fallback": True}
    assert not config._plugin_data_dir_has_content(tmp_path / "empty")
    plugin_data = tmp_path / "plugin"
    plugin_data.mkdir()
    (plugin_data / "memes_data.json").write_text("{}", encoding="utf-8")
    assert config._plugin_data_dir_has_content(plugin_data)


def test_copy_directory_contents_merges_without_overwriting(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    (source / "nested").mkdir(parents=True)
    target.mkdir()
    (source / "file.txt").write_text("source", encoding="utf-8")
    (source / "nested" / "child.txt").write_text("child", encoding="utf-8")
    (target / "file.txt").write_text("target", encoding="utf-8")
    config._copy_directory_contents(source, target)
    assert (target / "file.txt").read_text(encoding="utf-8") == "target"
    assert (target / "nested" / "child.txt").read_text(encoding="utf-8") == "child"


def test_migrate_legacy_data_only_when_target_empty(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "memes_data.json").write_text('{"happy":"开心"}', encoding="utf-8")
    target = tmp_path / "target"
    monkeypatch.setattr(config, "get_legacy_plugin_data_dir", lambda: legacy)
    config.migrate_legacy_data_dir_if_needed(target)
    assert (target / "memes_data.json").is_file()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    (legacy / "keep.txt").write_text("overwrite", encoding="utf-8")
    config.migrate_legacy_data_dir_if_needed(target)
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_collect_descriptions_combines_metadata_fallback_and_directories(tmp_path):
    memes_dir = tmp_path / "memes"
    (memes_dir / "local").mkdir(parents=True)
    metadata = tmp_path / "memes_data.json"
    metadata.write_text(
        json.dumps({"happy": "元数据", "": "忽略"}, ensure_ascii=False),
        encoding="utf-8",
    )
    result = config._collect_category_descriptions(
        metadata, memes_dir, {"happy": "默认", "sad": "伤心"}
    )
    assert result == {"happy": "元数据", "sad": "伤心", "local": "请添加描述"}


def test_pack_manifest_uses_stable_names_and_sorted_categories():
    manifest = config._build_pack_manifest(
        config.DEFAULT_PACK_ID, {"sad": "伤心", "happy": "开心"}
    )
    assert manifest["name"] == "Builtin Default Meme Pack"
    assert list(manifest["categories"]) == ["happy", "sad"]
    legacy = config._build_pack_manifest(config.LEGACY_MIGRATED_PACK_ID, {})
    assert legacy["name"] == "Migrated Legacy Meme Pack"
    custom = config._build_pack_manifest("custom", {})
    assert custom["name"] == "Meme Pack custom"


def test_bootstrap_fresh_runtime_creates_builtin_registry_and_rules(tmp_path):
    config._bootstrap_pack_runtime(tmp_path)
    builtin = tmp_path / "packs" / config.DEFAULT_PACK_ID
    assert (builtin / "memes").is_dir()
    assert (builtin / "manifest.json").is_file()
    registry = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    assert registry["installed_packs"][0]["id"] == config.DEFAULT_PACK_ID
    rules = json.loads((tmp_path / "selection_rules.json").read_text(encoding="utf-8"))
    assert rules["rules"][0]["pack_id"] == config.DEFAULT_PACK_ID
    for directory in ("backup", "migration", "temp"):
        assert (tmp_path / directory).is_dir()


def test_bootstrap_migrates_legacy_root_data(tmp_path):
    legacy_memes = tmp_path / "memes" / "happy"
    legacy_memes.mkdir(parents=True)
    (legacy_memes / "meme.png").write_bytes(b"image")
    (tmp_path / "memes_data.json").write_text(
        json.dumps({"happy": "开心"}, ensure_ascii=False), encoding="utf-8"
    )
    config._bootstrap_pack_runtime(tmp_path)
    migrated = tmp_path / "packs" / config.LEGACY_MIGRATED_PACK_ID
    assert (migrated / "memes" / "happy" / "meme.png").is_file()
    assert (tmp_path / "migration" / "legacy_runtime_migrated.json").is_file()
    rules = json.loads((tmp_path / "selection_rules.json").read_text(encoding="utf-8"))
    assert rules["rules"][0]["pack_id"] == config.LEGACY_MIGRATED_PACK_ID


def test_resolve_default_pack_id_prefers_valid_rule_then_legacy(tmp_path):
    custom = tmp_path / "packs" / "custom"
    custom.mkdir(parents=True)
    config._write_default_selection_rules(tmp_path, "custom")
    assert config._resolve_default_pack_id(tmp_path) == "custom"

    (tmp_path / "selection_rules.json").unlink()
    legacy_memes = tmp_path / "packs" / config.LEGACY_MIGRATED_PACK_ID / "memes"
    legacy_memes.mkdir(parents=True)
    (legacy_memes / "meme.png").write_bytes(b"image")
    assert config._resolve_default_pack_id(tmp_path) == config.LEGACY_MIGRATED_PACK_ID
