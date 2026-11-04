import asyncio
import base64
import io
import json
import threading
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_meme_manager.backend.packs import images as pack_images
from astrbot_plugin_meme_manager.backend.packs import resolver
from astrbot_plugin_meme_manager.backend.semantic.task import SemanticTaskManager
from astrbot_plugin_meme_manager.main import MemeSender
from astrbot_plugin_meme_manager.mixins import event_handlers
from PIL import Image as PillowImage

from astrbot.core.message.components import Image, Plain
from astrbot.core.message.message_event_result import MessageChain


class FakeEvent:
    """供真实命令与上传处理器使用的最小事件。"""

    def __init__(self, message, session_id="session-1", sender_id="user-1"):
        self.session_id = session_id
        self.sender_id = sender_id
        self.message_str = ""
        self.persona_id = ""
        self.unified_msg_origin = f"test:{session_id}"
        self.message_obj = SimpleNamespace(message=list(message))
        self._extra = {}

    def get_sender_id(self):
        return self.sender_id

    def get_message_str(self):
        return self.message_str

    def get_extra(self, key):
        return self._extra.get(key)

    def set_extra(self, key, value):
        self._extra[key] = value

    def plain_result(self, text):
        return MessageChain([Plain(text)])


def message_text(result):
    return "".join(
        component.text
        for component in getattr(result, "chain", [])
        if isinstance(component, Plain)
    )


def upload_key(event):
    return f"{event.session_id}_{event.get_sender_id()}"


@pytest.fixture(autouse=True)
def isolate_framework_temp_path(tmp_path, monkeypatch):
    from astrbot.core.utils import media_utils

    monkeypatch.setattr(media_utils, "get_astrbot_temp_path", lambda: str(tmp_path))


def write_rules(path, *, default, session=None):
    rules = [{"id": "default", "scope": "default", "pack_ids": list(default)}]
    if session is not None:
        rules.insert(
            0,
            {
                "id": "session",
                "scope": "session",
                "target": "session-1",
                "pack_ids": list(session),
            },
        )
    path.write_text(json.dumps({"rules": rules}), encoding="utf-8")


