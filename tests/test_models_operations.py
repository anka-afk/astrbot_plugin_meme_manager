import io
from pathlib import Path

import pytest
from astrbot_plugin_meme_manager.backend import models


class UploadedFile:
    def __init__(self, filename, content):
        self.filename = filename
        self.stream = io.BytesIO(content)


class SavedFile:
    def __init__(self, filename, content):
        self.filename = filename
        self.content = content

    def save(self, target):
        Path(target).write_bytes(self.content)


@pytest.fixture
def memes_root(tmp_path, monkeypatch):
    root = tmp_path / "memes"
    root.mkdir()
    monkeypatch.setattr(models, "resolve_pack_context", lambda: {"memes_dir": root})
    return root


def test_supported_image_and_available_filename_helpers(tmp_path):
    assert models._is_supported_image("meme.PNG")
    assert not models._is_supported_image("meme.txt")
    assert models._iter_category_image_paths(tmp_path) == []
    (tmp_path / "a.png").write_bytes(b"a")
    (tmp_path / "b.txt").write_bytes(b"b")
    assert models._iter_category_image_paths(tmp_path) == [tmp_path / "a.png"]
    assert models._build_available_file_path(tmp_path, "a.png") == tmp_path / "a_1.png"
    (tmp_path / "a_1.png").write_bytes(b"a")
    assert models._build_available_file_path(tmp_path, "a.png") == tmp_path / "a_2.png"


@pytest.mark.asyncio
async def test_scan_emoji_folder_filters_files(memes_root):
    happy = memes_root / "happy"
    happy.mkdir()
    (happy / "one.png").write_bytes(b"one")
    (happy / "ignore.txt").write_bytes(b"ignore")
    (memes_root / "root.png").write_bytes(b"root")
    assert await models.scan_emoji_folder() == {"happy": ["one.png"]}


def test_add_emoji_handles_duplicate_content_and_filename_collision(memes_root):
    first = models.add_emoji_to_category("happy", UploadedFile("same.png", b"one"))
    second = models.add_emoji_to_category("happy", UploadedFile("same.png", b"two"))
    assert first["filename"] == "same.png"
    assert second["filename"] == "same_1.png"
    assert sorted(models.get_emoji_by_category("happy")) == ["same.png", "same_1.png"]
    with pytest.raises(models.DuplicateEmojiError) as error:
        models.add_emoji_to_category("happy", UploadedFile("copy.png", b"one"))
    assert error.value.existing_filename == "same.png"


@pytest.mark.parametrize(
    "uploaded_file",
    [None, UploadedFile("", b"image"), UploadedFile("meme.png", b"")],
)
def test_add_emoji_rejects_missing_upload_data(memes_root, uploaded_file):
    expected_error = (
        ValueError if uploaded_file is None or not uploaded_file.filename else OSError
    )
    with pytest.raises(expected_error):
        models.add_emoji_to_category("happy", uploaded_file)


def test_delete_and_batch_delete_report_results(memes_root):
    category = memes_root / "happy"
    category.mkdir()
    (category / "one.png").write_bytes(b"one")
    (category / "two.webp").write_bytes(b"two")
    (category / "ignore.txt").write_bytes(b"ignore")
    assert models.delete_emoji_from_category("happy", "../one.png")
    assert not models.delete_emoji_from_category("happy", "missing.png")
    result = models.batch_delete_emojis(
        "happy", ["two.webp", "two.webp", "missing.png"]
    )
    assert result["deleted_files"] == ["two.webp"]
    assert result["missing_files"] == ["missing.png"]
    assert (
        models.batch_delete_emojis("missing", ["one.png"])["category_exists"] is False
    )


def test_move_emoji_success_missing_conflict_and_missing_source(memes_root):
    source = memes_root / "source"
    target = memes_root / "target"
    source.mkdir()
    target.mkdir()
    (source / "move.png").write_bytes(b"move")
    result = models.move_emoji_to_category("source", "move.png", "target")
    assert result["moved"]
    assert (target / "move.png").is_file()

    (source / "conflict.png").write_bytes(b"source")
    (target / "conflict.png").write_bytes(b"target")
    assert models.move_emoji_to_category("source", "conflict.png", "target")["conflict"]
    assert models.move_emoji_to_category("source", "missing.png", "target")["missing"]
    assert not models.move_emoji_to_category("missing", "x.png", "target")[
        "source_category_exists"
    ]


