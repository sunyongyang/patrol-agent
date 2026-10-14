# -*- coding: utf-8 -*-
"""按日生成包含逐点巡逻证据的 PDF 报告。"""

from __future__ import annotations

import logging
import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from html import escape
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from shared import EXPORT_DIR, IMAGE_DIR, repo
from shared.scene import Point, RoutePoint, points as all_points, routes as all_routes

log = logging.getLogger("agent_service.patrol_pdf")

STATUS_LABELS = {
    "abnormal": "异常",
    "normal": "正常",
    "not_visited": "未完成",
    "completed": "已完成",
    "running": "执行中",
    "failed": "失败",
    "cancelled": "已取消",
    "rejected": "已拒绝",
}


@dataclass
class PointReport:
    point: Point
    status: str
    detections: list[dict]
    image_path: Path | None
    image_label: str
    infer_ms: int | None
    is_mock: bool | None
    alert_type: str = ""
    alert_level: str = ""
    alert_status: str = ""


@dataclass
class RunReport:
    run: dict
    route_name: str
    points: list[PointReport]


@lru_cache(maxsize=1)
def _font_name() -> str:
    candidates = (
        Path(r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\simsun.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    )
    for path in candidates:
        if not path.exists():
            continue
        try:
            name = "PatrolReportCJK"
            pdfmetrics.registerFont(TTFont(name, str(path), subfontIndex=0))
            return name
        except (OSError, ValueError):
            continue
    raise RuntimeError("找不到可用的中文字体，无法生成中文 PDF")


def _existing_path(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_file() else None


def _legacy_image_paths(run_id: str, point_id: str) -> tuple[Path | None, Path | None]:
    matches = sorted(IMAGE_DIR.glob(f"*_{point_id}_{run_id}_raw.jpg"))
    if not matches:
        return None, None
    raw_path = matches[-1]
    image_path = raw_path.with_name(raw_path.name.removesuffix("_raw.jpg") + ".jpg")
    return image_path if image_path.is_file() else None, raw_path


def _point_report(
    point: Point,
    run_id: str,
    capture: dict | None,
    evidence: list[dict],
) -> PointReport:
    point_evidence = [row for row in evidence if row.get("point_id") == point.id]
    alerts = [row for row in point_evidence if row.get("alert_id")]
    alert = alerts[-1] if alerts else {}

    if capture:
        status = capture.get("result_status", "normal")
        detections = capture.get("detections") or []
        annotated_path = _existing_path(capture.get("image_path"))
        raw_path = _existing_path(capture.get("raw_image_path"))
        infer_ms = capture.get("infer_ms")
        is_mock = bool(capture.get("is_mock"))
        alert_type = alert.get("alert_type", "")
        alert_level = alert.get("level", "")
        alert_status = alert.get("alert_status", "")
    else:
        detections = [
            {"label": row.get("coco_label", ""), "confidence": row.get("confidence", 0)}
            for row in point_evidence
            if row.get("coco_label") == point.target_label
        ]
        status = "abnormal" if alerts else "normal"
        annotated_path = next(
            (_existing_path(row.get("image_path")) for row in reversed(point_evidence)
             if row.get("image_path")),
            None,
        )
        raw_path = next(
            (_existing_path(row.get("raw_image_path")) for row in reversed(point_evidence)
             if row.get("raw_image_path")),
            None,
        )
        infer_ms = None
        is_mock = bool(point_evidence[-1].get("is_mock")) if point_evidence else None
        alert_type = alert.get("alert_type", "")
        alert_level = alert.get("level", "")
        alert_status = alert.get("alert_status", "")

    if annotated_path is None or raw_path is None:
        legacy_annotated, legacy_raw = _legacy_image_paths(run_id, point.id)
        annotated_path = annotated_path or legacy_annotated
        raw_path = raw_path or legacy_raw

    if capture is None and not point_evidence:
        status = "normal" if annotated_path or raw_path else "not_visited"

    if status == "abnormal":
        selected_path = annotated_path
        image_label = "异常算法结果图（标注帧）"
    elif status == "normal":
        selected_path = raw_path
        image_label = "正常现场图（原始帧）"
    else:
        selected_path = None
        image_label = "未完成点位，无抓拍图片"

    if selected_path is None and status in ("normal", "abnormal"):
        image_label = "异常标注图缺失" if status == "abnormal" else "正常原始图片缺失"

    return PointReport(
        point=point,
        status=status,
        detections=detections,
        image_path=selected_path,
        image_label=image_label,
        infer_ms=int(infer_ms) if infer_ms is not None else None,
        is_mock=is_mock,
        alert_type=alert_type,
        alert_level=alert_level,
        alert_status=alert_status,
    )


def _run_report(run: dict, point_map: dict[str, Point], route_map: dict) -> RunReport:
    run_id = run["id"]
    captures = {row["point_id"]: row for row in repo.list_patrol_captures(run_id)}
    evidence = repo.list_run_evidence(run_id)
    route = route_map.get(run.get("route_id"))
    route_points = route.points if route else tuple(
        RoutePoint(point_id=point_id, dwell_sec=0)
        for point_id in dict.fromkeys(
            [row["point_id"] for row in captures.values()]
            + [row["point_id"] for row in evidence]
        )
    )
    points = [
        _point_report(point_map[route_point.point_id], run_id,
                      captures.get(route_point.point_id), evidence)
        for route_point in route_points
        if route_point.point_id in point_map
    ]
    return RunReport(
        run=run,
        route_name=run.get("route_name") or (route.name if route else run.get("route_id", "未知路线")),
        points=points,
    )


def _paragraph(text: object, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(str(text)).replace("\n", "<br/>"), style)


def _fit_image(path: Path, max_width: float, max_height: float) -> Image:
    image = Image(str(path))
    scale = min(max_width / image.imageWidth, max_height / image.imageHeight, 1.0)
    image.drawWidth = image.imageWidth * scale
    image.drawHeight = image.imageHeight * scale
    return image


def _render_pdf(path: Path, report_label: str, runs: list[RunReport]) -> None:
    font = _font_name()
    styles = {
        "title": ParagraphStyle(
            "PatrolTitle", fontName=font, fontSize=21, leading=28,
            textColor=colors.HexColor("#17324D"), alignment=TA_CENTER, spaceAfter=5 * mm,
        ),
        "subtitle": ParagraphStyle(
            "PatrolSubtitle", fontName=font, fontSize=10, leading=15,
            textColor=colors.HexColor("#526579"), alignment=TA_CENTER, spaceAfter=5 * mm,
        ),
        "heading": ParagraphStyle(
            "RunHeading", fontName=font, fontSize=13, leading=18,
            textColor=colors.HexColor("#17324D"), spaceBefore=2 * mm, spaceAfter=2 * mm,
        ),
        "point": ParagraphStyle(
            "PointHeading", fontName=font, fontSize=14, leading=19,
            textColor=colors.HexColor("#17324D"), spaceAfter=2 * mm,
        ),
        "body": ParagraphStyle(
            "PatrolBody", fontName=font, fontSize=9, leading=14,
            textColor=colors.HexColor("#243648"), spaceAfter=1.5 * mm,
        ),
        "small": ParagraphStyle(
            "PatrolSmall", fontName=font, fontSize=8, leading=11,
            textColor=colors.HexColor("#526579"), spaceAfter=1 * mm,
        ),
        "center": ParagraphStyle(
            "PatrolCenter", fontName=font, fontSize=9, leading=13,
            textColor=colors.HexColor("#243648"), alignment=TA_CENTER,
        ),
    }
    page_width, _ = A4
    body_width = page_width - 32 * mm
    story: list = [
        _paragraph("巡逻结果报告", styles["title"]),
        _paragraph(f"报告范围：{report_label}", styles["subtitle"]),
    ]

    total_points = sum(len(run.points) for run in runs)
    abnormal_points = sum(p.status == "abnormal" for run in runs for p in run.points)
    normal_points = sum(p.status == "normal" for run in runs for p in run.points)
    unfinished_points = total_points - abnormal_points - normal_points
    missing_images = sum(
        p.status in ("normal", "abnormal") and p.image_path is None
        for run in runs for p in run.points
    )
    summary = Table(
        [[
            _paragraph(f"巡逻轮次　{len(runs)}", styles["center"]),
            _paragraph(f"点位总数　{total_points}", styles["center"]),
            _paragraph(f"异常　{abnormal_points}", styles["center"]),
            _paragraph(f"正常　{normal_points}", styles["center"]),
            _paragraph(f"未完成　{unfinished_points}", styles["center"]),
        ]],
        colWidths=[body_width / 5] * 5,
    )
    summary.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#EAF0F5")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#C8D3DD")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#C8D3DD")),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.extend([summary, Spacer(1, 5 * mm)])
    if missing_images:
        story.append(_paragraph(
            f"图片缺失：{missing_images} 个已判定点位的对应图片文件不存在。",
            styles["small"],
        ))

    status_colors = {
        "abnormal": colors.HexColor("#FCE8E6"),
        "normal": colors.HexColor("#E7F3EA"),
        "not_visited": colors.HexColor("#EEF1F4"),
    }
    for run_index, report in enumerate(runs):
        run = report.run
        if run_index:
            story.append(PageBreak())
        story.append(_paragraph(
            f"巡逻轮次：{run.get('id', '')}　路线：{report.route_name}", styles["heading"]
        ))
        run_status = STATUS_LABELS.get(run.get("status", ""), run.get("status", "未知"))
        progress = f"{run.get('points_done', 0)}/{run.get('points_total', 0)} 点位"
        story.append(_paragraph(
            f"任务：{run.get('task_id') or '—'}　状态：{run_status}　进度：{progress}　"
            f"开始时间：{run.get('started_at') or '—'}",
            styles["small"],
        ))
        story.append(Spacer(1, 2 * mm))

        for point_index, result in enumerate(report.points):
            if point_index:
                story.append(PageBreak())
                story.append(_paragraph(
                    f"{report.route_name} · {run.get('id', '')}", styles["small"]
                ))
            point = result.point
            status_label = STATUS_LABELS.get(result.status, result.status)
            story.append(_paragraph(
                f"{point.id}　{point.name}　|　{status_label}", styles["point"]
            ))
            rows = [
                ["绑定算法", f"{point.algorithm.name}（{point.target_label}）"],
                ["判断规则", f"置信度 ≥ {point.conf_threshold:.2f}"],
                ["检测结果", ", ".join(
                    f"{item.get('label', point.target_label)} {float(item.get('confidence', 0)):.2f}"
                    for item in result.detections
                ) or "未检出绑定类别"],
            ]
            if result.alert_type:
                alert_status = STATUS_LABELS.get(result.alert_status, result.alert_status)
                rows.append(["异常告警", f"{result.alert_type}（{result.alert_level}，{alert_status}）"])
            if result.infer_ms is not None:
                rows.append(["推理耗时", f"{result.infer_ms} ms"])
            if result.is_mock is not None:
                rows.append(["数据来源", "模拟检测" if result.is_mock else "YOLO 真实推理"])
            if point.algorithm.note:
                rows.append(["算法说明", point.algorithm.note])
            info = Table(
                [[_paragraph(label, styles["small"]), _paragraph(value, styles["body"])]
                 for label, value in rows],
                colWidths=[28 * mm, body_width - 28 * mm],
                hAlign="LEFT",
            )
            info.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F3F6F8")),
                ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#D4DDE5")),
                ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#D4DDE5")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]))
            info.setStyle(TableStyle([
                ("BACKGROUND", (1, 0), (1, -1), status_colors.get(result.status, colors.white)),
            ]))

            block = [info, Spacer(1, 3 * mm)]
            if result.image_path:
                block.extend([
                    _paragraph(result.image_label, styles["small"]),
                    _fit_image(result.image_path, body_width, 130 * mm),
                ])
            else:
                block.append(_paragraph(result.image_label, styles["small"]))
            story.append(KeepTogether(block))

    def draw_footer(canvas, document) -> None:
        canvas.saveState()
        canvas.setFont(font, 8)
        canvas.setFillColor(colors.HexColor("#6C7C8C"))
        canvas.drawString(16 * mm, 10 * mm, f"巡逻 Agent · {report_label}")
        canvas.drawRightString(A4[0] - 16 * mm, 10 * mm, f"第 {document.page} 页")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        str(path), pagesize=A4,
        rightMargin=16 * mm, leftMargin=16 * mm,
        topMargin=15 * mm, bottomMargin=18 * mm,
        title=f"巡逻结果报告 {report_label}", author="巡逻 Agent",
        subject="逐轮次、逐点位巡逻结果与图像证据",
    )
    doc.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)


