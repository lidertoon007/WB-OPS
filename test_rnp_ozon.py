import json
import os

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from app.services import rnp_ozon as ozon_module
from app.services.rnp import RNP, compose
from app.services.rnp_ozon import NO_ARTICLE, OzonRNP
from app.storage import DraftStore
from test_rnp import COSTS, D, economics


def posting(number, day, offer, sku, qty, price, status="delivered"):
    return {"posting_number": number, "status": status, "in_process_at": day + "T10:00:00Z",
            "products": [{"sku": sku, "offer_id": offer, "quantity": qty, "price": {"amount": str(price), "currency": "RUB"}}]}


def ozon_api(calls, funnel_metrics=2):
    """Seller API Ozon: заказ 2 шт A-1, отменённый заказ, воронка по двум SKU, начисление за продвижение."""
    def handler(request: httpx.Request):
        path, body = request.url.path, json.loads(request.content or b"{}")
        calls.append(path)
        assert request.headers["Client-Id"] == "123" and request.headers["Api-Key"] == "key-secret"
        if path == "/v3/posting/fbo/list":
            return httpx.Response(200, json={"result": {"postings": [posting("P1", D(0), "a-1", 111, 2, 500)], "has_next": False, "cursor": ""}})
        if path == "/v4/posting/fbs/list":
            return httpx.Response(200, json={"result": {"postings": [posting("P2", D(0), "a-1", 111, 1, 600, "cancelled")], "has_next": False}})
        if path == "/v1/analytics/data":
            assert body["dimension"] == ["sku", "day"]
            return httpx.Response(200, json={"result": {"data": [
                {"dimensions": [{"id": "111"}, {"id": D(0)}], "metrics": [1000, 50][:funnel_metrics]},
                {"dimensions": [{"id": "222"}, {"id": D(0)}], "metrics": [300, 6][:funnel_metrics]}]}})
        if path == "/v3/product/list":
            return httpx.Response(200, json={"result": {"items": [{"product_id": 9}], "last_id": ""}})
        if path == "/v3/product/info/list":
            return httpx.Response(200, json={"items": [{"offer_id": "b-2", "sku": 222}]})
        if path == "/v1/finance/accrual/types":
            return httpx.Response(200, json={"result": {"accrual_types": [{"id": 77, "name": "Оплата за клик", "description": "Продвижение: оплата за клик"}]}})
        if path == "/v1/finance/accrual/by-day":
            if body["date"] != D(0):
                return httpx.Response(200, json={"result": {"accruals": []}})
            return httpx.Response(200, json={"result": {"accruals": [
                {"accrual_id": "x1", "total_amount": {"amount": "-150"}, "item_fees": {"fees": [{"sku": 111, "fees": [{"type_id": 77, "accrued": "-150"}]}]}},
                {"accrual_id": "x2", "total_amount": {"amount": "-40"}, "non_item_fee": {"type_id": 77, "accrued": "-40"}}]}})
        return httpx.Response(404)
    return handler


class OzonRNPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DraftStore(f"sqlite:///{Path(self.temp.name) / 'db.sqlite3'}")
        self.accounts = [("Еврозоо", "123", "key-secret")]
        self.r = OzonRNP(self.store, lambda: self.accounts, economics, lambda: COSTS)

    def sync(self, handler):
        real = httpx.Client
        with mock.patch.object(ozon_module.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler))), \
                mock.patch.object(self.r.limiter, "wait", lambda *a: None):
            return self.r.sync()

    def test_sync_orders_funnel_ads(self):
        calls = []
        state = self.sync(ozon_api(calls))
        self.assertEqual(state["status"], "done", state)
        self.sync(ozon_api(calls))                         # повторная загрузка не дублирует заказы и рекламу
        self.assertIn("/v3/posting/fbo/list", calls)
        self.assertIn("/v4/posting/fbs/list", calls)
        t = self.r.table(D(0), D(0))
        row = t["by_day"][D(0)]
        self.assertEqual(row["orders"], 3)                 # 2 шт в P1 + 1 шт в отменённом P2
        self.assertEqual(row["orders_sum"], 1600)
        self.assertEqual(row["cancels"], 1)
        self.assertAlmostEqual(row["cancel_rate"], 100 / 3)
        self.assertEqual((row["card_views"], row["tocart"]), (1300, 56))
        self.assertAlmostEqual(row["ads"], 190)
        self.assertIsNone(row["spp"])                      # СПП у Ozon нет
        self.assertIsNone(row["ctr"])                      # показов и кликов рекламы без Performance API нет
        self.assertEqual(t["sources"]["orders"], "api")
        self.assertEqual((t["sources"]["ads"], t["sources"]["ad_clicks"], t["sources"]["funnel"]), ("api", False, True))
        arts = {r["key"]: r for r in t["articles"]}
        self.assertAlmostEqual(arts["A-1"]["ads"], 150)
        self.assertAlmostEqual(arts[NO_ARTICLE]["ads"], 40)  # продвижение без товара — отдельной строкой
        cells = self.r.table(D(0), D(0), with_cells=True)["cells"]
        self.assertEqual(cells[(D(0), "B-2")]["card_views"], 300)    # SKU без заказов — артикул из справочника товаров
        self.assertAlmostEqual(cells[(D(0), "A-1")]["cart_rate"], 5)
        self.assertTrue(self.store.get_setting("rnp_ozon_synced_at", ""))
        self.assertFalse(self.store.get_setting("rnp_synced_at", ""))      # время загрузки WB не трогаем
        with self.store._connect() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='rnp_orders'").fetchone())   # в таблицы WB не пишем

    def test_forecast_uses_ozon_economics(self):
        self.sync(ozon_api([]))
        row = self.r.table(D(0), D(0))["by_day"][D(0)]
        # выкуп 50% по юнит-экономике: выручка 800, комиссия 30% = 240, себестоимость 1.5 × 250
        self.assertAlmostEqual(row["revenue"], 800)
        self.assertAlmostEqual(row["commission"], 240)
        self.assertAlmostEqual(row["cogs"] + row["prep"], 375)
        self.assertIsNotNone(row["margin"])

    def test_funnel_without_premium(self):
        state = self.sync(ozon_api([], funnel_metrics=1))  # Ozon молча отдал не все метрики
        self.assertEqual(state["status"], "done", state)
        t = self.r.table(D(0), D(0))
        self.assertEqual((t["sources"]["funnel"], t["sources"]["funnel_off"]), (False, ["Еврозоо"]))
        self.assertEqual(t["by_day"][D(0)]["orders"], 3)

    def test_refused_key(self):
        state = self.sync(lambda request: httpx.Response(403, json={"message": "denied"}))
        self.assertEqual(state["status"], "partial")
        self.assertTrue(all("key-secret" not in e for e in state["errors"]))
        self.accounts = []
        self.assertEqual(self.r.sync()["status"], "error")

    def test_cabinet_filter_and_plans(self):
        self.sync(ozon_api([]))
        self.assertEqual(self.r.table(D(0), D(0), cabinets={"Другой"})["total"]["orders"], 0)
        self.assertEqual(self.r.table(D(0), D(0), cabinets={"Еврозоо"})["total"]["orders"], 3)
        wb = RNP(self.store, lambda: [], economics, lambda: COSTS)
        self.r.set_plan("a-1", D(0)[:7], 5, 20)
        self.assertEqual(wb.plans(), {})                   # план Ozon отдельно от плана WB
        self.assertEqual(self.r.plans()[("A-1", D(0)[:7])]["orders_per_day"], 5)

    def test_compose_wb_and_ozon(self):
        self.sync(ozon_api([]))
        wb = RNP(self.store, lambda: [], economics, lambda: COSTS)
        with self.store._connect() as c:
            c.execute("INSERT INTO rnp_orders(srid,cabinet,day,nm_id,article,price,spp,finished,is_cancel) VALUES('s1','WB',?,1,'a-1',1000,20,800,0)", (D(0),))
        wb.set_plan("a-1", D(0)[:7], 2, 10)
        self.r.set_plan("a-1", D(0)[:7], 3, 20)
        t = compose([wb.collect(D(0), D(0)), self.r.collect(D(0), D(0))])
        self.assertEqual(t["total"]["orders"], 4)
        self.assertEqual(t["total"]["orders_sum"], 2600)
        self.assertEqual(t["marketplaces"], ["wb", "ozon"])
        self.assertEqual(set(t["sources_by_mp"]), {"wb", "ozon"})
        a = next(r for r in t["articles"] if r["key"] == "A-1")
        self.assertEqual(a["orders"], 4)
        self.assertEqual(a["plan_orders_per_day"], 5)
        self.assertAlmostEqual(a["plan_margin"], 16)        # (2 × 10 + 3 × 20) / 5
        self.assertAlmostEqual(a["spp"], 20)                # СПП — только по заказам WB


class OzonRNPApiTests(unittest.TestCase):
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

    def test_api_marketplace_switch(self):
        from fastapi.testclient import TestClient
        m = self.m
        client = TestClient(m.app)
        with mock.patch.object(m, "_rnp_tokens", lambda: []), mock.patch.object(m, "_rnp_ozon_accounts", lambda: []), \
                mock.patch.object(m.rnp, "economics", lambda: None), mock.patch.object(m.rnp_ozon, "economics", lambda: None), \
                mock.patch.object(m, "_rnp_options", lambda mp: {"brands": [], "categories": [], "articles": []}):
            params = {"from": "2026-10-01", "to": "2026-10-14"}
            for mp in ("wb", "ozon", "all"):
                d = client.get("/api/rnp", params={**params, "mp": mp}).json()
                self.assertEqual((d["mp"], len(d["days"]), d["has_tokens"]), (mp, 14, False), mp)
            self.assertEqual(d["marketplaces"], ["wb", "ozon"])
            self.assertEqual(client.get("/api/rnp", params={**params, "mp": "ym"}).status_code, 400)
            self.assertEqual(client.post("/api/rnp/sync", params={"mp": "ozon"}).status_code, 400)
            self.assertEqual(client.get("/api/rnp/heatmap", params={**params, "mp": "all", "metric": "orders"}).status_code, 200)
            self.assertIn("by_mp", client.get("/api/rnp/status", params={"mp": "all"}).json())
            self.assertEqual(client.put("/api/rnp/plan", json={"article": "A-1", "month": "2026-10", "mp": "all"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
