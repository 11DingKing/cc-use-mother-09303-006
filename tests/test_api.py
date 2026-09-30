"""HTTP/JSON 接口端到端测试。"""
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

from licensing.api import AuthContext, create_server

TOKENS = {
    "tkn-cn-prov": {"subject_id": "u-prov", "org_id": "ORG_CN",
                    "roles": ["PROVIDER"], "display_name": "中方教务"},
    "tkn-cn-admin": {"subject_id": "u-admin", "org_id": "ORG_CN",
                     "roles": ["COPYRIGHT_ADMIN"], "display_name": "版权管理员"},
    "tkn-other-prov": {"subject_id": "u-prov2", "org_id": "ORG_OTHER",
                       "roles": ["PROVIDER"], "display_name": "外校教务"},
}


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.server = create_server(
            "127.0.0.1", 0, str(base / "db.sqlite3"), str(base / "artifacts"),
            AuthContext(tokens=TOKENS), quiet=True)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, method: str, path: str, token: str | None, body: dict | None = None) -> tuple[int, dict | bytes]:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                if "application/json" in ctype:
                    return resp.status, json.loads(raw)
                return resp.status, raw
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_full_flow_over_http(self) -> None:
        # 未鉴权
        status, _ = self.call("GET", "/resources", None)
        self.assertEqual(status, 401)
        # 探活
        status, body = self.call("GET", "/health", None)
        self.assertEqual(status, 200)

        # 登记 + 授权
        status, _ = self.call("POST", "/resources", "tkn-cn-prov", {
            "resource_id": "r-handbook", "title": "实训手册",
            "digest": "sha256:abc", "resource_type": "manual"})
        self.assertEqual(status, 201)
        status, _ = self.call("POST", "/resources/r-handbook/licenses", "tkn-cn-admin", {
            "rights_holder": "本校出版社", "territories": ["CN"],
            "org_scope": {"type": "WHITELIST", "orgs": ["ORG_CN_BRANCH"]},
            "recipient_qualification": {"conditions": [
                {"attr": "accredited", "op": "truthy", "value": True}]},
            "valid_from": "2026-01-01T00:00:00Z",
            "valid_until": "2027-01-01T00:00:00Z",
            "basis": "协议第3条"})
        self.assertEqual(status, 201)

        # 越权角色：教务不能授权
        status, body = self.call("POST", "/resources/r-handbook/licenses", "tkn-cn-prov", {
            "rights_holder": "h", "territories": ["*"],
            "org_scope": {"type": "ALL", "orgs": []}, "basis": "b"})
        self.assertEqual(status, 403)

        # 海外组包被拒 422，且失败构建不可下载
        status, body = self.call("POST", "/packages", "tkn-cn-prov", {
            "name": "海外包", "resource_ids": ["r-handbook"],
            "recipient": {"org_id": "ORG_FRN", "org_name": "海外校",
                          "country": "FR", "attrs": {"accredited": True}}})
        self.assertEqual(status, 422)
        self.assertEqual(body["code"], "BUILD_REJECTED")
        self.assertTrue(any("地域" in r for f in body["details"] for r in f["reasons"]))
        status, failed = self.call("GET", "/builds/failed", "tkn-cn-prov")
        self.assertEqual(len(failed["failed_builds"]), 1)
        bad_id = failed["failed_builds"][0]["package_id"]
        status, _ = self.call("GET", f"/packages/{bad_id}/download", "tkn-cn-prov")
        self.assertEqual(status, 404)

        # 国内组包成功并下载
        status, pkg = self.call("POST", "/packages", "tkn-cn-prov", {
            "name": "国内包", "resource_ids": ["r-handbook"],
            "idempotency_key": "k-1",
            "recipient": {"org_id": "ORG_CN_BRANCH", "org_name": "国内分校",
                          "country": "CN", "attrs": {"accredited": True}}})
        self.assertEqual(status, 201)
        pid = pkg["package_id"]
        status, data = self.call("GET", f"/packages/{pid}/download", "tkn-cn-prov")
        self.assertEqual(status, 200)
        manifest = data if isinstance(data, dict) else json.loads(data)
        self.assertIn("snapshot_digest", manifest)  # 确保拿到的是制品而非错误体
        self.assertEqual(manifest["snapshot_digest"], pkg["snapshot_digest"])

        # 重复组包（同幂等键）返回同一冻结包
        status, pkg2 = self.call("POST", "/packages", "tkn-cn-prov", {
            "name": "国内包", "resource_ids": ["r-handbook"],
            "idempotency_key": "k-1",
            "recipient": {"org_id": "ORG_CN_BRANCH", "org_name": "国内分校",
                          "country": "CN", "attrs": {"accredited": True}}})
        self.assertEqual(pkg2["package_id"], pid)

        # 撤回 -> 影响分析可反查到包
        status, _ = self.call("POST", "/resources/r-handbook/revocation",
                              "tkn-cn-admin", {"reason": "终止"})
        self.assertEqual(status, 201)
        status, impact = self.call("GET", "/resources/r-handbook/impact", "tkn-cn-prov")
        self.assertEqual(impact["affected_package_count"], 1)
        self.assertEqual(impact["affected_packages"][0]["package_id"], pid)

        # 跨机构隔离：外校看不到资源与包
        status, _ = self.call("GET", "/resources/r-handbook", "tkn-other-prov")
        self.assertEqual(status, 404)
        status, body = self.call("GET", f"/packages/{pid}", "tkn-other-prov")
        self.assertEqual(status, 404)
        status, body = self.call("GET", "/packages", "tkn-other-prov")
        self.assertEqual(body["packages"], [])


if __name__ == "__main__":
    unittest.main()
