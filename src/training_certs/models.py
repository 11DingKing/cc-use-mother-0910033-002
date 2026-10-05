"""事件回放（reducer）：把仅追加账本归约成当前领域状态。

状态中保留每个单元的全部考试尝试，使证书在任意日期的
"首考通过 / 补考通过 / 替代单元通过" 都可以从历史重建。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from .events import parse_date


@dataclass
class Attempt:
    score_id: str
    attempt_no: int
    unit_code: str
    paper_id: str
    paper_version: int
    score: float
    passed: bool
    kind: str  # first | retake
    examiner_id: str
    exam_date: date
    recorded_seq: int
    revoked: bool = False
    revoke_reason: Optional[str] = None
    revoked_by: Optional[str] = None


@dataclass
class Eligibility:
    max_retakes: int
    granted_by: str
    reason: str


@dataclass
class Suspension:
    from_date: date
    reason: str
    resume_date: Optional[date] = None
    resume_reason: Optional[str] = None


@dataclass
class Certificate:
    cert_no: str
    volunteer_id: str
    topic: str
    issued_on: date
    expires_on: date
    validity_years: int
    issued_by: str
    declaration: dict[str, Any]
    state: str = "issued"  # issued | suspended | revoked
    suspend_reason: Optional[str] = None
    suspensions: list[Suspension] = field(default_factory=list)
    revoke_reason: Optional[str] = None
    revoked_by: Optional[str] = None
    revoked_on: Optional[date] = None
    anchor_seq: Optional[int] = None
    anchor_hash: Optional[str] = None


@dataclass
class AppState:
    papers: dict[str, dict[str, Any]] = field(default_factory=dict)
    units: dict[str, dict[str, Any]] = field(default_factory=dict)
    volunteers: dict[str, dict[str, Any]] = field(default_factory=dict)
    # (volunteer, unit) -> 按时间排列的尝试
    attempts: dict[tuple[str, str], list[Attempt]] = field(default_factory=dict)
    eligibility: dict[tuple[str, str], Eligibility] = field(default_factory=dict)
    # (topic, old_unit) -> (new_unit, effective_date)
    replacements: dict[tuple[str, str], tuple[str, date]] = field(default_factory=dict)
    certificates: dict[str, Certificate] = field(default_factory=dict)
    # (volunteer, topic) -> cert_no，仅 issued/suspended
    active_certs: dict[tuple[str, str], str] = field(default_factory=dict)
    score_index: dict[str, tuple[str, str]] = field(default_factory=dict)
    last_seq: int = 0

    # ---- 查询辅助 ----

    def topic_units(self, topic: str) -> list[str]:
        return sorted(code for code, u in self.units.items() if u["topic"] == topic)

    def retakes_used(self, volunteer_id: str, unit_code: str) -> int:
        used = 0
        for attempt in self.attempts.get((volunteer_id, unit_code), []):
            if attempt.kind == "retake":
                used += 1  # 撤销的成绩仍然占用一次补考机会
        return used

    def retake_remaining(self, volunteer_id: str, unit_code: str) -> Optional[int]:
        grant = self.eligibility.get((volunteer_id, unit_code))
        if grant is None:
            return None
        return max(0, grant.max_retakes - self.retakes_used(volunteer_id, unit_code))


def _payload(row: sqlite3.Row) -> dict[str, Any]:
    return json.loads(row["payload"])


def rebuild(conn: sqlite3.Connection) -> AppState:
    """顺序回放全部账本事件，重建内存状态。"""
    state = AppState()
    rows = conn.execute(
        "SELECT seq, event_id, event_type, aggregate_id, payload, hash "
        "FROM ledger ORDER BY seq"
    ).fetchall()
    for row in rows:
        state.last_seq = row["seq"]
        _apply(state, row["event_type"], row["aggregate_id"], _payload(row), row["seq"], row["hash"])
    return state


def _apply(
    state: AppState,
    event_type: str,
    aggregate_id: str,
    p: dict[str, Any],
    seq: int,
    digest: str,
) -> None:
    if event_type == "VolunteerRegistered":
        state.volunteers[aggregate_id] = {"volunteer_id": aggregate_id, "name": p.get("name", "")}

    elif event_type == "UnitRegistered":
        state.units[aggregate_id] = {
            "unit_code": aggregate_id,
            "topic": p["topic"],
            "title": p.get("title", ""),
        }

    elif event_type == "PaperVersionRegistered":
        state.papers[aggregate_id] = {
            "paper_id": aggregate_id,
            "unit_code": p["unit_code"],
            "topic": p["topic"],
            "version": p["version"],
            "published_at": p["published_at"],
            "created_by": p["created_by"],
            "pass_score": p["pass_score"],
            "content_ref": p.get("content_ref"),
        }

    elif event_type == "ScoreRecorded":
        attempt = Attempt(
            score_id=aggregate_id,
            attempt_no=p["attempt_no"],
            unit_code=p["unit_code"],
            paper_id=p["paper_id"],
            paper_version=p["paper_version"],
            score=p["score"],
            passed=p["passed"],
            kind=p["kind"],
            examiner_id=p["examiner_id"],
            exam_date=parse_date(p["exam_date"], "考试日期"),
            recorded_seq=seq,
        )
        key = (p["volunteer_id"], p["unit_code"])
        state.attempts.setdefault(key, []).append(attempt)
        state.score_index[aggregate_id] = key

    elif event_type == "ScoreRevoked":
        key = state.score_index.get(aggregate_id)
        if key is not None:
            for attempt in state.attempts[key]:
                if attempt.score_id == aggregate_id:
                    attempt.revoked = True
                    attempt.revoke_reason = p["reason"]
                    attempt.revoked_by = p["by"]
                    break

    elif event_type == "RetakeEligibilityGranted":
        state.eligibility[(p["volunteer_id"], p["unit_code"])] = Eligibility(
            max_retakes=p["max_retakes"],
            granted_by=p["by"],
            reason=p.get("reason", ""),
        )

    elif event_type == "UnitReplaced":
        state.replacements[(p["topic"], p["old_unit"])] = (
            p["new_unit"],
            parse_date(p["effective_date"], "生效日期"),
        )

    elif event_type == "CertificateIssued":
        cert = Certificate(
            cert_no=aggregate_id,
            volunteer_id=p["volunteer_id"],
            topic=p["topic"],
            issued_on=parse_date(p["issued_on"], "签发日期"),
            expires_on=parse_date(p["expires_on"], "到期日期"),
            validity_years=p["validity_years"],
            issued_by=p["issued_by"],
            declaration=p["declaration"],
            anchor_seq=seq,
            anchor_hash=digest,
        )
        state.certificates[aggregate_id] = cert
        state.active_certs[(cert.volunteer_id, cert.topic)] = cert.cert_no

    elif event_type == "CertificateSuspended":
        cert = state.certificates[aggregate_id]
        cert.state = "suspended"
        cert.suspend_reason = p["reason"]
        cert.suspensions.append(
            Suspension(from_date=parse_date(p["from_date"], "暂停日期"), reason=p["reason"])
        )

    elif event_type == "CertificateResumed":
        cert = state.certificates[aggregate_id]
        cert.state = "issued"
        cert.suspend_reason = None
        cert.suspensions[-1].resume_date = parse_date(p["resume_date"], "恢复日期")
        cert.suspensions[-1].resume_reason = p.get("reason", "")

    elif event_type == "CertificateRevoked":
        cert = state.certificates[aggregate_id]
        cert.state = "revoked"
        cert.revoke_reason = p["reason"]
        cert.revoked_by = p["by"]
        cert.revoked_on = parse_date(p["on_date"], "撤销日期")
        state.active_certs.pop((cert.volunteer_id, cert.topic), None)

    else:
        raise ValueError(f"未知事件类型：{event_type}")
