"""HTTP 接口：仅依赖标准库 http.server。

路由：
  POST   /api/papers                     登记试卷版本
  GET    /api/papers?topic_code=...
  POST   /api/scores                     记录单元成绩（只追加）
  GET    /api/scores?volunteer_id&topic_code&unit_code  成绩历史
  POST   /api/scores/<id>/revoke         撤销成绩
  POST   /api/retake-eligibility         授予补考资格（次数限制）
  POST   /api/substitutions              定义单元替代
  POST   /api/certificates               发证（可用 Idempotency-Key 头）
  GET    /api/certificates/<id>          查看证书与固定证据
  GET    /api/certificates/<id>/verify?on_date=&topic_code=  验证有效范围
  POST   /api/certificates/<id>/suspend
  POST   /api/certificates/<id>/resume
  GET    /api/events                     不可变链事件
  GET    /api/chain                      链完整性校验报告
  GET    /healthz
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .service import CertService, DomainError
from .store import Store


def _required(body: dict, keys: list[str]) -> None:
    missing = [k for k in keys if body.get(k) in (None, "")]
    if missing:
        raise DomainError("缺少必填字段：" + "、".join(missing), 400)


class CertHandler(BaseHTTPRequestHandler):
    server_version = "TrainingCert/1.0"

    # 由 make_server 注入
    service: CertService = None  # type: ignore[assignment]

    def log_message(self, fmt: str, *args) -> None:  # 静默，保持测试输出干净
        return

    # ------------------------------------------------------------------ #
    def _send_json(self, status: int, payload: dict | list) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise DomainError("请求体不是合法 JSON", 400)
        if not isinstance(value, dict):
            raise DomainError("请求体必须是 JSON 对象", 400)
        return value

    def _query(self) -> dict:
        parsed = parse_qs(urlsplit(self.path).query)
        return {k: v[-1] for k, v in parsed.items()}

    # ------------------------------------------------------------------ #
    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path.rstrip("/") or "/"
        try:
            if path == "/healthz":
                self._send_json(200, {"ok": True})
            elif path == "/api/papers":
                q = self._query()
                _required(q, ["topic_code"])
                self._send_json(200, self.service.list_paper_versions(q["topic_code"]))
            elif path == "/api/scores":
                q = self._query()
                _required(q, ["volunteer_id", "topic_code", "unit_code"])
                self._send_json(
                    200,
                    self.service.score_history(q["volunteer_id"], q["topic_code"], q["unit_code"]),
                )
            elif path == "/api/events":
                self._send_json(200, self.service.list_events(int(self._query().get("limit", 100))))
            elif path == "/api/chain":
                self._send_json(200, self.service.chain_report())
            else:
                m = re.fullmatch(r"/api/certificates/([A-Za-z0-9_\-]+)/verify", path)
                if m:
                    q = self._query()
                    self._send_json(
                        200,
                        self.service.verify_certificate(
                            m.group(1), q.get("on_date"), q.get("topic_code")
                        ),
                    )
                    return
                m = re.fullmatch(r"/api/certificates/([A-Za-z0-9_\-]+)", path)
                if m:
                    self._send_json(200, self.service.get_certificate(m.group(1)))
                    return
                self._send_json(404, {"error": "not found", "path": path})
        except DomainError as exc:
            self._send_json(exc.status, {"error": str(exc)})
        except Exception as exc:  # 防御性兜底
            self._send_json(500, {"error": f"服务器内部错误：{exc}"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path.rstrip("/") or "/"
        try:
            body = self._read_body()
            svc = self.service

            if path == "/api/papers":
                _required(body, ["topic_code", "unit_code", "version_tag", "published_at"])
                self._send_json(
                    201,
                    svc.create_paper_version(
                        body["topic_code"], body["unit_code"], body["version_tag"],
                        body["published_at"], int(body.get("pass_mark", 60)),
                    ),
                )
            elif path == "/api/scores":
                _required(
                    body,
                    ["volunteer_id", "topic_code", "unit_code", "paper_id",
                     "examiner_id", "score", "exam_date"],
                )
                self._send_json(
                    201,
                    svc.record_score(
                        body["volunteer_id"], body["topic_code"], body["unit_code"],
                        body["paper_id"], body["examiner_id"], int(body["score"]),
                        body["exam_date"], body.get("override_pass"),
                    ),
                )
            elif path == "/api/retake-eligibility":
                _required(
                    body, ["volunteer_id", "topic_code", "unit_code", "max_extra_attempts"]
                )
                self._send_json(
                    201,
                    svc.grant_retake_eligibility(
                        body["volunteer_id"], body["topic_code"], body["unit_code"],
                        int(body["max_extra_attempts"]), body.get("reason", ""),
                    ),
                )
            elif path == "/api/substitutions":
                _required(body, ["topic_code", "old_unit", "new_unit", "effective_from"])
                self._send_json(201, svc.define_substitution(
                    body["topic_code"], body["old_unit"], body["new_unit"],
                    body["effective_from"],
                ))
            elif path == "/api/certificates":
                _required(body, ["volunteer_id", "topic_code"])
                self._send_json(
                    201,
                    svc.issue_certificate(
                        body["volunteer_id"], body["topic_code"], body.get("units"),
                        body.get("issue_date"), int(body.get("validity_months", 24)),
                        self.headers.get("Idempotency-Key") or body.get("idem_key"),
                    ),
                )
            else:
                m = re.fullmatch(r"/api/scores/([A-Za-z0-9_\-]+)/revoke", path)
                if m:
                    _required(body, ["reason"])
                    self._send_json(
                        200, svc.revoke_score(m.group(1), body["reason"], body.get("revoked_on"))
                    )
                    return
                m = re.fullmatch(r"/api/certificates/([A-Za-z0-9_\-]+)/suspend", path)
                if m:
                    _required(body, ["reason"])
                    self._send_json(
                        200,
                        svc.suspend_certificate(m.group(1), body["reason"], body.get("suspend_from")),
                    )
                    return
                m = re.fullmatch(r"/api/certificates/([A-Za-z0-9_\-]+)/resume", path)
                if m:
                    self._send_json(200, svc.resume_certificate(m.group(1), body.get("resume_on")))
                    return
                self._send_json(404, {"error": "not found", "path": path})
        except DomainError as exc:
            self._send_json(exc.status, {"error": str(exc)})
        except (ValueError, TypeError) as exc:
            self._send_json(400, {"error": f"参数错误：{exc}"})
        except Exception as exc:
            self._send_json(500, {"error": f"服务器内部错误：{exc}"})


def make_server(host: str, port: int, db_path: str = ":memory:") -> ThreadingHTTPServer:
    store = Store(db_path)
    service = CertService(store)

    class _BoundHandler(CertHandler):
        pass

    _BoundHandler.service = service
    httpd = ThreadingHTTPServer((host, port), _BoundHandler)
    httpd.store = store  # type: ignore[attr-defined]
    httpd.service = service  # type: ignore[attr-defined]
    return httpd


def main() -> None:
    import argparse
    import os

    parser = argparse.ArgumentParser(description="培训补考证书管理服务端")
    parser.add_argument("--host", default=os.environ.get("CERT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CERT_PORT", "8080")))
    parser.add_argument("--db", default=os.environ.get("CERT_DB", "training_cert.db"))
    args = parser.parse_args()

    httpd = make_server(args.host, args.port, args.db)
    print(f"培训补考证书服务监听 http://{args.host}:{args.port} （数据库 {args.db}）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
        httpd.store.close()


if __name__ == "__main__":
    main()
