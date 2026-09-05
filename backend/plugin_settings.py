"""Schema-driven editing of the configuration shared with AstrBot's dashboard."""

import copy
import hashlib
import json
import math
import re
from pathlib import Path
from urllib.parse import urlsplit


def describe_settings(config) -> tuple[dict, dict]:
    """Read the shared file and describe its editable fields without exposing secrets.

    Args:
        config: The AstrBotConfig instance supplied to the plugin by AstrBot.

    Returns:
        A public snapshot and the complete private configuration used for merging.

    Raises:
        ValueError: The configuration file or schema is unavailable or malformed.
    """
    config_path = getattr(config, "config_path", None)
    schema = getattr(config, "schema", None)
    if not config_path or not isinstance(schema, dict):
        raise ValueError("当前插件未注册可编辑的 AstrBot 配置，请重载插件后重试。")
    values = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
    if not isinstance(values, dict):
        raise ValueError("配置文件格式异常，请先在 AstrBot 中检查配置。")
    revision = hashlib.sha256(
        json.dumps(values, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    fields = []
    pending = [((), schema, values, [])]
    defaults = {
        "string": "",
        "text": "",
        "bool": False,
        "int": 0,
        "float": 0,
        "list": [],
    }
    while pending:
        prefix, items, current, groups = pending.pop()
        for key, spec in items.items():
            path = (*prefix, key)
            label = str(spec.get("description") or key)
            value = current.get(key) if isinstance(current, dict) else None
            if spec.get("type") == "object":
                pending.append((path, spec.get("items", {}), value, [*groups, label]))
                continue
            kind = spec.get("type", "string")
            default = copy.deepcopy(spec.get("default", defaults.get(kind, "")))
            if value is None:
                value = copy.deepcopy(default)
            secret = key in {
                "key",
                "secret",
                "password",
                "access_key_id",
                "secret_access_key",
            }
            field = {
                "path": ".".join(path),
                "label": label,
                "groups": groups,
                "type": kind,
                "hint": str(spec.get("hint") or ""),
                "default": "" if secret else default,
                "value": "" if secret else value,
                "secret": secret,
                "configured": bool(value) if secret else False,
                "options": spec.get("options", []),
                "special": spec.get("_special", ""),
                "bounds": spec.get("slider", {}),
            }
            fields.append(field)
    return {"revision": revision, "fields": fields}, values


def validate_settings_changes(
    values: dict, fields: list[dict], changes: dict, schema: dict
) -> dict:
    """Validate a partial edit and preserve the current schema's other settings.

    Args:
        values: The latest contents of the shared configuration file.
        fields: Editable field descriptions derived from the plugin schema.
        changes: A mapping of dotted field paths to explicitly changed values.
        schema: The plugin schema, including empty informational groups.

    Returns:
        A new complete configuration containing only declared schema fields.

    Raises:
        ValueError: A path, type, option, range, URL or regular expression is invalid.
    """
    if not isinstance(changes, dict) or not changes:
        raise ValueError("没有需要保存的配置更改。")
    allowed = {field["path"]: field for field in fields}
    updated = {}
    pending = [(updated, schema)]
    while pending:
        node, items = pending.pop()
        for key, spec in items.items():
            if spec["type"] == "object":
                node[key] = {}
                pending.append((node[key], spec.get("items", {})))
    for path, field in allowed.items():
        keys = path.split(".")
        value = values
        for key in keys:
            value = value.get(key) if isinstance(value, dict) else None
        if value is None:
            value = field["default"]
        node = updated
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        node[keys[-1]] = copy.deepcopy(value)
    for path, value in changes.items():
        if path not in allowed:
            raise ValueError("包含未知配置项，请重新加载设置。")
        field = allowed[path]
        kind = field["type"]
        label = field["label"]
        valid = (
            (kind == "bool" and type(value) is bool)
            or (kind == "int" and type(value) is int)
            or (
                kind == "float" and type(value) in {int, float} and math.isfinite(value)
            )
            or (kind in {"string", "text"} and isinstance(value, str))
            or (
                kind == "list"
                and isinstance(value, list)
                and all(isinstance(item, str) for item in value)
            )
        )
        if not valid:
            raise ValueError(f"「{label}」的格式不正确。")
        if field["options"] and value not in field["options"]:
            raise ValueError(f"请为「{label}」选择有效选项。")
        if kind in {"int", "float"}:
            bounds = field["bounds"]
            minimum = bounds.get("min", 0)
            maximum = bounds.get("max")
            if path.endswith(
                ("min_score", "min_meme_confidence", "min_category_confidence")
            ):
                maximum = 1
            if path.endswith(("top_k", ".timeout")):
                minimum = 1
            if value < minimum or (maximum is not None and value > maximum):
                raise ValueError(f"「{label}」超出允许范围。")
        if kind in {"string", "text"} and len(value) > 65536:
            raise ValueError(f"「{label}」内容过长。")
        if path.endswith((".url", ".public_url", ".github_accelerator_url")) and value:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"「{label}」需要填写完整的 HTTP 或 HTTPS 地址。")
        if path.endswith("content_cleanup_rule"):
            try:
                re.compile(value)
            except re.error as error:
                raise ValueError(f"「{label}」不是有效的正则表达式。") from error
        node = updated
        keys = path.split(".")
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = value
    return updated
