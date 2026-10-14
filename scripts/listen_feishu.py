# -*- coding: utf-8 -*-
"""飞书通道全链路验证（长连接）。

对应文档：91_实验项目/agent_patrol/飞书开放平台配置清单.md 步骤 8

用法：
    python scripts/listen_feishu.py
然后给你的机器人发一条私聊消息。

本脚本会依次验证 Demo 需要的全部通道能力：

    1. 长连接建立          （事件订阅方式=长连接）
    2. 接收消息事件        （im.message.receive_v1）
    3. 发送文本消息        （im:message:send_as_bot）
    4. 上传并发送图片      （im:resource）
    5. 发送交互卡片
    6. 卡片按钮回调        （card.action.trigger）
    7. 卡片就地更新        （PATCH message）

任何一步失败都会打印明确的 [FAIL] 与飞书返回的原始错误码。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

import lark_oapi as lark
import requests
import yaml

BASE = "https://open.feishu.cn/open-apis"
ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "config.yaml"
CONTACT_PATH = ROOT / "logs" / "feishu_contacts.json"

OK = "[OK]"
FAIL = "[FAIL]"
INFO = "[..]"

# ---------------------------------------------------------------- 配置与工具


def load_config() -> dict:
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return cfg.get("feishu") or {}


CFG = load_config()
APP_ID = (CFG.get("app_id") or "").strip()
APP_SECRET = (CFG.get("app_secret") or "").strip()

_token_cache: dict[str, object] = {"value": None, "expire_at": 0.0}


def get_token(force: bool = False) -> str:
    """取 tenant_access_token，带本地缓存。"""
    now = time.time()
    if not force and _token_cache["value"] and now < float(_token_cache["expire_at"]):
        return str(_token_cache["value"])

    resp = requests.post(
        f"{BASE}/auth/v3/app_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET},
        timeout=15,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"取 token 失败: {json.dumps(data, ensure_ascii=False)}")

    token = data["tenant_access_token"]
    _token_cache["value"] = token
    _token_cache["expire_at"] = now + int(data.get("expire", 7200)) - 300
    return token


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_token()}"}


def api_error(resp: requests.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}: {resp.text[:200]}"
    return f"code={body.get('code')} msg={body.get('msg')}"


def make_test_image() -> bytes:
    """生成一张带时间戳的测试图，验证图片上传与发送。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (640, 360), (18, 32, 56))
    draw = ImageDraw.Draw(img)
    draw.rectangle([12, 12, 628, 348], outline=(0, 200, 160), width=3)
    draw.text((40, 60), "PATROL AGENT", fill=(0, 220, 180))
    draw.text((40, 100), "R-01  camera channel test", fill=(230, 230, 230))
    draw.text((40, 160), f"point : P1 / East corridor", fill=(200, 200, 200))
    draw.text((40, 190), f"time  : {datetime.now():%Y-%m-%d %H:%M:%S}", fill=(200, 200, 200))
    draw.text((40, 250), "detector : YOLO (real inference)", fill=(150, 200, 255))
    draw.text((40, 280), "this image verifies im:resource", fill=(255, 200, 120))

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- 发送侧


def send_text(chat_id: str, text: str) -> str | None:
    resp = requests.post(
        f"{BASE}/im/v1/messages",
        headers=auth_headers(),
        params={"receive_id_type": "chat_id"},
        json={
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
        timeout=15,
    )
    if resp.status_code != 200 or resp.json().get("code") != 0:
        print(f"      {FAIL} 发送文本失败: {api_error(resp)}")
        return None
    return resp.json()["data"]["message_id"]


def upload_image(png: bytes) -> str | None:
    resp = requests.post(
        f"{BASE}/im/v1/images",
        headers=auth_headers(),
        data={"image_type": "message"},
        files={"image": ("patrol_test.png", png, "image/png")},
        timeout=30,
    )
    if resp.status_code != 200 or resp.json().get("code") != 0:
        print(f"      {FAIL} 上传图片失败: {api_error(resp)}")
        print("           若提示无权限，请检查是否已申请 im:resource")
        return None
    return resp.json()["data"]["image_key"]


def send_image(chat_id: str, image_key: str) -> str | None:
    resp = requests.post(
        f"{BASE}/im/v1/messages",
        headers=auth_headers(),
        params={"receive_id_type": "chat_id"},
        json={
            "receive_id": chat_id,
            "msg_type": "image",
            "content": json.dumps({"image_key": image_key}),
        },
        timeout=15,
    )
    if resp.status_code != 200 or resp.json().get("code") != 0:
        print(f"      {FAIL} 发送图片失败: {api_error(resp)}")
        return None
    return resp.json()["data"]["message_id"]


def build_verify_card(via: str | None = None) -> dict:
    """via: None=待点击的蓝卡 / 'response'=回调响应更新 / 'patch'=PATCH API 更新。"""
    if via:
        if via == "patch":
            detail = (
                "**✅ 最终状态由 PATCH API 写入**\n"
                "这说明两种就地更新机制都可用。\n\n"
                "告警卡「确认告警 / 标记误报」用回调响应即可；\n"
                "而**异步**结果回填（巡逻完成、报表生成）必须走 PATCH API——"
                "因为那时已经没有回调响应可用了。"
            )
        else:
            detail = (
                "**卡片已就地更新（回调响应方式）**\n"
                "本卡片是被更新而非新发的一条消息。"
            )
        return {
            "config": {"wide_screen_mode": True, "update_multi": True},
            "header": {
                "template": "green",
                "title": {"tag": "plain_text", "content": "✅ 巡逻 Agent 通道验证 · 全部通过"},
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": detail}},
                {"tag": "hr"},
                {
                    "tag": "note",
                    "elements": [
                        {"tag": "plain_text", "content": f"更新于 {datetime.now():%Y-%m-%d %H:%M:%S}"}
                    ],
                },
            ],
        }

    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "🔧 巡逻 Agent 通道验证"},
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        "**最后一步：卡片按钮回调**\n"
                        "请点击下方按钮。点击后本卡片应当**就地变成绿色**。\n\n"
                        "若按钮点了没反应 → 说明 `card.action.trigger` 回调未订阅。"
                    ),
                },
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "点我验证回调"},
                        "type": "primary",
                        "value": {"action": "verify_callback"},
                    }
                ],
            },
        ],
    }


