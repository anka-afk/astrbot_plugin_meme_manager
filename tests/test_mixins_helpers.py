import base64
import json
from types import SimpleNamespace

import pytest
from astrbot_plugin_meme_manager.mixins import web_api as web_api_module
from astrbot_plugin_meme_manager.mixins.commands import CommandMixin
from astrbot_plugin_meme_manager.mixins.event_handlers import (
    LLM_REQUEST_ORIGIN_CHAT,
    LLM_REQUEST_ORIGIN_EXTRA_KEY,
    LLM_REQUEST_ORIGIN_PLUGIN,
    TRIGGER_SCOPE_CHAT_AND_PLUGIN,
    TRIGGER_SCOPE_CHAT_ONLY,
    EventHandlerMixin,
    normalize_trigger_scope,
)
from astrbot_plugin_meme_manager.mixins.web_api import WebAPIMixin

from astrbot.api.message_components import Plain


class FakeEvent:
    def __init__(self, message=""):
        self.message = message
        self.extra = {}

    def get_message_str(self):
        return self.message

    def get_extra(self, key):
        return self.extra.get(key)

    def set_extra(self, key, value):
        self.extra[key] = value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, TRIGGER_SCOPE_CHAT_ONLY),
        ("only_chat_llm", TRIGGER_SCOPE_CHAT_ONLY),
        ("all_llm", TRIGGER_SCOPE_CHAT_AND_PLUGIN),
        ("all_messages", TRIGGER_SCOPE_CHAT_AND_PLUGIN),
        ("chat_and_plugin_llm", TRIGGER_SCOPE_CHAT_AND_PLUGIN),
        ("invalid", TRIGGER_SCOPE_CHAT_ONLY),
    ],
)
def test_normalize_trigger_scope(value, expected):
    assert normalize_trigger_scope(value) == expected


@pytest.mark.asyncio
async def test_mark_and_filter_llm_request_origin():
    mixin = object.__new__(EventHandlerMixin)
    event = FakeEvent()
    await mixin._mark_llm_request_origin_impl(event)
    assert event.extra[LLM_REQUEST_ORIGIN_EXTRA_KEY] == LLM_REQUEST_ORIGIN_CHAT
    mixin.trigger_scope = TRIGGER_SCOPE_CHAT_ONLY
    assert mixin._scope_allows_llm_origin(event)

    event.extra["provider_request"] = object()
    await mixin._mark_llm_request_origin_impl(event)
    assert event.extra[LLM_REQUEST_ORIGIN_EXTRA_KEY] == LLM_REQUEST_ORIGIN_PLUGIN
    assert not mixin._scope_allows_llm_origin(event)
    mixin.trigger_scope = TRIGGER_SCOPE_CHAT_AND_PLUGIN
    assert mixin._scope_allows_llm_origin(event)


def test_should_attach_for_llm_and_streaming_results():
    mixin = object.__new__(EventHandlerMixin)
    mixin.trigger_scope = TRIGGER_SCOPE_CHAT_ONLY
    event = FakeEvent()
    event.extra[LLM_REQUEST_ORIGIN_EXTRA_KEY] = LLM_REQUEST_ORIGIN_CHAT
    assert not mixin._should_attach_for_result(event, None)
    assert mixin._should_attach_for_result(
        event, SimpleNamespace(result_content_type="STREAMING_FINISH")
    )
    assert mixin._should_attach_for_result(
        event,
        SimpleNamespace(result_content_type="OTHER", is_llm_result=lambda: True),
    )
    assert not mixin._should_attach_for_result(
        event,
        SimpleNamespace(result_content_type="OTHER", is_llm_result=lambda: False),
    )


def test_emotion_context_text_role_and_content_helpers():
    mixin = EventHandlerMixin
    assert mixin._stringify_emotion_context_text(None) == ""
    assert mixin._stringify_emotion_context_text(["hello", {"text": "world"}]) == (
        "hello world"
    )
    assert (
        mixin._stringify_emotion_context_text({"unknown": "值"}) == '{"unknown": "值"}'
    )
    assert mixin._extract_emotion_context_role({"sender": " USER "}) == "user"
    assert mixin._extract_emotion_context_role(SimpleNamespace(role="Assistant")) == (
        "assistant"
    )
    assert mixin._extract_emotion_context_content({"message": {"text": "消息"}}) == (
        "消息"
    )
    assert mixin._extract_emotion_context_content(SimpleNamespace(content="回复")) == (
        "回复"
    )


