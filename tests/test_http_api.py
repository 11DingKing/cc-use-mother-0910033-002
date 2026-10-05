"""HTTP 接口端到端测试。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from training_certs.http_api import serve


class HttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db = str(Path(self.tmp.name) / "http.db")
        self.httpd = serve(db, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _request(self, method: str, path: str, body: dict | None = None):
        from urllib.parse import quote, urlsplit, urlunsplit

        parts = urlsplit(path)
        encoded_path = quote(parts.path)
        encoded_query = quote(parts.query, safe="=&")
        encoded = urlunsplit(("", "", encoded_path, encoded_query, ""))
        url = f"http://127.0.0.1:{self.port}{encoded}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_workflow_over_http(self) -> None:
        self.assertEqual(self._request("POST", "/volunteers",
                                       {"volunteer_id": "V1", "name": "张三"})[0], 201)
        self.assertEqual(self._request("POST", "/units",
                                       {"unit_code": "U-A", "topic": "急救"})[0], 201)
        self.assertEqual(self._request("POST", "/papers",
                                       {"unit_code": "U-A", "created_by": "admin",
                                        "paper_id": "P-A1"})[0], 201)
        # 首考不过
        self.assertEqual(self._request("POST", "/scores", {
            "volunteer_id": "V1", "unit_code": "U-A", "paper_id": "P-A1",
            "score": 50, "examiner_id": "EX1", "exam_date": "2026-03-01",
        })[0], 201)
        # 无资格补考 -> 400
        status, body = self._request("POST", "/scores", {
            "volunteer_id": "V1", "unit_code": "U-A", "paper_id": "P-A1",
            "score": 80, "examiner_id": "EX1", "exam_date": "2026-03-05",
        })
        self.assertEqual(status, 400)
        self.assertIn("补考资格", body["error"])

        self.assertEqual(self._request("POST", "/retake-eligibility", {
            "volunteer_id": "V1", "unit_code": "U-A", "max_retakes": 1,
            "by": "dean", "reason": "结业补考",
        })[0], 201)
        status, retake = self._request("POST", "/scores", {
            "volunteer_id": "V1", "unit_code": "U-A", "paper_id": "P-A1",
            "score": 82, "examiner_id": "EX2", "exam_date": "2026-03-05",
        })
        self.assertEqual(status, 201)
        self.assertEqual(retake["kind"], "retake")

        # 发证
        status, issued = self._request("POST", "/certificates", {
            "volunteer_id": "V1", "topic": "急救", "by": "dean",
            "issued_on": "2026-04-01",
        })
        self.assertEqual(status, 201)
        cert_no = issued["cert_no"]

        # 重复发证 -> 409
        status, body = self._request("POST", "/certificates", {
            "volunteer_id": "V1", "topic": "急救", "by": "dean",
            "issued_on": "2026-04-02",
        })
        self.assertEqual(status, 409)

        # 验证：补考通过单元在有效期内
        status, check = self._request("GET", f"/certificates/{cert_no}/verify?date=2026-05-01")
        self.assertEqual(status, 200)
        self.assertEqual(check["lifecycle"], "valid")
        self.assertEqual(check["valid_scope"], ["U-A"])
        self.assertEqual(check["scope"][0]["pass_mode"], "retake_pass")
        self.assertTrue(check["evidence_intact"])

        # 暂停后验证为 suspended，恢复后重新 valid
        self.assertEqual(self._request("POST", f"/certificates/{cert_no}/suspend", {
            "from_date": "2026-06-01", "reason": "投诉调查",
        })[0], 200)
        _, check = self._request("GET", f"/certificates/{cert_no}/verify?date=2026-06-10")
        self.assertEqual(check["lifecycle"], "suspended")
        self.assertEqual(check["valid_scope"], [])

        # 主题不符 -> 400
        status, body = self._request(
            "GET", f"/certificates/{cert_no}/verify?date=2026-06-10&topic=消防"
        )
        self.assertEqual(status, 400)

        # 链自检
        status, chain = self._request("GET", "/chain")
        self.assertEqual(status, 200)
        self.assertTrue(chain["intact"])

        # 错误处理：缺字段、非法 JSON、不存在资源
        status, body = self._request("POST", "/scores", {"volunteer_id": "V1"})
        self.assertEqual(status, 400)
        self.assertIn("unit_code", body["error"])
        self.assertEqual(self._request("GET", "/certificates/NOPE/verify?date=2026-01-01")[0], 404)

    def test_verify_requires_date_param(self) -> None:
        status, body = self._request("GET", "/certificates/x/verify")
        self.assertEqual(status, 400)
        self.assertIn("date", body["error"])


if __name__ == "__main__":
    unittest.main()
