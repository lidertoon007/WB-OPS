import importlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app.services.team import COOKIE, Team
from app.storage import DraftStore


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DraftStore(f"sqlite:///{Path(self.temp.name) / 'auth.sqlite3'}")
        self.team = Team(self.store)

    def test_password_hash_sessions_revocation_and_expiry(self):
        user = self.team.create_user("anna", "A safe password 123", "Анна")
        self.assertNotIn("password", str(user))
        with self.store._connect() as c:
            stored = c.execute("SELECT password_hash FROM team_members").fetchone()[0]
        self.assertNotIn("A safe password 123", stored)
        with self.assertRaises(ValueError):
            self.team.login("anna", "wrong")
        _, token = self.team.login("ANNA", "A safe password 123")
        self.assertEqual(self.team.by_token(token)["id"], user["id"])
        self.team.logout(token)
        self.assertIsNone(self.team.by_token(token))
        _, token = self.team.login("anna", "A safe password 123")
        with self.store._connect() as c:
            c.execute("UPDATE team_sessions SET expires_at=?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
        self.assertIsNone(self.team.by_token(token))
        _, token = self.team.login("anna", "A safe password 123")
        self.team.set_active(user["id"], False)
        self.team.set_active(user["id"], True)
        self.assertIsNone(self.team.by_token(token))

    def test_password_change_last_admin_and_rate_limit(self):
        user = self.team.create_user("admin", "A safe password 123", role="owner")
        _, token = self.team.login("admin", "A safe password 123")
        for action in (lambda: self.team.delete_member(user["id"]), lambda: self.team.set_active(user["id"], False)):
            with self.assertRaisesRegex(ValueError, "последнего"):
                action()
        self.team.change_password(user["id"], "A safe password 123", "A different password 456")
        self.assertIsNone(self.team.by_token(token))
        self.team.login("admin", "A different password 456")
        for _ in range(5):
            with self.assertRaises(ValueError):
                self.team.login("admin", "wrong")
        with self.assertRaisesRegex(ValueError, "много попыток"):
            self.team.login("admin", "A different password 456")


class ProductionAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, {"WBOPS_DATABASE_URL": f"sqlite:///{Path(cls.temp.name) / 'db.sqlite3'}",
                                              "WBOPS_ENVIRONMENT": "production", "WBOPS_ALLOW_REMOTE": "true"})
        cls.env.start()
        from app.config import get_settings
        get_settings.cache_clear()
        import app.main
        cls.m = importlib.reload(app.main)

    def setUp(self):
        with self.m.team.store._connect() as connection:
            for table in ("team_sessions", "team_login_attempts", "team_invites", "team_members"):
                connection.execute(f"DELETE FROM {table}")

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        from app.config import get_settings
        get_settings.cache_clear()
        cls.temp.cleanup()

    def test_proxy_cannot_be_owner_and_authenticated_admin_can_manage_users(self):
        client = TestClient(self.m.app, base_url="https://temporary.example", client=("127.0.0.1", 5000))
        self.assertEqual(client.get("/health").status_code, 200)
        self.assertEqual(client.get("/join").status_code, 200)
        self.assertEqual(client.get("/", follow_redirects=False).status_code, 303)
        for headers in ({}, {"Host": "localhost"}, {"X-Forwarded-For": "127.0.0.1", "Forwarded": "for=127.0.0.1"}):
            self.assertEqual(client.get("/api/team", headers=headers).status_code, 401)
        admin = self.m.team.create_user("administrator", "A safe password 123", role="owner")
        response = client.post("/api/team/login", json={"login": "administrator", "password": "A safe password 123"})
        self.assertEqual(response.status_code, 200)
        for flag in ("HttpOnly", "Secure", "SameSite=lax"):
            self.assertIn(flag, response.headers["set-cookie"])
        self.assertEqual(client.get("/api/team/me").json()["role"], "owner")
        user = client.post("/api/team/users", json={"login": "employee", "password": "Another safe password", "name": "Сотрудник"}).json()
        self.assertNotIn("hash", str(client.get("/api/team").json()))
        self.assertEqual(client.post("/api/team/users", json={"login": "employee", "password": "Another safe password"}).status_code, 400)
        remote = TestClient(self.m.app, base_url="https://temporary.example", client=("192.0.2.4", 5000))
        self.assertEqual(remote.post("/api/team/login", json={"login": "employee", "password": "Another safe password"}).status_code, 200)
        self.assertEqual(remote.get("/api/team/me").json()["role"], "member")
        for path in ("/api/team", "/api/admin/backup", "/api/finance/settings"):
            self.assertEqual(remote.get(path).status_code, 403)
        self.assertEqual(remote.post("/api/team/users", json={}).status_code, 403)
        self.assertEqual(client.post("/api/team/users", headers={"Origin": "https://evil.example"}, json={}).status_code, 403)
        stolen = remote.cookies.get(COOKIE)
        self.assertEqual(remote.post("/api/team/logout").status_code, 200)
        remote.cookies.set(COOKIE, stolen)
        self.assertEqual(remote.get("/api/team/me").status_code, 401)
        self.assertEqual(client.delete(f"/api/team/members/{user['id']}").status_code, 200)
        self.assertEqual(client.delete(f"/api/team/members/{admin['id']}").status_code, 400)

    def test_https_origin_behind_loopback_proxy_and_spoofed_headers(self):
        login = "proxy-admin"
        password = "Proxy safe password 123"
        self.m.team.create_user(login, password, role="owner")
        client = TestClient(self.m.app, base_url="http://temporary.example", client=("127.0.0.1", 5000))
        for forwarded in ({"X-Forwarded-Proto": "https"}, {"Forwarded": 'for=192.0.2.1;proto="https";host=ignored.example'}):
            headers = {"Origin": "https://temporary.example", **forwarded}
            response = client.post("/api/team/login", headers=headers, json={"login": login, "password": password})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("Secure", response.headers["set-cookie"])
            token = response.cookies.get(COOKIE)
            self.assertEqual(client.get("/api/team", headers={"Cookie": f"{COOKIE}={token}", **forwarded}).status_code, 200)
            self.assertEqual(client.post("/api/team/login", headers={**forwarded, "Origin": "https://evil.example"}, json={}).status_code, 403)
            self.assertEqual(client.get("/api/team", headers=forwarded).status_code, 401)
        # An external peer cannot assert TLS termination or an alternative Host.
        remote = TestClient(self.m.app, base_url="http://temporary.example", client=("192.0.2.1", 5000))
        for proxy in ({"X-Forwarded-Proto": "https"}, {"Forwarded": "proto=https;host=temporary.example"}):
            self.assertEqual(remote.post("/api/team/login", headers={"Origin": "https://temporary.example", **proxy}, json={}).status_code, 403)
            self.assertEqual(remote.get("/api/team", headers={"X-Forwarded-For": "127.0.0.1", **proxy}).status_code, 401)
        self.assertEqual(client.post("/api/team/login", headers={"Origin": "https://evil.example", "X-Forwarded-Proto": "https", "X-Forwarded-Host": "evil.example"}, json={}).status_code, 403)

    def test_standard_loopback_tunnel_origin_and_http_cookie(self):
        self.m.team.create_user("tunnel-admin", "Tunnel safe password 123", role="owner")
        for host in ("127.0.0.1", "localhost", "[::1]"):
            client = TestClient(self.m.app, base_url=f"http://{host}:8080", client=("127.0.0.1", 5000))
            headers = {"Host": host, "Origin": f"http://{host}:8080", "X-Forwarded-Proto": "http"}
            response = client.post("/api/team/login", headers=headers, json={"login": "tunnel-admin", "password": "Tunnel safe password 123"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("Secure", response.headers["set-cookie"])
            self.assertIn("HttpOnly", response.headers["set-cookie"])
            # The browser sends the stored cookie back through the external :8080 URL.
            self.assertEqual(client.get("/api/team", headers={"Host": host}).status_code, 200)
            self.assertEqual(client.post("/api/team/logout", headers=headers).status_code, 200)
            self.assertEqual(client.get("/api/team", headers={"Host": host}).status_code, 401)
        denied = TestClient(self.m.app, base_url="http://127.0.0.1", client=("127.0.0.1", 5000))
        for origin in ("http://127.0.0.1:8081", "http://evil.example:8080", "https://127.0.0.1:8080"):
            self.assertEqual(denied.post("/api/team/login", headers={"Origin": origin}, json={}).status_code, 403)
        public = TestClient(self.m.app, base_url="http://temporary.example", client=("127.0.0.1", 5000))
        self.assertEqual(public.post("/api/team/login", headers={"Origin": "http://temporary.example:8080"}, json={}).status_code, 403)
        remote = TestClient(self.m.app, base_url="http://127.0.0.1", client=("192.0.2.1", 5000))
        self.assertEqual(remote.post("/api/team/login", headers={"Origin": "http://127.0.0.1:8080", "X-Forwarded-For": "127.0.0.1"}, json={}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
