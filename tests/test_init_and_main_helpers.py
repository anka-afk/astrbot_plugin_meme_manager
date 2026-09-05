import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_meme_manager import main as plugin_main
from astrbot_plugin_meme_manager.main import MemeSender


def make_sender(config=None):
    sender = object.__new__(MemeSender)
    sender.config = config or {}
    return sender


def test_main_config_reading_uses_only_current_paths():
    sender = make_sender(
        {
            "storage": {"provider": "webdav", "providers": {"r2": {"key": "new"}}},
            "image_host": "stardots",
            "image_host_config": {"r2": {"key": "old", "secret": "legacy"}},
            "semantic_enabled": True,
        }
    )

    assert sender._read_path(("storage", "provider")) == "webdav"
    assert sender._read_path(("missing",), "fallback") == "fallback"
    assert sender._read_config_value(("semantic", "enabled"), default=False) is False
    assert sender._get_provider_config("r2") == {"key": "new"}
    assert sender._get_provider_config("webdav") == {}
    assert sender._get_image_host_type() == "webdav"


def test_main_image_host_type_ignores_old_selection():
    assert make_sender({"storage": {"provider": "WebDAV"}})._get_image_host_type() == (
        "webdav"
    )
    assert (
        make_sender({"image_host": "cloudflare_r2"})._get_image_host_type()
        == "stardots"
    )
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


def test_main_webdav_does_not_read_old_aliases_or_top_level_config():
    sender = make_sender(
        {
            "webdav": {"url": "https://old.example", "password": "old-password"},
            "webdav_username": "old-user",
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
            },
        }
    )

    result = sender._get_provider_config("webdav")
    assert (
        not {"url", "username", "password", "base_path", "verify_ssl"} & result.keys()
    )


def test_main_prompt_build_wrap_and_strip_round_trip():
    sender = make_sender()
    sender.category_mapping_string = "happy - 开心"
    sender.prompt_head = "请选择："
    sender.prompt_tail = "，按上下文选择。"
    sender.sys_prompt_add = ""

    prompt = sender._build_meme_prompt()
    assert prompt == "请选择：happy - 开心，按上下文选择。"
    wrapped = sender._wrap_meme_prompt(prompt)
    assert sender._strip_meme_prompt("基础提示" + wrapped) == "基础提示"
    semantic = "基础" + sender._semantic_system_prompt()
    assert sender._strip_meme_prompt(semantic) == "基础"


@pytest.mark.parametrize(
    ("category_enabled", "reply_enabled", "category_text", "reply_text", "expected"),
    [
        (False, False, "分类示例", "形式示例", ""),
        (True, False, "分类示例", "形式示例", "\n\n分类示例"),
        (False, True, "分类示例", "形式示例", "\n\n形式示例"),
        (True, True, "分类示例", "形式示例", "\n\n分类示例\n\n形式示例"),
        (True, True, "  ", "形式示例", "\n\n形式示例"),
        (True, True, "分类示例", "  ", "\n\n分类示例"),
    ],
)
def test_prompt_examples_are_independent_and_removed_with_prompt(
    category_enabled, reply_enabled, category_text, reply_text, expected
):
    sender = make_sender()
    sender.prompt_head = "前缀\n"
    sender.prompt_tail = "\n后缀"
    sender.prompt_examples = [
        (category_enabled, category_text),
        (reply_enabled, reply_text),
    ]
    sender.sys_prompt_add = ""
    prompt = sender._build_meme_prompt("真实分类")
    assert prompt == "前缀\n真实分类\n后缀" + expected
    assert (
        sender._strip_meme_prompt("人设" + sender._wrap_meme_prompt(prompt)) == "人设"
    )


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
    sender.prompt_tail = "个。"
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("conversation_persona", "default_persona", "forced_persona", "expected_pack"),
    [
        (None, "default", None, "seio-stickers"),
        ("other", "default", None, "official-001"),
        ("other", "default", "default", "seio-stickers"),
        ("default", "default", "other", "official-001"),
        ("[%None]", "default", None, "official-001"),
        (None, "other", None, "official-001"),
        (None, "missing", None, "seio-stickers"),
    ],
)
async def test_webchat_pack_rules_use_effective_persona(
    tmp_path,
    monkeypatch,
    conversation_persona,
    default_persona,
    forced_persona,
    expected_pack,
):
    from astrbot_plugin_meme_manager.backend.packs import resolver as pack_resolver

    from astrbot.core import persona_mgr

    manager = object.__new__(persona_mgr.PersonaManager)
    manager.personas_v3 = [{"name": "default"}, {"name": "other"}]
    monkeypatch.setattr(
        persona_mgr.sp,
        "get_async",
        AsyncMock(return_value={"persona_id": forced_persona}),
    )
    monkeypatch.setattr(pack_resolver, "PACKS_DIR", tmp_path / "packs")
    monkeypatch.setattr(pack_resolver, "REGISTRY_PATH", tmp_path / "registry.json")
    monkeypatch.setattr(pack_resolver, "SELECTION_RULES_PATH", tmp_path / "rules.json")
    for pack_id in ("seio-stickers", "official-001"):
        (tmp_path / "packs" / pack_id / "memes").mkdir(parents=True)
    (tmp_path / "registry.json").write_text(
        json.dumps(
            {
                "installed_packs": [
                    {"id": "seio-stickers", "enabled": True},
                    {"id": "official-001", "enabled": True},
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "rules.json").write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "scope": "persona",
                        "target": "default",
                        "pack_id": "seio-stickers",
                    },
                    {"scope": "default", "pack_id": "official-001"},
                ]
            }
        ),
        encoding="utf-8",
    )
    sender = make_sender()
    sender.emotion_llm_enabled = False
    sender._scope_allows_llm_origin = lambda event: True
    sender.context = SimpleNamespace(
        persona_manager=manager,
        get_config=lambda umo: {
            "provider_settings": {"default_personality": default_persona}
        },
    )
    extras = {}
    event = SimpleNamespace(
        unified_msg_origin="webchat:FriendMessage:test",
        session_id="test",
        get_platform_name=lambda: "webchat",
        get_extra=extras.get,
        set_extra=lambda key, value: extras.__setitem__(key, value),
    )
    request = SimpleNamespace(
        conversation=SimpleNamespace(persona_id=conversation_persona), session_id="test"
    )
    selected = []
    sender._apply_request_prompt = lambda req, event: selected.append(
        sender._resolve_runtime_pack_context(event=event, req=req)["pack_id"]
    )
    await sender._inject_meme_prompt_impl(event, request)
    assert selected == [expected_pack]
    assert sender._get_runtime_memes_dir_for_event(event) == str(
        tmp_path / "packs" / expected_pack / "memes"
    )


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
