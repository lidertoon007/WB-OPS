import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import httpx

from app.services import rnp as rnp_module
from app.services.rnp import RNP, RNPError
from app.storage import DraftStore

TODAY = date.today()
D = lambda n: (TODAY - timedelta(days=n)).isoformat()   # noqa: E731


def economics():
    """Похоже на Dashboard.data(): 30 дней истории (в окне юнит-экономики) по артикулу A-1."""
    days = {}
    for n in range(20, 50):
        days[D(n)] = {"A-1": {"orders_qty": 2, "orders_sum": 2000, "net_qty": 1, "revenue_sales": 1000, "commission": -300, "acquiring": -20,
                              "logistics": -100, "storage": -6, "paid": 800, "ads": -50}}
    days[D(1)] = {"A-1": {"orders_qty": 4, "orders_sum": 4000, "cancel_qty": 1, "ads": -40}}     # «кэш ОПиУ» за вчера
    return {"cabinets": {"Shop": {"days": days, "stocks": {"A-1": 30}, "prices": {"A-1": 1100}, "stocks_at": "2026-10-22 21:48", "tax_rate": 6.0}},
            "catalog": {"A-1": {"article": "a-1", "brand": "Br", "category": "Джинсы"}}}


COSTS = [{"артикул": "a-1", "кратность": "1", "сс_за_ед": "200", "подготовка_за_ед": "50", "действует_с": ""}]


class RNPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DraftStore(f"sqlite:///{Path(self.temp.name) / 'db.sqlite3'}")
        self.tokens = [("Shop", "tok-12345678")]
        self.r = RNP(self.store, lambda: self.tokens, economics, lambda: COSTS)

    def test_unit_economics(self):
        e = self.r.unit_economics(economics())
        a = e["articles"]["A-1"]
        self.assertAlmostEqual(a["buyout"], 0.5)
        self.assertAlmostEqual(a["commission"], 0.3)
        self.assertAlmostEqual(a["logistics_per_order"], 50)
        self.assertAlmostEqual(a["paid_share"], 0.8)
        self.assertEqual(a["source"], "артикул")
        self.assertEqual(e["tax_rate"], 6.0)

    def test_forecast_from_cache(self):
        t = self.r.table(D(1), D(1))
        self.assertEqual(t["sources"], {"orders": "cache", "ads": "report"})
        row = t["by_day"][D(1)]
        # 4 заказа на 4000 ₽, выкуп 50% → выручка 2000; комиссия 600, эквайринг 40, логистика 200, реклама 40,
        # себестоимость 2 × 250 = 500, налог 2000 × 0.8 × 6% = 96, хранение 6/60 × 30 дней / 60 … ≈ 3
        self.assertEqual(row["orders"], 4)
        self.assertAlmostEqual(row["revenue"], 2000)
        self.assertAlmostEqual(row["cogs"] + row["prep"], 500)
        self.assertAlmostEqual(row["tax"], 96)
        self.assertAlmostEqual(row["profit"], 2000 - 600 - 40 - 200 - 40 - 500 - 96 - row["storage"], places=2)
        art = t["articles"][0]
        self.assertEqual((art["article"], art["brand"], art["stock"], art["current_price"]), ("a-1", "Br", 30, 1100))
        self.assertAlmostEqual(art["stock_days"], 7.5)

    def test_retro_bonus_in_forecast(self):
        """Ретро-бонус бренда (% от себестоимости проданного) увеличивает прогноз прибыли."""
        base = self.r.table(D(1), D(1))["by_day"][D(1)]
        retro = [{"brand": "BR", "pct": 10.0, "since": ""}]
        r = RNP(self.store, lambda: self.tokens, economics, lambda: COSTS, lambda: retro)
        row = r.table(D(1), D(1))["by_day"][D(1)]
        self.assertAlmostEqual(row["retro"], 2 * 200 * 0.10)        # 2 проданные шт × себестоимость 200 × 10%
        self.assertAlmostEqual(row["profit"], base["profit"] + 40, places=2)
        retro[0]["since"] = (TODAY + timedelta(days=40)).isoformat()   # бонус начнёт действовать позже
        self.assertAlmostEqual(r.table(D(1), D(1))["by_day"][D(1)]["profit"], base["profit"], places=2)

    def test_sync_orders_and_ads(self):
        calls = []

        def handler(request: httpx.Request):
            calls.append(request.url.path)
            if request.url.path.endswith("/supplier/orders"):
                return httpx.Response(200, json=[
                    {"srid": "s1", "date": D(0) + "T10:00:00", "lastChangeDate": D(0) + "T10:00:00", "nmId": 11, "supplierArticle": "a-1",
                     "priceWithDisc": 1000, "spp": 20, "finishedPrice": 800, "totalPrice": 2000, "isCancel": False},
                    {"srid": "s2", "date": D(0) + "T11:00:00", "lastChangeDate": D(0) + "T12:00:00", "nmId": 11, "supplierArticle": "a-1",
                     "priceWithDisc": 1200, "spp": 30, "finishedPrice": 840, "totalPrice": 2400, "isCancel": True}])
            if request.url.path.endswith("/promotion/count"):
                return httpx.Response(200, json={"adverts": [{"status": 9, "advert_list": [{"advertId": 5, "changeTime": D(0)}]}]})
            if request.url.path.endswith("/fullstats"):
                return httpx.Response(200, json=[{"advertId": 5, "days": [{"date": D(0) + "T00:00:00Z", "apps": [{"nms": [
                    {"nmId": 11, "views": 1000, "clicks": 50, "sum": 300, "atbs": 5, "orders": 2, "shks": 2, "sum_price": 2200}]}]}]}])
            return httpx.Response(404)

        real = httpx.Client
        with mock.patch.object(rnp_module.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler))), \
                mock.patch.object(self.r.limiter, "wait", lambda *a: None):
            state = self.r.sync()
            self.assertEqual(state["status"], "done", state)
            self.r.sync()                                  # повторная загрузка не дублирует заказы
        self.assertIn("/adv/v3/fullstats", calls)
        t = self.r.table(D(0), D(0))
        row = t["by_day"][D(0)]
        self.assertEqual(t["sources"], {"orders": "api", "ads": "api"})
        self.assertEqual(row["orders"], 2)
        self.assertEqual(row["cancels"], 1)
        self.assertAlmostEqual(row["spp"], 25)
        self.assertAlmostEqual(row["price_after_spp"], 820)
        self.assertAlmostEqual(row["ctr"], 5)
        self.assertAlmostEqual(row["cpc"], 6)
        self.assertAlmostEqual(row["cpo"], 150)
        self.assertAlmostEqual(row["drr"], 300 / 2200 * 100)
        self.assertTrue(self.store.get_setting("rnp_synced_at", ""))

    def test_sync_reports_missing_scope(self):
        handler = lambda request: httpx.Response(401, json={})   # noqa: E731
        real = httpx.Client
        with mock.patch.object(rnp_module.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler))), \
                mock.patch.object(self.r.limiter, "wait", lambda *a: None):
            state = self.r.sync()
        self.assertEqual(state["status"], "partial")
        self.assertTrue(any("Статистика" in e for e in state["errors"]))
        self.assertTrue(any("Продвижение" in e for e in state["errors"]))
        self.tokens = []
        self.assertEqual(self.r.sync()["status"], "error")

    def test_plans_notes_heatmap(self):
        month = D(1)[:7]
        self.r.set_plan("a-1", month, 2, 15)
        t = self.r.table(D(1), D(1))
        self.assertAlmostEqual(t["articles"][0]["plan_done"], 200)
        self.assertAlmostEqual(t["total"]["plan_done"], 200)
        self.r.set_plan("A-1", month, None, None)
        self.assertEqual(self.r.plans(), {})
        with self.assertRaises(RNPError):
            self.r.set_plan("a-1", "2026", 1, 1)
        note = self.r.add_note(D(1), "a-1", "  подняли   цену ")
        self.assertEqual(self.r.notes(D(1), D(1))[0]["text"], "подняли цену")
        self.assertTrue(self.r.delete_note(note["id"]))
        h = self.r.heatmap(D(2), D(1), "orders")
        self.assertEqual(h["cells"][f"{D(1)}|A-1"], 4)
        with self.assertRaises(RNPError):
            self.r.heatmap(D(2), D(1), "secret")

    def test_filters(self):
        self.assertEqual(self.r.table(D(1), D(1), brand="Other")["articles"], [])
        self.assertEqual(len(self.r.table(D(1), D(1), category="Джинсы")["articles"]), 1)


if __name__ == "__main__":
    unittest.main()
