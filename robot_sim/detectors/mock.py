# -*- coding: utf-8 -*-
"""降级用模拟检测器。

仅在未安装 ultralytics 时启用。所有产出都带 is_mock=True，
落库后可与真实推理数据区分（Spec §5.3 明确要求不得混淆）。
"""

from __future__ import annotations

import random

import numpy as np

from robot_sim.detectors.base import Detection


class MockDetector:
    """按候选类别随机生成检测结果，用于链路联调。

    它存在的唯一理由是：没装 torch 时服务仍能起来、链路仍能跑通。
    绝不用于演示「检测能力」。
    """

    is_mock = True
    name = "mock"
    model_desc = "模拟检测（未安装 ultralytics 时的降级路径）"

    def __init__(
        self,
        candidate_labels: tuple[str, ...] = ("person", "book", "cup", "laptop", "chair"),
        hit_prob: float = 0.65,
        conf_range: tuple[float, float] = (0.45, 0.93),
        seed: int | None = None,
    ) -> None:
        self.candidate_labels = candidate_labels
        self.hit_prob = hit_prob
        self.conf_range = conf_range
        self._rng = random.Random(seed)

    def detect(self, image: np.ndarray) -> list[Detection]:
        h, w = (image.shape[0], image.shape[1]) if image is not None and image.ndim >= 2 else (720, 1280)
        out: list[Detection] = []

        for label in self.candidate_labels:
            if self._rng.random() > self.hit_prob:
                continue
            bw = self._rng.uniform(0.10, 0.32) * w
            bh = self._rng.uniform(0.12, 0.38) * h
            x1 = self._rng.uniform(0, max(1.0, w - bw))
            y1 = self._rng.uniform(0, max(1.0, h - bh))
            out.append(
                Detection(
                    label=label,
                    confidence=self._rng.uniform(*self.conf_range),
                    bbox=(x1, y1, x1 + bw, y1 + bh),
                )
            )

        out.sort(key=lambda d: d.confidence, reverse=True)
        return out
