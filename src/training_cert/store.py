"""SQLite 持久层：表结构、连接管理、链事件追加。

所有业务表均保留业务时间（created_at）与审计字段；易变状态
（补考资格剩余次数、证书状态）独立存放，但其每次变化都同步追加
immutable_event 链事件，因此当前状态只是链的投影，可随时重放校验。
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS paper_version (
    id              TEXT PRIMARY KEY,
    topic_code      TEXT NOT NULL,
    unit_code       TEXT NOT NULL,
    version_tag     TEXT NOT NULL,
    pass_mark       INTEGER NOT NULL DEFAULT 60,
    published_at    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE(topic_code, unit_code, version_tag)
);

CREATE TABLE IF NOT EXISTS unit_score (
    id              TEXT PRIMARY KEY,
    volunteer_id    TEXT NOT NULL,
    topic_code      TEXT NOT NULL,
    unit_code       TEXT NOT NULL,
    paper_id        TEXT NOT NULL REFERENCES paper_version(id),
    exam_date       TEXT NOT NULL,
    attempt_no      INTEGER NOT NULL,
    score           INTEGER NOT NULL,
    pass            INTEGER NOT NULL,
    examiner_id     TEXT NOT NULL,
    recorded_at     TEXT NOT NULL,
    -- 撤销不删除行：仅置位并记录撤销事件与生效日
    revoked         INTEGER NOT NULL DEFAULT 0,
    revoked_at      TEXT,
    UNIQUE(volunteer_id, topic_code, unit_code, attempt_no)
);
CREATE INDEX IF NOT EXISTS idx_unit_score_lookup
    ON unit_score(volunteer_id, topic_code, unit_code);

CREATE TABLE IF NOT EXISTS retake_eligibility (
    id              TEXT PRIMARY KEY,
    volunteer_id    TEXT NOT NULL,
    topic_code      TEXT NOT NULL,
    unit_code       TEXT NOT NULL,
    max_attempts    INTEGER NOT NULL,
    remaining       INTEGER NOT NULL,
    reason          TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(volunteer_id, topic_code, unit_code)
);

-- 单元替代：effective_from 起，old_unit 由 new_unit 满足（同主题）
CREATE TABLE IF NOT EXISTS unit_substitution (
    id              TEXT PRIMARY KEY,
    topic_code      TEXT NOT NULL,
    old_unit        TEXT NOT NULL,
    new_unit        TEXT NOT NULL,
    effective_from  TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE(topic_code, old_unit, new_unit, effective_from)
);

CREATE TABLE IF NOT EXISTS certificate (
    id              TEXT PRIMARY KEY,
    volunteer_id    TEXT NOT NULL,
    topic_code      TEXT NOT NULL,
    issue_date      TEXT NOT NULL,
    status          TEXT NOT NULL CHECK(status IN ('active','suspended')),
    fingerprint     TEXT NOT NULL,
    manifest_hash   TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    suspended_at    TEXT,
    suspension_reason TEXT,
    resume_seq      INTEGER,
    UNIQUE(volunteer_id, fingerprint)
);

-- 证书中的逐条单元声明；证据组合在发证瞬间固定
CREATE TABLE IF NOT EXISTS certificate_claim (
    id              TEXT PRIMARY KEY,
    certificate_id  TEXT NOT NULL REFERENCES certificate(id),
    topic_code      TEXT NOT NULL,
    unit_code       TEXT NOT NULL,
    basis           TEXT NOT NULL CHECK(basis IN ('first_pass','retake_pass','substituted')),
    score_id        TEXT REFERENCES unit_score(id),
    substituted_from TEXT,
    paper_id        TEXT REFERENCES paper_version(id),
    examiner_id     TEXT,
    score           INTEGER,
    pass_date       TEXT NOT NULL,
    valid_from      TEXT NOT NULL,
    valid_until     TEXT NOT NULL,
    evidence_hash   TEXT NOT NULL,
    claim_seq       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claim_cert ON certificate_claim(certificate_id);

-- 不可变事件链：任何业务状态变化追加一行，绝不 UPDATE/DELETE
CREATE TABLE IF NOT EXISTS immutable_event (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type   TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    prev_hash    TEXT NOT NULL,
    hash         TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS idempotency (
    idem_key     TEXT PRIMARY KEY,
    response_json TEXT NOT NULL
);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Store:
    """线程安全的 SQLite 封装。

    一把进程内 RLock 串行化全部写事务（SQLite 本身亦会串行化写），
    配合证书表 UNIQUE 指纹约束，共同防止并发重复发证。
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self._path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
