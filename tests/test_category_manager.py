import json

import pytest
from astrbot_plugin_meme_manager.backend import category_manager as category_module


@pytest.fixture
def category_context(tmp_path, monkeypatch):
    pack_dir = tmp_path / "pack"
    memes_dir = pack_dir / "memes"
    memes_dir.mkdir(parents=True)
    metadata_path = pack_dir / "memes_data.json"
    manifest_path = pack_dir / "manifest.json"
    metadata_path.write_text(
        json.dumps({"happy": "开心"}, ensure_ascii=False), encoding="utf-8"
    )
    manifest_path.write_text(
        json.dumps(
            {
                "id": "test-pack",
                "name": "测试包",
                "version": "1.0.0",
                "categories": {"happy": {"description": "开心"}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    context = {
        "pack_id": "test-pack",
        "pack_dir": pack_dir,
        "memes_dir": memes_dir,
        "metadata_path": metadata_path,
        "manifest_path": manifest_path,
        "category_mapping": {"happy": "开心"},
    }
    monkeypatch.setattr(category_module, "resolve_pack_context", lambda: context)
    return context


def test_safe_category_name_and_directory_resolution(category_context):
    assert category_module.is_safe_category_name("happy")
    assert not category_module.is_safe_category_name(" happy ")
    assert not category_module.is_safe_category_name("a/b")
    assert not category_module.is_safe_category_name("..")
    assert (
        category_module.resolve_safe_category_directory(
            category_context["memes_dir"], "happy"
        )
        == (category_context["memes_dir"] / "happy").resolve()
    )


def test_initialization_builds_metadata_from_manifest_and_directories(
    category_context,
):
    category_context["metadata_path"].unlink()
    (category_context["memes_dir"] / "happy").mkdir()
    (category_context["memes_dir"] / "sad").mkdir()
    manager = category_module.CategoryManager()
    assert manager.descriptions == {"happy": "开心", "sad": "请添加描述"}
    assert json.loads(
        category_context["metadata_path"].read_text(encoding="utf-8")
    ) == (manager.descriptions)


def test_create_update_rename_and_delete_category(category_context):
    manager = category_module.CategoryManager()
    assert manager.create_category("sad", "伤心")
    assert (category_context["memes_dir"] / "sad").is_dir()
    assert manager.update_description("sad", "非常伤心")
    assert manager.rename_category("sad", "cry")
    assert not (category_context["memes_dir"] / "sad").exists()
    assert (category_context["memes_dir"] / "cry").is_dir()
    assert manager.delete_category("cry")
    assert not (category_context["memes_dir"] / "cry").exists()
    assert "cry" not in manager.get_descriptions()
    manifest = json.loads(category_context["manifest_path"].read_text(encoding="utf-8"))
    assert set(manifest["categories"]) == {"happy"}


def test_category_mutations_reject_invalid_or_conflicting_names(category_context):
    manager = category_module.CategoryManager()
    assert not manager.create_category("../bad", "bad")
    assert not manager.update_description("../bad", "bad")
    assert not manager.rename_category("missing", "new")
    assert manager.create_category("sad", "伤心")
    assert not manager.rename_category("happy", "sad")
    assert not manager.delete_category("../bad")


def test_rename_rejects_existing_target_directory(category_context):
    manager = category_module.CategoryManager()
    (category_context["memes_dir"] / "happy").mkdir()
    (category_context["memes_dir"] / "occupied").mkdir()
    assert not manager.rename_category("happy", "occupied")


def test_remove_from_config_keeps_directory(category_context):
    manager = category_module.CategoryManager()
    category_path = category_context["memes_dir"] / "happy"
    category_path.mkdir()
    assert manager.remove_from_config("happy")
    assert category_path.is_dir()
    assert "happy" not in manager.get_descriptions()
    assert not manager.remove_from_config("missing")
    assert not manager.remove_from_config("../bad")


def test_sync_status_reports_missing_and_deleted_categories(category_context):
    manager = category_module.CategoryManager()
    (category_context["memes_dir"] / "local-only").mkdir()
    missing_in_config, deleted_categories = manager.get_sync_status()
    assert missing_in_config == ["local-only"]
    assert deleted_categories == ["happy"]


def test_sync_with_filesystem_aligns_descriptions(category_context):
    manager = category_module.CategoryManager()
    (category_context["memes_dir"] / "local-only").mkdir()
    assert manager.sync_with_filesystem()
    assert manager.get_descriptions() == {"local-only": "请添加描述"}
    assert manager.sync_with_filesystem()


def test_semantic_invalidation_only_runs_when_metadata_exists(
    category_context, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        category_module,
        "invalidate_semantic_metadata",
        lambda pack_dir: calls.append(pack_dir),
    )
    manager = category_module.CategoryManager()
    assert manager.update_description("happy", "更开心")
    assert calls == []
    (category_context["pack_dir"] / "semantic_metadata.json").write_text(
        "{}", encoding="utf-8"
    )
    assert manager.update_description("happy", "最开心")
    assert calls == [category_context["pack_dir"].resolve()]