def latest_report_runs(
    limit: int = 10, created_by: str | None = None
) -> list[dict]:
    return repo.latest_patrol_runs(limit, created_by=created_by)


def available_report_periods(scope: str, limit: int = 6) -> list[dict[str, str]]:
    """Return recent periods that actually contain patrol runs."""
    runs = repo.latest_patrol_runs(500)
    periods: dict[str, dict[str, str]] = {}
    today = date.today()

    for run in runs:
        run_day = datetime.fromisoformat(run["started_at"]).date()
        if scope == "day":
            start = end = run_day
            key = run_day.isoformat()
            label = "今天" if run_day == today else run_day.strftime("%Y-%m-%d")
        elif scope == "week":
            start = run_day - timedelta(days=run_day.weekday())
            end = start + timedelta(days=6)
            key = start.isoformat()
            if start <= today <= end:
                prefix = "本周"
            elif start == (today - timedelta(days=today.weekday() + 7)):
                prefix = "上周"
            else:
                prefix = ""
            label = f"{prefix} {start:%m-%d} 至 {end:%m-%d}".strip()
        elif scope == "month":
            start = run_day.replace(day=1)
            end = run_day.replace(day=monthrange(run_day.year, run_day.month)[1])
            key = start.strftime("%Y-%m")
            label = start.strftime("%Y年%m月")
        else:
            raise ValueError(f"不支持的报表范围：{scope}")

        periods.setdefault(key, {
            "label": label,
            "start_day": start.isoformat(),
            "end_day": end.isoformat(),
        })

    return sorted(periods.values(), key=lambda item: item["start_day"], reverse=True)[:limit]


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_-]+", "_", value).strip("_")
    return cleaned[:60] or "patrol"