@pytest.fixture
def pack_runtime(tmp_path, monkeypatch):
    packs = tmp_path / "packs"
    packs.mkdir()
    registry = tmp_path / "registry.json"
    rules = tmp_path / "selection_rules.json"
    monkeypatch.setattr(resolver, "PACKS_DIR", packs)
    monkeypatch.setattr(resolver, "REGISTRY_PATH", registry)
    monkeypatch.setattr(resolver, "SELECTION_RULES_PATH", rules)
    registry.write_text(
        json.dumps(
            {
                "installed_packs": [
                    {"id": "pack-a", "enabled": True},
                    {"id": "pack-b", "enabled": True},
                    {"id": "pack-c", "enabled": True},
                ]
            }
        ),
        encoding="utf-8",
    )

    def create_pack(pack_id, categories):
        pack_dir = packs / pack_id
        (pack_dir / "memes").mkdir(parents=True)
        (pack_dir / "memes_data.json").write_text(
            json.dumps(categories, ensure_ascii=False), encoding="utf-8"
        )
        (pack_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "id": pack_id,
                    "categories": {
                        category: {"description": description}
                        for category, description in categories.items()
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        for category in categories:
            (pack_dir / "memes" / category).mkdir()

    create_pack("pack-a", {"happy": "A happy"})
    create_pack("pack-b", {"sad": "B sad"})
    create_pack("pack-c", {"c-only": "C only"})

    def set_rules(*, default, session=None):
        write_rules(rules, default=default, session=session)

    set_rules(default=["pack-a"], session=["pack-b", "pack-c"])
    return SimpleNamespace(
        packs=packs,
        registry=registry,
        rules=rules,
        set_rules=set_rules,
    )


@pytest.fixture
def sender(pack_runtime, tmp_path):
    subject = object.__new__(MemeSender)
    subject.config = {}
    subject.upload_states = {}
    subject.img_sync = None
    subject.semantic_task_manager = SemanticTaskManager(
        tmp_path / "plugin-data", context=SimpleNamespace()
    )
    subject.reload_emotions = AsyncMock()
    return subject


@pytest.fixture
def refresh_tracker(sender, monkeypatch):
    tracker = SimpleNamespace(invalidate=[], reload=[])
    manager = sender.semantic_task_manager

    def invalidate(pack_dir):
        assert "pack-b" in manager._external_pack_operations
        tracker.invalidate.append(Path(pack_dir))

    async def reload():
        assert "pack-b" in manager._external_pack_operations
        tracker.reload.append(True)

    monkeypatch.setattr(event_handlers, "invalidate_semantic_metadata", invalidate)
    sender.reload_emotions = AsyncMock(side_effect=reload)
    return tracker


async def prepare_upload(sender, event, category):
    results = [
        message_text(result) async for result in sender.upload_meme(event, category)
    ]
    return results[-1]


async def run_upload(sender, event):
    return [
        message_text(result) async for result in sender._handle_upload_image_impl(event)
    ]


def png_bytes(color=(255, 0, 0), size=(4, 4)):
    output = io.BytesIO()
    PillowImage.new("RGB", size, color).save(output, "PNG")
    return output.getvalue()


def write_png(path, color=(255, 0, 0)):
    content = png_bytes(color)
    path.write_bytes(content)
    return content


def bmp_bytes():
    output = io.BytesIO()
    PillowImage.new("RGB", (4, 4), (1, 2, 3)).save(output, "BMP")
    return output.getvalue()


def animated_gif_bytes():
    frames = [
        PillowImage.new("RGB", (4, 4), (255, 0, 0)),
        PillowImage.new("RGB", (4, 4), (0, 0, 255)),
    ]
    output = io.BytesIO()
    frames[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=40,
        loop=0,
    )
    return output.getvalue()


def image_from_filesystem(path):
    return Image.fromFileSystem(str(path))


def image_from_file_url(path):
    return Image(file=path.as_uri())


def image_from_qq_local_path(path):
    return Image(url=str(path), file=str(path))


def image_from_base64(path):
    return Image.fromBase64(base64.b64encode(path.read_bytes()).decode())


IMAGE_SOURCE_BUILDERS = [
    pytest.param(image_from_filesystem, id="filesystem"),
    pytest.param(image_from_file_url, id="file-url"),
    pytest.param(image_from_qq_local_path, id="qq-local-path"),
    pytest.param(image_from_base64, id="base64"),
]


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@contextmanager
def local_http_server(directory):
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(QuietHandler, directory=str(directory))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    ("image_format", "extension"),
    [
        pytest.param("PNG", ".png", id="png"),
        pytest.param("JPEG", ".jpg", id="jpeg"),
        pytest.param("GIF", ".gif", id="gif"),
        pytest.param("WEBP", ".webp", id="webp"),
    ],
)
def test_chat_save_preserves_supported_image_bytes(tmp_path, image_format, extension):
    memes_root = tmp_path / "memes"
    output = io.BytesIO()
    PillowImage.new("RGB", (5, 5), (10, 20, 30)).save(output, image_format)
    content = output.getvalue()

    result = pack_images.save_chat_upload_image(memes_root, "happy", content)

    saved = Path(result["path"])
    assert saved.suffix == extension
    assert saved.read_bytes() == content
    assert result["filename"] == saved.name
    assert not list(saved.parent.glob(".*.tmp"))


def test_chat_save_preserves_gif_animation(tmp_path):
    memes_root = tmp_path / "memes"
    content = animated_gif_bytes()

    result = pack_images.save_chat_upload_image(memes_root, "happy", content)

    saved = Path(result["path"])
    assert saved.read_bytes() == content
    with PillowImage.open(saved) as image:
        assert image.format == "GIF"
        assert image.n_frames == 2


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"not-an-image", id="corrupt"),
        pytest.param(bmp_bytes(), id="unsupported-bmp"),
    ],
)
def test_chat_save_rejects_invalid_content_without_creating_files(tmp_path, content):
    memes_root = tmp_path / "memes"
    category_path = memes_root / "happy"

    with pytest.raises(ValueError):
        pack_images.save_chat_upload_image(memes_root, "happy", content)

    assert not category_path.exists()
    assert not list(memes_root.rglob("*.bin"))


def test_chat_save_rejects_truncated_jpeg(tmp_path):
    output = io.BytesIO()
    PillowImage.new("RGB", (100, 100), (12, 34, 56)).save(output, "JPEG")
    truncated = output.getvalue()[:-30]
    memes_root = tmp_path / "memes"

    with pytest.raises(ValueError):
        pack_images.save_chat_upload_image(memes_root, "happy", truncated)

    assert not (memes_root / "happy").exists()


