# -*- coding: utf-8 -*-
"""摄像头 + YOLO 实测脚本（风险 R6 验证）。

设计成**无人值守定时采样**，不需要按键配合：
在采样窗口内你只要做动作，脚本自己抓拍、推理、汇总。

用法：
    .venv\\Scripts\\python.exe scripts\\probe_camera.py --seconds 60 --every 2

典型流程（60 秒窗口）：
    第 0-25 秒   站到摄像头前，让画面能看到你     → 验证 person
    第 25-60 秒  把一本书放到镜头前并退开         → 验证 book

为什么必须实测：
    Spec §12 的 R6 风险是「book 的检出率可能低于阈值」。
    COCO 的 book 类训练样本多是书架/成摞书本/清晰封面，
    单本平放桌面可能只给 0.3-0.5 分，而 P2 阈值设在 0.50。
    过不了就要调阈值、换 yolov8s、或改拍摄条件。
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2  # noqa: E402

from robot_sim.camera import Camera, CameraError  # noqa: E402
from robot_sim.detectors import build_detector  # noqa: E402
from shared import IMAGE_DIR  # noqa: E402
from shared.scene import camera as cam_profile  # noqa: E402
from shared.scene import detector as det_profile  # noqa: E402
from shared.scene import point as get_point  # noqa: E402

OK = "[OK]"
FAIL = "[FAIL]"
WARN = "[!]"


def banner(text: str) -> None:
    print("\n" + "=" * 70)
    print(text)
    print("=" * 70, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="摄像头 + YOLO 实测（无人值守采样）")
    ap.add_argument("--seconds", type=float, default=60.0, help="采样总时长（秒）")
    ap.add_argument("--every", type=float, default=2.0, help="采样间隔（秒）")
    ap.add_argument(
        "--conf-floor",
        type=float,
        default=None,
        help="覆盖检测置信度下限（诊断用）。默认取 config/mapping.yaml 的 detector.conf_floor。"
        "想看「有没有微弱信号」时传 0.10",
    )
    args = ap.parse_args()

    banner("1/3 摄像头自检")
    cam = Camera(cam_profile())
    info = cam.probe()
    for k, v in info.items():
        print(f"      {k} = {v}")
    if not info.get("ok"):
        print(f"\n{FAIL} 摄像头不可用。")
        print("     常见原因：飞书/钉钉/相机应用正占用摄像头；Windows 隐私设置未放行。")
        return 1
    print(f"      {OK} 摄像头可用，抓帧 {info.get('capture_ms')}ms")

    banner("2/3 加载检测器")
    prof = det_profile()
    if args.conf_floor is not None:
        prof = replace(prof, conf_floor=args.conf_floor)
        print(f"      [诊断模式] 置信度下限覆盖为 {args.conf_floor}")
    det = build_detector(prof)
    print(f"      检测器   = {det.name}")
    print(f"      模型     = {det.model_desc}")
    print(f"      是否模拟 = {det.is_mock}")
    if det.is_mock:
        print(f"      {FAIL} 当前是模拟检测 —— ultralytics/torch 没装上，实测无意义。")
        return 1
    print(f"      {OK} 真实推理检测器就绪")

    watched = ["person", "book"]
    p1, p2 = get_point("P1"), get_point("P2")
    thresholds = {p1.target_label: p1.conf_threshold, p2.target_label: p2.conf_threshold}

    banner("3/3 开始采样")
    print("  ★ 现在开始动作，脚本会自动抓拍，不需要按键：")
    print(f"      · 前 {int(args.seconds * 0.4)} 秒：请站到摄像头前，让画面能看到你")
    print(f"      · 之后     ：把一本书拿到镜头前，然后人退开，只留书在画面里")
    print(f"  采样 {args.seconds:.0f} 秒，每 {args.every:.0f} 秒一帧\n", flush=True)

    best: dict[str, dict] = {label: {"conf": 0.0, "path": None, "at": None} for label in watched}
    seen_labels: dict[str, int] = {}
    frames = 0
    infer_total = 0.0
    started = time.monotonic()
    next_tick = started

    try:
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= args.seconds:
                break
            if time.monotonic() < next_tick:
                time.sleep(min(0.2, next_tick - time.monotonic()))
                continue
            next_tick += args.every

            try:
                frame = cam.capture()
            except CameraError as exc:
                print(f"  [{elapsed:5.1f}s] {FAIL} 抓帧失败: {exc}", flush=True)
                break

            t0 = time.monotonic()
            dets = det.detect(frame)
            infer_ms = (time.monotonic() - t0) * 1000
            frames += 1
            infer_total += infer_ms

            for d in dets:
                seen_labels[d.label] = seen_labels.get(d.label, 0) + 1

            summary = ", ".join(f"{d.label}={d.confidence:.2f}" for d in dets[:5]) or "无检出"
            print(f"  [{elapsed:5.1f}s] {infer_ms:4.0f}ms  {summary}", flush=True)

            # 为每个关注类别留存最高分的那一帧，便于事后人工核对
            for d in dets:
                if d.label in best and d.confidence > best[d.label]["conf"]:
                    stamp = time.strftime("%H%M%S")
                    path = IMAGE_DIR / f"probe_{d.label}_{d.confidence:.2f}_{stamp}.jpg"
                    cv2.imwrite(str(path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
                    best[d.label] = {
                        "conf": d.confidence,
                        "path": str(path),
                        "at": round(elapsed, 1),
                    }
    except KeyboardInterrupt:
        print(f"\n{WARN} 手动中断")

    cam.close()

    banner("实测结论")
    print(f"  采样 {frames} 帧，平均推理 {infer_total / frames:.0f}ms" if frames else "  未采集到任何帧")
    print()
    print(f"  {'类别':<10}{'最高分':>8}{'阈值':>8}{'判定':>8}   出现帧数   最佳帧")
    print("  " + "-" * 74)

    all_pass = True
    for label in watched:
        b = best[label]
        th = thresholds.get(label, 0.5)
        passed = b["conf"] >= th
        all_pass = all_pass and passed
        mark = "会告警" if passed else "不告警"
        print(
            f"  {label:<10}{b['conf']:>8.3f}{th:>8.2f}{mark:>8}   "
            f"{seen_labels.get(label, 0):>6}     {b['path'] or '-'}"
        )

    print()
    if all_pass:
        print(f"  {OK} 两个关注类别都能过阈值 —— R6 风险解除，可按现配置开发。")
        return 0

    print(f"  {WARN} 有类别没过阈值。可选处置：")
    print("       1) 降低该点位 conf_threshold（config/routes.yaml）")
    print("       2) 换更大模型：config/mapping.yaml 的 detector.model 改为 yolov8s.pt")
    print("       3) 改拍摄条件：书立起来朝镜头、靠近到 1-2 米内、增加光照")
    print(f"\n  抓拍图在 {IMAGE_DIR}，可以打开看当时的画面。")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
