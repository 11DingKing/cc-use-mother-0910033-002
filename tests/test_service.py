"""领域服务测试：成绩历史、补考约束、单元替代、证书生命周期与哈希链。"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from training_certs.errors import ConflictError, DomainError, NotFoundError
from training_certs.service import TrainingService


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = TrainingService(str(Path(self.tmp.name) / "test.db"))
        self._seed()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed(self) -> None:
        self.svc.register_volunteer("V1", "张三")
        self.svc.register_unit("U-A", "急救", "心肺复苏")
        self.svc.register_unit("U-B", "急救", "止血包扎")
        self.svc.paper_a1 = self.svc.register_paper("U-A", "admin", paper_id="P-A1")["event_id"]
        self.svc.paper_b1 = self.svc.register_paper("U-B", "admin", paper_id="P-B1")["event_id"]

    # ---- 成绩历史与首考/补考 ----

    def test_first_and_retake_attempts_are_distinguished(self) -> None:
        r1 = self.svc.record_score("V1", "U-A", "P-A1", 55, "EX1", "2026-03-01")
        self.assertEqual(r1["kind"], "first")
        self.assertEqual(r1["attempt_no"], 1)

        # 未授予补考资格，不能补考
        with self.assertRaises(DomainError):
            self.svc.record_score("V1", "U-A", "P-A1", 70, "EX1", "2026-03-05")

        self.svc.grant_retake_eligibility("V1", "U-A", 1, "dean", "首次补考")
        r2 = self.svc.record_score("V1", "U-A", "P-A1", 72, "EX2", "2026-03-05")
        self.assertEqual(r2["kind"], "retake")
        self.assertEqual(r2["attempt_no"], 2)

        history = self.svc.volunteer_history("V1")["units"]["U-A"]
        self.assertEqual([h["kind"] for h in history], ["first", "retake"])
        self.assertEqual([h["paper_version"] for h in history], [1, 1])

    def test_retake_limit_is_enforced_even_after_pass(self) -> None:
        self.svc.grant_retake_eligibility("V1", "U-A", 1, "dean")
        self.svc.record_score("V1", "U-A", "P-A1", 55, "EX1", "2026-03-01")
        self.svc.record_score("V1", "U-A", "P-A1", 90, "EX1", "2026-03-02")
        with self.assertRaises(ConflictError):
            self.svc.record_score("V1", "U-A", "P-A1", 95, "EX1", "2026-03-03")

    def test_revoked_score_still_consumes_retake_quota(self) -> None:
        self.svc.grant_retake_eligibility("V1", "U-A", 1, "dean")
        self.svc.record_score("V1", "U-A", "P-A1", 55, "EX1", "2026-03-01")
        retake = self.svc.record_score("V1", "U-A", "P-A1", 80, "EX1", "2026-03-02")
        self.svc.revoke_score(retake["score_id"], "dean", "考官登记有误")
        self.assertEqual(self.svc._query(
            lambda s, c: s.retake_remaining("V1", "U-A")
        ), 0)
        # 撤销是追加事件，历史里仍有两条记录
        history = self.svc.volunteer_history("V1")["units"]["U-A"]
        self.assertFalse(history[0]["revoked"])
        self.assertTrue(history[1]["revoked"])
        self.assertEqual(history[1]["revoke_reason"], "考官登记有误")
        # 撤销的成绩不能再撤销
        with self.assertRaises(ConflictError):
            self.svc.revoke_score(retake["score_id"], "dean", "再次撤销")

    # ---- 试卷版本 ----

    def test_paper_versions_are_monotonic_and_pinned_to_scores(self) -> None:
        v2 = self.svc.register_paper("U-A", "admin")
        self.assertEqual(v2["paper_id"], "P-U-A-V2")
        self.assertEqual(v2["version"], 2)
        self.svc.grant_retake_eligibility("V1", "U-A", 2, "dean")
        self.svc.record_score("V1", "U-A", "P-A1", 55, "EX1", "2026-03-01")
        self.svc.record_score("V1", "U-A", "P-U-A-V2", 58, "EX1", "2026-03-02")
        history = self.svc.volunteer_history("V1")["units"]["U-A"]
        self.assertEqual([h["paper_id"] for h in history], ["P-A1", "P-U-A-V2"])
        self.assertEqual([h["paper_version"] for h in history], [1, 2])

    # ---- 发证与声明 ----

    def _pass_all(self) -> None:
        self.svc.record_score("V1", "U-A", "P-A1", 80, "EX1", "2026-03-01")
        self.svc.record_score("V1", "U-B", "P-B1", 90, "EX2", "2026-03-01")

    def test_issue_certificate_fixes_evidence_combination(self) -> None:
        self._pass_all()
        result = self.svc.issue_certificate("V1", "急救", "dean", "2026-04-01")
        cert = self.svc.get_certificate(result["cert_no"])
        self.assertEqual(len(cert["declaration"]["claims"]), 2)
        modes = {c["unit_code"]: c["pass_mode"] for c in cert["declaration"]["claims"]}
        self.assertEqual(modes, {"U-A": "first_pass", "U-B": "first_pass"})
        # 锚点固定在哈希链上
        self.assertEqual(cert["anchor_hash"][:8], result["anchor_hash"][:8])

    def test_cannot_issue_before_all_units_passed(self) -> None:
        self.svc.record_score("V1", "U-A", "P-A1", 80, "EX1", "2026-03-01")
        with self.assertRaises(DomainError):
            self.svc.issue_certificate("V1", "急救", "dean", "2026-04-01")

    def test_declaration_is_not_mutated_by_later_revocation(self) -> None:
        self._pass_all()
        result = self.svc.issue_certificate("V1", "急救", "dean", "2026-04-01")
        score_id = self.svc.volunteer_history("V1")["units"]["U-A"][0]["score_id"]
        # 发证后撤销成绩：声明原样保留，但验证时证据被标记失效
        self.svc.revoke_score(score_id, "dean", "事后核查违规")
        cert = self.svc.get_certificate(result["cert_no"])
        self.assertEqual(cert["declaration"]["claims"][0]["score_id"], score_id)
        check = self.svc.verify(result["cert_no"], "2026-05-01")
        self.assertFalse(check["evidence_intact"])
        statuses = {c["unit_code"]: c["status"] for c in check["scope"]}
        self.assertEqual(statuses["U-A"], "not_valid")
        self.assertEqual(statuses["U-B"], "in_scope")

    # ---- 验证接口 ----

    def test_verify_reports_lifecycle_across_dates(self) -> None:
        self._pass_all()
        no = self.svc.issue_certificate("V1", "急救", "dean", "2026-04-01", validity_years=2)["cert_no"]
        self.assertEqual(self.svc.verify(no, "2026-03-31")["lifecycle"], "not_yet_issued")
        self.assertEqual(self.svc.verify(no, "2026-04-01")["lifecycle"], "valid")
        self.svc.suspend_certificate(no, "2026-06-01", "投诉调查")
        self.assertEqual(self.svc.verify(no, "2026-06-15")["lifecycle"], "suspended")
        self.svc.resume_certificate(no, "2026-07-01", "调查结束")
        self.assertEqual(self.svc.verify(no, "2026-07-02")["lifecycle"], "valid")
        self.assertEqual(self.svc.verify(no, "2028-04-02")["lifecycle"], "expired")
        self.svc.revoke_certificate(no, "2026-09-01", "dean", "资格造假")
        self.assertEqual(self.svc.verify(no, "2026-09-01")["lifecycle"], "revoked")
        with self.assertRaises(DomainError):
            self.svc.verify(no, "2026-09-01", topic="消防")

    def test_exam_after_verify_date_is_out_of_scope(self) -> None:
        # U-A 在 5 月才通过，4 月 1 日不能发证
        self.svc.record_score("V1", "U-A", "P-A1", 80, "EX1", "2026-05-01")
        self.svc.record_score("V1", "U-B", "P-B1", 90, "EX2", "2026-03-01")
        with self.assertRaises(DomainError):
            self.svc.issue_certificate("V1", "急救", "dean", "2026-04-01")
        no = self.svc.issue_certificate("V1", "急救", "dean", "2026-06-01")["cert_no"]
        # 发证后验证 4 月的有效范围：U-A 当时尚无成绩
        check = self.svc.verify(no, "2026-04-15")
        self.assertFalse(check["evidence_intact"])
        self.assertNotIn("U-A", check["valid_scope"])

    # ---- 单元替代 ----

    def test_unit_substitution_claim_uses_legacy_pass(self) -> None:
        # 旧单元 U-A 在 2026-02 首考通过；3 月起 U-A 被 U-C 替代
        self.svc.register_unit("U-C", "急救", "新版心肺复苏")
        self.svc.register_paper("U-C", "admin", paper_id="P-C1")
        self.svc.record_score("V1", "U-A", "P-A1", 85, "EX1", "2026-02-10")
        self.svc.record_score("V1", "U-B", "P-B1", 75, "EX2", "2026-02-10")
        self.svc.define_unit_replacement("急救", "U-A", "U-C", "2026-03-01", "dean")
        result = self.svc.issue_certificate("V1", "急救", "dean", "2026-04-01")
        cert = self.svc.get_certificate(result["cert_no"])
        claims = {c["unit_code"]: c for c in cert["declaration"]["claims"]}
        self.assertEqual(set(claims), {"U-C", "U-B"})
        self.assertEqual(claims["U-C"]["pass_mode"], "substitution")
        self.assertEqual(claims["U-C"]["evidence_unit_code"], "U-A")
        # 验证接口也承认替代证据
        check = self.svc.verify(result["cert_no"], "2026-04-02")
        self.assertTrue(check["evidence_intact"])
        self.assertEqual(set(check["valid_scope"]), {"U-C", "U-B"})

    def test_legacy_unit_pass_after_replacement_date_does_not_count(self) -> None:
        self.svc.register_unit("U-C", "急救", "新版心肺复苏")
        self.svc.define_unit_replacement("急救", "U-A", "U-C", "2026-03-01", "dean")
        # 替代生效后才考旧单元，不算数；U-C 没有成绩
        self.svc.record_score("V1", "U-A", "P-A1", 85, "EX1", "2026-03-10")
        self.svc.record_score("V1", "U-B", "P-B1", 75, "EX2", "2026-03-10")
        with self.assertRaises(DomainError):
            self.svc.issue_certificate("V1", "急救", "dean", "2026-04-01")

    def test_issue_before_replacement_effective_date_requires_old_unit(self) -> None:
        self.svc.register_unit("U-C", "急救", "新版心肺复苏")
        self.svc.register_paper("U-C", "admin", paper_id="P-C1")
        self.svc.define_unit_replacement("急救", "U-A", "U-C", "2026-03-01", "dean")
        # 2 月发证仍按旧大纲要求 U-A
        self.svc.record_score("V1", "U-C", "P-C1", 85, "EX1", "2026-02-10")
        self.svc.record_score("V1", "U-B", "P-B1", 75, "EX2", "2026-02-10")
        with self.assertRaises(DomainError):
            self.svc.issue_certificate("V1", "急救", "dean", "2026-02-15")

    # ---- 暂停 / 撤销链 ----

    def test_suspend_resume_transitions_are_guarded(self) -> None:
        self._pass_all()
        no = self.svc.issue_certificate("V1", "急救", "dean", "2026-04-01")["cert_no"]
        with self.assertRaises(ConflictError):
            self.svc.resume_certificate(no, "2026-05-01")
        self.svc.suspend_certificate(no, "2026-05-01", "调查")
        with self.assertRaises(ConflictError):
            self.svc.suspend_certificate(no, "2026-05-02", "再次暂停")
        # 暂停期间不能再发第二张
        with self.assertRaises(ConflictError):
            self.svc.issue_certificate("V1", "急救", "dean", "2026-05-03")
        self.svc.resume_certificate(no, "2026-06-01")
        # 撤销后腾出名额，可以重新发证
        self.svc.revoke_certificate(no, "2026-06-05", "dean", "材料不实")
        no2 = self.svc.issue_certificate("V1", "急救", "dean", "2026-06-10")["cert_no"]
        self.assertNotEqual(no, no2)

    # ---- 哈希链 ----

    def test_hash_chain_is_intact_and_detects_tampering(self) -> None:
        self._pass_all()
        self.svc.issue_certificate("V1", "急救", "dean", "2026-04-01")
        report = self.svc.verify_chain()
        self.assertTrue(report["intact"])
        self.assertGreaterEqual(report["events"], 5)

        # 直接篡改账本中一条事件的 payload
        conn = sqlite3.connect(self.svc.db_path)
        conn.execute("UPDATE ledger SET payload = ? WHERE event_type = 'ScoreRecorded'",
                     ('{"tampered": true}',))
        conn.commit()
        conn.close()
        report = self.svc.verify_chain()
        self.assertFalse(report["intact"])
        self.assertIn(report["reason"], ("内容哈希不匹配", "prev_hash 断链"))

    def test_unknown_event_type_in_ledger_fails_rebuild(self) -> None:
        self._pass_all()
        import uuid

        conn = sqlite3.connect(self.svc.db_path)
        conn.execute(
            "INSERT INTO ledger (event_id, event_type, aggregate_id, payload, recorded_at, "
            "prev_hash, hash) SELECT ?, 'BogusEvent', 'x', '{}', recorded_at, hash, ? "
            "FROM ledger ORDER BY seq DESC LIMIT 1",
            ("bogus", uuid.uuid4().hex),
        )
        conn.commit()
        conn.close()
        # 任何读取都会回放账本：未知事件让整个服务 fail-closed
        with self.assertRaises(ValueError):
            self.svc.volunteer_history("V1")
        with self.assertRaises(ValueError):
            self.svc.issue_certificate("V1", "急救", "dean", "2026-04-01")

    # ---- 边界 ----

    def test_validation_errors(self) -> None:
        with self.assertRaises(DomainError):
            self.svc.record_score("V1", "U-A", "P-A1", 150, "EX1", "2026-03-01")
        with self.assertRaises(DomainError):
            self.svc.record_score("V1", "U-A", "P-A1", 80, "EX1", "2026-3-1")
        with self.assertRaises(NotFoundError):
            self.svc.record_score("V1", "U-A", "P-NOPE", 80, "EX1", "2026-03-01")
        with self.assertRaises(NotFoundError):
            self.svc.verify("NO-SUCH-CERT", "2026-04-01")


if __name__ == "__main__":
    unittest.main()
