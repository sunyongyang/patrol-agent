# -*- coding: utf-8 -*-
"""数据库维护操作。

用法：
    .venv\\Scripts\\python.exe scripts\\maintain.py --merge-alerts
    .venv\\Scripts\\python.exe scripts\\maintain.py --merge-alerts --window 30

`--merge-alerts`
    合并历史上因缺少抑制而产生的重复告警。
    引入告警抑制之前，同一个目标每次巡逻都会新建一条告警 ——
    实测 10 条真实告警里 9 条是同一个人在同一个点位的重复。
    合并后：保留最早那条并累加出现次数，证据指向最后一次的抓拍，
    其余标记为 `merged`（保留记录不删除，可审计）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import db, repo  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="数据库维护")
    ap.add_argument("--merge-alerts", action="store_true",
                    help="合并历史重复告警")
    ap.add_argument("--window", type=int, default=30,
                    help="判定为同一次事件的相邻间隔上限（分钟），默认 30")
    args = ap.parse_args()

    db.init_db()
    repo.ensure_base_entities()

    if not args.merge_alerts:
        ap.print_help()
        return 0

    print(f"合并重复告警（窗口 {args.window} 分钟）…")
    merged = repo.merge_duplicate_alerts(args.window)
    if not merged:
        print("  没有需要合并的重复告警。")
    else:
        for m in merged:
            print(f"  {m['point_id']} {m['alert_type']}："
                  f"保留 {m['kept']}，合并掉 {m['merged_count']} 条，"
                  f"累计出现 {m['occurrence_count']} 次")
    print(f"\n共处理 {len(merged)} 组")

    print("\n--- 合并后告警状态 ---")
    from shared.db import connect

    with connect() as conn:
        for r in conn.execute(
            "SELECT status, COUNT(*) AS n, SUM(occurrence_count) AS occ "
            "FROM alert GROUP BY status ORDER BY status"
        ):
            print(f"  {r['status']:<16} {r['n']:>3} 条，累计出现 {r['occ']} 次")
        print("\n  待处置明细：")
        for r in conn.execute(
            "SELECT id, alert_type, occurrence_count, created_at, last_seen_at "
            "FROM alert WHERE status='pending' ORDER BY created_at"
        ):
            print(f"    {r['id']} {r['alert_type']:<8} 出现 {r['occurrence_count']:>2} 次"
                  f"  首次 {r['created_at'][5:19]}  最近 {(r['last_seen_at'] or '—')[5:19]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
