"""Главная: сводка по магазинам (app/services/home.py) — факт, маркетинг Ozon отдельно от рекламы, прогноз."""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from app.services import home
from app.services.dashboard import DUMP_NAME, Dashboard

OZON_DUMP = {"months": ["2026-10-01", "2026-10-02"], "cabinets": {"Еврозоо": {
    "by_article": [["2026-10-01", "a-1", "Выручка", "Продажи (цена продавца)", 10000.0],
                   ["2026-10-01", "a-1", "Реклама и продвижение", "Продвижение товара", -500.0],
                   ["2026-10-01", "", "Реклама и продвижение", "Продвижение бренда", -300.0],
                   ["2026-10-01", "", "Реклама и продвижение", "Ускоренный сбор отзывов", -100.0],
                   ["2026-10-01", "", "Реклама и продвижение", "Бейдж «Бренд проверен»", -50.0],
                   ["2026-10-01", "", "Подписки и прочие услуги Ozon", "Подписка Premium Pro (процент)", -50.0],
                   ["2026-10-01", "", "Подписки и прочие услуги Ozon", "Аналитика Pro", -40.0],
                   ["2026-10-02", "a-1", "Выручка", "Продажи (цена продавца)", 10000.0]],
    "orders": [["2026-10-01", "2026-10-01", "p1", "a-1", 2.0, 10000.0], ["2026-10-02", "2026-10-02", "p2", "a-1", 2.0, 10000.0]],
    "ordered": [["2026-10-01", "a-1", 4.0, 20000.0, 0.0, 0.0], ["2026-10-02", "a-1", 4.0, 20000.0, 1.0, 5000.0]],
    "stocks": [["a-1", "Склады Ozon (FBO)", "mp", 3]], "prices": []}}}


class HomeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "Ozon" / "cache").mkdir(parents=True)
        (root / "Ozon" / "ozon_accounts.json").write_text(json.dumps({"accounts": [{"name": "Еврозоо"}]}), encoding="utf-8")
        (root / "Ozon" / "cache" / DUMP_NAME).write_text(json.dumps(OZON_DUMP), encoding="utf-8")
        costs = [{"артикул": "A-1", "кратность": "1", "сс_за_ед": "1000", "подготовка_за_ед": "0", "действует_с": ""}]
        self.boards = {"wb": Dashboard(lambda: root, lambda: "python", lambda: costs),            # WB не подключён
                       "ozon": Dashboard(lambda: root, lambda: "python", lambda: costs, mp="ozon")}
        self.legal = {("Ozon", "Еврозоо"): 'ООО "ЕВРОЗОО"'}

    def test_rows_marketing_and_forecast(self):
        d = home.summary(self.boards, date(2026, 10, 1), date(2026, 10, 31), self.legal)
        self.assertEqual(d["missing"], ["Wildberries"])
        self.assertEqual(len(d["rows"]), 1)
        r = d["rows"][0]
        self.assertEqual((r["shop"], r["mp"], r["cabinet"]), ('ООО "ЕВРОЗОО"', "ozon", "Еврозоо"))
        self.assertEqual((r["orders_qty"], r["orders_sum"], r["net_qty"], r["revenue"]), (8, 40000, 4, 20000))
        self.assertEqual(r["ads_only"], -500)                          # продвижение товара — реклама
        self.assertEqual(r["marketing"], -500)                         # бренд + отзывы + «Бренд проверен» + Premium Pro
        self.assertEqual(r["total_marketing"], -1000)
        self.assertEqual((r["drr"], r["marketing_pct"], r["total_marketing_pct"]), (2.5, 2.5, 5.0))
        self.assertEqual(r["profit"], 20000 - 1000 - 40 - 4000)        # Аналитика Pro — прочие, себестоимость 4 × 1000
        self.assertEqual(r["net_to_pay"], 20000 - 1040)
        self.assertEqual(r["buyout"], 87.5)                            # 8 заказано, 1 отменён
        self.assertEqual(r["stock_cost"], 3000)
        self.assertEqual((r["days_passed"], r["days_total"], r["fact_to"]), (2, 31, "2026-10-02"))
        f = r["forecast"]
        self.assertAlmostEqual(f["orders_qty"], 8 * 31 / 2)
        self.assertAlmostEqual(f["profit"], r["profit"] * 31 / 2)
        self.assertAlmostEqual(f["drr"], 2.5)                          # доли от выручки прогноз не меняет
        self.assertEqual(d["total"]["revenue"], 20000)
        self.assertAlmostEqual(d["total"]["forecast"]["revenue"], 20000 * 31 / 2)

    def test_closed_period_forecast_equals_fact(self):
        r = home.summary(self.boards, date(2026, 10, 1), date(2026, 10, 2), self.legal)["rows"][0]
        self.assertEqual(r["forecast"]["revenue"], r["revenue"])

    def test_own_warehouse_counted_once_and_wb_first(self):
        root = Path(self.temp.name)
        (root / "Wildberries" / "cache").mkdir(parents=True)
        (root / "Wildberries" / "wb_accounts.json").write_text(json.dumps({"accounts": [{"name": "Еврозоо"}]}), encoding="utf-8")
        wb = {"months": ["2026-10-01"], "cabinets": {"Еврозоо": {
            "by_article": [["2026-10-01", "a-1", "Выручка", "Продажи (по цене продавца)", 1000.0]],
            "stocks": [["a-1", "FBS Москва", "own", 10], ["a-1", "Коледино", "mp", 2]]}}}
        (root / "Wildberries" / "cache" / DUMP_NAME).write_text(json.dumps(wb), encoding="utf-8")
        ozon = json.loads(json.dumps(OZON_DUMP))
        ozon["cabinets"]["Еврозоо"]["stocks"] = [["a-1", "Свой склад (FBS)", "own", 8], ["a-1", "Склады Ozon (FBO)", "mp", 3]]
        (root / "Ozon" / "cache" / DUMP_NAME).write_text(json.dumps(ozon), encoding="utf-8")
        legal = {**self.legal, ("Wildberries", "Еврозоо"): 'ООО "ЕВРОЗОО"'}
        d = home.summary(self.boards, date(2026, 10, 1), date(2026, 10, 31), legal)
        self.assertEqual([r["mp"] for r in d["rows"]], ["wb", "ozon"])
        self.assertEqual([r["stock_cost"] for r in d["rows"]], [12000, 11000])     # в строке — всё, что видит МП
        self.assertEqual(d["total"]["stock_cost"], (2 + 3 + 10) * 1000)              # свой склад — один раз, больший остаток

    def test_dashboard_ads_still_include_marketing(self):
        c = self.boards["ozon"].summary(date(2026, 10, 1), date(2026, 10, 2))["current"]
        self.assertEqual(c["ads"], -1000)                              # на дашборде Ozon реклама — вся, как раньше
        self.assertEqual((c["ads_only"], c["marketing"]), (-500, -500))


if __name__ == "__main__":
    unittest.main()
