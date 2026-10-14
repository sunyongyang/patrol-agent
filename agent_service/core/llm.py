# -*- coding: utf-8 -*-
"""LLM 客户端（DeepSeek 官方 API，OpenAI 兼容协议）。

实测数据（scripts/check_llm.py）：
    基础对话 0.73s ／ 函数调用 0.64-1.05s ／ JSON 模式 0.97s
都在飞书事件 3 秒超时之内，但编排层仍按异步回填设计更稳。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import requests

from shared import config

log = logging.getLogger("agent_service.llm")


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self) -> None:
        cfg = config.section("llm")
        self.base_url = (cfg.get("base_url") or "https://api.deepseek.com").rstrip("/")
        self.api_key = cfg.get("api_key") or ""
        self.model = cfg.get("model") or "deepseek-chat"
        self.timeout = int(cfg.get("timeout_sec", 30))
        if not self.api_key:
            raise LLMError("config.yaml 里 llm.api_key 为空")

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_mode: bool = False,
    ) -> tuple[dict, float]:
        """返回 (message, 耗时秒)。message 含 content / tool_calls。"""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice or "auto"
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        started = time.monotonic()
        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise LLMError(f"调用 LLM 失败：{exc}") from exc
        elapsed = time.monotonic() - started

        if resp.status_code != 200:
            raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"LLM 返回结构异常：{json.dumps(data)[:300]}") from exc

        log.info(
            "LLM %.2fs tool_calls=%d",
            elapsed,
            len(message.get("tool_calls") or []),
        )
        return message, elapsed


_client: LLMClient | None = None


def client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
