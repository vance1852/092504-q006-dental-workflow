"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import dataclasses
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .service import DomainService
from .storage import Database
from .tracing import TracingService


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    segments = [segment for segment in parsed.path.split("/") if segment]
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}

        status, payload = _route_tracing(service, method, segments, body, actor_id)
        if status is not None:
            return status, payload
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _route_tracing(service: DomainService, method: str, segments: list[str],
                   body: dict[str, Any], actor_id: str) -> tuple[int | None, dict[str, Any]]:
    """分派病例工序与材料追溯相关路由。"""

    if not isinstance(service, TracingService):
        return None, {}

    def receipt_result(receipt) -> tuple[int, dict[str, Any]]:
        return 200 if receipt.replayed else 201, receipt.__dict__

    if method == "POST" and segments == ["cases"]:
        return receipt_result(service.create_case(actor_id=actor_id, **body))
    if method == "POST" and len(segments) == 3 and segments[0] == "cases" and segments[2] == "prescription-revisions":
        return receipt_result(service.revise_prescription(actor_id=actor_id, case_id=segments[1], **body))
    if method == "POST" and segments == ["operations", "start"]:
        return receipt_result(service.start_operation(actor_id=actor_id, **body))
    if method == "POST" and segments == ["handoffs"]:
        return receipt_result(service.propose_handoff(actor_id=actor_id, **body))
    if method == "POST" and len(segments) == 3 and segments[0] == "handoffs" and segments[2] == "respond":
        return receipt_result(service.respond_handoff(actor_id=actor_id, handoff_id=segments[1], **body))
    if method == "POST" and len(segments) == 3 and segments[0] == "discrepancies" and segments[2] == "resolve":
        return receipt_result(service.resolve_discrepancy(actor_id=actor_id, discrepancy_id=segments[1], **body))
    if method == "POST" and segments == ["material-lots"]:
        return receipt_result(service.register_material_lot(actor_id=actor_id, **body))
    if method == "POST" and segments == ["materials", "split"]:
        return receipt_result(service.split_material(actor_id=actor_id, **body))
    if method == "POST" and segments == ["material-consumptions"]:
        return receipt_result(service.consume_material(actor_id=actor_id, **body))
    if method == "POST" and len(segments) == 3 and segments[0] == "products" and segments[2] == "finish":
        return receipt_result(service.finish_product(actor_id=actor_id, product_id=segments[1], **body))
    if method == "POST" and len(segments) == 3 and segments[0] == "cases" and segments[2] == "rework":
        return receipt_result(service.rework(actor_id=actor_id, case_id=segments[1], **body))
    if method == "POST" and len(segments) == 3 and segments[0] == "cases" and segments[2] == "close":
        return receipt_result(service.close_case(actor_id=actor_id, case_id=segments[1]))
    if method == "GET" and len(segments) == 2 and segments[0] == "cases":
        return 200, dataclasses.asdict(service.get_case_ledger(actor_id=actor_id, case_id=segments[1]))
    if method == "GET" and len(segments) == 3 and segments[0] == "material-lots" and segments[2] == "balance":
        return 200, service.material_balance(actor_id=actor_id, lot_id=segments[1])
    if method == "GET" and len(segments) == 3 and segments[0] == "products" and segments[2] == "trace":
        return 200, dataclasses.asdict(service.trace_product(actor_id=actor_id, product_id=segments[1]))
    return None, {}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动技能赛训协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = TracingService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
