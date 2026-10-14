# -*- coding: utf-8 -*-
"""造演示历史数据（Spec §11 M2）。

用法：
    .venv\\Scripts\\python.exe scripts\\seed_demo.py --days 7
    .venv\\Scripts\\python.exe scripts\\seed_demo.py --days 7 --reset   # 先清空再造

⚠️ 诚实性说明
    本脚本造出来的检测记录**一律标记 is_mock=1**。
    它们不是 YOLO 真实推理的结果，报表里会显示「含模拟检测数据」提示。
    这是刻意为之：Spec §5.3 明确要求模拟数据绝不允许与真实推理混淆 ——
    如果为了让报表好看而伪造成真实数据，整个「可追溯、可当证据」的定位就塌了。

为什么需要它：
    真实巡逻只能造出「今天」的数据，而演示要问「上周有没有遗留物告警」。
    没有历史数据，这类问题一律返回 0 条，演示效果差。
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import db, repo  # noqa: E402
from shared.db import connect, now_iso  # noqa: E402
from shared.scene import points as all_points, robot as robot_profile  # noqa: E402

OK, INFO = "[OK]", "[..]"

# (点位, COCO 类别, 告警类型, 等级码, 典型置信度区间)
ALERT_POOL = [
    ("P1", "person", "人员闯入", "urgent", (0.72, 0.95)),
    ("P2", "chair", "通道占用", "normal", (0.48, 0.72)),
]


def reset_tables() -> None:
    with connect() as conn:
        for t in ("alert", "detection", "patrol_run", "task", "message", "pending_action"):
            conn.execute(f"DELETE FROM {t}")
    print(f"{OK} 已清空业务表")


def seed(days: int) -> None:
    rng = random.Random(20261013)  # 固定种子，结果可复现
    rp = robot_profile()
    pts = all_points()
    now = datetime.now()
    tasks = runs = dets = alerts = 0

    with connect() as conn:
        for back in range(days - 1, -1, -1):
            day = now - timedelta(days=back)
            # 每天 1-2 轮巡逻
            for r in range(rng.choice([1, 1, 2, 2, 3])):
                if back == 0:
                    # 今天只能放在已经过去的时段，否则会被下面的 started > now 过滤掉
                    latest = max(0, now.hour - 1)
                    hour = rng.randint(0, latest) if latest > 0 else 0
                else:
                    hour = rng.choice([9, 11, 14, 16, 20, 22])
                minute = rng.randint(0, 59)
                started = day.replace(hour=hour, minute=minute, second=rng.randint(0, 59),
                                      microsecond=0)
                if started > now:
                    continue
                duration = timedelta(minutes=rng.randint(2, 4))
                ended = min(started + duration, now)

                tasks += 1
                task_id = db.next_id(conn, "task", "T", started.strftime("%Y%m%d"))
                conn.execute(
                    """INSERT INTO task (id, robot_id, route_id, status, created_by,
                                         created_at, source_channel)
                       VALUES (?, ?, 'standard_patrol', 'completed', 'seed', ?, 'seed')""",
                    (task_id, rp.id, started.isoformat(timespec="seconds")),
                )

                runs += 1
                run_id = db.next_id(conn, "patrol_run", "RUN", started.strftime("%Y%m%d"))
                run_status = "completed" if rng.random() > 0.08 else "failed"
                conn.execute(
                    """INSERT INTO patrol_run (id, task_id, robot_id, started_at, ended_at,
                                               status, points_total, points_done)
                       VALUES (?, ?, ?, ?, ?, ?, 2, ?)""",
                    (run_id, task_id, rp.id,
                     started.isoformat(timespec="seconds"),
                     ended.isoformat(timespec="seconds"),
                     run_status, 2 if run_status == "completed" else rng.choice([0, 1])),
                )

                ts = started
                for point_id in pts:
                    ts = ts + timedelta(seconds=rng.randint(60, 180))

                    # 每个点位先落一条「背景」检测（有检出但未必告警）
                    dets += 1
                    det_id = db.next_id(conn, "detection", "D", ts.strftime("%Y%m%d"), width=4)
                    bg_label = {"P1": "person", "P2": "chair"}[point_id]
                    bg_conf = round(rng.uniform(0.36, 0.62), 3)
                    conn.execute(
                        """INSERT INTO detection (id, run_id, point_id, coco_label, confidence,
                                                  bbox_json, image_path, is_mock, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, NULL, 1, ?)""",
                        (det_id, run_id, point_id, bg_label, bg_conf,
                         json.dumps([rng.randint(0, 400), rng.randint(0, 200),
                                     rng.randint(500, 1200), rng.randint(300, 700)]),
                         ts.isoformat(timespec="seconds")),
                    )

                    # 约 35% 概率产生一条告警
                    if rng.random() > 0.35:
                        continue
                    pool = [x for x in ALERT_POOL if x[0] == point_id]
                    if not pool:
                        continue
                    _, label, atype, level_code, (lo, hi) = pool[0]
                    conf = round(rng.uniform(lo, hi), 3)

                    dets += 1
                    det_id = db.next_id(conn, "detection", "D", ts.strftime("%Y%m%d"), width=4)
                    conn.execute(
                        """INSERT INTO detection (id, run_id, point_id, coco_label, confidence,
                                                  bbox_json, image_path, is_mock, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, NULL, 1, ?)""",
                        (det_id, run_id, point_id, label, conf,
                         json.dumps([rng.randint(0, 400), rng.randint(0, 200),
                                     rng.randint(500, 1200), rng.randint(300, 700)]),
                         ts.isoformat(timespec="seconds")),
                    )

                    alerts += 1
                    alert_id = db.next_id(conn, "alert", "A", ts.strftime("%Y%m%d"), width=4)
                    # 近 2 天的留一部分待处置，好演示「现在有哪些没处理的告警」
                    if back <= 1 and rng.random() < 0.45:
                        status = "pending"
                    else:
                        status = rng.choice(["confirmed", "confirmed", "false_positive"])
                    level = "紧急" if level_code == "urgent" else "一般"
                    conn.execute(
                        """INSERT INTO alert (id, detection_id, level, level_code, alert_type,
                                              status, handled_by, handled_at, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (alert_id, det_id, level, level_code, atype, status,
                         "seed" if status != "pending" else None,
                         ts.isoformat(timespec="seconds") if status != "pending" else None,
                         ts.isoformat(timespec="seconds")),
                    )

    print(f"{OK} 造了 {days} 天数据：任务 {tasks}｜轮次 {runs}｜检测 {dets}｜告警 {alerts}")


