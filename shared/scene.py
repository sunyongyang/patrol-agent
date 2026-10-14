# -*- coding: utf-8 -*-
"""业务语义：点位、路线、目标类别 → 告警映射。

对应 Spec §5.2 / §5.5。改 config/*.yaml 即可改业务，不动代码。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import yaml

from shared import CONFIG_DIR


# ------------------------------------------------------------------ 数据模型


@dataclass(frozen=True)
class Point:
    id: str
    name: str
    area: str
    target_label: str
    conf_threshold: float
    zone: list[list[float]] | None = None

    @property
    def full_name(self) -> str:
        return f"{self.name}（{self.area}）"


@dataclass(frozen=True)
class RoutePoint:
    point_id: str
    dwell_sec: int


@dataclass(frozen=True)
class Route:
    id: str
    name: str
    description: str
    points: tuple[RoutePoint, ...]


@dataclass(frozen=True)
class AlertType:
    label: str          # COCO 类别
    type: str           # 安防语义，如「人员闯入」
    level: str          # 中文等级，如「紧急」
    level_code: str     # 机器码，如 urgent
    push: bool
    emoji: str
    note: str = ""


@dataclass(frozen=True)
class RobotProfile:
    id: str
    name: str
    model: str
    initial_battery: float = 90.0


@dataclass(frozen=True)
class CameraProfile:
    index: int = 0
    width: int = 1280
    height: int = 720
    warmup_frames: int = 5


@dataclass(frozen=True)
class DetectorProfile:
    conf_floor: float = 0.35
    model: str = "yolov8n.pt"
    allow_mock_fallback: bool = True


# ------------------------------------------------------------------ 载入


def _read(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"找不到配置文件 {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@lru_cache(maxsize=1)
def _routes_doc() -> dict[str, Any]:
    return _read("routes.yaml")


@lru_cache(maxsize=1)
def _mapping_doc() -> dict[str, Any]:
    return _read("mapping.yaml")


def robot() -> RobotProfile:
    r = _routes_doc().get("robot") or {}
    return RobotProfile(
        id=r.get("id", "R-01"),
        name=r.get("name", "巡逻机器人"),
        model=r.get("model", "PATROL-DEMO-01"),
        initial_battery=float(r.get("initial_battery", 90.0)),
    )


def camera() -> CameraProfile:
    c = _routes_doc().get("camera") or {}
    return CameraProfile(
        index=int(c.get("index", 0)),
        width=int(c.get("width", 1280)),
        height=int(c.get("height", 720)),
        warmup_frames=int(c.get("warmup_frames", 5)),
    )


def detector() -> DetectorProfile:
    d = _mapping_doc().get("detector") or {}
    return DetectorProfile(
        conf_floor=float(d.get("conf_floor", 0.35)),
        model=d.get("model", "yolov8n.pt"),
        allow_mock_fallback=bool(d.get("allow_mock_fallback", True)),
    )


def points() -> dict[str, Point]:
    out: dict[str, Point] = {}
    for p in _routes_doc().get("points") or []:
        pt = Point(
            id=p["id"],
            name=p.get("name", p["id"]),
            area=p.get("area", ""),
            target_label=p.get("target_label", ""),
            conf_threshold=float(p.get("conf_threshold", 0.5)),
            zone=p.get("zone"),
        )
        out[pt.id] = pt
    return out


def point(point_id: str) -> Point:
    pts = points()
    if point_id not in pts:
        raise KeyError(f"未定义的点位: {point_id}（已定义: {list(pts)}）")
    return pts[point_id]


def routes() -> dict[str, Route]:
    out: dict[str, Route] = {}
    for r in _routes_doc().get("routes") or []:
        rps = tuple(
            RoutePoint(
                point_id=rp["point_id"],
                dwell_sec=int(rp.get("dwell_sec", 20)),
            )
            for rp in (r.get("points") or [])
        )
        out[r["id"]] = Route(
            id=r["id"],
            name=r.get("name", r["id"]),
            description=r.get("description", ""),
            points=rps,
        )
    return out


def route(route_id: str) -> Route:
    rs = routes()
    if route_id not in rs:
        raise KeyError(f"未定义的路线: {route_id}（已定义: {list(rs)}）")
    return rs[route_id]


def alert_types() -> dict[str, AlertType]:
    out: dict[str, AlertType] = {}
    for label, a in (_mapping_doc().get("alert_types") or {}).items():
        out[label] = AlertType(
            label=label,
            type=a.get("type", label),
            level=a.get("level", "一般"),
            level_code=a.get("level_code", "normal"),
            push=bool(a.get("push", True)),
            emoji=a.get("emoji", "⚠️"),
            note=a.get("note", ""),
        )
    return out


def alert_type(label: str) -> AlertType | None:
    return alert_types().get(label)


def describe() -> str:
    """给 LLM 看的业务上下文，避免它瞎猜点位和告警类型。"""
    lines = [f"机器人：{robot().name}（{robot().id}）", "", "点位："]
    for p in points().values():
        lines.append(f"  - {p.id} {p.full_name}｜检测目标={p.target_label}｜阈值={p.conf_threshold}")
    lines.append("")
    lines.append("路线：")
    for r in routes().values():
        seq = " → ".join(rp.point_id for rp in r.points)
        lines.append(f"  - {r.id} {r.name}（{seq}）")
    lines.append("")
    lines.append("告警类型：")
    for a in alert_types().values():
        lines.append(f"  - {a.label} → {a.type}（{a.level}）")
    return "\n".join(lines)
