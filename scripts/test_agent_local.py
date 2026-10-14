# -*- coding: utf-8 -*-
"""编排层本地测试（不经过飞书）。

用法：
    .venv\\Scripts\\python.exe scripts\\test_agent_local.py
    .venv\\Scripts\\python.exe scripts\\test_agent_local.py "让机器人去巡逻一圈"

为什么先测这一层：
    把「LLM + 工具 + 卡片渲染」和「飞书通道」的问题分开定位。
    通道已经单独验证过了（scripts/listen_feishu.py），
    这里挂了就是编排层的问题，一目了然。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_service.core import orchestrator  # noqa: E402
from agent_service.report import feishu_card as fc  # noqa: E402
from shared import db, repo  # noqa: E402

OK, FAIL, INFO = "[OK]", "[FAIL]", "[..]"

CHAT = "test_chat"
USER = "ou_test_user"


def show(replies: list[orchestrator.Reply]) -> None:
    for r in replies:
        if r.kind == "card":
            print("      ┌─ 卡片 ────────────────────────────────")
            for line in orchestrator._card_to_text(r.card or {}).splitlines():  # noqa: SLF001
                print(f"      │ {line}")
            print("      └───────────────────────────────────────")
        elif r.kind == "text":
            print(f"      文本: {r.text}")
        elif r.kind == "image":
            print(f"      图片: {r.image_path}")
        elif r.kind == "file":
            print(f"      文件: {r.file_path}")
            if r.caption:
                print(f"      说明: {r.caption.replace(chr(10), ' / ')}")
            # 把 CSV 前几行打出来，确认编码与列名
            try:
                with open(r.file_path, encoding="utf-8-sig", newline="") as fh:
                    head = fh.read().splitlines()[:3]
                print("      CSV 前 3 行:")
                for line in head:
                    print(f"        {line[:120]}")
                raw = Path(r.file_path).read_bytes()[:3]
                has_bom = raw == bytes([0xEF, 0xBB, 0xBF])
                verdict = "utf-8-sig 正确" if has_bom else "缺少 BOM，Excel 打开会乱码!"
                print(f"      文件头 3 字节: {raw.hex(' ')}  -> {verdict}")
            except Exception as exc:  # noqa: BLE001
                print(f"      [FAIL] 读取 CSV 失败: {exc}")


def main() -> int:
    db.init_db()
    repo.ensure_base_entities()

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")

    cases = sys.argv[1:] or [
        "昨天的巡逻报表",
        "现在有哪些没处理的告警",
        "上周有没有遗留物的告警",
        "让机器人去巡逻一圈",
        "机器人现在在哪",
        "你能做什么",
        "昨天晚上有没有烟火告警",   # 能力边界测试：应当如实说没有该能力
    ]

    print("=" * 72)
    print(f"编排层本地测试（今天={today}，昨天={yesterday}）")
    print("=" * 72)

    for i, text in enumerate(cases, start=1):
        print(f"\n[{i}/{len(cases)}] 用户：{text}")
        try:
            replies = orchestrator.handle_text(CHAT, USER, text)
            show(replies)
            print(f"      {OK} 返回 {len(replies)} 条")
        except Exception as exc:  # noqa: BLE001
            print(f"      {FAIL} {type(exc).__name__}: {exc}")
            import traceback

            traceback.print_exc()

    # 验证 F1：相对时间是否被正确换算
    print("\n" + "=" * 72)
    print("F1 校验：LLM 是否把相对时间换算成正确日期")
    print("=" * 72)
    pending = repo.latest_pending_action(CHAT)
    if pending:
        payload = pending["payload"]
        print(f"      {INFO} 最近一条 pending_action：{pending['id']} {pending['intent']}")
        print(f"      payload = {payload}")
    else:
        print(f"      {INFO} 无待确认动作（本次用例里可能没触发下发）")

    print("\n" + "=" * 72)
    print("按钮回调测试：确认下发")
    print("=" * 72)
    pending = repo.latest_pending_action(CHAT)
    if pending:
        result = orchestrator.handle_card_action(
            CHAT, USER, {"act": fc.ACT_TASK_CONFIRM, "pa_id": pending["id"]}
        )
        print(f"      toast = {result.toast}")
        if result.response_card:
            for line in orchestrator._card_to_text(result.response_card).splitlines():  # noqa: SLF001
                print(f"      │ {line}")
    else:
        print(f"      {INFO} 没有待确认动作，跳过（先跑一次「让机器人去巡逻一圈」）")

    print(f"\n{INFO} 库状态：{db.table_counts()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
