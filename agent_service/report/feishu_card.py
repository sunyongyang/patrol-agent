# -*- coding: utf-8 -*-
"""飞书卡片渲染（交互设计 §5.1 的四个标准模板）。

四个模板：
    report_card   日报/周报 —— 指标栅格 + 异常 TopN
    confirm_card  所有写操作 —— 把 LLM 解析结果摊开，让用户核对
    list_card     历史查询/列表 —— 带缩略图的分页列表
    alert_card    告警推送 —— 可就地更新的状态机卡片

所有按钮的 value 统一用 {"act": "...", ...} 约定，由 im/feishu.py 分发。
"""

from __future__ import annotations

from typing import Any

from agent_service.report.builder import AlertItem, DailyReport

# 按钮动作
ACT_TASK_CONFIRM = "task_confirm"
ACT_TASK_CANCEL = "task_cancel"
ACT_ALERT_CONFIRM = "alert_confirm"
ACT_ALERT_FALSE = "alert_false"
ACT_ALERT_DETAIL = "alert_detail"
ACT_ALERTS_MORE = "alerts_more"
ACT_ADVANCE = "advance"
ACT_HELP = "help"

LEVEL_TEMPLATE = {"urgent": "red", "normal": "orange", "info": "blue"}


def _md(content: str) -> dict:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def _note(content: str) -> dict:
    return {"tag": "note", "elements": [{"tag": "plain_text", "content": content}]}


def _hr() -> dict:
    return {"tag": "hr"}


def _button(text: str, value: dict, type_: str = "default") -> dict:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": type_,
        "value": value,
    }


def _actions(buttons: list[dict]) -> dict:
    return {"tag": "action", "actions": buttons}


def _card(title: str, template: str, elements: list[dict]) -> dict:
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
        "elements": elements,
    }


def _metric_columns(items: list[tuple[str, str, str]]) -> dict:
    """三列指标栅格。items 为 (emoji, 数值, 说明) 三元组列表。"""
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": "grey",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [_md(f"{emoji} **{value}**\n{label}")],
            }
            for emoji, value, label in items
        ],
    }


# ==================================================================== 日报


def report_card(report: DailyReport) -> dict:
    st = report.stats
    ac = report.alert_counts

    metrics = [
        ("✅", f"{st.get('runs_completed', 0)}/{st.get('runs_total', 0)}", "任务完成"),
        ("⚠️", f"{ac.get('total', 0)} 条", "告警"),
        ("🔋", f"{ac.get('pending', 0)} 条", "待处置"),
    ]

    elements: list[dict] = [_metric_columns(metrics), _hr()]

    coverage = f"路径完成率 {st.get('path_completion_rate', 0)}%"
    elements.append(
        _md(
            f"{coverage}（{st.get('points_done', 0)}/{st.get('points_total', 0)} 个点位）\n"
            f"拍照 {st.get('captures', 0)} 次　·　检出目标 {st.get('detections', 0)} 个"
        )
    )

    if report.breakdown:
        dist = "、".join(f"{b['alert_type']} {b['n']}" for b in report.breakdown)
        elements.append(_md(f"类型分布：{dist}"))

    elements.append(_hr())

    if report.top_alerts:
        lines = ["**重点异常**"]
        for i, a in enumerate(report.top_alerts, start=1):
            mark = "⚠️" if a.status == "pending" else "　"
            extra = ""
            if a.repeated:
                extra += f"　持续{a.duration_label}({a.occurrence_count}次)"
            if a.target_count > 1:
                extra += f"　{a.target_count}个目标"
            lines.append(
                f"{i}. {a.emoji} {a.alert_type} · {a.point_name} · {a.time_hm}"
                f"　[{a.status_label}] {mark}{extra}"
            )
        elements.append(_md("\n".join(lines)))
    else:
        elements.append(_md("**重点异常**\n无"))

    elements.append(_hr())

    notes = [report.coverage_note]
    if report.has_mock:
        notes.append(
            f"⚠️ 含模拟检测数据：{st.get('mock_detections', 0)}/"
            f"{st.get('detections', 0)} 条为模拟，非真实推理结果。"
        )
    elements.append(_note("　".join(notes)))

    buttons = [
        _button("查看待处置告警", {"act": ACT_ALERTS_MORE, "status": "pending"}, "primary"),
        _button("看最近 7 天", {"act": "history", "days": 7}),
    ]
    elements.append(_actions(buttons))

    return _card(f"📊 巡逻日报 · {report.label}", "blue", elements)


