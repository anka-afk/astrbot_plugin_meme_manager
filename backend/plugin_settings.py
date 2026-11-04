"""按配置结构编辑与 AstrBot 仪表盘共享的配置。"""

import copy
import hashlib
import json
import math
import re
from pathlib import Path
from urllib.parse import urlsplit

from .decision import (
    CLOUDFLARE_ACCOUNT_ID,
    CLOUDFLARE_MODELS,
    DecisionClient,
)


def describe_settings(config) -> tuple[dict, dict]:
    """读取共享文件并描述可编辑字段，不暴露密钥。

    Args:
        config: AstrBot 传入插件的 AstrBotConfig 实例。

    Returns:
        公开快照及用于合并的完整私有配置。

    Raises:
        ValueError: 配置文件或结构不可用、格式错误。
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
                "token",
                "account_id",
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
    """校验局部编辑，保留当前配置结构中的其他设置。

    Args:
        values: 共享配置文件的最新内容。
        fields: 由插件配置结构生成的可编辑字段描述。
        changes: 点分字段路径到明确修改值的映射。
        schema: 插件配置结构，包含空的信息分组。

    Returns:
        仅含配置结构已声明字段的完整新配置。

    Raises:
        ValueError: 路径、类型、选项、范围、URL 或正则表达式无效。
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
    # 先确定最终提供方，地址校验不依赖草稿字段的提交顺序。
    decision = updated.get("decision_service", {})
    provider = (
        str(
            changes.get("decision_service.provider", decision.get("provider"))
            or "systemone"
        )
        .strip()
        .casefold()
    )
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
            minimum = (
                None
                if path == "generation.emotion.max_memes_per_message"
                else bounds.get("min", 0)
            )
            maximum = bounds.get("max")
            if path.endswith(
                ("min_score", "min_meme_confidence", "min_category_confidence")
            ):
                maximum = 1
            if path.endswith(("top_k", ".timeout")):
                minimum = 1
            if (minimum is not None and value < minimum) or (
                maximum is not None and value > maximum
            ):
                raise ValueError(f"「{label}」超出允许范围。")
        if kind in {"string", "text"} and len(value) > 65536:
            raise ValueError(f"「{label}」内容过长。")
        if (
            path.endswith(
                (".url", ".public_url", ".github_accelerator_url", ".base_url")
            )
            and (path != "decision_service.base_url" or provider != "cloudflare")
            and value
        ):
            try:
                parsed = urlsplit(value)
                port = parsed.port
                valid_url = (
                    parsed.scheme in {"http", "https"}
                    and parsed.hostname
                    and (port is None or port > 0)
                )
                if path == "decision_service.base_url":
                    valid_url = (
                        valid_url
                        and not parsed.username
                        and not parsed.password
                        and not parsed.query
                        and not parsed.fragment
                        and not any(char.isspace() for char in value)
                    )
            except ValueError:
                valid_url = False
            if not valid_url:
                raise ValueError(f"「{label}」需要填写完整的 HTTP 或 HTTPS 地址。")
        if path == "decision_service.token" and any(
            ord(char) < 33 or ord(char) == 127 for char in value
        ):
            raise ValueError("决策服务密钥包含无效字符。")
        if path == "decision_service.model" and (
            len(value) > 128
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError("决策模型 ID 格式无效。")
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
    if provider == "cloudflare":
        account_id = str(decision.get("account_id") or "").strip()
        if account_id and not CLOUDFLARE_ACCOUNT_ID.fullmatch(account_id):
            raise ValueError("Cloudflare 账户 ID 需为 32 位十六进制字符。")
        model = str(decision.get("model") or "").strip()
        if model and model not in CLOUDFLARE_MODELS:
            raise ValueError("决策模型 ID 仅支持 clef、clef-flash、clef-omni。")
    selection = updated.get("generation", {}).get("emotion", {}).get("llm", {})
    collection = updated.get("auto_collect", {})
    decision_used = (
        selection.get("enabled") and selection.get("backend") == "decision"
    ) or (
        collection.get("enabled") and collection.get("judgment_backend") == "decision"
    )
    if decision_used:
        status = DecisionClient(updated.get("decision_service", {})).status()
        if not status["ready"]:
            reason = {
                "missing_base_url": "请填写决策服务基础地址。",
                "missing_token": "请填写决策服务密钥。",
                "missing_model": "请填写决策模型 ID。",
                "missing_account_id": "请填写 Cloudflare 账户 ID。",
                "invalid_account_id": "Cloudflare 账户 ID 需为 32 位十六进制字符。",
                "unsupported_model": "决策模型 ID 仅支持 clef、clef-flash、clef-omni。",
                "invalid_provider": "决策服务提供方仅支持 systemone 或 cloudflare。",
            }.get(status["reason"], "请检查决策服务配置。")
            raise ValueError(reason)
    return updated
