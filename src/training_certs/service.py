"""领域服务：所有写操作在单个 SQLite 事务内完成"重建状态→校验→追加事件"。

并发安全依赖两道防线：
1. IMMEDIATE 事务让写操作在数据库层串行化；
2. score_attempts 与 certificates 上的唯一索引兜底，
   即使两个事务同时通过业务校验，重复数据也无法落库。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import date
from typing import Optional

from .errors import ConflictError, DomainError, NotFoundError
from .events import GENESIS_HASH, add_years, canonical, event_hash, now_iso, parse_date
from .models import AppState, Attempt, Certificate, rebuild
from .store import append_event, connect


class TrainingService:
    def __init__(self, db_path: str):
        self.db_path = db_path

    # ---- 连接与事务 ----

    def _conn(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def _mutate(self, fn):
        """打开 IMMEDIATE 事务，重建状态后执行业务函数。"""
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            state = rebuild(conn)
            result = fn(state, conn)
            conn.execute("COMMIT")
            return result
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _query(self, fn):
        conn = self._conn()
        try:
            return fn(rebuild(conn), conn)
        finally:
            conn.close()

    # ---- 基础注册 ----

    def register_volunteer(self, volunteer_id: str, name: str = "") -> dict:
        def do(state: AppState, conn: sqlite3.Connection) -> dict:
            if volunteer_id in state.volunteers:
                raise ConflictError(f"志愿者已存在：{volunteer_id}")
            return append_event(conn, "VolunteerRegistered", volunteer_id, {"name": name})

        return self._mutate(do)

    def register_unit(self, unit_code: str, topic: str, title: str = "") -> dict:
        def do(state: AppState, conn: sqlite3.Connection) -> dict:
            if not topic:
                raise DomainError("主题不能为空")
            if unit_code in state.units:
                raise ConflictError(f"单元已存在：{unit_code}")
            return append_event(
                conn, "UnitRegistered", unit_code, {"topic": topic, "title": title}
            )

        return self._mutate(do)

    def register_paper(
        self,
        unit_code: str,
        created_by: str,
        pass_score: float = 60.0,
        paper_id: Optional[str] = None,
        content_ref: Optional[str] = None,
        published_at: Optional[str] = None,
    ) -> dict:
        """为单元登记新试卷版本，版本号在该单元内单调递增并随成绩永久保存。"""
        def do(state: AppState, conn: sqlite3.Connection) -> dict:
            unit = state.units.get(unit_code)
            if unit is None:
                raise NotFoundError(f"单元不存在：{unit_code}")
            version = (
                max(
                    (p["version"] for p in state.papers.values() if p["unit_code"] == unit_code),
                    default=0,
                )
                + 1
            )
            pid = paper_id or f"P-{unit_code}-V{version}"
            if pid in state.papers:
                raise ConflictError(f"试卷编号已存在：{pid}")
            payload = {
                "unit_code": unit_code,
                "topic": unit["topic"],
                "version": version,
                "published_at": published_at or now_iso(),
                "created_by": created_by,
                "pass_score": float(pass_score),
                "content_ref": content_ref,
            }
            meta = append_event(conn, "PaperVersionRegistered", pid, payload)
            return {
                "paper_id": pid,
                "version": version,
                **meta,
            }

        return self._mutate(do)

    # ---- 补考资格 ----

    def grant_retake_eligibility(
        self, volunteer_id: str, unit_code: str, max_retakes: int, by: str, reason: str = ""
    ) -> dict:
        if not isinstance(max_retakes, int) or max_retakes < 0:
            raise DomainError("补考次数上限必须是非负整数")
        if not by:
            raise DomainError("缺少授权教务员")

        def do(state: AppState, conn: sqlite3.Connection) -> dict:
            self._require_volunteer(state, volunteer_id)
            if unit_code not in state.units:
                raise NotFoundError(f"单元不存在：{unit_code}")
            return append_event(
                conn,
                "RetakeEligibilityGranted",
                f"{volunteer_id}:{unit_code}",
                {
                    "volunteer_id": volunteer_id,
                    "unit_code": unit_code,
                    "max_retakes": max_retakes,
                    "by": by,
                    "reason": reason,
                },
            )

        return self._mutate(do)

    # ---- 成绩 ----

    def record_score(
        self,
        volunteer_id: str,
        unit_code: str,
        paper_id: str,
        score: float,
        examiner_id: str,
        exam_date: str,
    ) -> dict:
        on = parse_date(exam_date, "考试日期")
        if not isinstance(score, (int, float)) or not 0 <= float(score) <= 100:
            raise DomainError("成绩必须是 0 到 100 之间的数值")
        if not examiner_id:
            raise DomainError("缺少考官")

        def do(state: AppState, conn: sqlite3.Connection) -> dict:
            self._require_volunteer(state, volunteer_id)
            paper = state.papers.get(paper_id)
            if paper is None:
                raise NotFoundError(f"试卷不存在：{paper_id}")
            if paper["unit_code"] != unit_code:
                raise DomainError(f"试卷 {paper_id} 不属于单元 {unit_code}")

            prior = state.attempts.get((volunteer_id, unit_code), [])
            attempt_no = len(prior) + 1
            if not prior:
                kind = "first"
            else:
                kind = "retake"
                remaining = state.retake_remaining(volunteer_id, unit_code)
                if remaining is None:
                    raise DomainError(f"志愿者 {volunteer_id} 的单元 {unit_code} 尚无补考资格")
                if remaining <= 0:
                    raise ConflictError(
                        f"补考次数已用完（上限 "
                        f"{state.eligibility[(volunteer_id, unit_code)].max_retakes} 次）"
                    )

            score_id = uuid.uuid4().hex
            payload = {
                "volunteer_id": volunteer_id,
                "unit_code": unit_code,
                "attempt_no": attempt_no,
                "kind": kind,
                "paper_id": paper_id,
                "paper_version": paper["version"],
                "score": float(score),
                "passed": float(score) >= paper["pass_score"],
                "examiner_id": examiner_id,
                "exam_date": on.isoformat(),
            }
            meta = append_event(conn, "ScoreRecorded", score_id, payload)
            # 兜底唯一约束：同单元同一次尝试只能有一条成绩
            try:
                conn.execute(
                    "INSERT INTO score_attempts (volunteer_id, unit_code, attempt, score_id) "
                    "VALUES (?, ?, ?, ?)",
                    (volunteer_id, unit_code, attempt_no, score_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("并发冲突：该次考试成绩已被其他请求记录") from exc
            return {"score_id": score_id, "attempt_no": attempt_no, "kind": kind, **meta}

        return self._mutate(do)

    def revoke_score(self, score_id: str, by: str, reason: str) -> dict:
        if not by or not reason:
            raise DomainError("撤销成绩必须提供经办人与原因")

        def do(state: AppState, conn: sqlite3.Connection) -> dict:
            key = state.score_index.get(score_id)
            if key is None:
                raise NotFoundError(f"成绩不存在：{score_id}")
            attempt = next(a for a in state.attempts[key] if a.score_id == score_id)
            if attempt.revoked:
                raise ConflictError(f"成绩已撤销：{score_id}")
            return append_event(
                conn,
                "ScoreRevoked",
                score_id,
                {"by": by, "reason": reason, "revoked_at": now_iso()},
            )

        return self._mutate(do)

    # ---- 单元替代 ----

    def define_unit_replacement(
        self, topic: str, old_unit: str, new_unit: str, effective_date: str, by: str
    ) -> dict:
        on = parse_date(effective_date, "生效日期")
        if not by:
            raise DomainError("缺少经办人")

        def do(state: AppState, conn: sqlite3.Connection) -> dict:
            for code in (old_unit, new_unit):
                unit = state.units.get(code)
                if unit is None:
                    raise NotFoundError(f"单元不存在：{code}")
                if unit["topic"] != topic:
                    raise DomainError(f"单元 {code} 不属于主题 {topic}")
            if old_unit == new_unit:
                raise DomainError("替代单元不能与原单元相同")
            if (topic, old_unit) in state.replacements:
                raise ConflictError(f"单元 {old_unit} 已定义替代关系")
            # 防止替代环
            cursor = new_unit
            seen = {old_unit}
            while cursor is not None:
                if cursor in seen:
                    raise DomainError("替代关系形成环，拒绝登记")
                seen.add(cursor)
                nxt = state.replacements.get((topic, cursor))
                cursor = nxt[0] if nxt else None
            return append_event(
                conn,
                "UnitReplaced",
                f"{topic}:{old_unit}->{new_unit}",
                {
                    "topic": topic,
                    "old_unit": old_unit,
                    "new_unit": new_unit,
                    "effective_date": on.isoformat(),
                    "by": by,
                },
            )

        return self._mutate(do)

    # ---- 发证 ----

    def issue_certificate(
        self,
        volunteer_id: str,
        topic: str,
        issued_by: str,
        issued_on: str,
        validity_years: int = 2,
        cert_no: Optional[str] = None,
    ) -> dict:
        on = parse_date(issued_on, "签发日期")
        if not isinstance(validity_years, int) or validity_years <= 0:
            raise DomainError("证书有效期必须是正整数年")
        if not issued_by:
            raise DomainError("缺少发证人")

        def do(state: AppState, conn: sqlite3.Connection) -> dict:
            self._require_volunteer(state, volunteer_id)
            units = state.topic_units(topic)
            if not units:
                raise NotFoundError(f"主题不存在或没有单元：{topic}")
            existing = state.active_certs.get((volunteer_id, topic))
            if existing:
                raise ConflictError(f"该志愿者在主题 {topic} 已持有有效证书：{existing}")

            # 每个注册单元沿替代链归约到签发日实际要求的单元后去重：
            # 已被替代的旧单元不会成为独立条目。
            required_units = sorted(
                {self._resolve_required(state, topic, code, on) for code in units}
            )
            claims = []
            for required in required_units:
                claim = self._build_claim(state, volunteer_id, topic, required, on)
                if claim is None:
                    raise DomainError(
                        f"志愿者 {volunteer_id} 在签发日尚未通过单元 {required}（或其可替代旧单元）"
                    )
                claims.append(claim)

            number = cert_no or f"CERT-{volunteer_id}-{topic}-{uuid.uuid4().hex[:8]}"
            if number in state.certificates:
                raise ConflictError(f"证书编号已存在：{number}")
            expires_on = add_years(on, validity_years)
            declaration = {
                "topic": topic,
                "issued_on": on.isoformat(),
                "expires_on": expires_on.isoformat(),
                "validity_years": validity_years,
                "claims": claims,
                "evidence_hash": self._evidence_hash(claims),
            }
            payload = {
                "volunteer_id": volunteer_id,
                "topic": topic,
                "issued_on": on.isoformat(),
                "expires_on": expires_on.isoformat(),
                "validity_years": validity_years,
                "issued_by": issued_by,
                "declaration": declaration,
            }
            meta = append_event(conn, "CertificateIssued", number, payload)
            try:
                conn.execute(
                    "INSERT INTO certificates (cert_no, volunteer_id, topic, state, issued_on) "
                    "VALUES (?, ?, ?, 'issued', ?)",
                    (number, volunteer_id, topic, on.isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("并发冲突：该志愿者在此主题上的证书已被其他请求签发") from exc
            return {
                "cert_no": number,
                "declaration": declaration,
                "anchor_seq": state.last_seq + 1,
                "anchor_hash": meta["hash"],
            }

        return self._mutate(do)

    def suspend_certificate(self, cert_no: str, from_date: str, reason: str) -> dict:
        on = parse_date(from_date, "暂停日期")
        if not reason:
            raise DomainError("暂停必须说明原因")

        def do(state: AppState, conn: sqlite3.Connection) -> dict:
            cert = self._require_cert(state, cert_no)
            if cert.state != "issued":
                raise ConflictError(f"证书当前状态为 {cert.state}，不能暂停")
            meta = append_event(
                conn,
                "CertificateSuspended",
                cert_no,
                {"from_date": on.isoformat(), "reason": reason},
            )
            conn.execute("UPDATE certificates SET state = 'suspended' WHERE cert_no = ?", (cert_no,))
            return meta

        return self._mutate(do)

    def resume_certificate(self, cert_no: str, resume_date: str, reason: str = "") -> dict:
        on = parse_date(resume_date, "恢复日期")

        def do(state: AppState, conn: sqlite3.Connection) -> dict:
            cert = self._require_cert(state, cert_no)
            if cert.state != "suspended":
                raise ConflictError(f"证书当前状态为 {cert.state}，不能恢复")
            meta = append_event(
                conn,
                "CertificateResumed",
                cert_no,
                {"resume_date": on.isoformat(), "reason": reason},
            )
            conn.execute("UPDATE certificates SET state = 'issued' WHERE cert_no = ?", (cert_no,))
            return meta

        return self._mutate(do)

    def revoke_certificate(self, cert_no: str, on_date: str, by: str, reason: str) -> dict:
        on = parse_date(on_date, "撤销日期")
        if not by or not reason:
            raise DomainError("撤销证书必须提供经办人与原因")

        def do(state: AppState, conn: sqlite3.Connection) -> dict:
            cert = self._require_cert(state, cert_no)
            if cert.state == "revoked":
                raise ConflictError("证书已撤销")
            meta = append_event(
                conn,
                "CertificateRevoked",
                cert_no,
                {"on_date": on.isoformat(), "by": by, "reason": reason},
            )
            conn.execute("DELETE FROM certificates WHERE cert_no = ?", (cert_no,))
            return meta

        return self._mutate(do)

    # ---- 查询 ----

    def get_certificate(self, cert_no: str) -> dict:
        def do(state: AppState, conn: sqlite3.Connection) -> dict:
            return self._cert_json(self._require_cert(state, cert_no), state)

        return self._query(do)

    def volunteer_history(self, volunteer_id: str) -> dict:
        def do(state: AppState, conn: sqlite3.Connection) -> dict:
            self._require_volunteer(state, volunteer_id)
            units: dict[str, list[dict]] = {}
            for (vid, code), attempts in state.attempts.items():
                if vid != volunteer_id:
                    continue
                units[code] = [self._attempt_json(a) for a in attempts]
            return {"volunteer_id": volunteer_id, "units": units}

        return self._query(do)

    def verify(self, cert_no: str, on_date: str, topic: Optional[str] = None) -> dict:
        """回答：证书在指定日期、指定主题上的有效范围是什么。"""
        on = parse_date(on_date, "验证日期")

        def do(state: AppState, conn: sqlite3.Connection) -> dict:
            cert = self._require_cert(state, cert_no)
            if topic is not None and cert.topic != topic:
                raise DomainError(
                    f"证书主题为 {cert.topic}，与请求的主题 {topic} 不符"
                )
            # 生命周期状态（按日期重建）
            if on < cert.issued_on:
                lifecycle = "not_yet_issued"
            elif cert.revoked_on is not None and on >= cert.revoked_on:
                lifecycle = "revoked"
            elif on > cert.expires_on:
                lifecycle = "expired"
            elif any(
                s.from_date <= on and (s.resume_date is None or on < s.resume_date)
                for s in cert.suspensions
            ):
                lifecycle = "suspended"
            else:
                lifecycle = "valid"

            claim_results = []
            evidence_intact = True
            for claim in cert.declaration["claims"]:
                key = (cert.volunteer_id, claim["evidence_unit_code"])
                attempt = next(
                    (a for a in state.attempts.get(key, []) if a.score_id == claim["score_id"]),
                    None,
                )
                if attempt is None or attempt.revoked or attempt.exam_date > on:
                    evidence_intact = False
                    status = "not_valid"
                elif lifecycle in ("valid", "suspended"):
                    status = "in_scope" if lifecycle == "valid" else "suspended"
                else:
                    status = "not_valid"
                claim_results.append(
                    {
                        "unit_code": claim["unit_code"],
                        "evidence_unit_code": claim["evidence_unit_code"],
                        "pass_mode": claim["pass_mode"],
                        "paper_id": claim["paper_id"],
                        "paper_version": claim["paper_version"],
                        "examiner_id": claim["examiner_id"],
                        "exam_date": claim["exam_date"],
                        "score_id": claim["score_id"],
                        "status": status,
                    }
                )

            return {
                "cert_no": cert.cert_no,
                "volunteer_id": cert.volunteer_id,
                "topic": cert.topic,
                "query_date": on.isoformat(),
                "issued_on": cert.issued_on.isoformat(),
                "expires_on": cert.expires_on.isoformat(),
                "lifecycle": lifecycle,
                "evidence_hash": cert.declaration["evidence_hash"],
                "evidence_intact": evidence_intact,
                "anchor_seq": cert.anchor_seq,
                "anchor_hash": cert.anchor_hash,
                "scope": claim_results,
                "valid_scope": [c["unit_code"] for c in claim_results if c["status"] == "in_scope"],
            }

        return self._query(do)

    def verify_chain(self) -> dict:
        """重算整条哈希链，检测账本是否被篡改或断链。"""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT seq, event_id, event_type, aggregate_id, payload, recorded_at, "
                "prev_hash, hash FROM ledger ORDER BY seq"
            ).fetchall()
            prev = GENESIS_HASH
            for row in rows:
                digest = event_hash(
                    prev,
                    row["event_id"],
                    row["event_type"],
                    row["aggregate_id"],
                    row["recorded_at"],
                    json.loads(row["payload"]),
                )
                if row["prev_hash"] != prev:
                    return {"intact": False, "broken_at": row["seq"], "reason": "prev_hash 断链"}
                if digest != row["hash"]:
                    return {"intact": False, "broken_at": row["seq"], "reason": "内容哈希不匹配"}
                prev = row["hash"]
            return {"intact": True, "events": len(rows), "head": prev}
        finally:
            conn.close()

    # ---- 内部辅助 ----

    @staticmethod
    def _require_volunteer(state: AppState, volunteer_id: str) -> None:
        if volunteer_id not in state.volunteers:
            raise NotFoundError(f"志愿者不存在：{volunteer_id}")

    @staticmethod
    def _require_cert(state: AppState, cert_no: str) -> Certificate:
        cert = state.certificates.get(cert_no)
        if cert is None:
            raise NotFoundError(f"证书不存在：{cert_no}")
        return cert

    @staticmethod
    def _resolve_required(state: AppState, topic: str, code: str, on: date) -> str:
        """沿替代链找到在指定日期实际要求的单元（防止成环死循环）。"""
        seen: set[str] = set()
        cur = code
        while cur not in seen:
            seen.add(cur)
            replacement = state.replacements.get((topic, cur))
            if replacement and replacement[1] <= on:
                cur = replacement[0]
            else:
                return cur
        return cur

    def _build_claim(
        self,
        state: AppState,
        volunteer_id: str,
        topic: str,
        required: str,
        on: date,
    ) -> Optional[dict]:
        # 1) 直接通过要求单元（取签发日前最新一条未撤销且通过的成绩）
        direct = self._passing_attempt(state, volunteer_id, required, on)
        if direct is not None:
            mode = "first_pass" if direct.kind == "first" else "retake_pass"
            return self._claim(required, direct, mode)
        # 2) 旧单元替代：沿"谁替代成了 required"反向查找，
        #    旧单元成绩必须早于该旧单元被替代的生效日。
        for old_unit, effective in self._predecessors(state, topic, required):
            attempt = self._passing_attempt(state, volunteer_id, old_unit, min(on, effective))
            # 考试日期必须严格早于替代生效日
            if attempt is not None and attempt.exam_date < effective:
                claim = self._claim(
                    required,
                    attempt,
                    "substitution",
                    evidence_unit=old_unit,
                )
                claim["replaced_from"] = old_unit
                claim["replacement_effective_date"] = effective.isoformat()
                return claim
        return None

    @staticmethod
    def _predecessors(
        state: AppState, topic: str, target: str
    ) -> list[tuple[str, date]]:
        """返回替代链上所有最终指向 target 的 (旧单元, 该旧单元的替代生效日)。"""
        result: list[tuple[str, date]] = []
        for (t, old), (new, effective) in state.replacements.items():
            if t != topic:
                continue
            cur = new
            seen: set[str] = set()
            while cur not in seen:
                seen.add(cur)
                if cur == target:
                    result.append((old, effective))
                    break
                nxt = state.replacements.get((topic, cur))
                if not nxt:
                    break
                cur = nxt[0]
        return result

    @staticmethod
    def _passing_attempt(
        state: AppState, volunteer_id: str, unit_code: str, on: date
    ) -> Optional[Attempt]:
        live = [
            a
            for a in state.attempts.get((volunteer_id, unit_code), [])
            if not a.revoked and a.exam_date <= on
        ]
        if not live:
            return None
        latest = live[-1]
        return latest if latest.passed else None

    @staticmethod
    def _claim(
        required_unit: str,
        attempt: Attempt,
        pass_mode: str,
        evidence_unit: Optional[str] = None,
    ) -> dict:
        return {
            "unit_code": required_unit,
            "evidence_unit_code": evidence_unit or attempt.unit_code,
            "score_id": attempt.score_id,
            "paper_id": attempt.paper_id,
            "paper_version": attempt.paper_version,
            "score": attempt.score,
            "passed": attempt.passed,
            "examiner_id": attempt.examiner_id,
            "exam_date": attempt.exam_date.isoformat(),
            "pass_mode": pass_mode,
        }

    @staticmethod
    def _evidence_hash(claims: list[dict]) -> str:
        return hashlib.sha256(canonical(claims).encode("utf-8")).hexdigest()

    @staticmethod
    def _attempt_json(a: Attempt) -> dict:
        return {
            "score_id": a.score_id,
            "attempt_no": a.attempt_no,
            "kind": a.kind,
            "paper_id": a.paper_id,
            "paper_version": a.paper_version,
            "score": a.score,
            "passed": a.passed,
            "examiner_id": a.examiner_id,
            "exam_date": a.exam_date.isoformat(),
            "revoked": a.revoked,
            "revoke_reason": a.revoke_reason,
            "revoked_by": a.revoked_by,
        }

    @staticmethod
    def _cert_json(cert: Certificate, state: AppState) -> dict:
        return {
            "cert_no": cert.cert_no,
            "volunteer_id": cert.volunteer_id,
            "topic": cert.topic,
            "state": cert.state,
            "issued_on": cert.issued_on.isoformat(),
            "expires_on": cert.expires_on.isoformat(),
            "issued_by": cert.issued_by,
            "declaration": cert.declaration,
            "anchor_seq": cert.anchor_seq,
            "anchor_hash": cert.anchor_hash,
            "suspend_reason": cert.suspend_reason,
            "revoke_reason": cert.revoke_reason,
            "revoked_on": cert.revoked_on.isoformat() if cert.revoked_on else None,
        }
