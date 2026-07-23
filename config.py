import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from astrbot.core.utils.astrbot_path import get_astrbot_data_path

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
except ImportError:
    # AstrBot 4.5.6 尚未提供这个接口，保持与新版插件的数据目录约定一致。
    def get_astrbot_plugin_data_path() -> str:
        return os.path.realpath(os.path.join(get_astrbot_data_path(), "plugin_data"))


PLUGIN_DIR = Path(__file__).resolve().parent
CURRENT_DIR = str(PLUGIN_DIR)
DEFAULT_PLUGIN_NAME = "meme_manager"
DEFAULT_PACK_ID = "builtin-default"
LEGACY_MIGRATED_PACK_ID = "legacy-migrated"
PACK_SCHEMA_VERSION = 1
RUNTIME_SCHEMA_VERSION = 1

DEFAULT_CATEGORY_DESCRIPTIONS = {
    "angry": "当对话包含抱怨、批评或激烈反对时使用（如用户投诉/观点反驳）",
    "happy": "用于成功确认、积极反馈或庆祝场景（问题解决/获得成就）",
    "sad": "表达伤心, 歉意、遗憾或安慰场景（遇到挫折/传达坏消息）",
    "surprised": "响应超出预期的信息（重大发现/意外转折）注意：轻微惊讶慎用",
    "confused": "请求澄清或表达理解障碍时（概念模糊/逻辑矛盾）或对于用户的请求感到困惑",
    "color": "社交场景中的暧昧表达（调情）使用频率≤1次/对话",
    "cpu": "技术讨论中表示思维卡顿（复杂问题/需要加载时间）",
    "fool": "自嘲或缓和气氛的幽默场景（小失误/无伤大雅的玩笑）",
    "givemoney": "涉及报酬讨论时使用（服务付费/奖励机制）需配合明确金额",
    "like": "表达对事物或观点的喜爱（美食/艺术/优秀方案）",
    "see": "表示偷瞄或持续关注（监控进度/观察变化）常与时间词搭配",
    "shy": "涉及隐私话题或收到赞美时（个人故事/外貌评价）",
    "work": "工作流程相关场景（任务分配/进度汇报）",
    "reply": "等待用户反馈时（提问后/需要确认）最长间隔30分钟",
    "meow": "卖萌或萌系互动场景（宠物话题/安抚情绪）慎用于正式场合",
    "baka": "轻微责备或吐槽（低级错误/可爱型抱怨）禁用程度：友善级",
    "morning": "早安问候专用（UTC时间6:00-10:00）跨时区需换算",
    "sleep": "涉及作息场景（熬夜/疲劳/休息建议）",
    "sigh": "表达无奈, 无语或感慨（重复问题/历史遗留难题）",
}


def resolve_plugin_name(plugin_name: str | None = None) -> str:
    """返回运行时插件名称，并提供稳定的回退值。"""
    candidate = plugin_name or DEFAULT_PLUGIN_NAME
    return candidate.strip() or DEFAULT_PLUGIN_NAME


def get_legacy_plugin_data_dir() -> Path | None:
    """如果存在，返回旧的 AstrBot 全局 meme 数据目录。"""
    try:
        return (Path(get_astrbot_data_path()) / "memes_data").resolve()
    except Exception:
        return None


def get_plugin_data_dir(plugin_name: str | None = None) -> Path:
    """返回插件运行时数据目录。"""
    resolved_plugin_name = resolve_plugin_name(plugin_name)
    try:
        plugin_data_root = Path(get_astrbot_plugin_data_path())
        return (plugin_data_root / resolved_plugin_name).resolve()
    except Exception:
        fallback_data_path = (
            PLUGIN_DIR / "data" / "plugin_data" / resolved_plugin_name
        ).resolve()
        print(
            f"无法解析 AstrBot 插件数据目录，回退到: {fallback_data_path}",
            file=sys.stderr,
        )
        return fallback_data_path


def _plugin_data_dir_has_content(plugin_data_dir: Path) -> bool:
    """返回目标插件数据目录是否已包含数据。"""
    metadata_file = plugin_data_dir / "memes_data.json"
    if metadata_file.is_file():
        return True

    memes_dir = plugin_data_dir / "memes"
    return memes_dir.is_dir() and any(memes_dir.iterdir())


