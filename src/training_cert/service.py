"""领域服务：成绩只追加、补考配额、单元替代、发证与验证。

所有写操作在 store.lock + BEGIN IMMEDIATE 事务内完成，并同步向
不可变链追加事件；发证时把每条声明的证据组合哈希固定到证书上。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date

from .events import append_event, verify_chain
from .hashing import add_months, canonical, day_str, parse_day, sha256_text
from .store import Store, utc_now_iso

DEFAULT_VALIDITY_MONTHS = 24


class DomainError(Exception):
    """业务规则违反；status 供 HTTP 层映射。"""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class CertService:
    def __init__(self, store: Store) -> None:
        self.store = store

    # ------------------------------------------------------------------ #
    # 试卷版本
    # ------------------------------------------------------------------ #
    def create_paper_version(
        self,
        topic_code: str,
        unit_code: str,
        version_tag: str,
        published_at: str | date,
        pass_mark: int = 60,
    ) -> dict:
        published = day_str(published_at)
        paper_id = _new_id("paper")
        ts = utc_now_iso()
        with self.store.lock:
            self.store.execute("BEGIN IMMEDIATE")
            try:
                dup = self.store.query_one(
                    "SELECT id FROM paper_version WHERE topic_code=? AND unit_code=? AND version_tag=?",
                    (topic_code, unit_code, version_tag),
                )
                if dup:
                    raise DomainError("试卷版本已存在", 409)
                self.store.execute(
                    "INSERT INTO paper_version(id, topic_code, unit_code, version_tag,"
                    " pass_mark, published_at, created_at) VALUES (?,?,?,?,?,?,?)",
                    (paper_id, topic_code, unit_code, version_tag, pass_mark, published, ts),
                )
                evt = append_event(
                    self.store,
                    "paper_published",
                    paper_id,
                    {
                        "topic_code": topic_code,
                        "unit_code": unit_code,
                        "version_tag": version_tag,
                        "pass_mark": pass_mark,
                        "published_at": published,
                    },
                    ts,
                )
                self.store.execute("COMMIT")
            except Exception:
                self.store.execute("ROLLBACK")
                raise
        return {"paper_id": paper_id, "event_seq": evt["seq"], "published_at": published}

    def list_paper_versions(self, topic_code: str) -> list[dict]:
        rows = self.store.query_all(
            "SELECT * FROM paper_version WHERE topic_code=? ORDER BY unit_code, published_at",
            (topic_code,),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 单元成绩（只追加，不覆盖旧结果）
    # ------------------------------------------------------------------ #
    def record_score(
        self,
        volunteer_id: str,
        topic_code: str,
        unit_code: str,
        paper_id: str,
        examiner_id: str,
        score: int,
        exam_date: str | date,
        override_pass: bool | None = None,
    ) -> dict:
        exam_day = day_str(exam_date)
        ts = utc_now_iso()
        with self.store.lock:
            self.store.execute("BEGIN IMMEDIATE")
            try:
                paper = self.store.query_one(
                    "SELECT * FROM paper_version WHERE id=?", (paper_id,)
                )
                if paper is None:
                    raise DomainError("试卷版本不存在", 404)
                if paper["topic_code"] != topic_code or paper["unit_code"] != unit_code:
                    raise DomainError("试卷与主题/单元不匹配")
                prior = self.store.query_all(
                    "SELECT id FROM unit_score WHERE volunteer_id=? AND topic_code=? AND unit_code=?",
                    (volunteer_id, topic_code, unit_code),
                )
                attempt_no = len(prior) + 1

                remaining = None
                if attempt_no > 1:
                    elig = self.store.query_one(
                        "SELECT * FROM retake_eligibility WHERE volunteer_id=? AND topic_code=? AND unit_code=?",
                        (volunteer_id, topic_code, unit_code),
                    )
                    if elig is None:
                        raise DomainError(f"单元 {unit_code} 无补考资格，不能记录第 {attempt_no} 次成绩")
                    if elig["remaining"] <= 0:
                        raise DomainError(f"单元 {unit_code} 补考次数已用尽")
                    remaining = elig["remaining"] - 1
                    self.store.execute(
                        "UPDATE retake_eligibility SET remaining=?, updated_at=? WHERE id=?",
                        (remaining, ts, elig["id"]),
                    )

                passed = (score >= paper["pass_mark"]) if override_pass is None else override_pass
                score_id = _new_id("score")
                self.store.execute(
                    "INSERT INTO unit_score(id, volunteer_id, topic_code, unit_code, paper_id,"
                    " exam_date, attempt_no, score, pass, examiner_id, recorded_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        score_id, volunteer_id, topic_code, unit_code, paper_id,
                        exam_day, attempt_no, score, int(passed), examiner_id, ts,
                    ),
                )
                evt = append_event(
                    self.store,
                    "score_recorded",
                    score_id,
                    {
                        "volunteer_id": volunteer_id,
                        "topic_code": topic_code,
                        "unit_code": unit_code,
                        "paper_id": paper_id,
                        "paper_version_tag": paper["version_tag"],
                        "attempt_no": attempt_no,
                        "score": score,
                        "pass": passed,
                        "examiner_id": examiner_id,
                        "exam_date": exam_day,
                        "retake_remaining_after": remaining,
                    },
                    ts,
                )
                self.store.execute("COMMIT")
            except Exception:
                self.store.execute("ROLLBACK")
                raise
        return {
            "score_id": score_id,
            "attempt_no": attempt_no,
            "pass": passed,
            "retake_remaining": remaining,
            "event_seq": evt["seq"],
        }

    def revoke_score(
        self,
        score_id: str,
        reason: str,
        revoked_on: str | date | None = None,
    ) -> dict:
        revoked_day = day_str(revoked_on) or date.today().isoformat()
        ts = utc_now_iso()
        with self.store.lock:
            self.store.execute("BEGIN IMMEDIATE")
            try:
                row = self.store.query_one("SELECT * FROM unit_score WHERE id=?", (score_id,))
                if row is None:
                    raise DomainError("成绩不存在", 404)
                if row["revoked"]:
                    raise DomainError("成绩已撤销，不能重复撤销", 409)
                self.store.execute(
                    "UPDATE unit_score SET revoked=1, revoked_at=? WHERE id=?",
                    (revoked_day, score_id),
                )
                evt = append_event(
                    self.store,
                    "score_revoked",
                    score_id,
                    {
                        "volunteer_id": row["volunteer_id"],
                        "topic_code": row["topic_code"],
                        "unit_code": row["unit_code"],
                        "attempt_no": row["attempt_no"],
                        "reason": reason,
                        "revoked_on": revoked_day,
                    },
                    ts,
                )
                self.store.execute("COMMIT")
            except Exception:
                self.store.execute("ROLLBACK")
                raise
        return {"score_id": score_id, "revoked_on": revoked_day, "event_seq": evt["seq"]}

    def score_history(self, volunteer_id: str, topic_code: str, unit_code: str) -> list[dict]:
        rows = self.store.query_all(
            "SELECT s.*, p.version_tag AS paper_version_tag, p.pass_mark"
            " FROM unit_score s JOIN paper_version p ON p.id = s.paper_id"
            " WHERE s.volunteer_id=? AND s.topic_code=? AND s.unit_code=?"
            " ORDER BY s.attempt_no",
            (volunteer_id, topic_code, unit_code),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 补考资格（次数限制）
    # ------------------------------------------------------------------ #
    def grant_retake_eligibility(
        self,
        volunteer_id: str,
        topic_code: str,
        unit_code: str,
        max_extra_attempts: int,
        reason: str = "",
    ) -> dict:
        if max_extra_attempts <= 0:
            raise DomainError("额外补考次数必须大于 0")
        ts = utc_now_iso()
        elig_id = _new_id("elig")
        with self.store.lock:
            self.store.execute("BEGIN IMMEDIATE")
            try:
                existing = self.store.query_one(
                    "SELECT id, remaining FROM retake_eligibility"
                    " WHERE volunteer_id=? AND topic_code=? AND unit_code=?",
                    (volunteer_id, topic_code, unit_code),
                )
                if existing:
                    # 追加配额，而非覆盖：旧资格历史保留在链上
                    new_remaining = existing["remaining"] + max_extra_attempts
                    self.store.execute(
                        "UPDATE retake_eligibility SET remaining=?, updated_at=? WHERE id=?",
                        (new_remaining, ts, existing["id"]),
                    )
                    target_id = existing["id"]
                else:
                    self.store.execute(
                        "INSERT INTO retake_eligibility(id, volunteer_id, topic_code, unit_code,"
                        " max_attempts, remaining, reason, created_at, updated_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            elig_id, volunteer_id, topic_code, unit_code,
                            max_extra_attempts + 1, max_extra_attempts, reason, ts, ts,
                        ),
                    )
                    target_id = elig_id
                evt = append_event(
                    self.store,
                    "retake_granted",
                    target_id,
                    {
                        "volunteer_id": volunteer_id,
                        "topic_code": topic_code,
                        "unit_code": unit_code,
                        "extra_attempts": max_extra_attempts,
                        "reason": reason,
                    },
                    ts,
                )
                self.store.execute("COMMIT")
            except Exception:
                self.store.execute("ROLLBACK")
                raise
        return {"eligibility_id": target_id, "event_seq": evt["seq"]}

    # ------------------------------------------------------------------ #
    # 单元替代
    # ------------------------------------------------------------------ #
    def define_substitution(
        self,
        topic_code: str,
        old_unit: str,
        new_unit: str,
        effective_from: str | date,
    ) -> dict:
        if old_unit == new_unit:
            raise DomainError("替代单元不能与原单元相同")
        eff = day_str(effective_from)
        sub_id = _new_id("sub")
        ts = utc_now_iso()
        with self.store.lock:
            self.store.execute("BEGIN IMMEDIATE")
            try:
                self.store.execute(
                    "INSERT INTO unit_substitution(id, topic_code, old_unit, new_unit,"
                    " effective_from, created_at) VALUES (?,?,?,?,?,?)",
                    (sub_id, topic_code, old_unit, new_unit, eff, ts),
                )
                evt = append_event(
                    self.store,
                    "substitution_defined",
                    sub_id,
                    {
                        "topic_code": topic_code,
                        "old_unit": old_unit,
                        "new_unit": new_unit,
                        "effective_from": eff,
                    },
                    ts,
                )
                self.store.execute("COMMIT")
            except sqlite3.IntegrityError:
                self.store.execute("ROLLBACK")
                raise DomainError("该替代规则已存在", 409)
            except Exception:
                self.store.execute("ROLLBACK")
                raise
        return {"substitution_id": sub_id, "event_seq": evt["seq"]}

    # ------------------------------------------------------------------ #
    # 发证：固定证据组合
    # ------------------------------------------------------------------ #
    def _latest_passing_score(self, volunteer_id: str, topic_code: str, unit_code: str):
        return self.store.query_one(
            "SELECT s.*, p.version_tag AS paper_version_tag, p.pass_mark"
            " FROM unit_score s JOIN paper_version p ON p.id = s.paper_id"
            " WHERE s.volunteer_id=? AND s.topic_code=? AND s.unit_code=? AND s.pass=1"
            " ORDER BY s.attempt_no DESC LIMIT 1",
            (volunteer_id, topic_code, unit_code),
        )

    def _resolve_claim(
        self, volunteer_id: str, topic_code: str, unit_code: str, issue_day: str,
        validity_months: int,
    ) -> dict:
        score = self._latest_passing_score(volunteer_id, topic_code, unit_code)
        if score is not None:
            basis = "first_pass" if score["attempt_no"] == 1 else "retake_pass"
            return self._build_claim(unit_code, basis, score, None, validity_months)

        # 本单元无直接通过成绩：沿生效替代链追溯（同日发证只看生效日 <= 发证日）
        substituted_evidence = self._resolve_via_substitution(
            volunteer_id, topic_code, unit_code, issue_day, validity_months, set()
        )
        if substituted_evidence is not None:
            return substituted_evidence
        raise DomainError(f"单元 {unit_code} 缺少有效通过证据（含替代），不能发证")

    def _resolve_via_substitution(
        self, volunteer_id: str, topic_code: str, unit_code: str, issue_day: str,
        validity_months: int, visited: set[str],
    ) -> dict | None:
        if unit_code in visited:
            return None  # 替代环，放弃此路径
        visited = visited | {unit_code}
        subs = self.store.query_all(
            "SELECT * FROM unit_substitution WHERE topic_code=? AND old_unit=?"
            " AND effective_from<=? ORDER BY effective_from DESC, new_unit",
            (topic_code, unit_code, issue_day),
        )
        for sub in subs:
            alt = self._latest_passing_score(volunteer_id, topic_code, sub["new_unit"])
            if alt is not None:
                return self._build_claim(
                    unit_code, "substituted", alt, sub["new_unit"], validity_months
                )
            # 传递替代：新单元本身也可由其他单元满足
            nested = self._resolve_via_substitution(
                volunteer_id, topic_code, sub["new_unit"], issue_day,
                validity_months, visited,
            )
            if nested is not None:
                # 声明仍针对最初请求的单元 unit_code，直接来源是 new_unit
                nested["unit_code"] = unit_code
                nested["substituted_from"] = sub["new_unit"]
                nested.pop("evidence_hash")  # 重算时不能把旧哈希纳入自身载荷
                nested["evidence_hash"] = sha256_text(canonical(nested))
                return nested
        return None

    @staticmethod
    def _build_claim(
        unit_code: str, basis: str, score, substituted_from: str | None,
        validity_months: int,
    ) -> dict:
        pass_day = parse_day(score["exam_date"])
        valid_from = pass_day.isoformat()
        valid_until = add_months(pass_day, validity_months).isoformat()
        evidence = {
            "unit_code": unit_code,
            "basis": basis,
            "score_id": score["id"],
            "attempt_no": score["attempt_no"],
            "paper_id": score["paper_id"],
            "paper_version_tag": score["paper_version_tag"],
            "examiner_id": score["examiner_id"],
            "score": score["score"],
            "pass": bool(score["pass"]),
            "exam_date": score["exam_date"],
            "pass_date": score["exam_date"],
            "substituted_from": substituted_from,
            "valid_from": valid_from,
            "valid_until": valid_until,
        }
        evidence["evidence_hash"] = sha256_text(canonical(evidence))
        return evidence

    def issue_certificate(
        self,
        volunteer_id: str,
        topic_code: str,
        units: list[str] | None = None,
        issue_date: str | date | None = None,
        validity_months: int = DEFAULT_VALIDITY_MONTHS,
        idem_key: str | None = None,
    ) -> dict:
        issue_day = day_str(issue_date) or date.today().isoformat()
        if validity_months <= 0:
            raise DomainError("有效期月数必须大于 0")

        with self.store.lock:
            self.store.execute("BEGIN IMMEDIATE")
            try:
                # 幂等检查置于发证事务内：并发同键请求只会有一个真正发证
                if idem_key:
                    cached = self.store.query_one(
                        "SELECT response_json FROM idempotency WHERE idem_key=?", (idem_key,)
                    )
                    if cached:
                        self.store.execute("ROLLBACK")
                        return json.loads(cached["response_json"])

                if units is None:
                    rows = self.store.query_all(
                        "SELECT DISTINCT unit_code FROM paper_version WHERE topic_code=?"
                        " ORDER BY unit_code",
                        (topic_code,),
                    )
                    units = [r["unit_code"] for r in rows]
                if not units:
                    raise DomainError("证书至少声明一个单元")
                units = sorted(dict.fromkeys(units))

                claims = [
                    self._resolve_claim(volunteer_id, topic_code, u, issue_day, validity_months)
                    for u in units
                ]

                cert_id = _new_id("cert")
                ts = utc_now_iso()
                # 业务指纹：同志愿者+同主题+同证据组合+同发证日 只允许一证
                fingerprint = sha256_text(
                    canonical(
                        {
                            "volunteer_id": volunteer_id,
                            "topic_code": topic_code,
                            "issue_date": issue_day,
                            "claims": [c["evidence_hash"] for c in claims],
                        }
                    )
                )
                manifest = {
                    "certificate_id": cert_id,
                    "volunteer_id": volunteer_id,
                    "topic_code": topic_code,
                    "issue_date": issue_day,
                    "validity_months": validity_months,
                    "claims": [
                        {"seq": i + 1, "unit_code": c["unit_code"], "evidence_hash": c["evidence_hash"]}
                        for i, c in enumerate(claims)
                    ],
                    "created_at": ts,
                }
                manifest_hash = sha256_text(canonical(manifest))

                try:
                    self.store.execute(
                        "INSERT INTO certificate(id, volunteer_id, topic_code, issue_date,"
                        " status, fingerprint, manifest_hash, created_at)"
                        " VALUES (?,?,?,?, 'active', ?, ?, ?)",
                        (cert_id, volunteer_id, topic_code, issue_day, fingerprint, manifest_hash, ts),
                    )
                except sqlite3.IntegrityError:
                    # 并发或重复发证：绝不发出第二张；回滚后返回已有证书
                    self.store.execute("ROLLBACK")
                    return self._duplicate_result(volunteer_id, fingerprint)

                for i, c in enumerate(claims):
                    self.store.execute(
                        "INSERT INTO certificate_claim(id, certificate_id, topic_code, unit_code,"
                        " basis, score_id, substituted_from, paper_id, examiner_id, score,"
                        " pass_date, valid_from, valid_until, evidence_hash, claim_seq)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            _new_id("clm"), cert_id, topic_code, c["unit_code"], c["basis"],
                            c["score_id"], c["substituted_from"], c["paper_id"], c["examiner_id"],
                            c["score"], c["pass_date"], c["valid_from"], c["valid_until"],
                            c["evidence_hash"], i + 1,
                        ),
                    )

                evt = append_event(
                    self.store,
                    "certificate_issued",
                    cert_id,
                    {
                        "volunteer_id": volunteer_id,
                        "topic_code": topic_code,
                        "issue_date": issue_day,
                        "fingerprint": fingerprint,
                        "manifest_hash": manifest_hash,
                        "manifest": manifest,
                    },
                    ts,
                )

                result = {
                    "certificate_id": cert_id,
                    "volunteer_id": volunteer_id,
                    "topic_code": topic_code,
                    "issue_date": issue_day,
                    "fingerprint": fingerprint,
                    "manifest_hash": manifest_hash,
                    "claims": [
                        {
                            "unit_code": c["unit_code"],
                            "basis": c["basis"],
                            "valid_from": c["valid_from"],
                            "valid_until": c["valid_until"],
                            "evidence_hash": c["evidence_hash"],
                        }
                        for c in claims
                    ],
                    "event_seq": evt["seq"],
                    "duplicate": False,
                }
                if idem_key:
                    self.store.execute(
                        "INSERT OR IGNORE INTO idempotency(idem_key, response_json) VALUES (?,?)",
                        (idem_key, canonical(result)),
                    )
                self.store.execute("COMMIT")
            except Exception:
                self.store.execute("ROLLBACK")
                raise

        return result

    def _duplicate_result(self, volunteer_id: str, fingerprint: str) -> dict:
        """相同证据组合已发证时，回填既有证书作为幂等重复响应。"""
        cert = self.store.query_one(
            "SELECT * FROM certificate WHERE volunteer_id=? AND fingerprint=?",
            (volunteer_id, fingerprint),
        )
        claims = self.store.query_all(
            "SELECT unit_code, basis, valid_from, valid_until, evidence_hash"
            " FROM certificate_claim WHERE certificate_id=? ORDER BY claim_seq",
            (cert["id"],),
        ) if cert else []
        return {
            "certificate_id": cert["id"] if cert else None,
            "volunteer_id": volunteer_id,
            "topic_code": cert["topic_code"] if cert else None,
            "issue_date": cert["issue_date"] if cert else None,
            "fingerprint": fingerprint,
            "manifest_hash": cert["manifest_hash"] if cert else None,
            "claims": [dict(c) for c in claims],
            "event_seq": None,
            "duplicate": True,
        }

    # ------------------------------------------------------------------ #
    # 证书暂停 / 恢复（形成链上的暂停区间）
    # ------------------------------------------------------------------ #
    def suspend_certificate(
        self, certificate_id: str, reason: str, suspend_from: str | date | None = None
    ) -> dict:
        day = day_str(suspend_from) or date.today().isoformat()
        ts = utc_now_iso()
        with self.store.lock:
            self.store.execute("BEGIN IMMEDIATE")
            try:
                cert = self.store.query_one("SELECT * FROM certificate WHERE id=?", (certificate_id,))
                if cert is None:
                    raise DomainError("证书不存在", 404)
                if cert["status"] == "suspended":
                    raise DomainError("证书已处于暂停状态", 409)
                evt = append_event(
                    self.store,
                    "certificate_suspended",
                    certificate_id,
                    {"reason": reason, "suspend_from": day},
                    ts,
                )
                self.store.execute(
                    "UPDATE certificate SET status='suspended', suspended_at=?,"
                    " suspension_reason=?, resume_seq=NULL WHERE id=?",
                    (day, reason, certificate_id),
                )
                self.store.execute("COMMIT")
            except Exception:
                self.store.execute("ROLLBACK")
                raise
        return {"certificate_id": certificate_id, "suspend_from": day, "event_seq": evt["seq"]}

    def resume_certificate(self, certificate_id: str, resume_on: str | date | None = None) -> dict:
        day = day_str(resume_on) or date.today().isoformat()
        ts = utc_now_iso()
        with self.store.lock:
            self.store.execute("BEGIN IMMEDIATE")
            try:
                cert = self.store.query_one("SELECT * FROM certificate WHERE id=?", (certificate_id,))
                if cert is None:
                    raise DomainError("证书不存在", 404)
                if cert["status"] != "suspended":
                    raise DomainError("证书未暂停，无需恢复", 409)
                evt = append_event(
                    self.store,
                    "certificate_resumed",
                    certificate_id,
                    {"resume_on": day},
                    ts,
                )
                self.store.execute(
                    "UPDATE certificate SET status='active', suspended_at=NULL,"
                    " suspension_reason=NULL, resume_seq=? WHERE id=?",
                    (evt["seq"], certificate_id),
                )
                self.store.execute("COMMIT")
            except Exception:
                self.store.execute("ROLLBACK")
                raise
        return {"certificate_id": certificate_id, "resume_on": day, "event_seq": evt["seq"]}

    def _suspension_intervals(self, certificate_id: str) -> list[dict]:
        """以链事件为唯一事实源，重放暂停/恢复区间。"""
        rows = self.store.query_all(
            "SELECT seq, event_type, payload_json FROM immutable_event"
            " WHERE aggregate_id=? AND event_type IN ('certificate_suspended','certificate_resumed')"
            " ORDER BY seq",
            (certificate_id,),
        )
        intervals: list[dict] = []
        open_start: str | None = None
        for r in rows:
            payload = json.loads(r["payload_json"])
            if r["event_type"] == "certificate_suspended":
                if open_start is None:
                    open_start = payload["suspend_from"]
            elif open_start is not None:
                intervals.append({"from": open_start, "to": payload["resume_on"]})
                open_start = None
        if open_start is not None:
            intervals.append({"from": open_start, "to": None})
        return intervals

    # ------------------------------------------------------------------ #
    # 验证：指定日期 + 主题上的有效范围
    # ------------------------------------------------------------------ #
    def verify_certificate(
        self,
        certificate_id: str,
        on_date: str | date | None = None,
        topic_code: str | None = None,
    ) -> dict:
        target_day = day_str(on_date) or date.today().isoformat()
        cert = self.store.query_one("SELECT * FROM certificate WHERE id=?", (certificate_id,))
        if cert is None:
            raise DomainError("证书不存在", 404)
        if topic_code and cert["topic_code"] != topic_code:
            raise DomainError("证书不属于该主题", 404)

        intervals = self._suspension_intervals(certificate_id)
        suspended_now = any(i["to"] is None for i in intervals)

        claim_rows = self.store.query_all(
            "SELECT * FROM certificate_claim WHERE certificate_id=? ORDER BY claim_seq",
            (certificate_id,),
        )
        claim_results = []
        overall_valid = not suspended_now
        for cr in claim_rows:
            reasons: list[str] = []
            valid = True

            if not (cr["valid_from"] <= target_day <= cr["valid_until"]):
                valid = False
                if target_day < cr["valid_from"]:
                    reasons.append("尚未生效")
                else:
                    reasons.append("已过有效期")

            if any(iv["from"] <= target_day and (iv["to"] is None or target_day < iv["to"])
                   for iv in intervals):
                valid = False
                reasons.append("证书在该日期处于暂停期")

            score = self.store.query_one(
                "SELECT revoked, revoked_at FROM unit_score WHERE id=?", (cr["score_id"],)
            )
            if score is None:
                valid = False
                reasons.append("证据成绩缺失")
            else:
                # 撤销事实以不可变链为准；行内标志只是投影，两者必须一致
                rev_events = self.store.query_all(
                    "SELECT payload_json FROM immutable_event"
                    " WHERE event_type='score_revoked' AND aggregate_id=?",
                    (cr["score_id"],),
                )
                revoked_on = None
                if rev_events:
                    revoked_on = json.loads(rev_events[-1]["payload_json"])["revoked_on"]
                chain_says_revoked = bool(rev_events)
                if bool(score["revoked"]) != chain_says_revoked:
                    valid = False
                    reasons.append("撤销状态与不可变链不一致")
                elif chain_says_revoked and revoked_on <= target_day:
                    valid = False
                    reasons.append(f"证据成绩已于 {revoked_on} 撤销")

            # 证据哈希复核：从成绩表/试卷表现值重组，检测库内直接篡改
            recomputed = self.store.query_one(
                "SELECT s.exam_date AS exam_date, s.attempt_no AS attempt_no, s.score AS score,"
                " s.examiner_id AS examiner_id, p.id AS paper_id, p.version_tag AS version_tag"
                " FROM unit_score s JOIN paper_version p ON p.id=s.paper_id WHERE s.id=?",
                (cr["score_id"],),
            )
            if recomputed is not None:
                evidence = {
                    "unit_code": cr["unit_code"],
                    "basis": cr["basis"],
                    "score_id": cr["score_id"],
                    "attempt_no": recomputed["attempt_no"],
                    "paper_id": recomputed["paper_id"],
                    "paper_version_tag": recomputed["version_tag"],
                    "examiner_id": recomputed["examiner_id"],
                    "score": recomputed["score"],
                    "pass": True,
                    "exam_date": recomputed["exam_date"],
                    "pass_date": cr["pass_date"],
                    "substituted_from": cr["substituted_from"],
                    "valid_from": cr["valid_from"],
                    "valid_until": cr["valid_until"],
                }
                if sha256_text(canonical(evidence)) != cr["evidence_hash"]:
                    valid = False
                    reasons.append("证据哈希不匹配")

            overall_valid = overall_valid and valid
            claim_results.append(
                {
                    "unit_code": cr["unit_code"],
                    "basis": cr["basis"],
                    "substituted_from": cr["substituted_from"],
                    "valid": valid,
                    "valid_from": cr["valid_from"],
                    "valid_until": cr["valid_until"],
                    "reasons": reasons,
                    "evidence_hash": cr["evidence_hash"],
                }
            )

        chain = verify_chain(self.store)
        return {
            "certificate_id": certificate_id,
            "volunteer_id": cert["volunteer_id"],
            "topic_code": cert["topic_code"],
            "on_date": target_day,
            "overall_valid": overall_valid,
            "certificate_status": cert["status"],
            "suspension_intervals": intervals,
            "issue_date": cert["issue_date"],
            "fingerprint": cert["fingerprint"],
            "manifest_hash": cert["manifest_hash"],
            "claims": claim_results,
            "chain_ok": chain["ok"],
            "valid_scope": {
                "topic_code": cert["topic_code"],
                "units": [c["unit_code"] for c in claim_results if c["valid"]],
                "date": target_day,
            },
        }

    def get_certificate(self, certificate_id: str) -> dict:
        cert = self.store.query_one("SELECT * FROM certificate WHERE id=?", (certificate_id,))
        if cert is None:
            raise DomainError("证书不存在", 404)
        claims = self.store.query_all(
            "SELECT claim_seq, unit_code, basis, substituted_from, score_id, paper_id,"
            " examiner_id, score, pass_date, valid_from, valid_until, evidence_hash"
            " FROM certificate_claim WHERE certificate_id=? ORDER BY claim_seq",
            (certificate_id,),
        )
        result = dict(cert)
        result["claims"] = [dict(c) for c in claims]
        result["suspension_intervals"] = self._suspension_intervals(certificate_id)
        return result

    def chain_report(self) -> dict:
        return verify_chain(self.store)

    def list_events(self, limit: int = 100) -> list[dict]:
        rows = self.store.query_all(
            "SELECT seq, event_type, aggregate_id, payload_json, created_at, prev_hash, hash"
            " FROM immutable_event ORDER BY seq DESC LIMIT ?",
            (limit,),
        )
        return [
            {
                "seq": r["seq"],
                "event_type": r["event_type"],
                "aggregate_id": r["aggregate_id"],
                "payload": json.loads(r["payload_json"]),
                "created_at": r["created_at"],
                "prev_hash": r["prev_hash"],
                "hash": r["hash"],
            }
            for r in reversed(rows)
        ]
