"""Cover pack boundaries, restore failures and external archive handling."""

import base64
import json
import zipfile
from pathlib import Path

import pytest
from astrbot_plugin_meme_manager.backend.packs import resolver, storage
from astrbot_plugin_meme_manager.backend.semantic import index as semantic_index
from astrbot_plugin_meme_manager.backend.semantic import storage as semantic_storage
from astrbot_plugin_meme_manager.backend.semantic.task import SemanticTaskManager
from astrbot_plugin_meme_manager.mixins import web_api
from astrbot_plugin_meme_manager.mixins.event_handlers import EventHandlerMixin
from PIL import Image
from quart import Quart


@pytest.fixture
def audit_runtime(tmp_path, monkeypatch):
    paths = {
        "PACKS_DIR": tmp_path / "packs",
        "PLUGIN_DATA_DIR": tmp_path,
        "TEMP_DIR": tmp_path / "temp",
        "BACKUP_DIR": tmp_path / "backup",
        "REGISTRY_PATH": tmp_path / "registry.json",
        "SELECTION_RULES_PATH": tmp_path / "selection_rules.json",
        "COMMUNITY_CACHE_PATH": tmp_path / "community_cache.json",
    }
    for name, path in paths.items():
        monkeypatch.setattr(storage, name, path)
    monkeypatch.setattr(resolver, "PACKS_DIR", paths["PACKS_DIR"])
    monkeypatch.setattr(web_api, "PACKS_DIR", paths["PACKS_DIR"])
    manifest = {
        "id": "audit-pack",
        "name": "Audit pack",
        "version": "1.0",
        "categories": {"happy": {"description": "Joy"}},
    }
    pack = paths["PACKS_DIR"] / "audit-pack"
    (pack / "memes/happy").mkdir(parents=True)
    storage._save_json(pack / "manifest.json", manifest)
    storage._save_json(pack / "memes_data.json", {"happy": "Joy"})
    Image.new("RGB", (8, 8), "red").save(pack / "memes/happy/image.png")
    storage._save_registry({"installed_packs": [{"id": "audit-pack"}]})
    storage.set_default_pack("audit-pack")
    return tmp_path, pack, manifest


@pytest.mark.parametrize(
    "pack_id", ["..", ".", "../other", "other\\pack", "audit-pack."]
)
def test_invalid_pack_ids_leave_runtime_untouched(audit_runtime, pack_id):
    root, pack, _ = audit_runtime
    original = (pack / "memes/happy/image.png").read_bytes()
    manager = SemanticTaskManager(root)
    for operation in (
        storage.uninstall_pack,
        storage.get_pack_detail,
        storage.set_default_pack,
        resolver.get_pack_paths,
        manager._validate_pack_id,
        lambda value: semantic_index.index_dir(root, value),
    ):
        with pytest.raises(ValueError):
            operation(pack_id)
    assert (pack / "memes/happy/image.png").read_bytes() == original
    assert storage._load_selection_rules()["rules"][0]["pack_id"] == "audit-pack"


@pytest.mark.asyncio
async def test_uninstall_api_uses_real_operation_guard(audit_runtime):
    root, pack, _ = audit_runtime
    subject = web_api.WebAPIMixin()
    subject.semantic_task_manager = SemanticTaskManager(root)
    subject._reload_personas = lambda: None
    app = Quart(__name__)
    app.add_url_rule(
        "/uninstall", view_func=subject._api_uninstall_pack, methods=["POST"]
    )
    response = await app.test_client().post("/uninstall", json={"pack_id": ".."})
    assert response.status_code == 400
    assert (pack / "memes/happy/image.png").is_file()
    assert subject.semantic_task_manager._external_pack_operations == {}
    response = await app.test_client().post(
        "/uninstall", json={"pack_id": "audit-pack"}
    )
    assert response.status_code == 200
    assert not pack.exists()
    assert (storage.PACKS_DIR / "builtin-default/manifest.json").is_file()
    assert subject.semantic_task_manager._external_pack_operations == {}