def _period_bounds(scope: str, anchor_day: str) -> tuple[str, str, str]:
    anchor = datetime.strptime(anchor_day, "%Y-%m-%d").date()
    if scope == "day":
        start = end = anchor
        label = anchor.strftime("%Y-%m-%d")
    elif scope == "week":
        start = anchor - timedelta(days=anchor.weekday())
        end = start + timedelta(days=6)
        label = f"{start:%Y-%m-%d} 至 {end:%Y-%m-%d}"
    elif scope == "month":
        start = anchor.replace(day=1)
        end = anchor.replace(day=monthrange(anchor.year, anchor.month)[1])
        label = start.strftime("%Y年%m月")
    else:
        raise ValueError(f"不支持的报表范围：{scope}")
    return start.isoformat(), end.isoformat(), label


def build_patrol_pdf(
    day: str | None = None,
    *,
    scope: str = "day",
    task_id: str | None = None,
    run_id: str | None = None,
    start_day: str | None = None,
    end_day: str | None = None,
    period_label: str | None = None,
    created_by: str | None = None,
) -> tuple[Path | None, dict]:
    """Generate a PDF for one run/task or a date range."""
    if task_id and task_id.startswith("RUN-") and not run_id:
        run_id, task_id = task_id, None

    if run_id or task_id:
        if not created_by:
            raise ValueError("导出单次任务报告需要发起人身份")
        if run_id:
            run = repo.get_patrol_run_detail(run_id, created_by=created_by)
        else:
            task_run = repo.run_for_task(task_id or "", created_by=created_by)
            run = repo.get_patrol_run_detail(task_run["id"], created_by=created_by) if task_run else None
        runs = [run] if run else []
        report_label = f"单次任务 {task_id or run_id}"
        file_scope = task_id or run_id or "run"
    else:
        if start_day or end_day:
            if not start_day or not end_day:
                raise ValueError("开始日期和结束日期必须同时提供")
            if date.fromisoformat(end_day) < date.fromisoformat(start_day):
                raise ValueError("结束日期不能早于开始日期")
            start, end, report_label = start_day, end_day, period_label or f"{start_day} 至 {end_day}"
        else:
            anchor = day or datetime.now().strftime("%Y-%m-%d")
            start, end, report_label = _period_bounds(scope, anchor)
        since = repo.day_bounds(start)[0]
        until = repo.day_bounds(end)[1]
        runs = repo.list_patrol_runs(since, until)
        file_scope = f"{start}_{end}"

    if not runs:
        return None, {
            "day": report_label, "runs": 0, "points": 0, "abnormal": 0,
            "normal": 0, "unfinished": 0, "image_missing": 0, "has_mock": False,
        }

    point_map = all_points()
    reports = [
        _run_report(run, point_map, all_routes())
        for run in runs
    ]
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = EXPORT_DIR / f"patrol_report_{_safe_filename(file_scope)}_{stamp}.pdf"
    _render_pdf(path, report_label, reports)

    point_results = [point for run in reports for point in run.points]
    summary = {
        "day": report_label,
        "runs": len(reports),
        "points": len(point_results),
        "abnormal": sum(point.status == "abnormal" for point in point_results),
        "normal": sum(point.status == "normal" for point in point_results),
        "unfinished": sum(point.status == "not_visited" for point in point_results),
        "image_missing": sum(
            point.status in ("normal", "abnormal") and point.image_path is None
            for point in point_results
        ),
        "has_mock": any(point.is_mock for point in point_results),
    }
    log.info("生成巡逻 PDF：%s（%d 轮，%d 点位）", path.name, summary["runs"], summary["points"])
    return path, summary