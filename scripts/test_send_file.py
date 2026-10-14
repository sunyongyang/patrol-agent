# -*- coding: utf-8 -*-
"""验证飞书「发文件」通道（不启动长连接）。

用法：
    .venv\\Scripts\\python.exe scripts\\test_send_file.py              # 发最新的导出 CSV
    .venv\\Scripts\\python.exe scripts\\test_send_file.py 路径.csv

为什么单独做这个：
    卡片、图片、文本三种消息类型都实测过了，唯独「文件」是新引入的
    （im/v1/files 上传 + msg_type=file 发送）。这条链路要先确认可用，
    否则用户在飞书里点「导出 CSV」会直接失败。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_service.im.feishu import FeishuAdapter  # noqa: E402
from shared import EXPORT_DIR  # noqa: E402

OK, FAIL = "[OK]", "[FAIL]"


def main() -> int:
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    else:
        csvs = sorted(EXPORT_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not csvs:
            print(f"{FAIL} {EXPORT_DIR} 下没有 CSV，先跑一次导出")
            print("      .venv\\Scripts\\python.exe scripts\\test_agent_local.py \"导出最近 7 天的告警\"")
            return 1
        target = csvs[0]

    if not target.exists():
        print(f"{FAIL} 文件不存在: {target}")
        return 1

    size_kb = target.stat().st_size / 1024
    print(f"待发送文件: {target.name}（{size_kb:.1f} KB）")

    adapter = FeishuAdapter()
    token = adapter.token(force=True)
    print(f"{OK} 凭证有效（token {token[:6]}…）")

    contacts = adapter.known_contacts()
    if not contacts:
        print(f"{FAIL} 没有已知联系人 —— 请先用飞书给机器人发一条消息")
        return 1
    print(f"      {len(contacts)} 个联系人")

    key = adapter.upload_file(str(target))
    if not key:
        print(f"{FAIL} 上传失败（看上面的错误码）")
        return 1
    print(f"{OK} 上传成功 file_key={key[:24]}…")

    sent = 0
    for open_id, info in contacts.items():
        chat_id = info.get("chat_id")
        mid = adapter.send_file(chat_id, str(target))
        if mid:
            sent += 1
            print(f"{OK} 已发送给 {open_id}（message_id={mid}）")
        else:
            print(f"{FAIL} 发送给 {open_id} 失败")
        adapter.send_text(
            chat_id,
            f"📄 通道验证：这是一次手动的 CSV 发送测试\n文件：{target.name}（{size_kb:.1f} KB）",
        )

    print(f"\n{'[OK]' if sent else FAIL} 完成，成功 {sent}/{len(contacts)}")
    return 0 if sent else 1


if __name__ == "__main__":
    raise SystemExit(main())
