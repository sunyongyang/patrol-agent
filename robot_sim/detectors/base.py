# -*- coding: utf-8 -*-
"""检测器抽象。

关键设计（Spec §5.3 / §8）：
    检测器**输出全类别**，不做点位过滤。
    点位过滤发生在 mapper 层——这样 detection 表里保留完整原始证据，
    客户追问「这个点位当时到底看到了什么」时能答得出来。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np


@dataclass(frozen=True)
class Detection:
    label: str
    confidence: float
    bbox: tuple[float, float, float, float]  # x1, y1, x2, y2（像素）

    @property
    def width(self) -> float:
        return max(0.0, self.bbox[2] - self.bbox[0])

    @property
    def height(self) -> float:
        return max(0.0, self.bbox[3] - self.bbox[1])

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center(self) -> tuple[float, float]:
        return ((self.bbox[0] + self.bbox[2]) / 2, (self.bbox[1] + self.bbox[3]) / 2)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "confidence": round(self.confidence, 4),
            "bbox": [round(v, 1) for v in self.bbox],
        }


@runtime_checkable
class Detector(Protocol):
    name: str
    is_mock: bool
    model_desc: str

    def detect(self, image: np.ndarray) -> list[Detection]: ...
