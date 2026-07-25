import json
import os
import re
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import requests

from ..config import (
    BACKUP_DIR,
    COMMUNITY_CACHE_PATH,
    DEFAULT_CATEGORY_DESCRIPTIONS,
    DEFAULT_PACK_ID,
    LEGACY_MIGRATED_PACK_ID,
    PACKS_DIR,
    PLUGIN_DATA_DIR,
    REGISTRY_PATH,
    RUNTIME_SCHEMA_VERSION,
    SELECTION_RULES_PATH,
    TEMP_DIR,
)
from .pack_protocol import (
    PACK_EXPORT_MODES,
    PACK_TRANSFER_FORMAT,
    PACK_TRANSFER_MANIFEST,
    PACK_TRANSFER_VERSION,
    is_official_pack_entry,
    validate_community_index,
    validate_pack_directory,
    validate_pack_id,
    validate_pack_manifest,
    validate_source_descriptor,
    validate_transfer_manifest,
)
from .semantic_index import index_is_ready, load_index_manifest
from .semantic_storage import (
    LEGACY_METADATA_BACKUP_NAME,
    get_pack_semantic_summary,
    import_metadata_file,
    load_metadata,
    reconcile_metadata,
    reset_local_embedding_state,
    save_metadata,
)

PackOperationGuard = Callable[[str, str], None]
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_ARCHIVE_FILE_COUNT = 20_000
MAX_ARCHIVE_COMPRESSED_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024
MAX_ARCHIVE_SINGLE_FILE_BYTES = 1024 * 1024 * 1024
MIN_FREE_SPACE_RESERVE_BYTES = 512 * 1024 * 1024
ARCHIVE_JSON_SIZE_LIMITS = {
    "manifest.json": 4 * 1024 * 1024,
    PACK_TRANSFER_MANIFEST: 1024 * 1024,
    "memes_data.json": 16 * 1024 * 1024,
    "semantic_metadata.json": 256 * 1024 * 1024,
    "index_manifest.json": 256 * 1024 * 1024,
}


def _build_accelerated_url(raw_url: str, github_accelerator_url: str) -> str:
    accelerator = str(github_accelerator_url or "").strip()
    url = str(raw_url or "").strip()
    if not accelerator or not url:
        return url
    if "{url}" in accelerator:
        return accelerator.replace("{url}", url)
    if accelerator.endswith("/"):
        return f"{accelerator}{url}"
    return f"{accelerator}/{url}"


def _http_get_with_optional_acceleration(
    raw_url: str,
    timeout: int,
    github_accelerator_url: str = "",
) -> requests.Response:
    request_url = _build_accelerated_url(raw_url, github_accelerator_url)
    last_error = None

    if request_url and request_url != raw_url:
        try:
            accelerated_response = requests.get(request_url, timeout=timeout)
            if accelerated_response.status_code == 200:
                return accelerated_response
            last_error = ValueError(
                "加速地址请求失败，状态码: "
                f"{accelerated_response.status_code}"
            )
        except Exception as exc:
            last_error = exc

    try:
        native_response = requests.get(raw_url, timeout=timeout)
        return native_response
    except Exception as exc:
        if last_error is not None:
            raise ValueError(f"加速与原生请求均失败: {last_error}; {exc}") from exc
        raise


def _load_json(path: Path, default):
    try:
        with path.open(encoding="utf-8-sig") as file_obj:
            return json.load(file_obj)
    except Exception:
        return default


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file_obj:
            json.dump(data, file_obj, ensure_ascii=False, indent=2)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _restore_file_snapshot(path: Path, content: bytes | None) -> None:
    """Restore one transaction control file from an exact byte snapshot.

    Args:
        path: Runtime control file to restore.
        content: Original bytes, or ``None`` if the file did not exist.
    """
    if content is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".rollback", dir=path.parent
    )
    try:
        with os.fdopen(fd, "wb") as file_obj:
            file_obj.write(content)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _safe_nonnegative_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
    else:
        return None
    return parsed if parsed >= 0 else None


def _index_bundle_details(index_root: Path) -> tuple[dict, Path | None]:
    """Resolve the manifest-selected FAISS file from old or snapshot bundles.

    Args:
        index_root: Extracted semantic index directory.

    Returns:
        The parsed manifest and its safe existing FAISS file, if any.
    """
    manifest = _load_json(index_root / "index_manifest.json", {})
    if not isinstance(manifest, dict):
        return {}, None
    filename = str(manifest.get("index_file") or "index.faiss").strip()
    if (
        not filename
        or Path(filename).name != filename
        or not filename.endswith(".faiss")
    ):
        return manifest, None
    index_path = index_root / filename
    return manifest, index_path if index_path.is_file() else None


def _directory_size(path: Path) -> int:
    if not path.is_dir():
        return 0
    total = 0
    for file_path in path.rglob("*"):
        if not file_path.is_file():
            continue
        try:
            total += max(0, file_path.stat().st_size)
        except OSError:
            continue
    return total


def _require_regular_tree(path: Path, operation: str) -> None:
    if path.is_symlink():
        raise ValueError(f"{operation}目录不能是符号链接")
    if not path.is_dir():
        return
    for item in path.rglob("*"):
        if item.is_symlink():
            raise ValueError(f"{operation}包含符号链接，已拒绝处理: {item.name}")


