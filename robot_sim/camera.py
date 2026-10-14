# -*- coding: utf-8 -*-
"""摄像头采集。

对应 Spec §5.2：两个点位共用同一台摄像头，差异只在业务标签。
所以这里只负责「抓一张当前帧」，不关心是哪个点位。

线程安全说明：内部统一用 RLock + *_locked 私有方法，
持锁的公开方法只负责加锁，避免自我重入死锁。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from shared import IMAGE_DIR
from shared.scene import CameraProfile


class CameraError(RuntimeError):
    pass


def new_basename(tag: str) -> str:
    """生成同一次拍照共用的文件名主干（原始帧与标注帧共用，便于配对）。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    return f"{stamp}_{tag}"


def save_jpeg(frame: np.ndarray, path: str | Path, quality: int = 88) -> str:
    if not cv2.imwrite(str(path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality]):
        raise CameraError(f"写入图片失败: {path}")
    return str(path)


class Camera:
    """线程安全的单摄像头封装。

    Windows 上优先用 CAP_DSHOW 后端：默认 MSMF 后端打开设备要 2-3 秒，
    DSHOW 通常 0.3 秒内可用，对演示体验影响很大。
    """

    def __init__(self, profile: CameraProfile) -> None:
        self.profile = profile
        self._cap: cv2.VideoCapture | None = None
        self._lock = threading.RLock()

    # -------------------------------------------------- 内部（需持锁）

    def _open_locked(self) -> None:
        if self._cap is not None and self._cap.isOpened():
            return

        last_err: str | None = None
        for backend, bname in ((cv2.CAP_DSHOW, "DSHOW"), (cv2.CAP_ANY, "ANY")):
            cap = cv2.VideoCapture(self.profile.index, backend)
            if not cap.isOpened():
                cap.release()
                last_err = f"{bname} 后端打不开设备 index={self.profile.index}"
                continue

            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.profile.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.profile.height)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            self._cap = cap
            # 丢弃前几帧：自动曝光/白平衡未稳定时前几帧偏暗或偏色
            for _ in range(max(0, self.profile.warmup_frames)):
                cap.grab()
            return

        raise CameraError(
            f"摄像头不可用：{last_err}\n"
            "请确认：1) 设备未被其他程序占用（飞书/钉钉/相机应用会抢设备） "
            "2) Windows 相机隐私开关已允许 3) config/routes.yaml 的 camera.index 正确"
        )

    def _release_locked(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def _read_locked(self) -> np.ndarray:
        self._open_locked()
        assert self._cap is not None
        ok, frame = self._cap.read()
        if ok and frame is not None:
            return frame

        # 抓帧失败通常是设备被抢占：重开一次再试
        self._release_locked()
        self._open_locked()
        assert self._cap is not None
        ok, frame = self._cap.read()
        if not ok or frame is None:
            raise CameraError("摄像头读取失败（设备可能被其他程序占用）")
        return frame

    # -------------------------------------------------- 公开 API

    def open(self) -> None:
        with self._lock:
            self._open_locked()

    def close(self) -> None:
        with self._lock:
            self._release_locked()

    def capture(self) -> np.ndarray:
        """抓一帧 BGR 图像。失败抛 CameraError（不静默返回空图）。"""
        with self._lock:
            return self._read_locked()

    def capture_to_file(self, tag: str = "frame") -> tuple[np.ndarray, str]:
        """抓帧并落盘（原始帧），返回 (图像, 路径)。probe 脚本用。"""
        with self._lock:
            frame = self._read_locked()
            path = IMAGE_DIR / f"{new_basename(tag)}.jpg"
            save_jpeg(frame, path)
            return frame, str(path)

    @staticmethod
    def encode_jpeg(frame: np.ndarray, quality: int = 85) -> bytes:
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        if not ok:
            raise CameraError("JPEG 编码失败")
        return buf.tobytes()

    def probe(self, close: bool = False) -> dict:
        """自检：能否打开、分辨率、抓一帧耗时。

        ⚠️ close 默认 **False**。
        这里曾经无条件 `finally: self.close()`，而 /health 会调 probe ——
        结果**任何一次健康检查都会把常驻的摄像头关掉**，
        下一次抓帧要重新打开（本机实测 6 秒），巡逻中途被这么打断一次就很难看。
        只有确实要释放设备的场景（如独立自检脚本收尾）才传 close=True。
        """
        info: dict = {"index": self.profile.index}
        started = time.monotonic()
        try:
            with self._lock:
                frame = self._read_locked()
            info.update(
                ok=True,
                height=int(frame.shape[0]),
                width=int(frame.shape[1]),
                channels=int(frame.shape[2]),
                capture_ms=round((time.monotonic() - started) * 1000, 1),
            )
        except Exception as exc:  # noqa: BLE001
            info.update(ok=False, error=f"{type(exc).__name__}: {exc}")
        finally:
            if close:
                self.close()
        return info

    def opened(self) -> bool:
        with self._lock:
            return self._cap is not None and self._cap.isOpened()
