# -*- coding: utf-8 -*-
"""机器人模拟器服务 :8100（Spec §5.6）。

启动：
    .venv\\Scripts\\python.exe -m uvicorn robot_sim.main:app --port 8100

设计说明：本服务**不含任何业务对话逻辑**，只负责「一台机器人能做什么」。
将来换成真机或真平台时，Agent 服务不需要改一行 —— 这是 Spec §3 的核心架构决策。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from robot_sim.camera import Camera, CameraError
from robot_sim.detectors import build_detector
from robot_sim.robot import RobotBusy, VirtualRobot
from shared import config, db, repo
from shared.scene import algorithms, camera as cam_profile, detector as det_profile
from shared.scene import robot as robot_profile
from shared.scene import routes as all_routes, points as all_points

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("robot_sim")

STATE: dict[str, object] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    repo.ensure_base_entities()

    rp = robot_profile()
    agent_base = config.get(
        "agent_service.base_url",
        f"http://127.0.0.1:{config.get('agent_service.port', 8200)}",
    )

    camera = Camera(cam_profile())
    detector = build_detector(det_profile())
    robot = VirtualRobot(rp.id, camera, detector, agent_base_url=agent_base)

    try:
        camera.open()
        log.info("摄像头已打开并常驻（避免每次抓拍重开，实测重开要 6 秒）")
    except CameraError as exc:
        log.error("摄像头打开失败：%s", exc)
        log.error("服务仍会启动，但任务执行会失败 —— 请先解决设备占用问题")

    STATE["robot"] = robot
    STATE["camera"] = camera
    log.info("机器人模拟器就绪：%s（%s）", rp.name, rp.id)
    log.info("点位 %s｜路线 %s", list(all_points()), list(all_routes()))
    log.info("Agent 回调地址 %s", agent_base)

    yield

    camera.close()
    log.info("机器人模拟器已停止")


app = FastAPI(title="巡逻机器人模拟器", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------- 请求模型


class TaskCreate(BaseModel):
    route_id: str = Field(..., description="路线 ID")
    start_at: str | None = Field(None, description="ISO8601 开始时间，None=立即")
    repeat: str | None = Field(None, description="once / daily / cron 表达式")
    created_by: str = Field("", description="发起人 open_id")
    source_channel: str = Field("feishu")


class CaptureRequest(BaseModel):
    point_id: str = Field(..., description="点位 ID")


def _robot() -> VirtualRobot:
    robot = STATE.get("robot")
    if robot is None:
        raise HTTPException(503, "机器人尚未就绪")
    return robot  # type: ignore[return-value]


# ---------------------------------------------------------------- 接口


@app.get("/health")
def health() -> dict:
    robot = STATE.get("robot")
    camera = STATE.get("camera")
    cam_info = camera.probe() if camera else {"ok": False, "error": "未初始化"}
    return {
        "ok": True,
        "service": "robot_sim",
        "robot": robot.robot_id if robot else None,
        "camera": cam_info,
        "detector": (
            {"name": robot.detector.name, "is_mock": robot.detector.is_mock,
             "model": robot.detector.model_desc}
            if robot else None
        ),
        "tables": db.table_counts(),
    }


@app.get("/routes")
def list_routes() -> dict:
    """点位与**算法绑定关系**。这是「该点位只上报什么」的唯一事实来源。"""
    prof = det_profile()
    return {
        "robot": robot_profile().__dict__,
        "binding_policy": {
            "show_other_detections": prof.show_other_detections,
            "store_all_classes": prof.store_all_classes,
            "note": "点位只上报绑定算法的检测结果；上述两项控制结果图与落库范围",
        },
        "algorithms": [
            {
                "id": a.id,
                "name": a.name,
                "coco_label": a.coco_label,
                "alert_type": a.alert_type,
                "level": a.level,
            }
            for a in algorithms().values()
        ],
        "points": [
            {
                "id": p.id,
                "name": p.name,
                "area": p.area,
                "algorithm_id": p.algorithm_id,
                "algorithm_name": p.algorithm.name,
                "coco_label": p.target_label,
                "alert_type": p.algorithm.alert_type,
                "conf_threshold": p.conf_threshold,
            }
            for p in all_points().values()
        ],
        "routes": [
            {
                "id": r.id, "name": r.name, "description": r.description,
                "points": [{"point_id": rp.point_id, "dwell_sec": rp.dwell_sec} for rp in r.points],
            }
            for r in all_routes().values()
        ],
    }


@app.get("/robot/{robot_id}/status")
def robot_status(robot_id: str) -> dict:
    robot = _robot()
    st = robot.status_dict()
    repo.update_robot(robot_id, online=1)
    return st


@app.post("/robot/{robot_id}/tasks", status_code=201)
def create_task(robot_id: str, body: TaskCreate) -> dict:
    robot = _robot()

    # 定时任务尚未实现：没有调度器读取 task.start_at。
    # 与其静默忽略（用户以为设了 22:00，实际立刻出发），不如明确拒绝。
    if body.start_at:
        try:
            start = datetime.fromisoformat(body.start_at)
        except ValueError as exc:
            raise HTTPException(400, f"start_at 格式不正确，应为 ISO8601：{body.start_at}") from exc
        if start > datetime.now() + timedelta(seconds=5):
            raise HTTPException(
                501,
                "定时巡逻尚未实现。本版本只支持立即执行，请留空 start_at。",
            )

    try:
        task = repo.create_task(
            robot_id=robot_id,
            route_id=body.route_id,
            created_by=body.created_by,
            source_channel=body.source_channel,
            start_at=None,
            repeat_cron=None,
        )
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from exc

    try:
        run = robot.start_task(task)
    except RobotBusy as exc:
        repo.set_task_status(task["id"], "rejected")
        raise HTTPException(409, str(exc)) from exc

    return {"task": task, "run": run}


@app.get("/robot/{robot_id}/tasks/{task_id}")
def task_detail(robot_id: str, task_id: str) -> dict:
    task = repo.get_task(task_id)
    if not task:
        raise HTTPException(404, f"任务不存在: {task_id}")
    return {"task": task, "run": repo.run_for_task(task_id)}


@app.delete("/robot/{robot_id}/tasks/{task_id}")
def cancel_task(robot_id: str, task_id: str) -> dict:
    robot = _robot()
    task = repo.get_task(task_id)
    if not task:
        raise HTTPException(404, f"任务不存在: {task_id}")
    if task["status"] not in ("pending", "running"):
        raise HTTPException(409, f"任务当前状态 {task['status']}，无法取消")
    ok = robot.cancel()
    return {"cancelled": ok, "task_id": task_id}


@app.post("/robot/{robot_id}/capture")
def capture(robot_id: str, body: CaptureRequest) -> dict:
    robot = _robot()
    try:
        return robot.capture_now(body.point_id)
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from exc
    except CameraError as exc:
        raise HTTPException(503, f"摄像头不可用: {exc}") from exc


@app.post("/sim/advance")
def sim_advance() -> dict:
    """时间快进：跳过当前点位剩余的就位等待（Spec §5.6）。"""
    robot = _robot()
    return {"message": robot.advance(), "status": robot.status_dict()}