def _require_free_space(path: Path, required_bytes: int, operation: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    free_bytes = shutil.disk_usage(path).free
    if free_bytes < max(0, int(required_bytes)):
        required_gib = required_bytes / (1024**3)
        free_gib = free_bytes / (1024**3)
        raise ValueError(
            f"剩余磁盘空间不足，无法{operation}：预计至少需要 {required_gib:.2f} GB，"
            f"当前可用 {free_gib:.2f} GB"
        )


def _is_legacy_pack(pack_id: str, manifest: dict) -> bool:
    tags = {
        str(item or "").strip().lower()
        for item in manifest.get("tags", [])
        if str(item or "").strip()
    }
    return str(pack_id or "").strip() == LEGACY_MIGRATED_PACK_ID or bool(
        tags.intersection({"legacy", "converted"})
    )


def _normalize_installed_packs(installed_packs) -> list[dict]:
    if not isinstance(installed_packs, list):
        return []
    normalized = []
    for item in installed_packs:
        if isinstance(item, dict):
            normalized.append(item)
    return normalized


def _load_registry() -> dict:
    registry = _load_json(
        REGISTRY_PATH,
        {"schema_version": RUNTIME_SCHEMA_VERSION, "installed_packs": []},
    )
    registry["schema_version"] = RUNTIME_SCHEMA_VERSION
    registry["installed_packs"] = _normalize_installed_packs(
        registry.get("installed_packs", [])
    )
    return registry


def _save_registry(registry: dict) -> None:
    registry["schema_version"] = RUNTIME_SCHEMA_VERSION
    registry["installed_packs"] = _normalize_installed_packs(
        registry.get("installed_packs", [])
    )
    _save_json(REGISTRY_PATH, registry)


def _load_selection_rules() -> dict:
    selection_rules = _load_json(
        SELECTION_RULES_PATH,
        {
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "rules": [
                {"id": "default", "scope": "default", "pack_id": DEFAULT_PACK_ID}
            ],
        },
    )
    if not isinstance(selection_rules, dict):
        selection_rules = {}
    rules = selection_rules.get("rules", [])
    if not isinstance(rules, list):
        rules = []
    selection_rules["schema_version"] = RUNTIME_SCHEMA_VERSION
    selection_rules["rules"] = [rule for rule in rules if isinstance(rule, dict)]
    return selection_rules


def _save_selection_rules(selection_rules: dict) -> None:
    selection_rules["schema_version"] = RUNTIME_SCHEMA_VERSION
    rules = selection_rules.get("rules", [])
    if not isinstance(rules, list):
        rules = []
    selection_rules["rules"] = [rule for rule in rules if isinstance(rule, dict)]
    _save_json(SELECTION_RULES_PATH, selection_rules)


def _load_manifest(pack_id: str) -> dict:
    manifest_path = PACKS_DIR / pack_id / "manifest.json"
    manifest = _load_json(manifest_path, {})
    if not isinstance(manifest, dict):
        return {}
    try:
        return validate_pack_manifest(manifest)
    except Exception:
        return manifest


def _count_images(memes_dir: Path) -> int:
    if not memes_dir.is_dir():
        return 0
    total = 0
    for category_dir in memes_dir.iterdir():
        if not category_dir.is_dir():
            continue
        for file_path in category_dir.iterdir():
            if file_path.is_file() and file_path.suffix.lower() in {
                ".png",
                ".jpg",
                ".jpeg",
                ".gif",
                ".webp",
            }:
                total += 1
    return total


def _current_default_pack_id() -> str:
    selection_rules = _load_selection_rules()
    for rule in reversed(selection_rules.get("rules", [])):
        if str(rule.get("scope") or "") == "default":
            pack_id = str(rule.get("pack_id") or "").strip()
            if pack_id:
                return pack_id
    for fallback_pack_id in (LEGACY_MIGRATED_PACK_ID, DEFAULT_PACK_ID):
        if (PACKS_DIR / fallback_pack_id).is_dir():
            return fallback_pack_id
    return DEFAULT_PACK_ID


def _snapshot_single_empty_pack() -> str | None:
    """快照当前是否仅存在一个空表情包。"""
    if not PACKS_DIR.is_dir():
        return None

    pack_dirs = sorted(path for path in PACKS_DIR.iterdir() if path.is_dir())
    if len(pack_dirs) != 1:
        return None

    only_pack = pack_dirs[0]
    if _count_images(only_pack / "memes") != 0:
        return None
    return only_pack.name


def _apply_post_install_policy(
    new_pack_id: str,
    previous_single_empty_pack_id: str | None,
    set_as_default: bool,
    operation_guard: PackOperationGuard | None = None,
) -> dict:
    """安装完成后执行策略：必要时移除空包并设置默认包。"""
    result = {
        "removed_empty_pack_id": None,
        "forced_set_default": False,
    }

    normalized_new_pack_id = str(new_pack_id or "").strip()
    if not normalized_new_pack_id:
        return result

    previous_empty_pack_id = str(previous_single_empty_pack_id or "").strip()

    should_cleanup_previous_empty = bool(
        previous_empty_pack_id
        and previous_empty_pack_id != normalized_new_pack_id
        and (PACKS_DIR / previous_empty_pack_id).is_dir()
    )

    if should_cleanup_previous_empty:
        uninstall_pack(previous_empty_pack_id, operation_guard=operation_guard)
        result["removed_empty_pack_id"] = previous_empty_pack_id

    should_set_default = bool(set_as_default) or bool(previous_empty_pack_id)
    if should_set_default and (PACKS_DIR / normalized_new_pack_id).is_dir():
        set_default_pack(normalized_new_pack_id)
        result["forced_set_default"] = not bool(set_as_default)

    return result


def _create_empty_pack(pack_id: str) -> str:
    pack_id = str(pack_id or "").strip() or DEFAULT_PACK_ID
    pack_dir = PACKS_DIR / pack_id
    memes_dir = pack_dir / "memes"
    empty_category = "empty"
    category_descriptions = {
        empty_category: str(
            DEFAULT_CATEGORY_DESCRIPTIONS.get(empty_category) or "请添加描述"
        )
    }

    pack_dir.mkdir(parents=True, exist_ok=True)
    (memes_dir / empty_category).mkdir(parents=True, exist_ok=True)
    _save_json(pack_dir / "memes_data.json", category_descriptions)
    _save_json(
        pack_dir / "manifest.json",
        {
            "schema_version": 1,
            "id": pack_id,
            "name": f"Runtime Empty Pack ({pack_id})",
            "version": "1.0.0",
            "description": "Auto-created empty meme pack",
            "tags": ["runtime", "auto-created"],
            "categories": {
                empty_category: {
                    "description": category_descriptions[empty_category],
                }
            },
        },
    )

    return pack_id


def list_installed_packs() -> list[dict]:
    registry = _load_registry()
    default_pack_id = _current_default_pack_id()
    packs = []
    for item in registry["installed_packs"]:
        pack_id = str(item.get("id") or "").strip()
        if not pack_id:
            continue
        pack_dir = PACKS_DIR / pack_id
        if not pack_dir.is_dir():
            continue
        manifest = _load_manifest(pack_id)
        memes_dir = pack_dir / "memes"
        image_count = _count_images(memes_dir)
        has_semantic_metadata = (pack_dir / "semantic_metadata.json").is_file()
        is_legacy_pack = _is_legacy_pack(pack_id, manifest)
        pack_data = {
            "id": pack_id,
            "name": str(item.get("name") or manifest.get("name") or pack_id),
            "version": str(item.get("version") or manifest.get("version") or "0.0.0"),
            "enabled": bool(item.get("enabled", True)),
            "installed_at": item.get("installed_at"),
            "is_default": pack_id == default_pack_id,
            "image_count": image_count,
            "category_count": (
                len([d for d in memes_dir.iterdir() if d.is_dir()])
                if memes_dir.is_dir()
                else 0
            ),
            "has_semantic_metadata": has_semantic_metadata,
            "is_legacy_pack": is_legacy_pack,
            "supports_vector_rebuild": bool(
                has_semantic_metadata and not is_legacy_pack
            ),
        }
        pack_data.update(get_pack_semantic_summary(pack_dir, image_count))
        packs.append(pack_data)
    return packs


def get_pack_detail(pack_id: str) -> dict:
    pack_id = str(pack_id or "").strip()
    if not pack_id:
        raise ValueError("pack_id 不能为空")

    pack_dir = PACKS_DIR / pack_id
    if not pack_dir.is_dir():
        raise FileNotFoundError(f"表情包 {pack_id} 不存在")

    manifest = _load_manifest(pack_id)
    memes_dir = pack_dir / "memes"
    categories = []
    if memes_dir.is_dir():
        for category_dir in sorted(memes_dir.iterdir(), key=lambda x: x.name):
            if category_dir.is_dir():
                categories.append(
                    {
                        "name": category_dir.name,
                        "image_count": len(
                            [
                                p
                                for p in category_dir.iterdir()
                                if p.is_file()
                                and p.suffix.lower()
                                in {".png", ".jpg", ".jpeg", ".gif", ".webp"}
                            ]
                        ),
                    }
                )

    result = {
        "id": pack_id,
        "manifest": manifest,
        "pack_dir": str(pack_dir),
        "categories": categories,
        "total_images": _count_images(memes_dir),
        "has_semantic_metadata": (pack_dir / "semantic_metadata.json").is_file(),
    }
    result.update(get_pack_semantic_summary(pack_dir, result["total_images"]))
    return result


def set_default_pack(pack_id: str) -> dict:
    pack_id = str(pack_id or "").strip()
    if not pack_id:
        raise ValueError("pack_id 不能为空")
    if not (PACKS_DIR / pack_id).is_dir():
        raise FileNotFoundError(f"表情包 {pack_id} 不存在")

    selection_rules = _load_selection_rules()
    rules = [
        rule
        for rule in selection_rules.get("rules", [])
        if str(rule.get("scope") or "") != "default"
    ]
    rules.append({"id": "default", "scope": "default", "pack_id": pack_id})
    selection_rules["rules"] = rules
    _save_selection_rules(selection_rules)
    return {"pack_id": pack_id}


def _archive_root_candidates(extract_root: Path) -> list[Path]:
    candidates = [extract_root]
    candidates.extend(
        child
        for child in extract_root.iterdir()
        if child.is_dir() and child.name != "__MACOSX"
    )
    return candidates


def _find_manifest_root(extract_root: Path) -> Path:
    candidates = [
        root
        for root in _archive_root_candidates(extract_root)
        if (root / "manifest.json").is_file()
    ]

    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise ValueError("压缩包中存在多个 manifest 根目录")
    raise ValueError("压缩包中未找到 manifest.json")


def _legacy_root_has_categories(root: Path) -> bool:
    metadata_path = root / "memes_data.json"
    if not metadata_path.is_file():
        return False
    for child in root.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        if any(
            file_path.is_file() and file_path.suffix.lower() in IMAGE_EXTENSIONS
            for file_path in child.rglob("*")
        ):
            return True
    return False


def _find_import_root(extract_root: Path) -> tuple[Path, str]:
    try:
        pack_root = _find_manifest_root(extract_root)
        transfer_path = pack_root / PACK_TRANSFER_MANIFEST
        detected_format = "v2" if transfer_path.is_file() else "v1"
        return pack_root, detected_format
    except ValueError as manifest_error:
        legacy_candidates = [
            root
            for root in _archive_root_candidates(extract_root)
            if (root / "memes").is_dir() or _legacy_root_has_categories(root)
        ]
        if len(legacy_candidates) == 1:
            return legacy_candidates[0], "legacy"
        if len(legacy_candidates) > 1:
            raise ValueError("压缩包中存在多个可能的旧版表情包目录") from manifest_error
        raise ValueError(
            "无法识别压缩包：新版包需要 manifest.json，旧版包需要 memes 目录"
        ) from manifest_error


def _legacy_pack_id(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9._-]+", "-", str(value or "").lower())
    normalized = normalized.strip("._-")[:64]
    if len(normalized) < 2:
        normalized = "legacy-pack"
    return validate_pack_id(normalized, "旧版表情包")


def _legacy_descriptions(root: Path, memes_dir: Path) -> dict[str, str]:
    raw = _load_json(root / "memes_data.json", {})
    if not isinstance(raw, dict):
        raw = {}
    descriptions = {
        str(category): str(description or "请添加描述")
        for category, description in raw.items()
        if str(category).strip()
    }
    if memes_dir.is_dir():
        for category_dir in memes_dir.iterdir():
            if category_dir.is_dir():
                descriptions.setdefault(category_dir.name, "请添加描述")
    return descriptions


def _copy_legacy_pack(
    legacy_root: Path,
    target_root: Path,
    suggested_pack_id: str,
) -> dict:
    pack_id = _legacy_pack_id(suggested_pack_id)
    source_memes = legacy_root / "memes"
    direct_category_layout = not source_memes.is_dir()
    if direct_category_layout:
        source_memes = legacy_root

    target_memes = target_root / "memes"
    target_memes.mkdir(parents=True, exist_ok=True)
    for source_path in sorted(source_memes.rglob("*")):
        if (
            not source_path.is_file()
            or source_path.suffix.lower() not in IMAGE_EXTENSIONS
        ):
            continue
        relative = source_path.relative_to(source_memes)
        if not relative.parts or relative.parts[0].startswith("."):
            continue
        if len(relative.parts) == 1:
            relative = Path("default") / relative
        target_path = target_memes / relative
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)

    descriptions = _legacy_descriptions(legacy_root, target_memes)
    if not descriptions:
        descriptions = {
            path.name: "请添加描述" for path in target_memes.iterdir() if path.is_dir()
        }
    if not descriptions:
        raise ValueError("旧版压缩包中没有可导入的表情图片")

    manifest = {
        "schema_version": 1,
        "id": pack_id,
        "name": f"旧版导入包 ({pack_id})",
        "version": "1.0.0",
        "description": "由旧版无语义化表情包自动转换",
        "tags": ["legacy", "converted"],
        "categories": {
            category: {"description": description}
            for category, description in sorted(descriptions.items())
        },
    }
    _save_json(target_root / "manifest.json", manifest)
    _save_json(target_root / "memes_data.json", descriptions)
    return manifest


