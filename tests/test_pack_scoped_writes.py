"""Regression coverage for pack-scoped WebUI write operations.

管理页切换到非默认表情包后，写接口必须落到被管理的那个包，
而不是固定写入默认包。
"""

import io
import json
import zipfile
from types import SimpleNamespace

import pytest
from astrbot_plugin_meme_manager.backend.packs import resolver, storage
from astrbot_plugin_meme_manager.backend.packs.categories import CategoryManager
from astrbot_plugin_meme_manager.mixins import web_api as web_api_module
from astrbot_plugin_meme_manager.mixins.web_api import WebAPIMixin
from PIL import Image as PillowImage
from quart import Quart


def _png_bytes(color=(200, 30, 30)) -> bytes:
    buffer = io.BytesIO()
    PillowImage.new("RGB", (2, 2), color=color).save(buffer, format="PNG")
    return buffer.getvalue()


def _pack_archive_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "id": "chunk-pack",
                    "name": "分片包",
                    "version": "1.0.0",
                    "description": "分片导入测试包",
                    "categories": {"happy": {"description": "开心"}},
                },
                ensure_ascii=False,
            ),
        )
        archive.writestr("memes/happy/one.png", _png_bytes())
    return buffer.getvalue()


def _multipart_body(content: bytes, filename: str = "pack.zip") -> tuple[bytes, str]:
    boundary = "----MemeManagerTestBoundary"
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/zip\r\n\r\n"
    ).encode()
    body = head + content + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


