# -*- coding: utf-8 -*-
"""CSV 导出（Spec Q2）。

两个容易踩的坑，这里都处理了：

1. **编码必须是 utf-8-sig（带 BOM）**
   不加 BOM 的话，Excel 会按本地代码页（GBK）解析，中文全变乱码。
   这是中文 CSV 最常见的投诉来源。

2. **CSV 里不写查询条件**
   条件写进表格会破坏数据结构（Excel 排序/透视都受影响）。
   条件放文件名和消息说明里。

证据链字段（结果图 / 原始帧路径）也一并导出 —— 导出的表格要能独立支撑复盘。
"""

from __future__ import annotations

import csv
import logging
import re
from datetime import datetime
from pathlib import Path

from agent_service.report.builder import AlertItem
from shared import EXPORT_DIR

log = logging.getLogger("agent_service.csv_export")

COLUMNS = [
    ("alert_id", "告警号"),
    ("alert_type", "告警类型"),
    ("level", "等级"),
    ("status_label", "处置状态"),
    ("point_name", "点位"),
    ("point_id", "点位ID"),
    ("coco_label", "算法类别"),
    ("confidence", "置信度"),
    ("data_source", "数据来源"),
    ("created_at", "检测时间"),
    ("handled_by", "处理人"),
    ("handled_at", "处理时间"),
    ("run_id", "巡逻轮次"),
    ("image_path", "结果图(带框)"),
    ("raw_image_path", "原始帧"),
]


def _safe_name(text: str) -> str:
    """把条件描述转成文件名安全片段。"""
    cleaned = re.sub(r"[\\/:*?\"<>|\s]+", "_", text.strip())
    return cleaned.strip("_")[:60] or "all"


def build_csv(
    items: list[AlertItem],
    condition: str = "",
    extra_note: str = "",
) -> Path:
    """生成 CSV，返回文件路径。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"patrol_alerts_{stamp}"
    if condition:
        name += f"_{_safe_name(condition)}"
    path = EXPORT_DIR / f"{name}.csv"

    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([label for _, label in COLUMNS])
        for a in items:
            row = []
            for key, _ in COLUMNS:
                if key == "data_source":
                    row.append("模拟数据" if a.is_mock else "YOLO 真实推理")
                elif key == "status_label":
                    row.append(a.status_label)
                elif key == "confidence":
                    row.append(f"{a.confidence:.4f}")
                else:
                    row.append(getattr(a, key, "") or "")
            writer.writerow(row)

    log.info("导出 CSV：%s（%d 条）", path.name, len(items))
    return path


def describe(items: list[AlertItem]) -> str:
    """给消息正文用的一句话说明。"""
    if not items:
        return "0 条记录。"
    real = sum(1 for a in items if not a.is_mock)
    mock = len(items) - real
    bits = [f"共 {len(items)} 条"]
    if real:
        bits.append(f"真实推理 {real} 条")
    if mock:
        bits.append(f"模拟数据 {mock} 条")
    return "，".join(bits) + "。"