def test_collect_emotion_context_filters_roles_and_limits_turns():
    mixin = object.__new__(EventHandlerMixin)
    mixin.emotion_llm_context_turns = 1
    request = SimpleNamespace(
        contexts=json.dumps(
            [
                {"role": "system", "content": "系统"},
                {"role": "user", "content": "第一条"},
                {"role": "assistant", "content": "第二条"},
                {"role": "user", "content": "第三条"},
            ],
            ensure_ascii=False,
        )
    )
    assert mixin._collect_emotion_context_lines_from_request(request) == [
        "助手: 第二条",
        "用户: 第三条",
    ]
    mixin.emotion_llm_context_turns = 0
    assert mixin._collect_emotion_context_lines_from_request(request) == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"emotions":["happy","HAPPY","bad",3]}', ["happy"]),
        ('prefix ["sad", "happy"] suffix', ["sad", "happy"]),
        ('{"tag":"&&happy&&"}', ["happy"]),
        ("not-json", []),
        ("", []),
    ],
)
def test_parse_emotion_llm_selection(raw, expected):
    assert (
        EventHandlerMixin._parse_emotion_llm_selection(raw, {"happy", "sad"})
        == expected
    )


def test_filter_emotion_selection_deduplicates_and_applies_optional_limit():
    mixin = object.__new__(EventHandlerMixin)
    mixin.max_emotions_per_message = 2
    mixin.strict_max_emotions_per_message = True
    assert mixin._filter_emotion_selection(["happy", "happy", "sad", "angry"]) == [
        "happy",
        "sad",
    ]
    mixin.strict_max_emotions_per_message = False
    assert mixin._filter_emotion_selection(["happy", "sad", "angry"]) == [
        "happy",
        "sad",
        "angry",
    ]


def test_marked_emotion_extraction_and_text_cleanup():
    mixin = object.__new__(EventHandlerMixin)
    mixin.remove_invalid_alternative_markup = False
    mixin._read_config_value = lambda *args, **kwargs: True
    cleaned, emotions = mixin._extract_marked_emotions_from_text(
        "你好 &&happy&& [sad] (happy) [unknown]", {"happy", "sad"}
    )
    assert emotions == ["happy", "sad", "happy"]
    assert "&&" not in cleaned
    assert "[unknown]" in cleaned


def test_emotion_markup_context_heuristics():
    mixin = object.__new__(EventHandlerMixin)
    mixin._read_config_value = lambda *args, **kwargs: ["special"]
    text = "before <thinking>happy</thinking> after"
    assert mixin._is_position_in_thinking_tags(text, text.index("happy"))
    assert not mixin._is_position_in_thinking_tags(text, 0)
    assert not mixin._is_likely_emotion_markup("[1]", "value [1] value", 6)
    assert not mixin._is_likely_emotion_markup("(two words)", "(two words)", 0)
    assert mixin._is_likely_emotion_markup("(happy)", "你好(happy)", 2)
    assert mixin._is_likely_emotion("happy", "你好happy", 2, {"happy"})
    assert mixin._is_likely_emotion("special", "xspecialx", 1, {"special"})


def test_merge_components_distributes_images_after_plain_components():
    mixin = object.__new__(EventHandlerMixin)
    first = Plain("first")
    second = Plain("second")
    image_one = object()
    image_two = object()
    assert mixin._merge_components_with_images([first], []) == [first]
    assert mixin._merge_components_with_images([], [image_one]) == [image_one]
    assert mixin._merge_components_with_images([object()], [image_one])[-1] is image_one
    merged = mixin._merge_components_with_images(
        [first, second], [image_one, image_two]
    )
    assert merged == [first, image_one, second, image_two]


def test_command_description_and_category_count_helpers():
    mixin = object.__new__(CommandMixin)
    event = FakeEvent("表情管理   添加分类 happy   表示开心")
    assert mixin._extract_category_description_from_command(event, "happy") == (
        "表示开心"
    )
    assert (
        mixin._extract_category_description_from_command(
            FakeEvent("其他命令 happy 描述"), "happy"
        )
        == ""
    )
    assert (
        mixin._extract_category_description_from_command(
            FakeEvent("表情管理 添加分类 happy"), "happy"
        )
        == ""
    )
    assert mixin._format_category_counts({"empty": 0}) == "无可删除的表情包文件。"
    summary = mixin._format_category_counts(
        {f"category-{index}": 1 for index in range(10)}, limit=2
    )
    assert summary.count("个") == 3
    assert "其余 8 个类型已省略" in summary


