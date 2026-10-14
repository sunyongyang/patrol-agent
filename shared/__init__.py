# -*- coding: utf-8 -*-
"""巡逻 Agent Demo · 共享层。"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
LOG_DIR = ROOT / "logs"
IMAGE_DIR = ROOT / "data" / "images"
EXPORT_DIR = ROOT / "data" / "exports"
MODEL_DIR = ROOT / "models"
DB_PATH = ROOT / "data" / "patrol.db"

for _d in (LOG_DIR, IMAGE_DIR, EXPORT_DIR, MODEL_DIR, DB_PATH.parent):
    _d.mkdir(parents=True, exist_ok=True)
