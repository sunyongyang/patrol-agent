# -*- coding: utf-8 -*-
"""检测器工厂：优先真实推理，装不上才降级。"""

from __future__ import annotations

import logging

from robot_sim.detectors.base import Detection, Detector
from robot_sim.detectors.mock import MockDetector
from shared.scene import DetectorProfile

log = logging.getLogger(__name__)

__all__ = ["Detection", "Detector", "MockDetector", "build_detector"]


def build_detector(profile: DetectorProfile) -> Detector:
    """构造检测器。

    优先 YOLO 真实推理；只有 import 失败（没装 ultralytics/torch）时
    才按配置降级为 MockDetector，并打醒目日志——避免「以为在真跑，其实在模拟」。
    """
    try:
        from robot_sim.detectors.yolo import YoloDetector

        det = YoloDetector(model_name=profile.model, conf_floor=profile.conf_floor)
        log.info("检测器就绪：%s（真实推理）", det.model_desc)
        return det
    except ImportError as exc:
        if not profile.allow_mock_fallback:
            raise
        log.warning(
            "无法加载 YOLO（%s），降级为模拟检测。"
            "所有结果将标记 is_mock=true，不可用于演示检测能力。",
            exc,
        )
        return MockDetector()
