"""HTTP JSON 接口（标准库实现，零第三方依赖）。

鉴权：所有业务请求携带 ``X-User-Id`` 头，服务端据此加载受权人员；
机构隔离与角色判定全部在领域服务内完成，接口层不做业务豁免。

路由
----
POST   /institutions                 登记机构
POST   /users                        登记受权人员
POST   /resources                    登记资源（content_base64 + depends_on）
POST   /resources/{id}/versions      登记新版本
GET    /resources                    列出可见资源
GET    /resources/{id}               资源摘要与版本谱系
POST   /licenses                     授予授权（full/partial）
POST   /licenses/{id}/revoke         撤回许可
GET    /licenses/{id}                查看授权
GET    /licenses/{id}/impact         权利变化影响了哪些包
POST   /recipients                   登记接收方资格
POST   /recipients/{id}/suspend      暂停接收方资格
POST   /packages                     组包（逐项核验 + 固定快照）
GET    /packages                     列出可见课程包
GET    /packages/{id}                包详情（快照/条目/谱系/交付）
GET    /packages/{id}/lineage        谱系
POST   /packages/{id}/replace        替代材料并重组
POST   /packages/{id}/deliver        交付（再核验）
GET    /packages/{id}/artifact       下载产物（仅成功包）
GET    /attempts?name=               组包尝试审计（失败记录无产物路径）
"""
from __future__ import annotations

import base64
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import urlparse, parse_qs

from .errors import (
    AuthenticationError,
    DomainError,
    NotFoundError,
    ValidationError,
)
from .services import LicensingService
from .store import Store


def _b64decode(value: str) -> bytes:
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except Exception as exc:
        from .errors import ValidationError
        raise ValidationError("content_base64 必须是合法 base64") from exc


