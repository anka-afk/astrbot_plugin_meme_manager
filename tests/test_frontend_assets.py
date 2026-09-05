import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

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
    for source in APP.rglob("*"):
        if source.suffix not in {".html", ".css", ".js"}:
            continue
        relative = source.relative_to(APP).as_posix()
        content = source.read_text("utf-8")
        kwargs = {"extra_query_params": {"asset_token": "test-token"}}
        if source.suffix == ".html":
            rewritten = service.rewrite_plugin_page_html(
                content, "meme_manager", "app", relative, theme=None, **kwargs
            )
            urls = re.findall(r'(?:src|href)=["\']([^"\']+)', rewritten)
        elif source.suffix == ".css":
            rewritten = service.rewrite_plugin_page_css(
                content, "meme_manager", "app", relative, **kwargs
            )
            urls = re.findall(r'url\(["\']?([^\)"\']+)', rewritten)
        else:
            rewritten = service.rewrite_plugin_page_js(
                content, "meme_manager", "app", relative, **kwargs
            )
            urls = re.findall(r'(?:from\s*|import\s*\()["\']([^"\']+)', rewritten)
        for url in urls:
            if not url.startswith("/api/plugin/page/"):
                continue
            parts = urlsplit(url)
            assert parse_qs(parts.query)["asset_token"] == ["test-token"]
            assert PluginPageAuth.is_scope_valid(payload, parts.path)
            if parts.path == "/api/plugin/page/bridge-sdk.js":
                continue
            prefix = "/api/plugin/page/content/meme_manager/app/"
            assert parts.path.startswith(prefix)
            asset = parts.path.removeprefix(prefix)
            resolved = await service.resolve_plugin_page_file(plugin, "app", asset)
            assert resolved.is_relative_to(APP)
            if asset.startswith("shared/"):
                shared_urls.setdefault(asset, set()).add(url)
    assert len(shared_urls) == 7
    assert all(len(urls) == 1 for urls in shared_urls.values())
    with pytest.raises(ValueError):
        await service.resolve_plugin_page_file(plugin, "app", "../_conf_schema.json")


def test_fonts_and_shared_scripts_have_no_delivery_copies():
    for name in ("ui.js", "ui.css", "fa.min.css"):
        assert list((ROOT / "pages").rglob(name)) == [APP / "shared" / name]
    fonts = list((ROOT / "pages").rglob("*.woff2"))
    assert len(fonts) == 4
    assert all(path.parent == APP / "shared/webfonts" for path in fonts)
