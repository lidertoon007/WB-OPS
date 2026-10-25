import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from app.services import stocks as stocks_module
from app.services.cabinets import Cabinet
from app.services.stocks import Stocks, StocksError
from app.storage import DraftStore

CARDS = [{"nmID": 111, "vendorCode": "3140-D1", "title": "Джинсы", "brand": "Deemivis", "subjectName": "Джинсы", "photos": [{"tm": "https://p/1.webp"}],
          "sizes": [{"chrtID": 1, "techSize": "32", "wbSize": "48", "skus": ["200001"]}, {"chrtID": 2, "techSize": "34", "wbSize": "50", "skus": ["200002"]},
                    {"chrtID": 3, "techSize": "36", "wbSize": "52", "skus": ["200003"]}]}]


class Fake:
    def __init__(self):
        self.amounts = {1: 12, 2: 6, 3: 0}
        self.puts = []

    def __call__(self, request):
        p = request.url.path
        if p == "/content/v2/get/cards/list":
            return httpx.Response(200, json={"cards": CARDS, "cursor": {"total": 1}})
        if p == "/api/v3/warehouses":
            return httpx.Response(200, json=[{"id": 77, "name": "Мой склад"}])
        if p == "/api/v3/stocks/77" and request.method == "POST":
            ids = json.loads(request.content)["chrtIds"]
            return httpx.Response(200, json={"stocks": [{"chrtId": i, "amount": self.amounts[i]} for i in ids if self.amounts.get(i)]})
        if p == "/api/v3/stocks/77" and request.method == "PUT":
            body = json.loads(request.content)
            self.puts.append(body)
            for s in body["stocks"]:
                self.amounts[s["chrtId"]] = s["amount"]
            return httpx.Response(204)
        return httpx.Response(404)


class StocksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DraftStore(f"sqlite:///{Path(self.temp.name) / 'db.sqlite3'}")
        self.fake = Fake()
        real = httpx.Client
        for p in (mock.patch.object(stocks_module.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(self.fake), timeout=5)),
                  mock.patch.object(stocks_module.time, "sleep", lambda *_: None)):
            p.start()
            self.addCleanup(p.stop)
        self.s = Stocks(self.store, lambda: [Cabinet(id="wb1", name="ИП", token="t")], lambda: {"3140-D1": {"wb_stock": 40}})
        self.assertEqual(self.s.refresh(), {"wb1": "ok"})

    def rows(self):
        return {r["chrt_id"]: r for r in self.s.rows()}

    def test_rows(self):
        r = self.rows()
        self.assertEqual((r[1]["amount"], r[1]["status"], r[1]["size"], r[1]["ru_size"], r[1]["barcode"]), (12, "ok", "32", "48", "200001"))
        self.assertEqual(r[2]["status"], "ok")            # 6 > 5
        self.assertEqual(r[3]["status"], "zero")
        self.assertFalse(r[3]["watched"])                 # остатка ещё не было — не следим
        self.assertEqual((r[1]["warehouse"], r[1]["wb_stock"]), ("Мой склад", 40))

    def test_update_and_history(self):
        res = self.s.update([{"id": "wb1:77:3", "amount": 20}, {"id": "wb1:77:2", "amount": -1}, {"id": "x:1:1", "amount": 1}], author="Анна")
        self.assertEqual(res["ok"], 1)
        self.assertIn("wb1:77:2", res["errors"])
        self.assertEqual(self.fake.puts, [{"stocks": [{"chrtId": 3, "amount": 20}]}])
        r = self.rows()[3]
        self.assertEqual((r["amount"], r["watched"]), (20, True))
        h = self.s.history()[0]
        self.assertEqual((h["author"], h["old"], h["new"], h["status"]), ("Анна", 0, 20, "ok"))
        with self.assertRaises(StocksError):
            self.s.update([])

    def test_alerts_once_then_zero_then_reset(self):
        self.assertEqual(self.s.check_alerts()["messages"], [])
        self.fake.amounts[2] = 4                                   # 34-й размер дошёл до порога
        self.s.refresh()
        res = self.s.check_alerts()
        self.assertEqual((res["low"], res["zero"]), (1, 0))
        subject, text = res["messages"][0]
        self.assertEqual(subject, "Остатки FBS · ИП")
        self.assertIn("3140-D1 · размер 34 (50) — 4 шт.", text)
        self.assertEqual(self.s.check_alerts()["messages"], [])     # повторно не шлём
        self.fake.amounts[2] = 0
        self.s.refresh()
        res = self.s.check_alerts()
        self.assertEqual((res["low"], res["zero"]), (0, 1))
        self.assertIn("Закончились", res["messages"][0][1])
        self.s.update([{"id": "wb1:77:2", "amount": 30}])          # пополнили — слежение заново
        self.fake.amounts[2] = 3
        self.s.refresh()
        self.assertEqual(self.s.check_alerts()["low"], 1)
        self.s.set_watch(["wb1:77:1"], threshold=20)                # свой порог у размера
        self.assertEqual(self.rows()[1]["status"], "low")
        self.s.set_watch(["wb1:77:1"], muted=True)
        self.assertFalse(self.rows()[1]["watched"])
        self.s.save_settings({"monitor": False})
        self.assertEqual(self.s.check_alerts()["messages"], [])
        self.assertTrue(self.s.check_alerts(force=True)["messages"])
        with self.assertRaises(StocksError):
            self.s.save_settings({"interval_min": 1})

    def test_archive_and_size_velocity(self):
        from datetime import date
        from app.services.rnp import RNP
        RNP(self.store, lambda: [], lambda: None, lambda: [])            # создаёт rnp_orders с размерами
        today = (date.today() - __import__("datetime").timedelta(days=1)).isoformat()   # вчера — полный день
        with self.store._connect() as c:
            for i in range(28):                                              # 2 заказа в день по 32-му размеру (по баркоду)
                c.execute("INSERT INTO rnp_orders(srid, cabinet, day, nm_id, article, is_cancel, barcode, tech_size) VALUES(?,?,?,?,?,?,?,?)",
                          (f"a{i}", "ИП", today, 111, "3140-D1", 0, "200001", "32"))
            for i in range(7):                                               # 0,5 в день по 34-му (только размер)
                c.execute("INSERT INTO rnp_orders(srid, cabinet, day, nm_id, article, is_cancel, barcode, tech_size) VALUES(?,?,?,?,?,?,?,?)",
                          (f"b{i}", "ИП", today, 111, "3140-D1", 0, "", "34"))
        r = self.rows()
        self.assertEqual((r[1]["size_orders_per_day"], r[2]["size_orders_per_day"], r[3]["size_orders_per_day"]), (2.0, 0.5, 0))
        self.assertEqual(r[1]["orders_per_day"], 2.5)
        self.fake.amounts[2] = 2
        self.s.refresh()
        self.s.set_archive(["wb1:111"], True, "Анна")
        r = self.rows()
        self.assertTrue(r[2]["archived"])
        self.assertFalse(r[2]["watched"])
        self.assertEqual(self.s.check_alerts()["messages"], [])              # архив — без оповещений
        self.s.set_archive(["wb1:111"], False)
        self.assertEqual(self.s.check_alerts()["low"], 1)


if __name__ == "__main__":
    unittest.main()
