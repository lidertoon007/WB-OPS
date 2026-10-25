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


if __name__ == "__main__":
    unittest.main()
