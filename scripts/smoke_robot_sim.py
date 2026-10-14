# -*- coding: utf-8 -*-
"""机器人模拟器冒烟测试（自动用 /sim/advance 跳过就位窗口）。

用法（先起服务）：
    .venv\\Scripts\\python.exe -m uvicorn robot_sim.main:app --port 8100
    .venv\\Scripts\\python.exe scripts\\smoke_robot_sim.py

它会：
    1. /health      看摄像头与检测器
    2. /routes      核对点位与路线
    3. POST 任务    下发一次标准巡检
    4. 轮询状态     期间自动 advance 跳过等待
    5. 汇总         打印落库的 detection / alert
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402

from shared import db, repo  # noqa: E402

BASE = "http://127.0.0.1:8100"
OK, FAIL, INFO = "[OK]", "[FAIL]", "[..]"


def banner(t: str) -> None:
    print("\n" + "=" * 68)
    print(t)
    print("=" * 68)


def main() -> int:
    banner("1/5 健康检查")
    try:
        health = requests.get(f"{BASE}/health", timeout=20).json()
    except requests.RequestException as exc:
        print(f"{FAIL} 连不上 {BASE}：{exc}")
        print("     请先启动：.venv\\Scripts\\python.exe -m uvicorn robot_sim.main:app --port 8100")
        return 1

    cam = health.get("camera") or {}
    det = health.get("detector") or {}
    print(f"      摄像头   ok={cam.get('ok')} {cam.get('width')}x{cam.get('height')} "
          f"抓帧={cam.get('capture_ms')}ms")
    if not cam.get("ok"):
        print(f"      {FAIL} 摄像头不可用：{cam.get('error')}")
    print(f"      检测器   {det.get('name')} / {det.get('model')} / is_mock={det.get('is_mock')}")
    if det.get("is_mock"):
        print(f"      {INFO} 模拟检测模式，结果会带 is_mock=true")

    banner("2/5 点位与算法绑定")
    routes = requests.get(f"{BASE}/routes", timeout=10).json()
    print("      算法：")
    for a in routes.get("algorithms", []):
        print(f"        {a['id']:<20} {a['name']:<14} {a['coco_label']:<8} → {a['alert_type']}")
    print("      点位绑定：")
    for p in routes["points"]:
        print(f"        {p['id']} {p['name']:<8} 绑定 {p['algorithm_name']:<12}"
              f"（{p['coco_label']}）→ 「{p['alert_type']}」　阈值 {p['conf_threshold']}")
    for r in routes["routes"]:
        seq = " → ".join(f"{x['point_id']}({x['dwell_sec']}s)" for x in r["points"])
        print(f"      路线 {r['id']} {r['name']}：{seq}")

    route_id = routes["routes"][0]["id"]
    robot_id = routes["robot"]["id"]

    banner("3/5 下发任务")
    resp = requests.post(
        f"{BASE}/robot/{robot_id}/tasks",
        json={"route_id": route_id, "created_by": "smoke_test", "source_channel": "test"},
        timeout=20,
    )
    if resp.status_code != 201:
        print(f"{FAIL} 下发失败 HTTP {resp.status_code}: {resp.text[:300]}")
        return 1
    data = resp.json()
    task_id = data["task"]["id"]
    run_id = data["run"]["run_id"]
    print(f"      {OK} 任务 {task_id} / 轮次 {run_id}")

    banner("4/5 轮询执行（自动 advance 跳过就位窗口）")
    deadline = time.monotonic() + 120
    last_line = ""
    while time.monotonic() < deadline:
        st = requests.get(f"{BASE}/robot/{robot_id}/status", timeout=10).json()
        line = (f"      status={st['status']:<11} point={st['current_point'] or '-':<3} "
                f"剩余等待={st['dwell_remaining']:>5.1f}s")
        if line != last_line:
            print(line, flush=True)
            last_line = line

        if st["status"] != "patrolling":
            break
        if st["dwell_remaining"] > 3:
            requests.post(f"{BASE}/sim/advance", timeout=10)
        time.sleep(1.5)
    else:
        print(f"      {FAIL} 超时未结束")

    banner("5/5 结果汇总")
    task = requests.get(f"{BASE}/robot/{robot_id}/tasks/{task_id}", timeout=10).json()
    print(f"      任务状态 = {task['task']['status']}")
    run = task.get("run") or {}
    print(f"      轮次     = {run.get('id')} status={run.get('status')} "
          f"点位 {run.get('points_done')}/{run.get('points_total')}")

    counts = db.table_counts()
    print(f"      落库行数 = {counts}")

    alerts = repo.list_alerts(limit=5)
    if alerts:
        print(f"\n      最近告警 {len(alerts)} 条：")
        for a in alerts:
            print(f"        {a['id']} {a['alert_type']:<8} {a['level']:<4} "
                  f"conf={a['confidence']:.3f} label={a['coco_label']:<8} "
                  f"point={a['point_id']} is_mock={a['is_mock']}")
    else:
        print(f"      {INFO} 无告警（画面里没有 P1/P2 关心的目标，属正常）")

    dets = repo.list_alerts(limit=1)
    print(f"\n      抓拍图目录：{Path(__file__).resolve().parents[1] / 'data' / 'images'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
