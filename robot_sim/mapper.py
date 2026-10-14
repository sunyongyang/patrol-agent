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
    """
    spec = alert_type(point.target_label)
    if spec is None:
        # 点位配了未定义映射的类别——配置错误，静默不告警但要有迹可循
        return []

    out: list[AlertDecision] = []
    for det in detections:
        if det.label != point.target_label:
            continue
        if det.confidence < point.conf_threshold:
            continue
        if point.zone and not point_in_zone(det.center, point.zone):
            continue
        out.append(AlertDecision(detection=det, spec=spec))

    # 同一次拍照同类目标可能检出多个：只保留置信度最高的一个作为告警，
    # 避免「一次巡逻刷出 5 条人员闯入」把 IM 淹没。
    out.sort(key=lambda d: d.detection.confidence, reverse=True)
    return out[:1]


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