# ==================================================================== 确认卡


def confirm_card(title: str, rows: list[tuple[str, str]], pa_id: str,
                 hint: str | None = None) -> dict:
    """写操作的确认卡。

    曾经这里有个「✏️ 修改时间」按钮，暗示支持定时巡逻 ——
    但 task.start_at 从来没被任何调度器读取过，点了确认机器人会立刻出发。
    功能没实现不是问题，**承诺了做不到才是**。所以这个按钮已移除。
    """
    if hint is None:
        hint = (
            "请核对以上解析结果，确认后才会真正下发。\n"
            "当前版本只支持立即执行，定时与重复巡逻暂不支持。"
        )
    body = "\n".join(f"**{k}**：{v}" for k, v in rows)
    elements = [
        _md(body),
        _hr(),
        _note(hint),
        _actions([
            _button("✅ 确认下发", {"act": ACT_TASK_CONFIRM, "pa_id": pa_id}, "primary"),
            _button("❌ 取消", {"act": ACT_TASK_CANCEL, "pa_id": pa_id}, "danger"),
        ]),
    ]
    return _card(f"📋 {title}", "turquoise", elements)


# ==================================================================== 列表卡


def list_card(title: str, condition: str, items: list[AlertItem],
              empty_hint: str = "没有符合条件的记录。\n可以放宽条件试试，比如「最近 7 天全部告警」。",
              extra_buttons: list[dict] | None = None) -> dict:
    elements: list[dict] = [_md(f"🔍 {condition}"), _hr()]

    if not items:
        elements.append(_md(f"**0 条**\n{empty_hint}"))
    else:
        lines = [f"共 **{len(items)}** 条："]
        for i, a in enumerate(items, start=1):
            tags = []
            if a.repeated:
                tags.append(f"持续{a.duration_label}·{a.occurrence_count}次")
            if a.target_count > 1:
                tags.append(f"{a.target_count}个目标")
            if a.is_mock:
                tags.append("模拟")
            tail = ("　`" + "·".join(tags) + "`") if tags else ""
            lines.append(
                f"{i}. {a.emoji} **{a.alert_type}** · {a.point_name} · {a.time_short}\n"
                f"　　conf={a.confidence:.2f} · {a.status_label}{tail}"
            )
        elements.append(_md("\n".join(lines)))

    buttons = list(extra_buttons or [])
    if items:
        buttons.append(_button("导出 CSV", {"act": "export", "ids": [a.alert_id for a in items]}))
    if buttons:
        elements.append(_hr())
        elements.append(_actions(buttons))

    return _card(title, "blue", elements)


# ==================================================================== 告警卡


