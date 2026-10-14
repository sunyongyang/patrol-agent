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
class Algorithm:
    """一个算法 = 一类检测能力。

    点位**绑定**一个算法，然后只上报这个算法的检测结果。
    这是「点位 → 算法 → 告警语义」的绑定关系的核心载体。
    """

    id: str             # 配置键，如 person_intrusion
    name: str           # 中文名，如「人员闯入检测」
    coco_label: str     # 对应的模型类别，如 person
    alert_type: str     # 映射出的告警语义，如「人员闯入」
    level: str          # 中文等级，如「紧急」
    level_code: str     # 机器码，如 urgent
    push: bool
    emoji: str
    note: str = ""


@dataclass(frozen=True)
class Point:
    """巡逻点位。

    `algorithm` 就是那个绑定关系 —— 该点位只上报它的结果。
    `target_label` 是从绑定算法派生出来的模型类别，不单独配置，
    避免出现「点位写了 person、算法却是通道占用」这种自相矛盾的配置。
    """

    id: str
    name: str
    area: str
    algorithm: Algorithm
    conf_threshold: float
    zone: list[list[float]] | None = None

    @property
    def algorithm_id(self) -> str:
        return self.algorithm.id

    @property
    def target_label(self) -> str:
        """本点位关心的模型类别（派生自绑定算法）。"""
        return self.algorithm.coco_label

    @property
    def full_name(self) -> str:
        return f"{self.name}（{self.area}）"

    @property
    def binding_label(self) -> str:
        """给用户看的绑定说明，如「通道占用检测（chair）」。"""
        return f"{self.algorithm.name}（{self.algorithm.coco_label}）"


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
    # —— 绑定关系的作用范围 ——
    show_other_detections: bool = False   # 结果图是否画非绑定算法的框
    store_all_classes: bool = True        # 检测表是否保留非绑定算法的原始输出


@dataclass(frozen=True)
class AlertPolicy:
    """告警抑制策略（config/mapping.yaml alert_policy）。"""

    suppress_minutes: int = 30
    update_card_on_suppress: bool = True


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
        show_other_detections=bool(d.get("show_other_detections", False)),
        store_all_classes=bool(d.get("store_all_classes", True)),
    )


def alert_policy() -> AlertPolicy:
    a = _mapping_doc().get("alert_policy") or {}
    return AlertPolicy(
        suppress_minutes=int(a.get("suppress_minutes", 30)),
        update_card_on_suppress=bool(a.get("update_card_on_suppress", True)),
    )


def algorithms() -> dict[str, Algorithm]:
    """全部算法定义，键为算法 ID。"""
    out: dict[str, Algorithm] = {}
    for aid, a in (_mapping_doc().get("algorithms") or {}).items():
        out[aid] = Algorithm(
            id=aid,
            name=a.get("name", aid),
            coco_label=a.get("coco_label", aid),
            alert_type=a.get("alert_type", aid),
            level=a.get("level", "一般"),
            level_code=a.get("level_code", "normal"),
            push=bool(a.get("push", True)),
            emoji=a.get("emoji", "⚠️"),
            note=a.get("note", ""),
        )
    return out


def algorithm(algorithm_id: str) -> Algorithm:
    algs = algorithms()
    if algorithm_id not in algs:
        raise KeyError(
            f"未定义的算法: {algorithm_id}（已定义: {list(algs)}）"
        )
    return algs[algorithm_id]


def algorithm_for_label(coco_label: str) -> Algorithm | None:
    """按模型类别反查算法（用于把手上的类别还原成算法）。"""
    for a in algorithms().values():
        if a.coco_label == coco_label:
            return a
    return None


def points() -> dict[str, Point]:
    """点位表。每个点位的算法绑定在这里解析出来。

    绑定缺失或指向不存在的算法时**直接抛错**，不静默降级 ——
    一个没绑算法的点位等于不知道要检测什么，让它早点失败比默默跑强。
    """
    out: dict[str, Point] = {}
    for p in _routes_doc().get("points") or []:
        aid = p.get("algorithm")
        if not aid:
            raise KeyError(
                f"点位 {p.get('id')} 没有绑定算法。"
                "请在 config/routes.yaml 里加 algorithm 字段，"
                "取值来自 config/mapping.yaml 的 algorithms。"
            )
        pt = Point(
            id=p["id"],
            name=p.get("name", p["id"]),
            area=p.get("area", ""),
            algorithm=algorithm(aid),
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


def describe() -> str:
    """给 LLM 看的业务上下文，避免它瞎猜点位和算法绑定。"""
    lines = [f"机器人：{robot().name}（{robot().id}）", "", "点位与绑定的算法："]
    for p in points().values():
        lines.append(
            f"  - {p.id} {p.full_name}｜绑定算法={p.algorithm.name}"
            f"（{p.algorithm.coco_label}）→ 告警「{p.algorithm.alert_type}」"
            f"｜阈值={p.conf_threshold}"
        )
    lines.append("")
    lines.append("路线：")
    for r in routes().values():
        seq = " → ".join(rp.point_id for rp in r.points)
        lines.append(f"  - {r.id} {r.name}（{seq}）")
    lines.append("")
    lines.append("可用算法：")
    for a in algorithms().values():
        lines.append(f"  - {a.id}｜{a.name}｜模型类别 {a.coco_label} → 「{a.alert_type}」（{a.level}）")
    return "\n".join(lines)
