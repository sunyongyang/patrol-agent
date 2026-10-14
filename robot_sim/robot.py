# -*- coding: utf-8 -*-
"""虚拟机器人 R-01：状态机 + 任务执行。

对应 Spec §5.1 / §5.7。

关键设计：
1. **常驻持有摄像头**。实测摄像头首次打开要 6 秒，每次拍照重开会让演示卡顿。
2. **就位窗口在拍照之前**。到达点位 → 等 dwell_sec（这段时间现场人员走过去/放书）
   → 拍照 → 推理 → 落库 → 下一点位。
   `/sim/advance` 跳过当前点位剩余的等待时间（已经就位时用）。
3. 告警落库后**回调 Agent 服务**触发 IM 推送。回调失败不影响巡逻本身。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any

import requests

from robot_sim.annotate import annotate
from robot_sim.camera import Camera, CameraError, new_basename, save_jpeg
from robot_sim.detectors.base import Detector
from robot_sim.mapper import evaluate
from shared import IMAGE_DIR, repo
from shared.db import now_iso
from shared.scene import Point, Route, point as get_point, robot as robot_profile, route as get_route

log = logging.getLogger("robot_sim.robot")

PUSH_TIMEOUT = 8


class RobotBusy(RuntimeError):
    pass


class VirtualRobot:
    def __init__(
        self,
        robot_id: str,
        camera: Camera,
        detector: Detector,
        agent_base_url: str = "http://127.0.0.1:8200",
    ) -> None:
        self.robot_id = robot_id
        self.camera = camera
        self.detector = detector
        self.agent_base_url = agent_base_url.rstrip("/")

        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._skip = threading.Event()      # 时间快进：跳过当前 dwell
        self._stop = threading.Event()      # 取消任务
        self.cancel_requested = False

        # 运行态
        self.status = "idle"                # idle | patrolling | charging | offline
        self.current_point: str | None = None
        self.current_task_id: str | None = None
        self.current_run_id: str | None = None
        self.battery = 100.0
        self.last_error: str | None = None
        self.last_dwell_remaining = 0.0

    # ------------------------------------------------------------ 对外状态

    def status_dict(self) -> dict[str, Any]:
        with self._lock:
            pt = get_point(self.current_point) if self.current_point else None
            return {
                "robot_id": self.robot_id,
                "status": self.status,
                "battery": round(self.battery, 1),
                "current_point": self.current_point,
                "current_point_name": pt.name if pt else None,
                "current_task_id": self.current_task_id,
                "current_run_id": self.current_run_id,
                "online": self.status != "offline",
                "last_error": self.last_error,
                "dwell_remaining": round(self.last_dwell_remaining, 1),
                "detector": {
                    "name": self.detector.name,
                    "model": self.detector.model_desc,
                    "is_mock": self.detector.is_mock,
                },
            }

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------ 控制

    def start_task(self, task: dict) -> dict:
        with self._lock:
            if self.busy:
                raise RobotBusy(
                    f"机器人正在执行 {self.current_task_id}，请等它结束或先取消"
                )
            rt: Route = get_route(task["route_id"])
            run_id = repo.create_run(task["id"], self.robot_id, len(rt.points))

            self.current_task_id = task["id"]
            self.current_run_id = run_id
            self.status = "patrolling"
            self.last_error = None
            self._stop.clear()
            self._skip.clear()
            self.cancel_requested = False

            repo.set_task_status(task["id"], "running")
            repo.update_robot(self.robot_id, status="patrolling")

            self._thread = threading.Thread(
                target=self._execute,
                args=(task, rt, run_id),
                name=f"run-{run_id}",
                daemon=True,
            )
            self._thread.start()

            return {"task_id": task["id"], "run_id": run_id, "route_id": rt.id,
                    "points": [rp.point_id for rp in rt.points]}

    def cancel(self) -> bool:
        with self._lock:
            if not self.busy:
                return False
            self._stop.set()
            self._skip.set()
            self.cancel_requested = True
            return True

    def advance(self) -> str:
        """时间快进：跳过当前点位剩余的等待时间。"""
        with self._lock:
            if not self.busy:
                return "当前没有正在执行的巡逻"
            self._skip.set()
            return "已跳过当前等待，立即执行本点位拍照"

    def capture_now(self, point_id: str) -> dict:
        """立即抓拍（不参与任务流程）。"""
        pt = get_point(point_id)
        with self._lock:
            self.current_point = pt.id
        return self._capture_and_detect(run_id="", pt=pt, record=True)

    # ------------------------------------------------------------ 执行

    def _execute(self, task: dict, rt: Route, run_id: str) -> None:
        started = now_iso()
        alerts: list[str] = []
        points_done = 0
        status = "completed"

        try:
            for rp in rt.points:
                if self._stop.is_set():
                    status = "cancelled"
                    break

                pt = get_point(rp.point_id)
                with self._lock:
                    self.current_point = pt.id
                    self.battery = max(5.0, self.battery - 1.5)
                repo.update_robot(self.robot_id, current_point=pt.id, battery=self.battery)

                log.info("[%s] 到达 %s %s，就位窗口 %.0fs",
                         run_id, pt.id, pt.name, rp.dwell_sec)
                self._dwell(rp.dwell_sec)
                if self._stop.is_set():
                    status = "cancelled"
                    break

                result = self._capture_and_detect(run_id=run_id, pt=pt, record=True)
                points_done += 1
                repo.bump_run_progress(run_id)

                if result.get("alert_id"):
                    alerts.append(result["alert_id"])
                    self._notify_alert(result)

            if self._stop.is_set():
                status = "cancelled"

        except CameraError as exc:
            status = "failed"
            self.last_error = str(exc)
            log.error("[%s] 摄像头故障: %s", run_id, exc)
        except Exception as exc:  # noqa: BLE001
            status = "failed"
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.exception("[%s] 执行异常", run_id)
        finally:
            repo.finish_run(run_id, status)
            repo.set_task_status(task["id"], status)
            with self._lock:
                self.status = "idle"
                self.current_task_id = None
                self.current_run_id = None
                self._thread = None
            repo.update_robot(self.robot_id, status="idle")

            summary = {
                "run_id": run_id,
                "task_id": task["id"],
                "robot_id": self.robot_id,
                "route_id": rt.id,
                "status": status,
                "points_total": len(rt.points),
                "points_done": points_done,
                "alert_ids": alerts,
                "started_at": started,
                "ended_at": now_iso(),
                "error": self.last_error,
            }
            log.info("[%s] 轮次结束 status=%s 告警=%d", run_id, status, len(alerts))
            self._notify_run_finished(summary)

    def _dwell(self, seconds: float) -> None:
        """可被时间快进打断的等待。"""
        self._skip.clear()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._stop.is_set():
                return
            if self._skip.wait(timeout=0.25):
                self._skip.clear()
                log.info("时间快进：跳过剩余等待")
                with self._lock:
                    self.last_dwell_remaining = 0.0
                return
            with self._lock:
                self.last_dwell_remaining = max(0.0, deadline - time.monotonic())
        with self._lock:
            self.last_dwell_remaining = 0.0

    def _capture_and_detect(self, run_id: str, pt: Point, record: bool = True) -> dict:
        """抓帧 → 推理 → 绘制结果图 → 落库。返回本次结果摘要。

        落两张图（证据链，Spec §8）：
            <base>_raw.jpg  原始帧，未绘制 —— 证据本体
            <base>.jpg      标注帧，带检测框 —— 推送给用户的那张
        image_path 指向标注帧，raw_image_path 指向原始帧。
        """
        frame = self.camera.capture()
        base = new_basename(f"{pt.id}_{run_id or 'manual'}")

        started = time.monotonic()
        detections = self.detector.detect(frame)
        infer_ms = (time.monotonic() - started) * 1000

        # 原始帧先落盘 —— 即使后面绘制或入库失败，证据也已经保住了
        raw_path = save_jpeg(frame, IMAGE_DIR / f"{base}_raw.jpg")

        # 标注帧：把算法的包围框画出来
        annotated = annotate(
            frame,
            detections,
            point_id=pt.id,
            point_name=pt.name,
            target_label=pt.target_label,
            target_threshold=pt.conf_threshold,
            model_desc=self.detector.model_desc,
            is_mock=self.detector.is_mock,
            robot_name=robot_profile().name,
        )
        image_path = save_jpeg(annotated, IMAGE_DIR / f"{base}.jpg")

        # 全类别落库（保留原始证据），点位过滤只影响是否产生告警
        detection_ids: list[str] = []
        if record:
            for d in detections:
                detection_ids.append(
                    repo.insert_detection(
                        run_id=run_id or "manual",
                        point_id=pt.id,
                        coco_label=d.label,
                        confidence=d.confidence,
                        bbox=d.bbox,
                        image_path=image_path,
                        raw_image_path=raw_path,
                        is_mock=self.detector.is_mock,
                    )
                )

        decisions = evaluate(pt, detections)
        result: dict[str, Any] = {
            "point_id": pt.id,
            "point_name": pt.name,
            "image_path": image_path,
            "raw_image_path": raw_path,
            "infer_ms": round(infer_ms),
            "detections": [d.to_dict() for d in detections],
            "alert_id": None,
            "run_id": run_id,
        }

        log.info("[%s] %s 推理 %.0fms 检出 %s", run_id or "manual", pt.id, infer_ms,
                 [(d.label, round(d.confidence, 3)) for d in detections] or "无")

        if decisions:
            dec = decisions[0]
            if record:
                alert_id = repo.insert_alert(
                    detection_id=detection_ids[0] if detection_ids else "",
                    level=dec.level,
                    level_code=dec.level_code,
                    alert_type=dec.alert_type,
                )
                result["alert_id"] = alert_id
                result["alert_type"] = dec.alert_type
                result["level"] = dec.level
                result["confidence"] = round(dec.detection.confidence, 4)
                result["coco_label"] = dec.detection.label
                result["detected_at"] = now_iso()
                log.info("[%s] ★ 告警 %s（%s）conf=%.3f",
                         run_id or "manual", dec.alert_type, dec.level,
                         dec.detection.confidence)

        return result

    # ------------------------------------------------------------ 回调 Agent

    def _notify_alert(self, result: dict) -> None:
        payload = {
            "alert_id": result["alert_id"],
            "alert_type": result.get("alert_type"),
            "level": result.get("level"),
            "coco_label": result.get("coco_label"),
            "confidence": result.get("confidence"),
            "point_id": result.get("point_id"),
            "point_name": result.get("point_name"),
            "run_id": result.get("run_id"),
            "image_path": result.get("image_path"),
            "detected_at": result.get("detected_at"),
        }
        try:
            resp = requests.post(
                f"{self.agent_base_url}/internal/alert", json=payload, timeout=PUSH_TIMEOUT
            )
            log.info("告警回调 Agent 服务: HTTP %s", resp.status_code)
        except requests.RequestException as exc:
            # 推送失败不影响巡逻与落库——告警在库里，报表照样出得来
            log.warning("告警回调失败（Agent 服务未启动？）: %s", exc)

    def _notify_run_finished(self, summary: dict) -> None:
        try:
            resp = requests.post(
                f"{self.agent_base_url}/internal/run-finished",
                json=summary,
                timeout=PUSH_TIMEOUT,
            )
            log.info("轮次完成回调 Agent 服务: HTTP %s", resp.status_code)
        except requests.RequestException as exc:
            log.warning("轮次完成回调失败: %s", exc)
