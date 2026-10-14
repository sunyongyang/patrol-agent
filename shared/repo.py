# -*- coding: utf-8 -*-
"""数据访问层：SQL 集中在这里，robot_sim 与 agent_service 共用。

设计取舍：不做 ORM。表只有 9 张、字段稳定，手写 SQL 更好读、更好查问题，
也不会为了一个 Demo 引入 SQLAlchemy 的迁移负担。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from shared import db
from shared.db import connect, next_id, now_iso
from shared.scene import algorithms, points as all_points, robot as robot_profile, route as get_route


# ==================================================================== 初始化


def ensure_base_entities() -> None:
    """把 config/routes.yaml 里的机器人、点位、路线同步进库。

    幂等：每次启动调用，配置改了会自动收敛。
    """
    rp = robot_profile()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO robot (id, name, model, status, battery, online, last_seen_at)
            VALUES (?, ?, ?, 'idle', ?, 1, ?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name, model=excluded.model
            """,
            (rp.id, rp.name, rp.model, rp.initial_battery, now_iso()),
        )

        for p in all_points().values():
            conn.execute(
                """
                INSERT INTO point (id, name, area, algorithm_id, target_label,
                                   conf_threshold, zone_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    area=excluded.area,
                    algorithm_id=excluded.algorithm_id,
                    target_label=excluded.target_label,
                    conf_threshold=excluded.conf_threshold,
                    zone_json=excluded.zone_json
                """,
                (
                    p.id, p.name, p.area, p.algorithm_id, p.target_label, p.conf_threshold,
                    json.dumps(p.zone) if p.zone else None,
                ),
            )

        # 路线来自 scene（已解析），逐条写入
        from shared.scene import routes as all_routes

        for r in all_routes().values():
            conn.execute(
                """
                INSERT INTO route (id, name, description) VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name, description=excluded.description
                """,
                (r.id, r.name, r.description),
            )
            conn.execute("DELETE FROM route_point WHERE route_id = ?", (r.id,))
            for seq, rp_item in enumerate(r.points, start=1):
                conn.execute(
                    "INSERT INTO route_point (route_id, point_id, seq, dwell_sec) VALUES (?, ?, ?, ?)",
                    (r.id, rp_item.point_id, seq, rp_item.dwell_sec),
                )


# ==================================================================== 机器人