def test_chat_save_deduplicates_within_category_only(tmp_path):
    memes_root = tmp_path / "memes"
    content = png_bytes()

    first = pack_images.save_chat_upload_image(memes_root, "happy", content)
    with pytest.raises(pack_images.DuplicateEmojiError):
        pack_images.save_chat_upload_image(memes_root, "happy", content)
    second = pack_images.save_chat_upload_image(memes_root, "sad", content)

    assert Path(first["path"]).read_bytes() == content
    assert Path(second["path"]).read_bytes() == content
    assert len(list((memes_root / "happy").iterdir())) == 1
    assert len(list((memes_root / "sad").iterdir())) == 1


def test_chat_save_uuid_collision_never_overwrites_existing_file(tmp_path, monkeypatch):
    memes_root = tmp_path / "memes"
    category_path = memes_root / "happy"
    category_path.mkdir(parents=True)
    existing = category_path / "fixed.png"
    existing.write_bytes(b"existing-content")
    monkeypatch.setattr(pack_images.uuid, "uuid4", lambda: SimpleNamespace(hex="fixed"))

    result = pack_images.save_chat_upload_image(
        memes_root, "happy", png_bytes((0, 255, 0))
    )

    assert existing.read_bytes() == b"existing-content"
    assert result["filename"] == "fixed_1.png"
    assert (category_path / result["filename"]).read_bytes() == png_bytes((0, 255, 0))


def test_chat_save_atomic_failure_cleans_temporary_and_partial_files(
    tmp_path, monkeypatch
):
    memes_root = tmp_path / "memes"
    category_path = memes_root / "happy"
    category_path.mkdir(parents=True)

    def fail_replace(source, target):
        raise OSError("replace failed")

    monkeypatch.setattr(pack_images.os, "replace", fail_replace)

    with pytest.raises(OSError):
        pack_images.save_chat_upload_image(memes_root, "happy", png_bytes())

    assert list(category_path.iterdir()) == []


@pytest.mark.parametrize("builder", IMAGE_SOURCE_BUILDERS)
@pytest.mark.asyncio
async def test_chat_upload_uses_true_framework_converter_and_primary_pack(
    pack_runtime, sender, tmp_path, builder
):
    source = tmp_path / "source.png"
    content = write_png(source)
    image = builder(source)
    converted = Path(await image.convert_to_file_path())
    assert converted.read_bytes() == content

    event = FakeEvent([])
    prompt = await prepare_upload(sender, event, "sad")
    assert "pack-b" in prompt and "sad" in prompt
    event.message_obj.message = [image]

    messages = await run_upload(sender, event)

    assert len(messages) == 1
    assert "✅" in messages[0]
    assert "pack-b" in messages[0] and "sad" in messages[0]
    saved = list((pack_runtime.packs / "pack-b" / "memes" / "sad").iterdir())
    assert len(saved) == 1
    assert saved[0].read_bytes() == content
    assert list((pack_runtime.packs / "pack-a" / "memes" / "happy").iterdir()) == []
    assert source.is_file()
    assert source.read_bytes() == content


@pytest.mark.asyncio
async def test_chat_upload_downloads_real_local_http_image(
    pack_runtime, sender, tmp_path
):
    served = tmp_path / "served.png"
    content = write_png(served)
    event = FakeEvent([])
    await prepare_upload(sender, event, "sad")

    with local_http_server(tmp_path) as base_url:
        served_url = f"{base_url}/served.png"
        image = Image(file=served_url, url=served_url)
        converted = Path(await image.convert_to_file_path())
        assert converted.read_bytes() == content
        event.message_obj.message = [image]
        messages = await run_upload(sender, event)

    assert "✅" in messages[0]
    saved = list((pack_runtime.packs / "pack-b" / "memes" / "sad").iterdir())
    assert len(saved) == 1
    assert saved[0].read_bytes() == content