@pytest.mark.parametrize("link_location", ["pack_root", "index_root", "contents"])
def test_uninstall_handles_links_without_deleting_external_data(
    audit_runtime, link_location
):
    root, pack, _ = audit_runtime
    external = root / "external"
    external.mkdir()
    sentinel = external / "keep.txt"
    sentinel.write_bytes(b"outside library")
    index_dir = root / "semantic_indexes/audit-pack"
    index_dir.parent.mkdir()
    try:
        if link_location == "pack_root":
            pack.rename(root / "original-pack")
            pack.symlink_to(external, target_is_directory=True)
        elif link_location == "index_root":
            index_dir.symlink_to(external, target_is_directory=True)
        else:
            (pack / "linked-directory").symlink_to(external, target_is_directory=True)
            (pack / "memes/happy/linked.txt").symlink_to(sentinel)
            index_dir.mkdir()
            (index_dir / "linked-directory").symlink_to(
                external, target_is_directory=True
            )
    except (OSError, NotImplementedError):
        pytest.skip("Creating filesystem links is unavailable")
    if link_location == "contents":
        storage.uninstall_pack("audit-pack")
        assert not pack.exists()
        assert not index_dir.exists()
    else:
        with pytest.raises(ValueError):
            storage.uninstall_pack("audit-pack")
        assert pack.exists()
    assert sentinel.read_bytes() == b"outside library"


@pytest.mark.parametrize(
    "failure", ["replace", "index_move", "index_create", "registry"]
)
def test_runtime_restore_recovers_original_pack_on_failure(
    audit_runtime, monkeypatch, failure
):
    root, pack, manifest = audit_runtime
    registry_before = storage.REGISTRY_PATH.read_bytes()
    index_dir = root / "semantic_indexes"
    (index_dir / "audit-pack").mkdir(parents=True)
    (index_dir / "audit-pack/original.bin").write_bytes(b"original index")
    archive_path = root / "restore.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("packs/audit-pack/manifest.json", json.dumps(manifest))
        archive.writestr("packs/audit-pack/memes/happy/new.png", b"replacement")
        for name in ("prepared_packs", "old_runtime_packs", "old_runtime_indexes"):
            archive.writestr(f"{name}/extra.txt", b"archive-owned file")
    move = storage.shutil.move
    mkdir = Path.mkdir

    def move_with_failure(source, destination, *args, **kwargs):
        if failure == "replace" and Path(source).name == "prepared_packs":
            raise PermissionError("Replacement temporarily unavailable")
        if failure == "index_move" and Path(source) == index_dir:
            raise PermissionError("Index temporarily unavailable")
        return move(source, destination, *args, **kwargs)

    def mkdir_with_failure(path, *args, **kwargs):
        if failure == "index_create" and path == index_dir and not path.exists():
            raise PermissionError("Index directory temporarily unavailable")
        return mkdir(path, *args, **kwargs)

    def registry_failure(_payload):
        raise PermissionError("Registry temporarily unavailable")

    monkeypatch.setattr(storage.shutil, "move", move_with_failure)
    monkeypatch.setattr(Path, "mkdir", mkdir_with_failure)
    if failure == "registry":
        monkeypatch.setattr(storage, "_save_registry", registry_failure)
    with pytest.raises(PermissionError):
        storage.import_runtime_backup(archive_path, overwrite=True)
    assert (pack / "memes/happy/image.png").is_file()
    assert not (pack / "memes/happy/new.png").exists()
    assert not (storage.PACKS_DIR / "packs").exists()
    assert storage.REGISTRY_PATH.read_bytes() == registry_before
    assert (index_dir / "audit-pack/original.bin").read_bytes() == b"original index"


def test_runtime_restore_keeps_recovery_copy_when_rollback_is_blocked(
    audit_runtime, monkeypatch
):
    root, _, manifest = audit_runtime
    archive_path = root / "restore.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("packs/audit-pack/manifest.json", json.dumps(manifest))
        archive.writestr("packs/audit-pack/memes/happy/new.png", b"replacement")
    move = storage.shutil.move
    rename = Path.rename

    def fail_replacement(source, destination, *args, **kwargs):
        if Path(source).name == "prepared_packs":
            raise PermissionError("Replacement temporarily unavailable")
        return move(source, destination, *args, **kwargs)

    def fail_rollback(source, destination):
        if source.name.startswith(".packs-runtime_restore_"):
            raise PermissionError("Rollback temporarily unavailable")
        return rename(source, destination)

    monkeypatch.setattr(storage.shutil, "move", fail_replacement)
    monkeypatch.setattr(Path, "rename", fail_rollback)
    with pytest.raises(PermissionError):
        storage.import_runtime_backup(archive_path, overwrite=True)
    copies = list(root.glob(".packs-runtime_restore_*.old"))
    assert len(copies) == 1
    assert (copies[0] / "audit-pack/memes/happy/image.png").is_file()


