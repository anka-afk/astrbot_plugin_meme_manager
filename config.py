import json
import os
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
DEFAULT_PLUGIN_NAME = "meme_manager"
DEFAULT_PACK_ID = "builtin-default"
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


def _get_pack_display_name(pack_id: str) -> str:
    if pack_id == DEFAULT_PACK_ID:
        return "Builtin Default Meme Pack"
    return f"Meme Pack {pack_id}"


def _get_pack_description(pack_id: str) -> str:
    if pack_id == DEFAULT_PACK_ID:
        return "Builtin default meme pack generated during runtime bootstrap"
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
        plugin_data_dir / "temp",
    ):
        directory.mkdir(parents=True, exist_ok=True)


def _ensure_builtin_default_pack(plugin_data_dir: Path) -> None:
    builtin_pack_dir = plugin_data_dir / "packs" / DEFAULT_PACK_ID
    builtin_pack_memes_dir = builtin_pack_dir / "memes"
    builtin_pack_memes_dir.mkdir(parents=True, exist_ok=True)
    if not (builtin_pack_dir / "manifest.json").exists():
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

    return DEFAULT_PACK_ID


def _bootstrap_pack_runtime(plugin_data_dir: Path) -> None:
    _ensure_runtime_layout(plugin_data_dir)

    if (
        not (plugin_data_dir / "registry.json").is_file()
        or not (plugin_data_dir / "selection_rules.json").is_file()
    ):
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
_bootstrap_pack_runtime(PLUGIN_DATA_DIR)
PACKS_DIR = PLUGIN_DATA_DIR / "packs"
REGISTRY_PATH = PLUGIN_DATA_DIR / "registry.json"
SELECTION_RULES_PATH = PLUGIN_DATA_DIR / "selection_rules.json"
COMMUNITY_CACHE_PATH = PLUGIN_DATA_DIR / "community_cache.json"
COMMUNITY_INDEX_URL = "https://raw.githubusercontent.com/anka-afk/astrbot-meme-pack-index/main/community-index.json"
BACKUP_DIR = PLUGIN_DATA_DIR / "backup"
TEMP_DIR = PLUGIN_DATA_DIR / "temp"
SEMANTIC_INDEXES_DIR = PLUGIN_DATA_DIR / "semantic_indexes"

SEMANTIC_INDEXES_DIR.mkdir(parents=True, exist_ok=True)

print(f"Plugin directory: {PLUGIN_DIR}", file=sys.stderr)
print(f"Plugin data directory: {PLUGIN_DATA_DIR}", file=sys.stderr)