@pytest.mark.asyncio
async def test_upload_command_uses_primary_and_refuses_foreign_categories(
    pack_runtime, sender
):
    event = FakeEvent([])

    prompt = await prepare_upload(sender, event, "sad")
    state = sender.upload_states[upload_key(event)]
    assert state["pack_id"] == "pack-b"
    assert state["memes_dir"] == str(
        (pack_runtime.packs / "pack-b" / "memes").resolve()
    )
    assert "pack-b" in prompt

    sender.upload_states.clear()
    invalid = await prepare_upload(sender, event, "happy")
    assert "无效" in invalid
    assert "pack-b" in invalid
    assert "/表情管理 查看图库" in invalid
    assert upload_key(event) not in sender.upload_states

    secondary = await prepare_upload(sender, event, "c-only")
    assert "其他绑定的表情包" in secondary
    assert "pack-b" in secondary and "c-only" in secondary
    assert upload_key(event) not in sender.upload_states


@pytest.mark.asyncio
async def test_chat_upload_state_path_mismatch_writes_nothing(
    pack_runtime, sender, tmp_path
):
    event = FakeEvent([])
    await prepare_upload(sender, event, "sad")
    state = sender.upload_states[upload_key(event)]
    state["memes_dir"] = str(pack_runtime.packs / "pack-a" / "memes")

    image_path = tmp_path / "source.png"
    write_png(image_path)
    event.message_obj.message = [image_from_filesystem(image_path)]
    messages = await run_upload(sender, event)

    assert "记录路径已失效" in messages[0]
    assert list((pack_runtime.packs / "pack-b" / "memes" / "sad").iterdir()) == []
    assert list((pack_runtime.packs / "pack-a" / "memes" / "happy").iterdir()) == []
    assert upload_key(event) not in sender.upload_states


@pytest.mark.asyncio
async def test_chat_upload_rejects_memes_root_symlink_outside_pack(
    pack_runtime, sender, tmp_path
):
    pack_dir = pack_runtime.packs / "pack-b"
    memes_dir = pack_dir / "memes"
    outside = tmp_path / "outside-memes"
    memes_dir.rename(outside)
    try:
        memes_dir.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted")

    event = FakeEvent([])
    prompt = await prepare_upload(sender, event, "sad")

    assert "路径" in prompt and "不一致" in prompt
    assert upload_key(event) not in sender.upload_states

    source = tmp_path / "source.png"
    write_png(source)
    sender.upload_states[upload_key(event)] = {
        "category": "sad",
        "pack_id": "pack-b",
        "memes_dir": str(memes_dir.resolve()),
        "expire_time": float("inf"),
    }
    event.message_obj.message = [image_from_filesystem(source)]

    messages = await run_upload(sender, event)

    assert "记录路径已失效" in messages[0]
    assert list((outside / "sad").iterdir()) == []
    assert upload_key(event) not in sender.upload_states


@pytest.mark.asyncio
async def test_chat_upload_partial_failure_reports_counts_and_refreshes_only_saved(
    pack_runtime, sender, tmp_path, refresh_tracker
):
    good = tmp_path / "good.png"
    content = write_png(good)
    corrupt = tmp_path / "corrupt.png"
    corrupt.write_bytes(b"corrupt")
    missing = tmp_path / "missing.png"

    event = FakeEvent([])
    await prepare_upload(sender, event, "sad")
    event.message_obj.message = [
        image_from_filesystem(good),
        image_from_filesystem(corrupt),
        image_from_filesystem(missing),
    ]

    messages = await run_upload(sender, event)

    text = messages[0]
    assert "⚠️" in text and "新增 1 张" in text and "失败 2 张" in text
    assert str(tmp_path) not in text
    saved = list((pack_runtime.packs / "pack-b" / "memes" / "sad").iterdir())
    assert len(saved) == 1
    assert saved[0].read_bytes() == content
    assert refresh_tracker.invalidate == [(pack_runtime.packs / "pack-b").resolve()]
    assert len(refresh_tracker.reload) == 1
    assert upload_key(event) not in sender.upload_states