def send_card(chat_id: str, card: dict) -> str | None:
    resp = requests.post(
        f"{BASE}/im/v1/messages",
        headers=auth_headers(),
        params={"receive_id_type": "chat_id"},
        json={
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        },
        timeout=15,
    )
    if resp.status_code != 200 or resp.json().get("code") != 0:
        print(f"      {FAIL} 发送卡片失败: {api_error(resp)}")
        return None
    return resp.json()["data"]["message_id"]


def patch_card(message_id: str, card: dict) -> bool:
    resp = requests.patch(
        f"{BASE}/im/v1/messages/{message_id}",
        headers=auth_headers(),
        json={"content": json.dumps(card, ensure_ascii=False)},
        timeout=15,
    )
    if resp.status_code != 200 or resp.json().get("code") != 0:
        print(f"      {FAIL} 更新卡片失败: {api_error(resp)}")
        return False
    return True


# ---------------------------------------------------------------- 会话状态


def save_contact(open_id: str, chat_id: str, name: str = "") -> None:
    CONTACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if CONTACT_PATH.exists():
        try:
            data = json.loads(CONTACT_PATH.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
    data[open_id] = {
        "open_id": open_id,
        "chat_id": chat_id,
        "sender_type": name,
        "first_seen": data.get(open_id, {}).get("first_seen", datetime.now().isoformat(timespec="seconds")),
        "last_seen": datetime.now().isoformat(timespec="seconds"),
    }
    CONTACT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# 记录待更新的验证卡片：chat_id -> message_id
pending_cards: dict[str, str] = {}


# ---------------------------------------------------------------- 事件处理


def handle_message(data: "lark.im.v1.P2ImMessageReceiveV1") -> None:
    try:
        event = data.event
        msg = event.message
        sender = event.sender

        # 注意：lark-oapi 的 EventMessage 字段名是 message_type，不是 msg_type
        chat_id = getattr(msg, "chat_id", "") or ""
        chat_type = getattr(msg, "chat_type", "") or ""
        msg_type = getattr(msg, "message_type", None) or getattr(msg, "msg_type", "") or ""
        message_id = getattr(msg, "message_id", "") or ""
        sender_id = getattr(sender, "sender_id", None) if sender else None
        open_id = (getattr(sender_id, "open_id", "") if sender_id else "") or ""

        try:
            text = json.loads(msg.content or "{}").get("text", "")
        except ValueError:
            text = "<非文本>"

        print(f"\n{'=' * 66}")
        print(f"{OK} 收到消息  {datetime.now():%Y-%m-%d %H:%M:%S}")
        print(f"{'=' * 66}")
        print(f"      chat_id     = {chat_id}")
        print(f"      chat_type   = {chat_type}   (p2p=私聊, group=群聊)")
        print(f"      msg_type    = {msg_type}")
        print(f"      message_id  = {message_id}")
        sender_type = getattr(sender, "sender_type", "") or ""
        print(f"      sender open_id = {open_id}")
        print(f"      sender_type = {sender_type}")
        print(f"      内容        = {text!r}")

        if open_id:
            save_contact(open_id, chat_id, sender_type)
            print(f"      {OK} 已记录联系人 -> logs/feishu_contacts.json")

        print(f"\n{INFO} 开始回执验证…")

        # 2. 发送文本
        if send_text(chat_id, "✅ 1/4 通道已通：文本消息接收与发送正常。\n正在继续验证图片…"):
            print(f"      {OK} 发送文本成功")

        # 3. 上传并发送图片
        png = make_test_image()
        image_key = upload_image(png)
        if image_key:
            print(f"      {OK} 上传图片成功 image_key={image_key[:20]}…")
            if send_image(chat_id, image_key):
                print(f"      {OK} 发送图片成功（im:resource 正常）")

        # 4. 发送卡片
        card_id = send_card(chat_id, build_verify_card())
        if card_id:
            pending_cards[chat_id] = card_id
            print(f"      {OK} 发送交互卡片成功 message_id={card_id}")
            print(f"\n{INFO} 请在飞书里点击卡片上的「点我验证回调」按钮。")

    except Exception as exc:  # noqa: BLE001 - 验证脚本，尽量不因异常中断长连接
        print(f"      {FAIL} 处理消息时异常: {type(exc).__name__}: {exc}")
        import traceback  # noqa: PLC0415

        traceback.print_exc()
        try:
            print("      --- 原始事件（便于核对字段名） ---")
            print(lark.JSON.marshal(data, indent=2))
        except Exception:  # noqa: BLE001
            pass


def handle_card_action(data) -> "P2CardActionTriggerResponse":
    """卡片按钮回调：必须快速返回，耗时操作丢线程。"""
    from lark_oapi.event.callback.model.p2_card_action_trigger import (  # noqa: PLC0415
        P2CardActionTriggerResponse,
    )

    response_card = build_verify_card(via="response")

    try:
        event = data.event
        action = getattr(event, "action", None)
        value = getattr(action, "value", None) or {}
        context = getattr(event, "context", None)
        # 注意：回调模型里的字段是 open_chat_id / open_message_id，不是 chat_id
        chat_id = (getattr(context, "open_chat_id", "") if context else "") or ""
        card_message_id = (getattr(context, "open_message_id", "") if context else "") or ""
        operator_obj = getattr(event, "operator", None)
        operator = getattr(operator_obj, "open_id", "") if operator_obj else ""

        print(f"\n{'=' * 66}")
        print(f"{OK} 收到卡片按钮回调（card.action.trigger 已验证）")
        print(f"{'=' * 66}")
        print(f"      action.value     = {json.dumps(value, ensure_ascii=False)}")
        print(f"      open_chat_id     = {chat_id}")
        print(f"      open_message_id  = {card_message_id}")
        print(f"      operator         = {operator}")

        def update() -> None:
            target = card_message_id or pending_cards.get(chat_id, "")
            if not target:
                print(f"      {FAIL} 拿不到待更新的卡片 message_id，PATCH 无法验证")
                return
            # 稍等，让回调响应的卡片先落地，再看 PATCH 能否覆盖成功
            time.sleep(1.5)
            if patch_card(target, build_verify_card(via="patch")):
                print(f"      {OK} PATCH API 就地更新成功（异步回填可用）")
                print(f"\n{'=' * 66}")
                print(f"{OK} 全部通道能力验证通过（含 PATCH 异步更新）")
                print(f"{'=' * 66}")
            else:
                print(f"      {FAIL} PATCH API 更新失败 —— 异步回填（巡逻完成推送）会受影响")

        threading.Thread(target=update, daemon=True).start()

    except Exception as exc:  # noqa: BLE001
        print(f"      {FAIL} 处理卡片回调异常: {type(exc).__name__}: {exc}")
        import traceback  # noqa: PLC0415

        traceback.print_exc()

    # 方式一：回调响应里直接返回新卡片（同步就地更新，无需额外 API 调用）
    return P2CardActionTriggerResponse(
        {
            "toast": {"type": "success", "content": "回调已收到，正在更新卡片…"},
            "card": {"type": "card_json", "data": json.dumps(response_card, ensure_ascii=False)},
        }
    )


# ---------------------------------------------------------------- 主流程


def main() -> int:
    print("=" * 66)
    print("飞书通道全链路验证（长连接模式）")
    print("=" * 66)
    print(f"app_id = {APP_ID}")

    if not APP_ID or not APP_SECRET:
        print(f"{FAIL} config.yaml 缺少 feishu.app_id / app_secret")
        return 1

    print(f"{INFO} 预热凭证…")
    try:
        get_token(force=True)
        print(f"{OK} 凭证有效，长连接即将建立")
    except Exception as exc:  # noqa: BLE001
        print(f"{FAIL} {exc}")
        return 1

    builder = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(
        handle_message
    )
    try:
        builder = builder.register_p2_card_action_trigger(handle_card_action)
        print(f"{OK} 已注册卡片回调处理器")
    except Exception as exc:  # noqa: BLE001
        print(f"{FAIL} 注册卡片回调失败: {type(exc).__name__}: {exc}")
        print("     卡片按钮将无法验证（不影响其余项目）")

    handler = builder.build()

    cli = lark.ws.Client(
        APP_ID,
        APP_SECRET,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )

    print(f"\n{INFO} 正在建立 WebSocket 长连接…")
    print(f"{INFO} 看到 connected to wss:// 即建连成功")
    print(f"\n>>> 现在请打开飞书，给你的机器人「巡逻agent」发一条私聊消息 <<<\n")

    try:
        cli.start()
    except KeyboardInterrupt:
        print(f"\n{INFO} 已手动停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
