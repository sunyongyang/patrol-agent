# -*- coding: utf-8 -*-
"""数据库与存储体检。

用法：
    .venv\\Scripts\\python.exe scripts\\inspect_db.py

输出表结构、行数、索引、SQLite 扩展能力与磁盘占用。
排查「数据到底存了什么」时先跑这个。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import DB_PATH, EXPORT_DIR, IMAGE_DIR  # noqa: E402
from shared.db import connect  # noqa: E402


def human(nbytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


def dir_stats(folder: Path, pattern: str) -> tuple[int, int]:
    files = list(folder.glob(pattern))
    return len(files), sum(f.stat().st_size for f in files)


def main() -> int:
    print("=" * 68)
    print("存储体检")
    print("=" * 68)

    print(f"\n数据库   {DB_PATH}")
    if DB_PATH.exists():
        print(f"         大小 {human(DB_PATH.stat().st_size)}")
        for suffix in ("-wal", "-shm"):
            side = Path(str(DB_PATH) + suffix)
            if side.exists():
                print(f"         伴随文件 {side.name} {human(side.stat().st_size)}")
    else:
        print("         （还不存在，跑一次服务会自动建）")

    n, size = dir_stats(IMAGE_DIR, "*.jpg")
    print(f"\n抓拍图   {IMAGE_DIR}")
    print(f"         {n} 张，{human(size)}")
    csv_n, csv_size = dir_stats(EXPORT_DIR, "*.csv")
    pdf_n, pdf_size = dir_stats(EXPORT_DIR, "*.pdf")
    print(f"\n导出     {EXPORT_DIR}")
    print(f"         {pdf_n} 个 PDF，{human(pdf_size)}")
    print(f"         {csv_n} 个 CSV，{human(csv_size)}")

    with connect() as conn:
        print("\n" + "-" * 68)
        print("表与行数")
        print("-" * 68)
        tables = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for t in tables:
            cnt = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({t})")]
            print(f"  {t:<16} {cnt:>6} 行   ({len(cols)} 列)")

        print("\n" + "-" * 68)
        print("索引")
        print("-" * 68)
        for r in conn.execute(
            "SELECT name, tbl_name FROM sqlite_master WHERE type='index' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY tbl_name, name"
        ):
            print(f"  {r['name']:<30} on {r['tbl_name']}")

        print("\n" + "-" * 68)
        print("SQLite 能力")
        print("-" * 68)
        print(f"  版本          {conn.execute('SELECT sqlite_version()').fetchone()[0]}")
        print(f"  journal_mode  {conn.execute('PRAGMA journal_mode').fetchone()[0]}")
        print(f"  page_size     {conn.execute('PRAGMA page_size').fetchone()[0]}")

        caps = {}
        for name, ddl in (
            ("FTS5 全文检索", "CREATE VIRTUAL TABLE _p USING fts5(x)"),
            ("JSON1 函数", None),
        ):
            try:
                if ddl:
                    conn.execute(ddl)
                    conn.execute("DROP TABLE _p")
                else:
                    conn.execute("SELECT json_extract('{\"a\":1}', '$.a')")
                caps[name] = "可用"
            except Exception as exc:  # noqa: BLE001
                caps[name] = f"不可用（{exc}）"
        for k, v in caps.items():
            print(f"  {k:<14} {v}")

        print("\n" + "-" * 68)
        print("数据构成")
        print("-" * 68)
        rows = conn.execute(
            "SELECT is_mock, COUNT(*) AS n FROM detection GROUP BY is_mock"
        ).fetchall()
        for r in rows:
            label = "模拟数据" if r["is_mock"] else "YOLO 真实推理"
            print(f"  detection {label:<16} {r['n']:>6} 条")
        print()
        for r in conn.execute(
            "SELECT alert_type, status, COUNT(*) AS n FROM alert "
            "GROUP BY alert_type, status ORDER BY alert_type, status"
        ):
            print(f"  alert {r['alert_type']:<10} {r['status']:<16} {r['n']:>4} 条")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