def alert_card(a: AlertItem, img_key: str | None = None) -> dict:
    """告警推送卡。处置或抑制后由 im/feishu.py 用同一结构重新渲染并就地更新。

    抑制后的卡片会显示「已持续 X · 出现 N 次」——
    这条比「又来一条新告警」有用得多：值班员一眼知道这是**同一个**目标还在那儿。
    """
    head = (
        f"**点位**：{a.point_name}　　**机器人**：R-01\n"
        f"**置信度**：{a.confidence:.2f}（最近一次）\n"
        f"**原始模型输出**：`{a.coco_label}` @ {a.confidence:.2f}"
        + ("　`模拟检测`" if a.is_mock else "")
    )
    if a.target_count > 1:
        head += f"\n\n👥 **画面中检测到 {a.target_count} 个目标**"
    if a.repeated:
        head += (
            f"\n\n⏱ **已持续 {a.duration_label}　出现 {a.occurrence_count} 次**\n"
            f"首次：{a.created_at[11:19]}　最近：{(a.last_seen_at or '')[11:19] or a.time_hm}"
        )
    elif a.target_count <= 1:
        head += f"\n\n**时间**：{a.time_short}"

    elements: list[dict] = [_md(head), _hr()]

    if img_key:
        elements.append({"tag": "img", "img_key": img_key, "alt": {"tag": "plain_text", "content": "算法结果图"}})
    else:
        elements.append(_note(f"算法结果图：{a.image_path or '未留存'}"))

    if a.status == "pending":
        elements.append(
            _actions([
                _button("✅ 确认告警", {"act": ACT_ALERT_CONFIRM, "alert_id": a.alert_id}, "primary"),
                _button("❌ 标记误报", {"act": ACT_ALERT_FALSE, "alert_id": a.alert_id}, "danger"),
                _button("👀 查看详情", {"act": ACT_ALERT_DETAIL, "alert_id": a.alert_id}),
            ])
        )
    else:
        elements.append(_md(f"**已处置**：{a.status_label}"))

    template = LEVEL_TEMPLATE.get(a.level_code, "orange")
    title = f"🚨 新告警 · {a.alert_type}"
    if a.repeated:
        title = f"🚨 持续告警 · {a.alert_type}（第 {a.occurrence_count} 次）"
    return _card(title, template, elements)


def alert_resolved_card(a: AlertItem, handled_by: str, handled_at: str,
                        img_key: str | None = None) -> dict:
    """处置后就地更新用的卡片。"""
    label = {"confirmed": "✅ 已确认告警", "false_positive": "❌ 已标记为误报"}.get(
        a.status, f"已处置（{a.status}）"
    )
    duration = ""
    if a.repeated:
        duration = f"\n告警持续：{a.duration_label}　共出现 {a.occurrence_count} 次"
    elements: list[dict] = [
        _md(
            f"**{label}**\n"
            f"点位：{a.point_name}　首次：{a.time_short}{duration}\n"
            f"处理人：{handled_by or '未知'}　处理时间：{handled_at}\n\n"
            f"原始模型输出：`{a.coco_label}` @ {a.confidence:.2f}"
            + (f"\n处置结论：{a.resolution_note}" if a.resolution_note else "")
        )
    ]
    if img_key:
        elements.append({"tag": "img", "img_key": img_key, "alt": {"tag": "plain_text", "content": "算法结果图"}})
    elements.append(_hr())
    elements.append(_note("该记录已从待处置列表移除。"))
    return _card(f"✅ {a.alert_type} · 已处置", "green", elements)


# ==================================================================== 其它


def help_card() -> dict:
    elements = [
        _md(
            "**我能做什么**\n\n"
            "**📊 取报表**\n"
            "「昨天的巡逻报表」「现在有哪些没处理的告警」\n\n"
            "**📋 下发任务**\n"
            "「让机器人去巡逻一圈」（会先弹确认卡）\n"
            "「有哪些任务在跑」「把那个任务停了」\n\n"
            "**🔍 查历史**\n"
            "「上周有没有人员闯入的告警」「最近 7 天的通道占用」\n"
            "「导出最近 7 天的告警」（会发你一个 CSV 文件）\n\n"
            "**🤖 机器人与抓拍**\n"
            "「机器人现在在哪」「拍一张现在的照片」\n\n"
            "**🚨 处置告警**\n"
            "「第 2 条是误报」「确认第 1 条」"
        ),
        _hr(),
        _note(
            "当前检测能力：人员闯入（person）、通道占用（chair）。"
            "烟火、翻墙、漏水等场景本版本不具备。"
        ),
    ]
    return _card("🆘 巡逻 Agent · 能力清单", "blue", elements)


def text_card(title: str, body: str, template: str = "blue") -> dict:
    return _card(title, template, [_md(body)])


def result_card(title: str, body: str, ok: bool = True) -> dict:
    return _card(title, "green" if ok else "red", [_md(body)])
