import json
from types import SimpleNamespace

import pytest

from astrbot_plugin_meme_manager import init as plugin_init
from astrbot_plugin_meme_manager import main as plugin_main
from astrbot_plugin_meme_manager.main import MemeSender


@pytest.fixture
def init_paths(tmp_path, monkeypatch):
    base = tmp_path / "data"
    memes = base / "memes"
    metadata = base / "memes_data.json"
    manifest = base / "manifest.json"
    monkeypatch.setattr(plugin_init, "BASE_DATA_DIR", str(base))
    monkeypatch.setattr(plugin_init, "MEMES_DIR", str(memes))
    monkeypatch.setattr(plugin_init, "MEMES_DATA_PATH", str(metadata))
    monkeypatch.setattr(plugin_init, "ACTIVE_PACK_MANIFEST_PATH", manifest)
    sync_calls = []
    monkeypatch.setattr(
        plugin_init,
        "sync_active_pack_metadata",
        lambda: sync_calls.append(True),
    )
    return base, memes, metadata, manifest, sync_calls


def test_init_plugin_builds_descriptions_from_manifest_and_directories(init_paths):
    _, memes, metadata, manifest, sync_calls = init_paths
    (memes / "happy").mkdir(parents=True)
    (memes / "local").mkdir()
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "categories": {
                    "happy": {"description": "happy description"},
                    "missing": {"description": "missing description"},
                }
            }
        ),
        encoding="utf-8",
    )

    assert plugin_init.init_plugin()
    assert json.loads(metadata.read_text(encoding="utf-8")) == {
        "happy": "happy description",
        "local": "请添加描述",
    }
    assert sync_calls == [True]


def test_init_plugin_normalizes_existing_descriptions(init_paths):
    _, memes, metadata, _, sync_calls = init_paths
    (memes / "happy").mkdir(parents=True)
    (memes / "new").mkdir()
    metadata.write_text(
        json.dumps({"happy": "existing", "stale": "orphan"}),
        encoding="utf-8",
    )

    assert plugin_init.init_plugin()
    assert json.loads(metadata.read_text(encoding="utf-8")) == {
        "happy": "existing",
        "new": "请添加描述",
    }
    assert sync_calls == [True]


def test_init_plugin_reports_outer_failure(init_paths, monkeypatch):
    monkeypatch.setattr(
        plugin_init,
        "ensure_dir_exists",
        lambda path: (_ for _ in ()).throw(OSError("disk")),
    )

    assert not plugin_init.init_plugin()


def make_sender(config=None):
    sender = object.__new__(MemeSender)
    sender.config = config or {}
    return sender


def test_main_config_reading_prefers_modern_then_legacy_values():
    sender = make_sender(
        {
            "storage": {"provider": "webdav", "providers": {"r2": {"key": "new"}}},
            "image_host": "stardots",
            "image_host_config": {"r2": {"key": "old", "secret": "legacy"}},
            "legacy_value": 3,
        }
    )

    assert sender._read_path(("storage", "provider")) == "webdav"
    assert sender._read_path(("missing",), "fallback") == "fallback"
    assert sender._read_config_value(
        ("storage", "missing"), default=1, legacy_keys=("legacy_value",)
    ) == 3
    assert sender._get_provider_config("r2") == {"key": "new", "secret": "legacy"}
    assert sender._get_nested_config("storage", "providers", "r2") == {"key": "new"}
    assert sender._get_nested_config("storage", "missing") == {}
    assert sender._has_required_config({"a": 1, "b": "x"}, ["a", "b"])
    assert not sender._has_required_config({"a": 1, "b": ""}, ["a", "b"])


def test_main_image_host_type_supports_string_and_object_config():
    assert make_sender({"storage": {"provider": "WebDAV"}})._get_image_host_type() == (
        "webdav"
    )
    assert make_sender(
        {"storage": {"provider": {"name": "CloudFlare_R2"}}}
    )._get_image_host_type() == "cloudflare_r2"
    assert make_sender({})._get_image_host_type() == "stardots"