def ensure_demo_minimums() -> None:
    """保证演示现场一定有料可看。

    随机生成有可能出现「今天没跑过」「一条待处置都没有」——
    演示时那两种情况都很尴尬。这里做确定性兜底：
        1. 今天至少 1 个已完成轮次
        2. 待处置告警至少 3 条（分布在最近 2 天）
    """
    rng = random.Random(7)
    rp = robot_profile()
    now = datetime.now()
    created: list[str] = []

    with connect() as conn:
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        n_today = conn.execute(
            "SELECT COUNT(*) AS n FROM patrol_run WHERE started_at >= ?",
            (today_start.isoformat(timespec="seconds"),),
        ).fetchone()["n"]

        if n_today == 0:
            started = max(today_start + timedelta(hours=1), now - timedelta(hours=2))
            ended = now
            day_key = started.strftime("%Y%m%d")
            task_id = db.next_id(conn, "task", "T", day_key)
            conn.execute(
                """INSERT INTO task (id, robot_id, route_id, status, created_by,
                                     created_at, source_channel)
                   VALUES (?, ?, 'standard_patrol', 'completed', 'seed', ?, 'seed')""",
                (task_id, rp.id, started.isoformat(timespec="seconds")),
            )
            run_id = db.next_id(conn, "patrol_run", "RUN", day_key)
            conn.execute(
                """INSERT INTO patrol_run (id, task_id, robot_id, started_at, ended_at,
                                           status, points_total, points_done)
                   VALUES (?, ?, ?, ?, ?, 'completed', 2, 2)""",
                (run_id, task_id, rp.id,
                 started.isoformat(timespec="seconds"),
                 ended.isoformat(timespec="seconds")),
            )
            created.append(f"今日轮次 {run_id}")

            for point_id, label, atype, level_code, (lo, hi) in ALERT_POOL:
                ts = started + timedelta(seconds=rng.randint(30, 120))
                det_id = db.next_id(conn, "detection", "D", ts.strftime("%Y%m%d"), width=4)
                conf = round(rng.uniform(lo, hi), 3)
                conn.execute(
                    """INSERT INTO detection (id, run_id, point_id, coco_label, confidence,
                                              bbox_json, image_path, is_mock, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, NULL, 1, ?)""",
                    (det_id, run_id, point_id, label, conf,
                     json.dumps([120, 90, 640, 520]),
                     ts.isoformat(timespec="seconds")),
                )
                alert_id = db.next_id(conn, "alert", "A", ts.strftime("%Y%m%d"), width=4)
                conn.execute(
                    """INSERT INTO alert (id, detection_id, level, level_code, alert_type,
                                          status, created_at)
                       VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
                    (alert_id, det_id, "紧急" if level_code == "urgent" else "一般",
                     level_code, atype, ts.isoformat(timespec="seconds")),
                )
                created.append(f"待处置告警 {alert_id}（{atype}）")

        # 待处置不足 3 条就补
        n_pending = conn.execute(
            "SELECT COUNT(*) AS n FROM alert WHERE status='pending'"
        ).fetchone()["n"]
        need = 3 - n_pending
        if need > 0:
            for i in range(need):
                back = i % 2 + 1  # 最近两天
                ts = (now - timedelta(days=back)).replace(
                    hour=rng.randint(10, 21), minute=rng.randint(0, 59), second=0
                )
                point_id, label, atype, level_code, (lo, hi) = ALERT_POOL[i % len(ALERT_POOL)]
                run_row = conn.execute(
                    "SELECT id FROM patrol_run ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
                run_id = run_row["id"] if run_row else "RUN-seed"
                det_id = db.next_id(conn, "detection", "D", ts.strftime("%Y%m%d"), width=4)
                conn.execute(
                    """INSERT INTO detection (id, run_id, point_id, coco_label, confidence,
                                              bbox_json, image_path, is_mock, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, NULL, 1, ?)""",
                    (det_id, run_id, point_id, label, round(rng.uniform(lo, hi), 3),
                     json.dumps([150, 110, 700, 560]),
                     ts.isoformat(timespec="seconds")),
                )
                alert_id = db.next_id(conn, "alert", "A", ts.strftime("%Y%m%d"), width=4)
                conn.execute(
                    """INSERT INTO alert (id, detection_id, level, level_code, alert_type,
                                          status, created_at)
                       VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
                    (alert_id, det_id, "紧急" if level_code == "urgent" else "一般",
                     level_code, atype, ts.isoformat(timespec="seconds")),
                )
                created.append(f"补造待处置告警 {alert_id}（{atype}）")

    if created:
        print(f"{OK} 演示下限兜底，补了 {len(created)} 项：")
        for c in created:
            print(f"      · {c}")
    else:
        print(f"{OK} 演示下限已满足，无需兜底")


def main() -> int:
    ap = argparse.ArgumentParser(description="造演示历史数据")
    ap.add_argument("--days", type=int, default=7, help="造多少天（默认 7）")
    ap.add_argument("--reset", action="store_true", help="先清空业务表")
    args = ap.parse_args()

    db.init_db()
    repo.ensure_base_entities()

    if args.reset:
        reset_tables()

    seed(args.days)
    ensure_demo_minimums()

    print(f"\n{INFO} 当前库状态：{db.table_counts()}")
    rpt = repo.run_stats(*repo.day_bounds())
    print(f"{INFO} 今日统计：{rpt}")
    pending = repo.list_alerts(status="pending", limit=10)
    print(f"{INFO} 待处置告警 {len(pending)} 条")
    print(f"\n{INFO} 提醒：这些记录全部 is_mock=1，报表会标注「含模拟检测数据」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
