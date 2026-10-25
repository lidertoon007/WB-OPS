import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import httpx

from app.services import fbs_analytics as fa
from app.services.cabinets import Cabinet
from app.services.fbs_analytics import FbsAnalytics, commission_effect, status_group
from app.storage import DraftStore

NOW = datetime.now(timezone.utc).replace(microsecond=0)
T = lambda h: (NOW - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")      # noqa: E731
ORDERS = [
    {"id": 1, "rid": "r1", "createdAt": T(100), "warehouseId": 7, "nmId": 111, "chrtId": 11, "article": "3140-D1", "convertedPrice": 500000, "supplyId": "WB-GI-1"},
    {"id": 2, "rid": "r2", "createdAt": T(99), "warehouseId": 7, "nmId": 111, "chrtId": 12, "article": "3140-D1", "convertedPrice": 400000, "supplyId": "WB-GI-2"},
    {"id": 3, "rid": "r3", "createdAt": T(98), "warehouseId": 7, "nmId": 222, "chrtId": 21, "article": "B012", "convertedPrice": 300000, "supplyId": "WB-GI-1"},
    {"id": 4, "rid": "r4", "createdAt": T(5), "warehouseId": 7, "nmId": 222, "chrtId": 21, "article": "B012", "convertedPrice": 300000},
]
STATUSES = {1: ("complete", "sold"), 2: ("complete", "canceled_by_client"), 3: ("complete", "sold"), 4: ("new", "waiting")}
SUPPLIES = {"WB-GI-1": T(90), "WB-GI-2": T(39)}      # отгрузка через 10 ч и через 60 ч


class Fake:
    def __call__(self, request):
        p = request.url.path
        if p == "/api/v3/warehouses":
            return httpx.Response(200, json=[{"id": 7, "name": "Мой склад"}])
        if p == "/api/v3/orders":
            return httpx.Response(200, json={"orders": ORDERS, "next": 0})
        if p == "/api/v3/orders/status":
            ids = json.loads(request.content)["orders"]
            return httpx.Response(200, json={"orders": [{"id": i, "supplierStatus": STATUSES[i][0], "wbStatus": STATUSES[i][1]} for i in ids]})
        if p.startswith("/api/v3/supplies/"):
            sid = p.rsplit("/", 1)[1]
            return httpx.Response(200, json={"id": sid, "scanDt": SUPPLIES[sid], "closedAt": SUPPLIES[sid], "done": True})
        if p == "/api/v1/supplier/sales":
            return httpx.Response(200, json=[{"srid": "r1", "saleID": "S1", "date": (NOW - timedelta(hours=18)).astimezone(timezone(timedelta(hours=3))).strftime("%Y-%m-%dT%H:%M:%S")},
                                             {"srid": "r3", "saleID": "S2", "date": (NOW - timedelta(hours=42)).astimezone(timezone(timedelta(hours=3))).strftime("%Y-%m-%dT%H:%M:%S")}])
        return httpx.Response(404)


class FbsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DraftStore(f"sqlite:///{Path(self.temp.name) / 'db.sqlite3'}")
        real = httpx.Client
        for p in (mock.patch.object(fa.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(Fake()), timeout=5)),
                  mock.patch.object(fa.time, "sleep", lambda *_: None)):
            p.start()
            self.addCleanup(p.stop)
        self.f = FbsAnalytics(self.store, lambda: [Cabinet(id="wb1", name="ИП", token="t")], lambda: {"3140-D1": {"brand": "Deemivis", "category": "Джинсы"}})

    def test_rules(self):
        self.assertEqual([commission_effect(h) for h in (5, 20, 45, 50, 56, 70)], [-5.0, -3.5, 0.0, 0.6, 2.8, 9.9])
        self.assertEqual(status_group("complete", "sold"), "sold")
        self.assertEqual(status_group("confirm", "waiting"), "assembly")
        self.assertEqual(status_group("cancel", "canceled"), "seller_cancel")
        self.assertEqual(status_group("complete", "canceled_by_client"), "refused")

    def test_sync_and_report(self):
        self.assertEqual(self.f.sync()["status"], "done")
        today = datetime.now(timezone(timedelta(hours=3))).date()
        rep = self.f.report((today - timedelta(days=7)).isoformat(), today.isoformat(), {})
        s = rep["summary"]
        self.assertEqual((s["orders"], s["sold"], s["buyout_pct"], s["in_progress"]), (4, 2, 66.7, 1))
        self.assertEqual(s["bands"], {"lt13": 2, "gt60": 1})
        self.assertEqual(s["late"], 1)
        # 5000 × −5% + 3000 × −5% + 4000 × (0,45% × 12 ч) = −250 − 150 + 216
        self.assertAlmostEqual(s["effect_rub"], -184.0)
        self.assertAlmostEqual(s["avg_days_to_buyout"], 2.5)
        # упущенная выгода: задание 2 (60 ч, 4000 ₽) — недополученная скидка 5% (200) + штраф 5,4% (216)
        self.assertEqual((s["lost_orders"], s["lost_discount"], s["lost_penalty"], s["lost_rub"]), (1, 200.0, 216.0, 416.0))
        self.assertAlmostEqual(s["potential_rub"], -600.0)
        self.assertEqual(s["lost_bands"]["gt60"]["lost"], 416.0)
        self.assertAlmostEqual(sum(d["lost_discount"] + d["lost_penalty"] for d in rep["by_day"]), 416.0)   # (72 ч + 48 ч) / 2
        self.assertEqual(rep["warehouses"][0]["warehouse"], "Мой склад")
        arts = {a["article"]: a for a in rep["articles"]}
        self.assertEqual(arts["3140-D1"]["brand"], "Deemivis")
        feed = self.f.feed((today - timedelta(days=7)).isoformat(), today.isoformat(), {}, status="late")
        self.assertEqual([r["id"] for r in feed["items"]], [2])
        self.assertEqual(feed["counts"]["sold"], 2)
        only = self.f.report((today - timedelta(days=7)).isoformat(), today.isoformat(), {"brand": "Deemivis"})
        self.assertEqual(only["summary"]["orders"], 2)


if __name__ == "__main__":
    unittest.main()
