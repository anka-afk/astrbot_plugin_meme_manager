import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urljoin, urlsplit

import pytest

from astrbot.dashboard.plugin_page_auth import PluginPageAuth
from astrbot.dashboard.services.plugin_page_service import PluginPageService

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "pages/app"


@pytest.mark.asyncio
async def test_shared_assets_resolve_through_astrbot_page_service(monkeypatch):
    service = PluginPageService(SimpleNamespace())
    monkeypatch.setattr(service, "get_plugin_root_dir", lambda plugin: ROOT)
    plugin = SimpleNamespace()
    assert [page.name for page in await service.discover_plugin_pages(plugin)] == [
        "app"
    ]
    payload = {
        "token_type": "plugin_page_asset",
        "plugin_name": "meme_manager",
        "page_name": "app",
    }
    shared_urls = {}
    if hasattr(service, "build_plugin_page_view_content_path"):
        base = service.build_plugin_page_view_content_path(
            "meme_manager", "app", "test-token"
        )
    else:
        base = service.build_plugin_page_content_path("meme_manager", "app")
    for source in APP.rglob("*"):
        if source.suffix not in {".html", ".css", ".js"}:
            continue
        relative = source.relative_to(APP).as_posix()
        content = source.read_text("utf-8")
        if source.suffix == ".html":
            if hasattr(service, "process_plugin_page_html"):
                content = service.process_plugin_page_html(
                    content,
                    theme=None,
                    extra_query_params={"asset_token": "test-token"},
                )
            else:
                content = service.rewrite_plugin_page_html(
                    content,
                    "meme_manager",
                    "app",
                    relative,
                    theme=None,
                    extra_query_params={"asset_token": "test-token"},
                )
            urls = re.findall(r"(?:src|href)=[\"']([^\"']+)", content)
        elif source.suffix == ".css":
            urls = re.findall(r"url\([\"']?([^\)\"']+)", content)
        else:
            urls = re.findall(r"(?:\bfrom\s+|\bimport\s*\()[\"']([^\"'\r\n]+)", content)
        for url in urls:
            parts = urlsplit(urljoin(base + relative, url))
            if parts.path == "/api/plugin/page/bridge-sdk.js":
                assert parse_qs(parts.query)["asset_token"] == ["test-token"]
                assert PluginPageAuth.is_scope_valid(payload, parts.path)
                continue
            if not parts.path.startswith(base):
                continue
            asset = parts.path.removeprefix(base)
            resolved = await service.resolve_plugin_page_file(plugin, "app", asset)
            assert resolved.is_relative_to(APP)
            if asset.startswith("shared/"):
                shared_urls.setdefault(asset, set()).add(parts.path)
    assert len(shared_urls) == 9
    assert all(len(urls) == 1 for urls in shared_urls.values())
    with pytest.raises(ValueError):
        await service.resolve_plugin_page_file(plugin, "app", "../_conf_schema.json")


def test_fonts_and_shared_scripts_have_no_delivery_copies():
    for name in ("ui.js", "ui.css", "fa.min.css"):
        assert list((ROOT / "pages").rglob(name)) == [APP / "shared" / name]
    fonts = list((ROOT / "pages").rglob("*.woff2"))
    assert len(fonts) == 4
    assert all(path.parent == APP / "shared/webfonts" for path in fonts)
