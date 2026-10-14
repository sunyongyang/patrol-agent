# -*- coding: utf-8 -*-
"""触发一次抓拍（演示与调试用）。

用法：
    .venv\\Scripts\\python.exe scripts\\capture_once.py            # 抓 P1
    .venv\\Scripts\\python.exe scripts\\capture_once.py P2         # 抓 P2
    .venv\\Scripts\\python.exe scripts\\capture_once.py P1 P2      # 两个都抓

会打印结果图与原始帧的路径，方便直接打开核对标注效果。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

BASE = "http://127.0.0.1:8100"


def main() -> int:
    point_ids = sys.argv[1:] or ["P1"]
    for pid in point_ids:
        try:
            resp = requests.post(
                f"{BASE}/robot/R-01/capture", json={"point_id": pid}, timeout=60
            )
        except requests.RequestException as exc:
            print(f"[FAIL] 连不上机器人模拟器 {BASE}：{exc}")
            return 1

        if resp.status_code != 200:
            print(f"[FAIL] {pid} 抓拍失败 HTTP {resp.status_code}: {resp.text[:200]}")
            continue

        d = resp.json()
        dets = d.get("detections") or []
        print(f"--- {pid} {d.get('point_name')} ---")
        print(f"    绑定算法: {d.get('algorithm_name')}（{d.get('algorithm_id')}）")
        print(f"    结果图（带框）: {d.get('image_path')}")
        print(f"    原始帧（未绘制）: {d.get('raw_image_path')}")
        print("    检出（仅绑定算法的结果）: " + (", ".join(
            f"{x['label']}={x['confidence']:.2f}" for x in dets) or "无"))
        print(f"    本次告警: {d.get('alert_type') or '无'}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