@pytest.mark.asyncio
async def test_chat_upload_all_failure_reports_error_without_refresh(
    pack_runtime, sender, tmp_path, monkeypatch
):
    invalidate = Mock()
    monkeypatch.setattr(event_handlers, "invalidate_semantic_metadata", invalidate)
    corrupt = tmp_path / "corrupt.png"
    corrupt.write_bytes(b"corrupt")
    event = FakeEvent([])
    await prepare_upload(sender, event, "sad")
    event.message_obj.message = [
        image_from_filesystem(corrupt),
        image_from_filesystem(tmp_path / "missing.png"),
    ]

    messages = await run_upload(sender, event)

    assert "❌" in messages[0]
    assert "失败 2 张" in messages[0]
    assert not list((pack_runtime.packs / "pack-b" / "memes" / "sad").glob("*"))
    invalidate.assert_not_called()
    sender.reload_emotions.assert_not_awaited()
    assert upload_key(event) not in sender.upload_states


@pytest.mark.asyncio
async def test_chat_upload_same_message_duplicate_reports_saved_and_duplicate(
    pack_runtime, sender, tmp_path
):
    source = tmp_path / "source.png"
    content = write_png(source)
    event = FakeEvent([])
    await prepare_upload(sender, event, "sad")
    event.message_obj.message = [
        image_from_filesystem(source),
        image_from_filesystem(source),
    ]

    messages = await run_upload(sender, event)

    assert "✅" in messages[0]
    assert "已收录 1 张" in messages[0]
    assert "另有 1 张重复" in messages[0]
    saved = list((pack_runtime.packs / "pack-b" / "memes" / "sad").iterdir())
    assert len(saved) == 1
    assert saved[0].read_bytes() == content


@pytest.mark.asyncio
async def test_chat_upload_prior_duplicate_is_informational_and_preserves_source(
    pack_runtime, sender, tmp_path, monkeypatch
):
    source = tmp_path / "source.png"
    content = write_png(source)
    pack_images.save_chat_upload_image(
        pack_runtime.packs / "pack-b" / "memes", "sad", content
    )
    invalidate = Mock()
    monkeypatch.setattr(event_handlers, "invalidate_semantic_metadata", invalidate)
    sender.reload_emotions.reset_mock()

    event = FakeEvent([])
    await prepare_upload(sender, event, "sad")
    event.message_obj.message = [
        image_from_filesystem(source),
        image_from_filesystem(source),
    ]

    messages = await run_upload(sender, event)

    text = messages[0]
    assert text.startswith("ℹ️")
    assert "未新增" in text and "2 张相同" in text
    assert "✅" not in text and "❌" not in text
    assert len(list((pack_runtime.packs / "pack-b" / "memes" / "sad").iterdir())) == 1
    invalidate.assert_not_called()
    sender.reload_emotions.assert_not_awaited()
    assert source.is_file() and source.read_bytes() == content


@pytest.mark.asyncio
async def test_chat_upload_routing_switch_before_processing_writes_nothing(
    pack_runtime, sender, tmp_path, monkeypatch
):
    invalidate = Mock()
    monkeypatch.setattr(event_handlers, "invalidate_semantic_metadata", invalidate)
    source = tmp_path / "source.png"
    write_png(source)
    event = FakeEvent([])
    await prepare_upload(sender, event, "sad")
    pack_runtime.set_rules(default=["pack-a"], session=["pack-a"])
    event.message_obj.message = [image_from_filesystem(source)]

    messages = await run_upload(sender, event)

    assert "主表情包在接收过程中发生了变化" in messages[0]
    assert list((pack_runtime.packs / "pack-a" / "memes" / "happy").iterdir()) == []
    assert list((pack_runtime.packs / "pack-b" / "memes" / "sad").iterdir()) == []
    invalidate.assert_not_called()
    sender.reload_emotions.assert_not_awaited()
    assert upload_key(event) not in sender.upload_states


@pytest.mark.asyncio
async def test_chat_upload_routing_change_during_mapping_read_writes_nothing(
    pack_runtime, sender, tmp_path, monkeypatch
):
    source = tmp_path / "source.png"
    write_png(source)
    event = FakeEvent([])
    await prepare_upload(sender, event, "sad")

    original_load = event_handlers.load_pack_category_mapping
    calls = {"count": 0}

    def switching_load(pack_id):
        calls["count"] += 1
        mapping = original_load(pack_id)
        if calls["count"] == 2:
            pack_runtime.set_rules(default=["pack-c"], session=["pack-c"])
        return mapping

    monkeypatch.setattr(event_handlers, "load_pack_category_mapping", switching_load)
    event.message_obj.message = [image_from_filesystem(source)]

    messages = await run_upload(sender, event)

    text = messages[0]
    assert "❌" in text
    assert "新增 0 张" in text
    assert "主表情包在接收过程中发生了变化" in text
    assert "剩余 1 张未处理" in text
    assert list((pack_runtime.packs / "pack-b" / "memes" / "sad").iterdir()) == []
    assert list((pack_runtime.packs / "pack-c" / "memes" / "c-only").iterdir()) == []
    assert upload_key(event) not in sender.upload_states


