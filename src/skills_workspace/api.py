"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .ledger import LedgerService
from .models import WriteReceipt
from .storage import Database


def _respond(receipt: WriteReceipt) -> tuple[int, dict[str, Any]]:
    return (200 if receipt.replayed else 201), receipt.__dict__


def _query(path: str, field: str, required: bool = True) -> str | None:
    value = parse_qs(urlparse(path).query).get(field, [None])[0]
    if required and not value:
        raise ValidationError(f"{field} 不能为空")
    return value


def route(service: LedgerService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            return _respond(service.register_organization(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/actors":
            return _respond(service.register_actor(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/sites":
            return _respond(service.register_site(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/domain-records":
            return _respond(service.record_domain_data(actor_id=actor_id, **body))
        if method == "GET" and parsed.path == "/domain-records":
            site_id = _query(path, "site_id")
            category = _query(path, "category", required=False)
            return 200, {"items": [item.__dict__ for item in
                                   service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            after = int(_query(path, "after_sequence", required=False) or 0)
            return 200, {"items": service.audit_events(after)}
        if method == "POST" and parsed.path == "/cases":
            return _respond(service.register_case(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/prescription-versions":
            return _respond(service.register_prescription_version(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/routes":
            return _respond(service.create_route(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/steps/start":
            return _respond(service.start_step(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/steps/complete":
            return _respond(service.complete_step(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/handovers":
            return _respond(service.initiate_handover(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/handovers/confirm":
            return _respond(service.confirm_handover(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/handovers/discrepancies":
            return _respond(service.raise_discrepancy(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/discrepancies/resolve":
            return _respond(service.resolve_discrepancy(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/material-batches":
            return _respond(service.register_material_batch(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/material-batches/split":
            return _respond(service.split_material_batch(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/material-consumptions":
            return _respond(service.consume_material(actor_id=actor_id, **body))
        if method == "GET" and parsed.path == "/products/trace":
            return 200, service.trace_product(actor_id=actor_id,
                                              product_id=_query(path, "product_id"))
        if method == "GET" and parsed.path == "/cases/trace":
            return 200, service.trace_case(actor_id=actor_id, case_id=_query(path, "case_id"))
        if method == "GET" and parsed.path == "/materials/trace":
            return 200, service.trace_material(actor_id=actor_id,
                                               batch_id=_query(path, "batch_id"))
        if method == "GET" and parsed.path == "/discrepancies":
            status = _query(path, "status", required=False) or "open"
            return 200, {"items": service.list_discrepancies(
                actor_id=actor_id, site_id=_query(path, "site_id"), status=status)}
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: LedgerService

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
    Handler.service = LedgerService(database)
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