def test_batch_move_deduplicates_and_groups_outcomes(memes_root):
    source = memes_root / "source"
    target = memes_root / "target"
    source.mkdir()
    target.mkdir()
    (source / "move.png").write_bytes(b"move")
    (source / "conflict.png").write_bytes(b"source")
    (target / "conflict.png").write_bytes(b"target")
    result = models.batch_move_emojis(
        "source", ["move.png", "move.png", "conflict.png", "missing.png"], "target"
    )
    assert result["moved_files"] == ["move.png"]
    assert result["conflicting_files"] == ["conflict.png"]
    assert result["missing_files"] == ["missing.png"]
    assert not models.batch_move_emojis("missing", [], "target")[
        "source_category_exists"
    ]


def test_copy_and_batch_copy_keep_source_files(memes_root):
    source = memes_root / "source"
    target = memes_root / "target"
    source.mkdir()
    target.mkdir()
    (source / "copy.png").write_bytes(b"copy")
    assert models.copy_emoji_to_category("source", "copy.png", "target")["copied"]
    assert (source / "copy.png").is_file()
    assert (target / "copy.png").is_file()
    assert models.copy_emoji_to_category("source", "copy.png", "target")["conflict"]
    assert models.copy_emoji_to_category("source", "missing.png", "target")["missing"]
    assert not models.copy_emoji_to_category("missing", "x.png", "target")[
        "source_category_exists"
    ]

    (source / "second.png").write_bytes(b"second")
    result = models.batch_copy_emojis(
        "source", ["second.png", "second.png", "copy.png", "missing.png"], "target"
    )
    assert result["copied_files"] == ["second.png"]
    assert result["conflicting_files"] == ["copy.png"]
    assert result["missing_files"] == ["missing.png"]
    assert not models.batch_copy_emojis("missing", [], "target")[
        "source_category_exists"
    ]


def test_clear_category_and_all_keep_directories_and_non_images(memes_root):
    for category_name in ("happy", "sad"):
        category = memes_root / category_name
        category.mkdir()
        (category / "meme.png").write_bytes(b"image")
        (category / "note.txt").write_bytes(b"note")
    result = models.clear_category_emojis("happy")
    assert result == {"category_exists": True, "deleted_files": ["meme.png"]}
    assert (memes_root / "happy" / "note.txt").is_file()
    assert models.clear_category_emojis("missing")["category_exists"] is False

    result = models.clear_all_emojis()
    assert result == {"deleted_by_category": {"sad": 1}}
    assert (memes_root / "sad").is_dir()


def test_clear_all_handles_missing_root(tmp_path, monkeypatch):
    missing = tmp_path / "missing"
    monkeypatch.setattr(models, "resolve_pack_context", lambda: {"memes_dir": missing})
    assert models.clear_all_emojis() == {"deleted_by_category": {}}


def test_update_emoji_replaces_supported_file(memes_root):
    category = memes_root / "happy"
    category.mkdir()
    (category / "old.png").write_bytes(b"old")
    assert models.update_emoji_in_category(
        "happy", "old.png", SavedFile("new.webp", b"new")
    )
    assert not (category / "old.png").exists()
    assert (category / "new.webp").read_bytes() == b"new"
    assert not models.update_emoji_in_category(
        "happy", "missing.png", SavedFile("new.png", b"new")
    )
    assert not models.update_emoji_in_category(
        "missing", "old.png", SavedFile("new.png", b"new")
    )


@pytest.mark.parametrize("filename", ["", "bad.txt", "中文.png"])
def test_update_emoji_rejects_unsafe_new_filename(memes_root, filename):
    category = memes_root / "happy"
    category.mkdir()
    (category / "old.png").write_bytes(b"old")
    with pytest.raises(ValueError):
        models.update_emoji_in_category("happy", "old.png", SavedFile(filename, b"new"))