@pytest.mark.asyncio
async def test_chat_upload_creates_configured_empty_category(
    pack_runtime, sender, tmp_path
):
    pack_dir = pack_runtime.packs / "pack-b"
    metadata_path = pack_dir / "memes_data.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["empty"] = "B empty"
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    assert not (pack_dir / "memes" / "empty").exists()

    source = tmp_path / "source.png"
    content = write_png(source)
    event = FakeEvent([])
    prompt = await prepare_upload(sender, event, "empty")
    assert "pack-b" in prompt and "empty" in prompt
    event.message_obj.message = [image_from_filesystem(source)]

    messages = await run_upload(sender, event)

    assert "✅" in messages[0]
    saved = list((pack_dir / "memes" / "empty").iterdir())
    assert len(saved) == 1 and saved[0].read_bytes() == content


@pytest.mark.asyncio
async def test_chat_upload_routing_switch_during_convert_stops_remaining_writes(
    pack_runtime, sender, tmp_path, refresh_tracker, monkeypatch
):
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first_content = write_png(first, (255, 0, 0))
    write_png(second, (0, 255, 0))
    event = FakeEvent([])
    await prepare_upload(sender, event, "sad")
    event.message_obj.message = [
        image_from_filesystem(first),
        image_from_filesystem(second),
    ]
    original_convert = Image.convert_to_file_path
    calls = {"count": 0}

    async def switching_convert(image_self):
        calls["count"] += 1
        path = await original_convert(image_self)
        if calls["count"] == 2:
            pack_runtime.set_rules(default=["pack-a"], session=["pack-a"])
        return path

    monkeypatch.setattr(Image, "convert_to_file_path", switching_convert)

    messages = await run_upload(sender, event)

    text = messages[0]
    assert "⚠️" in text and "新增 1 张" in text
    assert "剩余 1 张未处理" in text
    saved = list((pack_runtime.packs / "pack-b" / "memes" / "sad").iterdir())
    assert len(saved) == 1
    assert saved[0].read_bytes() == first_content
    assert list((pack_runtime.packs / "pack-a" / "memes" / "happy").iterdir()) == []
    assert refresh_tracker.invalidate == [(pack_runtime.packs / "pack-b").resolve()]
    assert len(refresh_tracker.reload) == 1
    assert upload_key(event) not in sender.upload_states


@pytest.mark.asyncio
async def test_chat_upload_category_deleted_during_convert_is_not_recreated(
    pack_runtime, sender, tmp_path, monkeypatch
):
    source = tmp_path / "source.png"
    write_png(source)
    event = FakeEvent([])
    await prepare_upload(sender, event, "sad")
    event.message_obj.message = [image_from_filesystem(source)]
    pack_dir = pack_runtime.packs / "pack-b"
    category_dir = pack_dir / "memes" / "sad"
    original_convert = Image.convert_to_file_path
    converted = {"done": False}

    async def deleting_convert(image_self):
        path = await original_convert(image_self)
        if not converted["done"]:
            converted["done"] = True
            (pack_dir / "memes_data.json").write_text("{}", encoding="utf-8")
            (pack_dir / "manifest.json").write_text(
                json.dumps({"id": "pack-b", "categories": {}}), encoding="utf-8"
            )
            for child in category_dir.iterdir():
                child.unlink()
            category_dir.rmdir()
        return path

    monkeypatch.setattr(Image, "convert_to_file_path", deleting_convert)

    messages = await run_upload(sender, event)

    assert "分类已被删除" in messages[0]
    assert not category_dir.exists()
    assert upload_key(event) not in sender.upload_states