def _copy_directory_contents(source_dir: Path, target_dir: Path) -> None:
    """合并复制目录内容，不覆盖已存在的文件。"""
    for item in source_dir.iterdir():
        target_path = target_dir / item.name
        if item.is_dir():
            shutil.copytree(item, target_path, dirs_exist_ok=True)
            continue
        if not target_path.exists():
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target_path)


def migrate_legacy_data_dir_if_needed(plugin_data_dir: Path) -> None:
    """将旧的 AstrBot 全局数据目录复制到插件数据目录中。"""
    legacy_data_dir = get_legacy_plugin_data_dir()
    if legacy_data_dir is None or not legacy_data_dir.exists():
        return

    if legacy_data_dir.resolve() == plugin_data_dir.resolve():
        return

    if _plugin_data_dir_has_content(plugin_data_dir):
        return

    try:
        plugin_data_dir.mkdir(parents=True, exist_ok=True)
        _copy_directory_contents(legacy_data_dir, plugin_data_dir)
        print(
            f"检测到旧版插件数据目录，已复制到: {plugin_data_dir}",
            file=sys.stderr,
        )
    except Exception as exc:
        print(
            f"复制旧版插件数据目录失败: {exc}",
            file=sys.stderr,
        )


def _load_json_file(path: Path, default):
    try:
        with path.open(encoding="utf-8") as file_obj:
            return json.load(file_obj)
    except Exception:
        return default