def update_robot(robot_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["last_seen_at"] = now_iso()
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE robot SET {cols} WHERE id=?", (*fields.values(), robot_id))


def get_robot(robot_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM robot WHERE id=?", (robot_id,)).fetchone()
        return dict(row) if row else None


# ==================================================================== 任务


def create_task(
    robot_id: str,
    route_id: str,
    created_by: str = "",
    source_channel: str = "feishu",
    start_at: str | None = None,
    repeat_cron: str | None = None,
) -> dict:
    with connect() as conn:
        # 路线必须存在，否则早失败好过跑到一半崩
        get_route(route_id)
        task_id = next_id(conn, "task", "T")
        conn.execute(
            """
            INSERT INTO task (id, robot_id, route_id, start_at, repeat_cron, status,
                              created_by, created_at, source_channel)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (task_id, robot_id, route_id, start_at, repeat_cron,
             created_by, now_iso(), source_channel),
        )
        row = conn.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone()
        return dict(row)


def get_task(task_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None


def list_tasks(status: str | None = None, limit: int = 5) -> list[dict]:
    sql = "SELECT * FROM task"
    params: list[Any] = []
    if status:
        sql += " WHERE status=?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def set_task_status(task_id: str, status: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE task SET status=? WHERE id=?", (status, task_id))


def running_task(robot_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM task WHERE robot_id=? AND status IN ('running','pending') "
            "ORDER BY created_at DESC LIMIT 1",
            (robot_id,),
        ).fetchone()
        return dict(row) if row else None


# ==================================================================== 轮次


def create_run(task_id: str, robot_id: str, points_total: int) -> str:
    with connect() as conn:
        run_id = next_id(conn, "patrol_run", "RUN")
        conn.execute(
            """
            INSERT INTO patrol_run (id, task_id, robot_id, started_at, status, points_total, points_done)
            VALUES (?, ?, ?, ?, 'running', ?, 0)
            """,
            (run_id, task_id, robot_id, now_iso(), points_total),
        )
        return run_id


def bump_run_progress(run_id: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE patrol_run SET points_done = points_done + 1 WHERE id=?", (run_id,))


def finish_run(run_id: str, status: str = "completed") -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE patrol_run SET status=?, ended_at=? WHERE id=?",
            (status, now_iso(), run_id),
        )


def get_run(run_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM patrol_run WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None


def run_for_task(task_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM patrol_run WHERE task_id=? ORDER BY started_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return dict(row) if row else None


def latest_run(limit: int = 1) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM patrol_run ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ==================================================================== 检测与告警


def insert_detection(
    run_id: str,
    point_id: str,
    coco_label: str,
    confidence: float,
    bbox: tuple[float, float, float, float] | list[float] | None,
    image_path: str | None,
    is_mock: bool,
    raw_image_path: str | None = None,
) -> str:
    with connect() as conn:
        det_id = next_id(conn, "detection", "D")
        conn.execute(
            """
            INSERT INTO detection (id, run_id, point_id, coco_label, confidence,
                                   bbox_json, image_path, raw_image_path, is_mock, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                det_id, run_id, point_id, coco_label, float(confidence),
                json.dumps(list(bbox)) if bbox else None,
                image_path, raw_image_path, 1 if is_mock else 0, now_iso(),
            ),
        )
        return det_id


def insert_alert(
    detection_id: str,
    level: str,
    level_code: str,
    alert_type: str,
    status: str = "pending",
    target_count: int = 1,
) -> str:
    with connect() as conn:
        alert_id = next_id(conn, "alert", "A")
        ts = now_iso()
        conn.execute(
            """
            INSERT INTO alert (id, detection_id, level, level_code, alert_type,
                               status, occurrence_count, target_count, last_seen_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (alert_id, detection_id, level, level_code, alert_type, status,
             max(1, int(target_count)), ts, ts),
        )
        return alert_id


def find_open_alert(
    point_id: str, alert_type: str, window_minutes: int
) -> dict | None:
    """找同点位、同类型、窗口内且**未处置**的告警 —— 用于抑制重复告警。

    判定基准用 `COALESCE(last_seen_at, created_at)`：
    只要目标持续出现（每次都被刷新），就一直算在窗口内，告警持续累加而不新建；
    目标消失超过窗口后再出现，才算新事件。
    """
    since = (datetime.now() - timedelta(minutes=window_minutes)).isoformat(timespec="seconds")
    with connect() as conn:
        row = conn.execute(
            """
            SELECT a.*, d.point_id, d.coco_label, d.confidence, d.image_path
            FROM alert a JOIN detection d ON d.id = a.detection_id
            WHERE a.status = 'pending'
              AND a.alert_type = ?
              AND d.point_id = ?
              AND COALESCE(a.last_seen_at, a.created_at) >= ?
            ORDER BY COALESCE(a.last_seen_at, a.created_at) DESC
            LIMIT 1
            """,
            (alert_type, point_id, since),
        ).fetchone()
        return dict(row) if row else None


def touch_alert(
    alert_id: str, detection_id: str | None = None, target_count: int | None = None
) -> dict:
    """抑制时刷新既有告警：次数 +1、更新时间，并把证据换成最新一次。

    把 detection_id 指向最新检测是有意的 —— 卡片上展示的应该是**最近一次**的
    抓拍与置信度，而不是第一次的。这样值班员看到的画面始终是当前的。
    """
    with connect() as conn:
        sets = ["occurrence_count = occurrence_count + 1", "last_seen_at = ?"]
        params: list[Any] = [now_iso()]
        if detection_id:
            sets.append("detection_id = ?")
            params.append(detection_id)
        if target_count is not None:
            sets.append("target_count = ?")
            params.append(max(1, int(target_count)))
        params.append(alert_id)
        conn.execute(f"UPDATE alert SET {', '.join(sets)} WHERE id=?", params)
        row = conn.execute("SELECT * FROM alert WHERE id=?", (alert_id,)).fetchone()
        return dict(row) if row else {}


def get_alert(alert_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT a.*, d.coco_label, d.confidence, d.image_path, d.raw_image_path, d.is_mock,
                   d.point_id, d.run_id, d.created_at AS detected_at
            FROM alert a JOIN detection d ON d.id = a.detection_id
            WHERE a.id = ?
            """,
            (alert_id,),
        ).fetchone()
        return dict(row) if row else None


def update_alert_status(
    alert_id: str, status: str, handled_by: str = "", resolution_note: str | None = None
) -> bool:
    """处置告警。resolution_note 存「怎么处理的」—— 这是经验复用与 RAG 的前置。"""
    with connect() as conn:
        if resolution_note is None:
            cur = conn.execute(
                "UPDATE alert SET status=?, handled_by=?, handled_at=? WHERE id=?",
                (status, handled_by, now_iso(), alert_id),
            )
        else:
            cur = conn.execute(
                "UPDATE alert SET status=?, handled_by=?, handled_at=?, resolution_note=? "
                "WHERE id=?",
                (status, handled_by, now_iso(), resolution_note, alert_id),
            )
        return cur.rowcount > 0


def mark_alert_pushed(alert_id: str, card_message_id: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE alert SET pushed_at=?, card_message_id=? WHERE id=?",
            (now_iso(), card_message_id, alert_id),
        )


def list_alerts(
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
    alert_type: str | None = None,
    point_id: str | None = None,
    limit: int = 5,
) -> list[dict]:
    sql = """
        SELECT a.*, d.coco_label, d.confidence, d.image_path, d.raw_image_path, d.is_mock,
               d.point_id, d.run_id, d.created_at AS detected_at
        FROM alert a JOIN detection d ON d.id = a.detection_id
        WHERE 1=1
    """
    params: list[Any] = []
    if status:
        sql += " AND a.status=?"
        params.append(status)
    if alert_type:
        sql += " AND a.alert_type=?"
        params.append(alert_type)
    if point_id:
        sql += " AND d.point_id=?"
        params.append(point_id)
    if since:
        sql += " AND a.created_at >= ?"
        params.append(since)
    if until:
        sql += " AND a.created_at <= ?"
        params.append(until)
    sql += " ORDER BY a.created_at DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def list_alerts_by_ids(ids: list[str]) -> list[dict]:
    """按告警号取记录，用于「导出卡片上列出的这几条」。"""
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    sql = f"""
        SELECT a.*, d.coco_label, d.confidence, d.image_path, d.raw_image_path, d.is_mock,
               d.point_id, d.run_id, d.created_at AS detected_at
        FROM alert a JOIN detection d ON d.id = a.detection_id
        WHERE a.id IN ({marks})
        ORDER BY a.created_at DESC
    """
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, list(ids)).fetchall()]


