import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.models import InboxItem
from app.services.cabinets import Cabinet, dump_cabinets, load_cabinets
from app.services.notifier import Notifier
from app.services.team import Team
from app.storage import DraftStore


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DraftStore(f"sqlite:///{Path(self.temp.name) / 'db.sqlite3'}")


class CabinetTests(unittest.TestCase):
    def test_roundtrip_and_defaults(self):
        old = load_cabinets('[{"id":"main","name":"ИП","token":"t"}]')[0]
        self.assertEqual((old.marketplace, old.reviews, old.questions, old.chats), ("wildberries", True, True, False))
        c = Cabinet(id="oz", name="Ozon ИП", token="k", marketplace="ozon", client_id="42", chats=True, reviews=False)
        back = load_cabinets(dump_cabinets([c]))[0]
        self.assertEqual(back, c)
        self.assertNotIn("token", c.public())
        self.assertFalse(c.public()["supported"])


class TeamTests(Base):
    def test_invite_join_once_and_disable(self):
        team = Team(self.store)
        invite = team.create_invite("owner@example.com")
        self.assertEqual(invite["status"], "active")
        member, token = team.join(invite["code"], "Анна", "anna@example.com")
        self.assertEqual(team.by_token(token)["name"], "Анна")
        self.assertEqual(team.invite(invite["code"])["status"], "used")
        with self.assertRaises(ValueError):
            team.join(invite["code"], "Боб")
        team.set_active(member["id"], False)
        self.assertIsNone(team.by_token(token))
        self.assertIsNone(team.by_token("wrong"))

    def test_expired_and_validation(self):
        team = Team(self.store)
        invite = team.create_invite("x")
        with self.store._connect() as c:
            c.execute("UPDATE team_invites SET expires_at='2020-01-01T00:00:00+00:00'")
        self.assertEqual(team.invites()[0]["status"], "expired")
        with self.assertRaisesRegex(ValueError, "истёк"):
            team.join(invite["code"], "Анна")
        with self.assertRaisesRegex(ValueError, "имя"):
            team.join(team.create_invite("x")["code"], "А")


class NotifierTests(Base):
    def test_settings_masking_and_migration(self):
        self.store.set_setting("module:telegram", json.dumps({"enabled": True, "bot_token": "1:a", "chat_id": "5", "notify_new_reviews": True}))
        n = Notifier(self.store)
        self.assertTrue(n.config()["telegram"]["enabled"])           # перенос со старой панели
        pub = n.public()
        self.assertEqual(pub["telegram"]["bot_token"], "")
        self.assertTrue(pub["telegram"]["bot_token_set"])
        n.save({"telegram": {"chat_id": "7", "bot_token": ""}})        # пустой токен = оставить прежний
        self.assertEqual(n.config()["telegram"]["bot_token"], "1:a")
        with self.assertRaises(ValueError):
            n.save({"email": {"enabled": True, "host": ""}})

    def test_new_items_negative_only_and_cabinet_filter(self):
        n = Notifier(self.store)
        n.save({"telegram": {"enabled": True, "bot_token": "1:a", "chat_id": "5"}, "events": {"reviews_negative_only": True}})
        items = [InboxItem(id="1", cabinet_id="main", item_type="review", text="Мало", rating=2),
                 InboxItem(id="2", cabinet_id="main", item_type="review", text="Супер", rating=5),
                 InboxItem(id="3", cabinet_id="main", item_type="question", text="Какой размер?")]
        cabinet = Cabinet(id="main", name="ИП", token="t")
        with mock.patch("app.services.notifier.send_telegram_message", new=mock.AsyncMock(return_value=True)) as send:
            asyncio.run(n.new_items(cabinet, items))
            text = send.call_args[0][1]
            self.assertIn("Новых негативных отзывов: 1", text)
            self.assertNotIn("Супер", text)
            self.assertIn("Новых вопросов: 1", text)
            n.save({"cabinets": ["other"]})
            send.reset_mock()
            asyncio.run(n.new_items(cabinet, items))
            send.assert_not_called()
        self.assertTrue(n.status()["telegram"]["ok"])

    def test_journal_summary(self):
        n = Notifier(self.store)
        n.init_cursor()
        n.save({"telegram": {"enabled": True, "bot_token": "1:a", "chat_id": "5"}, "events": {"reviews_answered": True, "questions_status": True}})
        self.store.audit("published", "r1", "type=review")
        self.store.audit("published", "q1", "type=question")
        self.store.audit("auto_question_generated", "q2", "status=moderation;template=x")
        with mock.patch("app.services.notifier.send_telegram_message", new=mock.AsyncMock(return_value=True)) as send:
            counts = asyncio.run(n.scan_journal())
            self.assertEqual(counts["review_answers"], 1)
            text = send.call_args[0][1]
            self.assertIn("ответов на отзывы: 1", text)
            self.assertIn("ждут проверки: 1", text)
            send.reset_mock()
            asyncio.run(n.scan_journal())                                 # повторно не шлём
            send.assert_not_called()


class AccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, {"WBOPS_DATABASE_URL": f"sqlite:///{Path(cls.temp.name) / 'db.sqlite3'}"})
        cls.env.start()
        from app.config import get_settings
        get_settings.cache_clear()
        import importlib
        import app.main
        cls.m = importlib.reload(app.main)

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        from app.config import get_settings
        get_settings.cache_clear()
        cls.temp.cleanup()

    def test_remote_access_requires_invite(self):
        from fastapi.testclient import TestClient
        owner = TestClient(self.m.app)
        remote = TestClient(self.m.app, client=("192.168.1.20", 5000))
        self.assertEqual(remote.get("/api/inbox").status_code, 401)
        self.assertEqual(remote.get("/", follow_redirects=False).status_code, 303)
        self.assertEqual(remote.get("/join").status_code, 200)
        code = owner.post("/api/team/invites", json={}).json()["code"]
        self.assertEqual(remote.post("/api/team/join", json={"code": code, "name": "Анна"}).status_code, 200)
        self.assertEqual(remote.get("/api/inbox").status_code, 200)
        for path in ("/api/team", "/api/notifications", "/api/finance/settings", "/api/admin/backup", "/api/rnp/status"):
            self.assertEqual(remote.get(path).status_code, 403, path)
        self.assertEqual(remote.get("/rnp", follow_redirects=False).status_code, 303)
        self.assertEqual(remote.post("/api/cabinets", json={"name": "x", "token": "t"}).status_code, 403)
        self.assertEqual(remote.get("/api/cabinets").status_code, 200)
        self.assertNotIn("token", json.dumps(remote.get("/api/cabinets").json()).replace("token_configured", ""))

    def test_rnp_api(self):
        from fastapi.testclient import TestClient
        owner = TestClient(self.m.app)
        with mock.patch.object(self.m, "_rnp_tokens", lambda: []):
            d = owner.get("/api/rnp", params={"from": "2026-10-01", "to": "2026-10-14"}).json()
            self.assertEqual((d["total"]["orders"], d["has_tokens"], len(d["days"])), (0, False, 14))
            self.assertEqual(owner.post("/api/rnp/sync").status_code, 400)
        self.assertEqual(owner.get("/api/rnp", params={"from": "2026-10-14", "to": "2026-10-01"}).status_code, 400)
        self.assertEqual(owner.put("/api/rnp/plan", json={"article": "A-1", "month": "2026-10", "orders_per_day": 5}).status_code, 200)
        note = owner.post("/api/rnp/notes", json={"day": "2026-10-02", "article": "A-1", "text": "Подняли цену"}).json()
        self.assertEqual(owner.delete(f"/api/rnp/notes/{note['id']}").status_code, 200)
        self.assertEqual(owner.get("/api/rnp/heatmap", params={"from": "2026-10-01", "to": "2026-10-02", "metric": "x"}).status_code, 400)
        self.assertIn("window.WBOPSRnp", owner.get("/rnp").text)

    def test_cabinet_channels_and_unsupported_sync(self):
        from fastapi.testclient import TestClient
        owner = TestClient(self.m.app)
        oz = owner.post("/api/cabinets", json={"name": "Ozon", "marketplace": "ozon", "client_id": "1"}).json()
        self.assertTrue(oz["id"].startswith("ozon-"))
        self.assertTrue(owner.patch(f"/api/cabinets/{oz['id']}/channels", json={"chats": True}).json()["chats"])
        r = owner.post(f"/api/sync/{oz['id']}")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(owner.post("/api/cabinets", json={"name": "WB"}).status_code, 400)   # для WB нужен токен
        self.assertEqual(owner.delete(f"/api/cabinets/{oz['id']}").status_code, 200)


if __name__ == "__main__":
    unittest.main()
