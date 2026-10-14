# -*- coding: utf-8 -*-
"""微信降级渲染（交互设计 §5.2）。

微信没有交互卡片、没有按钮，只能发文本 + 图片。
所以交互退化为**编号回复协议**：Agent 编号列出选项，用户回复数字。
这不是设计缺陷，是通道能力决定的。
"""

from __future__ import annotations

from agent_service.report.builder import AlertItem, DailyReport

SEP = "━" * 16


def render_daily(r: DailyReport) -> str:
    st, ac = r.stats, r.alert_counts
    lines = [
        f"📊 巡逻日报 · {r.label}",
        SEP,
        f"✅ 任务完成 {st.get('runs_completed', 0)}/{st.get('runs_total', 0)}　"
        f"覆盖率 {st.get('coverage_rate', 0)}%",
        f"⚠️ 告警 {ac.get('total', 0)} 条　待处置 {ac.get('pending', 0)} 条",
        f"🔍 检测记录 {st.get('detections', 0)} 条",
        SEP,
    ]
    if r.top_alerts:
        lines.append("重点异常：")
        for i, a in enumerate(r.top_alerts, start=1):
            mark = " ⚠️" if a.status == "pending" else ""
            lines.append(f"{i}. {a.emoji} {a.alert_type}·{a.point_name}·{a.time_hm} [{a.status_label}]{mark}")
    else:
        lines.append("重点异常：无")

    if r.has_mock:
        lines.append("⚠️ 含模拟检测数据，非真实推理结果。")
    lines.append("")
    lines.append("回复 1 看待处置告警｜2 看最近 7 天")
    return "\n".join(lines)


def render_alert_list(condition: str, items: list[AlertItem]) -> str:
    lines = [f"🔍 {condition}", SEP]
    if not items:
        lines.append("0 条。")
        lines.append("可以放宽条件，比如「最近 7 天全部告警」。")
        return "\n".join(lines)

    lines.append(f"共 {len(items)} 条：")
    for i, a in enumerate(items, start=1):
        lines.append(
            f"{i}. {a.emoji} {a.alert_type}·{a.point_name}·{a.time_short}\n"
            f"   conf={a.confidence:.2f} [{a.status_label}]"
        )
    lines.append("")
    lines.append(f"回复 1-{min(len(items), 9)} 查看该条详情")
    return "\n".join(lines)


def render_alert(a: AlertItem) -> str:
    lines = [
        f"🚨 新告警 · {a.alert_type}",
        SEP,
        f"点位：{a.point_name}",
        f"时间：{a.time_short}",
        f"置信度：{a.confidence:.2f}",
        f"原始模型输出：{a.coco_label} @ {a.confidence:.2f}",
    ]
    if a.is_mock:
        lines.append("⚠️ 模拟检测（非真实推理）")
    lines.append("")
    lines.append("回复 1 确认告警｜2 标记误报")
    return "\n".join(lines)


def render_confirm(title: str, rows: list[tuple[str, str]]) -> str:
    lines = [f"📋 {title}", SEP]
    lines.extend(f"{k}：{v}" for k, v in rows)
    lines.append(SEP)
    lines.append("请核对以上解析结果，确认后才会真正下发。")
    lines.append("回复 1 确认下发｜2 取消")
    return "\n".join(lines)


def render_result(title: str, body: str) -> str:
    return f"{title}\n{SEP}\n{body}"


def render_help() -> str:
    return "\n".join([
        "🆘 我能做什么",
        SEP,
        "📊 取报表",
        "  「昨天的巡逻报表」「有哪些没处理的告警」",
        "📋 下发任务",
        "  「让机器人去巡逻一圈」（会先让你确认）",
        "🔍 查历史",
        "  「上周有没有人员闯入的告警」",
        "🤖 机器人",
        "  「机器人现在在哪」「拍一张照片」",
        SEP,
        "当前检测能力：人员闯入（person）、遗留物（book）。",
        "烟火、翻墙、漏水等本版本不具备。",
    ])
