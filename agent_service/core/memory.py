# -*- coding: utf-8 -*-
"""会话记忆（Spec §6.3）。

两件事：
1. 保留最近 N 轮对话供 LLM 使用
2. 记住「上一条列表卡片里的告警顺序」，用于指代消解（「第 2 条是误报」）

过期策略：30 分钟无交互就清空，避免陈旧指代 ——
否则用户上午看了列表、下午说「第 2 条」，会指到一条毫不相干的记录。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

MAX_TURNS = 10
IDLE_TTL_SEC = 30 * 60


@dataclass
class ChatState:
    turns: list[dict] = field(default_factory=list)
    last_alert_ids: list[str] = field(default_factory=list)
    last_pending_pa: str | None = None
    last_seen: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.last_seen = time.monotonic()

    @property
    def stale(self) -> bool:
        return (time.monotonic() - self.last_seen) > IDLE_TTL_SEC


class Memory:
    def __init__(self) -> None:
        self._states: dict[str, ChatState] = {}
        self._lock = threading.RLock()

    def _state(self, chat_id: str) -> ChatState:
        with self._lock:
            st = self._states.get(chat_id)
            if st is None or st.stale:
                if st is not None:
                    # 过期就重建，但保留空壳语义：旧指代一律作废
                    self._states[chat_id] = ChatState()
                else:
                    self._states[chat_id] = ChatState()
                st = self._states[chat_id]
            st.touch()
            return st

    # ------------------------------------------------------------ 对话轮次

    def add_user(self, chat_id: str, text: str) -> None:
        st = self._state(chat_id)
        with self._lock:
            st.turns.append({"role": "user", "content": text})
            st.turns = st.turns[-MAX_TURNS * 2:]

    def add_assistant(self, chat_id: str, text: str) -> None:
        st = self._state(chat_id)
        with self._lock:
            st.turns.append({"role": "assistant", "content": text[:1500]})
            st.turns = st.turns[-MAX_TURNS * 2:]

    def messages(self, chat_id: str, system: str) -> list[dict]:
        st = self._state(chat_id)
        with self._lock:
            return [{"role": "system", "content": system}, *st.turns]

    def clear(self, chat_id: str) -> None:
        with self._lock:
            self._states.pop(chat_id, None)

    # ------------------------------------------------------------ 指代消解

    def remember_alerts(self, chat_id: str, alert_ids: list[str]) -> None:
        st = self._state(chat_id)
        with self._lock:
            st.last_alert_ids = list(alert_ids)

    def resolve_alert_index(self, chat_id: str, index: int) -> str | None:
        """把「第 N 条」解析成告警号。越界返回 None。"""
        st = self._state(chat_id)
        with self._lock:
            if 1 <= index <= len(st.last_alert_ids):
                return st.last_alert_ids[index - 1]
            return None

    def last_alerts_snapshot(self, chat_id: str) -> list[str]:
        st = self._state(chat_id)
        with self._lock:
            return list(st.last_alert_ids)

    # ------------------------------------------------------------ 待确认动作

    def remember_pending(self, chat_id: str, pa_id: str) -> None:
        st = self._state(chat_id)
        with self._lock:
            st.last_pending_pa = pa_id

    def get_pending(self, chat_id: str) -> str | None:
        st = self._state(chat_id)
        with self._lock:
            return st.last_pending_pa


memory = Memory()
