import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services import tax
from app.services.cabinets import Cabinet, dump_cabinets, load_cabinets


def write_cfg(root: Path, accounts, default=6, bom=False):
    p = root / "Wildberries" / "wb_accounts.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"period": {"from": "2025-09"}, "tax_percent": default, "accounts": accounts}, ensure_ascii=False, indent=2)
    p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
    return p


class TaxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_match_by_token_name_and_single(self):
        write_cfg(self.root, [{"name": "МИП Нгуен К. Н.", "token": "T1", "enabled": True}, {"name": "Пример", "token": "x", "enabled": False}])
        by_token = Cabinet(id="a", name="Другое имя", token="T1")
        self.assertEqual(tax.opiu_rate(self.root, by_token, 2), {"rate": 6.0, "account": "МИП Нгуен К. Н."})
        by_name = Cabinet(id="b", name="ИП Нгуен К. Н.", token="T2")
        self.assertEqual(tax.opiu_rate(self.root, by_name, 2)["account"], "МИП Нгуен К. Н.")
        other = Cabinet(id="c", name="ИП Лыонг Т.", token="T3")
        self.assertIsNone(tax.opiu_rate(self.root, other, 2)["account"])
        self.assertEqual(tax.opiu_rate(self.root, other, 1)["account"], "МИП Нгуен К. Н.")   # единственный кабинет

    def test_write_keeps_other_fields_and_bom(self):
        p = write_cfg(self.root, [{"name": "Shop", "token": "SECRET", "enabled": True}], bom=True)
        cab = Cabinet(id="a", name="Shop", token="SECRET")
        self.assertEqual(tax.write_opiu_rate(self.root, cab, 1, 4), "Shop")
        raw = p.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        cfg = json.loads(raw.decode("utf-8-sig"))
        self.assertEqual(cfg["accounts"][0], {"name": "Shop", "token": "SECRET", "enabled": True, "tax_percent": 4})
        self.assertEqual(cfg["tax_percent"], 6)
        self.assertTrue(p.with_name("wb_accounts.json.bak").exists())
        self.assertEqual(tax.opiu_rate(self.root, cab, 1)["rate"], 4.0)
        tax.write_opiu_rate(self.root, cab, 1, None)
        self.assertEqual(tax.opiu_rate(self.root, cab, 1)["rate"], 6.0)
        self.assertIsNone(tax.write_opiu_rate(None, cab, 1, 4))

    def test_cabinet_roundtrip(self):
        c = load_cabinets(dump_cabinets([Cabinet(id="a", name="A", token="t", tax_percent=7.5)]))[0]
        self.assertEqual(c.tax_percent, 7.5)
        self.assertIsNone(load_cabinets('[{"id": "b", "token": "t"}]')[0].tax_percent)


