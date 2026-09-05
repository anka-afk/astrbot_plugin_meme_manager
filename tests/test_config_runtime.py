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


def test_pack_manifest_uses_stable_names_and_sorted_categories():
    manifest = config._build_pack_manifest(
        config.DEFAULT_PACK_ID, {"sad": "伤心", "happy": "开心"}
    )
    assert manifest["name"] == "Builtin Default Meme Pack"
    assert list(manifest["categories"]) == ["happy", "sad"]
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
    for directory in ("backup", "temp"):
        assert (tmp_path / directory).is_dir()


def test_bootstrap_ignores_legacy_root_data_without_deleting_it(tmp_path):
    legacy_memes = tmp_path / "memes" / "happy"
    legacy_memes.mkdir(parents=True)
    (legacy_memes / "meme.png").write_bytes(b"image")
    (tmp_path / "memes_data.json").write_text(
        json.dumps({"happy": "开心"}, ensure_ascii=False), encoding="utf-8"
    )
    config._bootstrap_pack_runtime(tmp_path)
    assert not (tmp_path / "packs" / "legacy-migrated").exists()
    assert not (tmp_path / "migration").exists()
    assert (legacy_memes / "meme.png").read_bytes() == b"image"
    rules = json.loads((tmp_path / "selection_rules.json").read_text(encoding="utf-8"))
    assert rules["rules"][0]["pack_id"] == config.DEFAULT_PACK_ID


def test_resolve_default_pack_id_has_no_legacy_directory_fallback(tmp_path):
    custom = tmp_path / "packs" / "custom"
    custom.mkdir(parents=True)
    config._write_default_selection_rules(tmp_path, "custom")
    assert config._resolve_default_pack_id(tmp_path) == "custom"

    (tmp_path / "selection_rules.json").unlink()
    legacy_memes = tmp_path / "packs" / "legacy-migrated" / "memes"
    legacy_memes.mkdir(parents=True)
    (legacy_memes / "meme.png").write_bytes(b"image")
    assert config._resolve_default_pack_id(tmp_path) == config.DEFAULT_PACK_ID


def test_bootstrap_preserves_installed_pack_manifest_and_rules(tmp_path):
    config._bootstrap_pack_runtime(tmp_path)
    manifest = tmp_path / "packs" / config.DEFAULT_PACK_ID / "manifest.json"
    custom = {"id": config.DEFAULT_PACK_ID, "name": "My pack", "categories": {}}
    manifest.write_text(json.dumps(custom), encoding="utf-8")
    before = {
        name: (tmp_path / name).read_bytes()
        for name in ("registry.json", "selection_rules.json")
    }
    config._bootstrap_pack_runtime(tmp_path)
    assert json.loads(manifest.read_text(encoding="utf-8")) == custom
    for name, content in before.items():
        assert (tmp_path / name).read_bytes() == content


def test_startup_no_longer_exports_frozen_pack_directory_aliases():
    for name in (
        "MEMES_DIR",
        "MEMES_DATA_PATH",
        "ACTIVE_PACK_ID",
        "ACTIVE_PACK_DIR",
        "sync_active_pack_metadata",
        "migrate_legacy_data_dir_if_needed",
    ):
        assert not hasattr(config, name)