def alert_counts(since: str, until: str) -> dict[str, int]:
    """按状态统计告警数，报表用。

    只统计三种主要状态。`merged`（历史重复告警合并后的存档）与 `expired`
    必须排除在 total 之外，否则报表会重复计数。
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM alert WHERE created_at >= ? AND created_at <= ? "
            "GROUP BY status",
            (since, until),
        ).fetchall()
        out = {"pending": 0, "confirmed": 0, "false_positive": 0}
        for r in rows:
            if r["status"] in out:
                out[r["status"]] = r["n"]
        out["total"] = out["pending"] + out["confirmed"] + out["false_positive"]
        return out


def merge_duplicate_alerts(window_minutes: int = 30) -> list[dict]:
    """合并历史上因缺少抑制而产生的重复告警（维护操作）。

    背景：在引入告警抑制之前，同一个目标每次巡逻都会新建一条告警。
    实测 10 条真实告警里 9 条是同一个人在同一个点位的重复。

    规则：同一 (点位, 告警类型) 的一串 pending 告警，相邻两次间隔小于
    window_minutes 的视为同一次事件，合并到最早那条：
        - occurrence_count 累加
        - last_seen_at 取最后时间，detection_id 指向最后一次的检测（证据用最新画面）
        - 其余标记 status='merged'（**保留记录不删除**，可审计）

    返回合并摘要。
    """
    expect_label = {}  # point_id + alert_type -> 不校验，直接按 point/type 分组
    merged: list[dict] = []

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.alert_type, a.created_at, a.last_seen_at, a.detection_id,
                   a.occurrence_count, d.point_id, d.confidence
            FROM alert a JOIN detection d ON d.id = a.detection_id
            WHERE a.status = 'pending'
            ORDER BY d.point_id, a.alert_type, a.created_at
            """
        ).fetchall()

        groups: dict[tuple[str, str], list] = {}
        for r in rows:
            groups.setdefault((r["point_id"], r["alert_type"]), []).append(r)

        for (point_id, alert_type), items in groups.items():
            chain: list = []
            for item in items:
                if not chain:
                    chain = [item]
                    continue
                prev = chain[-1]
                prev_seen = prev["last_seen_at"] or prev["created_at"]
                gap_min = _minutes_between(prev_seen, item["created_at"])
                if gap_min is not None and gap_min <= window_minutes:
                    chain.append(item)
                else:
                    _flush_chain(conn, chain, point_id, alert_type, merged)
                    chain = [item]
            if chain:
                _flush_chain(conn, chain, point_id, alert_type, merged)

    return merged