def _extract_zip_safely(
    zip_path: Path, target_dir: Path, block_executable_scripts: bool = True
) -> None:
    archive_size = zip_path.stat().st_size
    if archive_size > MAX_ARCHIVE_COMPRESSED_BYTES:
        raise ValueError("压缩包体积超过 1 GB，无法安全导入")
    with zipfile.ZipFile(zip_path, "r") as zip_file:
        members = zip_file.infolist()
        if len(members) > MAX_ARCHIVE_FILE_COUNT:
            raise ValueError("压缩包文件数量过多")
        total_uncompressed = sum(max(0, member.file_size) for member in members)
        if total_uncompressed > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise ValueError("压缩包解压后的体积过大")
        # 导入时会同时保留解压目录、规范化目录和向量校验副本。提前预留
        # 三倍解压体积以及固定安全余量，避免把 AstrBot 所在磁盘写满。
        _require_free_space(
            target_dir,
            total_uncompressed * 3 + MIN_FREE_SPACE_RESERVE_BYTES,
            "解压表情包",
        )
        for member in members:
            member_path = Path(member.filename)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError("压缩包包含非法路径")
            if member.filename.endswith("/"):
                continue
            if member.file_size > MAX_ARCHIVE_SINGLE_FILE_BYTES:
                raise ValueError(f"压缩包中的单个文件过大: {member_path.name}")
            json_limit = ARCHIVE_JSON_SIZE_LIMITS.get(member_path.name)
            if json_limit is not None and member.file_size > json_limit:
                raise ValueError(f"压缩包中的 {member_path.name} 体积异常")
            suffix = member_path.suffix.lower()
            if (
                block_executable_scripts
                and suffix
                and suffix in {".exe", ".bat", ".cmd", ".ps1", ".sh"}
            ):
                raise ValueError("压缩包包含不允许的可执行脚本文件")
        zip_file.extractall(target_dir)


def _load_transfer_info(pack_root: Path, detected_format: str) -> dict:
    if detected_format != "v2":
        return {
            "format": PACK_TRANSFER_FORMAT,
            "format_version": 1,
            "export_mode": "share",
            "features": {
                "semantic_metadata": (pack_root / "semantic_metadata.json").is_file(),
                "vectors": False,
            },
        }
    transfer_info = _load_json(pack_root / PACK_TRANSFER_MANIFEST, {})
    return validate_transfer_manifest(transfer_info)


def _prepare_import_pack(
    pack_root: Path,
    detected_format: str,
    target_root: Path,
    suggested_pack_id: str,
) -> tuple[dict, dict]:
    if detected_format == "legacy":
        manifest = _copy_legacy_pack(pack_root, target_root, suggested_pack_id)
        transfer_info = {
            "format": PACK_TRANSFER_FORMAT,
            "format_version": 0,
            "export_mode": "share",
            "features": {"semantic_metadata": False, "vectors": False},
        }
        return manifest, transfer_info

    transfer_info = _load_transfer_info(pack_root, detected_format)

    def ignore_transfer_files(directory: str, names: list[str]) -> set[str]:
        if Path(directory).resolve() != pack_root.resolve():
            return set()
        return {
            name for name in names if name in {PACK_TRANSFER_MANIFEST, "semantic_index"}
        }

    shutil.copytree(pack_root, target_root, ignore=ignore_transfer_files)
    (target_root / LEGACY_METADATA_BACKUP_NAME).unlink(missing_ok=True)
    manifest = _load_json(target_root / "manifest.json", {})
    if not isinstance(manifest, dict):
        raise ValueError("manifest.json 格式无效")
    return validate_pack_manifest(manifest), transfer_info


def inspect_pack_archive(zip_path: Path, suggested_pack_id: str | None = None) -> dict:
    """只读检查导入包，返回 WebUI 确认导入所需的兼容性摘要。"""
    if not zip_path.is_file():
        raise FileNotFoundError("压缩包不存在")
    if not zipfile.is_zipfile(zip_path):
        raise ValueError("文件不是有效的 zip 压缩包")

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=TEMP_DIR, prefix="pack_inspect_") as tmp_dir:
        workspace = Path(tmp_dir)
        extract_root = workspace / "extract"
        extract_root.mkdir()
        _extract_zip_safely(zip_path, extract_root)
        pack_root, detected_format = _find_import_root(extract_root)
        prepared_root = workspace / "prepared"
        manifest, transfer_info = _prepare_import_pack(
            pack_root,
            detected_format,
            prepared_root,
            suggested_pack_id or zip_path.stem,
        )
        validate_pack_directory(prepared_root, context="待导入表情包")

        image_count = _count_images(prepared_root / "memes")
        categories = manifest.get("categories", {})
        semantic_path = prepared_root / "semantic_metadata.json"
        semantic_data = load_metadata(prepared_root) if semantic_path.is_file() else {}
        if semantic_data.get("metadata_read_only"):
            raise ValueError(str(semantic_data.get("metadata_error") or "语义文件无效"))
        semantic_images = (
            semantic_data.get("images", {}) if isinstance(semantic_data, dict) else {}
        )
        semantic_done = sum(
            1
            for item in semantic_images.values()
            if isinstance(item, dict) and item.get("caption_status") == "done"
        )
        declared_vectors = bool(transfer_info.get("features", {}).get("vectors", False))
        _, bundled_index_path = _index_bundle_details(pack_root / "semantic_index")
        vector_files_present = bundled_index_path is not None
        warnings = []
        if detected_format == "legacy":
            warnings.append("已识别为旧版无语义化压缩包，导入时会自动转换为新版结构。")
        if declared_vectors and not vector_files_present:
            warnings.append(
                "压缩包声明包含向量，但缺少完整索引文件；将按无向量包导入。"
            )

        return {
            "detected_format": detected_format,
            "format_version": int(transfer_info.get("format_version", 0) or 0),
            "export_mode": str(transfer_info.get("export_mode") or "share"),
            "pack_id": str(manifest.get("id") or ""),
            "name": str(manifest.get("name") or manifest.get("id") or ""),
            "version": str(manifest.get("version") or "1.0.0"),
            "image_count": image_count,
            "category_count": len(categories) if isinstance(categories, dict) else 0,
            "semantic_metadata": semantic_path.is_file(),
            "semantic_done": semantic_done,
            "vectors_declared": declared_vectors,
            "vectors_present": vector_files_present,
            "warnings": warnings,
        }


