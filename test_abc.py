"""ABC-анализ (app/services/abc.py): группы по накопительной доле, статусы остатка, подсорт, сумма по маркетплейсам."""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from app.services import abc
from app.services.dashboard import DUMP_NAME, Dashboard


def wb_dump():
    rev = lambda day, art, v: [day, art, "Выручка", "Продажи (по цене продавца)", v]  # noqa: E731
    return {"months": ["2026-10-01", "2026-10-02"], "cabinets": {"Магазин": {
        "by_article": [rev("2026-10-01", "a-1", 8000.0), rev("2026-10-01", "b-2", 3000.0), rev("2026-10-02", "c-3", 1000.0),
                       ["2026-10-01", "a-1", "Вознаграждение Wildberries", "Комиссия WB", -800.0],
                       ["2026-10-01", "", "Хранение и приёмка", "Хранение", -100.0]],
        "orders": [["2026-10-01", "2026-10-01", "o1", "a-1", 20.0, 8000.0], ["2026-10-01", "2026-10-01", "o2", "b-2", 3.0, 3000.0],
                   ["2026-10-02", "2026-10-02", "o3", "c-3", 1.0, 1000.0]],
        "stocks": [["a-1", "FBS Москва", "own", 2], ["b-2", "Коледино", "mp", 30], ["d-4", "FBS Москва", "own", 7]]}}}


class AbcRulesTests(unittest.TestCase):
    def test_groups_by_cumulative_share(self):
        g = abc.groups({"a": 70, "b": 15, "c": 10, "d": 5, "e": 0, "f": -3})
        self.assertEqual(g, {"a": "A", "b": "B", "c": "B", "d": "C", "e": "C", "f": "C"})   # 70 → 85 → 95 → 100

    def test_statuses(self):
        self.assertEqual(abc.status(0, 0, 0), "Нет остатка")
        self.assertEqual(abc.status(5, 0, 999), "Критический неликвид")       # остаток без продаж
        self.assertEqual(abc.status(500, 2, 250), "Критический неликвид")     # больше 120 дней
        self.assertEqual(abc.status(200, 2, 100), "Избыток запаса")
        self.assertEqual(abc.status(0, 10, 0), "Дефицит")
        self.assertEqual(abc.status(40, 30, 10), "Дефицит")
        self.assertEqual(abc.status(40, 30, 30), "В норме")


class AbcAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for mp, cfg in (("Wildberries", "wb_accounts.json"), ("Ozon", "ozon_accounts.json")):
            (root / mp / "cache").mkdir(parents=True)
            (root / mp / cfg).write_text(json.dumps({"accounts": [{"name": "Магазин"}]}), encoding="utf-8")
        (root / "Wildberries" / "cache" / DUMP_NAME).write_text(json.dumps(wb_dump()), encoding="utf-8")
        ozon = {"months": ["2026-10-01"], "cabinets": {"Магазин": {
            "by_article": [["2026-10-01", "a-1", "Выручка", "Продажи (цена продавца)", 2000.0]],
            "orders": [["2026-10-01", "2026-10-01", "p1", "A-1", 4.0, 2000.0]],
            "stocks": [["A-1", "Свой склад (FBS)", "own", 1], ["A-1", "Склады Ozon (FBO)", "mp", 3]]}}}
        (root / "Ozon" / "cache" / DUMP_NAME).write_text(json.dumps(ozon), encoding="utf-8")
        costs = [{"артикул": "A-1", "кратность": "1", "сс_за_ед": "100", "подготовка_за_ед": "0", "действует_с": ""}]
        self.boards = {"wb": Dashboard(lambda: root, lambda: "python", lambda: costs),
                       "ozon": Dashboard(lambda: root, lambda: "python", lambda: costs, mp="ozon")}
        self.book = {"A-1": {"article": "A-1", "name": "Корм", "brand": "Zillii", "category": "Корм сухой", "cost": 100.0},
                     "B-2": {"article": "B-2", "name": "", "brand": "", "category": "", "cost": 50.0},
                     "E-5": {"article": "E-5", "name": "Не продаётся", "brand": "", "category": "", "cost": 10.0}}

    def test_analysis(self):
        d = abc.analysis(self.boards, date(2026, 10, 1), date(2026, 10, 2), self.book)
        rows = {r["key"]: r for r in d["rows"]}
        self.assertEqual(set(rows), {"A-1", "B-2", "C-3", "D-4", "E-5"})     # продажи, остатки и справочник
        a = rows["A-1"]
        self.assertEqual((a["sold"], a["revenue"], a["marketplaces"]), (24, 10000, ["Ozon", "Wildberries"]))
        self.assertEqual(a["profit"], 10000 - 800 - 2400)                   # без хранения без артикула; себест. 24 × 100
        self.assertEqual(a["stock"], 3 + 2)                                 # FBO Ozon + свой склад один раз (больший: 2)
        self.assertEqual((a["abc_revenue"], a["abc"]), ("A", "ABA"))     # 71% выручки, 86% штук, 64% прибыли
        self.assertEqual(a["per_day"], 12)
        self.assertEqual(a["status"], "Дефицит")
        self.assertEqual(a["restock"], 14 * 12 - 5)
        self.assertEqual(rows["B-2"]["abc_revenue"], "B")
        self.assertEqual(rows["C-3"]["abc_revenue"], "C")
        self.assertEqual(rows["C-3"]["restock"], 0)                         # группа C — не дозакупаем
        self.assertEqual(rows["D-4"]["status"], "Критический неликвид")     # остаток без продаж
        self.assertIsNone(rows["D-4"]["stock_cost"])                       # себестоимости нет
        self.assertEqual(rows["E-5"]["status"], "Нет остатка")
        self.assertEqual(rows["B-2"]["stock_cost"], 1500)
        self.assertEqual(d["kpi"]["revenue"], 14000)
        self.assertEqual(d["kpi"]["dead_count"], 1)
        self.assertEqual(d["kpi"]["no_cost"], 1)
        self.assertEqual(d["groups"]["A"]["count"], 1)
        self.assertEqual(d["rows"][0]["key"], "A-1")                        # по убыванию выручки
        self.assertAlmostEqual(d["rows"][-1]["cum_share"], 100)

    def test_only_one_marketplace(self):
        d = abc.analysis({"ozon": self.boards["ozon"]}, date(2026, 10, 1), date(2026, 10, 2), self.book)
        self.assertEqual(d["marketplaces"], ["Ozon"])
        self.assertEqual({r["key"]: r["revenue"] for r in d["rows"]}["A-1"], 2000)


class AbcExportApiTests(unittest.TestCase):
    def test_export_keeps_filter_and_order(self):
        import io
        from unittest import mock

        from fastapi.testclient import TestClient
        from openpyxl import load_workbook

        from app import main
        rows = [{"key": k, "article": k, "brand": "", "category": "", "name": "", "marketplaces": ["Ozon"], "sold": 1, "revenue": 1,
                 "share": 1, "cum_share": 1, "abc_revenue": "A", "abc_turnover": "A", "abc_profit": "A", "abc": "AAA", "profit": 1,
                 "stock": 0, "unit_cost": None, "stock_cost": None, "per_day": 0, "days_left": 0, "status": "Нет остатка", "restock": 0}
                for k in ("A-1", "B-2", "C-3")]
        with mock.patch.object(main, "_abc_data", return_value={"days": 30, "rows": rows}):
            r = TestClient(main.app).post("/api/abc/export", json={"from": "2026-10-01", "to": "2026-10-30", "mp": "all",
                                                                    "keys": ["C-3", "A-1", "X-9", "A-1"]})
        self.assertEqual(r.status_code, 200)
        self.assertIn("abc_2026-10-01_2026-10-30_filtered.xlsx", r.headers["content-disposition"])
        ws = load_workbook(io.BytesIO(r.content)).active
        self.assertEqual([row[2] for row in ws.iter_rows(min_row=2, values_only=True)], ["C-3", "A-1"])   # порядок экрана, без лишних


if __name__ == "__main__":
    unittest.main()
