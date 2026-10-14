# -*- coding: utf-8 -*-
"""YOLO 真实推理（主路径，Spec §5.3）。

用 COCO 预训练的 YOLOv8n：CPU 可跑，权重约 6MB。
输出全类别，不做点位过滤。
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from robot_sim.detectors.base import Detection
from shared import MODEL_DIR


class YoloDetector:
    """ultralytics YOLOv8 COCO 检测器。"""

    is_mock = False

    def __init__(self, model_name: str = "yolov8n.pt", conf_floor: float = 0.35) -> None:
        from ultralytics import YOLO  # 延迟导入：没装也不影响其他模块 import

        self.conf_floor = conf_floor
        self.name = "yolo"
        self.model_desc = f"YOLOv8 COCO · {model_name}"

        # 权重统一放 models/，避免污染工作目录
        local = Path(MODEL_DIR) / model_name
        self._model = YOLO(str(local))
        self._warm = False

    def _warmup(self) -> None:
        """首帧要建图+分配内存，实测比后续帧慢 5-10 倍。预热一次。"""
        if self._warm:
            return
        blank = np.zeros((640, 640, 3), dtype=np.uint8)
        self._model.predict(source=blank, conf=self.conf_floor, verbose=False, device="cpu")
        self._warm = True

    def detect(self, image: np.ndarray) -> list[Detection]:
        self._warmup()
        results = self._model.predict(
            source=image,
            conf=self.conf_floor,
            verbose=False,
            device="cpu",
        )
        if not results:
            return []

        r = results[0]
        names = r.names or {}
        out: list[Detection] = []
        boxes = getattr(r, "boxes", None)
        if boxes is None:
            return []

        for box in boxes:
            try:
                cls_id = int(box.cls[0])
                conf = float(box.conf[0])
                xyxy = [float(v) for v in box.xyxy[0]]
            except (IndexError, TypeError, ValueError):
                continue
            out.append(
                Detection(
                    label=str(names.get(cls_id, cls_id)),
                    confidence=conf,
                    bbox=(xyxy[0], xyxy[1], xyxy[2], xyxy[3]),
                )
            )

        out.sort(key=lambda d: d.confidence, reverse=True)
        return out

    def detect_timed(self, image: np.ndarray) -> tuple[list[Detection], float]:
        started = time.monotonic()
        dets = self.detect(image)
        return dets, time.monotonic() - started
