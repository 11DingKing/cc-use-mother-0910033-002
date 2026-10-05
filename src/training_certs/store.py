"""SQLite 持久化：仅追加账本与并发唯一约束。"""
from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from .events import GENESIS_HASH, canonical, event_hash, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     TEXT NOT NULL UNIQUE,
    event_type   TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload      TEXT NOT NULL,
    recorded_at  TEXT NOT NULL,
    prev_hash    TEXT NOT NULL,
    hash         TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS certificates (
    cert_no      TEXT PRIMARY KEY,
    volunteer_id TEXT NOT NULL,
    topic        TEXT NOT NULL,
    state        TEXT NOT NULL CHECK (state IN ('issued', 'suspended', 'revoked')),
    issued_on    TEXT NOT NULL
);

-- 同一志愿者同一主题至多一张有效（签发/暂停）证书；撤销后腾出名额。
CREATE UNIQUE INDEX IF NOT EXISTS ux_certificate_active
ON certificates(volunteer_id, topic)
WHERE state IN ('issued', 'suspended');

-- 同一单元的第 N 次考试只能有一条成绩，防止并发重复记录。
CREATE TABLE IF NOT EXISTS score_attempts (
    volunteer_id TEXT NOT NULL,
    unit_code    TEXT NOT NULL,
    attempt      INTEGER NOT NULL,
    score_id     TEXT NOT NULL,
    PRIMARY KEY (volunteer_id, unit_code, attempt)
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """打开数据库连接并确保 schema 就绪。"""
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    return conn


def append_event(
    conn: sqlite3.Connection,
    event_type: str,
    aggregate_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """在账本尾部追加事件并计算哈希；必须在写事务内调用。"""
    row = conn.execute("SELECT hash FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
    prev_hash = row["hash"] if row else GENESIS_HASH
    event_id = uuid.uuid4().hex
    recorded_at = now_iso()
    digest = event_hash(prev_hash, event_id, event_type, aggregate_id, recorded_at, payload)
    conn.execute(
        "INSERT INTO ledger "
        "(event_id, event_type, aggregate_id, payload, recorded_at, prev_hash, hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            event_type,
            aggregate_id,
            canonical(payload),
            recorded_at,
            prev_hash,
            digest,
        ),
    )
    return {
        "event_id": event_id,
        "recorded_at": recorded_at,
        "prev_hash": prev_hash,
        "hash": digest,
    }
