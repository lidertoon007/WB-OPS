import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from app.services.dashboard import DUMP_NAME, Dashboard

CAB = "Магазин"


def dump(days):
    """Мини-выгрузка wb_finance --grain day: 2 дня, 2 артикула."""
    return {"months": days, "cabinets": {CAB: {
        "by_article": [
            ["2026-10-01", "a-1", "Выручка", "Продажи (по цене продавца)", 10000.0],
            ["2026-10-01", "a-1", "Вознаграждение Wildberries", "Комиссия WB", -3000.0],
            ["2026-10-01", "a-1", "Логистика", "Логистика: К клиенту при продаже", -500.0],
            ["2026-10-01", "", "Реклама и продвижение", "Оказание услуг «WB Продвижение»", -1000.0],
            ["2026-10-02", "b-2", "Выручка", "Продажи (по цене продавца)", 5000.0],
            ["2026-10-02", "b-2", "Выручка", "Возвраты", -2500.0],
            ["2026-10-02", "", "Хранение и приёмка", "Хранение", -100.0],
            ["2026-09-30", "a-1", "Выручка", "Продажи (по цене продавца)", 4000.0],
        ],
        "orders": [["2026-10-01", "2026-10-01", "o1", "a-1", 2.0, 10000.0], ["2026-10-02", "2026-10-02", "o2", "b-2", 2.0, 5000.0],
                   ["2026-10-02", "2026-10-02", "o3", "b-2", -1.0, -2500.0], ["2026-09-30", "2026-09-30", "o0", "a-1", 1.0, 4000.0]],
        "tax_base_by_article": [["2026-10-01", "a-1", 8000.0], ["2026-10-02", "b-2", 2000.0]],
        "ordered": [["2026-10-01", "a-1", 5.0, 25000.0, 1.0, 5000.0], ["2026-10-02", "b-2", 3.0, 7500.0, 0.0, 0.0]],
        "catalog": [["a-1", "Бренд A", "Джинсы"], ["b-2", "Бренд B", "Брюки"]],
        "stocks": [["A-1", "Склад", "mp", 10.0], ["A-1", "В пути", "to_client", 4.0]],
        "prices": [["a-1", 9000, 5000, None]], "stocks_at": "2026-10-03 10:00", "ordered_from": "2026-09-01"}}}


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "Wildberries" / "cache").mkdir(parents=True)
        (root / "Wildberries" / "wb_accounts.json").write_text(json.dumps({"tax_percent": 6, "accounts": [{"name": CAB}]}), encoding="utf-8")
        (root / "Wildberries" / "cache" / DUMP_NAME).write_text(json.dumps(dump(["2026-09-30", "2026-10-01", "2026-10-02"])), encoding="utf-8")
        costs = [{"артикул": "a-1", "кратность": "1", "сс_за_ед": "1000,00", "подготовка_за_ед": "100,00", "действует_с": ""}]
        self.db = Dashboard(lambda: root, lambda: "python", lambda: costs)

    def test_totals_and_profit(self):
        s = self.db.summary(date(2026, 10, 1), date(2026, 10, 2))
        c = s["current"]
        self.assertEqual(c["revenue"], 12500)
        self.assertEqual((c["sold_qty"], c["return_qty"], c["net_qty"]), (4, 1, 3))
        self.assertEqual(c["ads"], -1000)
        self.assertEqual(c["cogs"], -2000)              # 2 шт a-1 × 1000; у b-2 цены нет
        self.assertEqual(c["no_cost_qty"], 1)
        self.assertAlmostEqual(c["tax"], -600)          # 6% от 10 000 оплачено покупателями
        # прибыль = 12500 - 3000 - 500 - 1000 - 100 - 2000 - 200 - 600
        self.assertAlmostEqual(c["profit"], 5100)
        self.assertEqual((c["orders_qty"], c["orders_sum"]), (8, 32500))
        self.assertEqual(c["stock_qty"], 10)            # в пути к покупателю не считаем
        self.assertEqual((c["stock_cost"], c["stock_retail"]), (10000, 50000))
        self.assertEqual(s["previous_period"], ["2026-09-29", "2026-09-30"])
        self.assertEqual(s["previous"]["revenue"], 4000)

    def test_retro_bonus(self):
        """Ретро-бонус бренда — отдельная строка дохода: себестоимость не меняется, прибыль растёт."""
        base = self.db.summary(date(2026, 10, 1), date(2026, 10, 2))["current"]
        self.assertEqual(base["retro"], 0)
        self.db.retro_rows = lambda: [{"brand": "бренд  a", "pct": 5.0, "since": ""},          # регистр и пробелы не важны
                                      {"brand": "Бренд A", "pct": 10.0, "since": "2026-11-01"}]  # с ноября — 10%
        c = self.db.summary(date(2026, 10, 1), date(2026, 10, 2))["current"]
        self.assertEqual(c["cogs"], base["cogs"])
        self.assertAlmostEqual(c["retro"], 2 * 1000 * 0.05)              # 2 шт a-1 × 1000 × 5%; у b-2 нет себестоимости
        self.assertAlmostEqual(c["profit"], base["profit"] + 100)
        self.assertAlmostEqual(c["retro_pct"], 100 / 12500 * 100)

    def test_ads_from_cabinet_spend(self):
        """Реклама — по затратам рекламного кабинета; «к перечислению» — по удержаниям WB."""
        base = self.db.summary(date(2026, 10, 1), date(2026, 10, 2))["current"]
        self.assertEqual((base["ads"], base["ads_source"]), (-1000, "withheld"))   # истории затрат нет — удержания
        calls = []

        def spend(start, end, cabinet):
            calls.append((start, end, cabinet))
            return {"by_day": {"2026-10-01": 600.0, "2026-10-02": 200.0},
                    "by_article": {("2026-10-01", "A-1"): 500.0, ("2026-10-02", "B-2"): 250.0}}
        self.db.ad_spend = spend
        c = self.db.summary(date(2026, 10, 1), date(2026, 10, 2))["current"]
        self.assertEqual((c["ads"], c["ads_source"], c["ads_withheld"]), (-800, "spend", -1000))
        self.assertEqual(c["net_to_pay"], base["net_to_pay"])              # выплата не меняется
        self.assertAlmostEqual(c["profit"], base["profit"] + 200)          # затрат меньше удержания на 200
        self.assertAlmostEqual(c["drr"], 800 / 12500 * 100)
        self.assertEqual(calls[0], ("2026-10-01", "2026-10-02", None))
        c = self.db.summary(date(2026, 10, 1), date(2026, 10, 2), brand="Бренд A")["current"]
        self.assertEqual(c["ads"], -500)                                   # с фильтром — по артикулам кампаний

    def test_buyout_from_wb_funnel(self):
        """Выкуп — как в воронке WB: выкуплено / (выкуплено + отменено), а не продано / заказано (уходило за 100%)."""
        c = self.db.summary(date(2026, 10, 1), date(2026, 10, 2))["current"]
        self.assertIsNone(c["buyout"])                   # у кабинета нет ключа — воронку не запрашиваем
        accounts = self.db.wb_dir() / "wb_accounts.json"
        accounts.write_text(json.dumps({"accounts": [{"name": CAB, "token": "T"}]}), encoding="utf-8")
        calls = []

        def fake(token, start, end):
            calls.append((token, start, end))
            return {"A-1": (326.0, 14.0), "B-2": (4.0, 6.0)}
        self.db.fetch_funnel = fake
        s = self.db.summary(date(2026, 10, 1), date(2026, 10, 2))
        self.assertAlmostEqual(s["current"]["buyout"], 330 / 350 * 100)
        self.assertEqual(calls, [("T", date(2026, 10, 1), date(2026, 10, 2)), ("T", date(2026, 9, 29), date(2026, 9, 30))])
        c = self.db.summary(date(2026, 10, 1), date(2026, 10, 2), brand="Бренд A")["current"]
        self.assertAlmostEqual(c["buyout"], 326 / 340 * 100)
        self.assertEqual(len(calls), 2)                  # повторно — из кэша

        def offline(token, start, end):
            raise RuntimeError("429")
        self.db.fetch_funnel = offline
        self.assertIsNone(self.db.summary(date(2026, 10, 3), date(2026, 10, 3))["current"]["buyout"])

    def test_filters(self):
        c = self.db.summary(date(2026, 10, 1), date(2026, 10, 2), brand="Бренд A")["current"]
        self.assertEqual(c["revenue"], 10000)
        self.assertEqual(c["ads"], 0)                    # расходы без артикула при фильтре не учитываются
        c = self.db.summary(date(2026, 10, 1), date(2026, 10, 2), articles={"B-2"})["current"]
        self.assertEqual(c["revenue"], 2500)
        prev = self.db.summary(date(2026, 10, 1), date(2026, 10, 2), previous=(date(2026, 9, 1), date(2026, 9, 30)))
        self.assertEqual(prev["previous_period"], ["2026-09-01", "2026-09-30"])

    def test_options(self):
        o = self.db.options()
        self.assertEqual(o["brands"], ["Бренд A", "Бренд B"])
        self.assertEqual(o["last_day"], "2026-10-02")


