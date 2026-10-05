"""并发测试：多线程同时发证 / 记录成绩时，唯一约束必须兜底。"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from training_certs.errors import ConflictError
from training_certs.service import TrainingService


class ConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "concurrent.db")
        self.svc = TrainingService(self.db)
        self.svc.register_volunteer("V1", "张三")
        self.svc.register_unit("U-A", "急救")
        self.svc.register_paper("U-A", "admin", paper_id="P-A1")
        self.svc.record_score("V1", "U-A", "P-A1", 90, "EX1", "2026-03-01")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_concurrent_certificate_issues_exactly_one_wins(self) -> None:
        results: list[object] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(8)

        def issue() -> None:
            try:
                barrier.wait()
                r = TrainingService(self.db).issue_certificate(
                    "V1", "急救", "dean", "2026-04-01"
                )
                results.append(r["cert_no"])
            except Exception as exc:  # noqa: BLE001 - 测试要收集所有线程结果
                errors.append(exc)

        threads = [threading.Thread(target=issue) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 1, f"应只成功一张，实际 {len(results)}：{results}")
        self.assertEqual(len(errors), 7)
        self.assertTrue(all(isinstance(e, ConflictError) for e in errors), errors)
        # 账本中也只有一张证书事件
        import sqlite3

        conn = sqlite3.connect(self.db)
        count = conn.execute(
            "SELECT COUNT(*) FROM ledger WHERE event_type = 'CertificateIssued'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)
        # 哈希链仍完整
        self.assertTrue(self.svc.verify_chain()["intact"])

    def test_concurrent_retake_recording_respects_quota(self) -> None:
        # 只有 1 次补考名额：5 个线程抢同一批次，
        # 串行化后仅 1 个成功，其余拿到名额耗尽冲突。
        self.svc.grant_retake_eligibility("V1", "U-A", 1, "dean")
        outcomes: dict[str, int] = {"ok": 0, "conflict": 0}
        lock = threading.Lock()
        barrier = threading.Barrier(5)

        def record() -> None:
            try:
                barrier.wait()
                TrainingService(self.db).record_score(
                    "V1", "U-A", "P-A1", 70, "EX1", "2026-03-02"
                )
                with lock:
                    outcomes["ok"] += 1
            except ConflictError:
                with lock:
                    outcomes["conflict"] += 1

        threads = [threading.Thread(target=record) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 第二次尝试（attempt_no=2）只能落一条
        self.assertEqual(outcomes["ok"], 1)
        self.assertEqual(outcomes["conflict"], 4)
        history = self.svc.volunteer_history("V1")["units"]["U-A"]
        self.assertEqual(len(history), 2)
        self.assertTrue(self.svc.verify_chain()["intact"])


if __name__ == "__main__":
    unittest.main()
