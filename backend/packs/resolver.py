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
    """Normalize pack_id or pack_ids field into a list of pack IDs.

    Args:
        raw_value: Either a string (single pack_id) or list (pack_ids).

    Returns:
        List of non-empty pack ID strings.
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
    """Resolve pack IDs by selection rules and fallback strategy.

    Supports both single-pack (pack_id: str) and multi-pack (pack_ids: list)
    rules for backward compatibility. Returns enabled and existing packs only.

    Args:
        session_id: Optional session identifier for session-scoped rules.
        persona_id: Optional persona identifier for persona-scoped rules.

    Returns:
        List of resolved pack IDs, ordered by priority.
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

            # Support both pack_id (legacy) and pack_ids (multi-pack).
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

            # Validate all candidates are enabled and exist.
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

    # Fall back to the built-in default only when no selected pack is usable.
    if _pack_exists(DEFAULT_PACK_ID):
        return [DEFAULT_PACK_ID]

    # Last resort: first enabled pack in registry.
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
    """Resolve a single pack ID (backward compatibility wrapper).

    Args:
        session_id: Optional session identifier for session-scoped rules.
        persona_id: Optional persona identifier for persona-scoped rules.

    Returns:
        The highest-priority resolved pack ID.
    """
    pack_ids = resolve_pack_ids(session_id=session_id, persona_id=persona_id)
    return pack_ids[0] if pack_ids else DEFAULT_PACK_ID


def get_pack_paths(pack_id: str) -> dict[str, Path]:
    pack_dir = PACKS_DIR / pack_id
    return {
        "pack_dir": pack_dir,
        "memes_dir": pack_dir / "memes",
        "metadata_path": pack_dir / "memes_data.json",
        "manifest_path": pack_dir / "manifest.json",
    }


def resolve_pack_paths(pack_id: str) -> dict[str, Path]:
    """Resolve an existing pack without allowing paths outside the pack root.

    Args:
        pack_id: Protocol-compatible pack identifier.

    Returns:
        Absolute paths for the pack, images, category metadata and manifest.

    Raises:
        ValueError: The ID is invalid or escapes the pack root.
        FileNotFoundError: The requested pack directory does not exist.
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
    """Resolve pack context with multi-pack support.

    When multiple packs are bound, merges their category mappings.
    Returns primary pack paths with merged categories.

    Args:
        session_id: Optional session identifier for session-scoped rules.
        persona_id: Optional persona identifier for persona-scoped rules.

    Returns:
        Context dict with pack_id, pack_ids, paths, and merged category_mapping.
    """
    pack_ids = resolve_pack_ids(session_id=session_id, persona_id=persona_id)
    primary_pack_id = pack_ids[0] if pack_ids else DEFAULT_PACK_ID
    paths = get_pack_paths(primary_pack_id)
    pack_dirs = {pack_id: get_pack_paths(pack_id)["pack_dir"] for pack_id in pack_ids}

    # Merge category mappings from all packs (primary takes precedence).
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
