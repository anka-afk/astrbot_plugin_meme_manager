import json
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from ..config import (
    BACKUP_DIR,
    DEFAULT_PACK_ID,
    LEGACY_MIGRATED_PACK_ID,
    PACKS_DIR,
    REGISTRY_PATH,
    RUNTIME_SCHEMA_VERSION,
    SELECTION_RULES_PATH,
    TEMP_DIR,
)


def _load_json(path: Path, default):
    try:
        with path.open(encoding="utf-8") as file_obj:
            return json.load(file_obj)
    except Exception:
        return default


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file_obj:
        json.dump(data, file_obj, ensure_ascii=False, indent=2)


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
    return manifest if isinstance(manifest, dict) else {}


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
        packs.append(
            {
                "id": pack_id,
                "name": str(item.get("name") or manifest.get("name") or pack_id),
                "version": str(
                    item.get("version") or manifest.get("version") or "0.0.0"
                ),
                "enabled": bool(item.get("enabled", True)),
                "installed_at": item.get("installed_at"),
                "is_default": pack_id == default_pack_id,
                "image_count": _count_images(memes_dir),
                "category_count": (
                    len([d for d in memes_dir.iterdir() if d.is_dir()])
                    if memes_dir.is_dir()
                    else 0
                ),
            }
        )
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

    return {
        "id": pack_id,
        "manifest": manifest,
        "pack_dir": str(pack_dir),
        "categories": categories,
        "total_images": _count_images(memes_dir),
    }


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


def _ensure_manifest_basics(manifest: dict) -> None:
    required_fields = ["id", "name", "version", "categories"]
    for field_name in required_fields:
        if field_name not in manifest:
            raise ValueError(f"manifest 缺少字段: {field_name}")
    if not isinstance(manifest.get("categories"), dict) or not manifest["categories"]:
        raise ValueError("manifest.categories 不能为空")


def _find_manifest_root(extract_root: Path) -> Path:
    direct_manifest = extract_root / "manifest.json"
    if direct_manifest.is_file():
        return extract_root

    candidates = []
    for child in extract_root.iterdir():
        if not child.is_dir():
            continue
        if (child / "manifest.json").is_file():
            candidates.append(child)

    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise ValueError("压缩包中存在多个 manifest 根目录")
    raise ValueError("压缩包中未找到 manifest.json")


def _extract_zip_safely(zip_path: Path, target_dir: Path) -> None:
    with zipfile.ZipFile(zip_path, "r") as zip_file:
        for member in zip_file.infolist():
            member_path = Path(member.filename)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError("压缩包包含非法路径")
            if member.filename.endswith("/"):
                continue
            suffix = member_path.suffix.lower()
            if suffix and suffix in {".exe", ".bat", ".cmd", ".ps1", ".sh"}:
                raise ValueError("压缩包包含不允许的可执行脚本文件")
        zip_file.extractall(target_dir)


def import_pack_archive(
    zip_path: Path,
    overwrite: bool = False,
    set_as_default: bool = False,
) -> dict:
    if not zip_path.is_file():
        raise FileNotFoundError("压缩包不存在")

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=TEMP_DIR, prefix="pack_import_") as tmp_dir:
        extract_root = Path(tmp_dir)
        _extract_zip_safely(zip_path, extract_root)
        pack_root = _find_manifest_root(extract_root)
        manifest = _load_json(pack_root / "manifest.json", {})
        if not isinstance(manifest, dict):
            raise ValueError("manifest.json 格式无效")

        _ensure_manifest_basics(manifest)
        pack_id = str(manifest.get("id") or "").strip()
        if not pack_id:
            raise ValueError("manifest.id 不能为空")

        target_pack_dir = PACKS_DIR / pack_id
        if target_pack_dir.exists() and not overwrite:
            raise FileExistsError(f"表情包 {pack_id} 已存在")

        if target_pack_dir.exists() and overwrite:
            shutil.rmtree(target_pack_dir)

        shutil.copytree(pack_root, target_pack_dir)

    registry = _load_registry()
    installed = registry["installed_packs"]
    manifest = _load_manifest(pack_id)
    replaced = False
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
        replaced = True
        break

    if not replaced:
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

    if set_as_default:
        set_default_pack(pack_id)

    return {
        "pack_id": pack_id,
        "name": str(manifest.get("name") or pack_id),
        "version": str(manifest.get("version") or "1.0.0"),
        "overwritten": overwrite and replaced,
    }


def export_pack_archive(pack_id: str, output_dir: str | None = None) -> dict:
    pack_id = str(pack_id or "").strip()
    if not pack_id:
        raise ValueError("pack_id 不能为空")

    pack_dir = PACKS_DIR / pack_id
    if not pack_dir.is_dir():
        raise FileNotFoundError(f"表情包 {pack_id} 不存在")

    target_dir = Path(output_dir).expanduser().resolve() if output_dir else BACKUP_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive_base = target_dir / f"{pack_id}_{timestamp}"
    archive_path = shutil.make_archive(str(archive_base), "zip", root_dir=pack_dir)

    return {"pack_id": pack_id, "archive_path": archive_path}


def uninstall_pack(pack_id: str) -> dict:
    pack_id = str(pack_id or "").strip()
    if not pack_id:
        raise ValueError("pack_id 不能为空")

    if pack_id == _current_default_pack_id():
        raise ValueError("不能卸载当前默认表情包，请先切换默认包")

    pack_dir = PACKS_DIR / pack_id
    if not pack_dir.is_dir():
        raise FileNotFoundError(f"表情包 {pack_id} 不存在")

    shutil.rmtree(pack_dir)

    registry = _load_registry()
    registry["installed_packs"] = [
        item
        for item in registry["installed_packs"]
        if str(item.get("id") or "") != pack_id
    ]
    _save_registry(registry)

    selection_rules = _load_selection_rules()
    selection_rules["rules"] = [
        rule
        for rule in selection_rules.get("rules", [])
        if str(rule.get("pack_id") or "") != pack_id
    ]
    if not any(
        str(rule.get("scope") or "") == "default" for rule in selection_rules["rules"]
    ):
        fallback = (
            DEFAULT_PACK_ID
            if (PACKS_DIR / DEFAULT_PACK_ID).is_dir()
            else LEGACY_MIGRATED_PACK_ID
        )
        selection_rules["rules"].append(
            {"id": "default", "scope": "default", "pack_id": fallback}
        )
    _save_selection_rules(selection_rules)

    return {"pack_id": pack_id}