class App:
    def __init__(self, service: LicensingService) -> None:
        self.service = service

    def authenticate(self, headers) -> dict[str, Any]:
        uid = headers.get("x-user-id")
        if not uid:
            raise AuthenticationError("缺少 X-User-Id 头")
        return self.service.get_user(uid)

    # ---- 处理器返回 (status, body)；body 为 bytes 时直接作为响应体 --------

    def handle(self, method: str, path: str, query: dict[str, str],
               body: dict[str, Any], headers) -> tuple[int, Any, str]:
        viewer = None
        # 机构/用户登记为引导接口，无需登录；其余全部需要受权人员
        public = (method == "POST" and path in ("/institutions", "/users"))
        if not public:
            viewer = self.authenticate(headers)

        s = self.service
        if method == "POST" and path == "/institutions":
            return 201, s.create_institution(body["id"], body["name"], body["country"]), "json"
        if method == "POST" and path == "/users":
            return 201, s.create_user(body["id"], body["org_id"], body["display_name"],
                                      body["role"]), "json"

        if method == "POST" and path == "/resources":
            return 201, s.register_resource(
                owner_org_id=body["owner_org_id"], title=body["title"], kind=body["kind"],
                summary=body["summary"], content=_b64decode(body["content_base64"]),
                depends_on=body.get("depends_on"), note=body.get("note", ""), actor=viewer), "json"
        if method == "GET" and path == "/resources":
            return 200, s.list_resources(viewer), "json"

        m = re.fullmatch(r"/resources/([^/]+)/versions", path)
        if method == "POST" and m:
            return 201, s.new_resource_version(
                m.group(1), content=_b64decode(body["content_base64"]),
                depends_on=body.get("depends_on"), note=body.get("note", ""), actor=viewer), "json"
        m = re.fullmatch(r"/resources/([^/]+)", path)
        if method == "GET" and m:
            return 200, s.get_resource(m.group(1), viewer=viewer), "json"

        if method == "POST" and path == "/licenses":
            return 201, s.grant_license(
                resource_id=body["resource_id"], licensor_org_id=body["licensor_org_id"],
                scope=body["scope"], territories=body["territories"], org_ids=body["org_ids"],
                valid_from=body["valid_from"], valid_until=body.get("valid_until"),
                grants=body.get("grants"), supersedes_id=body.get("supersedes_id"),
                actor=viewer), "json"
        m = re.fullmatch(r"/licenses/([^/]+)/revoke", path)
        if method == "POST" and m:
            return 200, s.revoke_license(m.group(1), body.get("reason", ""), viewer), "json"
        m = re.fullmatch(r"/licenses/([^/]+)/impact", path)
        if method == "GET" and m:
            return 200, s.rights_change_impact(m.group(1), viewer), "json"
        m = re.fullmatch(r"/licenses/([^/]+)", path)
        if method == "GET" and m:
            return 200, s.get_license(m.group(1), viewer=viewer), "json"

        if method == "POST" and path == "/recipients":
            return 201, s.register_recipient(
                owner_org_id=body["owner_org_id"], org_id=body["org_id"],
                territory=body["territory"], qualifications=body.get("qualifications", []),
                valid_from=body["valid_from"], valid_until=body.get("valid_until"),
                actor=viewer), "json"
        m = re.fullmatch(r"/recipients/([^/]+)/suspend", path)
        if method == "POST" and m:
            return 200, s.suspend_recipient(m.group(1), viewer), "json"

        if method == "POST" and path == "/packages":
            return 201, s.build_package(
                name=body["name"], resource_ids=body["resource_ids"],
                recipient_id=body["recipient_id"], license_selection=body.get("license_selection"),
                actor=viewer), "json"
        if method == "GET" and path == "/packages":
            return 200, s.list_packages(viewer), "json"
        if method == "GET" and path == "/attempts":
            return 200, s.list_attempts(query["name"], viewer), "json"

        m = re.fullmatch(r"/packages/([^/]+)/lineage", path)
        if method == "GET" and m:
            return 200, s.get_lineage(m.group(1), viewer=viewer), "json"
        m = re.fullmatch(r"/packages/([^/]+)/replace", path)
        if method == "POST" and m:
            return 201, s.replace_material(
                package_id=m.group(1), old_resource_id=body["old_resource_id"],
                new_resource_id=body["new_resource_id"],
                recipient_id=body.get("recipient_id"), actor=viewer), "json"
        m = re.fullmatch(r"/packages/([^/]+)/deliver", path)
        if method == "POST" and m:
            return 200, s.deliver(m.group(1), actor=viewer), "json"
        m = re.fullmatch(r"/packages/([^/]+)/artifact", path)
        if method == "GET" and m:
            data, pkg = s.download_artifact(m.group(1), viewer)
            return 200, data, "bytes"
        m = re.fullmatch(r"/packages/([^/]+)", path)
        if method == "GET" and m:
            return 200, s.get_package(m.group(1), viewer=viewer), "json"

        raise NotFoundError(f"无此路由：{method} {path}")


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "LicensingService/0.2"

        def log_message(self, fmt: str, *args) -> None:  # 安静输出
            return

        def _send(self, status: int, payload: Any, kind: str) -> None:
            if kind == "bytes":
                data = payload
                ctype = "application/octet-stream"
            else:
                data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
                ctype = "application/json; charset=utf-8"
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            query = {k: v[0] for k, v in qs.items()}
            body: dict[str, Any] = {}
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if raw:
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except json.JSONDecodeError as exc:
                        from .errors import ValidationError
                        self._send_error(ValidationError("请求体必须是 JSON 对象"))
                        return
            try:
                status, payload, kind = app.handle(method, parsed.path, query, body, self.headers)
                self._send(status, payload, kind)
            except DomainError as exc:
                self._send_error(exc)
            except KeyError as exc:
                self._send_error(ValidationError(f"缺少必填字段：{exc.args[0]}"))

        def _send_error(self, exc: DomainError) -> None:
            self._send(exc.status, exc.to_body(), "json")

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

    return Handler


def create_server(db_path: str, artifact_dir: str, host: str = "127.0.0.1",
                  port: int = 8080) -> tuple[ThreadingHTTPServer, LicensingService]:
    store = Store(db_path)
    service = LicensingService(store, artifact_dir)
    server = ThreadingHTTPServer((host, port), make_handler(App(service)))
    return server, service
