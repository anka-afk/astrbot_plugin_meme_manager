import json
import zipfile
from types import SimpleNamespace

import pytest
import requests
from astrbot_plugin_meme_manager.backend import pack_storage as storage


@pytest.mark.parametrize(
    ("raw", "accelerator", "expected"),
    [
        ("https://github.com/a", "", "https://github.com/a"),
        (
            "https://github.com/a",
            "https://proxy/",
            "https://proxy/https://github.com/a",
        ),
        (
            "https://github.com/a",
            "https://proxy/{url}",
            "https://proxy/https://github.com/a",
        ),
        ("", "https://proxy", ""),
    ],
)
def test_build_accelerated_url(raw, accelerator, expected):
    assert storage._build_accelerated_url(raw, accelerator) == expected


def test_http_get_uses_accelerator_and_falls_back(monkeypatch):
    class Response:
        def __init__(self, status_code):
            self.status_code = status_code
            self.closed = False

        def close(self):
            self.closed = True

    accelerated = Response(500)
    native = Response(200)
    responses = iter([accelerated, native])
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return next(responses)

    monkeypatch.setattr(storage.requests, "get", get)
    result = storage._http_get_with_optional_acceleration(
        "https://github.com/a", 10, "https://proxy", stream=True
    )
    assert result is native
    assert accelerated.closed
    assert calls[0][0] == "https://proxy/https://github.com/a"
    assert calls[1][0] == "https://github.com/a"
    assert calls[1][1] == {"timeout": 10, "stream": True}


def test_http_get_reports_both_failures(monkeypatch):
    def get(url, **kwargs):
        raise requests.RequestException(url)

    monkeypatch.setattr(storage.requests, "get", get)
    with pytest.raises(ValueError, match="加速与原生请求均失败"):
        storage._http_get_with_optional_acceleration(
            "https://github.com/a", 10, "https://proxy"
        )


def test_archive_download_falls_back_after_accelerated_stream_failure(
    tmp_path, monkeypatch
):
    class Response:
        status_code = 200
        headers = {}

        def __init__(self, chunks):
            self.chunks = chunks
            self.closed = False

        def iter_content(self, chunk_size):
            del chunk_size
            for chunk in self.chunks:
                if isinstance(chunk, Exception):
                    raise chunk
                yield chunk

        def close(self):
            self.closed = True

    accelerated = Response([b"partial", requests.ReadTimeout("stalled")])
    native = Response([b"complete"])
    responses = iter([accelerated, native])
    calls = []
    monkeypatch.setattr(
        storage.requests,
        "get",
        lambda url, **kwargs: calls.append((url, kwargs)) or next(responses),
    )

    target = tmp_path / "archive.zip"
    storage._download_github_archive(
        "owner/repo",
        "main",
        target,
        github_accelerator_url="https://proxy/",
    )

    assert target.read_bytes() == b"complete"
    assert calls[0][0].startswith("https://proxy/")
    assert calls[1][0] == "https://github.com/owner/repo/archive/main.zip"
    assert accelerated.closed and native.closed


def test_archive_download_cancellation_removes_partial_file(tmp_path, monkeypatch):
    class Response:
        status_code = 200
        headers = {}

        def iter_content(self, chunk_size):
            del chunk_size
            yield b"partial"
            yield b"ignored"

        def close(self):
            pass

    checks = iter([False, False, True])
    monkeypatch.setattr(storage.requests, "get", lambda *args, **kwargs: Response())
    target = tmp_path / "archive.zip"

    with pytest.raises(storage.InstallCancelledError, match="安装已取消"):
        storage._download_github_archive(
            "owner/repo",
            "main",
            target,
            cancel_check=lambda: next(checks),
        )
    assert not target.exists()


def test_atomic_json_and_snapshot_helpers(tmp_path):
    path = tmp_path / "state" / "data.json"
    storage._save_json(path, {"value": "开心"})
    original = path.read_bytes()
    assert storage._load_json(path, {}) == {"value": "开心"}
    storage._save_json(path, {"value": "changed"})
    storage._restore_file_snapshot(path, original)
    assert path.read_bytes() == original
    storage._restore_file_snapshot(path, None)
    assert not path.exists()


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0), (" 12 ", 12), (-1, None), (True, None), ("bad", None)],
)
def test_safe_nonnegative_integer(value, expected):
    assert storage._safe_nonnegative_int(value) == expected


def test_index_bundle_details_validates_manifest_path(tmp_path):
    root = tmp_path / "index"
    root.mkdir()
    index_path = root / "snapshot.faiss"
    index_path.write_bytes(b"index")
    (root / "index_manifest.json").write_text(
        json.dumps({"index_file": "snapshot.faiss"}), encoding="utf-8"
    )
    manifest, resolved = storage._index_bundle_details(root)
    assert manifest["index_file"] == "snapshot.faiss"
    assert resolved == index_path
    (root / "index_manifest.json").write_text(
        json.dumps({"index_file": "../escape.faiss"}), encoding="utf-8"
    )
    assert storage._index_bundle_details(root)[1] is None


def test_directory_size_and_free_space_guard(tmp_path, monkeypatch):
    (tmp_path / "nested").mkdir()
    (tmp_path / "a.bin").write_bytes(b"123")
    (tmp_path / "nested" / "b.bin").write_bytes(b"4567")
    assert storage._directory_size(tmp_path) == 7
    assert storage._directory_size(tmp_path / "missing") == 0
    monkeypatch.setattr(
        storage.shutil, "disk_usage", lambda path: SimpleNamespace(free=100)
    )
    storage._require_free_space(tmp_path / "space", 100, "测试")
    with pytest.raises(ValueError, match="剩余磁盘空间不足"):
        storage._require_free_space(tmp_path / "space", 101, "测试")


