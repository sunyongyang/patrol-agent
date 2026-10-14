# -*- coding: utf-8 -*-
"""场景映射：COCO 类别 + 点位标签 → 安防告警（Spec §5.5）。

职责边界：
    detector  → 输出全类别原始检测
    mapper    → 只挑出「本点位关心的那个类别」升级为告警
    detection 表存全部原始输出，alert 表存映射后语义

为什么这样分层：书一直在桌上、人也一直在画面里时，两个点位的告警不会互相污染。
"""

from __future__ import annotations

from dataclasses import dataclass

from robot_sim.detectors.base import Detection
from shared.scene import AlertType, Point, alert_type


@dataclass(frozen=True)
class AlertDecision:
    detection: Detection
    spec: AlertType
    count: int = 1          # 本次拍照里命中该目标的个数

    @property
    def alert_type(self) -> str:
        return self.spec.type

    @property
    def level(self) -> str:
        return self.spec.level

    @property
    def level_code(self) -> str:
        return self.spec.level_code


def point_in_zone(center: tuple[float, float], zone: list[list[float]] | None) -> bool:
    """射线法判断点是否在多边形内。zone 为 None 时视为不启用区域判定。"""
    if not zone or len(zone) < 3:
        return False
    x, y = center
    inside = False
    n = len(zone)
    j = n - 1
    for i in range(n):
        xi, yi = zone[i][0], zone[i][1]
        xj, yj = zone[j][0], zone[j][1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def evaluate(point: Point, detections: list[Detection]) -> list[AlertDecision]:
    """从一次拍照的全部检测里，挑出应升级为告警的项。

    规则（config/mapping.yaml 可改，代码不动）：
        1. 类别 == 本点位 target_label
        2. 置信度 >= 本点位 conf_threshold
        3. 若点位配了 zone，则检测框中心必须落在区域内（用于「越界闯入」）

    关于「命中多个」：
        这里只产出**一条**告警（避免一次巡逻刷出 N 张卡片把 IM 淹没），
        但会把命中个数记在 `count` 上 —— 画面里 3 个人闯入，
        卡片要显示「检测到 3 人」，而不是只提 1 个。
        早期版本直接 `out[:1]` 丢掉其余，对安防来说是**漏报**。
    """
    spec = alert_type(point.target_label)
    if spec is None:
        # 点位配了未定义映射的类别——配置错误，静默不告警但要有迹可循
        return []

    matched: list[Detection] = []
    for det in detections:
        if det.label != point.target_label:
            continue
        if det.confidence < point.conf_threshold:
            continue
        if point.zone and not point_in_zone(det.center, point.zone):
            continue
        matched.append(det)

    if not matched:
        return []

    matched.sort(key=lambda d: d.confidence, reverse=True)
    return [AlertDecision(detection=matched[0], spec=spec, count=len(matched))]


def explain(point: Point, detections: list[Detection]) -> str:
    """给日志/调试用的可读说明。"""
    if not detections:
        return f"{point.id} {point.name}：未检出任何目标"
    labels = ", ".join(f"{d.label}={d.confidence:.2f}" for d in detections[:6])
    decisions = evaluate(point, detections)
    verdict = (
        f"→ 告警 {decisions[0].alert_type}" if decisions
        else f"→ 无告警（本点位关心 {point.target_label}，阈值 {point.conf_threshold}）"
    )
    return f"{point.id} {point.name}：检出 {labels} {verdict}"
