# -*- coding: utf-8 -*-
"""编排层（Spec §6.1）。

    收到消息 → 载入上下文 → LLM 意图识别（tool_calls）
            → 执行工具 → 渲染卡片 → 回复

写操作的路径被刻意做成两段：`prepare_patrol_task` 只生成确认卡，
真正的下发在用户点按钮后的 `execute_patrol_task`。
这是「LLM 幻觉不得直接作用于物理世界」的唯一保障（Spec §6.4）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agent_service.core import intents
from agent_service.core.llm import LLMError, client
from agent_service.core.memory import memory
from agent_service.report import builder, csv_export, feishu_card as fc, plain_text
from agent_service.report.builder import AlertItem
from agent_service.tools import patrol as T

log = logging.getLogger("agent_service.orchestrator")

MAX_TOOL_ROUNDS = 3

_REPORT_QUERY_TERMS = re.compile(
    r"报表|报告|日报|周报|月报|导出|下载|查询|查看|获取|历史|最新一次|本次任务|功能清单"
)
_QUESTION_TERMS = re.compile(
    r"有没有|是否|是不是|能不能|能否|可不可以|可以吗|要不要|怎么|如何|为什么|"
    r"是什么意思|是什么含义|如何理解|吗|[？?]"
)
_PATROL_NEGATION_TERMS = re.compile(
    r"不要|别(?:让|再|去)?|不能|不可以|不需要|无需|不用|取消|停止|不想"
)
_NON_COMMAND_CONTEXT_TERMS = re.compile(
    r"[\"“”‘’「」『』]|建议|认为|觉得|听说|提到|举例|例如|比如|引用|转述|如果|假如|假设|要是|若|"
    r"(?:领导|客户|用户|他|她|他们).{0,4}(?:说|问)"
)
_PATROL_START_COMMAND = re.compile(
    r"(?:"
    r"(?:让|叫|派|安排|调度|启动|开始|下发|执行|进行).{0,12}"
    r"(?:机器人.{0,6})?(?:巡逻|巡检|巡视)"
    r"|(?:帮我|请).{0,6}(?:机器人.{0,6})?(?:巡逻|巡检|巡视)"
    r"|(?:机器人)?(?:现在)?(?:去)?(?:巡逻|巡检|巡视)(?:一圈|一次|一下|一遍)?[吧。！!]*"
    r")"
)


def _explicitly_requests_patrol(text: str) -> bool:
    """Only allow task preparation for an unambiguous start/dispatch request."""
    if (
        _REPORT_QUERY_TERMS.search(text)
        or _QUESTION_TERMS.search(text)
        or _PATROL_NEGATION_TERMS.search(text)
        or _NON_COMMAND_CONTEXT_TERMS.search(text)
    ):
        return False
    return bool(_PATROL_START_COMMAND.search(text.strip()))


@dataclass
class Reply:
    """一条要发出的消息。"""

    kind: str                       # card | text | image | file
    card: dict | None = None
    text: str | None = None
    image_path: str | None = None
    file_path: str | None = None
    caption: str | None = None
    # 卡片消息发出后需要记住其 message_id（用于后续就地更新）
    remember_as_alert_card: str | None = None   # alert_id


@dataclass
class ActionResult:
    """卡片按钮回调的处理结果。"""

    response_card: dict | None = None           # 回调响应里直接返回，就地更新
    replies: list[Reply] = field(default_factory=list)
    toast: str = "已处理"


# ==================================================================== 工具分发


def _alert_replies(
    items: list[AlertItem], title: str, condition: str, chat_id: str
) -> list[Reply]:
    memory.remember_alerts(chat_id, [a.alert_id for a in items])
    return [Reply(kind="card", card=fc.list_card(title, condition, items))]


def _export_replies(items: list[AlertItem], condition: str) -> list[Reply]:
    """生成 CSV 并发给用户。

    CSV 用 utf-8-sig 编码（带 BOM），否则 Excel 打开中文全是乱码 ——
    这是中文 CSV 最常见的投诉，见 report/csv_export.py。
    """
    if not items:
        return [
            Reply(
                kind="card",
                card=fc.result_card(
                    "📄 没有可导出的记录",
                    "当前条件下一条记录都没有。\n可以放宽条件，比如「导出最近 7 天全部告警」。",
                    ok=False,
                ),
            )
        ]
    try:
        path = T.build_export(items, condition)
    except Exception as exc:  # noqa: BLE001
        log.exception("生成 CSV 失败")
        return [Reply(kind="card", card=fc.result_card(
            "⚠️ 导出失败", f"{type(exc).__name__}: {exc}", ok=False))]

    return [
        Reply(
            kind="file",
            file_path=str(path),
            caption=f"📄 告警导出 · {csv_export.describe(items)}\n文件：{path.name}",
        )
    ]


def _patrol_pdf_replies(path: Any, summary: dict) -> list[Reply]:
    if path is None:
        return [Reply(kind="card", card=fc.result_card(
            "📄 没有巡逻记录", f"{summary.get('day', '所选范围')} 没有可导出的巡逻轮次。", ok=False))]

    caption = (
        f"巡逻报告 PDF · {summary['day']}\n"
        f"{summary['runs']} 轮，{summary['points']} 个点位；"
        f"异常 {summary['abnormal']}，正常 {summary['normal']}，未完成 {summary['unfinished']}。"
    )
    if summary.get("image_missing"):
        caption += f"\n有 {summary['image_missing']} 个点位的历史图片文件缺失。"
    if summary.get("has_mock"):
        caption += "\n报告含模拟检测数据。"
    return [Reply(kind="file", file_path=str(path), caption=caption)]


def _run_tool(
    name: str, args: dict, chat_id: str, user_id: str, user_text: str
) -> list[Reply]:
    log.info("执行工具 %s(%s)", name, json.dumps(args, ensure_ascii=False))

    if name == "get_daily_report":
        rpt = T.get_daily_report(args.get("date"))
        return [Reply(kind="card", card=fc.report_card(rpt))]

    if name == "list_alerts":
        status = args.get("status")
        items = T.get_alert_list(
            status=status,
            alert_type=args.get("alert_type"),
            point_id=args.get("point_id"),
            limit=int(args.get("limit") or 5),
        )
        cond_bits = []
        if status:
            cond_bits.append({"pending": "待处置", "confirmed": "已确认",
                              "false_positive": "误报"}.get(status, status))
        if args.get("alert_type"):
            cond_bits.append(args["alert_type"])
        if args.get("point_id"):
            cond_bits.append(args["point_id"])
        cond = "｜".join(cond_bits) or "全部告警"
        return _alert_replies(items, "🚨 告警列表", cond, chat_id)

    if name == "query_history":
        items, cond = T.query_history(
            days_back=int(args.get("days_back") or 7),
            alert_type=args.get("alert_type"),
            point_id=args.get("point_id"),
            limit=int(args.get("limit") or 5),
        )
        return _alert_replies(items, "🔍 历史查询", cond, chat_id)

    if name == "list_tasks":
        tasks = T.list_tasks(status=args.get("status"), limit=int(args.get("limit") or 5))
        if not tasks:
            return [Reply(kind="card",
                          card=fc.text_card("📋 任务列表", "当前没有任何任务记录。"))]
        status_cn = {"pending": "待执行", "running": "执行中", "completed": "已完成",
                     "cancelled": "已取消", "failed": "失败", "rejected": "已拒绝"}
        lines = [f"共 {len(tasks)} 条："]
        for t in tasks:
            lines.append(
                f"· `{t['id']}` {t['route_id']}　"
                f"{status_cn.get(t['status'], t['status'])}　{t['created_at'][11:16]}"
            )
        return [Reply(kind="card", card=fc.text_card("📋 任务列表", "\n".join(lines), "blue"))]

    if name == "prepare_patrol_task":
        if not _explicitly_requests_patrol(user_text):
            log.warning("拒绝非明确巡逻指令触发任务准备：%s", user_text[:100])
            return [Reply(
                kind="text",
                text=(
                    "这条消息没有明确要求开始巡逻，我没有创建任务。"
                    "查看日报可说「获取今天的巡逻日报」；"
                    "下载逐点报告可说「导出今天的巡逻报告 PDF」。"
                ),
            )]
        pa_id, rows = T.prepare_patrol_task(
            route_id=args.get("route_id"),
            chat_id=chat_id,
            user_id=user_id,
        )
        memory.remember_pending(chat_id, pa_id)
        return [Reply(kind="card", card=fc.confirm_card("任务确认", rows, pa_id))]

    if name == "cancel_task":
        res = T.cancel_task(args.get("task_id"))
        return [Reply(kind="card", card=fc.result_card(
            "🗑 任务已取消",
            f"任务 `{res['task_id']}` 已取消。",
            ok=bool(res.get("cancelled")),
        ))]

    if name == "get_robot_status":
        return [Reply(kind="card", card=fc.text_card("🤖 机器人状态", T.robot_status_text()))]

    if name == "capture_now":
        point_id = args.get("point_id") or T.available_points()[0].id
        res = T.capture_now(point_id)
        # res["detections"] 已经只含该点位绑定算法的结果（见 robot_sim/robot.py）
        dets = res.get("detections") or []
        alg = res.get("algorithm_name") or ""
        desc = (
            "、".join(f"{d['label']}={d['confidence']:.2f}" for d in dets)
            if dets else "未检出绑定算法的目标"
        )
        return [
            Reply(
                kind="image",
                image_path=res.get("image_path"),
                caption=f"📷 {res.get('point_name')} 算法结果图",
            ),
            Reply(
                kind="text",
                text=f"{res.get('point_name')}｜绑定算法：{alg}\n检出：{desc}",
            ),
        ]

    if name == "handle_alert":
        alert_id = args.get("alert_id")
        if not alert_id and args.get("index"):
            alert_id = memory.resolve_alert_index(chat_id, int(args["index"]))
            if not alert_id:
                return [Reply(kind="card", card=fc.result_card(
                    "⚠️ 找不到对应记录",
                    "我记不清你指的是哪一条了，请重新查一次列表再处置。",
                    ok=False,
                ))]
        res = T.handle_alert(alert_id, args.get("action"), user_id=user_id,
                             note=args.get("note") or "")
        label = "已确认告警" if res["status"] == "confirmed" else "已标记为误报"
        body = f"告警 `{res['alert_id']}` 已处置。"
        if res.get("note"):
            body += f"\n处置结论：{res['note']}"
        return [Reply(kind="card", card=fc.result_card(
            f"✅ {res['alert_type']} · {label}", body))]

    if name == "advance_patrol":
        msg = T.advance_patrol()
        return [Reply(kind="card", card=fc.text_card("⏩ 已加速", msg))]

    if name == "export_alerts":
        items, cond = T.query_history(
            days_back=int(args.get("days_back") or 7),
            alert_type=args.get("alert_type"),
            point_id=args.get("point_id"),
            status=args.get("status"),
            limit=int(args.get("limit") or 2000),
        )
        return _export_replies(items, cond)

    if name == "export_patrol_report":
        scope = args.get("scope")
        task_id = args.get("task_id")
        date = args.get("date")

        if task_id:
            build_args = {"task_id": task_id, "created_by": user_id}
        elif scope == "latest":
            latest = T.latest_report_runs(1, created_by=user_id)
            if not latest:
                return _patrol_pdf_replies(None, {"day": "所有日期"})
            build_args = {"run_id": latest[0]["id"], "created_by": user_id}
        elif scope == "task":
            return [Reply(kind="card", card=fc.patrol_report_task_card(
                T.latest_report_runs(10, created_by=user_id)))]
        elif scope in ("day", "week", "month") and date:
            build_args = {"day": date, "scope": scope}
        elif date:
            build_args = {"day": date, "scope": "day"}
        elif scope in ("day", "week", "month"):
            periods = T.available_report_periods(scope)
            return [Reply(kind="card", card=fc.patrol_report_period_card(scope, periods))]
        else:
            return [Reply(kind="card", card=fc.patrol_report_scope_card())]

        try:
            path, summary = T.build_patrol_pdf(**build_args)
        except ValueError as exc:
            return [Reply(kind="card", card=fc.result_card("⚠️ 日期格式不正确", str(exc), ok=False))]
        except Exception as exc:  # noqa: BLE001
            log.exception("生成巡逻 PDF 失败")
            return [Reply(kind="card", card=fc.result_card(
                "⚠️ PDF 报告生成失败", f"{type(exc).__name__}: {exc}", ok=False))]
        return _patrol_pdf_replies(path, summary)

    if name == "show_help":
        return [Reply(kind="card", card=fc.help_card())]

    return [Reply(kind="text", text=f"（内部错误：没有名为 {name} 的工具）")]


# ==================================================================== 文本入口


def handle_text(chat_id: str, user_id: str, text: str) -> list[Reply]:
    """处理一条用户消息，返回要发送的消息列表。"""
    memory.add_user(chat_id, text)
    llm = client()
    messages = memory.messages(chat_id, intents.system_prompt())

    replies: list[Reply] = []
    for round_no in range(MAX_TOOL_ROUNDS):
        try:
            message, elapsed = llm.chat(messages, tools=intents.TOOLS)
        except LLMError as exc:
            log.error("LLM 调用失败: %s", exc)
            return [Reply(kind="text", text=f"⚠️ 我这边出错了：{exc}")]

        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            content = (message.get("content") or "").strip()
            if content:
                replies.append(Reply(kind="text", text=content))
                memory.add_assistant(chat_id, content)
            break

        # 把 assistant 的 tool_call 也塞回上下文，保持协议完整
        messages.append({
            "role": "assistant",
            "content": message.get("content") or "",
            "tool_calls": tool_calls,
        })

        for call in tool_calls:
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}

            try:
                out = _run_tool(name, args, chat_id, user_id, text)
            except T.ToolError as exc:
                out = [Reply(kind="card", card=fc.result_card("⚠️ 操作未完成", str(exc), ok=False))]
            except Exception as exc:  # noqa: BLE001
                log.exception("工具 %s 执行异常", name)
                out = [Reply(kind="card", card=fc.result_card(
                    "⚠️ 操作未完成", f"{type(exc).__name__}: {exc}", ok=False))]

            replies.extend(out)
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id") or "",
                "content": _tool_result_digest(name, out),
            })

        # 有工具结果就不再追问 LLM 生成自然语言，直接返回卡片
        # （卡片已经承载了信息，再让 LLM 复述一遍只会重复刷屏）
        break

    # ⚠️ 只有「真正的文本回复」才写入对话历史。
    #
    # 这里踩过两次坑：最初把卡片渲染成 `[卡片] 标题` 写进 assistant 轮次，
    # LLM 把它当成输出格式，回复用户一句 `[卡片] 🚀 巡逻任务确认`（没有数据）。
    # 改成 `（已向用户展示卡片「…」）` 之后仍然被照抄，甚至凭空编出
    # 「📷 点位抓拍」这种不存在的卡片标题。
    #
    # 结论：任何描述「我刚才展示了卡片」的文字，只要进了 assistant 轮次，
    # 就会被当成可模仿的模板。所以卡片/图片一律不写历史 ——
    # 指代消解（「第 2 条是误报」）靠 memory 里的告警 ID 列表，不依赖这段文字。
    text_reply = next((r.text for r in replies if r.kind == "text" and r.text), None)
    if text_reply:
        memory.add_assistant(chat_id, text_reply)
    return replies


def _tool_result_digest(name: str, replies: list[Reply]) -> str:
    kinds = [r.kind for r in replies]
    return json.dumps({"tool": name, "rendered": kinds, "ok": True}, ensure_ascii=False)


# ==================================================================== 按钮回调


def handle_card_action(chat_id: str, user_id: str, value: dict) -> ActionResult:
    """处理卡片按钮回调。返回的 response_card 会被就地写回原卡片。"""
    act = (value or {}).get("act") or ""
    log.info("卡片动作 %s %s", act, json.dumps(value, ensure_ascii=False)[:200])

    try:
        if act == fc.ACT_TASK_CONFIRM:
            res = T.execute_patrol_task(value["pa_id"], user_id=user_id)
            card = fc.result_card(
                "✅ 任务已下发",
                f"任务号：`{res['task_id']}`\n"
                f"路线：{res['route_name']}\n"
                f"点位：{' → '.join(res['points'])}\n\n"
                "机器人已出发，完成后会自动推送结果。",
            )
            memory.remember_pending(chat_id, "")
            return ActionResult(response_card=card, toast="任务已下发")

        if act == fc.ACT_TASK_CANCEL:
            T.cancel_pending(value["pa_id"])
            memory.remember_pending(chat_id, "")
            card = fc.result_card("❌ 已取消", "没有下发任何任务。", ok=False)
            return ActionResult(response_card=card, toast="已取消")

        if act in (fc.ACT_ALERT_CONFIRM, fc.ACT_ALERT_FALSE):
            alert_id = value["alert_id"]
            action = "confirm" if act == fc.ACT_ALERT_CONFIRM else "false_positive"
            # 按钮点击拿不到文字结论，先记一条来源说明；
            # 用户随后可以补一句（「第2条是误报，门口在施工」）覆盖它
            default_note = "通过告警卡片按钮处置" if action == "confirm" else "通过告警卡片标记为误报"
            res = T.handle_alert(alert_id, action, user_id=user_id, note=default_note)
            row = _reload_alert(alert_id)
            if row:
                card = fc.alert_resolved_card(
                    row,
                    handled_by=user_id or "飞书用户",
                    handled_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                )
            else:
                label = "已确认" if res["status"] == "confirmed" else "已标记误报"
                card = fc.result_card(f"✅ {res['alert_type']} · {label}",
                                      f"告警 `{alert_id}` 已处置。")
            return ActionResult(response_card=card, toast="已处置")

        # 以下三个是「下钻」动作：发新消息，不销毁用户正在看的原卡片
        if act == fc.ACT_ALERT_DETAIL:
            row = _reload_alert(value["alert_id"])
            if not row:
                return ActionResult(response_card=fc.result_card(
                    "⚠️ 找不到该告警", "记录可能已被清理。", ok=False))
            body = (
                f"告警号：`{row.alert_id}`\n"
                f"类型：{row.alert_type}（{row.level}）\n"
                f"点位：{row.point_name}\n"
                f"首次出现：{row.created_at}\n"
                f"处置状态：{row.status_label}\n"
            )
            if row.repeated:
                body += f"持续：{row.duration_label}　出现 {row.occurrence_count} 次\n"
                body += f"最近一次：{row.last_seen_at or '—'}\n"
            if row.resolution_note:
                body += f"处置结论：{row.resolution_note}\n"
            body += (
                f"\n**原始模型输出**：`{row.coco_label}` @ {row.confidence:.4f}\n"
                f"数据来源：{'模拟检测' if row.is_mock else 'YOLO 真实推理'}\n"
                f"结果图（带检测框）：`{row.image_path or '未留存'}`\n"
                f"原始帧（未绘制）：`{row.raw_image_path or '未留存'}`"
            )
            return ActionResult(
                replies=[Reply(kind="card", card=fc.text_card("🔎 告警详情", body))],
                toast="",
            )

        if act == fc.ACT_ALERTS_MORE:
            items = T.get_alert_list(status=value.get("status"), limit=5)
            memory.remember_alerts(chat_id, [a.alert_id for a in items])
            return ActionResult(
                replies=[Reply(kind="card",
                               card=fc.list_card("🚨 告警列表", "待处置", items))],
                toast="",
            )

        if act == "history":
            items, cond = T.query_history(days_back=int(value.get("days") or 7), limit=5)
            memory.remember_alerts(chat_id, [a.alert_id for a in items])
            return ActionResult(
                replies=[Reply(kind="card", card=fc.list_card("🔍 历史查询", cond, items))],
                toast="",
            )

        if act == fc.ACT_ADVANCE:
            return ActionResult(response_card=fc.text_card("⏩ 已加速", T.advance_patrol()),
                                toast="已加速")

        if act == "export":
            ids = value.get("ids") or []
            items = T.alerts_by_ids(ids) if ids else []
            return ActionResult(
                replies=_export_replies(items, f"列表所选 {len(ids)} 条"),
                toast="已导出",
            )

        if act == fc.ACT_REPORT_SCOPE:
            scope = value.get("scope") or "menu"
            if scope == "menu":
                card = fc.patrol_report_scope_card()
                return ActionResult(response_card=card, toast="")
            if scope == "task":
                card = fc.patrol_report_task_card(
                    T.latest_report_runs(10, created_by=user_id)
                )
                return ActionResult(replies=[Reply(kind="card", card=card)], toast="")
            if scope in ("day", "week", "month"):
                periods = T.available_report_periods(scope)
                card = fc.patrol_report_period_card(scope, periods)
                return ActionResult(replies=[Reply(kind="card", card=card)], toast="")
            if scope == "latest":
                latest = T.latest_report_runs(1, created_by=user_id)
                if not latest:
                    return ActionResult(response_card=fc.result_card(
                        "📄 没有巡逻记录", "当前没有可导出的巡逻轮次。", ok=False))
                path, summary = T.build_patrol_pdf(
                    run_id=latest[0]["id"], created_by=user_id
                )
                return ActionResult(replies=_patrol_pdf_replies(path, summary), toast="")

        if act == fc.ACT_REPORT_PERIOD:
            path, summary = T.build_patrol_pdf(
                start_day=value["start_day"],
                end_day=value["end_day"],
                period_label=value.get("label"),
            )
            return ActionResult(replies=_patrol_pdf_replies(path, summary), toast="")

        if act == fc.ACT_REPORT_RUN:
            path, summary = T.build_patrol_pdf(
                run_id=value["run_id"], created_by=user_id
            )
            return ActionResult(replies=_patrol_pdf_replies(path, summary), toast="")

        if act == fc.ACT_HELP:
            return ActionResult(response_card=fc.help_card(), toast="")

        return ActionResult(
            response_card=fc.result_card("⚠️ 未知操作", f"不认识的按钮动作：{act}", ok=False),
            toast="未知操作",
        )

    except T.ToolError as exc:
        return ActionResult(response_card=fc.result_card("⚠️ 操作未完成", str(exc), ok=False),
                            toast="操作未完成")
    except Exception as exc:  # noqa: BLE001
        log.exception("卡片动作 %s 异常", act)
        return ActionResult(
            response_card=fc.result_card("⚠️ 操作未完成", f"{type(exc).__name__}: {exc}", ok=False),
            toast="出错",
        )


def _reload_alert(alert_id: str) -> AlertItem | None:
    from shared import repo

    row = repo.get_alert(alert_id)
    if not row:
        return None
    from agent_service.report.builder import _to_alert_item

    return _to_alert_item(row)


# ==================================================================== 告警推送


def build_alert_reply(alert_id: str) -> Reply | None:
    """机器人回调触发的告警推送。"""
    row = _reload_alert(alert_id)
    if not row:
        log.warning("告警 %s 不存在，无法推送", alert_id)
        return None
    return Reply(
        kind="card",
        card=fc.alert_card(row),
        remember_as_alert_card=alert_id,
    )


def build_run_finished_reply(summary: dict) -> Reply:
    status_cn = {"completed": "✅ 已完成", "failed": "❌ 失败", "cancelled": "⛔ 已取消"}
    alerts = summary.get("alert_ids") or []
    body = (
        f"轮次：`{summary.get('run_id')}`\n"
        f"状态：{status_cn.get(summary.get('status'), summary.get('status'))}\n"
        f"点位：{summary.get('points_done')}/{summary.get('points_total')}\n"
        f"本次告警：{len(alerts)} 条"
    )
    if summary.get("error"):
        body += f"\n\n错误：{summary['error']}"
    if alerts:
        body += "\n\n" + "\n".join(f"· `{a}`" for a in alerts)
    return Reply(kind="card", card=fc.text_card(
        "🏁 巡逻轮次结束", body,
        "green" if summary.get("status") == "completed" else "orange",
    ))


# ==================================================================== 纯文本（微信）


def handle_text_plain(chat_id: str, user_id: str, text: str) -> list[str]:
    """微信降级入口：同样的意图，输出纯文本。"""
    replies = handle_text(chat_id, user_id, text)
    out: list[str] = []
    for r in replies:
        if r.kind == "text":
            out.append(r.text or "")
        elif r.kind == "card":
            out.append(_card_to_text(r.card or {}))
        elif r.kind == "image":
            out.append(r.caption or "[图片]")
        elif r.kind == "file":
            out.append(
                f"{r.caption or '[文件]'}\n"
                "（当前通道不支持发送文件，CSV 已在服务端生成）"
            )
    return [t for t in out if t]


def _card_to_text(card: dict) -> str:
    header = ((card.get("header") or {}).get("title") or {}).get("content") or "卡片"
    lines = [str(header), "━" * 16]
    for el in card.get("elements") or []:
        tag = el.get("tag")
        if tag == "div":
            lines.append((el.get("text") or {}).get("content") or "")
        elif tag == "note":
            for sub in el.get("elements") or []:
                lines.append(f"（{sub.get('content', '')}）")
        elif tag == "column_set":
            cells = []
            for col in el.get("columns") or []:
                for sub in col.get("elements") or []:
                    cells.append(((sub.get("text") or {}).get("content") or "").replace("\n", " "))
            lines.append("　｜　".join(cells))
        elif tag == "action":
            labels = [((b.get("text") or {}).get("content") or "") for b in el.get("actions") or []]
            lines.append("")
            lines.append("选项：" + "　".join(f"{i}) {l}" for i, l in enumerate(labels, 1)))
        elif tag == "hr":
            lines.append("━" * 16)
    return "\n".join(x for x in lines if x is not None)
