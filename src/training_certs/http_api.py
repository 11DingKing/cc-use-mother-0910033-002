"""HTTP JSON 接口（标准库实现，无第三方依赖）。

路由：
  POST /volunteers                 登记志愿者
  POST /units                      登记单元
  POST /papers                     登记试卷版本
  POST /retake-eligibility         授予补考资格
  POST /scores                     记录单元成绩
  POST /scores/{score_id}/revoke   撤销成绩
  POST /unit-replacements          定义单元替代
  POST /certificates               签发证书（并发唯一）
  POST /certificates/{cert_no}/suspend
  POST /certificates/{cert_no}/resume
  POST /certificates/{cert_no}/revoke
  GET  /certificates/{cert_no}     证书详情（含固定声明与锚点）
  GET  /certificates/{cert_no}/verify?date=YYYY-MM-DD[&topic=...]
  GET  /volunteers/{volunteer_id}/history
  GET  /chain                      哈希链完整性自检
"""
from __future__ import annotations

import json
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import unquote, urlparse, parse_qs

from .errors import DomainError
from .service import TrainingService


def _require(body: dict, key: str) -> Any:
    if key not in body or body[key] in (None, ""):
        raise DomainError(f"缺少必填字段：{key}")
    return body[key]


def make_handler(service: TrainingService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "TrainingCerts/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # 安静模式
            pass

        def _send(self, status: int, obj: Any) -> None:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
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
                body = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise DomainError("请求体必须是合法 UTF-8 JSON") from exc
            if not isinstance(body, dict):
                raise DomainError("请求体必须是 JSON 对象")
            return body

        def _handle(self, dispatch: dict[tuple[str, re.Pattern], Callable]) -> None:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            handler_fn = None
            groups: tuple[str, ...] = ()
            for (method, pattern), fn in dispatch.items():
                if method != self.command:
                    continue
                m = pattern.fullmatch(path)
                if m:
                    handler_fn, groups = fn, m.groups()
                    break
            if handler_fn is None:
                if any(m == self.command for (m, _), _ in dispatch.items()):
                    self._send(404, {"error": "资源不存在", "path": path})
                else:
                    self._send(405, {"error": "方法不允许", "method": self.command})
                return
            try:
                body = self._read_body() if self.command == "POST" else {}
                query = parse_qs(parsed.query)
                status, result = handler_fn(body, query, *groups)
                self._send(status, result)
            except DomainError as exc:
                self._send(exc.status, {"error": str(exc)})
            except ValueError as exc:
                # 日期/数值格式等输入校验
                self._send(400, {"error": str(exc)})
            except sqlite3.OperationalError as exc:
                # 并发下锁等待耗尽等
                self._send(409, {"error": f"数据库暂时不可用，请重试：{exc}"})

    def register_volunteer(body, query):
        return 201, service.register_volunteer(
            volunteer_id=_require(body, "volunteer_id"),
            name=body.get("name", ""),
        )

    def register_unit(body, query):
        return 201, service.register_unit(
            unit_code=_require(body, "unit_code"),
            topic=_require(body, "topic"),
            title=body.get("title", ""),
        )

    def register_paper(body, query):
        return 201, service.register_paper(
            unit_code=_require(body, "unit_code"),
            created_by=_require(body, "created_by"),
            pass_score=float(body.get("pass_score", 60.0)),
            paper_id=body.get("paper_id"),
            content_ref=body.get("content_ref"),
            published_at=body.get("published_at"),
        )

    def grant(body, query):
        return 201, service.grant_retake_eligibility(
            volunteer_id=_require(body, "volunteer_id"),
            unit_code=_require(body, "unit_code"),
            max_retakes=int(_require(body, "max_retakes")),
            by=_require(body, "by"),
            reason=body.get("reason", ""),
        )

    def record_score(body, query):
        return 201, service.record_score(
            volunteer_id=_require(body, "volunteer_id"),
            unit_code=_require(body, "unit_code"),
            paper_id=_require(body, "paper_id"),
            score=float(_require(body, "score")),
            examiner_id=_require(body, "examiner_id"),
            exam_date=_require(body, "exam_date"),
        )

    def revoke_score(body, query, score_id):
        return 200, service.revoke_score(
            score_id=score_id,
            by=_require(body, "by"),
            reason=_require(body, "reason"),
        )

    def replace_unit(body, query):
        return 201, service.define_unit_replacement(
            topic=_require(body, "topic"),
            old_unit=_require(body, "old_unit"),
            new_unit=_require(body, "new_unit"),
            effective_date=_require(body, "effective_date"),
            by=_require(body, "by"),
        )

    def issue(body, query):
        return 201, service.issue_certificate(
            volunteer_id=_require(body, "volunteer_id"),
            topic=_require(body, "topic"),
            issued_by=_require(body, "by"),
            issued_on=_require(body, "issued_on"),
            validity_years=int(body.get("validity_years", 2)),
            cert_no=body.get("cert_no"),
        )

    def suspend(body, query, cert_no):
        return 200, service.suspend_certificate(
            cert_no=cert_no,
            from_date=_require(body, "from_date"),
            reason=_require(body, "reason"),
        )

    def resume(body, query, cert_no):
        return 200, service.resume_certificate(
            cert_no=cert_no,
            resume_date=_require(body, "resume_date"),
            reason=body.get("reason", ""),
        )

    def revoke_cert(body, query, cert_no):
        return 200, service.revoke_certificate(
            cert_no=cert_no,
            on_date=_require(body, "on_date"),
            by=_require(body, "by"),
            reason=_require(body, "reason"),
        )

    def get_cert(body, query, cert_no):
        return 200, service.get_certificate(cert_no)

    def verify(body, query, cert_no):
        date_value = query.get("date", [None])[0]
        if not date_value:
            raise DomainError("查询参数 date=YYYY-MM-DD 必填")
        topic = query.get("topic", [None])[0]
        return 200, service.verify(cert_no, date_value, topic)

    def history(body, query, volunteer_id):
        return 200, service.volunteer_history(volunteer_id)

    def chain(body, query):
        return 200, service.verify_chain()

    p = re.compile
    dispatch = {
        ("POST", p(r"/volunteers")): register_volunteer,
        ("POST", p(r"/units")): register_unit,
        ("POST", p(r"/papers")): register_paper,
        ("POST", p(r"/retake-eligibility")): grant,
        ("POST", p(r"/scores")): record_score,
        ("POST", p(r"/scores/([^/]+)/revoke")): revoke_score,
        ("POST", p(r"/unit-replacements")): replace_unit,
        ("POST", p(r"/certificates")): issue,
        ("POST", p(r"/certificates/([^/]+)/suspend")): suspend,
        ("POST", p(r"/certificates/([^/]+)/resume")): resume,
        ("POST", p(r"/certificates/([^/]+)/revoke")): revoke_cert,
        ("GET", p(r"/certificates/([^/]+)/verify")): verify,
        ("GET", p(r"/certificates/([^/]+)")): get_cert,
        ("GET", p(r"/volunteers/([^/]+)/history")): history,
        ("GET", p(r"/chain")): chain,
    }

    Handler.do_GET = lambda self: self._handle(dispatch)
    Handler.do_POST = lambda self: self._handle(dispatch)

    return Handler


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    """创建多线程 HTTP 服务（写操作在 SQLite IMMEDIATE 事务中串行化）。"""
    service = TrainingService(db_path)
    httpd = ThreadingHTTPServer((host, port), make_handler(service))
    return httpd