def test_web_api_response_status_and_safe_image_name():
    assert WebAPIMixin._get_webui_response_status(({}, 201)) == 201
    assert (
        WebAPIMixin._get_webui_response_status(SimpleNamespace(status_code=204)) == 204
    )
    assert WebAPIMixin._get_webui_response_status(object()) == "unknown"
    assert WebAPIMixin._safe_semantic_image_name("meme.png")
    for value in ("", ".", "..", "../meme.png", "nested/meme.png", "a\\b.png"):
        assert not WebAPIMixin._safe_semantic_image_name(value)


def test_web_api_scan_and_description_loading(tmp_path):
    pack_dir = tmp_path / "pack"
    memes_dir = pack_dir / "memes"
    (memes_dir / "happy").mkdir(parents=True)
    (memes_dir / "happy" / "meme.png").write_bytes(b"image")
    (memes_dir / "happy" / "ignore.txt").write_bytes(b"ignore")
    assert WebAPIMixin._scan_pack_emojis(memes_dir) == {"happy": ["meme.png"]}
    assert WebAPIMixin._scan_pack_emojis(tmp_path / "missing") == {}

    metadata_path = pack_dir / "memes_data.json"
    manifest_path = pack_dir / "manifest.json"
    metadata_path.write_text(
        json.dumps({"happy": "元数据"}, ensure_ascii=False), encoding="utf-8"
    )
    manifest_path.write_text(
        json.dumps(
            {
                "categories": {
                    "happy": {"description": "清单"},
                    "sad": {"description": "伤心"},
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    descriptions = WebAPIMixin._load_pack_descriptions(
        {"memes_data_path": metadata_path, "manifest_path": manifest_path}
    )
    assert descriptions == {"happy": "元数据", "sad": "伤心"}


def test_web_api_build_file_data_url(tmp_path):
    path = tmp_path / "image.png"
    path.write_bytes(b"image")
    result = WebAPIMixin._build_file_data_url(path, "image/png")
    assert result == "data:image/png;base64," + base64.b64encode(b"image").decode(
        "ascii"
    )


class CommunityInstallJobHarness(WebAPIMixin):
    def __init__(self):
        self._community_install_jobs = {}
        self.reloaded = False

    async def _run_guarded_runtime_file_operation(
        self, operation, function, *args, **kwargs
    ):
        del operation
        return function(*args, **kwargs)

    def _get_github_accelerator_url(self):
        return "https://proxy/"

    def _reload_personas(self):
        self.reloaded = True


@pytest.mark.asyncio
async def test_community_install_job_reports_unknown_total_without_fake_percent(
    monkeypatch,
):
    api = CommunityInstallJobHarness()
    api._community_install_jobs["job"] = {
        "status": "running",
        "progress": 0,
        "cancel_requested": False,
    }
    snapshots = []

    def install(**kwargs):
        kwargs["progress_callback"]("downloading", 1024, None)
        snapshots.append(dict(api._community_install_jobs["job"]))
        return {"pack_id": "pack"}

    monkeypatch.setattr(web_api_module, "install_pack_from_github_source", install)
    await api._run_community_pack_install_job("job", {}, False, False)

    assert snapshots[0]["progress"] is None
    assert snapshots[0]["downloaded_bytes"] == 1024
    assert api._community_install_jobs["job"]["status"] == "succeeded"
    assert api.reloaded


@pytest.mark.asyncio
async def test_community_install_job_honors_cancel_request(monkeypatch):
    api = CommunityInstallJobHarness()
    api._community_install_jobs["job"] = {
        "status": "cancelling",
        "progress": 0,
        "cancel_requested": True,
    }

    def install(**kwargs):
        kwargs["progress_callback"]("connecting", 0, None)
        raise AssertionError("cancelled progress callback must stop installation")

    monkeypatch.setattr(web_api_module, "install_pack_from_github_source", install)
    await api._run_community_pack_install_job("job", {}, False, False)

    job = api._community_install_jobs["job"]
    assert job["status"] == "cancelled"
    assert job["message"] == "安装已取消"
    assert not api.reloaded
