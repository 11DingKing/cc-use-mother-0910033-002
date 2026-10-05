"""哈希与日期工具：规范 JSON、证据哈希、不可变链哈希。"""
from __future__ import annotations

import calendar
import hashlib
import json
from datetime import date, datetime

GENESIS_HASH = "0" * 64


def canonical(payload: object) -> str:
    """对任意 JSON 可序列化对象生成稳定的规范字符串。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def event_hash(
    seq: int,
    event_type: str,
    aggregate_id: str,
    payload: object,
    created_at: str,
    prev_hash: str,
) -> str:
    """计算单条链事件哈希：序号与类型也参与摘要，防止重排/改类。"""
    joined = "\n".join(
        [str(seq), event_type, aggregate_id, canonical(payload), created_at, prev_hash]
    )
    return sha256_text(joined)


def parse_day(value: str | date | datetime | None) -> date | None:
    """接受 YYYY-MM-DD 或完整 ISO 时间，统一归一化为日期。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if len(text) >= 10:
        return date.fromisoformat(text[:10])
    return date.fromisoformat(text)


def day_str(value: str | date | datetime | None) -> str | None:
    day = parse_day(value)
    return day.isoformat() if day else None


def add_months(day: date, months: int) -> date:
    """月份加减，月末溢出时收敛到目标月最后一天。"""
    total = (day.month - 1) + months
    year = day.year + total // 12
    month = total % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))
