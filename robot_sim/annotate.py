# -*- coding: utf-8 -*-
"""检测结果图渲染：在抓拍帧上画出算法的包围框与标签。

为什么用 PIL 而不是 cv2：
    cv2.putText 只能用 Hershey 字体，**渲染不了中文**。
    而点位名（「东侧通道」）和标题栏都必须是中文才像真实安防平台的结果图。
    PIL + 微软雅黑可以正常渲染。

证据链设计：
    每次拍照会落**两张**图
        <base>_raw.jpg   原始帧，未经任何绘制 —— 这是不可篡改的证据本体
        <base>.jpg       标注帧，带框带标签 —— 这是推送给用户、报表里展示的那张
    库里 detection.image_path 指向标注帧，raw_image_path 指向原始帧。
"""

from __future__ import annotations

import logging
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from robot_sim.detectors.base import Detection

log = logging.getLogger("robot_sim.annotate")

# 颜色（RGB）
C_TARGET = (255, 64, 64)        # 本点位关心的目标：红
C_OTHER = (150, 158, 168)       # 其它检出：灰
C_BAR_BG = (16, 22, 32)         # 标题栏底色
C_TEXT = (255, 255, 255)
C_MUTED = (176, 186, 200)
C_WARN = (255, 190, 80)

FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",    # 黑体
    r"C:\Windows\Fonts\simsun.ttc",    # 宋体
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


@lru_cache(maxsize=16)
def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    log.warning("找不到中文字体，结果图上的中文会显示为方块")
    return ImageFont.load_default()


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return right - left, bottom - top


def annotate(
    frame_bgr: np.ndarray,
    detections: list[Detection],
    *,
    point_id: str = "",
    point_name: str = "",
    algorithm_name: str = "",
    target_label: str = "",
    target_threshold: float = 0.0,
    model_desc: str = "",
    is_mock: bool = False,
    robot_name: str = "",
    timestamp: datetime | None = None,
) -> np.ndarray:
    """在帧上绘制检测框，返回新的 BGR 图像（不修改入参）。

    调用方应只传入**本点位绑定算法**的检测结果（见 robot_sim/robot.py）。
    这样图上的框与「该点位绑定了什么算法」始终一致。
    """
    ts = timestamp or datetime.now()
    h, w = frame_bgr.shape[:2]

    base = Image.fromarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)).convert("RGBA")

    # ---- 半透明底栏（顶栏标题 + 底栏来源）----
    scale = w / 1280.0
    top_h = max(38, int(46 * scale))
    bot_h = max(30, int(36 * scale))
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rectangle([0, 0, w, top_h], fill=(*C_BAR_BG, 200))
    od.rectangle([0, h - bot_h, w, h], fill=(*C_BAR_BG, 190))
    base = Image.alpha_composite(base, overlay)

    draw = ImageDraw.Draw(base)
    f_top = _font(max(15, int(20 * scale)))
    f_small = _font(max(12, int(15 * scale)))
    f_box = _font(max(13, int(16 * scale)))

    # ---- 顶栏：机器人 · 点位 · 时间 ----
    title = f"{robot_name or 'R-01'} · {point_name}（{point_id}）"
    draw.text((int(12 * scale), int(9 * scale)), title, font=f_top, fill=C_TEXT)
    stamp = ts.strftime("%Y-%m-%d %H:%M:%S")
    tw, _ = _text_size(draw, stamp, f_small)
    draw.text((w - tw - int(12 * scale), int(14 * scale)), stamp, font=f_small, fill=C_MUTED)

    # ---- 检测框 ----
    thickness = max(2, int(3 * scale))
    pad = max(4, int(6 * scale))
    for d in detections:
        x1, y1, x2, y2 = (int(round(v)) for v in d.bbox)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w - 1, x2), min(h - 1, y2)
        if x2 <= x1 or y2 <= y1:
            continue

        is_target = bool(target_label) and d.label == target_label
        color = C_TARGET if is_target else C_OTHER
        draw.rectangle([x1, y1, x2, y2], outline=color, width=thickness)

        label = f"{d.label} {d.confidence:.2f}"
        if is_target:
            label = f"★ {label}"
        lw, lh = _text_size(draw, label, f_box)
        chip_w, chip_h = lw + pad * 2, lh + pad * 2

        # 标签优先放框上方；上方没空间就放框内顶部
        cy = y1 - chip_h
        if cy < top_h:
            cy = min(y1 + thickness, h - bot_h - chip_h)
        cx = min(x1, w - chip_w - 1)
        cx = max(0, cx)

        chip = Image.new("RGBA", (chip_w, chip_h), (0, 0, 0, 0))
        ImageDraw.Draw(chip).rectangle(
            [0, 0, chip_w - 1, chip_h - 1], fill=(*color, 235)
        )
        base.alpha_composite(chip, (cx, cy))
        draw.text((cx + pad, cy + pad), label, font=f_box, fill=(20, 24, 30))

    # ---- 底栏：模型来源与绑定说明 ----
    left = f"{model_desc or 'YOLO'}"
    if target_label:
        alg = f"{algorithm_name} " if algorithm_name else ""
        left += f" · 本点位绑定 {alg}{target_label}（阈值 {target_threshold}）"
    draw.text((int(12 * scale), h - bot_h + int(8 * scale)), left, font=f_small, fill=C_MUTED)

    if is_mock:
        warn = "⚠ 模拟检测，非真实推理"
        ww, _ = _text_size(draw, warn, f_small)
        draw.text((w - ww - int(12 * scale), h - bot_h + int(8 * scale)),
                  warn, font=f_small, fill=C_WARN)

    return cv2.cvtColor(np.array(base.convert("RGB")), cv2.COLOR_RGB2BGR)