@pytest.fixture
def pack_runtime(tmp_path, monkeypatch):
    packs = tmp_path / "packs"
    packs.mkdir()
    registry = tmp_path / "registry.json"
    rules = tmp_path / "selection_rules.json"
    for module in (resolver, storage, web_api_module):
        monkeypatch.setattr(module, "PACKS_DIR", packs)
    monkeypatch.setattr(resolver, "REGISTRY_PATH", registry)
    monkeypatch.setattr(resolver, "SELECTION_RULES_PATH", rules)
    monkeypatch.setattr(storage, "REGISTRY_PATH", registry)
    monkeypatch.setattr(storage, "TEMP_DIR", tmp_path / "temp")
    monkeypatch.setattr(web_api_module, "TEMP_DIR", tmp_path / "temp")
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
    rules.write_text(
        json.dumps(
            {
                "rules": [
                    {"id": "default", "scope": "default", "pack_ids": ["pack-a"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    for pack_id, description in (("pack-a", "Happy"), ("pack-b", "Joyful")):
        directory = packs / pack_id
        (directory / "memes" / "happy").mkdir(parents=True)
        (directory / "memes_data.json").write_text(
            json.dumps({"happy": description}), encoding="utf-8"
        )
        PillowImage.new("RGB", (2, 2), color=(10, 20, 30)).save(
            directory / "memes" / "happy" / "old.png"
        )
    return SimpleNamespace(packs=packs, rules=rules)


@pytest.fixture
def pack_api(pack_runtime):
    api = object.__new__(WebAPIMixin)
    api.category_manager = CategoryManager()
    api.semantic_task_manager = None
    api._reload_personas = lambda: None
    api._resolve_runtime_pack_context = lambda **kwargs: resolver.resolve_pack_context()
    return api


@pytest.mark.asyncio
async def test_category_write_targets_requested_pack(pack_api, pack_runtime):
    app = Quart(__name__)
    async with app.test_request_context(
        "/",
        method="POST",
        json={
            "managed_pack_id": "pack-b",
            "category": "joy",
            "description": "开心",
        },
    ):
        response, status = await pack_api._api_restore_category()
    assert status == 200
    assert (await response.get_json())["message"] == "分类创建成功"
    assert (pack_runtime.packs / "pack-b" / "memes" / "joy").is_dir()
    assert not (pack_runtime.packs / "pack-a" / "memes" / "joy").exists()
    descriptions = json.loads(
        (pack_runtime.packs / "pack-b" / "memes_data.json").read_text(encoding="utf-8")
    )
    assert descriptions["joy"] == "开心"
    assert "joy" not in json.loads(
        (pack_runtime.packs / "pack-a" / "memes_data.json").read_text(encoding="utf-8")
    )


@pytest.mark.asyncio
async def test_emoji_delete_targets_requested_pack(pack_api, pack_runtime):
    app = Quart(__name__)
    async with app.test_request_context(
        "/",
        method="POST",
        json={
            "managed_pack_id": "pack-b",
            "category": "happy",
            "image_file": "old.png",
        },
    ):
        response, status = await pack_api._api_delete_emoji()
    assert status == 200
    assert (await response.get_json())["filename"] == "old.png"
    assert not (pack_runtime.packs / "pack-b" / "memes" / "happy" / "old.png").exists()
    assert (pack_runtime.packs / "pack-a" / "memes" / "happy" / "old.png").is_file()


@pytest.mark.asyncio
async def test_batch_move_targets_requested_pack(pack_api, pack_runtime):
    app = Quart(__name__)
    async with app.test_request_context(
        "/",
        method="POST",
        json={
            "managed_pack_id": "pack-b",
            "source_category": "happy",
            "target_category": "joy",
            "image_files": ["old.png"],
        },
    ):
        _, status = await pack_api._api_batch_move_emojis()
    assert status == 200
    assert (pack_runtime.packs / "pack-b" / "memes" / "joy" / "old.png").is_file()
    assert not (pack_runtime.packs / "pack-b" / "memes" / "happy" / "old.png").exists()
    assert (pack_runtime.packs / "pack-a" / "memes" / "happy" / "old.png").is_file()


@pytest.mark.asyncio
async def test_upload_targets_requested_pack(pack_api, pack_runtime):
    app = Quart(__name__)

    @app.post("/upload/<pack_id>/<category>")
    async def upload(pack_id, category):
        return await pack_api._api_add_emoji_to_pack(pack_id, category)

    boundary = "----MemeManagerTestBoundary"
    body = (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="new.png"\r\n'
            "Content-Type: image/png\r\n\r\n"
        ).encode()
        + _png_bytes()
        + f"\r\n--{boundary}--\r\n".encode()
    )

    client = app.test_client()
    response = await client.post(
        "/upload/pack-b/happy",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    )
    assert response.status_code == 201
    assert (await response.get_json())["pack_id"] == "pack-b"
    assert (pack_runtime.packs / "pack-b" / "memes" / "happy" / "new.png").is_file()
    assert not (pack_runtime.packs / "pack-a" / "memes" / "happy" / "new.png").exists()


@pytest.mark.asyncio
async def test_write_without_pack_id_uses_default_pack(pack_api, pack_runtime):
    app = Quart(__name__)
    async with app.test_request_context(
        "/", method="POST", json={"category": "joy", "description": "开心"}
    ):
        _, status = await pack_api._api_restore_category()
    assert status == 200
    assert (pack_runtime.packs / "pack-a" / "memes" / "joy").is_dir()
    assert not (pack_runtime.packs / "pack-b" / "memes" / "joy").exists()


@pytest.mark.asyncio
async def test_unknown_pack_is_rejected(pack_api):
    app = Quart(__name__)
    async with app.test_request_context(
        "/",
        method="POST",
        json={"managed_pack_id": "missing-pack", "category": "joy"},
    ):
        _, status = await pack_api._api_restore_category()
    assert status == 404


@pytest.mark.asyncio
async def test_escaping_pack_id_is_rejected(pack_api):
    app = Quart(__name__)
    async with app.test_request_context(
        "/",
        method="POST",
        json={"managed_pack_id": "../escape", "category": "joy"},
    ):
        _, status = await pack_api._api_restore_category()
    assert status == 400


@pytest.mark.asyncio
async def test_mutation_lock_uses_requested_pack(pack_api):
    locked = []

    async def run_locked_pack_mutation(pack_id, operation, mutation):
        locked.append((pack_id, operation))
        return mutation()

    pack_api.semantic_task_manager = SimpleNamespace(
        run_locked_pack_mutation=run_locked_pack_mutation
    )
    app = Quart(__name__)
    async with app.test_request_context(
        "/",
        method="POST",
        json={"managed_pack_id": "pack-b", "category": "joy"},
    ):
        _, status = await pack_api._api_restore_category()
    assert status == 200
    assert locked == [("pack-b", "创建表情分类")]


def test_pack_scoped_upload_route_is_registered(pack_api, monkeypatch):
    registered = []

    def fake_register(route, handler, methods, desc):
        registered.append((route, methods))

    monkeypatch.setattr(pack_api, "_register_webui_api", fake_register)
    pack_api._register_web_apis()
    assert ("emoji/add/<pack_id>/<category>", ["POST"]) in registered
    assert ("emoji/add/<category>", ["POST"]) in registered
    assert ("packs/import/session", ["POST"]) in registered
    assert (
        "packs/import/session/chunk/<session_id>/<index>/<total_chunks>",
        ["POST"],
    ) in registered


@pytest.mark.asyncio
async def test_custom_pack_id_is_used(pack_api, pack_runtime):
    app = Quart(__name__)
    async with app.test_request_context(
        "/",
        method="POST",
        json={"name": "自定义 ID 包", "pack_id": "my-daily-pack"},
    ):
        response, status = await pack_api._api_create_local_pack()
    assert status == 200
    assert (await response.get_json())["pack_id"] == "my-daily-pack"
    assert (pack_runtime.packs / "my-daily-pack" / "manifest.json").is_file()
    manifest = json.loads(
        (pack_runtime.packs / "my-daily-pack" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["id"] == "my-daily-pack"


@pytest.mark.asyncio
async def test_invalid_custom_pack_id_is_rejected(pack_api, pack_runtime):
    app = Quart(__name__)
    for pack_id in ("../escape", "a", "bad id"):
        async with app.test_request_context(
            "/", method="POST", json={"name": "非法 ID", "pack_id": pack_id}
        ):
            _, status = await pack_api._api_create_local_pack()
        assert status == 400
    assert not (pack_runtime.packs / "bad id").exists()


@pytest.mark.asyncio
async def test_duplicate_custom_pack_id_is_rejected(pack_api, pack_runtime):
    app = Quart(__name__)
    async with app.test_request_context(
        "/",
        method="POST",
        json={"name": "重复 ID", "pack_id": "pack-b"},
    ):
        _, status = await pack_api._api_create_local_pack()
    assert status == 400
    assert (pack_runtime.packs / "pack-b" / "memes_data.json").is_file()


@pytest.mark.asyncio
async def test_chunked_import_assembles_and_inspects_archive(pack_api):
    app = Quart(__name__)
    session_id = "a" * 32
    archive = _pack_archive_bytes()
    chunks = [archive[: len(archive) // 2], archive[len(archive) // 2 :]]

    async with app.test_request_context(
        "/",
        method="POST",
        json={
            "session_id": session_id,
            "filename": "chunk-pack.zip",
            "total_chunks": len(chunks),
        },
    ):
        response, status = await pack_api._api_start_pack_import_session()
    assert status == 200
    assert (await response.get_json())["session_id"] == session_id

    payload = {}
    for index, chunk in enumerate(chunks):
        body, ctype = _multipart_body(chunk)
        async with app.test_request_context(
            "/",
            method="POST",
            data=body,
            headers={"Content-Type": ctype, "Content-Length": str(len(body))},
        ):
            response, status = await pack_api._api_upload_pack_import_chunk(
                session_id, str(index), str(len(chunks))
            )
        assert status == 200
        payload = await response.get_json()

    assert payload["import_token"] == session_id
    assert payload["pack_id"] == "chunk-pack"


@pytest.mark.asyncio
async def test_chunked_import_rejects_unknown_session(pack_api):
    app = Quart(__name__)
    body, ctype = _multipart_body(b"payload")
    async with app.test_request_context(
        "/",
        method="POST",
        data=body,
        headers={"Content-Type": ctype, "Content-Length": str(len(body))},
    ):
        _, status = await pack_api._api_upload_pack_import_chunk("b" * 32, "0", "1")
    assert status == 400
