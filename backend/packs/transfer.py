"""Preview and execute image transfers between explicitly selected packs."""

import hashlib
import json
import os
import re
import secrets
import shutil
from pathlib import Path

from ..semantic.storage import invalidate_semantic_metadata
from .categories import is_safe_category_name, resolve_safe_category_directory
from .images import IMAGE_EXTENSIONS


def transfer_pack_images(packs_root: Path, data: dict) -> dict:
    """Plan a transfer or execute a previously reviewed plan under caller locks.

    Args:
        packs_root: Directory containing installed packs.
        data: Explicit pack IDs, image selections, category policy and preview token.

    Returns:
        Category mappings for preview, or per-image outcomes after execution.

    Raises:
        ValueError: A request, metadata document or path is invalid.
        RuntimeError: The reviewed plan changed or category conflicts remain.
        OSError: Pack metadata could not be saved before moving images.
    """
    if not isinstance(data, dict) or not isinstance(data.get("preview", True), bool):
        raise ValueError("请求格式无效")
    contexts = []
    root = packs_root.resolve()
    for field in ("source_pack_id", "target_pack_id"):
        pack_id = data.get(field)
        if (
            not isinstance(pack_id, str)
            or not re.fullmatch(r"[A-Za-z0-9._-]{2,64}", pack_id)
            or pack_id in {"..", "."}
        ):
            raise ValueError("表情包 ID 无效")
        pack = root / pack_id
        if pack.is_symlink() or pack.resolve().parent != root or not pack.is_dir():
            raise ValueError("表情包不存在或路径无效")
        memes = pack / "memes"
        if memes.is_symlink() or memes.resolve().parent != pack.resolve():
            raise ValueError("图片目录无效")
        documents = {}
        for name in ("manifest.json", "memes_data.json"):
            path = pack / name
            if path.is_symlink():
                raise ValueError("分类配置路径无效")
            documents[name] = (
                json.loads(path.read_text(encoding="utf-8-sig"))
                if path.exists()
                else {}
            )
            if not isinstance(documents[name], dict):
                raise ValueError("分类配置格式无效")
        categories = documents["manifest.json"].get("categories", {})
        if not isinstance(categories, dict):
            raise ValueError("分类配置格式无效")
        descriptions = {
            key: str(value.get("description", "") if isinstance(value, dict) else value)
            for key, value in categories.items()
        }
        descriptions.update(
            {
                key: str(value or "")
                for key, value in documents["memes_data.json"].items()
            }
        )
        if memes.is_dir():
            for folder in memes.iterdir():
                if folder.is_dir() and not folder.is_symlink():
                    descriptions.setdefault(folder.name, "")
        contexts.append((pack, memes, documents, descriptions))
    source, target = contexts
    if source[0] == target[0]:
        raise ValueError("请选择其他表情包；同包移动请使用移动分类")
    mode = data.get("mode")
    if not isinstance(mode, str) or mode not in {"target", "preserve"}:
        raise ValueError("请选择分类处理方式")
    items = data.get("items")
    mapping = data.get("category_mapping", {})
    if (
        not isinstance(items, list)
        or not 1 <= len(items) <= 5000
        or not isinstance(mapping, dict)
    ):
        raise ValueError("请选择 1 至 5000 张图片")
    grouped = {}
    paths = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("图片选择格式无效")
        category, filename = item.get("category"), item.get("emoji")
        if (
            not isinstance(category, str)
            or not is_safe_category_name(category)
            or any(c in category for c in ':*?"<>|\x00')
            or category.endswith(".")
        ):
            raise ValueError("来源分类无效")
        if (
            not isinstance(filename, str)
            or not is_safe_category_name(filename)
            or any(c in filename for c in ':*?"<>|\x00')
            or not filename.lower().endswith(IMAGE_EXTENSIONS)
        ):
            raise ValueError("图片文件名无效")
        folder = resolve_safe_category_directory(source[1], category)
        path = folder / filename
        if (
            (source[1] / category).is_symlink()
            or path.is_symlink()
            or path.resolve().parent != folder
        ):
            raise ValueError("来源图片路径无效")
        grouped.setdefault(category, set()).add(filename)
        paths[(category, filename)] = path
    plan = []
    for category, filenames in sorted(grouped.items()):
        chosen = (
            data.get("target_category", "")
            if mode == "target"
            else mapping.get(category, "")
        )
        if not isinstance(chosen, str):
            raise ValueError("目标分类无效")
        destination = chosen or (category if mode == "preserve" else "")
        if destination and (
            not is_safe_category_name(destination)
            or any(c in destination for c in ':*?"<>|\x00')
            or destination.endswith(".")
        ):
            raise ValueError("目标分类无效")
        if destination and destination not in target[3]:
            # Windows paths compare case-insensitively; reuse the stored spelling.
            destination = next(
                (
                    name
                    for name in target[3]
                    if target[1] / name == target[1] / destination
                ),
                destination,
            )
        source_description = source[3].get(category, "")
        target_description = target[3].get(destination, "")
        exists = destination in target[3]
        if chosen and not exists:
            raise ValueError("指定的目标分类不存在，请重新选择")
        conflict = not destination or (
            not chosen
            and exists
            and " ".join(source_description.split())
            != " ".join(target_description.split())
        )
        if destination:
            folder = resolve_safe_category_directory(target[1], destination)
            if (target[1] / destination).is_symlink():
                raise ValueError("目标分类路径无效")
        plan.append(
            {
                "source_category": category,
                "target_category": destination,
                "count": len(filenames),
                "action": "choose" if conflict else "existing" if exists else "create",
                "source_description": source_description,
                "target_description": target_description,
                "conflict": conflict,
            }
        )
    token = hashlib.sha256(
        json.dumps(
            [
                data["source_pack_id"],
                data["target_pack_id"],
                mode,
                plan,
                [(key, sorted(value)) for key, value in sorted(grouped.items())],
            ],
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    result = {
        "plan": plan,
        "ready": all(not row["conflict"] for row in plan),
        "plan_token": token,
        "target_categories": target[3],
    }
    if data.get("preview", True):
        return result
    if not result["ready"] or data.get("plan_token") != token:
        raise RuntimeError("分类信息已变化或冲突未解决，请重新预览并确认")
    # Persist new category descriptions before any source image can be removed.
    if any(row["action"] == "create" for row in plan):
        descriptions = dict(target[3])
        manifest = dict(target[2]["manifest.json"])
        manifest_categories = dict(manifest.get("categories", {}))
        for row in plan:
            if row["action"] == "create":
                descriptions[row["target_category"]] = row["source_description"]
                manifest_categories[row["target_category"]] = {
                    "description": row["source_description"]
                }
        manifest["categories"] = manifest_categories
        for name, content in (
            ("memes_data.json", descriptions),
            ("manifest.json", manifest),
        ):
            temporary = target[0] / f".{name}.{secrets.token_hex(8)}.tmp"
            try:
                with temporary.open("x", encoding="utf-8") as stream:
                    json.dump(content, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(target[0] / name)
            finally:
                if temporary.exists() and not temporary.is_symlink():
                    temporary.unlink()
    moved, failed = [], []
    for row in plan:
        category, destination = row["source_category"], row["target_category"]
        folder = resolve_safe_category_directory(target[1], destination)
        for filename in sorted(grouped[category]):
            item = {
                "category": category,
                "emoji": filename,
                "target_category": destination,
            }
            source_file, target_file = paths[(category, filename)], folder / filename
            created = False
            try:
                folder.mkdir(parents=True, exist_ok=True)
                with (
                    source_file.open("rb") as incoming,
                    target_file.open("xb") as outgoing,
                ):
                    created = True
                    shutil.copyfileobj(incoming, outgoing)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                source_file.unlink()
                moved.append(item)
            except OSError as exc:
                cleanup_failed = False
                if created:
                    try:
                        target_file.unlink(missing_ok=True)
                    except OSError:
                        cleanup_failed = True
                reason = (
                    "目标分类已有同名图片"
                    if isinstance(exc, FileExistsError)
                    else "源图片不存在"
                    if isinstance(exc, FileNotFoundError)
                    else "文件读写失败，源图片已保留"
                )
                if cleanup_failed:
                    reason = "源图片已保留，目标残留文件清理失败，请检查后再重试"
                failed.append({**item, "reason": reason})
    warnings = []
    if moved:
        for pack, *_ in contexts:
            if (pack / "semantic_metadata.json").is_file():
                try:
                    invalidate_semantic_metadata(pack)
                except Exception:
                    warnings.append(f"{pack.name} 的语义索引刷新失败，请重新语义化")
    return {"moved": moved, "failed": failed, "warnings": warnings}
