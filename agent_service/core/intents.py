# -*- coding: utf-8 -*-
"""意图与工具定义（交互设计 §3 的 12 个意图 → 11 个工具）。

系统提示词里**必须注入当前日期** —— 实测不注入时 LLM 会把「昨天」
解析成训练数据里的日期（2026-07-15），导致所有相对时间查询全错。
"""

from __future__ import annotations

from datetime import datetime

from agent_service.tools import patrol

# 工具名 → 实际执行函数在 orchestrator 里映射
TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_daily_report",
            "description": "获取某一天的巡逻日报（任务完成率、覆盖率、告警数、重点异常）",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "日期 YYYY-MM-DD。不传=今天。'昨天'必须换算成具体日期",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_alerts",
            "description": "列出告警记录，常用于「现在有哪些没处理的告警」",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["pending", "confirmed", "false_positive"],
                        "description": "pending=待处置，confirmed=已确认，false_positive=误报",
                    },
                    "alert_type": {
                        "type": "string",
                        "description": "告警类型，只能是「人员闯入」或「通道占用」",
                    },
                    "point_id": {"type": "string", "description": "点位 ID，如 P1 / P2"},
                    "limit": {"type": "integer", "description": "最多返回几条，默认 5"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_history",
            "description": "按时间范围查询历史告警，用于「上周有没有X告警」这类问题",
            "parameters": {
                "type": "object",
                "properties": {
                    "days_back": {
                        "type": "integer",
                        "description": "往回查几天（含今天）。「上周」=7，「最近三天」=3",
                    },
                    "alert_type": {
                        "type": "string",
                        "description": "告警类型，只能是「人员闯入」或「通道占用」",
                    },
                    "point_id": {"type": "string", "description": "点位 ID，如 P1 / P2"},
                    "limit": {"type": "integer", "description": "最多返回几条，默认 5"},
                },
                "required": ["days_back"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tasks",
            "description": "列出巡逻任务及其状态",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["pending", "running", "completed", "cancelled", "failed"],
                    },
                    "limit": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "prepare_patrol_task",
            "description": (
                "准备下发一次巡逻任务。**只生成确认卡，不会真的下发**。"
                "用户点确认后才执行。任何「让机器人去巡逻」的请求都走这个工具。"
                "注意：本版本**只支持立即执行**，不支持定时或重复巡逻。"
                "如果用户要求「今晚10点」或「每天」，如实告知当前做不到，"
                "不要假装设置成功。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "route_id": {
                        "type": "string",
                        "description": "路线 ID。用户没指明就不传，默认用标准路线",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_task",
            "description": "取消巡逻任务。不传 task_id 就取消当前正在跑的那个",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "任务号，如 T-20261014-001"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_robot_status",
            "description": (
                "查询机器人的运行状态：位置、电量、是否正在巡逻。"
                "**只返回文字状态，不会有照片**。"
                "用户问「机器人在哪」「还有多少电」「是不是在巡逻」时用这个。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "capture_now",
            "description": (
                "让机器人**立刻抓拍一张现场照片并发给用户**。"
                "用户说「拍张照」「拍一张看看」「现在什么情况」「抓拍一下」时用这个。"
                "这个工具会返回一张图片，不是文字状态。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "point_id": {
                        "type": "string",
                        "description": "点位 ID，如 P1 / P2。不传就用默认点位",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "handle_alert",
            "description": (
                "处置一条告警。用户说「第 2 条是误报」时用 index=2；"
                "说告警号时用 alert_id"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "指代上一条列表里的第几条（从 1 开始）",
                    },
                    "alert_id": {"type": "string", "description": "告警号，如 A-20261014-001"},
                    "action": {
                        "type": "string",
                        "enum": ["confirm", "false_positive"],
                        "description": "confirm=确认告警，false_positive=标记误报",
                    },
                    "note": {
                        "type": "string",
                        "description": (
                            "处置结论：怎么处理的、为什么。"
                            "用户说了原因就原样带上（如「施工人员已登记」），"
                            "没说就不传。"
                        ),
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_alerts",
            "description": (
                "把告警记录导出成 CSV 文件并发给用户。"
                "仅当用户明确要求 CSV、Excel 或告警明细表格时使用。"
                "这是告警历史，不包含无异常点位的巡逻结果。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days_back": {
                        "type": "integer",
                        "description": "往回导出几天（含今天）。用户说「这个月」就换算成天数",
                    },
                    "alert_type": {
                        "type": "string",
                        "description": "告警类型，只能是「人员闯入」或「通道占用」。不传=全部",
                    },
                    "point_id": {"type": "string", "description": "点位 ID，如 P1 / P2"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "confirmed", "false_positive"],
                        "description": "不传=全部状态",
                    },
                    "limit": {"type": "integer", "description": "最多导出多少条，默认 2000"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_patrol_report",
            "description": (
                "导出指定日期的完整巡逻结果 PDF，包含当天所有巡逻轮次、每个路线点位的算法判断和图像证据。"
                "异常点位使用带检测框的算法结果图，正常点位使用原始拍照图。"
                "用户说巡逻报告、巡逻报表、导出 PDF、查看每个点位结果或图片时使用；日期不明默认今天。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "报告日期 YYYY-MM-DD。不传=今天",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "advance_patrol",
            "description": "时间快进：跳过当前点位剩余的就位等待，立即执行本点位拍照",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_help",
            "description": "展示能力清单。用户问「你能做什么」「帮助」时使用",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

TOOL_NAMES = {t["function"]["name"] for t in TOOLS}

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def system_prompt() -> str:
    now = datetime.now()
    weekday = WEEKDAY_CN[now.weekday()]
    ctx = patrol.business_context()

    return f"""你是「巡逻 Agent」，一个驻留在飞书里的安防巡逻业务助手。

{ctx}

## 硬性规则

1. **时间**：所有相对时间必须基于上面给出的当前日期换算。
   今天={now.strftime('%Y-%m-%d')}，昨天={_offset(-1)}，上周范围按最近 7 天算。
   **绝不允许凭记忆编造日期。**

2. **下发任务**：任何「让机器人去巡逻」的请求都必须调用 `prepare_patrol_task`，
   它会生成确认卡让用户核对。**你没有任何直接下发任务的工具**，不要试图绕过。

3. **数据来源**：所有数字、告警、任务状态必须来自工具调用结果。
   **绝不编造**。工具返回空就如实说「没有」。

4. **能力边界**：当前只支持两类检测——「人员闯入」（person）与「通道占用」（chair）。
   如果用户问烟火、翻墙、漏水、聚集等，**如实说明本版本不具备该能力**，不要敷衍或编造。

   同样地，**本版本的巡逻任务只支持「立即执行」**，
   不支持定时（今晚 10 点）和重复（每天）。用户提这类要求时如实说明，
   **绝不能假装已经设置成功** —— 确认卡上写着 22:00、机器人却立刻出发，
   这种欺骗比功能缺失严重得多。

5. **指代**：用户说「第 2 条」指的是上一条列表里的第 2 条。
   调用 `handle_alert` 时传 index=2，系统会自己解析成具体告警号。

6. **简洁**：回复不超过一屏。结论在前，细节在后。不要在文字里重复卡片上的表格内容。

7. **禁止伪造卡片**：你没有能力自己渲染卡片。
   需要展示报表、列表、任务确认、能力清单时，**唯一正确做法是调用对应工具**。
   绝不要在回复里输出「[卡片]」「📊 xxx」这类伪格式 ——
   那只会变成一条没有数据的纯文字消息，用户什么都看不到。

8. **你的文字回复只用来「说话」，不用来「呈现数据」**。
   报表、列表、任务确认、能力清单、详情都必须通过调用工具产出。
   如果你只用文字回复，用户那边就是一条干巴巴的消息，什么数据都看不到。
   所以：先想「哪个工具能给这个数据」，再决定说什么。

9. **报告格式**：用户要求巡逻报告、PDF 或逐点位结果时调用 `export_patrol_report`；
    只有用户明确要求 CSV/Excel 告警清单时才调用 `export_alerts`。

## 风格
口语化、直接。中文。不要用「您好，我是...」这类客套开场。
"""


def _offset(days: int) -> str:
    from datetime import timedelta

    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
