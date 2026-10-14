# -*- coding: utf-8 -*-
"""飞书凭证与机器人能力自检（离线检查，不需要长连接）。

对应文档：91_实验项目/agent_patrol/飞书开放平台配置清单.md 步骤 8

用法：
    python scripts/check_feishu.py

检查三项：
    1. App ID / App Secret 能否换取 tenant_access_token（凭证是否有效）
    2. 机器人能力是否已开启、机器人名称与启用状态
    3. 应用是否可访问开放平台接口

注意：本脚本无法验证「事件订阅」与「卡片回调」——那两项必须靠真实收发消息验证，
见 scripts/listen_feishu.py。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

import requests
import yaml

BASE = "https://open.feishu.cn/open-apis"
ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "config.yaml"

OK = "[OK]"
FAIL = "[FAIL]"
WARN = "[WARN]"


def load_credentials() -> tuple[str, str]:
    if not CONFIG_PATH.exists():
        sys.exit(f"{FAIL} 找不到配置文件：{CONFIG_PATH}")
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    feishu = cfg.get("feishu") or {}
    app_id = (feishu.get("app_id") or "").strip()
    app_secret = (feishu.get("app_secret") or "").strip()
    if not app_id or not app_secret:
        sys.exit(f"{FAIL} config.yaml 里 feishu.app_id / app_secret 为空")
    return app_id, app_secret


def mask(value: str, keep: int = 4) -> str:
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * 8}{value[-keep:]}"


def pretty_error(resp: requests.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    return json.dumps(body, ensure_ascii=False, indent=2)


def check_token(app_id: str, app_secret: str) -> str | None:
    """第 1 项：换取 tenant_access_token。"""
    print(f"\n[1/3] 校验凭证并获取 tenant_access_token")
    print(f"      app_id = {app_id}")
    url = f"{BASE}/auth/v3/app_access_token/internal"
    try:
        resp = requests.post(
            url,
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=15,
        )
    except requests.RequestException as exc:
        print(f"      {FAIL} 网络请求失败：{exc}")
        print("      请确认这台机器能访问 open.feishu.cn")
        return None

    if resp.status_code != 200:
        print(f"      {FAIL} HTTP {resp.status_code}")
        print(pretty_error(resp))
        return None

    data = resp.json()
    if data.get("code") != 0:
        print(f"      {FAIL} code={data.get('code')} msg={data.get('msg')}")
        print("      常见原因：App ID / App Secret 填错，或应用已被停用")
        return None

    token = data.get("tenant_access_token")
    expire = data.get("expire")
    print(f"      {OK} 凭证有效")
    print(f"      token  = {mask(token or '')}")
    print(f"      有效期 = {expire} 秒")
    return token


def check_bot(token: str) -> None:
    """第 2 项：查询机器人信息，验证机器人能力是否已开启。"""
    print(f"\n[2/3] 查询机器人能力")
    url = f"{BASE}/bot/v3/info"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
    except requests.RequestException as exc:
        print(f"      {FAIL} 网络请求失败：{exc}")
        return

    if resp.status_code != 200:
        print(f"      {FAIL} HTTP {resp.status_code}")
        print(pretty_error(resp))
        return

    data = resp.json()
    if data.get("code") != 0:
        print(f"      {FAIL} code={data.get('code')} msg={data.get('msg')}")
        print("      若提示无权限/能力未开启，请回到「添加应用能力」开启【机器人】")
        return

    bot = data.get("bot") or {}
    activate_status = bot.get("activate_status")
    # activate_status: 0=初始化 1=禁用 2=启用 3=未启用 4=未启用
    status_map = {0: "初始化", 1: "已禁用", 2: "已启用", 3: "未启用", 4: "未启用"}
    status_text = status_map.get(activate_status, f"未知({activate_status})")

    print(f"      {OK} 机器人能力已开启")
    print(f"      机器人名称   = {bot.get('app_name')}")
    print(f"      bot open_id  = {bot.get('open_id')}")
    print(f"      启用状态     = {status_text}")
    if activate_status != 2:
        print(f"      {WARN} 机器人当前不是「已启用」状态，可能尚未发布版本或可用范围不含你")


def check_im_reachable(token: str) -> None:
    """第 3 项：探测 IM 接口可达性（用无害的只读调用，仅判断连通与鉴权）。"""
    print(f"\n[3/3] 探测 IM 接口可达性")
    url = f"{BASE}/im/v1/chats"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params={"page_size": 1},
            timeout=15,
        )
    except requests.RequestException as exc:
        print(f"      {FAIL} 网络请求失败：{exc}")
        return

    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    code = data.get("code")
    if code == 0:
        items = (data.get("data") or {}).get("items") or []
        print(f"      {OK} 接口可达，机器人已在 {len(items)} 个群（本探测只取 1 条）")
    else:
        # 99991672 / 99991679 一类是「未申请该权限」，属预期——本 Demo 不需要 im:chat
        print(f"      {WARN} 未获得群列表权限（code={code}），这是预期的")
        print(f"           本 Demo 不需要 im:chat:readonly，此项不代表配置有问题")


def main() -> int:
    print("=" * 66)
    print("飞书凭证与机器人能力自检")
    print("=" * 66)

    app_id, app_secret = load_credentials()
    token = check_token(app_id, app_secret)
    if not token:
        print("\n" + "=" * 66)
        print(f"{FAIL} 凭证校验未通过，后续检查跳过。")
        print("=" * 66)
        return 1

    check_bot(token)
    check_im_reachable(token)

    print("\n" + "=" * 66)
    print("自检结论")
    print("=" * 66)
    print("以上三项只覆盖「凭证 + 机器人能力 + 网络可达」。")
    print("事件订阅 / 卡片回调 / 收发消息 必须靠真实消息验证：")
    print("    python scripts/listen_feishu.py")
    print("然后给机器人发一条私聊消息。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
