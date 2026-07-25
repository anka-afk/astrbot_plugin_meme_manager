import json
from pathlib import Path

from ..config import (
    DEFAULT_CATEGORY_DESCRIPTIONS,
    DEFAULT_PACK_ID,
    LEGACY_MIGRATED_PACK_ID,
    PACKS_DIR,
    REGISTRY_PATH,
    SELECTION_RULES_PATH,
)
from .semantic_models import runtime_category_mapping


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


def resolve_pack_id(
    session_id: str | None = None, persona_id: str | None = None
) -> str:
    """Resolve pack id by ordered selection rules and fallback strategy."""
    selection_data = _load_json(SELECTION_RULES_PATH, {})
    registry_data = _load_json(REGISTRY_PATH, {})
    rules = selection_data.get("rules", []) if isinstance(selection_data, dict) else []

    resolved_default = ""
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            scope = str(rule.get("scope") or "").strip().lower()
            pack_id = str(rule.get("pack_id") or "").strip()
            if not pack_id:
                continue

            if scope == "default":
                resolved_default = pack_id
                continue

            target = str(rule.get("target") or "").strip()
            if not target:
                continue

            if (
                scope == "session"
                and session_id
                and target == session_id
                and _pack_exists(pack_id)
                and _is_pack_enabled(pack_id, registry_data)
            ):
                return pack_id
            if (
                scope == "persona"
                and persona_id
                and target == persona_id
                and _pack_exists(pack_id)
                and _is_pack_enabled(pack_id, registry_data)
            ):
                return pack_id

    for fallback_pack_id in (
        resolved_default,
        LEGACY_MIGRATED_PACK_ID,
        DEFAULT_PACK_ID,
    ):
        if not fallback_pack_id:
            continue
        if _pack_exists(fallback_pack_id) and (
            _is_pack_enabled(fallback_pack_id, registry_data)
            or fallback_pack_id in {LEGACY_MIGRATED_PACK_ID, DEFAULT_PACK_ID}
        ):
            return fallback_pack_id

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
                return pack_id

    return DEFAULT_PACK_ID


def get_pack_paths(pack_id: str) -> dict[str, Path]:
    pack_dir = PACKS_DIR / pack_id
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
    pack_id = resolve_pack_id(session_id=session_id, persona_id=persona_id)
    paths = get_pack_paths(pack_id)
    return {
        "pack_id": pack_id,
        "pack_dir": paths["pack_dir"],
        "memes_dir": paths["memes_dir"],
        "metadata_path": paths["metadata_path"],
        "manifest_path": paths["manifest_path"],
        "category_mapping": load_pack_category_mapping(pack_id),
    }
