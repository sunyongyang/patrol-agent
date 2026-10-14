# -*- coding: utf-8 -*-
"""统一配置访问。所有模块都从这里读，不要各自 yaml.safe_load。

用法：
    from shared import config
    feishu = config.section("feishu")
    port = config.get("agent_service.port", 8200)
"""

from __future__ import annotations

from typing import Any

import yaml

from shared import CONFIG_DIR

_CACHE: dict[str, Any] | None = None


def _load() -> dict[str, Any]:
    global _CACHE
    if _CACHE is None:
        path = CONFIG_DIR / "config.yaml"
        if not path.exists():
            raise FileNotFoundError(
                f"找不到 {path}\n请复制 config/config.example.yaml 为 config.yaml 并填写。"
            )
        _CACHE = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return _CACHE


def reload() -> dict[str, Any]:
    """测试或运行中改配置后强制重载。"""
    global _CACHE
    _CACHE = None
    return _load()


def section(name: str) -> dict[str, Any]:
    return _load().get(name) or {}


def get(dotted: str, default: Any = None) -> Any:
    node: Any = _load()
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def all_config() -> dict[str, Any]:
    return _load()
