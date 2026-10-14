# -*- coding: utf-8 -*-
"""DeepSeek 接入自检：连通性 + 函数调用(tool calling) + 时延。

用法：
    .venv\\Scripts\\python.exe scripts\\check_llm.py

为什么必须单独验「函数调用」：
    Agent 编排层（Spec §6.1）依赖 LLM 输出结构化的 intent + slots。
    如果模型不支持或支持不好 function calling，就要退回「提示词约束 JSON 输出」，
    整条编排链路的实现方式会变。所以这一项必须在写编排层之前确认。

时延同样重要：
    Spec §9 要求读操作 ≤3s、飞书事件 3s 内必须返回。
    如果单次调用就逼近 3s，就必须走「占位消息 + PATCH 回填」的异步模式。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "config.yaml"

OK = "[OK]"
FAIL = "[FAIL]"
WARN = "[WARN]"


def load_llm() -> dict:
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    llm = cfg.get("llm") or {}
    if not llm.get("api_key"):
        raise SystemExit(f"{FAIL} config.yaml 里 llm.api_key 为空")
    return llm


def chat(llm: dict, payload_extra: dict, timeout: int = 60) -> tuple[dict | None, float]:
    url = f"{llm['base_url'].rstrip('/')}/chat/completions"
    body = {
        "model": llm.get("model", "deepseek-chat"),
        **payload_extra,
    }
    started = time.monotonic()
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {llm['api_key']}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        print(f"      {FAIL} 网络请求失败: {exc}")
        return None, time.monotonic() - started

    elapsed = time.monotonic() - started
    if resp.status_code != 200:
        print(f"      {FAIL} HTTP {resp.status_code}")
        print(f"      {resp.text[:400]}")
        return None, elapsed
    return resp.json(), elapsed


def test_basic(llm: dict) -> bool:
    print("\n[1/3] 基础对话")
    data, elapsed = chat(
        llm,
        {
            "messages": [
                {"role": "system", "content": "你是安防巡逻 Agent。回答极简。"},
                {"role": "user", "content": "一句话说明你能做什么，20字以内。"},
            ],
            "temperature": 0.2,
            "max_tokens": 64,
        },
    )
    if not data:
        return False

    msg = data["choices"][0]["message"]["content"].strip()
    usage = data.get("usage") or {}
    print(f"      {OK} 模型回复: {msg}")
    print(f"      时延 = {elapsed:.2f}s    tokens = {usage.get('total_tokens')}")
    if elapsed > 3:
        print(f"      {WARN} 单次调用已超 3s，编排层必须走异步回填（占位消息 + PATCH）")
    return True


def test_tool_calling(llm: dict) -> bool:
    """核心测试：能否稳定输出 tool_calls。"""
    print("\n[2/3] 函数调用（编排层地基）")

    tools = [
        {
            "type": "function",
            "function": {
                "name": "create_patrol_task",
                "description": "向巡逻机器人下发一次巡逻任务",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "route_id": {"type": "string", "description": "路线 ID"},
                        "start_at": {"type": "string", "description": "ISO8601 开始时间"},
                        "repeat": {"type": "string", "enum": ["once", "daily"], "description": "重复方式"},
                    },
                    "required": ["route_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_report",
                "description": "获取巡逻报表",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["daily", "weekly", "alerts"]},
                        "date": {"type": "string", "description": "YYYY-MM-DD"},
                    },
                    "required": ["kind"],
                },
            },
        },
    ]

    cases = [
        ("让机器人去巡逻一圈", "create_patrol_task"),
        ("昨天的巡逻报表给我看看", "get_report"),
    ]

    all_ok = True
    for utterance, expect in cases:
        data, elapsed = chat(
            llm,
            {
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "你是安防巡逻 Agent。根据用户意图调用合适的工具。"
                            "只调用工具，不要用自然语言回答。"
                        ),
                    },
                    {"role": "user", "content": utterance},
                ],
                "tools": tools,
                "tool_choice": "auto",
                "temperature": 0.0,
                "max_tokens": 256,
            },
        )
        if not data:
            all_ok = False
            continue

        msg = data["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            print(f"      {FAIL} 「{utterance}」未返回 tool_calls，而是: {str(msg.get('content'))[:80]}")
            all_ok = False
            continue

        call = calls[0]
        fn = call["function"]["name"]
        try:
            args = json.loads(call["function"]["arguments"])
        except ValueError:
            args = call["function"]["arguments"]

        flag = OK if fn == expect else WARN
        if fn != expect:
            all_ok = False
        print(f"      {flag} 「{utterance}」")
        print(f"           → {fn}({json.dumps(args, ensure_ascii=False)})   {elapsed:.2f}s")

    return all_ok


def test_json_slot_fill(llm: dict) -> bool:
    """备选方案验证：不支持 tool calling 时的兜底（强制 JSON 输出）。"""
    print("\n[3/3] 结构化槽位抽取（兜底方案）")
    data, elapsed = chat(
        llm,
        {
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "从用户话里抽取槽位，只输出 JSON，不要任何解释。"
                        '格式：{"intent": "...", "date": "YYYY-MM-DD 或 null", "route": "字符串或 null"}'
                    ),
                },
                {"role": "user", "content": "帮我看下昨天西侧通道那条遗留物告警"},
            ],
            "temperature": 0.0,
            "max_tokens": 128,
            "response_format": {"type": "json_object"},
        },
    )
    if not data:
        return False

    content = data["choices"][0]["message"]["content"].strip()
    try:
        parsed = json.loads(content)
        print(f"      {OK} JSON 解析成功: {json.dumps(parsed, ensure_ascii=False)}")
        print(f"      时延 = {elapsed:.2f}s")
        return True
    except ValueError:
        print(f"      {FAIL} 返回的不是合法 JSON: {content[:120]}")
        return False


def main() -> int:
    print("=" * 66)
    print("DeepSeek 接入自检")
    print("=" * 66)

    llm = load_llm()
    print(f"base_url = {llm['base_url']}")
    print(f"model    = {llm.get('model')}")
    print(f"api_key  = {llm['api_key'][:6]}...{llm['api_key'][-4:]}")

    results = {
        "基础对话": test_basic(llm),
        "函数调用": test_tool_calling(llm),
        "结构化JSON": test_json_slot_fill(llm),
    }

    print("\n" + "=" * 66)
    print("结论")
    print("=" * 66)
    for name, ok in results.items():
        print(f"  {OK if ok else FAIL} {name}")

    if all(results.values()):
        print("\n编排层可以按 Spec §6.1 设计：LLM 输出 intent + tool_calls。")
    elif results["函数调用"] is False and results["结构化JSON"]:
        print("\n函数调用不可靠 → 改用「提示词约束 JSON 输出」方案，编排层实现方式需调整。")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