def get_pack_export_capabilities(pack_id: str) -> dict:
    pack_id = validate_pack_id(pack_id, "表情包")
    pack_dir = PACKS_DIR / pack_id
    if not pack_dir.is_dir():
        raise FileNotFoundError(f"表情包 {pack_id} 不存在")
    metadata = load_metadata(pack_dir)
    semantic_metadata = (pack_dir / "semantic_metadata.json").is_file()
    try:
        vector_backup_available = bool(
            semantic_metadata
            and index_is_ready(PLUGIN_DATA_DIR, pack_id, metadata=metadata)
        )
    except Exception:
        vector_backup_available = False
    index_manifest = load_index_manifest(PLUGIN_DATA_DIR, pack_id)
    return {
        "pack_id": pack_id,
        "image_count": _count_images(pack_dir / "memes"),
        "semantic_metadata": semantic_metadata,
        "vector_backup_available": vector_backup_available,
        "embedding_provider_id": str(index_manifest.get("embedding_provider_id") or ""),
        "embedding_model": str(index_manifest.get("embedding_model") or ""),
        "embedding_dimension": _safe_nonnegative_int(
            index_manifest.get("embedding_dimension", 0)
        )
        or 0,
    }


def _allocate_pack_id(base_pack_id: str) -> str:
    base = str(base_pack_id or "").strip()
    if not base:
        raise ValueError("pack_id 不能为空")
    if not (PACKS_DIR / base).exists():
        return base
    index = 2
    while True:
        candidate = f"{base}-{index}"
        if not (PACKS_DIR / candidate).exists():
            return candidate
        index += 1


