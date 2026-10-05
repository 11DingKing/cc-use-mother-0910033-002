"""HTTP 端到端测试：真实 ThreadingHTTPServer + urllib，含并发发证。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from training_cert.app import make_server


class HttpApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.httpd = make_server("127.0.0.1", 0, ":memory:")
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.store.close()

    def call(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method
        )
        req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_lifecycle_over_http(self) -> None:
        for unit in ("U1", "U2"):
            status, _ = self.call("POST", "/api/papers", {
                "topic_code": "HM", "unit_code": unit, "version_tag": "v1",
                "published_at": "2025-09-01", "pass_mark": 60,
            })
            self.assertEqual(status, 201)

        papers, papers_body = self.call("GET", "/api/papers?topic_code=HM")
        self.assertEqual(papers, 200)
        paper_u1 = next(p["id"] for p in papers_body if p["unit_code"] == "U1")
        paper_u2 = next(p["id"] for p in papers_body if p["unit_code"] == "U2")

        # U1 首考通过
        status, first = self.call("POST", "/api/scores", {
            "volunteer_id": "v001", "topic_code": "HM", "unit_code": "U1",
            "paper_id": paper_u1, "examiner_id": "examiner-a",
            "score": 88, "exam_date": "2026-01-10",
        })
        self.assertEqual(status, 201)
        self.assertEqual(first["attempt_no"], 1)

        # U2 首考不过，无资格直接补考 → 422
        self.call("POST", "/api/scores", {
            "volunteer_id": "v001", "topic_code": "HM", "unit_code": "U2",
            "paper_id": paper_u2, "examiner_id": "e1", "score": 40, "exam_date": "2026-01-10",
        })
        status, err = self.call("POST", "/api/scores", {
            "volunteer_id": "v001", "topic_code": "HM", "unit_code": "U2",
            "paper_id": paper_u2, "examiner_id": "examiner-b",
            "score": 75, "exam_date": "2026-02-10",
        })
        self.assertEqual(status, 422)
        self.assertIn("补考资格", err["error"])

        self.assertEqual(201, self.call("POST", "/api/retake-eligibility", {
            "volunteer_id": "v001", "topic_code": "HM", "unit_code": "U2",
            "max_extra_attempts": 1, "reason": "首考不及格",
        })[0])
        status, retake = self.call("POST", "/api/scores", {
            "volunteer_id": "v001", "topic_code": "HM", "unit_code": "U2",
            "paper_id": paper_u2, "examiner_id": "examiner-b",
            "score": 75, "exam_date": "2026-02-10",
        })
        self.assertEqual(status, 201)
        self.assertEqual(retake["attempt_no"], 2)

        # 证据不足发证 → 422
        self.assertEqual(422, self.call("POST", "/api/certificates", {
            "volunteer_id": "v001", "topic_code": "HM", "units": ["U1", "U9"],
            "issue_date": "2026-02-15",
        })[0])

        status, cert = self.call("POST", "/api/certificates", {
            "volunteer_id": "v001", "topic_code": "HM", "issue_date": "2026-02-15",
        }, headers={"Idempotency-Key": "issue-v001-1"})
        self.assertEqual(status, 201)
        cert_id = cert["certificate_id"]
        bases = {c["unit_code"]: c["basis"] for c in cert["claims"]}
        self.assertEqual(bases, {"U1": "first_pass", "U2": "retake_pass"})

        # 同 Idempotency-Key 重放
        status, replay = self.call("POST", "/api/certificates", {
            "volunteer_id": "v001", "topic_code": "HM", "issue_date": "2026-02-15",
        }, headers={"Idempotency-Key": "issue-v001-1"})
        self.assertEqual(status, 201)
        self.assertEqual(replay["certificate_id"], cert_id)

        # 指定日期验证
        status, rep = self.call("GET", f"/api/certificates/{cert_id}/verify?on_date=2026-03-01&topic_code=HM")
        self.assertEqual(status, 200)
        self.assertTrue(rep["overall_valid"])
        self.assertTrue(rep["chain_ok"])
        self.assertEqual(set(rep["valid_scope"]["units"]), {"U1", "U2"})

        # 暂停期间失效，恢复后有效
        self.assertEqual(200, self.call("POST", f"/api/certificates/{cert_id}/suspend", {
            "reason": "投诉调查", "suspend_from": "2026-05-01",
        })[0])
        rep_susp = self.call("GET", f"/api/certificates/{cert_id}/verify?on_date=2026-05-02")[1]
        self.assertFalse(rep_susp["overall_valid"])
        self.assertEqual(200, self.call("POST", f"/api/certificates/{cert_id}/resume", {
            "resume_on": "2026-06-01",
        })[0])
        self.assertTrue(
            self.call("GET", f"/api/certificates/{cert_id}/verify?on_date=2026-06-01")[1]["overall_valid"]
        )

        # 链与事件可查
        chain = self.call("GET", "/api/chain")[1]
        self.assertTrue(chain["ok"])
        self.assertGreaterEqual(chain["total"], 6)
        events = self.call("GET", "/api/events")[1]
        self.assertIn("certificate_suspended", [e["event_type"] for e in events])

        # 参数错误
        self.assertEqual(400, self.call("POST", "/api/papers", {"topic_code": "HM"})[0])

    def test_concurrent_issue_over_http(self) -> None:
        self.call("POST", "/api/papers", {
            "topic_code": "HM", "unit_code": "U1", "version_tag": "v1",
            "published_at": "2025-09-01", "pass_mark": 60,
        })
        papers = self.call("GET", "/api/papers?topic_code=HM")[1]
        self.call("POST", "/api/scores", {
            "volunteer_id": "v009", "topic_code": "HM", "unit_code": "U1",
            "paper_id": papers[0]["id"], "examiner_id": "e1",
            "score": 90, "exam_date": "2026-01-10",
        })

        results: list[tuple[int, dict]] = []

        def worker() -> None:
            results.append(self.call("POST", "/api/certificates", {
                "volunteer_id": "v009", "topic_code": "HM",
                "units": ["U1"], "issue_date": "2026-01-20",
            }))

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertTrue(all(s == 201 for s, _ in results))
        ids = {b["certificate_id"] for _, b in results}
        self.assertEqual(len(ids), 1)
        self.assertEqual(sum(1 for _, b in results if b["duplicate"]), 9)


if __name__ == "__main__":
    unittest.main()
