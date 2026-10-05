"""不可变事件链：追加与重放校验。"""
from __future__ import annotations

from .hashing import GENESIS_HASH, canonical, event_hash
from .store import Store, utc_now_iso


def append_event(
    store: Store,
    event_type: str,
    aggregate_id: str,
    payload: dict,
    created_at: str | None = None,
) -> dict:
    """在写事务内追加链事件（调用方须持有 store.lock）。

    哈希依赖前一条哈希、序号、类型、聚合 ID、规范载荷与时间戳，
    任何篡改都会在 verify_chain 中暴露。
    """
    ts = created_at or utc_now_iso()
    last = store.query_one("SELECT seq, hash FROM immutable_event ORDER BY seq DESC LIMIT 1")
    seq = (last["seq"] + 1) if last else 1
    prev_hash = last["hash"] if last else GENESIS_HASH
    digest = event_hash(seq, event_type, aggregate_id, payload, ts, prev_hash)
    store.execute(
        "INSERT INTO immutable_event(seq, event_type, aggregate_id, payload_json,"
        " created_at, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
        (seq, event_type, aggregate_id, canonical(payload), ts, prev_hash, digest),
    )
    return {"seq": seq, "hash": digest, "prev_hash": prev_hash, "created_at": ts}


def verify_chain(store: Store) -> dict:
    """顺序重放全链，校验 prev_hash 衔接与每节点哈希。

    返回校验报告；发现断点时给出第一个失配位置，不抛异常，
    以便监控接口直接透传结果。
    """
    rows = store.query_all(
        "SELECT seq, event_type, aggregate_id, payload_json, created_at,"
        " prev_hash, hash FROM immutable_event ORDER BY seq"
    )
    expected_prev = GENESIS_HASH
    for row in rows:
        if row["prev_hash"] != expected_prev:
            return {
                "ok": False,
                "checked": row["seq"] - 1,
                "total": len(rows),
                "broken_at": row["seq"],
                "reason": "prev_hash 断链",
            }
        digest = event_hash(
            row["seq"],
            row["event_type"],
            row["aggregate_id"],
            canonical_json_load(row["payload_json"]),
            row["created_at"],
            row["prev_hash"],
        )
        if digest != row["hash"]:
            return {
                "ok": False,
                "checked": row["seq"] - 1,
                "total": len(rows),
                "broken_at": row["seq"],
                "reason": "内容哈希不匹配",
            }
        expected_prev = row["hash"]
    seqs = [row["seq"] for row in rows]
    if seqs and seqs != list(range(1, len(rows) + 1)):
        return {"ok": False, "checked": 0, "total": len(rows), "broken_at": None, "reason": "序号不连续"}
    return {"ok": True, "checked": len(rows), "total": len(rows), "broken_at": None, "reason": None}


def canonical_json_load(value: str):
    import json

    return json.loads(value)
