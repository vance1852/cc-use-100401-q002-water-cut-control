"""无第三方依赖的稳油控水协同 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import ValidationFailed, WaterControlError
from .service import WaterControlService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    """将 HTTP 路由映射到领域服务，便于无网络单元测试。"""

    def __init__(self, service: WaterControlService) -> None:
        self.service = service
        self._lock = threading.Lock()

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        # 单 SQLite 连接在 ThreadingHTTPServer 下需要串行调度。
        with self._lock:
            return self._dispatch(method, target, headers, body)

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    @staticmethod
    def _idempotency_key(headers: Mapping[str, str]) -> str:
        key = headers.get("idempotency-key", "").strip()
        if not key:
            raise ValidationFailed("缺少 Idempotency-Key")
        return key

    def _dispatch(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        path = urlparse(target).path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized)
            if method == "POST" and path == "/users":
                return Response(201, self.service.create_user(payload["user_id"], payload["display_name"], payload["role"]))
            if method == "POST" and path == "/layer-systems":
                return Response(201, self.service.create_layer_system(actor, payload))
            if method == "POST" and path == "/well-groups":
                return Response(201, self.service.create_well_group(actor, payload))
            if method == "POST" and path == "/wells":
                return Response(201, self.service.create_well(actor, payload))
            if method == "POST" and path == "/connectivity":
                return Response(201, self.service.add_connectivity(actor, payload))
            if method == "POST" and path == "/caps":
                return Response(201, self.service.register_cap(actor, payload))
            if method == "POST" and path == "/constraint-sets":
                return Response(201, self.service.create_constraint_set(actor, payload))
            if method == "POST" and path == "/tests":
                return Response(201, self.service.record_test(actor, payload))
            if method == "POST" and path == "/events":
                return Response(201, self.service.report_event(actor, payload))
            if method == "POST" and path == "/plans":
                return Response(201, self.service.create_plan(actor, payload, self._idempotency_key(normalized)))
            if method == "POST" and len(parts) == 3 and parts[0] == "plans" and parts[2] == "confirm":
                return Response(200, self.service.confirm_plan(actor, parts[1], int(payload["expected_revision"])))
            if method == "POST" and len(parts) == 3 and parts[0] == "plans" and parts[2] == "recompute":
                payload = {**payload, "source_plan_id": parts[1]}
                return Response(201, self.service.recompute_plan(actor, payload, self._idempotency_key(normalized)))
            if method == "POST" and len(parts) == 5 and parts[0] == "plans" and parts[2] == "phases" and parts[4] == "execute":
                return Response(200, self.service.execute_phase(actor, parts[1], int(parts[3]), int(payload["expected_revision"])))
            if method == "GET" and len(parts) == 2 and parts[0] == "plans":
                return Response(200, self.service.get_plan(actor, parts[1]))
            if method == "GET" and len(parts) == 5 and parts[0] == "plans" and parts[2] == "phases" and parts[4] == "explanation":
                return Response(200, self.service.explain_phase(actor, parts[1], int(parts[3])))
            if method == "GET" and len(parts) == 5 and parts[0] == "plans" and parts[2] == "phases" and parts[4] == "conservation":
                return Response(200, self.service.check_conservation(actor, parts[1], int(parts[3])))
            if method == "POST" and path == "/overrides":
                return Response(201, self.service.request_override(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "overrides" and parts[2] == "approve":
                return Response(200, self.service.approve_override(actor, int(parts[1])))
            if method == "POST" and len(parts) == 3 and parts[0] == "overrides" and parts[2] == "apply":
                return Response(200, self.service.apply_override(actor, int(parts[1])))
            if method == "GET" and path == "/audit/chain":
                return Response(200, self.service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except WaterControlError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "WaterControl/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动稳油控水协同调度 HTTP 服务")
    parser.add_argument("--database", type=Path, default=Path("water-control.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(WaterControlService(connection))))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