def test_main_image_sync_init_failure_degrades_gracefully(tmp_path, monkeypatch):
    sender = make_sender()
    sender.img_sync_config = {"bucket": "configured"}
    sender.img_sync_provider_type = "cloudflare_r2"
    sender.img_sync = None
    memes_dir = tmp_path / "memes"
    memes_dir.mkdir()
    sender._resolve_sync_pack_target = lambda preferred_pack_id=None: (
        "pack-a",
        memes_dir,
    )

    def fail_image_sync(**kwargs):
        raise RuntimeError("remote probe failed")

    monkeypatch.setattr(plugin_main, "ImageSync", fail_image_sync)

    assert sender._ensure_img_sync_for_pack() is None
    assert sender.img_sync is None


def test_main_webdav_aliases_are_normalized():
    sender = make_sender(
        {
            "storage": {
                "providers": {
                    "webdav": {
                        "webdav_url": "https://dav.example",
                        "user": "name",
                        "token": "secret",
                        "remote_path": "memes",
                        "ssl_verify": False,
                    }
                }
            }
        }
    )

    result = sender._get_webdav_config()
    assert result["url"] == "https://dav.example"
    assert result["username"] == "name"
    assert result["password"] == "secret"
    assert result["base_path"] == "memes"
    assert result["verify_ssl"] is False


def test_main_prompt_build_wrap_and_strip_round_trip():
    sender = make_sender()
    sender.category_mapping_string = "happy - 开心"
    sender.prompt_head = "请选择："
    sender.prompt_tail_1 = "，最多"
    sender.max_emotions_per_message = 2
    sender.prompt_tail_2 = "个。"
    sender.sys_prompt_add = ""

    prompt = sender._build_meme_prompt()
    assert prompt == "请选择：happy - 开心，最多2个。"
    wrapped = sender._wrap_meme_prompt(prompt)
    assert sender._strip_meme_prompt("基础提示" + wrapped) == "基础提示"
    semantic = "基础" + sender._semantic_system_prompt()
    assert sender._strip_meme_prompt(semantic) == "基础"


def test_main_semantic_mode_requires_matching_verified_pack():
    sender = make_sender()

    class Event:
        def __init__(self, values):
            self.values = values

        def get_extra(self, key):
            return self.values.get(key)

    assert sender._semantic_mode_active(
        Event(
            {
                "meme_manager_semantic_active": True,
                "meme_manager_semantic_verified_pack_id": "pack-a",
                "meme_manager_runtime_pack_id": "pack-a",
            }
        )
    )
    assert not sender._semantic_mode_active(
        Event(
            {
                "meme_manager_semantic_active": True,
                "meme_manager_semantic_verified_pack_id": "pack-a",
                "meme_manager_runtime_pack_id": "pack-b",
            }
        )
    )
    assert not sender._semantic_mode_active(None)


def test_main_persona_resolution_and_base_prompt_tracking():
    sender = make_sender()
    sender.prompt_head = "请选择："
    sender.prompt_tail_2 = "个。"
    sender.sys_prompt_add = ""
    sender.persona_base_prompts = {}
    request = SimpleNamespace(
        conversation=SimpleNamespace(persona_id="persona-from-request")
    )

    assert sender._resolve_persona_id(req=request) == "persona-from-request"
    event = SimpleNamespace(persona_id="event-persona")
    assert sender._resolve_persona_id(event=event) == "event-persona"
    personas = [
        {"name": "one", "prompt": "base prompt"},
        {"id": "two", "prompt": "second prompt"},
    ]
    sender._sync_persona_base_prompts(personas)
    assert sender.persona_base_prompts == {
        "one": "base prompt",
        "two": "second prompt",
    }
    sender._sync_persona_base_prompts([personas[0]])
    assert sender.persona_base_prompts == {"one": "base prompt"}


def test_main_manageable_categories_and_default_description_updates():
    sender = make_sender()

    class Manager:
        descriptions = {"happy": "existing"}
        local = {"happy", "sad"}

        def get_descriptions(self):
            return dict(self.descriptions)

        def get_local_categories(self):
            return set(self.local)

        def update_description(self, category, description):
            self.descriptions[category] = description
            return True

    sender.category_manager = Manager()
    reloads = []
    sender._reload_personas = lambda: reloads.append(True)

    assert sender._get_manageable_categories() == {"happy", "sad"}
    sender._ensure_default_category_descriptions(["happy", "sad", "unknown"])
    assert sender.category_manager.descriptions["sad"]
    assert reloads == [True]
