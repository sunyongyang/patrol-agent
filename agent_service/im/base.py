# -*- coding: utf-8 -*-
"""IM 适配层抽象（Spec §7.3）。

业务代码只依赖这一层，不感知飞书与微信的差异。
`capabilities()` 决定报表引擎走卡片还是降级渲染。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Capabilities:
    card: bool = False          # 能否发交互卡片
    button: bool = False        # 卡片是否支持按钮
    image: bool = True          # 能否发图片
    file: bool = False          # 能否发文件
    card_update: bool = False   # 能否就地更新已发出的卡片


@dataclass
class InboundMessage:
    channel: str
    chat_id: str
    user_id: str
    text: str
    chat_type: str = "p2p"
    raw: dict[str, Any] | None = None


@runtime_checkable
class IMAdapter(Protocol):
    channel: str

    def capabilities(self) -> Capabilities: ...
    def send_text(self, chat_id: str, text: str) -> str | None: ...
    def send_card(self, chat_id: str, card: dict) -> str | None: ...
    def update_card(self, message_id: str, card: dict) -> bool: ...
    def upload_image(self, path: str) -> str | None: ...
    def send_image(self, chat_id: str, path: str) -> str | None: ...


FEISHU_CAPS = Capabilities(card=True, button=True, image=True, file=True, card_update=True)
PLAIN_CAPS = Capabilities(card=False, button=False, image=True, file=False, card_update=False)
