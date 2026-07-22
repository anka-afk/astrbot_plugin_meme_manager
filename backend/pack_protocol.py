from pathlib import Path

PACK_TRANSFER_FORMAT = "astrbot-meme-pack"
PACK_TRANSFER_VERSION = 2
PACK_TRANSFER_MANIFEST = "meme_pack_export.json"
PACK_EXPORT_MODES = {"share", "backup"}


def _require_str(payload: dict, key: str, context: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ValueError(f"{context} 缺少字段: {key}")
    return value


def _ensure_pack_id(pack_id: str, context: str) -> str:
    pack_id = str(pack_id or "").strip()
    if not pack_id:
        raise ValueError(f"{context} 的 id 不能为空")
    if len(pack_id) < 2 or len(pack_id) > 64:
        raise ValueError(f"{context} 的 id 长度非法")
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789._-")
    if any(ch not in allowed for ch in pack_id):
        raise ValueError(f"{context} 的 id 含非法字符")
    return pack_id


def validate_pack_id(pack_id: str, context: str = "pack") -> str:
    """校验可用于目录名和资源包清单的稳定 ID。"""
    return _ensure_pack_id(pack_id, context)


def validate_transfer_manifest(payload: dict, context: str = "导出信息") -> dict:
    """校验新版导入导出压缩包携带的传输信息。"""
    if not isinstance(payload, dict):
        raise ValueError(f"{context} 必须是对象")
    if str(payload.get("format") or "") != PACK_TRANSFER_FORMAT:
        raise ValueError(f"{context}.format 不受支持")

    try:
        format_version = int(payload.get("format_version", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{context}.format_version 无效") from exc
    if format_version < 1 or format_version > PACK_TRANSFER_VERSION:
        raise ValueError(
            f"{context}版本不受支持: {format_version}，当前最高支持 {PACK_TRANSFER_VERSION}"
        )

    export_mode = str(payload.get("export_mode") or "share").strip().lower()
    if export_mode not in PACK_EXPORT_MODES:
        raise ValueError(f"{context}.export_mode 不受支持")

    features = payload.get("features", {})
    if not isinstance(features, dict):
        raise ValueError(f"{context}.features 必须是对象")

    normalized = dict(payload)
    normalized["format_version"] = format_version
    normalized["export_mode"] = export_mode
    normalized["features"] = {
        "semantic_metadata": bool(features.get("semantic_metadata", False)),
        "vectors": bool(features.get("vectors", False)),
    }
    return normalized


def validate_source_descriptor(source: dict, context: str = "source") -> dict:
    if not isinstance(source, dict):
        raise ValueError(f"{context} 必须是对象")

    source_type = str(source.get("type") or "").strip().lower()
    if source_type != "github":
        raise ValueError(f"{context}.type 目前仅支持 github")

    repo = _require_str(source, "repo", context)
    if "/" not in repo:
        raise ValueError(f"{context}.repo 格式应为 owner/repo")

    ref = _require_str(source, "ref", context)
    subpath = _require_str(source, "subpath", context).strip("/")
    if ".." in Path(subpath).parts or "\\" in subpath:
        raise ValueError(f"{context}.subpath 非法")

    return {
        "type": "github",
        "repo": repo,
        "ref": ref,
        "subpath": subpath,
    }


def validate_pack_manifest(manifest: dict, context: str = "manifest") -> dict:
    if not isinstance(manifest, dict):
        raise ValueError(f"{context} 必须是对象")

    pack_id = _ensure_pack_id(_require_str(manifest, "id", context), context)
    name = _require_str(manifest, "name", context)
    version = _require_str(manifest, "version", context)

    categories = manifest.get("categories")
    if not isinstance(categories, dict) or not categories:
        raise ValueError(f"{context}.categories 不能为空")

    normalized_categories = {}
    for category_name, category_meta in categories.items():
        category_name = str(category_name or "").strip()
        if not category_name:
            raise ValueError(f"{context}.categories 存在空分类名")

        if isinstance(category_meta, dict):
            description = str(category_meta.get("description") or "").strip()
        else:
            description = str(category_meta or "").strip()

        normalized_categories[category_name] = {
            "description": description or "请添加描述"
        }

    source = manifest.get("source")
    normalized_source = None
    if source is not None:
        normalized_source = validate_source_descriptor(source, f"{context}.source")

    normalized_manifest = dict(manifest)
    normalized_manifest["id"] = pack_id
    normalized_manifest["name"] = name
    normalized_manifest["version"] = version
    normalized_manifest["categories"] = normalized_categories
    if normalized_source is not None:
        normalized_manifest["source"] = normalized_source

    return normalized_manifest


def validate_pack_directory(pack_root: Path, context: str = "pack") -> dict:
    if not pack_root.is_dir():
        raise ValueError(f"{context} 目录不存在")

    manifest_path = pack_root / "manifest.json"
    memes_dir = pack_root / "memes"
    if not manifest_path.is_file():
        raise ValueError(f"{context} 缺少 manifest.json")
    if not memes_dir.is_dir():
        raise ValueError(f"{context} 缺少 memes 目录")

    try:
        import json

        with manifest_path.open(encoding="utf-8-sig") as file_obj:
            manifest = json.load(file_obj)
    except Exception as exc:
        raise ValueError(f"{context} 的 manifest.json 无法解析: {exc}") from exc

    normalized_manifest = validate_pack_manifest(manifest, f"{context}.manifest")

    return normalized_manifest


def validate_community_index(
    index_data: dict, context: str = "community_index"
) -> dict:
    if not isinstance(index_data, dict):
        raise ValueError(f"{context} 必须是对象")

    packs = index_data.get("packs")
    if not isinstance(packs, list):
        raise ValueError(f"{context}.packs 必须是数组")

    normalized_packs = []
    seen_ids = set()
    for index, entry in enumerate(packs):
        if not isinstance(entry, dict):
            raise ValueError(f"{context}.packs[{index}] 必须是对象")

        entry_context = f"{context}.packs[{index}]"
        pack_id = _ensure_pack_id(
            _require_str(entry, "id", entry_context), entry_context
        )
        if pack_id in seen_ids:
            raise ValueError(f"{entry_context} 的 id 重复: {pack_id}")
        seen_ids.add(pack_id)

        _require_str(entry, "name", entry_context)
        _require_str(entry, "maintainer", entry_context)
        _require_str(entry, "description", entry_context)
        _require_str(entry, "license", entry_context)

        previews = entry.get("previews")
        if not isinstance(previews, list) or not previews:
            raise ValueError(f"{entry_context}.previews 不能为空")

        source = validate_source_descriptor(
            entry.get("source"), f"{entry_context}.source"
        )

        normalized_entry = dict(entry)
        normalized_entry["id"] = pack_id
        normalized_entry["source"] = source
        normalized_packs.append(normalized_entry)

    normalized = dict(index_data)
    normalized["packs"] = normalized_packs
    return normalized


def is_official_pack_entry(entry: dict) -> bool:
    if not isinstance(entry, dict):
        return False
    pack_id = str(entry.get("id") or "").strip().lower()
    tags = entry.get("tags") if isinstance(entry.get("tags"), list) else []
    tag_set = {str(tag or "").strip().lower() for tag in tags}
    return pack_id.startswith("official-") or "official" in tag_set
