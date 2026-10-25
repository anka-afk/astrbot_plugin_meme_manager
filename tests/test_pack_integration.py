import json
import zipfile

import pytest
from astrbot_plugin_meme_manager.backend.packs import storage
from astrbot_plugin_meme_manager.config import DEFAULT_PACK_ID
from astrbot_plugin_meme_manager.main import MemeSender
from astrbot_plugin_meme_manager.mixins import web_api

SOURCE = {"type": "github", "repo": "test/repo", "ref": "v1", "subpath": "."}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    packs = tmp_path / "packs"
    for name, path in {
        "PACKS_DIR": packs,
        "PLUGIN_DATA_DIR": tmp_path,
        "TEMP_DIR": tmp_path / "temp",
        "REGISTRY_PATH": tmp_path / "registry.json",
        "SELECTION_RULES_PATH": tmp_path / "selection_rules.json",
    }.items():
        monkeypatch.setattr(storage, name, path)
    monkeypatch.setattr(web_api, "PACKS_DIR", packs)
    storage._create_empty_pack(DEFAULT_PACK_ID)
    storage._save_registry({"installed_packs": [{"id": DEFAULT_PACK_ID}]})
    storage.set_default_pack(DEFAULT_PACK_ID)
    sender = object.__new__(MemeSender)
    sender.config = {}
    sender._semantic_operation_guard = lambda *_: None

    def download(repo, ref, target_zip_path, **kwargs):
        assert (repo, ref) == (SOURCE["repo"], SOURCE["ref"])
        with zipfile.ZipFile(target_zip_path, "w") as archive:
            archive.writestr(
                "repo/manifest.json",
                json.dumps(
                    {
                        "schema_version": 1,
                        "id": "sample-pack",
                        "name": "Sample",
                        "version": "1.0.0",
                        "categories": {"happy": {"description": "Joy"}},
                    }
                ),
            )
            archive.writestr("repo/memes/happy/one.png", b"image")

    monkeypatch.setattr(storage, "_download_github_archive", download)
    return sender, tmp_path, packs


@pytest.mark.asyncio
async def test_install_preserves_rules_saved_during_preparation(runtime, monkeypatch):
    sender, data, packs = runtime
    prepare = storage._prepare_import_pack
    saved_rules = []

    def prepare_with_rule_save(*args, **kwargs):
        result = prepare(*args, **kwargs)
        storage.save_selection_rules(
            [
                {
                    "id": "session",
                    "scope": "session",
                    "target": "group-1",
                    "pack_id": DEFAULT_PACK_ID,
                },
                {"id": "default", "scope": "default", "pack_id": DEFAULT_PACK_ID},
            ]
        )
        saved_rules.append((data / "selection_rules.json").read_bytes())
        return result

    monkeypatch.setattr(storage, "_prepare_import_pack", prepare_with_rule_save)
    result = await sender.install_pack(SOURCE, expected_pack_id="sample-pack")
    assert result["pack_id"] == "sample-pack"
    assert result["source"] == SOURCE
    assert (data / "selection_rules.json").read_bytes() == saved_rules[0]
    assert (packs / DEFAULT_PACK_ID).is_dir()
    assert await sender.get_pack_status("sample-pack") == {
        "pack_id": "sample-pack",
        "status": "ready",
        "image_count": 1,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("collision", [False, True])
async def test_identity_failure_preserves_all_runtime_files(runtime, collision):
    sender, data, packs = runtime
    if collision:
        storage._create_empty_pack("sample-pack")
    before = {
        p.relative_to(data): p.read_bytes() for p in data.rglob("*") if p.is_file()
    }
    with pytest.raises(FileExistsError if collision else ValueError):
        await sender.install_pack(
            SOURCE, expected_pack_id="sample-pack" if collision else "different-pack"
        )
    after = {
        p.relative_to(data): p.read_bytes() for p in data.rglob("*") if p.is_file()
    }
    assert after == before
    assert not (packs / "sample-pack-2").exists()


@pytest.mark.asyncio
async def test_status_missing_and_invalid_id(runtime):
    sender, _, _ = runtime
    assert await sender.get_pack_status("absent") == {
        "pack_id": "absent",
        "status": "missing",
        "image_count": 0,
    }
    for invalid_id in ("../outside", ".."):
        with pytest.raises(ValueError):
            await sender.get_pack_status(invalid_id)
