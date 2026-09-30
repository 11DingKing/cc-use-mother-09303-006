"""HTTP 接口端到端测试：真实起服，走 http.client。"""
from __future__ import annotations

import base64
import json
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from licensing_service.app import create_server  # noqa: E402


class HttpTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.server, self.svc = create_server(f"{self.tmp}/license.db", f"{self.tmp}/art",
                                              "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def req(self, method: str, path: str, body=None, user: str | None = "u1"):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if user:
            headers["X-User-Id"] = user
        payload = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        ctype = resp.getheader("Content-Type", "")
        if "json" in ctype:
            return resp.status, json.loads(raw.decode()) if raw else None
        return resp.status, raw

    def post(self, path, body, user="u1"):
        return self.req("POST", path, body, user)

    def get(self, path, user="u1"):
        return self.req("GET", path, None, user)

    def seed(self) -> None:
        for iid, name, country in (("CNU", "国内职业学院", "CN"),
                                   ("OVS", "海外伙伴学院", "SG"),
                                   ("OTH", "第三方学院", "US")):
            self.post("/institutions", {"id": iid, "name": name, "country": country}, user=None)
        self.post("/users", {"id": "u1", "org_id": "CNU", "display_name": "王老师", "role": "provider"}, user=None)
        self.post("/users", {"id": "u2", "org_id": "CNU", "display_name": "李版权", "role": "copyright"}, user=None)
        self.post("/users", {"id": "u3", "org_id": "OVS", "display_name": "接收员", "role": "recipient"}, user=None)
        self.post("/users", {"id": "u4", "org_id": "OTH", "display_name": "外人", "role": "provider"}, user=None)


class TestHttpFlow(HttpTestBase):
    def test_full_scenario_over_http(self) -> None:
        self.seed()
        st, manual = self.post("/resources", {
            "owner_org_id": "CNU", "title": "数控实训手册", "kind": "manual",
            "summary": "仅限本国校内使用",
            "content_base64": base64.b64encode(b"manual").decode()}, user="u1")
        self.assertEqual(201, st)
        st, course = self.post("/resources", {
            "owner_org_id": "CNU", "title": "数控课件", "kind": "slides",
            "summary": "海外课件", "content_base64": base64.b64encode(b"course").decode()},
            user="u1")
        self.assertEqual(201, st)

        st, _ = self.post("/licenses", {
            "resource_id": manual["id"], "licensor_org_id": "CNU", "scope": "full",
            "territories": ["CN"], "org_ids": ["CNU"],
            "valid_from": "2026-01-01T00:00:00Z", "valid_until": "2027-01-01T00:00:00Z"},
            user="u2")
        self.assertEqual(201, st)
        st, lic_course = self.post("/licenses", {
            "resource_id": course["id"], "licensor_org_id": "CNU", "scope": "full",
            "territories": ["CN", "SG"], "org_ids": ["CNU", "OVS"],
            "valid_from": "2026-01-01T00:00:00Z", "valid_until": "2027-01-01T00:00:00Z"},
            user="u2")
        self.assertEqual(201, st)

        st, recipient = self.post("/recipients", {
            "owner_org_id": "CNU", "org_id": "OVS", "territory": "SG",
            "qualifications": ["vocational-partner"],
            "valid_from": "2026-01-01T00:00:00Z", "valid_until": "2027-06-01T00:00:00Z"},
            user="u1")
        self.assertEqual(201, st)

        # 整包出海 → 422，逐项违规
        st, err = self.post("/packages", {
            "name": "合作课程包", "resource_ids": [manual["id"], course["id"]],
            "recipient_id": recipient["id"]}, user="u1")
        self.assertEqual(422, st)
        self.assertEqual("package_verification_failed", err["error"])
        rules = {v["rule"] for v in err["details"]["violations"]}
        self.assertIn("territory", rules)

        # 替代材料：国际版手册 + 海外授权
        st, intl = self.post("/resources", {
            "owner_org_id": "CNU", "title": "实训手册国际版", "kind": "manual",
            "summary": "替代材料", "content_base64": base64.b64encode(b"intl").decode()},
            user="u1")
        st, intl_lic = self.post("/licenses", {
            "resource_id": intl["id"], "licensor_org_id": "CNU", "scope": "partial",
            "territories": ["CN", "SG"], "org_ids": ["CNU", "OVS"],
            "valid_from": "2026-01-01T00:00:00Z", "valid_until": "2027-01-01T00:00:00Z",
            "grants": [{"subject": "课堂教学", "permitted": True},
                       {"subject": "校内实训", "permitted": True}]}, user="u2")
        self.assertEqual(201, st)

        st, pkg = self.post("/packages", {
            "name": "合作课程包", "resource_ids": [intl["id"], course["id"]],
            "recipient_id": recipient["id"]}, user="u1")
        self.assertEqual(201, st)
        self.assertEqual("verified", pkg["status"])

        # 交付
        st, delivered = self.post(f"/packages/{pkg['id']}/deliver", {}, user="u1")
        self.assertEqual(200, st)
        self.assertEqual("delivered", delivered["status"])

        # 撤回 → 再交付阻断
        st, _ = self.post(f"/licenses/{intl_lic['id']}/revoke",
                          {"reason": "版权方撤回"}, user="u2")
        self.assertEqual(200, st)
        st, blocked = self.post(f"/packages/{pkg['id']}/deliver", {}, user="u1")
        self.assertEqual(409, st)
        self.assertEqual("delivery_blocked", blocked["error"])

        # 影响分析
        st, impact = self.get(f"/licenses/{intl_lic['id']}/impact", user="u2")
        self.assertEqual(200, st)
        self.assertTrue(impact["affected_packages"])
        self.assertTrue(any(e["event_type"] == "license_revoked" for e in impact["changes"]))

        # 跨机构隔离：404 防探测
        st, _ = self.get(f"/resources/{manual['id']}", user="u4")
        self.assertEqual(404, st)
        st, _ = self.get(f"/packages/{pkg['id']}", user="u4")
        self.assertEqual(404, st)
        st, _ = self.get(f"/licenses/{intl_lic['id']}/impact", user="u4")
        self.assertEqual(404, st)
        # 接收方可读已交付包与产物
        st, seen = self.get(f"/packages/{pkg['id']}", user="u3")
        self.assertEqual(200, st)
        st, artifact = self.get(f"/packages/{pkg['id']}/artifact", user="u3")
        self.assertEqual(200, st)
        self.assertTrue(artifact)

    def test_auth_required(self) -> None:
        st, err = self.req("GET", "/resources", None, user=None)
        self.assertEqual(401, st)
        self.assertEqual("authentication_required", err["error"])

    def test_role_forbidden(self) -> None:
        self.seed()
        st, course = self.post("/resources", {
            "owner_org_id": "CNU", "title": "课件", "kind": "slides",
            "summary": "x", "content_base64": base64.b64encode(b"c").decode()}, user="u1")
        st, err = self.post("/licenses", {
            "resource_id": course["id"], "licensor_org_id": "CNU", "scope": "full",
            "territories": "*", "org_ids": "*",
            "valid_from": "2026-01-01T00:00:00Z"}, user="u1")
        self.assertEqual(403, st)
        self.assertEqual("access_denied", err["error"])


if __name__ == "__main__":
    unittest.main()
