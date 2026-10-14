# -*- coding: utf-8 -*-
"""定时推送（Spec §6.6）。

用最简单的线程轮询，不引入 APScheduler —— 只有一条「每日 08:30 推日报」的规则，
上调度框架是过度设计。

幂等：同一天只推一次（记录 _last_sent_day），避免进程重启后重复轰炸。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from shared import LOG_DIR, config

log = logging.getLogger("agent_service.scheduler")

STATE_FILE = LOG_DIR / ".daily_report_state"


class DailyReportScheduler:
    def __init__(self, send_daily) -> None:
        self.send_daily = send_daily
        self.at = str(config.get("agent_service.daily_report_time", "08:30"))
        self.enabled = bool(config.get("agent_service.daily_report_enabled", True))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # 已推日期持久化：否则每次重启进程都会补推一次日报
        self._last_sent_day: str | None = self._load_state()

    # ------------------------------------------------------------ 状态持久化

    @staticmethod
    def _load_state() -> str | None:
        try:
            return STATE_FILE.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    @staticmethod
    def _save_state(day: str) -> None:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(day, encoding="utf-8")
        except OSError:
            log.warning("无法保存日报推送状态，重启后可能重复推送")

    # ------------------------------------------------------------ 主循环

    def start(self) -> None:
        if not self.enabled:
            log.info("定时日报已关闭")
            return
        self._thread = threading.Thread(target=self._loop, name="daily-report", daemon=True)
        self._thread.start()
        log.info(
            "定时日报已启动：每天 %s（上次已推 %s）",
            self.at,
            self._last_sent_day or "无记录",
        )

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                log.exception("定时日报检查失败")
            self._stop.wait(timeout=30)

    def _tick(self) -> None:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        hhmm = now.strftime("%H:%M")
        if hhmm >= self.at and self._last_sent_day != today:
            self._last_sent_day = today
            self._save_state(today)
            log.info("触发每日日报推送（%s）", today)
            try:
                self.send_daily(today)
            except Exception:  # noqa: BLE001
                log.exception("日报推送失败")


def wait_forever() -> None:
    while True:
        time.sleep(3600)
