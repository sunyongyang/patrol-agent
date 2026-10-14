# -*- coding: utf-8 -*-
"""报表引擎：数据 → 报表模型（Spec §6.5）。

这一层只产出**结构化模型**，不碰任何 IM 细节。
渲染由 report/feishu_card.py 与 report/plain_text.py 各自负责 ——
这样飞书和微信能用同一份数据出不同形态，业务代码不感知通道差异。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from shared import repo
from shared.scene import algorithm_for_label, points as all_points


@dataclass
class AlertItem:
    alert_id: str
    alert_type: str
    level: str
    level_code: str
    status: str
    point_id: str
    point_name: str
    confidence: float
    coco_label: str
    image_path: str | None
    created_at: str
    is_mock: bool
    raw_image_path: str | None = None
    # 告警抑制相关：同一目标持续存在时累加，而不是新建
    occurrence_count: int = 1
    # 本次拍照里命中该目标的个数（3 个人闯入 → 3）
    target_count: int = 1
    last_seen_at: str | None = None
    resolution_note: str | None = None
    card_message_id: str = ""

    @property
    def duration_minutes(self) -> int:
        """从首次出现到现在持续了多少分钟。"""
        try:
            start = datetime.fromisoformat(self.created_at)
            end = datetime.fromisoformat(self.last_seen_at) if self.last_seen_at else datetime.now()
            return max(0, int((end - start).total_seconds() // 60))
        except (ValueError, TypeError):
            return 0

    @property
    def duration_label(self) -> str:
        mins = self.duration_minutes
        if mins < 1:
            return "不足 1 分钟"
        if mins < 60:
            return f"{mins} 分钟"
        return f"{mins // 60} 小时 {mins % 60} 分钟"

    @property
    def repeated(self) -> bool:
        return self.occurrence_count > 1

    @property
    def emoji(self) -> str:
        spec = algorithm_for_label(self.coco_label)
        return spec.emoji if spec else "⚠️"

    @property
    def status_label(self) -> str:
        return {
            "pending": "待处置",
            "confirmed": "已确认",
            "false_positive": "误报",
            # 历史重复告警被合并后保留的存档记录，不计入报表总数
            "merged": "已合并",
        }.get(self.status, self.status)

    @property
    def time_short(self) -> str:
        try:
            return datetime.fromisoformat(self.created_at).strftime("%m-%d %H:%M")
        except ValueError:
            return self.created_at

    @property
    def time_hm(self) -> str:
        try:
            return datetime.fromisoformat(self.created_at).strftime("%H:%M")
        except ValueError:
            return self.created_at


@dataclass
class DailyReport:
    day: str                      # YYYY-MM-DD
    label: str                    # 2026-10-13 周一
    stats: dict = field(default_factory=dict)
    alert_counts: dict = field(default_factory=dict)
    breakdown: list[dict] = field(default_factory=list)
    top_alerts: list[AlertItem] = field(default_factory=list)
    has_mock: bool = False

    @property
    def headline(self) -> str:
        pending = self.alert_counts.get("pending", 0)
        total = self.alert_counts.get("total", 0)
        if total == 0:
            return "无异常"
        return f"告警 {total} 条，待处置 {pending} 条"

    @property
    def coverage_note(self) -> str:
        """口径说明。数字要站得住，名字必须叫准。"""
        return (
            "口径：路径完成率 = 已完成点位 / 应巡点位。"
            "这**不是**「巡逻覆盖率」——后者按 KPI 定义还含时间维度，本版本未实现。"
        )


WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _to_alert_item(row: dict) -> AlertItem:
    pid = row.get("point_id") or ""
    pts = all_points()
    pname = pts[pid].name if pid in pts else pid
    return AlertItem(
        alert_id=row["id"],
        alert_type=row.get("alert_type", ""),
        level=row.get("level", ""),
        level_code=row.get("level_code", ""),
        status=row.get("status", "pending"),
        point_id=pid,
        point_name=pname,
        confidence=float(row.get("confidence") or 0.0),
        coco_label=row.get("coco_label") or "",
        image_path=row.get("image_path"),
        created_at=row.get("created_at", ""),
        is_mock=bool(row.get("is_mock")),
        raw_image_path=row.get("raw_image_path"),
        occurrence_count=int(row.get("occurrence_count") or 1),
        target_count=int(row.get("target_count") or 1),
        last_seen_at=row.get("last_seen_at"),
        resolution_note=row.get("resolution_note"),
        card_message_id=row.get("card_message_id") or "",
    )


def build_daily(day: str | None = None) -> DailyReport:
    since, until = repo.day_bounds(day)
    stats = repo.run_stats(since, until)
    counts = repo.alert_counts(since, until)
    breakdown = repo.alert_type_breakdown(since, until)
    rows = repo.list_alerts(since=since, until=until, limit=5)

    d = datetime.fromisoformat(since)
    label = f"{d.strftime('%Y-%m-%d')} {WEEKDAY_CN[d.weekday()]}"

    return DailyReport(
        day=d.strftime("%Y-%m-%d"),
        label=label,
        stats=stats,
        alert_counts=counts,
        breakdown=breakdown,
        top_alerts=[_to_alert_item(r) for r in rows],
        has_mock=stats.get("mock_detections", 0) > 0,
    )


def build_alert_list(
    status: str | None = None,
    alert_type: str | None = None,
    point_id: str | None = None,
    limit: int = 5,
) -> list[AlertItem]:
    rows = repo.list_alerts(status=status, alert_type=alert_type, point_id=point_id, limit=limit)
    return [_to_alert_item(r) for r in rows]


def query_history(
    days_back: int,
    alert_type: str | None = None,
    point_id: str | None = None,
    status: str | None = None,
    limit: int = 5,
    end_day: str | None = None,
) -> tuple[list[AlertItem], str]:
    """返回 (告警列表, 条件描述)。"""
    since, until = repo.range_bounds(days_back, end_day)
    rows = repo.list_alerts(
        since=since, until=until, alert_type=alert_type, point_id=point_id,
        status=status, limit=limit,
    )
    conds = [f"{since[:10]} ~ {until[:10]}"]
    if alert_type:
        conds.append(f"类型={alert_type}")
    if point_id:
        pts = all_points()
        conds.append(f"点位={pts[point_id].name if point_id in pts else point_id}")
    if status:
        conds.append({"pending": "待处置", "confirmed": "已确认",
                      "false_positive": "误报"}.get(status, status))
    return [_to_alert_item(r) for r in rows], "｜".join(conds)


def alerts_by_ids(ids: list[str]) -> list[AlertItem]:
    return [_to_alert_item(r) for r in repo.list_alerts_by_ids(ids)]


def count_alerts(
    days_back: int, alert_type: str | None = None, point_id: str | None = None
) -> int:
    since, until = repo.range_bounds(days_back)
    rows = repo.list_alerts(since=since, until=until, alert_type=alert_type,
                            point_id=point_id, limit=1000)
    return len(rows)


def robot_status_text() -> str:
    from shared.scene import robot as robot_profile
    import requests
    from shared import config

    rp = robot_profile()
    base = config.get("robot_sim.base_url", "http://127.0.0.1:8100")
    try:
        st = requests.get(f"{base}/robot/{rp.id}/status", timeout=8).json()
    except requests.RequestException:
        return f"⚠️ 联系不上机器人模拟器（{base}），无法获取 {rp.name} 的状态。"

    point = st.get("current_point_name") or "未知位置"
    status_map = {
        "idle": "待命",
        "patrolling": "巡逻中",
        "charging": "充电中",
        "offline": "离线",
    }
    lines = [
        f"🤖 {rp.name}（{rp.id}）",
        f"状态：{status_map.get(st.get('status'), st.get('status'))}",
        f"位置：{point}",
        f"电量：{st.get('battery')}%",
        f"检测器：{st.get('detector', {}).get('model')}",
    ]
    if st.get("current_task_id"):
        lines.append(f"当前任务：{st['current_task_id']}（剩余等待 {st.get('dwell_remaining')}s）")
    return "\n".join(lines)
