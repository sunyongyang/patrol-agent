# -*- coding: utf-8 -*-
"""飞书适配器（Spec §7.1）。

通道能力已实测通过（见 scripts/listen_feishu.py 的验证结果）：
    长连接 / 收消息 / 发文本 / 上传图片 / 发图片 / 发卡片 / 按钮回调 / PATCH 更新

两个关键实现约束：
1. **3 秒返回**：飞书要求事件处理 3s 内完成且不抛异常，否则触发超时重试。
   所以文本消息一律丢后台线程处理；卡片回调因为只做数据库更新，可以同步返回。
2. **去重用 message_id，不要用 event_id**（官方明确要求）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from pathlib import Path

import lark_oapi as lark
import requests

from agent_service.core import orchestrator
from agent_service.im.base import FEISHU_CAPS, Capabilities
from shared import LOG_DIR, config, repo

log = logging.getLogger("agent_service.feishu")

BASE_FEISHU = "https://open.feishu.cn/open-apis"
BASE_LARK = "https://open.larksuite.com/open-apis"

CONTACT_PATH = LOG_DIR / "feishu_contacts.json"


class FeishuAdapter:
    channel = "feishu"

    def __init__(self) -> None:
        cfg = config.section("feishu")
        self.app_id = (cfg.get("app_id") or "").strip()
        self.app_secret = (cfg.get("app_secret") or "").strip()
        domain = (cfg.get("domain") or "feishu").lower()
        self.base = BASE_LARK if domain == "lark" else BASE_FEISHU
        if not self.app_id or not self.app_secret:
            raise RuntimeError("config.yaml 缺少 feishu.app_id / app_secret")

        self._token: str | None = None
        self._token_expire = 0.0
        self._chat_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._seen_messages: dict[str, float] = {}
        self._seen_actions: dict[str, float] = {}
        self._ws: lark.ws.Client | None = None
        self._stop = threading.Event()

    def capabilities(self) -> Capabilities:
        return FEISHU_CAPS

    # ------------------------------------------------------------ 凭证

    def token(self, force: bool = False) -> str:
        now = time.time()
        if not force and self._token and now < self._token_expire:
            return self._token
        resp = requests.post(
            f"{self.base}/auth/v3/app_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=15,
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"取 token 失败: {json.dumps(data, ensure_ascii=False)}")
        self._token = data["tenant_access_token"]
        self._token_expire = now + int(data.get("expire", 7200)) - 300
        return self._token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}"}

    @staticmethod
    def _err(resp: requests.Response) -> str:
        try:
            body = resp.json()
            return f"code={body.get('code')} msg={body.get('msg')}"
        except ValueError:
            return f"HTTP {resp.status_code}: {resp.text[:200]}"

    # ------------------------------------------------------------ 发送

    def send_text(self, chat_id: str, text: str) -> str | None:
        resp = requests.post(
            f"{self.base}/im/v1/messages",
            headers=self._headers(),
            params={"receive_id_type": "chat_id"},
            json={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
            timeout=15,
        )
        if resp.status_code != 200 or resp.json().get("code") != 0:
            log.error("发送文本失败: %s", self._err(resp))
            return None
        return resp.json()["data"]["message_id"]

    def send_card(self, chat_id: str, card: dict) -> str | None:
        resp = requests.post(
            f"{self.base}/im/v1/messages",
            headers=self._headers(),
            params={"receive_id_type": "chat_id"},
            json={
                "receive_id": chat_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
            timeout=15,
        )
        if resp.status_code != 200 or resp.json().get("code") != 0:
            log.error("发送卡片失败: %s", self._err(resp))
            return None
        return resp.json()["data"]["message_id"]

    def update_card(self, message_id: str, card: dict) -> bool:
        resp = requests.patch(
            f"{self.base}/im/v1/messages/{message_id}",
            headers=self._headers(),
            json={"content": json.dumps(card, ensure_ascii=False)},
            timeout=15,
        )
        if resp.status_code != 200 or resp.json().get("code") != 0:
            log.error("更新卡片 %s 失败: %s", message_id, self._err(resp))
            return False
        return True

    def upload_image(self, path: str) -> str | None:
        p = Path(path)
        if not p.exists():
            log.warning("图片不存在，跳过上传: %s", path)
            return None
        with p.open("rb") as fh:
            resp = requests.post(
                f"{self.base}/im/v1/images",
                headers=self._headers(),
                data={"image_type": "message"},
                files={"image": (p.name, fh, "image/jpeg")},
                timeout=30,
            )
        if resp.status_code != 200 or resp.json().get("code") != 0:
            log.error("上传图片失败: %s", self._err(resp))
            return None
        return resp.json()["data"]["image_key"]

    def send_image(self, chat_id: str, path: str) -> str | None:
        key = self.upload_image(path)
        if not key:
            return None
        resp = requests.post(
            f"{self.base}/im/v1/messages",
            headers=self._headers(),
            params={"receive_id_type": "chat_id"},
            json={
                "receive_id": chat_id,
                "msg_type": "image",
                "content": json.dumps({"image_key": key}),
            },
            timeout=15,
        )
        if resp.status_code != 200 or resp.json().get("code") != 0:
            log.error("发送图片失败: %s", self._err(resp))
            return None
        return resp.json()["data"]["message_id"]

    def upload_file(self, path: str, file_type: str = "stream") -> str | None:
        """上传文件，返回 file_key。CSV/Excel 用 file_type=stream。"""
        p = Path(path)
        if not p.exists():
            log.warning("文件不存在，跳过上传: %s", path)
            return None
        mime = {
            ".csv": "text/csv",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".pdf": "application/pdf",
            ".txt": "text/plain",
        }.get(p.suffix.lower(), "application/octet-stream")
        with p.open("rb") as fh:
            resp = requests.post(
                f"{self.base}/im/v1/files",
                headers=self._headers(),
                data={"file_type": file_type, "file_name": p.name},
                files={"file": (p.name, fh, mime)},
                timeout=60,
            )
        if resp.status_code != 200 or resp.json().get("code") != 0:
            log.error("上传文件失败: %s", self._err(resp))
            return None
        return resp.json()["data"]["file_key"]

    def send_file(self, chat_id: str, path: str, file_type: str = "stream") -> str | None:
        key = self.upload_file(path, file_type)
        if not key:
            return None
        resp = requests.post(
            f"{self.base}/im/v1/messages",
            headers=self._headers(),
            params={"receive_id_type": "chat_id"},
            json={
                "receive_id": chat_id,
                "msg_type": "file",
                "content": json.dumps({"file_key": key}),
            },
            timeout=30,
        )
        if resp.status_code != 200 or resp.json().get("code") != 0:
            log.error("发送文件失败: %s", self._err(resp))
            return None
        return resp.json()["data"]["message_id"]

    def reply_text(self, message_id: str, text: str) -> str | None:
        """回复某条消息（引用形式）。"""
        resp = requests.post(
            f"{self.base}/im/v1/messages/{message_id}/reply",
            headers=self._headers(),
            json={"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)},
            timeout=15,
        )
        if resp.status_code != 200 or resp.json().get("code") != 0:
            log.error("回复消息失败: %s", self._err(resp))
            return None
        return resp.json()["data"]["message_id"]

    # ------------------------------------------------------------ 联系人

    @staticmethod
    def known_contacts() -> dict:
        if not CONTACT_PATH.exists():
            return {}
        try:
            return json.loads(CONTACT_PATH.read_text(encoding="utf-8"))
        except ValueError:
            return {}

    def broadcast_card(self, card: dict) -> dict[str, str]:
        """向所有已知联系人推送卡片，返回 {chat_id: message_id}。

        返回 message_id 是必要的：告警被抑制时要把**原卡片**就地刷新，
        没有 message_id 就只能再发一条新消息，抑制就白做了。
        """
        sent: dict[str, str] = {}
        for open_id, info in self.known_contacts().items():
            chat_id = info.get("chat_id")
            if not chat_id:
                continue
            mid = self.send_card(chat_id, card)
            if mid:
                sent[chat_id] = mid
                log.info("已推送给 %s（chat_id=%s, message_id=%s）", open_id, chat_id, mid)
        if not sent:
            log.warning("没有已知联系人可推送 —— 请先用飞书给机器人发一条消息")
        return sent

    # ------------------------------------------------------------ 消息投递

    def deliver(self, chat_id: str, replies: list[orchestrator.Reply]) -> None:
        """把编排结果发出去，并记录告警卡的 message_id 供后续就地更新。"""
        for r in replies:
            if r.kind == "text" and r.text:
                self.send_text(chat_id, r.text)
            elif r.kind == "card" and r.card:
                mid = self.send_card(chat_id, r.card)
                if mid and r.remember_as_alert_card:
                    repo.mark_alert_pushed(r.remember_as_alert_card, mid)
            elif r.kind == "image" and r.image_path:
                self.send_image(chat_id, r.image_path)
                if r.caption:
                    self.send_text(chat_id, r.caption)
            elif r.kind == "file" and r.file_path:
                mid = self.send_file(chat_id, r.file_path)
                if r.caption:
                    self.send_text(chat_id, r.caption)
                if not mid:
                    self.send_text(chat_id, "⚠️ 文件发送失败，可稍后重试或先看列表结果。")

    # ------------------------------------------------------------ 事件处理

    def _on_message(self, data) -> None:
        """收到消息：**立即返回**，耗时逻辑丢线程（3 秒硬约束）。"""
        try:
            event = data.event
            msg = event.message
            sender = event.sender

            message_id = getattr(msg, "message_id", "") or ""
            # 去重用 message_id —— 官方明确不要依赖 event_id
            now = time.time()
            if message_id in self._seen_messages:
                log.info("重复投递，忽略 %s", message_id)
                return
            self._seen_messages[message_id] = now
            if len(self._seen_messages) > 500:
                for k, v in list(self._seen_messages.items()):
                    if now - v > 3600:
                        self._seen_messages.pop(k, None)

            chat_id = getattr(msg, "chat_id", "") or ""
            chat_type = getattr(msg, "chat_type", "") or ""
            msg_type = getattr(msg, "message_type", None) or getattr(msg, "msg_type", "") or ""
            sender_id = getattr(sender, "sender_id", None) if sender else None
            open_id = (getattr(sender_id, "open_id", "") if sender_id else "") or ""

            if msg_type != "text":
                # 非文本消息：直接告知不支持，避免用户以为机器人坏了
                threading.Thread(
                    target=self.send_text,
                    args=(chat_id, f"目前只认文字消息（收到的是 {msg_type}）。试试「昨天的巡逻报表」？"),
                    daemon=True,
                ).start()
                return

            try:
                text = json.loads(getattr(msg, "content", "") or "{}").get("text", "")
            except ValueError:
                text = ""
            text = (text or "").strip()
            if not text:
                return

            self._remember_contact(open_id, chat_id, chat_type)
            repo.log_message("feishu", chat_id, open_id, "in", {"text": text})
            log.info("收到消息 chat=%s user=%s: %s", chat_id, open_id, text[:80])

            threading.Thread(
                target=self._process_text,
                args=(chat_id, open_id, text),
                daemon=True,
            ).start()
        except Exception:  # noqa: BLE001
            log.exception("处理消息事件异常（已吞掉，避免飞书重试）")

    def _process_text(self, chat_id: str, open_id: str, text: str) -> None:
        lock = self._chat_locks[chat_id]
        if not lock.acquire(blocking=False):
            self.send_text(chat_id, "上一条还在处理中，稍等一下…")
            return
        try:
            replies = orchestrator.handle_text(chat_id, open_id, text)
            self.deliver(chat_id, replies)
            repo.log_message("feishu", chat_id, open_id, "out",
                             {"count": len(replies)})
        except Exception as exc:  # noqa: BLE001
            log.exception("编排失败")
            self.send_text(chat_id, f"⚠️ 处理出错：{type(exc).__name__}: {exc}")
        finally:
            lock.release()

    def _on_card_action(self, data):
        """卡片按钮回调。

        ⚠️ 关键约束：飞书要求 3 秒内返回，且长连接是**单线程处理事件**的。
        最初这里做成同步执行，结果任何超过 3 秒的动作都会：
            1) 触发飞书超时重试
            2) 堵住后续所有消息事件（表现为「点卡片很慢 / 没有后续」）
        所以现在只回一个 toast 就立刻返回，真正干活丢后台线程，
        干完再用 PATCH 就地更新原卡片。
        """
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        try:
            event = data.event
            action = getattr(event, "action", None)
            value = getattr(action, "value", None) or {}
            context = getattr(event, "context", None)
            # 注意字段名是 open_chat_id / open_message_id
            chat_id = (getattr(context, "open_chat_id", "") if context else "") or ""
            card_message_id = (getattr(context, "open_message_id", "") if context else "") or ""
            operator_obj = getattr(event, "operator", None)
            open_id = getattr(operator_obj, "open_id", "") if operator_obj else ""

            # 幂等：飞书会重试回调，同一个 (卡片, 操作人, 动作) 短时间内只处理一次
            dedup_key = (
                f"{card_message_id}|{open_id}|"
                f"{json.dumps(value, sort_keys=True, ensure_ascii=False)}"
            )
            now = time.time()
            if now - self._seen_actions.get(dedup_key, 0.0) < 20:
                log.info("重复的卡片回调，已忽略: %s", dedup_key[:80])
                return P2CardActionTriggerResponse(
                    {"toast": {"type": "info", "content": "这个操作已经在处理了"}}
                )
            self._seen_actions[dedup_key] = now
            if len(self._seen_actions) > 500:
                for k, v in list(self._seen_actions.items()):
                    if now - v > 300:
                        self._seen_actions.pop(k, None)

            repo.log_message("feishu", chat_id, open_id, "in", {"card_action": value})
            threading.Thread(
                target=self._process_card_action,
                args=(chat_id, open_id, card_message_id, value),
                daemon=True,
            ).start()

            return P2CardActionTriggerResponse(
                {"toast": {"type": "info", "content": "处理中…"}}
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("卡片回调入口异常")
            return P2CardActionTriggerResponse(
                {"toast": {"type": "error", "content": f"出错了：{exc}"}}
            )

    def _process_card_action(
        self, chat_id: str, open_id: str, card_message_id: str, value: dict
    ) -> None:
        """后台执行卡片动作，完成后就地更新原卡片。"""
        started = time.monotonic()
        act = (value or {}).get("act") or "?"
        with self._chat_locks[chat_id]:
            try:
                result = orchestrator.handle_card_action(chat_id, open_id, value)
            except Exception as exc:  # noqa: BLE001
                log.exception("卡片动作 %s 处理失败", act)
                from agent_service.report import feishu_card as fc

                result = orchestrator.ActionResult(
                    response_card=fc.result_card(
                        "⚠️ 操作未完成", f"{type(exc).__name__}: {exc}", ok=False
                    ),
                    toast="出错",
                )

            # 就地更新原卡片；更新失败就退回发新消息，至少让用户看到结果
            if result.response_card:
                if card_message_id and self.update_card(card_message_id, result.response_card):
                    pass
                else:
                    self.send_card(chat_id, result.response_card)

            if result.replies:
                self.deliver(chat_id, result.replies)

        log.info(
            "卡片动作 %s 处理完成，耗时 %.2fs（就地更新=%s）",
            act,
            time.monotonic() - started,
            bool(result.response_card and card_message_id),
        )

    def _remember_contact(self, open_id: str, chat_id: str, chat_type: str) -> None:
        if not open_id or not chat_id:
            return
        data = self.known_contacts()
        entry = data.get(open_id) or {}
        entry.update({
            "open_id": open_id,
            "chat_id": chat_id,
            "chat_type": chat_type,
            "last_seen": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        entry.setdefault("first_seen", entry["last_seen"])
        data[open_id] = entry
        CONTACT_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONTACT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ------------------------------------------------------------ 长连接

    def build_ws(self) -> lark.ws.Client:
        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message)
            .register_p2_card_action_trigger(self._on_card_action)
            .build()
        )
        return lark.ws.Client(
            self.app_id,
            self.app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
        )

    def start_ws(self) -> None:
        """阻塞式启动长连接（在后台线程里调用）。"""
        self._ws = self.build_ws()
        log.info("正在建立飞书长连接…")
        self._ws.start()
