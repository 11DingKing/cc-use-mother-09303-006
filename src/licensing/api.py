"""HTTP/JSON 接口（仅用标准库）。

鉴权：
- 生产方式：Bearer Token -> 主体映射文件（JSON，路径由环境变量
  LICENSE_TOKENS_FILE 指定），形如 {"tkn-prov-1": {"subject_id": "...",
  "org_id": "ORG_CN", "roles": ["PROVIDER"], "display_name": "..."}}。
- 开发方式：--dev-auth 启动时从 X-Subject / X-Org / X-Roles 头解析，
  仅限本地，默认关闭。

所有响应均为 JSON；下载接口返回固化的快照清单文件。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .errors import BuildRejected, DomainError
from .service import ROLE_COPYRIGHT_ADMIN, ROLE_PROVIDER, Actor, LicensingService
from .store import Store


class AuthContext:
    def __init__(self, tokens: dict[str, dict] | None = None, dev_auth: bool = False) -> None:
        self.tokens = tokens or {}
        self.dev_auth = dev_auth

    def resolve(self, headers: Any) -> Actor:
        auth = headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            entry = self.tokens.get(auth[7:].strip())
            if entry is None:
                raise PermissionError("令牌无效")
            return Actor(
                subject_id=entry["subject_id"],
                org_id=entry["org_id"],
                roles=set(entry.get("roles", [])),
                display_name=entry.get("display_name", ""),
            )
        if self.dev_auth:
            org = headers.get("X-Org")
            subject = headers.get("X-Subject")
            roles = headers.get("X-Roles", "")
            if not org or not subject:
                raise PermissionError("开发鉴权缺少 X-Subject/X-Org 头")
            return Actor(subject_id=subject, org_id=org,
                         roles={r.strip() for r in roles.split(",") if r.strip()})
        raise PermissionError("缺少 Authorization 头")


class LicensingHandler(BaseHTTPRequestHandler):
    server_version = "VocEdLicensing/1.0"

    # ---- 框架 -----------------------------------------------------------

    def _send_json(self, status: int, body: Any) -> None:
        data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            from .errors import ValidationError

            raise ValidationError(f"请求体不是合法 JSON：{exc}")
        if not isinstance(value, dict):
            from .errors import ValidationError

            raise ValidationError("请求体必须是 JSON 对象")
        return value

    def _actor(self) -> Actor:
        return self.server.auth.resolve(self.headers)  # type: ignore[attr-defined]

    def _dispatch(self, method: str) -> None:
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        try:
            actor = self._actor()
        except PermissionError as exc:
            self._send_json(401, {"code": "UNAUTHENTICATED", "message": str(exc)})
            return
        try:
            with self.server.service_lock:  # type: ignore[attr-defined]
                getattr(self, f"handle_{method}")(actor, path)
        except BuildRejected as exc:
            self._send_json(exc.http_status, exc.to_dict())
        except DomainError as exc:
            self._send_json(exc.http_status, exc.to_dict())
        except Exception as exc:  # noqa: BLE001 - 边界兜底，不泄漏堆栈
            self._send_json(500, {"code": "INTERNAL_ERROR", "message": "服务内部错误"})
            self.log_error("internal error: %s", exc)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def log_message(self, fmt: str, *args: Any) -> None:
        logger = getattr(self.server, "log_message", None)
        if logger is not None:
            logger(fmt, *args)

    # ---- 路由 -----------------------------------------------------------

    def handle_GET(self, actor: Actor, path: str) -> None:
        svc: LicensingService = self.server.service  # type: ignore[attr-defined]
        if path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        if path == "/resources":
            self._send_json(200, {"resources": svc.list_resources(actor)})
            return
        if path == "/packages":
            self._send_json(200, {"packages": svc.list_packages(actor)})
            return
        if path == "/builds/failed":
            self._send_json(200, {"failed_builds": svc.list_failed_builds(actor)})
            return

        parts = [p for p in path.split("/") if p]
        if len(parts) == 3 and parts[0] == "resources":
            rid, sub = parts[1], parts[2]
            if sub == "license-history":
                self._send_json(200, {"history": svc.list_license_history(actor, rid)})
                return
            if sub == "lineage":
                self._send_json(200, svc.get_lineage(actor, rid))
                return
            if sub == "impact":
                self._send_json(200, svc.impact_analysis(actor, rid))
                return
        if len(parts) == 3 and parts[0] == "packages" and parts[2] == "download":
            filename, artifact, digest = svc.download_package(actor, parts[1])
            data = Path(artifact).read_bytes()
            from urllib.parse import quote

            ascii_name = filename.encode("ascii", "ignore").decode() or "package.json"
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header(
                "Content-Disposition",
                f"attachment; filename=\"{ascii_name}\"; "
                f"filename*=UTF-8''{quote(filename)}")
            self.send_header("X-Snapshot-Digest", digest)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if len(parts) == 2 and parts[0] == "resources":
            self._send_json(200, svc.get_resource(actor, parts[1]))
            return
        if len(parts) == 2 and parts[0] == "packages":
            self._send_json(200, svc.get_package(actor, parts[1]))
            return
        self._send_json(404, {"code": "NOT_FOUND", "message": f"无此路由：{path}"})

    def handle_POST(self, actor: Actor, path: str) -> None:
        svc: LicensingService = self.server.service  # type: ignore[attr-defined]
        body = self._read_json()
        if path == "/resources":
            result = svc.register_resource(
                actor,
                resource_id=body["resource_id"],
                title=body["title"],
                digest=body["digest"],
                resource_type=body.get("resource_type", ""),
                metadata=body.get("metadata"),
                dependencies=body.get("dependencies"),
            )
            self._send_json(201, result)
            return
        if path == "/replacements":
            result = svc.register_replacement(
                actor,
                old_resource_id=body["old_resource_id"],
                new_resource_id=body["new_resource_id"],
                title=body["title"],
                digest=body["digest"],
                resource_type=body.get("resource_type", ""),
                metadata=body.get("metadata"),
                rights_holder=body["rights_holder"],
                territories=body["territories"],
                org_scope=body["org_scope"],
                recipient_qualification=body.get("recipient_qualification"),
                recipient_orgs=body.get("recipient_orgs"),
                valid_from=body.get("valid_from"),
                valid_until=body.get("valid_until"),
                basis=body["basis"],
                revoke_old=body.get("revoke_old", False),
                note=body.get("note"),
            )
            self._send_json(201, result)
            return
        if path == "/packages":
            result = svc.build_package(
                actor,
                name=body["name"],
                resource_ids=body["resource_ids"],
                recipient=body["recipient"],
                idempotency_key=body.get("idempotency_key"),
            )
            self._send_json(201, result)
            return

        parts = [p for p in path.split("/") if p]
        if len(parts) == 3 and parts[0] == "resources" and parts[2] == "licenses":
            result = svc.grant_license(
                actor,
                resource_id=parts[1],
                rights_holder=body["rights_holder"],
                territories=body["territories"],
                org_scope=body["org_scope"],
                recipient_qualification=body.get("recipient_qualification"),
                recipient_orgs=body.get("recipient_orgs"),
                valid_from=body.get("valid_from"),
                valid_until=body.get("valid_until"),
                basis=body["basis"],
                partial=body.get("partial", False),
                note=body.get("note"),
            )
            self._send_json(201, result)
            return
        if len(parts) == 3 and parts[0] == "resources" and parts[2] == "revocation":
            result = svc.revoke_license(actor, parts[1], reason=body.get("reason", ""))
            self._send_json(201, result)
            return
        self._send_json(404, {"code": "NOT_FOUND", "message": f"无此路由：{path}"})


def create_server(
    host: str,
    port: int,
    db_path: str,
    artifact_dir: str,
    auth: AuthContext,
    quiet: bool = False,
) -> ThreadingHTTPServer:
    store = Store(db_path)
    service = LicensingService(store, artifact_dir)

    class _Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = _Server((host, port), LicensingHandler)
    server.service = service  # type: ignore[attr-defined]
    server.auth = auth  # type: ignore[attr-defined]
    server.service_lock = threading.Lock()  # type: ignore[attr-defined]
    if quiet:
        server.log_message = lambda *_a, **_k: None  # type: ignore[method-assign]
    return server
