"""领域服务测试：成绩历史、补考资格、证据组合、暂停/撤销/替代、链与并发。"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from training_cert.service import CertService, DomainError
from training_cert.store import Store


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.svc = CertService(self.store)
        # 主题 HM 两个单元，试卷版本 v1 及格线 60
        for unit in ("U1", "U2"):
            self.svc.create_paper_version("HM", unit, "v1", "2025-09-01", pass_mark=60)

    def tearDown(self) -> None:
        self.store.close()

    def paper(self, unit: str) -> str:
        return next(
            r["id"] for r in self.svc.list_paper_versions("HM") if r["unit_code"] == unit
        )


class ScoreAndRetakeTest(ServiceTestBase):
    def test_total_score_does_not_overwrite_history(self) -> None:
        p1 = self.paper("U1")
        first = self.svc.record_score("v001", "HM", "U1", p1, "examiner-a", 45, "2026-01-10")
        self.assertEqual(first["attempt_no"], 1)
        self.assertFalse(first["pass"])

        # 第二次成绩必须先有补考资格
        with self.assertRaises(DomainError):
            self.svc.record_score("v001", "HM", "U1", p1, "examiner-a", 80, "2026-02-10")

        self.svc.grant_retake_eligibility("v001", "HM", "U1", 1, reason="首次不及格")
        second = self.svc.record_score("v001", "HM", "U1", p1, "examiner-b", 80, "2026-02-10")
        self.assertEqual(second["attempt_no"], 2)
        self.assertTrue(second["pass"])
        self.assertEqual(second["retake_remaining"], 0)

        # 补考配额用尽，第三次被拒绝
        with self.assertRaises(DomainError):
            self.svc.record_score("v001", "HM", "U1", p1, "examiner-b", 90, "2026-03-10")

        history = self.svc.score_history("v001", "HM", "U1")
        self.assertEqual(len(history), 2)  # 旧结果保留，不被总分覆盖
        self.assertEqual([h["attempt_no"] for h in history], [1, 2])
        self.assertEqual([h["score"] for h in history], [45, 80])
        self.assertEqual([h["examiner_id"] for h in history], ["examiner-a", "examiner-b"])

    def test_paper_must_match_unit(self) -> None:
        p2 = self.paper("U2")
        with self.assertRaises(DomainError):
            self.svc.record_score("v001", "HM", "U1", p2, "e1", 90, "2026-01-10")


class CertificateEvidenceTest(ServiceTestBase):
    def _pass(self, volunteer: str, unit: str, score: int, day: str, examiner: str = "e1"):
        return self.svc.record_score(
            volunteer, "HM", unit, self.paper(unit), examiner, score, day
        )

    def test_claims_fix_first_pass_vs_retake_pass(self) -> None:
        # U1 首次通过；U2 补考通过
        self._pass("v001", "U1", 88, "2026-01-10", "examiner-a")
        self.svc.record_score("v001", "HM", "U2", self.paper("U2"), "e1", 30, "2026-01-10")
        self.svc.grant_retake_eligibility("v001", "HM", "U2", 1)
        self.svc.record_score("v001", "HM", "U2", self.paper("U2"), "examiner-b", 75, "2026-02-10")

        cert = self.svc.issue_certificate("v001", "HM", issue_date="2026-02-15")
        by_unit = {c["unit_code"]: c for c in cert["claims"]}
        self.assertEqual(by_unit["U1"]["basis"], "first_pass")
        self.assertEqual(by_unit["U2"]["basis"], "retake_pass")

        stored = self.svc.get_certificate(cert["certificate_id"])
        hashes_at_issue = {c["unit_code"]: c["evidence_hash"] for c in stored["claims"]}

        # 发证后再次补考刷更高总分：已发证书的证据组合不变
        self.svc.grant_retake_eligibility("v001", "HM", "U1", 1)
        self.svc.record_score("v001", "HM", "U1", self.paper("U1"), "e2", 100, "2026-03-10")
        stored2 = self.svc.get_certificate(cert["certificate_id"])
        hashes_after = {c["unit_code"]: c["evidence_hash"] for c in stored2["claims"]}
        self.assertEqual(hashes_at_issue, hashes_after)

        rep = self.svc.verify_certificate(cert["certificate_id"], "2026-02-20", "HM")
        self.assertTrue(rep["overall_valid"])
        self.assertEqual(set(rep["valid_scope"]["units"]), {"U1", "U2"})

    def test_evidence_hash_mismatch_is_detected(self) -> None:
        self._pass("v001", "U1", 88, "2026-01-10")
        self._pass("v001", "U2", 70, "2026-01-11")
        cert = self.svc.issue_certificate("v001", "HM", issue_date="2026-01-20")

        # 库外直接篡改成绩表（绕过服务与链）：证据哈希复核必须发现
        self.svc.store.execute("UPDATE unit_score SET score=15 WHERE volunteer_id='v001' AND unit_code='U1'")
        rep = self.svc.verify_certificate(cert["certificate_id"], "2026-01-21")
        u1 = next(c for c in rep["claims"] if c["unit_code"] == "U1")
        self.assertFalse(u1["valid"])
        self.assertIn("证据哈希不匹配", u1["reasons"])
        self.assertFalse(rep["overall_valid"])

    def test_missing_evidence_blocks_issuance(self) -> None:
        self._pass("v001", "U1", 88, "2026-01-10")
        with self.assertRaises(DomainError):
            self.svc.issue_certificate("v001", "HM", issue_date="2026-01-20")

    def test_duplicate_and_concurrent_issuance(self) -> None:
        self._pass("v001", "U1", 88, "2026-01-10")
        self._pass("v001", "U2", 70, "2026-01-11")

        first = self.svc.issue_certificate(
            "v001", "HM", issue_date="2026-01-20", idem_key="key-1"
        )
        # 同幂等键重放：返回同一证书
        again = self.svc.issue_certificate(
            "v001", "HM", issue_date="2026-01-20", idem_key="key-1"
        )
        self.assertEqual(first["certificate_id"], again["certificate_id"])
        self.assertFalse(again["duplicate"])

        # 不同幂等键但同证据组合：标记为重复，不产生第二张证
        dup = self.svc.issue_certificate(
            "v001", "HM", issue_date="2026-01-20", idem_key="key-2"
        )
        self.assertTrue(dup["duplicate"])
        self.assertEqual(dup["certificate_id"], first["certificate_id"])

        # 并发发证：只有一张真实证书
        results: list[dict] = []
        errors: list[Exception] = []

        def worker() -> None:
            try:
                results.append(self.svc.issue_certificate(
                    "v009", "HM", issue_date="2026-01-20"
                ))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        self._pass("v009", "U1", 65, "2026-01-10")
        self._pass("v009", "U2", 65, "2026-01-11")
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        ids = {r["certificate_id"] for r in results if not r["duplicate"]}
        self.assertEqual(len(ids), 1)
        self.assertEqual(sum(not r["duplicate"] for r in results), 1)
        self.assertEqual(sum(r["duplicate"] for r in results), 7)

        count = self.svc.store.query_one(
            "SELECT COUNT(*) AS n FROM certificate WHERE volunteer_id='v009'"
        )
        self.assertEqual(count["n"], 1)


class RevocationSuspensionSubstitutionTest(ServiceTestBase):
    def _issue_two_unit_cert(self, volunteer: str = "v001") -> str:
        self.svc.record_score(volunteer, "HM", "U1", self.paper("U1"), "e1", 80, "2026-01-10")
        self.svc.record_score(volunteer, "HM", "U2", self.paper("U2"), "e1", 70, "2026-01-11")
        return self.svc.issue_certificate(volunteer, "HM", issue_date="2026-01-20")["certificate_id"]

    def test_revocation_invalidates_from_effective_date(self) -> None:
        cert_id = self._issue_two_unit_cert()
        u1_score = self.svc.score_history("v001", "HM", "U1")[0]["id"]

        # 撤销生效日 2026-03-01：之前仍有效，当日起 U1 失效
        self.svc.revoke_score(u1_score, reason="考官资格存疑", revoked_on="2026-03-01")
        before = self.svc.verify_certificate(cert_id, "2026-02-28")
        self.assertTrue(before["overall_valid"])

        after = self.svc.verify_certificate(cert_id, "2026-03-01")
        self.assertFalse(after["overall_valid"])
        u1 = next(c for c in after["claims"] if c["unit_code"] == "U1")
        self.assertFalse(u1["valid"])
        self.assertIn("撤销", u1["reasons"][0])
        u2 = next(c for c in after["claims"] if c["unit_code"] == "U2")
        self.assertTrue(u2["valid"])

        # 撤销不可重复
        with self.assertRaises(DomainError):
            self.svc.revoke_score(u1_score, reason="再次撤销", revoked_on="2026-03-02")

    def test_suspension_intervals(self) -> None:
        cert_id = self._issue_two_unit_cert()
        self.svc.suspend_certificate(cert_id, reason="调查中", suspend_from="2026-03-01")
        self.svc.resume_certificate(cert_id, resume_on="2026-06-01")

        rep_before = self.svc.verify_certificate(cert_id, "2026-02-28")
        rep_during = self.svc.verify_certificate(cert_id, "2026-04-01")
        rep_resume_day = self.svc.verify_certificate(cert_id, "2026-06-01")
        self.assertTrue(rep_before["overall_valid"])
        self.assertFalse(rep_during["overall_valid"])
        self.assertTrue(all("暂停期" in "".join(c["reasons"]) for c in rep_during["claims"]))
        self.assertTrue(rep_resume_day["overall_valid"])
        self.assertEqual(
            rep_resume_day["suspension_intervals"], [{"from": "2026-03-01", "to": "2026-06-01"}]
        )

        # 开放式暂停（未恢复）持续失效
        self.svc.suspend_certificate(cert_id, reason="复查", suspend_from="2026-09-01")
        self.assertFalse(self.svc.verify_certificate(cert_id, "2026-10-01")["overall_valid"])

    def test_validity_window(self) -> None:
        cert_id = self._issue_two_unit_cert()
        early = self.svc.verify_certificate(cert_id, "2026-01-09")
        self.assertFalse(early["overall_valid"])
        self.assertIn("尚未生效", "".join(r for c in early["claims"] for r in c["reasons"]))

        # 默认 24 个月：U1 考试日 2026-01-10 → 有效至 2028-01-10
        expired = self.svc.verify_certificate(cert_id, "2028-01-11")
        self.assertFalse(expired["overall_valid"])
        last_day = self.svc.verify_certificate(cert_id, "2028-01-10")
        self.assertTrue(last_day["overall_valid"])

    def test_substitution_respected_by_date(self) -> None:
        self.svc.create_paper_version("HM", "U3", "v1", "2025-09-01", pass_mark=60)
        self.svc.record_score("v007", "HM", "U3", self.paper("U3"), "e1", 90, "2026-01-15")

        # 生效日之前发证：U1 无证据
        self.svc.define_substitution("HM", "U1", "U3", effective_from="2026-02-01")
        with self.assertRaises(DomainError):
            self.svc.issue_certificate("v007", "HM", units=["U1"], issue_date="2026-01-31")

        cert = self.svc.issue_certificate("v007", "HM", units=["U1"], issue_date="2026-02-01")
        claim = cert["claims"][0]
        self.assertEqual(claim["basis"], "substituted")
        stored = self.svc.get_certificate(cert["certificate_id"])
        self.assertEqual(stored["claims"][0]["substituted_from"], "U3")

        rep = self.svc.verify_certificate(cert["certificate_id"], "2026-02-02")
        self.assertTrue(rep["overall_valid"])
        self.assertEqual(rep["valid_scope"]["units"], ["U1"])

    def test_transitive_substitution_and_cycle(self) -> None:
        self.svc.create_paper_version("HM", "U3", "v1", "2025-09-01", pass_mark=60)
        self.svc.create_paper_version("HM", "U4", "v1", "2025-09-01", pass_mark=60)
        self.svc.record_score("v008", "HM", "U4", self.paper("U4"), "e1", 95, "2026-01-15")
        # U1 -> U3 -> U4，志愿者只有 U4 成绩
        self.svc.define_substitution("HM", "U1", "U3", effective_from="2026-02-01")
        self.svc.define_substitution("HM", "U3", "U4", effective_from="2026-02-01")

        cert = self.svc.issue_certificate(
            "v008", "HM", units=["U1"], issue_date="2026-02-05"
        )
        claim = cert["claims"][0]
        self.assertEqual(claim["basis"], "substituted")
        stored = self.svc.get_certificate(cert["certificate_id"])
        self.assertEqual(stored["claims"][0]["substituted_from"], "U3")
        self.assertTrue(
            self.svc.verify_certificate(cert["certificate_id"], "2026-02-06")["overall_valid"]
        )

        # 纯粹的替代环且环中无任何通过成绩：必须安全失败而非死循环
        self.svc.define_substitution("CYC", "X1", "X2", effective_from="2026-02-01")
        self.svc.define_substitution("CYC", "X2", "X1", effective_from="2026-02-01")
        with self.assertRaises(DomainError):
            self.svc.issue_certificate(
                "v010", "CYC", units=["X1"], issue_date="2026-02-05"
            )

    def test_topic_mismatch(self) -> None:
        cert_id = self._issue_two_unit_cert()
        with self.assertRaises(DomainError):
            self.svc.verify_certificate(cert_id, "2026-02-01", topic_code="OTHER")


class ChainTest(ServiceTestBase):
    def test_chain_links_all_mutations_and_verifies(self) -> None:
        p1 = self.paper("U1")
        self.svc.record_score("v001", "HM", "U1", p1, "e1", 80, "2026-01-10")
        self.svc.grant_retake_eligibility("v001", "HM", "U2", 1)
        self.svc.define_substitution("HM", "U2", "U1", "2026-02-01")
        self.svc.record_score("v001", "HM", "U2", self.paper("U2"), "e1", 50, "2026-01-11")
        score2 = self.svc.score_history("v001", "HM", "U2")[0]["id"]
        self.svc.revoke_score(score2, reason="误录", revoked_on="2026-01-12")
        cert_id = self.svc.issue_certificate(
            "v001", "HM", units=["U1"], issue_date="2026-02-05"
        )["certificate_id"]
        self.svc.suspend_certificate(cert_id, "x", "2026-03-01")
        self.svc.resume_certificate(cert_id, "2026-04-01")

        report = self.svc.chain_report()
        self.assertTrue(report["ok"])
        types = [e["event_type"] for e in self.svc.list_events()]
        self.assertTrue(types)
        for expected in (
            "paper_published", "score_recorded", "retake_granted",
            "substitution_defined", "score_revoked", "certificate_issued",
            "certificate_suspended", "certificate_resumed",
        ):
            self.assertIn(expected, types)

    def test_tampered_event_breaks_chain(self) -> None:
        self.svc.record_score("v001", "HM", "U1", self.paper("U1"), "e1", 80, "2026-01-10")
        self.assertTrue(self.svc.chain_report()["ok"])

        # 直接改写链载荷
        self.svc.store.execute(
            "UPDATE immutable_event SET payload_json=? WHERE seq=2",
            ('{"tampered": true}',),
        )
        report = self.svc.chain_report()
        self.assertFalse(report["ok"])
        self.assertEqual(report["broken_at"], 2)

    def test_deleted_event_breaks_chain(self) -> None:
        self.svc.record_score("v001", "HM", "U1", self.paper("U1"), "e1", 80, "2026-01-10")
        self.svc.record_score("v001", "HM", "U2", self.paper("U2"), "e1", 80, "2026-01-11")
        self.svc.store.execute("DELETE FROM immutable_event WHERE seq=2")
        report = self.svc.chain_report()
        self.assertFalse(report["ok"])


if __name__ == "__main__":
    unittest.main()
