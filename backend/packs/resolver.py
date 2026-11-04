import json
from pathlib import Path

from ...config import (
    DEFAULT_CATEGORY_DESCRIPTIONS,
    DEFAULT_PACK_ID,
    PACKS_DIR,
    REGISTRY_PATH,
    SELECTION_RULES_PATH,
)
from ..semantic.models import runtime_category_mapping
from .protocol import validate_pack_id


def _load_json(path: Path, default):
    try:
        with path.open(encoding="utf-8") as file_obj:
            return json.load(file_obj)
    except Exception:
        return default


def _is_pack_enabled(pack_id: str, registry_data: dict) -> bool:
    installed = registry_data.get("installed_packs", [])
    if not isinstance(installed, list):
        return False

    for pack in installed:
        if not isinstance(pack, dict):
            continue
        if str(pack.get("id") or "").strip() != pack_id:
            continue
        return bool(pack.get("enabled", True))

    return False


def _pack_exists(pack_id: str) -> bool:
    return (PACKS_DIR / pack_id).is_dir()


def _normalize_pack_ids(raw_value) -> list[str]:
    """将 pack_id 或 pack_ids 字段规范为表情包 ID 列表。

    Args:
        raw_value: 字符串（单个 pack_id）或列表（pack_ids）。

    Returns:
        非空表情包 ID 字符串列表。
    """
    if isinstance(raw_value, str):
        pack_id = raw_value.strip()
        return [pack_id] if pack_id else []
    if isinstance(raw_value, list):
        result = []
        for item in raw_value:
            pack_id = str(item or "").strip()
            if pack_id and pack_id not in result:
                result.append(pack_id)
        return result
    return []


def resolve_pack_ids(
    session_id: str | None = None, persona_id: str | None = None
) -> list[str]:
    """根据选包规则与回退策略解析表情包 ID。

    同时支持单包（pack_id: str）与多包（pack_ids: list）
    规则以兼容旧配置，仅返回已启用且存在的表情包。

    Args:
        session_id: 可选的会话标识符，用于会话范围的规则。
        persona_id: 可选的人格标识符，用于人格范围的规则。

    Returns:
        按优先级排列的已解析表情包 ID 列表。
    """
    selection_data = _load_json(SELECTION_RULES_PATH, {})
    registry_data = _load_json(REGISTRY_PATH, {})
    rules = selection_data.get("rules", []) if isinstance(selection_data, dict) else []

    resolved_default_ids = []
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            scope = str(rule.get("scope") or "").strip().lower()

            # 同时支持旧版 pack_id 与多包 pack_ids。
            candidate_ids = _normalize_pack_ids(
                rule.get("pack_ids") or rule.get("pack_id")
            )
            if not candidate_ids:
                continue

            if scope == "default":
                resolved_default_ids = candidate_ids
                continue

            target = str(rule.get("target") or "").strip()
            if not target:
                continue

            # 校验所有候选表情包均已启用且存在。
            valid_ids = [
                pack_id
                for pack_id in candidate_ids
                if _pack_exists(pack_id) and _is_pack_enabled(pack_id, registry_data)
            ]
            if not valid_ids:
                continue

            if scope == "session" and session_id and target == session_id:
                return valid_ids
            if (
                scope == "persona"
                and persona_id
                and (
                    target == persona_id
                    or (target == "default" and persona_id == "_chatui_default_")
                )
            ):
                return valid_ids

    valid_defaults = list(
        dict.fromkeys(
            pack_id
            for pack_id in resolved_default_ids
            if _pack_exists(pack_id)
            and (_is_pack_enabled(pack_id, registry_data) or pack_id == DEFAULT_PACK_ID)
        )
    )
    if valid_defaults:
        return valid_defaults

    # 仅在所选表情包均不可用时回退到内置默认包。
    if _pack_exists(DEFAULT_PACK_ID):
        return [DEFAULT_PACK_ID]

    # 最后回退到注册表中第一个启用的表情包。
    installed = (
        registry_data.get("installed_packs", [])
        if isinstance(registry_data, dict)
        else []
    )
    if isinstance(installed, list):
        for pack in installed:
            if not isinstance(pack, dict):
                continue
            pack_id = str(pack.get("id") or "").strip()
            if not pack_id:
                continue
            if not bool(pack.get("enabled", True)):
                continue
            if _pack_exists(pack_id):
                return [pack_id]

    return [DEFAULT_PACK_ID]


