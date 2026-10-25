import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import httpx

from app.services import repricer as rp_module
from app.services.cabinets import Cabinet
from app.services.repricer import Repricer, RepricerError
from app.storage import DraftStore

GOODS = [{"nmID": 111, "vendorCode": "de-90", "discount": 50, "clubDiscount": 0, "editableSizePrice": False,
          "sizes": [{"sizeID": 1, "price": 10000, "discountedPrice": 5000, "techSizeName": "32"}, {"sizeID": 2, "price": 10000, "discountedPrice": 5000, "techSizeName": "34"}]},
         {"nmID": 222, "vendorCode": "B012", "discount": 0, "sizes": [{"sizeID": 3, "price": 3000, "discountedPrice": 3000, "techSizeName": "0"}]}]
ECON = {"articles": {"DE-90": {"buyout": 0.5, "commission": 0.3, "acquiring": 0.02, "logistics_per_order": 100, "paid_share": 0.8, "ads_share": 0.05}},
        "cabinet": {"buyout": 0.5, "commission": 0.3, "acquiring": 0.02, "logistics_per_order": 100, "paid_share": 0.8, "ads_share": 0.05}, "tax_rate": 6}
CATALOG = {"DE-90": {"name": "Джинсы", "brand": "Deemivis", "category": "Джинсы", "stock": 10, "cost": 1000, "prep": 100},
           "B012": {"name": "Футболка", "brand": "X", "category": "Футболки", "stock": 0, "cost": None, "prep": 0}}


class Fake:
    def __init__(self):
        self.uploads, self.status = [], 3

    def __call__(self, request):
        p = request.url.path
        if p == "/api/v2/list/goods/filter":
            return httpx.Response(200, json={"data": {"listGoods": GOODS}})
        if "card.wb.ru" in str(request.url):
            return httpx.Response(200, json={"products": [{"id": 111, "sizes": [{"price": {"basic": 1000000, "product": 400000}}]}]})
        if p == "/api/v2/upload/task":
            self.uploads.append(json.loads(request.content))
            return httpx.Response(200, json={"data": {"id": 55, "alreadyExists": False}, "error": False})
        if p == "/api/v2/history/tasks":
            return httpx.Response(200, json={"data": {"uploadID": 55, "status": self.status}})
        if p == "/api/v2/history/goods/task":
            return httpx.Response(200, json={"data": {"historyGoods": [{"nmID": 111, "status": 3, "errorText": "Товар в акции"}]}})
        return httpx.Response(404)


class RepricerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DraftStore(f"sqlite:///{Path(self.temp.name) / 'db.sqlite3'}")
        self.fake = Fake()
        real = httpx.Client
        for p in (mock.patch.object(rp_module.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(self.fake), timeout=5)),
                  mock.patch.object(rp_module.time, "sleep", lambda *_: None)):
            p.start()
            self.addCleanup(p.stop)
        self.r = Repricer(self.store, lambda: [Cabinet(id="wb1", name="ИП", token="t")], lambda: ECON, lambda: CATALOG)
        self.r.refresh()

    def row(self, nm):
        return next(x for x in self.r.rows() if x["nm_id"] == nm)

    def test_rows_spp_and_margin(self):
        r = self.row(111)
        self.assertEqual((r["price"], r["discount"], r["price_before_spp"]), (10000, 50, 5000))
        self.assertAlmostEqual(r["spp"], 20)
        self.assertEqual((r["client_price"], r["spp_source"]), (4000, "витрина"))
        # 5000 − 1500 комиссия − 100 эквайринг − 200 логистика − 250 реклама − 240 налог − 1100 себестоимость = 1610
        self.assertEqual(r["profit"], 1610)
        self.assertAlmostEqual(r["margin"], 32.2)
        b = self.row(222)
        self.assertFalse(b["in_stock"])
        self.assertIsNone(b["margin"])
        self.r.save_settings({"wallet": {"wb1": 2}})
        self.assertEqual(self.row(111)["client_wallet"], 3920)

    def test_preview_limits(self):
        res = self.r.preview(mass={"kind": "before", "action": "up", "unit": "pct", "value": 10}, ids=["wb1:111"])[0]
        self.assertEqual((res["new_base"], res["new_price"]), (11000, 5500))
        self.assertFalse(res["errors"])
        self.assertGreater(res["new_margin"], res["old_margin"])
        res = self.r.preview(changes=[{"id": "wb1:111", "new_price": 7000}])[0]
        self.assertTrue(res["confirm"])                                       # +40% > 20% за операцию
        self.assertTrue(any("дневного лимита" in e for e in res["errors"]))   # и > 30% за сутки
        self.r.save_settings({"min_price": 4800})
        self.r.set_items(["wb1:111"], min_price=4000)                         # индивидуальный диапазон главнее
        res = self.r.preview(changes=[{"id": "wb1:111", "new_price": 4500}])[0]
        self.assertFalse(res["errors"], res)
        res = self.r.preview(changes=[{"id": "wb1:111", "new_price": 3900}])[0]
        self.assertTrue(any("артикула" in e for e in res["errors"]))
        res = self.r.preview(changes=[{"id": "wb1:111", "new_client": 4400}])[0]  # цена для клиента → до СПП: 4400 / 0.8
        self.assertEqual(res["new_price"], 5500)
        with self.assertRaises(RepricerError):
            self.r.save_settings({"min_price": 9000, "max_price": 100})

    def wait_op(self, op):
        for _ in range(500):
            if self.r.op(op["id"])["status"] != "running":
                return self.r.op(op["id"])
            threading.Event().wait(0.01)
        self.fail("операция не закончилась")

    def test_apply_and_history(self):
        with self.assertRaises(RepricerError):
            self.r.apply([{"id": "wb1:111", "new_price": 6500}], confirmed_large=False)       # +30% — нужно подтверждение
        op = self.wait_op(self.r.apply([{"id": "wb1:111", "new_price": 5500}], confirmed_large=False, author="Анна"))
        self.assertEqual((op["status"], op["ok"]), ("Выполнено", 1))
        self.assertEqual(self.fake.uploads[0], {"data": [{"nmID": 111, "price": 11000, "discount": 50}]})
        self.assertEqual(self.row(111)["price_before_spp"], 5500)              # кэш обновился
        self.assertEqual(self.r.item_history("wb1:111")[0]["author"], "Анна")
        self.assertEqual(self.r.history()[0]["status"], "Выполнено")
        self.r.save_settings({"min_interval_hours": 2})
        res = self.r.preview(changes=[{"id": "wb1:111", "new_price": 5600}])[0]
        self.assertTrue(any("следующее изменение" in e for e in res["errors"]))
        self.r.save_settings({"min_interval_hours": 0})
        self.fake.status = 5
        op = self.wait_op(self.r.apply([{"id": "wb1:111", "new_price": 5600}], confirmed_large=False))
        self.assertEqual(op["status"], "Ошибка")
        self.assertEqual(op["items"]["wb1:111"], "Товар в акции")

    def test_refresh_rate_limit(self):
        with self.assertRaises(RepricerError):
            self.r.refresh(force=True)
        self.assertEqual(self.r.refresh(), {"wb1": "свежие"})


class RepricerApiTests(unittest.TestCase):
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

    def test_import_and_access(self):
        from fastapi.testclient import TestClient
        import openpyxl
        owner, remote = TestClient(self.m.app), TestClient(self.m.app, client=("192.168.1.20", 5000))
        self.assertIn(remote.get("/api/repricer").status_code, (401, 403))
        row = {"id": "wb1:111", "cabinet_id": "wb1", "cabinet": "ИП", "nm_id": 111}
        wb = openpyxl.Workbook()
        wb.active.append(["Магазин", "Артикул МП", "Новая цена до СПП", "Новая цена для клиента"])
        wb.active.append(["ИП", 111, 5200, ""])
        wb.active.append(["ИП", 999, 100, ""])
        wb.active.append(["ИП", 111, "", ""])
        buf = io.BytesIO()
        wb.save(buf)
        with mock.patch.object(self.m.repricer, "rows", lambda: [row]), \
                mock.patch.object(self.m.repricer, "preview", lambda changes=None, mass=None, ids=None: [{"id": c["id"], "new_price": c.get("new_price"), "errors": []} for c in changes]):
            res = owner.post("/api/repricer/import", files={"file": ("prices.xlsx", buf.getvalue())}).json()
        self.assertEqual(res["skipped"], 1)
        self.assertEqual([i["id"] for i in res["items"]], ["wb1:111", "?:999"])
        self.assertEqual(res["items"][0]["new_price"], 5200)
        self.assertEqual(res["items"][1]["errors"], ["артикул не найден"])


if __name__ == "__main__":
    unittest.main()
