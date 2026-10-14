# -*- coding: utf-8 -*-
"""Agent 服务 :8200（Spec §6）。

启动：
    .venv\\Scripts\\python.exe -m uvicorn agent_service.main:app --port 8200

这个进程同时承担四件事：
    1. 飞书长连接（收消息、收按钮回调）
    2. 编排与工具执行
    3. 内部回调端点（接收机器人模拟器的告警与轮次完成通知）
    4. 定时日报推送

放在同一个进程里是刻意的：告警推送需要复用同一条长连接与同一个适配器实例。
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from agent_service.core import orchestrator
from agent_service.im.feishu import FeishuAdapter
from agent_service.report import builder
from agent_service.report import feishu_card as fc
from agent_service.scheduler import DailyReportScheduler
from shared import config, db, repo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("agent_service")

STATE: dict[str, object] = {}


# ==================================================================== 推送


def push_alert(alert_id: str) -> dict:
    """把一条告警推给所有已知联系人（带抓拍原图）。"""
    adapter: FeishuAdapter = STATE["adapter"]  # type: ignore[assignment]
    row = orchestrator._reload_alert(alert_id)  # noqa: SLF001
    if not row:
        log.warning("告警 %s 不存在", alert_id)
        return {"sent": 0, "reason": "alert not found"}

    img_key = adapter.upload_image(row.image_path) if row.image_path else None
    card = fc.alert_card(row, img_key=img_key)
    sent = adapter.broadcast_card(card)
    if sent:
        repo.mark_alert_pushed(alert_id, "")
    return {"sent": len(sent), "alert_id": alert_id, "img_key": bool(img_key)}


def push_run_finished(summary: dict) -> dict:
    adapter: FeishuAdapter = STATE["adapter"]  # type: ignore[assignment]
    reply = orchestrator.build_run_finished_reply(summary)
    sent = adapter.broadcast_card(reply.card or {})
    return {"sent": len(sent), "run_id": summary.get("run_id")}


def push_daily(day: str | None = None) -> dict:
    adapter: FeishuAdapter = STATE["adapter"]  # type: ignore[assignment]
    rpt = builder.build_daily(day)
    sent = adapter.broadcast_card(fc.report_card(rpt))
    log.info("日报已推送 %s，%d 个会话", rpt.day, len(sent))
    return {"sent": len(sent), "day": rpt.day}


# ==================================================================== 生命周期


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    repo.ensure_base_entities()

    adapter = FeishuAdapter()
    STATE["adapter"] = adapter

    # 预热凭证：证书/密钥有问题要在这里就暴露，而不是等用户发消息才发现
    try:
        adapter.token(force=True)
        log.info("飞书凭证有效，app_id=%s", adapter.app_id)
    except Exception as exc:  # noqa: BLE001
        log.error("飞书凭证无效：%s", exc)

    ws_thread = threading.Thread(target=adapter.start_ws, name="feishu-ws", daemon=True)
    ws_thread.start()
    log.info("飞书长连接线程已启动")

    scheduler = DailyReportScheduler(send_daily=push_daily)
    scheduler.start()
    STATE["scheduler"] = scheduler

    log.info("Agent 服务就绪 :%s", config.get("agent_service.port", 8200))
    log.info("已知联系人：%s", list(adapter.known_contacts()) or "无（请先给机器人发一条消息）")

    yield

    scheduler.stop()
    log.info("Agent 服务已停止")


app = FastAPI(title="巡逻 Agent 服务", version="1.0.0", lifespan=lifespan)


# ==================================================================== 内部端点


class AlertPush(BaseModel):
    alert_id: str
    alert_type: str | None = None
    level: str | None = None
    coco_label: str | None = None
    confidence: float | None = None
    point_id: str | None = None
    point_name: str | None = None
    run_id: str | None = None
    image_path: str | None = None
    detected_at: str | None = None


class RunFinished(BaseModel):
    run_id: str
    task_id: str | None = None
    robot_id: str | None = None
    route_id: str | None = None
    status: str = "completed"
    points_total: int = 0
    points_done: int = 0
    alert_ids: list[str] = []
    started_at: str | None = None
    ended_at: str | None = None
    error: str | None = None


@app.get("/health")
def health() -> dict:
    adapter: FeishuAdapter | None = STATE.get("adapter")  # type: ignore[assignment]
    return {
        "ok": True,
        "service": "agent_service",
        "feishu_app_id": adapter.app_id if adapter else None,
        "known_contacts": list(adapter.known_contacts()) if adapter else [],
        "tables": db.table_counts(),
        "daily_report_at": config.get("agent_service.daily_report_time", "08:30"),
    }


@app.post("/internal/alert")
def internal_alert(body: AlertPush) -> dict:
    """机器人模拟器检测到告警时的回调。"""
    log.info("收到告警回调 %s %s", body.alert_id, body.alert_type)
    try:
        return push_alert(body.alert_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("告警推送失败")
        return {"sent": 0, "error": str(exc)}


@app.post("/internal/run-finished")
def internal_run_finished(body: RunFinished) -> dict:
    log.info("收到轮次完成回调 %s status=%s", body.run_id, body.status)
    try:
        return push_run_finished(body.model_dump())
    except Exception as exc:  # noqa: BLE001
        log.exception("轮次推送失败")
        return {"sent": 0, "error": str(exc)}


@app.post("/internal/push-daily")
def manual_daily(day: str | None = None) -> dict:
    """手动触发日报（演示与调试用）。"""
    return push_daily(day)


@app.post("/internal/say")
def internal_say(chat_id: str, text: str) -> dict:
    """直接往指定会话发条文本（调试用）。"""
    adapter: FeishuAdapter = STATE["adapter"]  # type: ignore[assignment]
    mid = adapter.send_text(chat_id, text)
    return {"ok": bool(mid), "message_id": mid}
