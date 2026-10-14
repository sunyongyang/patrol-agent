# -*- coding: utf-8 -*-
"""工具层：Agent 能执行的业务动作（Spec §6.2）。

**关键约束（Spec §6.4）**：
    写操作不能由编排层直接触发。解析完意图后先落一条 pending_action，
    渲染确认卡等用户点按钮，点完才真正调用 execute_*。
    这是「LLM 幻觉不得直接作用于物理世界」的唯一保障路径。
"""

from __future__ import annotations

import logging
from datetime import datetime

import requests

from agent_service.report import builder
from agent_service.report.builder import AlertItem, DailyReport
from shared import config, repo
from shared.scene import (
    Point,
    Route,
    alert_types,
    point as get_point,
    robot as robot_profile,
    route as get_route,
    routes as all_routes,
)

log = logging.getLogger("agent_service.tools")


def robot_base() -> str:
    return config.get("robot_sim.base_url", "http://127.0.0.1:8100")


class ToolError(RuntimeError):
    pass


# ==================================================================== 读操作


def get_daily_report(day: str | None = None) -> DailyReport:
    return builder.build_daily(day)


def get_alert_list(
    status: str | None = None,
    alert_type: str | None = None,
    point_id: str | None = None,
    limit: int = 5,
) -> list[AlertItem]:
    return builder.build_alert_list(
        status=status, alert_type=alert_type, point_id=point_id, limit=limit
    )


def query_history(
    days_back: int = 7,
    alert_type: str | None = None,
    point_id: str | None = None,
    limit: int = 5,
) -> tuple[list[AlertItem], str]:
    return builder.query_history(
        days_back=days_back, alert_type=alert_type, point_id=point_id, limit=limit
    )


def list_tasks(status: str | None = None, limit: int = 5) -> list[dict]:
    return repo.list_tasks(status=status, limit=limit)


def robot_status() -> dict:
    rp = robot_profile()
    try:
        resp = requests.get(f"{robot_base()}/robot/{rp.id}/status", timeout=8)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        raise ToolError(
            f"联系不上机器人模拟器（{robot_base()}）。"
            f"请确认它已启动。原始错误：{exc}"
        ) from exc


def robot_status_text() -> str:
    """机器人状态的人类可读文本（卡片与微信共用）。"""
    rp = robot_profile()
    try:
        st = robot_status()
    except ToolError as exc:
        return f"⚠️ {exc}"

    point = st.get("current_point_name") or "未知位置"
    status_map = {
        "idle": "待命",
        "patrolling": "巡逻中",
        "charging": "充电中",
        "offline": "离线",
    }
    det = st.get("detector") or {}
    lines = [
        f"🤖 {rp.name}（{rp.id}）",
        f"状态：{status_map.get(st.get('status'), st.get('status'))}",
        f"位置：{point}",
        f"电量：{st.get('battery')}%",
        f"检测器：{det.get('model')}",
    ]
    if det.get("is_mock"):
        lines.append("⚠️ 当前为模拟检测，非真实推理")
    if st.get("current_task_id"):
        lines.append(f"当前任务：{st['current_task_id']}（剩余等待 {st.get('dwell_remaining')}s）")
    return "\n".join(lines)