def test_runtime_backup_ignores_archive_owned_transaction_names(audit_runtime):
    root, pack, manifest = audit_runtime
    archive_path = root / "restore.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("packs/audit-pack/manifest.json", json.dumps(manifest))
        archive.writestr("packs/audit-pack/memes/happy/new.png", b"replacement")
        for name in ("prepared_packs", "old_runtime_packs", "old_runtime_indexes"):
            archive.writestr(f"{name}/extra.txt", b"archive-owned file")
    result = storage.import_runtime_backup(archive_path, overwrite=True)
    assert result["restored_packs"] == 1
    assert (pack / "memes/happy/new.png").read_bytes() == b"replacement"
    assert not (storage.PACKS_DIR / "packs").exists()
    assert not list(root.glob(".packs-runtime_restore_*.old"))


def test_runtime_backup_rejects_linked_content(audit_runtime):
    root, pack, _ = audit_runtime
    external = root / "external.txt"
    external.write_text("Outside library")
    link = pack / "linked.txt"
    try:
        link.symlink_to(external)
    except (OSError, NotImplementedError):
        pytest.skip("Creating filesystem links is unavailable")
    with pytest.raises(ValueError):
        storage.export_runtime_backup()
    assert not list((root / "backup").glob("*.zip"))


def test_imported_vectors_are_not_deserialized(audit_runtime, monkeypatch):
    root, pack, manifest = audit_runtime
    metadata = semantic_storage.reconcile_metadata(pack)
    for item in metadata["images"].values():
        item.update(caption="A cheerful reaction", tags=["joy"], caption_status="done")
    archive_path = root / "vectors.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr(
            "memes/happy/image.png", (pack / "memes/happy/image.png").read_bytes()
        )
        archive.writestr("semantic_metadata.json", json.dumps(metadata))
        archive.writestr(
            "meme_pack_export.json",
            json.dumps(
                {
                    "format": "astrbot-meme-pack",
                    "format_version": 2,
                    "export_mode": "backup",
                    "features": {"vectors": True},
                }
            ),
        )
        archive.writestr("semantic_index/index.faiss", b"external-index-placeholder")
        archive.writestr(
            "semantic_index/index_manifest.json",
            json.dumps(
                {
                    "index_format": semantic_index.INDEX_FORMAT,
                    "metadata_schema_version": "2.0",
                    "item_count": 1,
                    "embedding_provider_id": "audit-provider",
                    "embedding_model": "audit-model",
                    "embedding_dimension": 2,
                }
            ),
        )

    def reject_native_read(*_args, **_kwargs):
        pytest.fail("Imported bytes must not enter the native index reader")

    monkeypatch.setattr(semantic_index, "_import_faiss_modules", reject_native_read)
    inspection = storage.inspect_pack_archive(archive_path)
    assert inspection["warnings"]
    result = storage.import_pack_archive(
        archive_path,
        preserve_existing_selection=True,
    )
    imported_pack = storage.PACKS_DIR / result["pack_id"]
    imported = semantic_storage.load_metadata(imported_pack)
    assert result["vectors_restored"] is False
    assert result["vector_warning"]
    assert not (imported_pack / "semantic_index").exists()
    assert not (root / "semantic_indexes" / result["pack_id"]).exists()
    assert any(
        item["caption"] == "A cheerful reaction" for item in imported["images"].values()
    )
    assert all(
        item["embedding_status"] != "done" for item in imported["images"].values()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("overwrite", [False, True])
async def test_backup_round_trip_rebuilds_search_and_preserves_manual_text(
    audit_runtime, overwrite
):
    pytest.importorskip("faiss")
    root, pack, _ = audit_runtime

    class Provider:
        provider_config = {"id": "audit-provider", "embedding_model": "audit-model"}

        def __init__(self):
            self.calls = 0

        def get_dim(self):
            return 2

        async def get_embedding(self, _text):
            self.calls += 1
            return [1.0, 0.0]

    provider = Provider()
    embedding = semantic_index.EmbeddingAdapter(provider)
    metadata = semantic_storage.reconcile_metadata(pack)
    for item in metadata["images"].values():
        item.update(
            caption="A manually reviewed cheerful reaction",
            tags=["joy"],
            visible_text="Hello",
            caption_status="done",
            manual_override=True,
            provenance="manual",
        )
    semantic_storage.save_metadata(pack, metadata)
    await semantic_index.build_index(pack, root, "audit-pack", embedding)
    exported = storage.export_pack_archive("audit-pack", export_mode="backup")
    result = storage.import_pack_archive(
        Path(exported["archive_path"]),
        overwrite=overwrite,
        preserve_existing_selection=True,
    )
    imported_pack = storage.PACKS_DIR / result["pack_id"]
    assert result["vectors_restored"] is False
    assert provider.calls == 1
    await semantic_index.build_index(imported_pack, root, result["pack_id"], embedding)
    assert provider.calls == 2
    imported_metadata = semantic_storage.load_metadata(imported_pack)
    assert semantic_index.index_is_ready(root, result["pack_id"], imported_metadata)
    matches = await semantic_index.search_index(
        root,
        result["pack_id"],
        "cheerful",
        embedding,
        metadata=imported_metadata,
        pack_dir=imported_pack,
    )
    assert len(matches) == 1
    assert matches[0]["caption"] == "A manually reviewed cheerful reaction"
    (item,) = imported_metadata["images"].values()
    assert item["manual_override"] is True
    assert item["visible_text"] == "Hello"
    if not overwrite:
        assert storage.get_pack_export_capabilities("audit-pack")[
            "vector_backup_available"
        ]


@pytest.mark.asyncio
async def test_image_routes_only_return_supported_images(audit_runtime):
    _, pack, _ = audit_runtime
    subject = web_api.WebAPIMixin()
    subject._resolve_runtime_pack_context = lambda: {"memes_dir": pack / "memes"}
    app = Quart(__name__)
    app.add_url_rule("/image", view_func=subject._api_serve_meme_image)
    app.add_url_rule("/data", view_func=subject._api_get_meme_image_data)
    app.add_url_rule("/semantic", view_func=subject._api_get_meme_image_semantic)
    client = app.test_client()
    (pack / "memes/happy/notice.html").write_text("<p>Notice</p>")
    for route in ("/image", "/data"):
        response = await client.get(f"{route}?category=happy&filename=notice.html")
        assert response.status_code == 400
    for route in ("/image", "/data", "/semantic"):
        response = await client.get(
            f"{route}?managed_pack_id=missing&category=happy&filename=image.png"
        )
        assert response.status_code == 404
    response = await client.get("/image?category=happy&filename=image.png")
    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "sandbox" in response.headers["Content-Security-Policy"]
    assert await response.get_data() == (pack / "memes/happy/image.png").read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize("image_format", ["PNG", "JPEG", "WEBP", "GIF"])
async def test_image_routes_preserve_original_formats_and_unicode_names(
    audit_runtime, image_format
):
    _, pack, _ = audit_runtime
    filename = f"表情.{image_format}"
    path = pack / "memes/happy" / filename
    Image.new("RGB", (8, 8), "blue").save(path, format=image_format)
    subject = web_api.WebAPIMixin()
    subject._resolve_runtime_pack_context = lambda: {"memes_dir": pack / "memes"}
    app = Quart(__name__)
    app.add_url_rule("/image", view_func=subject._api_serve_meme_image)
    app.add_url_rule("/data", view_func=subject._api_get_meme_image_data)
    for route in ("/image", "/data"):
        response = await app.test_client().get(
            route,
            query_string={
                "category": "happy",
                "filename": filename,
                "size": "original",
            },
        )
        assert response.status_code == 200
        if route == "/image":
            content = await response.get_data()
            assert response.mimetype == f"image/{image_format.lower()}"
        else:
            payload = await response.get_json()
            assert payload["mime_type"] == f"image/{image_format.lower()}"
            content = base64.b64decode(payload["data_url"].split(",", 1)[1])
        assert content == path.read_bytes()


def test_grayscale_alpha_images_convert_to_gif(tmp_path):
    path = tmp_path / "transparent.png"
    Image.new("LA", (8, 8), (128, 128)).save(path)
    subject = EventHandlerMixin()
    subject.convert_static_to_gif = True
    converted = Path(subject._convert_to_gif(str(path)))
    try:
        assert converted != path
        with Image.open(converted) as image:
            assert image.format == "GIF"
    finally:
        if converted != path:
            converted.unlink(missing_ok=True)
