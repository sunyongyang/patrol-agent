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
from robot_sim.detectors.base import Detection, Detector
from robot_sim.mapper import bound_detections, evaluate
from shared import IMAGE_DIR, repo
from shared.db import now_iso
from shared.scene import Point, Route, alert_policy
from shared.scene import detector as detector_profile
from shared.scene import point as get_point, robot as robot_profile, route as get_route

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

        # ★ 绑定关系在这里生效：本点位只上报绑定算法的检测结果。
        # 模型是「一次推理出全类别」，但**上报链路只看绑定的那一类**。
        prof = detector_profile()
        bound = bound_detections(pt, detections)

        # 结果图：默认只画绑定算法的框。
        # 本机实测 P2 会同时检出 person 0.92 与 chair 0.46，
        # 若把 person 也画上，图和「该点位只做通道占用检测」自相矛盾。
        draw_list = detections if prof.show_other_detections else bound
        annotated = annotate(
            frame,
            draw_list,
            point_id=pt.id,
            point_name=pt.name,
            algorithm_name=pt.algorithm.name,
            target_label=pt.target_label,
            target_threshold=pt.conf_threshold,
            model_desc=self.detector.model_desc,
            is_mock=self.detector.is_mock,
            robot_name=robot_profile().name,
        )
        image_path = save_jpeg(annotated, IMAGE_DIR / f"{base}.jpg")

        # 落库：默认保留全类别原始输出（内部留档，不是「上报」）；
        # 想严格做到「一个点位只有一种算法的数据」就改 store_all_classes: false
        store_list = detections if prof.store_all_classes else bound
        detection_rows: list[tuple[Detection, str]] = []
        if record:
            for d in store_list:
                det_id = repo.insert_detection(
                    run_id=run_id or "manual",
                    point_id=pt.id,
                    coco_label=d.label,
                    confidence=d.confidence,
                    bbox=d.bbox,
                    image_path=image_path,
                    raw_image_path=raw_path,
                    is_mock=self.detector.is_mock,
                )
                detection_rows.append((d, det_id))

        decisions = evaluate(pt, detections)
        result: dict[str, Any] = {
            "point_id": pt.id,
            "point_name": pt.name,
            "algorithm_id": pt.algorithm_id,
            "algorithm_name": pt.algorithm.name,
            "image_path": image_path,
            "raw_image_path": raw_path,
            "infer_ms": round(infer_ms),
            # ⚠️ 只返回绑定算法的检测结果 —— 这是对外的「上报」口径。
            # 全类别原始输出在库里，可通过 /routes 与数据库查询，不从这里外泄。
            "detections": [d.to_dict() for d in bound],
            "alert_id": None,
            "run_id": run_id,
        }

        log.info("[%s] %s 绑定=%s 推理 %.0fms 命中 %s（全类别检出 %s）",
                 run_id or "manual", pt.id, pt.binding_label, infer_ms,
                 [(d.label, round(d.confidence, 3)) for d in bound] or "无",
                 [(d.label, round(d.confidence, 3)) for d in detections] or "无")

        if decisions:
            dec = decisions[0]
            policy = alert_policy()
            if record:
                # 按「对象身份」匹配触发告警的那条检测，而不是取下标 0。
                # 曾经的写法 detection_ids[0] 在 P2 上会错位：
                # 检出 [person 0.92, chair 0.46]，告警是 chair，却关联到了 person ——
                # 卡片上于是出现「通道占用 · 原始输出 person 0.92」这种自相矛盾的显示。
                matched_id = next(
                    (did for d, did in detection_rows if d is dec.detection),
                    detection_rows[0][1] if detection_rows else "",
                )

                result.update(
                    alert_type=dec.alert_type,
                    level=dec.level,
                    confidence=round(dec.detection.confidence, 4),
                    coco_label=dec.detection.label,
                    target_count=dec.count,
                    detected_at=now_iso(),
                )

                # —— 告警抑制 ——
                # 同一目标持续存在时，每轮巡逻都新建告警会把值班员淹没。
                # 实测：10 条真实告警里 9 条是同一个人在同一个点位的重复报警。
                existing = repo.find_open_alert(
                    pt.id, dec.alert_type, policy.suppress_minutes
                )
                if existing:
                    updated = repo.touch_alert(
                        existing["id"], detection_id=matched_id, target_count=dec.count
                    )
                    count = int(updated.get("occurrence_count")
                                or existing.get("occurrence_count", 1) + 1)
                    result["alert_id"] = existing["id"]
                    result["suppressed"] = True
                    result["occurrence_count"] = count
                    result["first_seen_at"] = existing["created_at"]
                    result["card_message_id"] = existing.get("card_message_id") or ""
                    log.info(
                        "[%s] 抑制重复告警：%s「%s」累加至第 %d 次（首次 %s）",
                        run_id or "manual", existing["id"], dec.alert_type,
                        count, existing["created_at"][11:19],
                    )
                else:
                    alert_id = repo.insert_alert(
                        detection_id=matched_id,
                        level=dec.level,
                        level_code=dec.level_code,
                        alert_type=dec.alert_type,
                        target_count=dec.count,
                    )
                    result["alert_id"] = alert_id
                    result["suppressed"] = False
                    result["occurrence_count"] = 1
                    result["first_seen_at"] = result["detected_at"]
                    result["card_message_id"] = ""
                    log.info("[%s] ★ 新告警 %s（%s）%s ×%d conf=%.3f",
                             run_id or "manual", dec.alert_type, dec.level,
                             dec.detection.label, dec.count, dec.detection.confidence)

        if record and run_id:
            repo.insert_patrol_capture(
                run_id=run_id,
                point_id=pt.id,
                algorithm_id=pt.algorithm_id,
                algorithm_name=pt.algorithm.name,
                result_status="abnormal" if decisions else "normal",
                detections=[d.to_dict() for d in bound],
                alert_id=result.get("alert_id"),
                image_path=image_path,
                raw_image_path=raw_path,
                infer_ms=round(infer_ms),
                is_mock=self.detector.is_mock,
            )

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
            # 抑制信息：让 Agent 服务决定「就地刷新原卡片」还是「发新卡片」
            "suppressed": result.get("suppressed", False),
            "occurrence_count": result.get("occurrence_count", 1),
            "target_count": result.get("target_count", 1),
            "first_seen_at": result.get("first_seen_at"),
            "card_message_id": result.get("card_message_id") or "",
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