def _save_json_file(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file_obj:
        json.dump(data, file_obj, ensure_ascii=False, indent=2)


def _runtime_legacy_memes_dir(plugin_data_dir: Path) -> Path:
    return plugin_data_dir / "memes"


def _runtime_legacy_metadata_path(plugin_data_dir: Path) -> Path:
    return plugin_data_dir / "memes_data.json"


def _pack_has_files(pack_dir: Path) -> bool:
    memes_dir = pack_dir / "memes"
    return memes_dir.is_dir() and any(memes_dir.iterdir())


def _collect_category_descriptions(
    metadata_path: Path, memes_dir: Path, fallback: dict[str, str] | None = None
) -> dict[str, str]:
    descriptions = {}
    loaded = _load_json_file(metadata_path, {})
    if isinstance(loaded, dict):
        descriptions.update(
            {
                str(category): str(description)
                for category, description in loaded.items()
                if str(category).strip()
            }
        )

    if fallback:
        for category, description in fallback.items():
            descriptions.setdefault(category, description)

    if memes_dir.is_dir():
        for item in sorted(memes_dir.iterdir()):
            if item.is_dir():
                descriptions.setdefault(item.name, "请添加描述")

    return descriptions


def _get_pack_display_name(pack_id: str) -> str:
    if pack_id == DEFAULT_PACK_ID:
        return "Builtin Default Meme Pack"
    if pack_id == LEGACY_MIGRATED_PACK_ID:
        return "Migrated Legacy Meme Pack"
    return f"Meme Pack {pack_id}"


def _get_pack_description(pack_id: str) -> str:
    if pack_id == DEFAULT_PACK_ID:
        return "Builtin default meme pack generated during runtime bootstrap"
    if pack_id == LEGACY_MIGRATED_PACK_ID:
        return "Legacy runtime data migrated into the Phase 1 pack layout"
    return "Runtime-managed meme pack"


def _build_pack_manifest(pack_id: str, category_descriptions: dict[str, str]) -> dict:
    return {
        "schema_version": PACK_SCHEMA_VERSION,
        "id": pack_id,
        "name": _get_pack_display_name(pack_id),
        "version": "1.0.0",
        "description": _get_pack_description(pack_id),
        "tags": ["runtime"],
        "categories": {
            category: {"description": description}
            for category, description in sorted(category_descriptions.items())
        },
    }


def _write_pack_manifest(
    pack_dir: Path, pack_id: str, category_descriptions: dict[str, str]
) -> None:
    _save_json_file(
        pack_dir / "manifest.json",
        _build_pack_manifest(pack_id, category_descriptions),
    )


def _write_pack_compatibility_metadata(
    pack_dir: Path, category_descriptions: dict[str, str]
) -> None:
    _save_json_file(pack_dir / "memes_data.json", category_descriptions)


def _write_registry(plugin_data_dir: Path, pack_id: str) -> None:
    registry_path = plugin_data_dir / "registry.json"
    registry = {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "installed_packs": [
            {
                "id": pack_id,
                "name": _get_pack_display_name(pack_id),
                "version": "1.0.0",
                "enabled": True,
                "installed_at": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }
    _save_json_file(registry_path, registry)


def _write_default_selection_rules(plugin_data_dir: Path, pack_id: str) -> None:
    selection_rules_path = plugin_data_dir / "selection_rules.json"
    selection_rules = {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "rules": [
            {
                "id": "default",
                "scope": "default",
                "pack_id": pack_id,
            }
        ],
    }
    _save_json_file(selection_rules_path, selection_rules)


def _ensure_runtime_layout(plugin_data_dir: Path) -> None:
    for directory in (
        plugin_data_dir / "packs",
        plugin_data_dir / "backup",
        plugin_data_dir / "migration",
        plugin_data_dir / "temp",
    ):
        directory.mkdir(parents=True, exist_ok=True)


def _has_legacy_root_runtime_data(plugin_data_dir: Path) -> bool:
    legacy_metadata_path = _runtime_legacy_metadata_path(plugin_data_dir)
    legacy_memes_dir = _runtime_legacy_memes_dir(plugin_data_dir)
    return legacy_metadata_path.is_file() or (
        legacy_memes_dir.is_dir() and any(legacy_memes_dir.iterdir())
    )


def _migrate_legacy_root_into_pack(plugin_data_dir: Path) -> None:
    legacy_pack_dir = plugin_data_dir / "packs" / LEGACY_MIGRATED_PACK_ID
    legacy_pack_memes_dir = legacy_pack_dir / "memes"
    legacy_pack_memes_dir.mkdir(parents=True, exist_ok=True)

    legacy_metadata_path = _runtime_legacy_metadata_path(plugin_data_dir)
    legacy_memes_dir = _runtime_legacy_memes_dir(plugin_data_dir)

    if legacy_memes_dir.is_dir():
        _copy_directory_contents(legacy_memes_dir, legacy_pack_memes_dir)

    category_descriptions = _collect_category_descriptions(
        legacy_metadata_path,
        legacy_pack_memes_dir,
        DEFAULT_CATEGORY_DESCRIPTIONS,
    )
    _write_pack_compatibility_metadata(legacy_pack_dir, category_descriptions)
    _write_pack_manifest(
        legacy_pack_dir, LEGACY_MIGRATED_PACK_ID, category_descriptions
    )
    _save_json_file(
        plugin_data_dir / "migration" / "legacy_runtime_migrated.json",
        {
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "pack_id": LEGACY_MIGRATED_PACK_ID,
            "migrated_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _ensure_builtin_default_pack(plugin_data_dir: Path) -> None:
    builtin_pack_dir = plugin_data_dir / "packs" / DEFAULT_PACK_ID
    builtin_pack_memes_dir = builtin_pack_dir / "memes"
    builtin_pack_memes_dir.mkdir(parents=True, exist_ok=True)
    _write_pack_manifest(
        builtin_pack_dir, DEFAULT_PACK_ID, DEFAULT_CATEGORY_DESCRIPTIONS
    )


def _resolve_default_pack_id(plugin_data_dir: Path) -> str:
    selection_rules_path = plugin_data_dir / "selection_rules.json"
    if selection_rules_path.is_file():
        selection_rules = _load_json_file(selection_rules_path, {})
        rules = (
            selection_rules.get("rules", [])
            if isinstance(selection_rules, dict)
            else []
        )
        if isinstance(rules, list):
            for rule in reversed(rules):
                if not isinstance(rule, dict):
                    continue
                if rule.get("scope") != "default":
                    continue
                pack_id = str(rule.get("pack_id") or "").strip()
                if pack_id and (plugin_data_dir / "packs" / pack_id).is_dir():
                    return pack_id

    legacy_pack_dir = plugin_data_dir / "packs" / LEGACY_MIGRATED_PACK_ID
    if legacy_pack_dir.is_dir() and _pack_has_files(legacy_pack_dir):
        return LEGACY_MIGRATED_PACK_ID

    return DEFAULT_PACK_ID


def sync_active_pack_metadata(
    category_descriptions: dict[str, str] | None = None,
) -> None:
    """将当前表情包的清单与兼容性元数据文件同步。"""
    active_pack_dir = PACKS_DIR / ACTIVE_PACK_ID
    active_pack_dir.mkdir(parents=True, exist_ok=True)
    (active_pack_dir / "memes").mkdir(parents=True, exist_ok=True)

    descriptions = category_descriptions or _collect_category_descriptions(
        MEMES_DATA_PATH,
        MEMES_DIR,
        DEFAULT_CATEGORY_DESCRIPTIONS if ACTIVE_PACK_ID == DEFAULT_PACK_ID else None,
    )
    _write_pack_manifest(active_pack_dir, ACTIVE_PACK_ID, descriptions)


def _bootstrap_pack_runtime(plugin_data_dir: Path) -> None:
    _ensure_runtime_layout(plugin_data_dir)

    if (
        not (plugin_data_dir / "registry.json").is_file()
        or not (plugin_data_dir / "selection_rules.json").is_file()
    ):
        if _has_legacy_root_runtime_data(plugin_data_dir):
            _migrate_legacy_root_into_pack(plugin_data_dir)
            default_pack_id = LEGACY_MIGRATED_PACK_ID
        else:
            _ensure_builtin_default_pack(plugin_data_dir)
            default_pack_id = DEFAULT_PACK_ID

        if not (plugin_data_dir / "registry.json").is_file():
            _write_registry(plugin_data_dir, default_pack_id)
        if not (plugin_data_dir / "selection_rules.json").is_file():
            _write_default_selection_rules(plugin_data_dir, default_pack_id)
        return

    default_pack_id = _resolve_default_pack_id(plugin_data_dir)
    if default_pack_id == DEFAULT_PACK_ID:
        _ensure_builtin_default_pack(plugin_data_dir)


PLUGIN_DATA_DIR = get_plugin_data_dir()
migrate_legacy_data_dir_if_needed(PLUGIN_DATA_DIR)
_bootstrap_pack_runtime(PLUGIN_DATA_DIR)
BASE_DATA_DIR = PLUGIN_DATA_DIR
PACKS_DIR = PLUGIN_DATA_DIR / "packs"
REGISTRY_PATH = PLUGIN_DATA_DIR / "registry.json"
SELECTION_RULES_PATH = PLUGIN_DATA_DIR / "selection_rules.json"
COMMUNITY_CACHE_PATH = PLUGIN_DATA_DIR / "community_cache.json"
COMMUNITY_INDEX_URL = "https://raw.githubusercontent.com/anka-afk/astrbot-meme-pack-index/main/community-index.json"
BACKUP_DIR = PLUGIN_DATA_DIR / "backup"
MIGRATION_DIR = PLUGIN_DATA_DIR / "migration"
TEMP_DIR = PLUGIN_DATA_DIR / "temp"
SEMANTIC_INDEXES_DIR = PLUGIN_DATA_DIR / "semantic_indexes"
ACTIVE_PACK_ID = _resolve_default_pack_id(PLUGIN_DATA_DIR)
ACTIVE_PACK_DIR = PACKS_DIR / ACTIVE_PACK_ID
MEMES_DIR = ACTIVE_PACK_DIR / "memes"
MEMES_DATA_PATH = ACTIVE_PACK_DIR / "memes_data.json"
ACTIVE_PACK_MANIFEST_PATH = ACTIVE_PACK_DIR / "manifest.json"
DEFAULT_MEMES_INIT_MARKER = ACTIVE_PACK_DIR / ".default_memes_initialized"

os.makedirs(MEMES_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(MIGRATION_DIR, exist_ok=True)
os.makedirs(SEMANTIC_INDEXES_DIR, exist_ok=True)

print(f"插件目录: {PLUGIN_DIR}", file=sys.stderr)
print(f"插件数据目录: {PLUGIN_DATA_DIR}", file=sys.stderr)
print(f"当前默认表情包: {ACTIVE_PACK_ID}", file=sys.stderr)
print(f"兼容表情包目录: {MEMES_DIR}", file=sys.stderr)