def resolve_pack_id(
    session_id: str | None = None, persona_id: str | None = None
) -> str:
    """解析单个表情包 ID，作为向后兼容的包装接口。

    Args:
        session_id: 可选的会话标识符，用于会话范围的规则。
        persona_id: 可选的人格标识符，用于人格范围的规则。

    Returns:
        已解析结果中优先级最高的表情包 ID。
    """
    pack_ids = resolve_pack_ids(session_id=session_id, persona_id=persona_id)
    return pack_ids[0] if pack_ids else DEFAULT_PACK_ID


def get_pack_paths(pack_id: str) -> dict[str, Path]:
    pack_id = validate_pack_id(pack_id)
    pack_dir = (PACKS_DIR / pack_id).resolve()
    if pack_dir.parent != PACKS_DIR.resolve():
        raise ValueError("pack_id 无效")
    return {
        "pack_dir": pack_dir,
        "memes_dir": pack_dir / "memes",
        "metadata_path": pack_dir / "memes_data.json",
        "manifest_path": pack_dir / "manifest.json",
    }


def resolve_pack_paths(pack_id: str) -> dict[str, Path]:
    """解析现有表情包，并禁止路径超出表情包根目录。

    Args:
        pack_id: 符合协议的表情包标识符。

    Returns:
        表情包、图片、分类元数据及清单的绝对路径。

    Raises:
        ValueError: ID 无效或路径超出表情包根目录。
        FileNotFoundError: 请求的表情包目录不存在。
    """
    normalized = validate_pack_id(pack_id)
    pack_dir = (PACKS_DIR / normalized).resolve()
    try:
        pack_dir.relative_to(PACKS_DIR.resolve())
    except ValueError as exc:
        raise ValueError("pack_id 无效") from exc
    if not pack_dir.is_dir():
        raise FileNotFoundError(f"表情包 {normalized} 不存在")
    return {
        "pack_dir": pack_dir,
        "memes_dir": pack_dir / "memes",
        "metadata_path": pack_dir / "memes_data.json",
        "manifest_path": pack_dir / "manifest.json",
    }


def load_pack_category_mapping(pack_id: str) -> dict[str, str]:
    paths = get_pack_paths(pack_id)
    mapping = _load_json(paths["metadata_path"], {})

    if isinstance(mapping, dict) and mapping:
        return runtime_category_mapping(mapping)

    manifest = _load_json(paths["manifest_path"], {})
    categories = manifest.get("categories", {}) if isinstance(manifest, dict) else {}
    if isinstance(categories, dict) and categories:
        resolved = {}
        for category, metadata in categories.items():
            if not str(category).strip():
                continue
            if isinstance(metadata, dict):
                description = str(metadata.get("description") or "请添加描述")
            else:
                description = str(metadata)
            resolved[str(category)] = description
        if resolved:
            return runtime_category_mapping(resolved)

    if pack_id == DEFAULT_PACK_ID:
        return runtime_category_mapping(DEFAULT_CATEGORY_DESCRIPTIONS)

    return {}


def resolve_pack_context(
    session_id: str | None = None,
    persona_id: str | None = None,
) -> dict[str, object]:
    """解析支持多包的表情包上下文。

    绑定多个表情包时合并其分类映射。
    返回主表情包路径及合并后的分类。

    Args:
        session_id: 可选的会话标识符，用于会话范围的规则。
        persona_id: 可选的人格标识符，用于人格范围的规则。

    Returns:
        包含 pack_id、pack_ids、路径及合并后 category_mapping 的上下文字典。
    """
    pack_ids = resolve_pack_ids(session_id=session_id, persona_id=persona_id)
    primary_pack_id = pack_ids[0] if pack_ids else DEFAULT_PACK_ID
    paths = get_pack_paths(primary_pack_id)
    pack_dirs = {pack_id: get_pack_paths(pack_id)["pack_dir"] for pack_id in pack_ids}

    # 合并全部表情包的分类映射，主表情包优先。
    merged_mapping = {}
    for pack_id in reversed(pack_ids):
        pack_mapping = load_pack_category_mapping(pack_id)
        merged_mapping.update(pack_mapping)

    return {
        "pack_id": primary_pack_id,
        "pack_ids": pack_ids,
        "pack_dirs": pack_dirs,
        "pack_dir": paths["pack_dir"],
        "memes_dir": paths["memes_dir"],
        "metadata_path": paths["metadata_path"],
        "manifest_path": paths["manifest_path"],
        "category_mapping": merged_mapping,
    }
