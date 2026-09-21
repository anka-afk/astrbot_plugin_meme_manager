from types import SimpleNamespace

import pytest
from astrbot_plugin_meme_manager.mixins import web_api
from PIL import Image
from quart import Quart


@pytest.fixture
def preview_api(tmp_path, monkeypatch):
    packs = tmp_path / "packs"
    for name, color in (("first", "red"), ("second", "blue")):
        folder = packs / name / "memes" / "happy"
        folder.mkdir(parents=True)
        Image.new("RGB", (8, 8), color).save(folder / "same.png")
    monkeypatch.setattr(web_api, "PACKS_DIR", packs)
    subject = web_api.WebAPIMixin()
    subject._read_config_value = lambda *args, **kwargs: 2
    subject._resolve_runtime_pack_context = lambda: {
        "pack_id": "first",
        "memes_dir": packs / "first" / "memes",
    }
    app = Quart(__name__)
    subject.context = SimpleNamespace(
        register_web_api=lambda route, handler, methods, desc: app.add_url_rule(
            route, view_func=handler, methods=methods
        )
    )
    subject._register_webui_api(
        "preview/manifest", subject._api_preview_manifest, ["GET"], ""
    )
    subject._register_webui_api(
        "meme_image_data", subject._api_get_meme_image_data, ["GET"], ""
    )
    return app.test_client(), subject, packs


@pytest.mark.asyncio
async def test_cached_preview_revalidates_without_encoding_and_changes_version(
    preview_api, monkeypatch
):
    client, subject, packs = preview_api
    manifest_response = await client.get("/meme_manager/preview/manifest")
    assert manifest_response.headers["Cache-Control"] == "no-store"
    manifest = await manifest_response.get_json()
    assert manifest["concurrency"] == 2
    revision = manifest["versions"]["happy/same.png"]
    url = f"/meme_manager/meme_image_data?managed_pack_id=first&category=happy&filename=same.png&v={revision}"
    first = await client.get(url)
    assert first.status_code == 200
    assert first.headers["Cache-Control"] == "private, max-age=86400"
    assert first.headers["Vary"] == "Authorization"

    def unexpected_encoding(*args):
        raise AssertionError("A cache hit must not encode image bytes")

    monkeypatch.setattr(subject, "_build_preview_data_url", unexpected_encoding)
    monkeypatch.setattr(subject, "_build_file_data_url", unexpected_encoding)
    cached = await client.get(url, headers={"If-None-Match": first.headers["ETag"]})
    assert cached.status_code == 304
    assert await cached.get_data() == b""
    Image.new("RGB", (16, 16), "green").save(packs / "first/memes/happy/same.png")
    updated = await (await client.get("/meme_manager/preview/manifest")).get_json()
    assert updated["versions"]["happy/same.png"] != revision
    stale = await client.get(url)
    assert stale.status_code == 409
    assert stale.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
async def test_pack_isolation_errors_and_legacy_revalidation(preview_api):
    client, _, _ = preview_api
    first = await (
        await client.get("/meme_manager/preview/manifest?managed_pack_id=first")
    ).get_json()
    second = await (
        await client.get("/meme_manager/preview/manifest?managed_pack_id=second")
    ).get_json()
    assert first["versions"] != second["versions"]
    legacy = await client.get(
        "/meme_manager/meme_image_data?category=happy&filename=same.png"
    )
    assert legacy.headers["Cache-Control"] == "private, no-cache"
    for query in (
        "category=happy&filename=missing.png",
        "managed_pack_id=missing",
        "size=bad",
        "category=..&filename=../outside",
    ):
        response = await client.get(f"/meme_manager/meme_image_data?{query}")
        assert response.status_code >= 400
        assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value,expected", [(1, 1), (4, 4), (8, 8), (0, 2), (99, 2), (True, 2), ("4", 2)]
)
async def test_manifest_concurrency_validation(preview_api, value, expected):
    client, subject, _ = preview_api
    subject._read_config_value = lambda *args, **kwargs: value
    manifest = await (await client.get("/meme_manager/preview/manifest")).get_json()
    assert manifest["concurrency"] == expected
