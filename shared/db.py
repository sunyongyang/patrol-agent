# -*- coding: utf-8 -*-
"""SQLite 数据层。表结构见 Spec §8。

两个进程（robot_sim:8100 与 agent_service:8200）共用同一个库文件，
因此启用 WAL 模式以支持并发读写。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

from shared import DB_PATH

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS robot (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    model         TEXT,
    status        TEXT NOT NULL DEFAULT 'idle',
    battery       REAL NOT NULL DEFAULT 100.0,
    current_point TEXT,
    online        INTEGER NOT NULL DEFAULT 1,
    last_seen_at  TEXT
);

-- 点位：2 个，仅业务标签与目标类别不同（Spec §5.2）
CREATE TABLE IF NOT EXISTS point (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    area           TEXT,
    target_label   TEXT NOT NULL,
    conf_threshold REAL NOT NULL DEFAULT 0.5,
    zone_json      TEXT
);

CREATE TABLE IF NOT EXISTS route (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT
);

CREATE TABLE IF NOT EXISTS route_point (
    route_id  TEXT NOT NULL,
    point_id  TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    dwell_sec INTEGER NOT NULL DEFAULT 20,
    PRIMARY KEY (route_id, point_id)
);

CREATE TABLE IF NOT EXISTS task (
    id             TEXT PRIMARY KEY,
    robot_id       TEXT NOT NULL,
    route_id       TEXT NOT NULL,
    start_at       TEXT,
    repeat_cron    TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',
    created_by     TEXT,
    created_at     TEXT NOT NULL,
    source_channel TEXT
);

CREATE TABLE IF NOT EXISTS patrol_run (
    id           TEXT PRIMARY KEY,
    task_id      TEXT,
    robot_id     TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    status       TEXT NOT NULL DEFAULT 'running',
    points_total INTEGER NOT NULL DEFAULT 0,
    points_done  INTEGER NOT NULL DEFAULT 0
);

-- 原始模型输出：全类别，保留证据（Spec §8 可追溯性要求）
-- image_path 指向「标注帧」（带检测框，推送给用户的那张）
-- raw_image_path 指向「原始帧」（未绘制，证据本体）
CREATE TABLE IF NOT EXISTS detection (
    id             TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    point_id       TEXT NOT NULL,
    coco_label     TEXT NOT NULL,
    confidence     REAL NOT NULL,
    bbox_json      TEXT,
    image_path     TEXT,
    raw_image_path TEXT,
    is_mock        INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);

-- 映射后的业务语义；一条 detection 可产生 0 或 1 条 alert
-- occurrence_count / last_seen_at 用于告警抑制：
--   同一目标持续存在时累加次数而不是新建告警（见 config/mapping.yaml alert_policy）
CREATE TABLE IF NOT EXISTS alert (
    id              TEXT PRIMARY KEY,
    detection_id    TEXT NOT NULL,
    level           TEXT NOT NULL,
    level_code      TEXT NOT NULL,
    alert_type      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    handled_by      TEXT,
    handled_at      TEXT,
    resolution_note TEXT,
    pushed_at       TEXT,
    card_message_id TEXT,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    target_count    INTEGER NOT NULL DEFAULT 1,
    last_seen_at    TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message (
    id           TEXT PRIMARY KEY,
    channel      TEXT NOT NULL,
    chat_id      TEXT,
    user_id      TEXT,
    direction    TEXT NOT NULL,
    content_json TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_action (
    id           TEXT PRIMARY KEY,
    chat_id      TEXT NOT NULL,
    user_id      TEXT,
    intent       TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    expires_at   TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_detection_run   ON detection(run_id);
CREATE INDEX IF NOT EXISTS idx_detection_time  ON detection(created_at);
CREATE INDEX IF NOT EXISTS idx_alert_status    ON alert(status);
CREATE INDEX IF NOT EXISTS idx_alert_time      ON alert(created_at);
CREATE INDEX IF NOT EXISTS idx_run_time        ON patrol_run(started_at);
CREATE INDEX IF NOT EXISTS idx_pending_chat    ON pending_action(chat_id, status);
"""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """每次操作开新连接——SQLite 开销极低，换来天然的线程安全。"""
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """给已存在的库补新列。SQLite 的 ADD COLUMN 是原地操作，不需要重建表。"""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(detection)")}
    for column, ddl in (
        ("raw_image_path", "ALTER TABLE detection ADD COLUMN raw_image_path TEXT"),
    ):
        if column not in existing:
            conn.execute(ddl)
            print(f"[migrate] detection 表补列 {column}")

    alert_cols = {row["name"] for row in conn.execute("PRAGMA table_info(alert)")}
    for column, ddl in (
        ("resolution_note", "ALTER TABLE alert ADD COLUMN resolution_note TEXT"),
        ("occurrence_count",
         "ALTER TABLE alert ADD COLUMN occurrence_count INTEGER NOT NULL DEFAULT 1"),
        ("target_count",
         "ALTER TABLE alert ADD COLUMN target_count INTEGER NOT NULL DEFAULT 1"),
        ("last_seen_at", "ALTER TABLE alert ADD COLUMN last_seen_at TEXT"),
    ):
        if column not in alert_cols:
            conn.execute(ddl)
            print(f"[migrate] alert 表补列 {column}")


# ------------------------------------------------------------------ ID 生成


def next_id(conn: sqlite3.Connection, table: str, prefix: str) -> str:
    """生成可读的当日流水号，如 T-20261013-004。

    演示用，够直观即可；并发冲突风险由 UNIQUE 主键兜底。
    """
    day = datetime.now().strftime("%Y%m%d")
    like = f"{prefix}-{day}-%"
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM {table} WHERE id LIKE ?", (like,)
    ).fetchone()
    seq = (row["n"] if row else 0) + 1
    return f"{prefix}-{day}-{seq:03d}"


def table_counts() -> dict[str, int]:
    """自检用：看各表有多少行。"""
    tables = ["robot", "point", "route", "task", "patrol_run", "detection", "alert", "message"]
    out: dict[str, int] = {}
    with connect() as conn:
        for t in tables:
            try:
                out[t] = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            except sqlite3.OperationalError:
                out[t] = -1
    return out
