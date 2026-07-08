import json
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests

from ..config import (
    BACKUP_DIR,
    COMMUNITY_CACHE_PATH,
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


def _validate_github_source(source: dict) -> dict:
    if not isinstance(source, dict):
        raise ValueError("source 必须是对象")

    source_type = str(source.get("type") or "").strip().lower()
    if source_type != "github":
        raise ValueError("目前仅支持 github 来源")

    repo = str(source.get("repo") or "").strip()
    ref = str(source.get("ref") or "").strip()
    subpath = str(source.get("subpath") or "").strip().strip("/")
    if not repo or "/" not in repo:
        raise ValueError("source.repo 无效，格式应为 owner/repo")
    if not ref:
        raise ValueError("source.ref 不能为空")
    if not subpath:
        raise ValueError("source.subpath 不能为空")
    if ".." in Path(subpath).parts or "\\" in subpath:
        raise ValueError("source.subpath 非法")

    return {"type": "github", "repo": repo, "ref": ref, "subpath": subpath}


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

    if not isinstance(index_data, dict):
        raise ValueError("社区索引必须是 JSON 对象")
    packs = index_data.get("packs")
    if not isinstance(packs, list):
        raise ValueError("社区索引缺少 packs 数组")

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
    github_source = _validate_github_source(source)
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
        _extract_zip_safely(remote_zip, extract_dir)

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