@pytest.mark.asyncio
async def test_chat_upload_busy_lock_preserves_state_and_allows_retry(
    pack_runtime, sender, tmp_path
):
    source = tmp_path / "source.png"
    content = write_png(source)
    event = FakeEvent([])
    await prepare_upload(sender, event, "sad")
    event.message_obj.message = [image_from_filesystem(source)]
    state = sender.upload_states[upload_key(event)]
    sender.semantic_task_manager.begin_external_pack_operation("pack-b", "other task")

    busy = await run_upload(sender, event)

    assert "暂时无法写入" in busy[0]
    assert "pack-b" in busy[0] and "sad" in busy[0]
    assert sender.upload_states[upload_key(event)] is state
    assert list((pack_runtime.packs / "pack-b" / "memes" / "sad").iterdir()) == []

    sender.semantic_task_manager.end_external_pack_operation("pack-b")
    retried = await run_upload(sender, event)

    assert "✅" in retried[0]
    saved = list((pack_runtime.packs / "pack-b" / "memes" / "sad").iterdir())
    assert len(saved) == 1 and saved[0].read_bytes() == content
    assert upload_key(event) not in sender.upload_states


@pytest.mark.asyncio
async def test_chat_upload_replacement_state_survives_old_cleanup(
    pack_runtime, sender, tmp_path, monkeypatch
):
    source = tmp_path / "source.png"
    write_png(source)
    event = FakeEvent([])
    await prepare_upload(sender, event, "sad")
    event.message_obj.message = [image_from_filesystem(source)]
    key = upload_key(event)
    original_convert = Image.convert_to_file_path
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_convert(image_self):
        started.set()
        await release.wait()
        return await original_convert(image_self)

    monkeypatch.setattr(Image, "convert_to_file_path", blocked_convert)
    task = asyncio.create_task(run_upload(sender, event))
    await asyncio.wait_for(started.wait(), timeout=2)
    replacement = {
        "category": "sad",
        "pack_id": "pack-b",
        "memes_dir": str(pack_runtime.packs / "pack-b" / "memes"),
        "expire_time": float("inf"),
    }
    sender.upload_states[key] = replacement
    release.set()

    messages = await asyncio.wait_for(task, timeout=5)

    assert "✅" in messages[0]
    assert sender.upload_states[key] is replacement
    assert len(list((pack_runtime.packs / "pack-b" / "memes" / "sad").iterdir())) == 1


@pytest.mark.asyncio
async def test_chat_upload_refreshes_before_first_yield_and_releases_lock(
    pack_runtime, sender, tmp_path, monkeypatch
):
    source = tmp_path / "source.png"
    write_png(source)
    event = FakeEvent([])
    await prepare_upload(sender, event, "sad")
    event.message_obj.message = [image_from_filesystem(source)]
    manager = sender.semantic_task_manager
    events = []
    original_begin = manager.begin_external_pack_operation
    original_end = manager.end_external_pack_operation

    def begin(pack_id, operation):
        events.append("lock")
        original_begin(pack_id, operation)

    def end(pack_id):
        events.append("unlock")
        original_end(pack_id)

    def invalidate(pack_dir):
        events.append("invalidate")
        assert "pack-b" in manager._external_pack_operations

    async def reload():
        events.append("reload")
        assert "pack-b" in manager._external_pack_operations

    monkeypatch.setattr(manager, "begin_external_pack_operation", begin)
    monkeypatch.setattr(manager, "end_external_pack_operation", end)
    monkeypatch.setattr(event_handlers, "invalidate_semantic_metadata", invalidate)
    sender.reload_emotions = AsyncMock(side_effect=reload)

    generator = sender._handle_upload_image_impl(event)
    first = await generator.__anext__()

    assert "✅" in message_text(first)
    assert events == ["lock", "invalidate", "reload", "unlock"]
    assert manager._external_pack_operations == {}
    await generator.aclose()
    assert manager._external_pack_operations == {}


@pytest.mark.asyncio
async def test_chat_upload_refresh_errors_keep_saved_count(
    pack_runtime, sender, tmp_path, monkeypatch
):
    source = tmp_path / "source.png"
    content = write_png(source)
    event = FakeEvent([])
    await prepare_upload(sender, event, "sad")
    event.message_obj.message = [image_from_filesystem(source)]

    def fail_invalidate(pack_dir):
        raise OSError("metadata refresh failed")

    monkeypatch.setattr(event_handlers, "invalidate_semantic_metadata", fail_invalidate)
    sender.reload_emotions = AsyncMock(side_effect=RuntimeError("reload failed"))

    messages = await run_upload(sender, event)

    text = messages[0]
    assert "✅" in text
    assert "✅ 已经成功收录了 1 张" in text
    assert "语义元数据刷新失败" in text
    assert "表情配置刷新失败" in text
    saved = list((pack_runtime.packs / "pack-b" / "memes" / "sad").iterdir())
    assert len(saved) == 1 and saved[0].read_bytes() == content
    sender.reload_emotions.assert_awaited_once()
    assert upload_key(event) not in sender.upload_states