def test_legacy_pack_and_installed_pack_helpers():
    assert storage._is_legacy_pack(storage.LEGACY_MIGRATED_PACK_ID, {})
    assert storage._is_legacy_pack("custom", {"tags": ["Converted"]})
    assert not storage._is_legacy_pack("custom", {"tags": ["normal"]})
    assert storage._normalize_installed_packs([{"id": "a"}, None, "bad"]) == [
        {"id": "a"}
    ]
    assert storage._normalize_installed_packs({}) == []


def test_count_images_only_counts_supported_files_in_categories(tmp_path):
    memes = tmp_path / "memes"
    category = memes / "happy"
    category.mkdir(parents=True)
    (category / "one.PNG").write_bytes(b"1")
    (category / "ignore.txt").write_bytes(b"2")
    (memes / "root.png").write_bytes(b"3")
    assert storage._count_images(memes) == 1
    assert storage._count_images(tmp_path / "missing") == 0


def test_archive_root_and_manifest_detection(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "manifest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "__MACOSX").mkdir()
    assert storage._find_manifest_root(tmp_path) == nested
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="多个 manifest"):
        storage._find_manifest_root(tmp_path)


def test_find_import_root_detects_v1_v2_and_legacy(tmp_path):
    v1 = tmp_path / "v1"
    v1.mkdir()
    (v1 / "manifest.json").write_text("{}", encoding="utf-8")
    assert storage._find_import_root(tmp_path) == (v1, "v1")
    (v1 / storage.PACK_TRANSFER_MANIFEST).write_text("{}", encoding="utf-8")
    assert storage._find_import_root(tmp_path) == (v1, "v2")

    (v1 / "manifest.json").unlink()
    legacy_memes = v1 / "memes" / "happy"
    legacy_memes.mkdir(parents=True)
    (legacy_memes / "meme.png").write_bytes(b"image")
    assert storage._find_import_root(tmp_path) == (v1, "legacy")


@pytest.mark.parametrize(
    ("value", "expected"),
    [("My Pack", "my-pack"), ("a", "legacy-pack"), ("中文", "legacy-pack")],
)
def test_legacy_pack_id_normalization(value, expected):
    assert storage._legacy_pack_id(value) == expected


def test_copy_legacy_pack_supports_direct_category_layout(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    category = source / "happy"
    category.mkdir(parents=True)
    (category / "meme.png").write_bytes(b"image")
    (source / "memes_data.json").write_text(
        json.dumps({"happy": "开心"}, ensure_ascii=False), encoding="utf-8"
    )
    manifest = storage._copy_legacy_pack(source, target, "My Pack")
    assert manifest["id"] == "my-pack"
    assert (target / "memes" / "happy" / "meme.png").is_file()
    assert storage._load_json(target / "memes_data.json", {}) == {"happy": "开心"}


def make_zip(path, files):
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)


def test_extract_zip_safely_extracts_regular_archive(tmp_path, monkeypatch):
    archive_path = tmp_path / "pack.zip"
    make_zip(archive_path, {"pack/manifest.json": "{}", "pack/memes/a.png": b"image"})
    monkeypatch.setattr(storage, "_require_free_space", lambda *args: None)
    target = tmp_path / "extract"
    storage._extract_zip_safely(archive_path, target)
    assert (target / "pack" / "memes" / "a.png").is_file()


@pytest.mark.parametrize("member", ["../escape.txt", "pack/script.sh"])
def test_extract_zip_safely_rejects_traversal_and_scripts(
    tmp_path, monkeypatch, member
):
    archive_path = tmp_path / "bad.zip"
    make_zip(archive_path, {member: "bad"})
    monkeypatch.setattr(storage, "_require_free_space", lambda *args: None)
    with pytest.raises(ValueError):
        storage._extract_zip_safely(archive_path, tmp_path / "extract")


def test_load_transfer_info_defaults_for_legacy_format(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    assert storage._load_transfer_info(pack, "v1") == {
        "format": storage.PACK_TRANSFER_FORMAT,
        "format_version": 1,
        "export_mode": "share",
        "features": {"semantic_metadata": False, "vectors": False},
    }


def test_validate_and_normalize_selection_rules():
    rules = [
        {"id": "session", "scope": "SESSION", "target": "s1", "pack_id": "a"},
        {"id": "default", "scope": "default", "pack_id": "b"},
    ]
    assert storage._validate_and_normalize_rules(rules, {"a", "b"}) == [
        {"id": "session", "scope": "session", "pack_id": "a", "target": "s1"},
        {"id": "default", "scope": "default", "pack_id": "b"},
    ]


@pytest.mark.parametrize(
    "rules",
    [
        [],
        [None],
        [{"id": "x", "scope": "bad", "pack_id": "a"}],
        [{"id": "x", "scope": "session", "pack_id": "a"}],
        [{"id": "default", "scope": "default", "pack_id": "missing"}],
        [
            {"id": "default", "scope": "default", "pack_id": "a"},
            {"id": "session", "scope": "session", "target": "s", "pack_id": "a"},
        ],
    ],
)
def test_validate_and_normalize_selection_rules_rejects_invalid_rules(rules):
    with pytest.raises(ValueError):
        storage._validate_and_normalize_rules(rules, {"a"})