class OzonDashboardTests(unittest.TestCase):
    """Выгрузка ozon_finance --grain day: свои статьи, бренд и категория — из cost.csv, выкуп — по заказам без отмен."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "Ozon" / "cache" / "123").mkdir(parents=True)
        (root / "Ozon" / "ozon_accounts.json").write_text(json.dumps({"tax_percent": 2, "accounts": [{"name": CAB}]}), encoding="utf-8")
        data = {"months": ["2026-10-01"], "cabinets": {CAB: {
            "by_article": [["2026-10-01", "a-1", "Выручка", "Продажи (цена продавца)", 10000.0],
                           ["2026-10-01", "a-1", "Комиссия Ozon", "Комиссия за продажу", -1500.0],
                           ["2026-10-01", "a-1", "Логистика и доставка", "Логистика", -800.0],
                           ["2026-10-01", "a-1", "Обработка возвратов и отмен", "Обратная логистика", -200.0],
                           ["2026-10-01", "a-1", "Реклама и продвижение", "Оплата за клик", -600.0],
                           ["2026-10-01", "", "Подписки и прочие услуги Ozon", "Подписка Premium Pro (процент)", -300.0],
                           ["2026-10-01", "", "Штрафы и нарушения", "Превышение индекса ошибок", -100.0],
                           ["2026-10-01", "", "Компенсации и прочие начисления", "Корректировка стоимости услуг", 50.0]],
            "orders": [["2026-10-01", "2026-10-01", "p1", "a-1", 2.0, 10000.0]],
            "tax_base_by_article": [["2026-10-01", "a-1", 9000.0]],
            "ordered": [["2026-10-01", "a-1", 4.0, 20000.0, 1.0, 5000.0]],
            "stocks": [["a-1", "Свой склад (FBS)", "own", 5]], "prices": [["a-1", 5000.0, 5000.0, 5000.0]]}}}
        (root / "Ozon" / "cache" / DUMP_NAME).write_text(json.dumps(data), encoding="utf-8")
        (root / "Ozon" / "cache" / "123" / "orders.json").write_text("{}", encoding="utf-8")
        costs = [{"артикул": "A-1", "кратность": "1", "сс_за_ед": "1000,00", "подготовка_за_ед": "60,00", "действует_с": "",
                  "бренд": "Zillii", "категория": "Корм"}]
        spend = lambda *a: {"by_day": {"2026-10-01": 99999}, "by_article": {}}  # noqa: E731 — реклама WB к Ozon не относится
        self.db = Dashboard(lambda: root, lambda: "python", lambda: costs, ad_spend=spend, mp="ozon")
        self.root = root

    def test_ozon_metrics(self):
        s = self.db.summary(date(2026, 10, 1), date(2026, 10, 1))
        c = s["current"]
        self.assertEqual(s["mp"], "ozon")
        self.assertEqual((c["revenue"], c["commission"], c["logistics"], c["ads"]), (10000, -1500, -1000, -900))   # + Premium Pro (маркетинг)
        self.assertEqual((c["other"], c["penalty"], c["compensation"], c["marketing"]), (0, -100, 50, -300))
        self.assertEqual(c["net_to_pay"], 10000 - 1500 - 1000 - 600 - 300 - 100 + 50)
        self.assertEqual((c["cogs"], c["prep"]), (-2000, -120))
        self.assertAlmostEqual(c["tax"], -180)                       # 2% от 9000
        self.assertAlmostEqual(c["profit"], 6550 - 2000 - 120 - 180)
        self.assertEqual(c["buyout"], 75)                            # 4 заказано, 1 отменён
        self.assertEqual(c["stock_qty"], 5)
        o = s["options"]
        self.assertEqual((o["brands"], o["categories"]), (["Zillii"], ["Корм"]))   # Ozon их не даёт — из cost.csv

    def test_ozon_cache_in_cabinet_folders_marks_stale(self):
        import os, time
        later = time.time() + 60
        os.utime(self.root / "Ozon" / "cache" / "123" / "orders.json", (later, later))
        self.assertTrue(self.db.stale())


if __name__ == "__main__":
    unittest.main()