def _minutes_between(a_iso: str, b_iso: str) -> float | None:
    try:
        return (datetime.fromisoformat(b_iso) - datetime.fromisoformat(a_iso)).total_seconds() / 60
    except (ValueError, TypeError):
        return None


def _flush_chain(conn, chain: list, point_id: str, alert_type: str,
                 merged: list[dict]) -> None:
    if len(chain) < 2:
        return
    head, last = chain[0], chain[-1]
    # 累加各条已有的 occurrence_count，而不是数链条长度 ——
    # 链条里可能已经有被抑制过的告警（count > 1），数长度会把这部分丢掉。
    total = sum(int(item["occurrence_count"] or 1) for item in chain)
    conn.execute(
        """UPDATE alert SET occurrence_count=?, last_seen_at=?, detection_id=?
           WHERE id=?""",
        (total, last["last_seen_at"] or last["created_at"], last["detection_id"], head["id"]),
    )
    for item in chain[1:]:
        conn.execute("UPDATE alert SET status='merged' WHERE id=?", (item["id"],))
    merged.append({
        "kept": head["id"],
        "point_id": point_id,
        "alert_type": alert_type,
        "merged_count": len(chain) - 1,
        "occurrence_count": total,
    })


def alert_type_breakdown(since: str, until: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT alert_type, COUNT(*) AS n FROM alert "
            "WHERE created_at >= ? AND created_at <= ? GROUP BY alert_type ORDER BY n DESC",
            (since, until),
        ).fetchall()
        return [dict(r) for r in rows]


# ==================================================================== 轮次统计


