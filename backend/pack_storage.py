import json
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests

from .pack_protocol import (
    validate_community_index,
    validate_pack_directory,
    validate_pack_manifest,
    validate_source_descriptor,
)

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


def _load_json(path: Path, default):
    try:
        with path.open(encoding="utf-8-sig") as file_obj:
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


def _extract_zip_safely(
    zip_path: Path, target_dir: Path, block_executable_scripts: bool = True
) -> None:
    with zipfile.ZipFile(zip_path, "r") as zip_file:
        for member in zip_file.infolist():
            member_path = Path(member.filename)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError("压缩包包含非法路径")
            if member.filename.endswith("/"):
                continue
            suffix = member_path.suffix.lower()
            if (
                block_executable_scripts
                and suffix
                and suffix in {".exe", ".bat", ".cmd", ".ps1", ".sh"}
            ):
                raise ValueError("压缩包包含不允许的可执行脚本文件")
        zip_file.extractall(target_dir)


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

        normalized_manifest = validate_pack_manifest(manifest)
        original_pack_id = str(normalized_manifest.get("id") or "").strip()
        pack_id = original_pack_id if overwrite else _allocate_pack_id(original_pack_id)
        if pack_id != original_pack_id:
            normalized_manifest["id"] = pack_id
            current_name = str(normalized_manifest.get("name") or original_pack_id)
            normalized_manifest["name"] = f"{current_name} ({pack_id})"

        target_pack_dir = PACKS_DIR / pack_id
        if target_pack_dir.exists() and overwrite:
            shutil.rmtree(target_pack_dir)

        shutil.copytree(pack_root, target_pack_dir)
    _save_json(target_pack_dir / "manifest.json", normalized_manifest)
    validate_pack_directory(target_pack_dir, context=f"导入包 {pack_id}")

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

    previous_default_pack_id = _current_default_pack_id()

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


def _download_github_archive(repo: str, ref: str, target_zip_path: Path) -> None:
    archive_url = f"https://github.com/{repo}/archive/{ref}.zip"
    response = requests.get(archive_url, timeout=30)
    if response.status_code != 200:
        raise ValueError(f"下载 GitHub 压缩包失败，状态码: {response.status_code}")
    target_zip_path.parent.mkdir(parents=True, exist_ok=True)
    target_zip_path.write_bytes(response.content)


def fetch_and_cache_community_index(index_url: str) -> dict:
    index_url = str(index_url or "").strip()
    if not index_url:
        raise ValueError("index_url 不能为空")

    response = requests.get(index_url, timeout=20)
    if response.status_code != 200:
        raise ValueError(f"下载社区索引失败，状态码: {response.status_code}")

    try:
        index_data = response.json()
    except Exception as exc:
        raise ValueError(f"社区索引不是有效 JSON: {exc}") from exc

    index_data = validate_community_index(index_data)
    packs = index_data.get("packs", [])

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
        _download_github_archive(repo, ref, remote_zip)

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
        )
        result["source"] = github_source
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


def _validate_and_normalize_rules(rules: list[dict]) -> list[dict]:
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
        if not (PACKS_DIR / pack_id).is_dir():
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


def export_runtime_backup(output_dir: str | None = None) -> dict:
    target_dir = Path(output_dir).expanduser().resolve() if output_dir else BACKUP_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

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
            shutil.copytree(PACKS_DIR, snapshot_root / "packs", dirs_exist_ok=True)

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


def import_runtime_backup(backup_zip_path: Path, overwrite: bool = False) -> dict:
    if not backup_zip_path.is_file():
        raise FileNotFoundError("备份压缩包不存在")

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=TEMP_DIR, prefix="runtime_restore_"
    ) as tmp_dir:
        extract_root = Path(tmp_dir)
        _extract_zip_safely(backup_zip_path, extract_root)
        backup_root = _find_backup_root(extract_root)

        backup_packs_dir = backup_root / "packs"
        backup_registry = backup_root / "registry.json"
        backup_rules = backup_root / "selection_rules.json"
        backup_community = backup_root / "community_cache.json"

        if not backup_packs_dir.is_dir() and not backup_registry.is_file():
            raise ValueError("备份包中没有可恢复的数据")

        if overwrite and PACKS_DIR.is_dir():
            shutil.rmtree(PACKS_DIR)
            PACKS_DIR.mkdir(parents=True, exist_ok=True)

        restored_packs = 0
        if backup_packs_dir.is_dir():
            PACKS_DIR.mkdir(parents=True, exist_ok=True)
            for pack_dir in backup_packs_dir.iterdir():
                if not pack_dir.is_dir():
                    continue
                target_pack_dir = PACKS_DIR / pack_dir.name
                if target_pack_dir.exists() and not overwrite:
                    continue
                if target_pack_dir.exists() and overwrite:
                    shutil.rmtree(target_pack_dir)
                shutil.copytree(pack_dir, target_pack_dir)
                restored_packs += 1

        if backup_registry.is_file():
            shutil.copy2(backup_registry, REGISTRY_PATH)
        if backup_rules.is_file():
            rules_data = _load_json(backup_rules, {})
            if not isinstance(rules_data, dict):
                raise ValueError("备份中的 selection_rules.json 格式无效")
            save_selection_rules(rules_data.get("rules", []))
        if backup_community.is_file():
            shutil.copy2(backup_community, COMMUNITY_CACHE_PATH)

    return {
        "restored_packs": restored_packs,
        "runtime_dir": str(PLUGIN_DATA_DIR),
    }
