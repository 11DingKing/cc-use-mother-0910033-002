"""事件哈希、规范化编码与日期工具。"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any

from .errors import DomainError

GENESIS_HASH = "0" * 64


def canonical(obj: Any) -> str:
    """确定性 JSON 编码：键排序、无空白，供哈希签名使用。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def event_hash(
    prev_hash: str,
    event_id: str,
    event_type: str,
    aggregate_id: str,
    recorded_at: str,
    payload: dict,
) -> str:
    """计算事件哈希，输入包含前一事件哈希，形成链式结构。"""
    body = canonical(
        {
            "prev_hash": prev_hash,
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_id": aggregate_id,
            "recorded_at": recorded_at,
            "payload": payload,
        }
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def parse_date(value: str, field: str = "日期") -> date:
    """严格解析 YYYY-MM-DD，拒绝 2026-1-1 之类的非规范写法。"""
    if not isinstance(value, str):
        raise DomainError(f"{field}必须是 YYYY-MM-DD 字符串")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise DomainError(f"{field}必须是 YYYY-MM-DD 格式") from exc
    if parsed.isoformat() != value:
        raise DomainError(f"{field}必须是 YYYY-MM-DD 格式")
    return parsed


def add_years(value: date, years: int) -> date:
    """日期加整年，2 月 29 日在平年回落到 2 月 28 日。"""
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(year=value.year + years, day=28)