@pytest.mark.parametrize("paused_worker", ["write", "invalidate"])
@pytest.mark.asyncio
async def test_chat_upload_cancellation_finishes_workers_before_unlock(
    pack_runtime, sender, tmp_path, monkeypatch, paused_worker
):
    source = tmp_path / "source.png"
    content = write_png(source)
    event = FakeEvent([])
    await prepare_upload(sender, event, "sad")
    event.message_obj.message = [image_from_filesystem(source)]

    started = threading.Event()
    release = threading.Event()

    if paused_worker == "write":
        original_save = event_handlers.save_chat_upload_image

        def paused_save(*args, **kwargs):
            result = original_save(*args, **kwargs)
            started.set()
            release.wait(timeout=5)
            return result

        monkeypatch.setattr(event_handlers, "save_chat_upload_image", paused_save)
    else:
        original_invalidate = event_handlers.invalidate_semantic_metadata

        def paused_invalidate(pack_dir):
            result = original_invalidate(pack_dir)
            started.set()
            release.wait(timeout=5)
            return result

        monkeypatch.setattr(
            event_handlers, "invalidate_semantic_metadata", paused_invalidate
        )

    manager = sender.semantic_task_manager
    task = asyncio.create_task(run_upload(sender, event))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()
        assert "pack-b" in manager._external_pack_operations
    finally:
        release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    pack_dir = pack_runtime.packs / "pack-b"
    saved = list((pack_dir / "memes" / "sad").iterdir())
    assert len(saved) == 1 and saved[0].read_bytes() == content
    metadata = json.loads(
        (pack_dir / "semantic_metadata.json").read_text(encoding="utf-8")
    )
    relative_paths = {
        str(item.get("relative_path") or "")
        for item in metadata.get("images", {}).values()
    }
    assert f"memes/sad/{saved[0].name}" in relative_paths
    sender.reload_emotions.assert_awaited_once()
    assert upload_key(event) not in sender.upload_states
    assert manager._external_pack_operations == {}


@pytest.mark.asyncio
async def test_chat_upload_timeout_counts_failure_without_writing(
    pack_runtime, sender, tmp_path, monkeypatch
):
    source = tmp_path / "source.png"
    write_png(source)
    event = FakeEvent([])
    await prepare_upload(sender, event, "sad")
    event.message_obj.message = [image_from_filesystem(source)]

    async def stalled_convert(image_self):
        await asyncio.sleep(10)

    real_wait_for = asyncio.wait_for

    async def short_wait_for(awaitable, timeout):
        return await real_wait_for(awaitable, timeout=0.01)

    monkeypatch.setattr(Image, "convert_to_file_path", stalled_convert)
    monkeypatch.setattr(event_handlers.asyncio, "wait_for", short_wait_for)

    messages = await run_upload(sender, event)

    assert "❌" in messages[0]
    assert "失败 1 张" in messages[0]
    assert list((pack_runtime.packs / "pack-b" / "memes" / "sad").iterdir()) == []
    assert upload_key(event) not in sender.upload_states


@pytest.mark.asyncio
async def test_chat_upload_without_images_keeps_pending_state(pack_runtime, sender):
    event = FakeEvent([])
    await prepare_upload(sender, event, "sad")
    event.message_obj.message = [Plain("hello")]

    messages = await run_upload(sender, event)

    assert messages == ["请发送图片文件来进行上传哦。"]
    assert upload_key(event) in sender.upload_states


@pytest.mark.asyncio
async def test_chat_upload_expired_state_is_discarded_silently(pack_runtime, sender):
    event = FakeEvent([])
    await prepare_upload(sender, event, "sad")
    sender.upload_states[upload_key(event)]["expire_time"] = 0
    event.message_obj.message = [Plain("hello")]

    messages = await run_upload(sender, event)

    assert messages == []
    assert upload_key(event) not in sender.upload_states
