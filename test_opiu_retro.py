"""Ретро-бонусы производителей в расчёте ОПиУ (opiu/opiu_all.py)."""
import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from app.services import finance

sys.path.insert(0, str(finance.ENGINE_DIR))
import opiu_all as o  # noqa: E402


class RetroBonusTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "retro_bonus.csv"
        self.path.write_text("бренд;ретро_бонус_%;действует_с\nDeemivis;5,00;\nDeemivis;10,00;2026-10-01\nПустой;;\n",
                             encoding="utf-8-sig")

    def test_read_and_apply(self):
        retro = o.read_retro(self.path)
        self.assertEqual(retro, {"DEEMIVIS": [("", 5.0), ("2026-10-01", 10.0)]})
        self.assertEqual(o.retro_for(retro, " deemivis", "2026-09"), 5.0)
        self.assertEqual(o.retro_for(retro, "Deemivis", "2026-10-15"), 10.0)   # разрез по дням
        costs = {"A-1": [("", 2.0, 100.0)]}                                      # кратность 2 × 100 ₽
        entities = [{"mp": "Wildberries", "cabinet": "К", "sales": [
            ["2026-09", "a-1", 3, 0.0], ["2026-10", "a-1", 1, 0.0], ["2026-10", "a-1", -1, 0.0],   # продажа и возврат
            ["2026-09", "b-2", 5, 0.0]]}]                                                           # бренда без бонуса
        catalog = {"A-1": ("Deemivis", "Джинсы"), "B-2": ("Другой", "")}
        out = o.apply_retro(entities, costs, retro, catalog, {"2026-09", "2026-10"})
        self.assertEqual(dict(out[("Wildberries", "К")]), {"2026-09": 3 * 200 * 0.05, "2026-10": 0.0})
        self.assertEqual(o.apply_retro(entities, costs, {}, catalog, {"2026-09"}), {})

    def test_pnl_line_and_unit_economics(self):
        wb = Workbook()
        months = ["2026-09"]
        table = {o.REV_SALES: {"Продажи": {"2026-09": 1000.0}}}
        o.write_pnl(wb.active, table, months, "ОПиУ", cogs={"2026-09": -400.0}, retro={"2026-09": 20.0},
                    tax={"2026-09": -60.0})
        values, shares = {}, {}
        for r in wb.active.iter_rows(values_only=True):
            if r and r[0]:
                (shares if r[0] in values else values)[r[0]] = r[1]   # второй раз строка — в разделе «Доля от выручки»
        self.assertEqual(values[o.RETRO], 20)
        self.assertAlmostEqual(shares[o.RETRO], 0.02)              # 2% выручки, со знаком «+» (доход)
        self.assertEqual(values[o.GROSS], 1000 - 400 + 20)
        self.assertEqual(values[o.PROFIT], 1000 - 400 + 20 - 60)
        unit = o.unit_values({o.REV_SALES: 1000.0, o.COGS: -400.0, o.RETRO: 20.0, o.QTY_SOLD: 2.0})
        self.assertEqual(unit["Ретро-бонус"], 20.0)
        self.assertEqual(unit["Прибыль, ₽"], 620.0)
        self.assertIn("Ретро-бонус", o.UNIT_MONEY)


if __name__ == "__main__":
    unittest.main()
