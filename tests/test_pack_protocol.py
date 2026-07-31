import json

import pytest
from backend import pack_protocol as protocol


def valid_manifest(**overrides):
    manifest = {
        "id": "test-pack",
        "name": "测试资源包",
        "version": "1.0.0",
        "categories": {"happy": {"description": "开心"}},
    }
    manifest.update(overrides)
    return manifest


@pytest.mark.parametrize("pack_id", ["ab", "pack-name", "pack_name", "pack.name", "a1"])
def test_validate_pack_id_accepts_stable_ids(pack_id):
    assert protocol.validate_pack_id(pack_id) == pack_id


@pytest.mark.parametrize(
    "pack_id",
    ["", "a", "A-pack", "有中文", "with space", "slash/name", "x" * 65],
)
def test_validate_pack_id_rejects_invalid_ids(pack_id):
    with pytest.raises(ValueError):
        protocol.validate_pack_id(pack_id)


def test_validate_transfer_manifest_normalizes_optional_fields():
    result = protocol.validate_transfer_manifest(
        {
            "format": protocol.PACK_TRANSFER_FORMAT,
            "format_version": "2",
            "export_mode": " BACKUP ",
            "features": {"semantic_metadata": 1, "vectors": 0, "ignored": True},
        }
    )
    assert result["format_version"] == 2
    assert result["export_mode"] == "backup"
    assert result["features"] == {"semantic_metadata": True, "vectors": False}


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"format": "wrong"},
        {"format": protocol.PACK_TRANSFER_FORMAT, "format_version": "bad"},
        {"format": protocol.PACK_TRANSFER_FORMAT, "format_version": 99},
        {
            "format": protocol.PACK_TRANSFER_FORMAT,
            "format_version": 1,
            "export_mode": "invalid",
        },
        {
            "format": protocol.PACK_TRANSFER_FORMAT,
            "format_version": 1,
            "features": [],
        },
    ],
)
def test_validate_transfer_manifest_rejects_invalid_payloads(payload):
    with pytest.raises(ValueError):
        protocol.validate_transfer_manifest(payload)


def test_validate_source_descriptor_normalizes_github_source():
    assert protocol.validate_source_descriptor(
        {
            "type": " GITHUB ",
            "repo": "owner/repo",
            "ref": "main",
            "subpath": "/packs/demo/",
        }
    ) == {
        "type": "github",
        "repo": "owner/repo",
        "ref": "main",
        "subpath": "packs/demo",
    }


@pytest.mark.parametrize(
    "source",
    [
        None,
        {},
        {"type": "gitlab", "repo": "owner/repo", "ref": "main", "subpath": "p"},
        {"type": "github", "repo": "owner", "ref": "main", "subpath": "p"},
        {"type": "github", "repo": "owner/repo", "ref": "", "subpath": "p"},
        {
            "type": "github",
            "repo": "owner/repo",
            "ref": "main",
            "subpath": "../secret",
        },
        {
            "type": "github",
            "repo": "owner/repo",
            "ref": "main",
            "subpath": "packs\\demo",
        },
    ],
)
def test_validate_source_descriptor_rejects_invalid_sources(source):
    with pytest.raises(ValueError):
        protocol.validate_source_descriptor(source)


def test_validate_pack_manifest_normalizes_categories_and_source():
    result = protocol.validate_pack_manifest(
        valid_manifest(
            categories={"happy": "开心", "sad": {"description": ""}},
            source={
                "type": "github",
                "repo": "owner/repo",
                "ref": "main",
                "subpath": "packs/demo",
            },
        )
    )
    assert result["categories"] == {
        "happy": {"description": "开心"},
        "sad": {"description": "请添加描述"},
    }
    assert result["source"]["repo"] == "owner/repo"


@pytest.mark.parametrize(
    "manifest",
    [
        None,
        {},
        valid_manifest(name=""),
        valid_manifest(version=""),
        valid_manifest(categories={}),
        valid_manifest(categories=[]),
        valid_manifest(categories={"": "空"}),
        valid_manifest(categories={"../escape": "越界"}),
    ],
)
def test_validate_pack_manifest_rejects_invalid_manifests(manifest):
    with pytest.raises(ValueError):
        protocol.validate_pack_manifest(manifest)


def test_validate_pack_directory_reads_valid_manifest(tmp_path):
    pack_dir = tmp_path / "pack"
    (pack_dir / "memes").mkdir(parents=True)
    (pack_dir / "manifest.json").write_text(
        json.dumps(valid_manifest(), ensure_ascii=False), encoding="utf-8"
    )
    result = protocol.validate_pack_directory(pack_dir)
    assert result["id"] == "test-pack"


@pytest.mark.parametrize("missing", ["directory", "manifest", "memes", "json"])
def test_validate_pack_directory_rejects_incomplete_packs(tmp_path, missing):
    pack_dir = tmp_path / "pack"
    if missing == "directory":
        with pytest.raises(ValueError):
            protocol.validate_pack_directory(pack_dir)
        return

    pack_dir.mkdir()
    if missing != "memes":
        (pack_dir / "memes").mkdir()
    if missing != "manifest":
        content = "not json" if missing == "json" else json.dumps(valid_manifest())
        (pack_dir / "manifest.json").write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        protocol.validate_pack_directory(pack_dir)


def test_validate_community_index_normalizes_entries():
    entry = {
        "id": "community-pack",
        "name": "社区包",
        "maintainer": "tester",
        "description": "description",
        "license": "MIT",
        "previews": ["preview.png"],
        "source": {
            "type": "github",
            "repo": "owner/repo",
            "ref": "main",
            "subpath": "packs/community",
        },
    }
    result = protocol.validate_community_index({"packs": [entry]})
    assert result["packs"][0]["id"] == "community-pack"
    assert result["packs"][0]["source"]["type"] == "github"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"packs": {}},
        {"packs": [None]},
        {"packs": [{"id": "missing-fields"}]},
        {
            "packs": [
                {
                    "id": "duplicate",
                    "name": "a",
                    "maintainer": "a",
                    "description": "a",
                    "license": "MIT",
                    "previews": ["a"],
                    "source": {
                        "type": "github",
                        "repo": "a/b",
                        "ref": "main",
                        "subpath": "a",
                    },
                }
            ]
            * 2
        },
    ],
)
def test_validate_community_index_rejects_invalid_payloads(payload):
    with pytest.raises(ValueError):
        protocol.validate_community_index(payload)


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"id": "official-demo"}, True),
        ({"id": "demo", "tags": ["Official"]}, True),
        ({"id": "demo", "tags": ["community"]}, False),
        (None, False),
    ],
)
def test_is_official_pack_entry(entry, expected):
    assert protocol.is_official_pack_entry(entry) is expected