class TaxApiTests(unittest.TestCase):
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

    def test_create_and_change_rate(self):
        from fastapi.testclient import TestClient
        root = Path(self.temp.name) / "opiu"
        p = write_cfg(root, [{"name": "МИП Нгуен К. Н.", "token": "OPIU", "enabled": True}])
        client = TestClient(self.m.app)
        with mock.patch.object(self.m, "_tax_project", lambda: root):
            self.m.save_configured_cabinets([])
            c = client.post("/api/cabinets", json={"name": "ИП Нгуен К. Н.", "token": "PANEL", "tax_percent": 6}).json()
            self.assertEqual((c["tax_percent"], c["tax_effective"], c["tax_opiu_account"]), (6.0, 6.0, "МИП Нгуен К. Н."))
            c = client.put(f"/api/cabinets/{c['id']}", json={"id": c["id"], "name": "ИП Нгуен К. Н.", "tax_percent": 4}).json()
            self.assertEqual(c["tax_effective"], 4.0)
            self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["accounts"][0]["tax_percent"], 4)
            c = client.put(f"/api/cabinets/{c['id']}", json={"id": c["id"], "name": "ИП Нгуен К. Н.", "tax_percent": None}).json()
            self.assertEqual((c["tax_percent"], c["tax_effective"]), (None, 6.0))
            self.assertNotIn("tax_percent", json.loads(p.read_text(encoding="utf-8"))["accounts"][0])
            self.assertEqual(client.put(f"/api/cabinets/{c['id']}", json={"id": c["id"], "name": "x", "tax_percent": 150}).status_code, 422)
            c = client.put(f"/api/cabinets/{c['id']}", json={"id": c["id"], "name": "ИП Нгуен К. Н.", "enabled": False, "country": "KZ",
                                                              "ads_token": "ADS"}).json()
            self.assertEqual((c["enabled"], c["currency"], c["ads_token_configured"]), (False, "KZT", True))
            self.assertEqual([t for t in self.m._rnp_tokens() if t[1] == "PANEL"], [])     # выключенный — не в РНП
            c = client.put(f"/api/cabinets/{c['id']}", json={"id": c["id"], "name": "ИП Нгуен К. Н.", "enabled": True}).json()
            with mock.patch.object(self.m, "_opiu_wb_tokens", lambda: []):
                self.assertEqual(self.m._rnp_tokens(), [("ИП Нгуен К. Н.", "PANEL", "ADS")])
            self.assertEqual(client.put(f"/api/cabinets/{c['id']}", json={"id": c["id"], "name": "x", "country": "Russia"}).status_code, 422)
            listed = client.get("/api/cabinets").json()
            self.assertNotIn("ADS", json.dumps(listed).replace("ads_token_configured", ""))
            self.assertNotIn("PANEL", json.dumps(listed))
            self.assertNotIn("OPIU", json.dumps(listed))

    def test_stats_toggle_and_key_check(self):
        from fastapi.testclient import TestClient
        root = Path(self.temp.name) / "opiu_stats"
        client = TestClient(self.m.app)
        with mock.patch.object(self.m, "_tax_project", lambda: root), \
                mock.patch.object(self.m, "_opiu_wb_tokens", lambda: []):
            self.m.save_configured_cabinets([])
            c = client.post("/api/cabinets", json={"name": "Еврозоо", "token": "KEY1"}).json()
            self.assertEqual((c["stats"], c["key_check"]), (True, None))
            self.assertEqual([t[1] for t in self.m._rnp_tokens()], ["KEY1"])
            c = client.patch(f"/api/cabinets/{c['id']}/channels", json={"stats": False}).json()
            self.assertFalse(c["stats"])
            self.assertEqual(self.m._rnp_tokens(), [])                        # без статистики — не в РНП
            self.assertEqual(self.m.sync_opiu_keys(root), {"wildberries": 0})
            account = json.loads((root / "Wildberries" / "wb_accounts.json").read_text(encoding="utf-8"))["accounts"][0]
            self.assertEqual((account["token"], account["enabled"]), ("KEY1", False))

            async def fake_check(cabinet):
                return {"status": "ok", "scopes": [{"title": "Финансы", "ok": False, "need": "stats"}],
                        "checked_at": "2026-10-27T08:00:00+00:00", "fingerprint": self.m.keycheck.fingerprint(cabinet)}
            with mock.patch.object(self.m.keycheck, "check", fake_check):
                c = client.post(f"/api/cabinets/{c['id']}/check-key").json()
            self.assertEqual(c["key_check"]["status"], "ok")                 # статистика выключена — «Финансы» не нужны
            c = client.patch(f"/api/cabinets/{c['id']}/channels", json={"stats": True}).json()
            self.assertEqual((c["key_check"]["status"], c["key_check"]["missing"]), ("partial", ["Финансы"]))
            c = client.put(f"/api/cabinets/{c['id']}", json={"id": c["id"], "name": "Еврозоо", "token": "KEY2"}).json()
            self.assertIsNone(c["key_check"])                                # ключ заменили — нужна новая проверка
            self.assertTrue(c["stats"])


if __name__ == "__main__":
    unittest.main()