def import_pack_archive(
    zip_path: Path,
    overwrite: bool = False,
    set_as_default: bool = False,
    operation_guard: PackOperationGuard | None = None,
    suggested_pack_id: str | None = None,
    embedding_provider_id: str = "",
    embedding_model: str = "",
    embedding_dimension: int = 0,
    preserve_existing_manual: bool = True,
) -> dict:
    if not zip_path.is_file():
        raise FileNotFoundError("压缩包不存在")
    if not zipfile.is_zipfile(zip_path):
        raise ValueError("文件不是有效的 zip 压缩包")

    previous_single_empty_pack_id = _snapshot_single_empty_pack()
    detected_format = ""
    transfer_info: dict = {}
    vectors_restored = False
    vector_warning = ""

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=TEMP_DIR, prefix="pack_import_") as tmp_dir:
        workspace = Path(tmp_dir)
        extract_root = workspace / "extract"
        extract_root.mkdir()
        _extract_zip_safely(zip_path, extract_root)
        pack_root, detected_format = _find_import_root(extract_root)
        prepared_pack_dir = workspace / "prepared"
        normalized_manifest, transfer_info = _prepare_import_pack(
            pack_root,
            detected_format,
            prepared_pack_dir,
            suggested_pack_id or zip_path.stem,
        )
        original_pack_id = str(normalized_manifest.get("id") or "").strip()
        pack_id = original_pack_id if overwrite else _allocate_pack_id(original_pack_id)
        if pack_id != original_pack_id:
            normalized_manifest["id"] = pack_id
            current_name = str(normalized_manifest.get("name") or original_pack_id)
            normalized_manifest["name"] = f"{current_name} ({pack_id})"
        _save_json(prepared_pack_dir / "manifest.json", normalized_manifest)

        compatibility_metadata = prepared_pack_dir / "memes_data.json"
        if not compatibility_metadata.is_file():
            descriptions = {
                str(category): str(
                    metadata.get("description") or "请添加描述"
                    if isinstance(metadata, dict)
                    else metadata or "请添加描述"
                )
                for category, metadata in normalized_manifest.get(
                    "categories", {}
                ).items()
            }
            _save_json(compatibility_metadata, descriptions)
        _require_regular_tree(prepared_pack_dir, "导入表情包")
        validate_pack_directory(prepared_pack_dir, context=f"导入包 {pack_id}")

        semantic_file = prepared_pack_dir / "semantic_metadata.json"
        declared_vectors = bool(transfer_info.get("features", {}).get("vectors", False))
        source_index_dir = pack_root / "semantic_index"
        source_index_manifest, bundled_index_path = _index_bundle_details(
            source_index_dir
        )
        vector_files_present = bundled_index_path is not None
        restore_candidate = bool(
            transfer_info.get("export_mode") == "backup"
            and declared_vectors
            and vector_files_present
            and semantic_file.is_file()
        )
        expected_dimension = _safe_nonnegative_int(embedding_dimension) or 0
        expected_signature_available = bool(
            str(embedding_provider_id or "").strip() and expected_dimension > 0
        )
        archive_dimension = _safe_nonnegative_int(
            source_index_manifest.get("embedding_dimension", 0)
        )
        signature_matches = bool(
            expected_signature_available
            and str(source_index_manifest.get("embedding_provider_id") or "")
            == str(embedding_provider_id or "")
            and str(source_index_manifest.get("embedding_model") or "")
            == str(embedding_model or "")
            and archive_dimension == expected_dimension
        )
        wants_vector_restore = bool(restore_candidate and signature_matches)
        if restore_candidate and not expected_signature_available:
            vector_warning = (
                "当前未提供本机向量模型信息，已保留语义描述并放弃压缩包向量。"
            )
        elif restore_candidate and not signature_matches:
            vector_warning = (
                "压缩包向量模型或维度与本机不一致，已保留语义描述并等待重建。"
            )

        if semantic_file.is_file():
            # 无论新旧包都先按图片内容复核路径和哈希，避免错误记录指向其他文件。
            imported_data = import_metadata_file(semantic_file)
            if wants_vector_restore:
                reconciled = reconcile_metadata(
                    prepared_pack_dir, external_data=imported_data
                )
                reconciled["pack_id"] = pack_id
                save_metadata(prepared_pack_dir, reconciled)
            else:
                portable_data = reset_local_embedding_state(
                    imported_data, prepared_pack_dir
                )
                reconciled = reconcile_metadata(
                    prepared_pack_dir, external_data=portable_data
                )
                reconciled = reset_local_embedding_state(reconciled)
                reconciled["pack_id"] = pack_id
                save_metadata(prepared_pack_dir, reconciled)

        target_pack_dir = PACKS_DIR / pack_id
        manual_data_preserved = False
        existing_metadata = None
        if overwrite and target_pack_dir.is_dir():
            existing_metadata_path = target_pack_dir / "semantic_metadata.json"
            if existing_metadata_path.is_file():
                existing_metadata = load_metadata(target_pack_dir)
                if existing_metadata.get("metadata_read_only"):
                    raise ValueError(
                        "现有图包语义文件无法安全读取，拒绝覆盖以保护人工内容："
                        f"{existing_metadata.get('metadata_error') or '未知错误'}"
                    )
        if preserve_existing_manual and existing_metadata is not None:
            manual_data_preserved = any(
                isinstance(item, dict)
                and (
                    item.get("manual_override")
                    or item.get("provenance") in {"manual", "mixed"}
                )
                for item in existing_metadata.get("images", {}).values()
            )
            if manual_data_preserved:
                merged = reconcile_metadata(
                    prepared_pack_dir,
                    external_data=existing_metadata,
                    prefer_external_manual=True,
                )
                merged["pack_id"] = pack_id
                save_metadata(prepared_pack_dir, reset_local_embedding_state(merged))
                wants_vector_restore = False
                vector_warning = (
                    "已保留现有图包的人工语义；为避免错配，压缩包向量已放弃并等待重建。"
                )

        prepared_index_dir = workspace / "prepared_index"
        if wants_vector_restore:
            shutil.copytree(source_index_dir, prepared_index_dir)
            index_manifest_path = prepared_index_dir / "index_manifest.json"
            index_manifest = _load_json(index_manifest_path, {})
            if isinstance(index_manifest, dict):
                index_manifest["pack_id"] = pack_id
                _save_json(index_manifest_path, index_manifest)

            validation_runtime = workspace / "vector_validation"
            validation_index_dir = validation_runtime / "semantic_indexes" / pack_id
            validation_index_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(prepared_index_dir, validation_index_dir)
            try:
                vectors_restored = index_is_ready(
                    validation_runtime,
                    pack_id,
                    metadata=load_metadata(prepared_pack_dir),
                    embedding_provider_id=str(embedding_provider_id or ""),
                    embedding_model=str(embedding_model or ""),
                    embedding_dimension=expected_dimension,
                )
            except Exception:
                vectors_restored = False
            if not vectors_restored:
                vector_warning = "向量索引校验未通过，已保留语义描述并改为待重建状态。"
                portable = reset_local_embedding_state(load_metadata(prepared_pack_dir))
                portable["pack_id"] = pack_id
                save_metadata(prepared_pack_dir, portable)
        elif declared_vectors and not vector_warning:
            vector_warning = "压缩包缺少完整向量索引，已按无向量包导入。"

        target_index_dir = PLUGIN_DATA_DIR / "semantic_indexes" / pack_id
        old_pack_dir = workspace / "replaced_pack"
        old_index_dir = workspace / "replaced_index"
        target_existed = target_pack_dir.exists()
        if target_existed and not overwrite:
            raise FileExistsError(f"表情包 {pack_id} 已存在，请重新检查后再导入")

        # Capture every rollback source before the first runtime mutation. A failed
        # snapshot must leave the installed pack and index exactly where they were.
        registry_snapshot = (
            REGISTRY_PATH.read_bytes() if REGISTRY_PATH.is_file() else None
        )
        rules_snapshot = (
            SELECTION_RULES_PATH.read_bytes()
            if SELECTION_RULES_PATH.is_file()
            else None
        )
        previous_empty_snapshot = workspace / "previous_empty_pack"
        previous_empty_index_snapshot = workspace / "previous_empty_index"
        previous_empty_id = str(previous_single_empty_pack_id or "").strip()
        previous_empty_dir = (
            PACKS_DIR / previous_empty_id if previous_empty_id else None
        )
        previous_empty_index = (
            PLUGIN_DATA_DIR / "semantic_indexes" / previous_empty_id
            if previous_empty_id
            else None
        )
        if (
            previous_empty_dir is not None
            and previous_empty_id != pack_id
            and previous_empty_dir.is_dir()
        ):
            shutil.copytree(previous_empty_dir, previous_empty_snapshot)
        if previous_empty_index is not None and previous_empty_index.is_dir():
            shutil.copytree(previous_empty_index, previous_empty_index_snapshot)

        old_pack_moved = False
        old_index_moved = False
        new_pack_installed = False
        try:
            if target_existed and overwrite:
                if operation_guard:
                    operation_guard(pack_id, "覆盖安装资源包")
                shutil.move(str(target_pack_dir), str(old_pack_dir))
                old_pack_moved = True
            if target_index_dir.exists():
                target_index_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target_index_dir), str(old_index_dir))
                old_index_moved = True

            PACKS_DIR.mkdir(parents=True, exist_ok=True)
            prepared_pack_dir.rename(target_pack_dir)
            new_pack_installed = True
            if vectors_restored:
                target_index_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(prepared_index_dir, target_index_dir)

            registry = _load_registry()
            installed = registry["installed_packs"]
            manifest = _load_manifest(pack_id)
            registry_entry_replaced = False
            for item in installed:
                if str(item.get("id") or "") != pack_id:
                    continue
                item.update(
                    {
                        "id": pack_id,
                        "name": str(manifest.get("name") or pack_id),
                        "version": str(manifest.get("version") or "1.0.0"),
                        "enabled": True,
                        "installed_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                registry_entry_replaced = True
                break

            if not registry_entry_replaced:
                installed.append(
                    {
                        "id": pack_id,
                        "name": str(manifest.get("name") or pack_id),
                        "version": str(manifest.get("version") or "1.0.0"),
                        "enabled": True,
                        "installed_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
            _save_registry(registry)

            post_install = _apply_post_install_policy(
                new_pack_id=pack_id,
                previous_single_empty_pack_id=previous_single_empty_pack_id,
                set_as_default=set_as_default,
                operation_guard=operation_guard,
            )
        except Exception:
            if new_pack_installed:
                shutil.rmtree(target_pack_dir, ignore_errors=True)
            if vectors_restored or old_index_moved:
                shutil.rmtree(target_index_dir, ignore_errors=True)
            if old_pack_moved and old_pack_dir.exists():
                shutil.move(str(old_pack_dir), str(target_pack_dir))
            if old_index_moved and old_index_dir.exists():
                target_index_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_index_dir), str(target_index_dir))
            if previous_empty_dir is not None and previous_empty_snapshot.is_dir():
                if not previous_empty_dir.exists():
                    shutil.copytree(previous_empty_snapshot, previous_empty_dir)
            if (
                previous_empty_index is not None
                and previous_empty_index_snapshot.is_dir()
                and not previous_empty_index.exists()
            ):
                previous_empty_index.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(previous_empty_index_snapshot, previous_empty_index)
            _restore_file_snapshot(REGISTRY_PATH, registry_snapshot)
            _restore_file_snapshot(SELECTION_RULES_PATH, rules_snapshot)
            raise

        return {
            "pack_id": pack_id,
            "name": str(manifest.get("name") or pack_id),
            "version": str(manifest.get("version") or "1.0.0"),
            "overwritten": bool(overwrite and target_existed),
            "detected_format": detected_format,
            "export_mode": str(transfer_info.get("export_mode") or "share"),
            "semantic_metadata": (target_pack_dir / "semantic_metadata.json").is_file(),
            "vectors_restored": vectors_restored,
            "vector_warning": vector_warning or None,
            "manual_data_preserved": manual_data_preserved,
            **post_install,
        }


def export_pack_archive(
    pack_id: str,
    output_dir: str | None = None,
    *,
    include_semantic: bool = True,
    export_mode: str = "share",
    operation_guard: PackOperationGuard | None = None,
) -> dict:
    pack_id = validate_pack_id(pack_id, "表情包")
    export_mode = str(export_mode or "share").strip().lower()
    if export_mode not in PACK_EXPORT_MODES:
        raise ValueError("导出类型仅支持 share 或 backup")

    pack_dir = PACKS_DIR / pack_id
    if not pack_dir.is_dir():
        raise FileNotFoundError(f"表情包 {pack_id} 不存在")
    _require_regular_tree(pack_dir, "导出表情包")
    if operation_guard:
        operation_guard(pack_id, "导出资源包")

    target_dir = Path(output_dir).expanduser().resolve() if output_dir else BACKUP_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive_base = target_dir / f"{pack_id}_{export_mode}_{timestamp}"
    if export_mode == "backup":
        _require_regular_tree(
            PLUGIN_DATA_DIR / "semantic_indexes" / pack_id,
            "导出向量索引",
        )
        capabilities = get_pack_export_capabilities(pack_id)
        if not capabilities["vector_backup_available"]:
            raise ValueError("当前表情包没有可用的完整向量，暂时不能导出带向量备份")

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    source_size = _directory_size(pack_dir)
    if export_mode == "backup":
        source_size += _directory_size(PLUGIN_DATA_DIR / "semantic_indexes" / pack_id)
    temp_device = TEMP_DIR.stat().st_dev
    target_device = target_dir.stat().st_dev
    if temp_device == target_device:
        _require_free_space(
            TEMP_DIR,
            source_size * 2 + MIN_FREE_SPACE_RESERVE_BYTES,
            "导出表情包",
        )
    else:
        _require_free_space(
            TEMP_DIR,
            source_size + MIN_FREE_SPACE_RESERVE_BYTES,
            "创建导出快照",
        )
        _require_free_space(
            target_dir,
            source_size + MIN_FREE_SPACE_RESERVE_BYTES,
            "写入导出文件",
        )
    with tempfile.TemporaryDirectory(dir=TEMP_DIR, prefix="pack_export_") as tmp_dir:
        staging = Path(tmp_dir) / pack_id
        shutil.copytree(pack_dir, staging)
        semantic_file = staging / "semantic_metadata.json"
        vectors_included = False
        if export_mode == "share" and include_semantic:
            (staging / LEGACY_METADATA_BACKUP_NAME).unlink(missing_ok=True)
            if semantic_file.exists():
                portable = reset_local_embedding_state(
                    import_metadata_file(semantic_file), staging
                )
                portable = reconcile_metadata(staging, external_data=portable)
                save_metadata(staging, reset_local_embedding_state(portable))
                (staging / LEGACY_METADATA_BACKUP_NAME).unlink(missing_ok=True)
        elif export_mode == "share" and semantic_file.exists():
            semantic_file.unlink()
        elif export_mode == "backup":
            source_index_dir = PLUGIN_DATA_DIR / "semantic_indexes" / pack_id
            shutil.copytree(source_index_dir, staging / "semantic_index")
            vectors_included = True

        raw_pack_schema_version = _load_manifest(pack_id).get("schema_version", 1)
        try:
            pack_schema_version = int(raw_pack_schema_version or 1)
        except (TypeError, ValueError):
            pack_schema_version = 1
        transfer_info = {
            "format": PACK_TRANSFER_FORMAT,
            "format_version": PACK_TRANSFER_VERSION,
            "export_mode": export_mode,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "pack": {
                "id": pack_id,
                "schema_version": pack_schema_version,
            },
            "features": {
                "semantic_metadata": semantic_file.is_file(),
                "vectors": vectors_included,
            },
            "compatibility": {
                "manifest_and_memes_at_archive_root": True,
                "legacy_nonsemantic_import_supported": True,
            },
        }
        _save_json(staging / PACK_TRANSFER_MANIFEST, transfer_info)
        archive_path = shutil.make_archive(str(archive_base), "zip", root_dir=staging)

    return {
        "pack_id": pack_id,
        "archive_path": archive_path,
        "archive_filename": Path(archive_path).name,
        "include_semantic": include_semantic,
        "export_mode": export_mode,
        "semantic_metadata_included": bool(
            transfer_info["features"]["semantic_metadata"]
        ),
        "vectors_included": vectors_included,
    }


def uninstall_pack(
    pack_id: str, operation_guard: PackOperationGuard | None = None
) -> dict:
    pack_id = str(pack_id or "").strip()
    if not pack_id:
        raise ValueError("pack_id 不能为空")

    previous_default_pack_id = _current_default_pack_id()

    pack_dir = PACKS_DIR / pack_id
    if not pack_dir.is_dir():
        raise FileNotFoundError(f"表情包 {pack_id} 不存在")
    if operation_guard:
        operation_guard(pack_id, "卸载资源包")

    shutil.rmtree(pack_dir)
    shutil.rmtree(PLUGIN_DATA_DIR / "semantic_indexes" / pack_id, ignore_errors=True)

    registry = _load_registry()
    registry["installed_packs"] = [
        item
        for item in registry["installed_packs"]
        if str(item.get("id") or "") != pack_id
    ]

    existing_pack_ids = (
        {path.name for path in PACKS_DIR.iterdir() if path.is_dir()}
        if PACKS_DIR.is_dir()
        else set()
    )

    if not existing_pack_ids:
        created_pack_id = _create_empty_pack(DEFAULT_PACK_ID)
        existing_pack_ids.add(created_pack_id)
    else:
        created_pack_id = ""

    normalized_installed = []
    seen_pack_ids = set()
    for item in registry["installed_packs"]:
        installed_pack_id = str(item.get("id") or "").strip()
        if (
            not installed_pack_id
            or installed_pack_id not in existing_pack_ids
            or installed_pack_id in seen_pack_ids
        ):
            continue
        normalized_installed.append(item)
        seen_pack_ids.add(installed_pack_id)

    for missing_pack_id in sorted(existing_pack_ids):
        if missing_pack_id in seen_pack_ids:
            continue
        manifest = _load_manifest(missing_pack_id)
        normalized_installed.append(
            {
                "id": missing_pack_id,
                "name": str(manifest.get("name") or missing_pack_id),
                "version": str(manifest.get("version") or "1.0.0"),
                "enabled": True,
                "installed_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    registry["installed_packs"] = normalized_installed
    _save_registry(registry)

    selection_rules = _load_selection_rules()
    next_default_pack_id = ""
    if (
        previous_default_pack_id
        and previous_default_pack_id != pack_id
        and (PACKS_DIR / previous_default_pack_id).is_dir()
    ):
        next_default_pack_id = previous_default_pack_id
    elif normalized_installed:
        next_default_pack_id = str(normalized_installed[0].get("id") or "").strip()
    if not next_default_pack_id:
        next_default_pack_id = DEFAULT_PACK_ID

    normalized_rules = []
    for rule in selection_rules.get("rules", []):
        if not isinstance(rule, dict):
            continue
        scope = str(rule.get("scope") or "").strip().lower()
        rule_pack_id = str(rule.get("pack_id") or "").strip()
        if not rule_pack_id or rule_pack_id == pack_id:
            continue
        if scope == "default":
            continue
        if not (PACKS_DIR / rule_pack_id).is_dir():
            continue
        normalized_rules.append(rule)

    normalized_rules.append(
        {"id": "default", "scope": "default", "pack_id": next_default_pack_id}
    )
    selection_rules["rules"] = normalized_rules
    _save_selection_rules(selection_rules)

    return {
        "pack_id": pack_id,
        "switched_default_to": next_default_pack_id,
        "auto_created_empty_pack": bool(created_pack_id),
        "created_pack_id": created_pack_id or None,
    }


def _download_github_archive(
    repo: str,
    ref: str,
    target_zip_path: Path,
    github_accelerator_url: str = "",
) -> None:
    archive_url = f"https://github.com/{repo}/archive/{ref}.zip"
    response = _http_get_with_optional_acceleration(
        archive_url,
        timeout=30,
        github_accelerator_url=github_accelerator_url,
    )
    if response.status_code != 200:
        raise ValueError(f"下载 GitHub 压缩包失败，状态码: {response.status_code}")
    target_zip_path.parent.mkdir(parents=True, exist_ok=True)
    target_zip_path.write_bytes(response.content)


def fetch_and_cache_community_index(
    index_url: str,
    github_accelerator_url: str = "",
) -> dict:
    index_url = str(index_url or "").strip()
    if not index_url:
        raise ValueError("index_url 不能为空")

    response = _http_get_with_optional_acceleration(
        index_url,
        timeout=20,
        github_accelerator_url=github_accelerator_url,
    )
    if response.status_code != 200:
        raise ValueError(f"下载社区索引失败，状态码: {response.status_code}")

    try:
        index_data = response.json()
    except Exception as exc:
        raise ValueError(f"社区索引不是有效 JSON: {exc}") from exc

    index_data = validate_community_index(index_data)

    cache_payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source_url": index_url,
        "index": index_data,
    }
    _save_json(COMMUNITY_CACHE_PATH, cache_payload)
    return cache_payload


def load_cached_community_index() -> dict:
    cache_data = _load_json(COMMUNITY_CACHE_PATH, {})
    if not isinstance(cache_data, dict) or not cache_data:
        raise FileNotFoundError("社区索引缓存不存在，请先拉取索引")
    index_data = cache_data.get("index")
    if not isinstance(index_data, dict):
        raise ValueError("社区索引缓存格式无效")
    return cache_data


def find_cached_pack_entry(pack_id: str) -> dict:
    pack_id = str(pack_id or "").strip()
    if not pack_id:
        raise ValueError("pack_id 不能为空")

    cache_data = load_cached_community_index()
    packs = cache_data.get("index", {}).get("packs", [])
    if not isinstance(packs, list):
        raise ValueError("社区索引缓存格式无效")

    for entry in packs:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("id") or "").strip() == pack_id:
            return entry
    raise FileNotFoundError(f"缓存索引中未找到 pack_id={pack_id} 的条目")


def install_pack_from_github_source(
    source: dict,
    overwrite: bool = False,
    set_as_default: bool = False,
    operation_guard: PackOperationGuard | None = None,
    github_accelerator_url: str = "",
) -> dict:
    github_source = validate_source_descriptor(source)
    repo = github_source["repo"]
    ref = github_source["ref"]
    subpath = github_source["subpath"]

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=TEMP_DIR, prefix="community_install_"
    ) as tmp_dir:
        tmp_root = Path(tmp_dir)
        remote_zip = tmp_root / "remote.zip"
        _download_github_archive(
            repo,
            ref,
            remote_zip,
            github_accelerator_url=github_accelerator_url,
        )

        extract_dir = tmp_root / "extract"
        extract_dir.mkdir(parents=True, exist_ok=True)
        # 远程仓库可能包含与 pack 无关的脚本文件；这里只做路径安全校验。
        _extract_zip_safely(
            remote_zip,
            extract_dir,
            block_executable_scripts=False,
        )

        roots = [child for child in extract_dir.iterdir() if child.is_dir()]
        if len(roots) != 1:
            raise ValueError("GitHub 压缩包结构异常")

        source_pack_dir = (roots[0] / subpath).resolve()
        try:
            source_pack_dir.relative_to(roots[0].resolve())
        except ValueError as exc:
            raise ValueError("source.subpath 越界") from exc
        if not source_pack_dir.is_dir():
            raise FileNotFoundError("source.subpath 对应目录不存在")
        validate_pack_directory(source_pack_dir, context="GitHub 包目录")

        local_zip = tmp_root / "pack.zip"
        with zipfile.ZipFile(local_zip, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for file_path in source_pack_dir.rglob("*"):
                if file_path.is_dir():
                    continue
                arc_name = file_path.relative_to(source_pack_dir).as_posix()
                zip_file.write(file_path, arcname=arc_name)

        result = import_pack_archive(
            local_zip,
            overwrite=overwrite,
            set_as_default=set_as_default,
            operation_guard=operation_guard,
        )
        result["source"] = github_source
        return result


def install_first_official_pack_from_index(
    index_url: str,
    overwrite: bool = False,
    set_as_default: bool = True,
    operation_guard: PackOperationGuard | None = None,
    github_accelerator_url: str = "",
) -> dict:
    """从社区索引安装首个官方包；若无官方条目则回退索引首项。"""
    cache_loaded = True
    try:
        cache_data = load_cached_community_index()
    except Exception:
        cache_loaded = False
        cache_data = fetch_and_cache_community_index(
            index_url,
            github_accelerator_url=github_accelerator_url,
        )

    packs = cache_data.get("index", {}).get("packs", [])
    if not isinstance(packs, list) or not packs:
        raise ValueError("社区索引中没有可安装的表情包")

    selected_entry = None
    for entry in packs:
        if is_official_pack_entry(entry):
            selected_entry = entry
            break
    if selected_entry is None:
        selected_entry = packs[0]

    source = selected_entry.get("source")
    if not isinstance(source, dict):
        raise ValueError("选中的社区条目缺少 source 信息")

    result = install_pack_from_github_source(
        source=source,
        overwrite=overwrite,
        set_as_default=set_as_default,
        operation_guard=operation_guard,
        github_accelerator_url=github_accelerator_url,
    )
    result["selected_pack_id"] = str(selected_entry.get("id") or "").strip()
    result["selected_pack_name"] = str(
        selected_entry.get("name") or result.get("name") or result.get("pack_id")
    )
    result["selected_is_official"] = is_official_pack_entry(selected_entry)
    result["from_cache"] = cache_loaded
    return result


def get_selection_rules() -> dict:
    selection_rules = _load_selection_rules()
    rules = selection_rules.get("rules", [])
    default_pack_id = _current_default_pack_id()
    return {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "rules": rules,
        "default_pack_id": default_pack_id,
    }


def _validate_and_normalize_rules(
    rules: list[dict], available_pack_ids: set[str] | None = None
) -> list[dict]:
    if not isinstance(rules, list) or not rules:
        raise ValueError("rules 不能为空")

    normalized = []
    default_count = 0
    scope_target_set = set()
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ValueError(f"第 {index + 1} 条规则格式无效")

        rule_id = str(rule.get("id") or "").strip()
        scope = str(rule.get("scope") or "").strip().lower()
        pack_id = str(rule.get("pack_id") or "").strip()
        target = str(rule.get("target") or "").strip()

        if not rule_id:
            raise ValueError(f"第 {index + 1} 条规则缺少 id")
        if scope not in {"persona", "session", "default"}:
            raise ValueError(f"第 {index + 1} 条规则 scope 非法")
        if not pack_id:
            raise ValueError(f"第 {index + 1} 条规则缺少 pack_id")
        pack_exists = (
            pack_id in available_pack_ids
            if available_pack_ids is not None
            else (PACKS_DIR / pack_id).is_dir()
        )
        if not pack_exists:
            raise ValueError(f"第 {index + 1} 条规则引用的 pack 不存在: {pack_id}")

        normalized_rule = {"id": rule_id, "scope": scope, "pack_id": pack_id}
        if scope in {"persona", "session"}:
            if not target:
                raise ValueError(f"第 {index + 1} 条规则缺少 target")
            scope_target_key = (scope, target)
            if scope_target_key in scope_target_set:
                raise ValueError(
                    f"第 {index + 1} 条规则与前序规则冲突: {scope} 目标 {target} 重复"
                )
            scope_target_set.add(scope_target_key)
            normalized_rule["target"] = target
        if scope == "default":
            default_count += 1

        normalized.append(normalized_rule)

    if default_count != 1:
        raise ValueError("必须且仅能存在一条 default 规则")
    if normalized[-1].get("scope") != "default":
        raise ValueError("default 规则必须位于最后")

    rule_ids = [rule["id"] for rule in normalized]
    if len(rule_ids) != len(set(rule_ids)):
        raise ValueError("规则 id 不能重复")

    return normalized


def save_selection_rules(rules: list[dict]) -> dict:
    normalized = _validate_and_normalize_rules(rules)
    payload = {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "rules": normalized,
    }
    _save_selection_rules(payload)
    return payload


def export_runtime_backup(
    output_dir: str | None = None,
    operation_guard: PackOperationGuard | None = None,
) -> dict:
    target_dir = Path(output_dir).expanduser().resolve() if output_dir else BACKUP_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive_base = target_dir / f"runtime_backup_{timestamp}"

    with tempfile.TemporaryDirectory(dir=TEMP_DIR, prefix="runtime_backup_") as tmp_dir:
        tmp_root = Path(tmp_dir)
        snapshot_root = tmp_root / "runtime_backup"
        snapshot_root.mkdir(parents=True, exist_ok=True)

        if REGISTRY_PATH.is_file():
            shutil.copy2(REGISTRY_PATH, snapshot_root / "registry.json")
        if SELECTION_RULES_PATH.is_file():
            shutil.copy2(SELECTION_RULES_PATH, snapshot_root / "selection_rules.json")
        if COMMUNITY_CACHE_PATH.is_file():
            shutil.copy2(COMMUNITY_CACHE_PATH, snapshot_root / "community_cache.json")
        if PACKS_DIR.is_dir():
            if operation_guard:
                for pack_dir in PACKS_DIR.iterdir():
                    if pack_dir.is_dir():
                        operation_guard(pack_dir.name, "导出全量备份")
            shutil.copytree(PACKS_DIR, snapshot_root / "packs", dirs_exist_ok=True)
            for copied_pack_dir in (snapshot_root / "packs").iterdir():
                semantic_file = copied_pack_dir / "semantic_metadata.json"
                if not copied_pack_dir.is_dir() or not semantic_file.is_file():
                    continue
                (copied_pack_dir / LEGACY_METADATA_BACKUP_NAME).unlink(missing_ok=True)
                portable = reset_local_embedding_state(
                    import_metadata_file(semantic_file), copied_pack_dir
                )
                portable = reconcile_metadata(copied_pack_dir, external_data=portable)
                save_metadata(copied_pack_dir, reset_local_embedding_state(portable))
                (copied_pack_dir / LEGACY_METADATA_BACKUP_NAME).unlink(missing_ok=True)

        archive_path = shutil.make_archive(
            str(archive_base), "zip", root_dir=snapshot_root
        )

    return {"archive_path": archive_path}


def _find_backup_root(extract_root: Path) -> Path:
    direct = extract_root / "registry.json"
    if direct.is_file() or (extract_root / "packs").is_dir():
        return extract_root

    candidates = [child for child in extract_root.iterdir() if child.is_dir()]
    for child in candidates:
        if (child / "registry.json").is_file() or (child / "packs").is_dir():
            return child
    raise ValueError("备份包结构无效，缺少 runtime 根目录")


def import_runtime_backup(
    backup_zip_path: Path,
    overwrite: bool = False,
    operation_guard: PackOperationGuard | None = None,
) -> dict:
    """Validate and restore a runtime backup as one rollback-safe transaction."""
    if not backup_zip_path.is_file():
        raise FileNotFoundError("备份压缩包不存在")

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=TEMP_DIR, prefix="runtime_restore_"
    ) as tmp_dir:
        extract_root = Path(tmp_dir)
        _extract_zip_safely(backup_zip_path, extract_root)
        backup_root = _find_backup_root(extract_root)
        _require_regular_tree(backup_root, "恢复全量备份")

        backup_packs_dir = backup_root / "packs"
        backup_registry = backup_root / "registry.json"
        backup_rules = backup_root / "selection_rules.json"
        backup_community = backup_root / "community_cache.json"

        if not backup_packs_dir.is_dir() and not backup_registry.is_file():
            raise ValueError("备份包中没有可恢复的数据")

        prepared_packs = extract_root / "prepared_packs"
        prepared_packs.mkdir()
        prepared_manifests: dict[str, dict] = {}
        if backup_packs_dir.is_dir():
            for source_pack_dir in sorted(backup_packs_dir.iterdir()):
                if not source_pack_dir.is_dir():
                    continue
                pack_id = validate_pack_id(source_pack_dir.name, "备份表情包")
                prepared_pack_dir = prepared_packs / pack_id
                shutil.copytree(source_pack_dir, prepared_pack_dir)
                prepared_manifests[pack_id] = validate_pack_directory(
                    prepared_pack_dir, context=f"备份表情包 {pack_id}"
                )
                semantic_file = prepared_pack_dir / "semantic_metadata.json"
                if semantic_file.is_file():
                    portable = reset_local_embedding_state(
                        import_metadata_file(semantic_file), prepared_pack_dir
                    )
                    reconciled = reconcile_metadata(
                        prepared_pack_dir, external_data=portable
                    )
                    save_metadata(
                        prepared_pack_dir,
                        reset_local_embedding_state(reconciled),
                    )
                    (prepared_pack_dir / LEGACY_METADATA_BACKUP_NAME).unlink(
                        missing_ok=True
                    )

        current_pack_ids = (
            {path.name for path in PACKS_DIR.iterdir() if path.is_dir()}
            if PACKS_DIR.is_dir()
            else set()
        )
        if overwrite and backup_packs_dir.is_dir():
            for current_pack_id in sorted(current_pack_ids):
                current_semantic_file = (
                    PACKS_DIR / current_pack_id / "semantic_metadata.json"
                )
                if not current_semantic_file.is_file():
                    continue
                current_metadata = load_metadata(PACKS_DIR / current_pack_id)
                if current_metadata.get("metadata_read_only"):
                    raise ValueError(
                        "现有图包语义文件无法安全读取，拒绝全量覆盖恢复："
                        f"{current_metadata.get('metadata_error') or '未知错误'}"
                    )
        backup_pack_ids = set(prepared_manifests)
        available_pack_ids = (
            backup_pack_ids
            if overwrite and backup_packs_dir.is_dir()
            else current_pack_ids | backup_pack_ids
        )

        if backup_registry.is_file():
            try:
                registry_data = json.loads(
                    backup_registry.read_text(encoding="utf-8-sig")
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"备份中的 registry.json 无法解析: {exc}") from exc
            if not isinstance(registry_data, dict):
                raise ValueError("备份中的 registry.json 格式无效")
            backup_entries = _normalize_installed_packs(
                registry_data.get("installed_packs", [])
            )
        else:
            backup_entries = []
        current_entries = _load_registry().get("installed_packs", [])
        if overwrite:
            merged_entries = list(backup_entries)
        else:
            # Existing packs are not replaced, so their local registry settings
            # must also win over entries carried by the backup.
            current_entry_ids = {
                str(item.get("id") or "").strip() for item in current_entries
            }
            merged_entries = list(current_entries)
            merged_entries.extend(
                item
                for item in backup_entries
                if str(item.get("id") or "").strip() not in current_entry_ids
            )
        entry_by_id = {
            str(item.get("id") or "").strip(): dict(item)
            for item in merged_entries
            if str(item.get("id") or "").strip() in available_pack_ids
        }
        for pack_id in sorted(available_pack_ids):
            if pack_id in entry_by_id:
                continue
            manifest = prepared_manifests.get(pack_id) or _load_manifest(pack_id)
            entry_by_id[pack_id] = {
                "id": pack_id,
                "name": str(manifest.get("name") or pack_id),
                "version": str(manifest.get("version") or "1.0.0"),
                "enabled": True,
                "installed_at": datetime.now(timezone.utc).isoformat(),
            }
        registry_payload = {
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "installed_packs": list(entry_by_id.values()),
        }

        rules_payload = None
        if backup_rules.is_file():
            try:
                rules_data = json.loads(backup_rules.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"备份中的 selection_rules.json 无法解析: {exc}"
                ) from exc
            if not isinstance(rules_data, dict):
                raise ValueError("备份中的 selection_rules.json 格式无效")
            rules_payload = {
                "schema_version": RUNTIME_SCHEMA_VERSION,
                "rules": _validate_and_normalize_rules(
                    rules_data.get("rules", []), available_pack_ids
                ),
            }
        elif overwrite and backup_packs_dir.is_dir():
            current_rules = _load_selection_rules().get("rules", [])
            rules_payload = {
                "schema_version": RUNTIME_SCHEMA_VERSION,
                "rules": _validate_and_normalize_rules(
                    current_rules, available_pack_ids
                ),
            }

        community_payload = None
        if backup_community.is_file():
            try:
                community_payload = json.loads(
                    backup_community.read_text(encoding="utf-8-sig")
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"备份中的 community_cache.json 无法解析: {exc}"
                ) from exc
            if not isinstance(community_payload, dict):
                raise ValueError("备份中的 community_cache.json 格式无效")

        if operation_guard:
            for pack_id in sorted(current_pack_ids):
                operation_guard(pack_id, "恢复全量备份")

        registry_snapshot = (
            REGISTRY_PATH.read_bytes() if REGISTRY_PATH.is_file() else None
        )
        rules_snapshot = (
            SELECTION_RULES_PATH.read_bytes()
            if SELECTION_RULES_PATH.is_file()
            else None
        )
        community_snapshot = (
            COMMUNITY_CACHE_PATH.read_bytes()
            if COMMUNITY_CACHE_PATH.is_file()
            else None
        )
        semantic_indexes_dir = PLUGIN_DATA_DIR / "semantic_indexes"
        old_packs_dir = extract_root / "old_runtime_packs"
        old_indexes_dir = extract_root / "old_runtime_indexes"
        added_pack_ids: list[str] = []
        moved_index_dirs: dict[str, Path] = {}
        packs_swapped = False
        indexes_swapped = False
        try:
            if overwrite and backup_packs_dir.is_dir():
                if PACKS_DIR.exists():
                    shutil.move(str(PACKS_DIR), str(old_packs_dir))
                shutil.move(str(prepared_packs), str(PACKS_DIR))
                packs_swapped = True
                if semantic_indexes_dir.exists():
                    shutil.move(str(semantic_indexes_dir), str(old_indexes_dir))
                    indexes_swapped = True
                semantic_indexes_dir.mkdir(parents=True, exist_ok=True)
                restored_packs = len(backup_pack_ids)
            else:
                PACKS_DIR.mkdir(parents=True, exist_ok=True)
                restored_packs = 0
                for prepared_pack_dir in sorted(prepared_packs.iterdir()):
                    target_pack_dir = PACKS_DIR / prepared_pack_dir.name
                    if target_pack_dir.exists():
                        continue
                    shutil.move(str(prepared_pack_dir), str(target_pack_dir))
                    added_pack_ids.append(prepared_pack_dir.name)
                    target_index_dir = semantic_indexes_dir / prepared_pack_dir.name
                    if target_index_dir.exists():
                        moved_index = (
                            extract_root / f"old_index_{prepared_pack_dir.name}"
                        )
                        shutil.move(str(target_index_dir), str(moved_index))
                        moved_index_dirs[prepared_pack_dir.name] = moved_index
                    restored_packs += 1

            _save_registry(registry_payload)
            if rules_payload is not None:
                _save_selection_rules(rules_payload)
            if community_payload is not None:
                _save_json(COMMUNITY_CACHE_PATH, community_payload)
        except Exception:
            if packs_swapped:
                shutil.rmtree(PACKS_DIR, ignore_errors=True)
                if old_packs_dir.exists():
                    shutil.move(str(old_packs_dir), str(PACKS_DIR))
            else:
                for pack_id in added_pack_ids:
                    shutil.rmtree(PACKS_DIR / pack_id, ignore_errors=True)
            if indexes_swapped:
                shutil.rmtree(semantic_indexes_dir, ignore_errors=True)
                if old_indexes_dir.exists():
                    shutil.move(str(old_indexes_dir), str(semantic_indexes_dir))
            else:
                for pack_id, moved_index in moved_index_dirs.items():
                    target_index_dir = semantic_indexes_dir / pack_id
                    if moved_index.exists() and not target_index_dir.exists():
                        target_index_dir.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(moved_index), str(target_index_dir))
            _restore_file_snapshot(REGISTRY_PATH, registry_snapshot)
            _restore_file_snapshot(SELECTION_RULES_PATH, rules_snapshot)
            _restore_file_snapshot(COMMUNITY_CACHE_PATH, community_snapshot)
            raise

        return {
            "restored_packs": restored_packs,
            "runtime_dir": str(PLUGIN_DATA_DIR),
        }