def run_stats(since: str, until: str) -> dict:
    """日报用的轮次统计（Spec §6.5）。

    指标命名刻意区分开，避免误导：
        captures      = 完成的点位数 = **实际拍照次数**
        detections    = detection 表行数 = **检出目标数**（没检出东西的那次不产生行）
        之前把 detections 标成「检测记录 N 条」，会被读成「检测了 N 次」——
        实际拍照 26 次、检出 55 个目标时，那个数字是错的。

        path_completion_rate = 已完成点位 / 应巡点位。
        **这不是「巡逻覆盖率」** —— 后者按 KPI 定义还含时间维度。
        名字必须叫准，否则客户按他们的定义理解，一问就穿帮。
    """
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
                   SUM(points_total) AS pts_total,
                   SUM(points_done)  AS pts_done
            FROM patrol_run WHERE started_at >= ? AND started_at <= ?
            """,
            (since, until),
        ).fetchone()
        total = row["total"] or 0
        completed = row["completed"] or 0
        pts_total = row["pts_total"] or 0
        pts_done = row["pts_done"] or 0

        counts = conn.execute(
            """
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN is_mock=1 THEN 1 ELSE 0 END) AS mock
            FROM detection WHERE created_at >= ? AND created_at <= ?
            """,
            (since, until),
        ).fetchone()
        detections = counts["n"] or 0
        mock = counts["mock"] or 0

        # 轮次层面也区分真假：一个轮次只要含真实推理检测就算「有真实数据」
        runs_with_real = conn.execute(
            """
            SELECT COUNT(DISTINCT r.id) AS n
            FROM patrol_run r JOIN detection d ON d.run_id = r.id
            WHERE r.started_at >= ? AND r.started_at <= ? AND d.is_mock = 0
            """,
            (since, until),
        ).fetchone()["n"] or 0

    return {
        "runs_total": total,
        "runs_completed": completed,
        "completion_rate": round(completed / total * 100, 1) if total else 0.0,
        "runs_with_real_data": runs_with_real,
        "points_total": pts_total,
        "points_done": pts_done,
        "captures": pts_done,
        "path_completion_rate": round(pts_done / pts_total * 100, 1) if pts_total else 0.0,
        "detections": detections,
        "real_detections": detections - mock,
        "mock_detections": mock,
    }


def day_bounds(day: str | None = None) -> tuple[str, str]:
    """返回某一天的 [00:00:00, 23:59:59] ISO 字符串。"""
    if day:
        d = datetime.strptime(day, "%Y-%m-%d")
    else:
        d = datetime.now()
    start = d.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")


def range_bounds(days_back: int, end_day: str | None = None) -> tuple[str, str]:
    """最近 N 天（含今天/end_day）。"""
    end = datetime.strptime(end_day, "%Y-%m-%d") if end_day else datetime.now()
    start = end - timedelta(days=days_back - 1)
    s = start.replace(hour=0, minute=0, second=0, microsecond=0)
    e = end.replace(hour=23, minute=59, second=59, microsecond=0)
    return s.isoformat(timespec="seconds"), e.isoformat(timespec="seconds")


# ==================================================================== 待确认动作


def save_pending_action(
    chat_id: str, user_id: str, intent: str, payload: dict, ttl_minutes: int = 10
) -> str:
    with connect() as conn:
        pa_id = next_id(conn, "pending_action", "PA")
        expires = (datetime.now() + timedelta(minutes=ttl_minutes)).isoformat(timespec="seconds")
        conn.execute(
            """
            INSERT INTO pending_action (id, chat_id, user_id, intent, payload_json,
                                        status, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (pa_id, chat_id, user_id, intent, json.dumps(payload, ensure_ascii=False),
             expires, now_iso()),
        )
        return pa_id


def get_pending_action(pa_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM pending_action WHERE id=?", (pa_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["payload"] = json.loads(d["payload_json"] or "{}")
        return d


def latest_pending_action(chat_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM pending_action WHERE chat_id=? AND status='pending' "
            "ORDER BY created_at DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["payload"] = json.loads(d["payload_json"] or "{}")
        return d


def consume_pending_action(pa_id: str, status: str = "confirmed") -> None:
    with connect() as conn:
        conn.execute("UPDATE pending_action SET status=? WHERE id=?", (status, pa_id))


def is_expired(pa: dict) -> bool:
    try:
        return datetime.fromisoformat(pa["expires_at"]) < datetime.now()
    except (ValueError, KeyError):
        return True


# ==================================================================== 消息日志


def log_message(channel: str, chat_id: str, user_id: str, direction: str, content: Any) -> None:
    with connect() as conn:
        mid = next_id(conn, "message", "M")
        conn.execute(
            """
            INSERT INTO message (id, channel, chat_id, user_id, direction, content_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (mid, channel, chat_id, user_id, direction,
             json.dumps(content, ensure_ascii=False)[:8000], now_iso()),
        )


def summary_line() -> str:
    """自检摘要。"""
    counts = db.table_counts()
    return f"表行数 {counts}｜算法 {list(algorithms())}"