def capture_now(point_id: str) -> dict:
    rp = robot_profile()
    try:
        resp = requests.post(
            f"{robot_base()}/robot/{rp.id}/capture",
            json={"point_id": point_id},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise ToolError(f"抓拍请求失败：{exc}") from exc
    if resp.status_code != 200:
        raise ToolError(f"抓拍失败：HTTP {resp.status_code} {resp.text[:200]}")
    return resp.json()


def available_routes() -> list[Route]:
    return list(all_routes().values())


def available_points() -> list[Point]:
    from shared.scene import points as all_points

    return list(all_points().values())


# ==================================================================== 写操作


def _route_summary(route: Route) -> tuple[str, str, int]:
    """返回 (路线描述, 点位链, 预计秒数)。"""
    names = []
    total = 0
    for rp in route.points:
        try:
            names.append(get_point(rp.point_id).name)
        except KeyError:
            names.append(rp.point_id)
        total += rp.dwell_sec
    return route.name, " → ".join(names), total


def prepare_patrol_task(
    route_id: str | None,
    chat_id: str,
    user_id: str,
    start_at: str | None = None,
    repeat: str = "once",
) -> tuple[str, list[tuple[str, str]]]:
    """准备下发任务：落 pending_action 并返回确认卡的字段行。

    这里**不会**碰机器人。真正下发在 execute_patrol_task。
    """
    routes = available_routes()
    if not routes:
        raise ToolError("没有配置任何巡逻路线，请检查 config/routes.yaml")

    if not route_id:
        route = routes[0]
    else:
        try:
            route = get_route(route_id)
        except KeyError as exc:
            opts = "、".join(r.id for r in routes)
            raise ToolError(f"没有叫「{route_id}」的路线。可选：{opts}") from exc

    name, chain, seconds = _route_summary(route)

    if start_at:
        time_desc = start_at
    else:
        time_desc = "立即执行一次"
    if repeat and repeat not in ("once", "None", ""):
        time_desc += f"（重复：{repeat}）"

    payload = {
        "route_id": route.id,
        "route_name": name,
        "start_at": start_at,
        "repeat": repeat,
        "robot_id": robot_profile().id,
    }
    pa_id = repo.save_pending_action(chat_id, user_id, "task.create", payload)

    rows = [
        ("路线", f"{name}（{len(route.points)} 个点位）"),
        ("时间", time_desc),
        ("预计", f"约 {max(1, round(seconds / 60))} 分钟"),
        ("点位", chain),
    ]
    return pa_id, rows


def execute_patrol_task(pa_id: str, user_id: str = "") -> dict:
    """用户点了「确认下发」——此时才真正调用机器人模拟器。"""
    pa = repo.get_pending_action(pa_id)
    if not pa:
        raise ToolError("这个确认已失效，请重新下指令。")
    if pa["status"] != "pending":
        raise ToolError(f"这个确认已经处理过了（{pa['status']}）。")
    if repo.is_expired(pa):
        repo.consume_pending_action(pa_id, "expired")
        raise ToolError("这个确认超过 10 分钟已过期，请重新下指令。")

    payload = pa["payload"]
    rp = robot_profile()
    try:
        resp = requests.post(
            f"{robot_base()}/robot/{rp.id}/tasks",
            json={
                "route_id": payload["route_id"],
                "created_by": user_id or pa.get("user_id", ""),
                "source_channel": "feishu",
            },
            timeout=20,
        )
    except requests.RequestException as exc:
        raise ToolError(f"下发失败，联系不上机器人模拟器：{exc}") from exc

    if resp.status_code == 409:
        raise ToolError(f"机器人正忙：{resp.json().get('detail', resp.text[:150])}")
    if resp.status_code != 201:
        raise ToolError(f"下发失败：HTTP {resp.status_code} {resp.text[:200]}")

    data = resp.json()
    repo.consume_pending_action(pa_id, "confirmed")
    return {
        "task_id": data["task"]["id"],
        "run_id": data["run"]["run_id"],
        "route_id": data["run"]["route_id"],
        "route_name": payload.get("route_name", ""),
        "points": data["run"]["points"],
    }


def cancel_pending(pa_id: str) -> None:
    repo.consume_pending_action(pa_id, "cancelled")


def cancel_task(task_id: str | None = None) -> dict:
    """取消任务。不传 task_id 就取消当前正在跑的那个。"""
    rp = robot_profile()
    if not task_id:
        running = repo.running_task(rp.id)
        if not running:
            raise ToolError("当前没有正在跑的任务。")
        task_id = running["id"]

    try:
        resp = requests.delete(f"{robot_base()}/robot/{rp.id}/tasks/{task_id}", timeout=15)
    except requests.RequestException as exc:
        raise ToolError(f"取消失败，联系不上机器人模拟器：{exc}") from exc

    if resp.status_code == 404:
        raise ToolError(f"没有找到任务 {task_id}。")
    if resp.status_code != 200:
        raise ToolError(f"取消失败：HTTP {resp.status_code} {resp.text[:200]}")

    return {"task_id": task_id, **resp.json()}


def handle_alert(alert_id: str, action: str, user_id: str = "") -> dict:
    """处置告警：action = confirm | false_positive。

    按钮点击本身就是「二次确认」，所以这里直接执行。
    """
    alert = repo.get_alert(alert_id)
    if not alert:
        raise ToolError(f"没有找到告警 {alert_id}。")

    status = {"confirm": "confirmed", "false_positive": "false_positive"}.get(action)
    if not status:
        raise ToolError(f"不认识的处理动作：{action}")

    if alert["status"] != "pending":
        raise ToolError(f"这条告警已经是「{alert['status']}」状态了。")

    ok = repo.update_alert_status(alert_id, status, handled_by=user_id)
    if not ok:
        raise ToolError(f"更新告警 {alert_id} 失败。")

    return {
        "alert_id": alert_id,
        "status": status,
        "alert_type": alert["alert_type"],
        "coco_label": alert.get("coco_label"),
        "confidence": alert.get("confidence"),
        "point_id": alert.get("point_id"),
    }


def advance_patrol() -> str:
    """时间快进：跳过当前点位剩余的就位等待。"""
    try:
        resp = requests.post(f"{robot_base()}/sim/advance", timeout=10)
        resp.raise_for_status()
        return resp.json().get("message", "已加速")
    except requests.RequestException as exc:
        raise ToolError(f"加速失败：{exc}") from exc


# ==================================================================== 上下文


def business_context() -> str:
    """给 LLM 的业务上下文。必须包含当前日期（Spec F1）。"""
    from shared.scene import describe

    now = datetime.now()
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]
    head = (
        f"当前日期：{now.strftime('%Y-%m-%d')}（{weekday}），当前时间 {now.strftime('%H:%M')}。\n"
        "所有相对时间（今天/昨天/上周/最近三天）都必须基于这个日期换算，不要凭记忆猜。\n"
    )
    types = "、".join(f"{a.type}（{a.label}）" for a in alert_types().values())
    return f"{head}\n{describe()}\n\n告警类型映射：{types}"
