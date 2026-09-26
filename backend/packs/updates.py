"""Reviewable community updates with three-way merging and recoverable backups."""

import hashlib
import json
import re
import secrets
import shutil
import time
import zipfile
from pathlib import Path

from ..semantic.storage import (
    load_metadata,
    reconcile_metadata,
    reset_local_embedding_state,
    save_metadata,
)
from . import storage
from .categories import is_safe_category_name
from .protocol import (
    validate_pack_directory,
    validate_pack_id,
    validate_source_descriptor,
)

ORIGIN_FILE = ".community-origin.json"


def snapshot(pack: Path) -> dict:
    """Read file fingerprints and effective category descriptions.

    Args:
        pack: Validated local or staged pack directory.

    Returns:
        Image hashes, categories, version and a fingerprint of all pack files.

    Raises:
        ValueError: The directory contains links or invalid metadata.
        OSError: A file cannot be read.
    """
    storage._require_regular_tree(pack, "检查更新")
    manifest = validate_pack_directory(pack)
    categories = {
        name: meta["description"] for name, meta in manifest["categories"].items()
    }
    data_path = pack / "memes_data.json"
    if data_path.exists():
        descriptions = json.loads(data_path.read_text(encoding="utf-8-sig"))
        if not isinstance(descriptions, dict):
            raise ValueError("分类描述文件格式无效")
        categories.update(
            {name: str(value or "") for name, value in descriptions.items()}
        )
    for name in categories:
        if (
            not is_safe_category_name(name)
            or any(char in name for char in ':*?"<>|\x00')
            or name.endswith(".")
        ):
            raise ValueError("分类名称无效，请先整理表情包分类")
    files, all_files = {}, {}
    for path in sorted(pack.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(pack).as_posix()
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        all_files[relative] = digest.hexdigest()
        if (
            relative.startswith("memes/")
            and path.suffix.lower() in storage.IMAGE_EXTENSIONS
        ):
            if len(Path(relative).parts) != 3:
                raise ValueError("更新仅支持分类目录下的图片，请先整理嵌套目录")
            files[relative] = digest.hexdigest()
            categories.setdefault(Path(relative).parts[1], "")
    signature = hashlib.sha256(
        json.dumps(all_files, sort_keys=True).encode()
    ).hexdigest()
    return {
        "files": files,
        "categories": categories,
        "version": manifest["version"],
        "signature": signature,
    }


def community_update_status() -> dict:
    """Return fresh install/update state only for entries present in the catalog.

    Returns:
        Status keyed by catalog pack ID, including the latest recovery backup.
    """
    installed = {pack["id"]: pack for pack in storage.list_installed_packs()}
    result = {}
    for entry in (
        storage.load_cached_community_index().get("index", {}).get("packs", [])
    ):
        pack_id = entry["id"]
        local = installed.get(pack_id)
        if not local:
            result[pack_id] = {"installed": False}
            continue
        pack = storage.PACKS_DIR / pack_id
        origin = storage._load_json(pack / ORIGIN_FILE, {})
        source_changed = bool(origin and origin.get("source") != entry.get("source"))
        current, latest = str(local.get("version", "")), str(entry.get("version", ""))
        available = bool(latest.strip()) and latest != current
        backups = []
        directory = storage.BACKUP_DIR / "community_updates"
        if directory.is_dir():
            for path in directory.glob("*/backup.json"):
                record = storage._load_json(path, {})
                if record.get("pack_id") == pack_id and record.get("complete"):
                    backups.append(
                        {
                            "id": path.parent.name,
                            "created_at": record["created_at"],
                            "version": record["version"],
                        }
                    )
        result[pack_id] = {
            "installed": True,
            "current_version": current,
            "latest_version": latest,
            "available": available and not source_changed,
            "source_changed": source_changed,
            "has_baseline": bool(origin.get("files")),
            "backups": sorted(
                backups, key=lambda item: item["created_at"], reverse=True
            ),
        }
    return result


def prepare_update(
    pack_id: str,
    github_accelerator_url: str = "",
    operation_guard=None,
    cancel_check=None,
) -> dict:
    """Download an isolated candidate without modifying installed files.

    Args:
        pack_id: Catalog and installed pack ID.
        github_accelerator_url: Configured download accelerator.
        operation_guard: Optional caller operation guard.
        cancel_check: Cooperative cancellation callback for downloads and staging.

    Returns:
        A random session token for subsequent previews.

    Raises:
        ValueError: Source, downloaded identity or version is inconsistent.
        FileNotFoundError: The catalog entry or installed pack is missing.
    """
    pack_id = validate_pack_id(pack_id)
    if pack_id in {".", ".."}:
        raise ValueError("表情包 ID 无效")
    entry = storage.find_cached_pack_entry(pack_id)
    source = validate_source_descriptor(entry["source"])
    local = storage.PACKS_DIR / pack_id
    if not local.is_dir():
        raise FileNotFoundError("表情包已被删除，请刷新资源广场后安装")
    origin = storage._load_json(local / ORIGIN_FILE, {})
    if origin and origin.get("source") != source:
        raise ValueError("广场来源已变化，不能直接更新原包")
    if operation_guard:
        operation_guard(pack_id, "准备资源包更新")
    cache_root = storage.TEMP_DIR / "community_updates"
    if cache_root.is_dir():
        for cached in sorted(
            cache_root.iterdir(), key=lambda path: path.stat().st_mtime, reverse=True
        ):
            if not re.fullmatch(r"[0-9a-f]{32}", cached.name) or cached.is_symlink():
                continue
            record = storage._load_json(cached / "session.json", {})
            if (
                record.get("pack_id") == pack_id
                and record.get("source") == source
                and time.time() - record.get("created_at", 0) < 14400
                and record.get("remote", {}).get("version")
                == str(entry.get("version") or "")
                and not (cached / "applied.json").exists()
            ):
                if cancel_check and cancel_check():
                    raise storage.InstallCancelledError("已取消下载")
                if (
                    snapshot(cached / "candidate")["signature"]
                    == record["remote"]["signature"]
                ):
                    return {
                        "session": cached.name,
                        "has_baseline": bool(origin.get("files")),
                        "version": record["remote"]["version"],
                        "cached": True,
                    }
    token = secrets.token_hex(16)
    session = storage.TEMP_DIR / "community_updates" / token
    if session.parent.is_dir():
        for expired in session.parent.iterdir():
            if (
                re.fullmatch(r"[0-9a-f]{32}", expired.name)
                and expired.is_dir()
                and not expired.is_symlink()
                and time.time() - expired.stat().st_mtime > 14400
            ):
                storage._require_regular_tree(expired, "清理过期更新预览")
                shutil.rmtree(expired)
    session.mkdir(parents=True)
    try:
        storage._download_github_archive(
            source["repo"],
            source["ref"],
            session / "download.zip",
            github_accelerator_url=github_accelerator_url,
            cancel_check=cancel_check,
        )
        if cancel_check and cancel_check():
            raise storage.InstallCancelledError("已取消检查更新")
        storage._extract_zip_safely(
            session / "download.zip",
            session / "repository",
            block_executable_scripts=False,
        )
        roots = [path for path in (session / "repository").iterdir() if path.is_dir()]
        if len(roots) != 1:
            raise ValueError("下载的仓库目录无效")
        remote = (roots[0] / source["subpath"]).resolve()
        remote.relative_to(roots[0].resolve())
        manifest = validate_pack_directory(remote)
        if manifest["id"] != pack_id or (
            entry.get("version") and str(entry["version"]) != manifest["version"]
        ):
            raise ValueError("下载内容的 ID 或版本与广场不一致，请刷新资源后重试")
        with zipfile.ZipFile(
            session / "pack.zip", "w", zipfile.ZIP_DEFLATED
        ) as archive:
            for path in remote.rglob("*"):
                if cancel_check and cancel_check():
                    raise storage.InstallCancelledError("已取消检查更新")
                if path.is_file():
                    archive.write(path, path.relative_to(remote).as_posix())
        storage._extract_zip_safely(session / "pack.zip", session / "candidate")
        candidate = session / "candidate"
        (candidate / ORIGIN_FILE).unlink(missing_ok=True)
        shutil.rmtree(candidate / "semantic_index", ignore_errors=True)
        remote_snapshot = snapshot(candidate)
        if cancel_check and cancel_check():
            raise storage.InstallCancelledError("已取消检查更新")
        storage._save_json(
            session / "session.json",
            {
                "pack_id": pack_id,
                "source": source,
                "created_at": time.time(),
                "remote": remote_snapshot,
            },
        )
        shutil.rmtree(session / "repository")
        (session / "download.zip").unlink()
        (session / "pack.zip").unlink()
        return {
            "session": token,
            "has_baseline": bool(origin.get("files")),
            "version": remote_snapshot["version"],
        }
    except Exception:
        shutil.rmtree(session, ignore_errors=True)
        raise


def plan_update(data: dict, operation_guard=None) -> dict:
    """Compute a deterministic merge plan, including unresolved category mappings.

    Args:
        data: Session, strategy, file decisions, category mappings and removals.
        operation_guard: Optional caller operation guard.

    Returns:
        A serializable plan and token tied to the current local snapshot.

    Raises:
        ValueError: A session or requested decision is invalid.
        RuntimeError: The session expired or its source changed.
    """
    token = data.get("session", "")
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("更新会话无效，请重新检查更新")
    session = storage.TEMP_DIR / "community_updates" / token
    record = storage._load_json(session / "session.json", {})
    if not record or time.time() - record["created_at"] > 14400:
        raise RuntimeError("更新预览已过期，请重新检查更新")
    pack_id = validate_pack_id(record["pack_id"])
    if pack_id in {".", ".."}:
        raise ValueError("表情包 ID 无效")
    if operation_guard:
        operation_guard(pack_id, "预览资源包更新")
    entry = storage.find_cached_pack_entry(pack_id)
    if entry["source"] != record["source"]:
        raise RuntimeError("广场来源已变化，请重新检查更新")
    local_pack = storage.PACKS_DIR / pack_id
    local = snapshot(local_pack)
    remote = record["remote"]
    if snapshot(session / "candidate")["signature"] != remote["signature"]:
        raise RuntimeError("下载内容已变化，请重新检查更新")
    origin = storage._load_json(local_pack / ORIGIN_FILE, {})
    if origin and origin.get("source") != record["source"]:
        raise RuntimeError("本地包来源已变化，请重新检查更新")
    base = origin.get("files", {})
    strategy = data.get("strategy", "merge_upstream")
    if strategy not in ("merge", "merge_upstream", "add", "replace"):
        raise ValueError("更新策略无效")
    choices = data.get("choices", {})
    mappings = data.get("categories", {})
    removals = data.get("remove", [])
    keep_deleted = data.get("keep_deleted", [])
    skipped = data.get("skip", [])
    if (
        not isinstance(choices, dict)
        or not isinstance(mappings, dict)
        or not isinstance(removals, list)
        or any(not isinstance(item, str) for item in removals)
        or not isinstance(keep_deleted, list)
        or any(not isinstance(item, str) for item in keep_deleted)
        or not isinstance(skipped, list)
        or any(not isinstance(item, str) for item in skipped)
    ):
        raise ValueError("更新选择格式无效")
    hashes = {}
    for path, digest in local["files"].items():
        hashes.setdefault(digest, []).append(path)
    rows, categories = [], {}
    destinations = set(local["files"])
    baseline_hashes = set(base.values())
    counts = {
        "added": 0,
        "changed": 0,
        "upstream_deleted": 0,
        "local_added": sum(
            path not in base and digest not in baseline_hashes
            for path, digest in local["files"].items()
        ),
        "conflicts": 0,
    }
    for path in sorted(set(remote["files"]) | set(base)):
        before, after = base.get(path), remote["files"].get(path)
        local_path = path
        if path not in local["files"] and before and len(hashes.get(before, [])) == 1:
            local_path = hashes[before][0]
        current = local["files"].get(local_path)
        action, kind, options = "keep", "unchanged", []
        if after is None:
            kind = "upstream_deleted"
            counts[kind] += 1
            if current and (
                (strategy == "merge" and local_path in removals)
                or (strategy == "merge_upstream" and local_path not in keep_deleted)
            ):
                action = "delete"
        elif current == after or (not current and after in hashes):
            kind = "unchanged" if current else "moved_or_duplicate"
        elif before is None and not current:
            action, kind = "add", "added"
            counts[kind] += 1
        elif before and current == before and local_path == path and after != before:
            kind = "changed"
            counts[kind] += 1
            action = "update" if strategy in ("merge", "merge_upstream") else "keep"
        elif before and after == before:
            kind = "local_modified"
        else:
            kind = "conflict"
            counts["conflicts"] += 1
            options = (
                ["local", "upstream", "both"]
                if strategy in ("merge", "merge_upstream")
                else ["local"]
            )
            decision = choices.get(
                path, "upstream" if strategy == "merge_upstream" else "local"
            )
            if decision not in options:
                raise ValueError("冲突处理选项与更新策略不符")
            action = {
                "local": "keep",
                "upstream": "update" if current else "add",
                "both": "copy",
            }[decision]
        destination = path
        selectable = action in {"add", "update", "copy"}
        if selectable and path in skipped:
            action = "keep"
        if action in {"add", "update", "copy"}:
            category = Path(path).parts[1]
            remote_description = remote["categories"].get(category, "")
            if category in local["categories"] and " ".join(
                local["categories"][category].split()
            ) != " ".join(remote_description.split()):
                mapped = mappings.get(category, category)
                if not isinstance(mapped, str):
                    raise ValueError("分类映射无效")
                create = mapped.startswith("new:")
                target = mapped[4:] if create else mapped
                if target and (
                    not is_safe_category_name(target)
                    or any(char in target for char in ':*?"<>|\x00')
                    or target.endswith(".")
                ):
                    raise ValueError("分类名称无效")
                if target and (
                    (create and target in local["categories"])
                    or (not create and target not in local["categories"])
                ):
                    raise ValueError("请选择已有分类，或使用不同的新分类名称")
                categories[category] = {
                    "source": category,
                    "local_description": local["categories"][category],
                    "remote_description": remote_description,
                    "target": target,
                    "create": create,
                }
                if target:
                    destination = f"memes/{target}/{Path(path).name}"
            if action == "copy" or (
                destination != local_path and destination in destinations
            ):
                original = Path(destination)
                number = 1
                while destination in destinations:
                    destination = (
                        original.parent
                        / f"{original.stem}_upstream_{number}{original.suffix}"
                    ).as_posix()
                    number += 1
            destinations.add(destination)
        rows.append(
            {
                "path": path,
                "local_path": local_path,
                "destination": destination,
                "kind": kind,
                "action": action,
                "options": options,
                "selectable": selectable,
                "local_exists": bool(current),
            }
        )
    if strategy == "replace":
        categories = {}
        rows = [
            {
                "path": path,
                "local_path": path,
                "destination": path,
                "kind": (
                    "unchanged"
                    if local["files"].get(path) == remote["files"][path]
                    else "changed"
                    if path in local["files"]
                    else "added"
                ),
                "action": "keep" if path in skipped else "add",
                "options": [],
                "selectable": local["files"].get(path) != remote["files"][path],
                "local_exists": path in local["files"],
            }
            for path in sorted(remote["files"])
        ]
        rows.extend(
            {
                "path": path,
                "local_path": path,
                "destination": path,
                "kind": "upstream_deleted",
                "action": "keep" if path in keep_deleted else "delete",
                "options": [],
                "selectable": True,
                "local_exists": True,
            }
            for path in sorted(set(local["files"]) - set(remote["files"]))
        )
    new_descriptions = dict(remote["categories"])
    for category in categories.values():
        if category["create"] and category["target"]:
            target = category["target"]
            if (
                target in new_descriptions
                and new_descriptions[target] != category["remote_description"]
            ):
                raise ValueError(
                    "多个不同描述的分类不能合并到同一个新分类，请使用不同名称"
                )
            new_descriptions[target] = category["remote_description"]
    summary = {
        "pack_id": pack_id,
        "session": token,
        "strategy": strategy,
        "current_version": local["version"],
        "latest_version": remote["version"],
        "has_baseline": bool(base),
        "counts": counts,
        "rows": rows,
        "categories": list(categories.values()),
        "local_categories": local["categories"],
        "local_images": len(local["files"]),
        "local_category_count": len(local["categories"]),
        "ready": all(row["target"] for row in categories.values()),
        "local_signature": local["signature"],
    }
    summary["plan_token"] = hashlib.sha256(
        json.dumps(summary, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return summary


def commit_snapshot(
    pack_id: str, prepared: Path, version: str, index: Path | None, kind: str
) -> dict:
    """Swap a staged pack with temporary rollback and preserve the local index.

    Args:
        pack_id: Installed pack ID held under the caller's mutation lock.
        prepared: Complete staged pack to install.
        version: Version written to the installed registry entry.
        index: Optional saved vector directory to restore.
        kind: Operation label stored in the recovery record.

    Returns:
        Identity and timestamp of the completed operation.

    Raises:
        OSError: Replacement failed and the original contents were restored.
    """
    target = storage.PACKS_DIR / pack_id
    current_index = storage.PLUGIN_DATA_DIR / "semantic_indexes" / pack_id
    storage._require_regular_tree(target, "更新备份")
    storage._require_regular_tree(current_index, "更新备份")
    if index is not None:
        storage._require_regular_tree(index, "恢复备份")
    backup_id = secrets.token_hex(16)
    storage._require_free_space(
        target.parent,
        storage._directory_size(prepared) + storage.MIN_FREE_SPACE_RESERVE_BYTES,
        "写入更新内容",
    )
    registry = storage._load_registry()
    entry = next(
        item.copy() for item in registry["installed_packs"] if item["id"] == pack_id
    )
    info = {
        "pack_id": pack_id,
        "created_at": time.time(),
        "version": entry.get("version", ""),
        "kind": kind,
        "complete": True,
    }
    old = target.with_name(f".{pack_id}.{backup_id}.old")
    staged = target.with_name(f".{pack_id}.{backup_id}.new")
    old_index = current_index.with_name(f".{pack_id}.{backup_id}.old")
    moved_pack = moved_index = installed = False
    try:
        shutil.copytree(prepared, staged)
        target.rename(old)
        moved_pack = True
        if kind == "restore" and current_index.exists():
            current_index.rename(old_index)
            moved_index = True
        staged.rename(target)
        installed = True
        if index is not None and index.is_dir():
            shutil.copytree(index, current_index)
        for item in registry["installed_packs"]:
            if item["id"] == pack_id:
                item["version"] = version
        storage._save_registry(registry)
    except Exception:
        shutil.rmtree(staged, ignore_errors=True)
        if moved_pack:
            shutil.rmtree(target, ignore_errors=True)
            old.rename(target)
        if moved_index:
            shutil.rmtree(current_index, ignore_errors=True)
            old_index.rename(current_index)
        elif installed and kind == "restore":
            shutil.rmtree(current_index, ignore_errors=True)
        registry["installed_packs"] = [
            entry if item["id"] == pack_id else item
            for item in registry["installed_packs"]
        ]
        storage._save_registry(registry)
        raise
    shutil.rmtree(old, ignore_errors=True)
    shutil.rmtree(old_index, ignore_errors=True)
    return info


def apply_update(data: dict, operation_guard=None) -> dict:
    """Apply exactly the reviewed plan, preserving local data unless authorized.

    Args:
        data: Reviewed preview request with plan_token and replacement acknowledgment.
        operation_guard: Optional caller mutation guard.

    Returns:
        Updated pack identity and recovery backup information.

    Raises:
        RuntimeError: The preview is stale or a conflict remains unresolved.
        ValueError: Complete replacement was not explicitly acknowledged.
    """
    plan = plan_update(data)
    session = storage.TEMP_DIR / "community_updates" / plan["session"]
    completed = storage._load_json(session / "applied.json", {})
    if completed:
        return completed
    if not plan["ready"] or plan["plan_token"] != data.get("plan_token"):
        raise RuntimeError("本地内容或更新选择已变化，请重新预览")
    if plan["strategy"] == "replace" and data.get("acknowledge_replace") is not True:
        raise ValueError("请确认完整替换将移除本地新增和修改")
    pack_id = plan["pack_id"]
    if operation_guard:
        operation_guard(pack_id, "更新资源包")
    record = storage._load_json(session / "session.json", {})
    local, remote = storage.PACKS_DIR / pack_id, session / "candidate"
    prepared = session / "prepared"
    shutil.rmtree(prepared, ignore_errors=True)
    shutil.copytree(remote if plan["strategy"] == "replace" else local, prepared)
    if plan["strategy"] == "replace":
        for row in plan["rows"]:
            if row["action"] != "keep":
                continue
            target = prepared / row["path"]
            if row["local_exists"]:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(local / row["path"], target)
            else:
                target.unlink(missing_ok=True)
    if plan["strategy"] == "replace" and (local / "semantic_metadata.json").is_file():
        shutil.copy2(
            local / "semantic_metadata.json", prepared / "semantic_metadata.json"
        )
    categories = snapshot(prepared)["categories"]
    origin = storage._load_json(local / ORIGIN_FILE, {})
    for name, description in record["remote"]["categories"].items():
        if name not in categories and name not in origin.get("categories", {}):
            categories[name] = description
            (prepared / "memes" / name).mkdir(parents=True, exist_ok=True)
    if plan["strategy"] != "replace":
        for row in plan["rows"]:
            if row["action"] == "delete":
                (prepared / row["local_path"]).unlink(missing_ok=True)
            elif row["action"] in {"add", "update", "copy"}:
                destination = prepared / row["destination"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                if (
                    row["action"] == "update"
                    and row["local_path"] != row["destination"]
                ):
                    (prepared / row["local_path"]).unlink(missing_ok=True)
                shutil.copy2(remote / row["path"], destination)
                categories.setdefault(
                    Path(row["destination"]).parts[1],
                    record["remote"]["categories"].get(Path(row["path"]).parts[1], ""),
                )
    if plan["strategy"] == "merge_upstream":
        categories.update(record["remote"]["categories"])
        for name in origin.get("categories", {}):
            if not is_safe_category_name(name):
                continue
            directory = prepared / "memes" / name
            if (
                name not in record["remote"]["categories"]
                and directory.is_dir()
                and not any(directory.iterdir())
            ):
                directory.rmdir()
                categories.pop(name, None)
    manifest = storage._load_json(prepared / "manifest.json", {})
    manifest["version"] = plan["latest_version"]
    manifest["categories"] = {
        name: {"description": description} for name, description in categories.items()
    }
    storage._save_json(prepared / "manifest.json", manifest)
    storage._save_json(prepared / "memes_data.json", categories)
    if (prepared / "semantic_metadata.json").exists() or (
        remote / "semantic_metadata.json"
    ).exists():
        if load_metadata(prepared).get("metadata_read_only") or load_metadata(
            remote
        ).get("metadata_read_only"):
            raise ValueError("语义数据版本不受支持，请升级插件后重试")
        metadata = reconcile_metadata(
            prepared,
            external_data=load_metadata(remote)
            if (remote / "semantic_metadata.json").exists()
            else None,
        )
        local_metadata = load_metadata(local)
        local_images = local_metadata.get("images", {})
        reset_metadata = reset_local_embedding_state(metadata)
        for entry_id, item in metadata.get("images", {}).items():
            previous = local_images.get(entry_id, {})
            reusable = (
                previous.get("embedding_status") == "done"
                and previous.get("text_hash") == item.get("text_hash")
                and previous.get("category_context_hash")
                == item.get("category_context_hash")
                and previous.get("content_sha256") == item.get("content_sha256")
            )
            if reusable:
                item["embedding_status"] = "done"
                reset_metadata["images"][entry_id] = item
        metadata["images"] = reset_metadata["images"]
        metadata["requires_local_index_rebuild"] = any(
            item.get("embedding_status") != "done"
            for item in metadata["images"].values()
        )
        save_metadata(prepared, metadata)
    storage._save_json(
        prepared / ORIGIN_FILE,
        {"source": record["source"], "catalog_id": pack_id, **record["remote"]},
    )
    result = commit_snapshot(pack_id, prepared, plan["latest_version"], None, "update")
    result.update(
        {
            "message": "更新完成，已保留有效向量，变化内容待增量更新",
            "version": plan["latest_version"],
        }
    )
    storage._save_json(session / "applied.json", result)
    return result


def restore_update(data: dict, operation_guard=None) -> dict:
    """Preview or restore a previously saved backup with failure rollback.

    Args:
        data: Backup ID, preview flag and current-content confirmation token.
        operation_guard: Optional caller mutation guard.

    Returns:
        Restore preview or recovery information for the replaced contents.

    Raises:
        ValueError: Backup identity or confirmation is invalid.
        RuntimeError: Contents changed after confirmation.
    """
    backup_id = data.get("backup_id", "")
    if not isinstance(backup_id, str) or not re.fullmatch(r"[0-9a-f]{32}", backup_id):
        raise ValueError("备份 ID 无效")
    backup = storage.BACKUP_DIR / "community_updates" / backup_id
    record = storage._load_json(backup / "backup.json", {})
    if not record.get("complete"):
        raise ValueError("备份不存在或不完整")
    pack_id = validate_pack_id(record["pack_id"])
    storage.find_cached_pack_entry(pack_id)
    current = snapshot(storage.PACKS_DIR / pack_id)
    saved = snapshot(backup / "pack")
    token = hashlib.sha256(
        (current["signature"] + saved["signature"] + backup_id).encode()
    ).hexdigest()
    if data.get("preview", True):
        return {
            "pack_id": pack_id,
            "current_version": current["version"],
            "version": saved["version"],
            "plan_token": token,
            "current_images": len(current["files"]),
        }
    if data.get("plan_token") != token or data.get("acknowledge_restore") is not True:
        raise RuntimeError("请重新预览并确认恢复，当前内容将被替换")
    if operation_guard:
        operation_guard(pack_id, "恢复更新备份")
    return commit_snapshot(
        pack_id, backup / "pack", saved["version"], backup / "index", "restore"
    )
