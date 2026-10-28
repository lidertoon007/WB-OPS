"""Фильтр «Кабинет»: ОПиУ (плашки, таблица, «Контроль», «Товары») и РНП под выбранный кабинет."""
import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from openpyxl import Workbook

from app.services import finance
from app.services.cabinets import Cabinet
from app.services.rnp import RNP
from app.storage import DraftStore

HEADER = ["Статья", "Итого", "% выручки", "Сен 2026", "Окт 2026"]


def pnl(ws, title, subtitle, revenue, profit, detail):
    """Лист ОПиУ как у opiu_all.write_pnl: под статьёй — расшифровка (detail)."""
    ws.append([title])
    ws.append([subtitle])
    ws.append([])
    ws.append(HEADER)
    ws.append(["ВЫРУЧКА"])
    ws.append(["Продажи (по цене продавца)", revenue, 1, revenue / 2, revenue / 2])
    for label in detail:
        ws.append([label, revenue / len(detail), 1, revenue / len(detail) / 2, revenue / len(detail) / 2])
    ws.append(["Выручка за вычетом возвратов", revenue, 1, revenue / 2, revenue / 2])
    ws.append(["Штрафы", -10, -0.01, -5, -5])
    ws.append(["Штрафы", -10, -0.01, -5, -5])       # исходная статья с тем же названием — расшифровка
    ws.append(["ПРИБЫЛЬ (после себестоимости, подготовки и налога)", profit, profit / revenue, profit / 2, profit / 2])


def make_report(path: Path):
    wb = Workbook()
    pnl(wb.active, "ОПиУ маркетплейсов — свод", "Сен 2026 — Окт 2026. Кабинеты — Ozon: К1, К2; Wildberries: Магазин.",
        1000, 100, ["Ozon", "Wildberries"])
    wb.active.title = "Свод"
    pnl(wb.create_sheet("Сравнение МП"), "Сравнение маркетплейсов за период", "", 1000, 100, [])
    pnl(wb.create_sheet("Ozon"), "ОПиУ Ozon", "Сен 2026 — Окт 2026. Строки — исходные статьи отчёта маркетплейса",
        700, 70, ["Продажи (цена продавца)"])
    pnl(wb.create_sheet("Ozon К1"), "ОПиУ Ozon — К1", "Сен 2026 — Окт 2026. Строки — исходные статьи отчёта маркетплейса",
        400, 40, ["Продажи (цена продавца)"])
    pnl(wb.create_sheet("Ozon К2"), "ОПиУ Ozon — К2", "Сен 2026 — Окт 2026. Строки — исходные статьи отчёта маркетплейса",
        300, 30, ["Продажи (цена продавца)"])
    pnl(wb.create_sheet("Wildberries"), "ОПиУ Wildberries — Магазин", "Сен 2026 — Окт 2026. Строки — исходные статьи отчёта маркетплейса",
        300, 30, ["Продажа"])
    c = wb.create_sheet("Контроль")
    c.append(["Маркетплейс", "Кабинет", "Показатель", "Значение", "Статус"])
    c.append(["Ozon", "К1", "Итог", 1, "OK"])
    c.append(["Ozon", "К2", "Итог", 2, "РАСХОЖДЕНИЕ"])
    c.append(["Ozon", "—", "Нет себестоимости: A", 3, "заполните cost_missing.csv"])
    c.append(["Wildberries", "Магазин", "Итог", 4, "OK"])
    g = wb.create_sheet("Товары")
    g.append(["Артикул", "Название", "Маркетплейс", "Штук", "Выручка"])
    g.append(["A", None, "Все маркетплейсы", 3, 900])
    g.append(["A", None, "Ozon", 2, 600])
    g.append(["A", None, "Wildberries", 1, 300])
    wb.save(path)


class FinanceCabinetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        (self.dir / "opiu_all.py").write_text("print('ok')", encoding="utf-8")
        self.path = self.dir / "opiu_all_2026-09_2026-10.xlsx"
        make_report(self.path)
        self.data = finance.read_report(self.path)

    def test_report_cabinets_and_views(self):
        self.assertEqual([c["key"] for c in self.data["cabinets"]], ["Ozon|К1", "Ozon|К2", "Wildberries|Магазин"])
        # У Wildberries один кабинет — его лист и площадка, и кабинет
        self.assertEqual(sorted(self.data["views"]), ["Ozon", "Ozon|К1", "Ozon|К2", "Wildberries", "Wildberries|Магазин"])
        self.assertIs(self.data["views"]["Wildberries"], self.data["views"]["Wildberries|Магазин"])

    def test_all_cabinets_is_summary(self):
        v = finance.report_view(self.data, "")
        self.assertEqual(v["selected"], "")
        self.assertNotIn("views", v)
        self.assertEqual(v["summary"]["kpi"]["profit"]["total"], 100)
        self.assertEqual(len(v["control"]), 4)
        self.assertEqual(v["goods"]["scope"], "Все маркетплейсы")
        details = [r["label"] for r in v["summary"]["rows"] if r["kind"] == "detail"]
        self.assertEqual(details, ["Ozon", "Wildberries"])

    def test_cabinet_view_recalculates_kpi_control_goods(self):
        v = finance.report_view(self.data, "Ozon|К2")
        self.assertEqual(v["selected"], "Ozon|К2")
        s = v["summary"]
        self.assertEqual(s["kpi"]["revenue"]["total"], 300)
        self.assertEqual(s["kpi"]["profit"]["total"], 30)
        self.assertAlmostEqual(s["kpi"]["margin"]["total"], 0.1)
        self.assertEqual(s["periods"], ["Сен 2026", "Окт 2026"])
        self.assertEqual(s["subtitle"], "Сен 2026 — Окт 2026. Ozon — кабинет К2.")
        rows = {(r["label"], r["kind"]) for r in s["rows"]}
        self.assertIn(("Продажи (по цене продавца)", "article"), rows)
        self.assertIn(("Продажи (цена продавца)", "detail"), rows)
        self.assertEqual([r["kind"] for r in s["rows"] if r["label"] == "Штрафы"], ["article", "detail"])
        # «Контроль»: свой кабинет и общие строки площадки («—»), чужих кабинетов нет
        self.assertEqual([(r["cabinet"], r["value"]) for r in v["control"]], [("К2", 2), ("—", 3)])
        self.assertEqual(v["goods"]["scope"], "Ozon")
        self.assertEqual(v["goods"]["rows"], [["A", None, "Ozon", 2, 600]])

    def test_marketplace_view(self):
        v = finance.report_view(self.data, "Ozon")
        self.assertEqual(v["summary"]["kpi"]["profit"]["total"], 70)
        self.assertEqual(v["summary"]["subtitle"], "Сен 2026 — Окт 2026. Ozon — все кабинеты.")
        self.assertEqual([r["cabinet"] for r in v["control"]], ["К1", "К2", "—"])

    def test_unknown_cabinet_falls_back_to_all(self):
        v = finance.report_view(self.data, "Ozon|Удалённый")
        self.assertEqual((v["selected"], v["summary"]["kpi"]["profit"]["total"]), ("", 100))

    def test_cabinet_options_take_legal_name_from_panel(self):
        (self.dir / "Ozon").mkdir()
        (self.dir / "Ozon" / "ozon_accounts.json").write_text(json.dumps({"accounts": [
            {"name": "К1", "client_id": "111", "enabled": True}, {"name": "К2", "client_id": "222", "enabled": True}]}), encoding="utf-8")
        panel = [Cabinet(id="o1", name="Озон первый", token="k", marketplace="ozon", client_id="111", legal_name="ООО Ромашка"),
                 Cabinet(id="w1", name="Магазин", token="t", legal_name="ИП Иванов")]
        opts = {o["key"]: o for o in finance.cabinet_options(self.data["cabinets"], self.dir, panel)}
        self.assertEqual((opts["Ozon|К1"]["legal_name"], opts["Ozon|К1"]["panel_id"]), ("ООО Ромашка", "o1"))
        self.assertEqual(opts["Ozon|К2"]["legal_name"], "")
        self.assertEqual(opts["Wildberries|Магазин"]["legal_name"], "ИП Иванов")      # совпадение по названию


class FinanceCabinetApiTests(unittest.TestCase):
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

    def test_report_endpoint_filters_by_cabinet(self):
        from fastapi.testclient import TestClient
        root = Path(self.temp.name) / "opiu"
        root.mkdir()
        (root / "opiu_all.py").write_text("print('ok')", encoding="utf-8")
        make_report(root / "opiu_all_2026-09_2026-10.xlsx")
        client = TestClient(self.m.app)
        with mock.patch.object(self.m, "finance_project", lambda: root):
            self.m.save_configured_cabinets([Cabinet(id="w1", name="Магазин", token="t", legal_name="ИП Иванов")])
            d = client.get("/api/finance/report").json()
            self.assertEqual((d["selected"], d["summary"]["kpi"]["profit"]["total"]), ("", 100))
            self.assertEqual([(c["key"], c["legal_name"]) for c in d["cabinets"]],
                             [("Ozon|К1", ""), ("Ozon|К2", ""), ("Wildberries|Магазин", "ИП Иванов")])
            d = client.get("/api/finance/report", params={"cabinet": "Ozon|К1"}).json()
            self.assertEqual((d["selected"], d["summary"]["kpi"]["profit"]["total"]), ("Ozon|К1", 40))
            self.assertEqual([r["cabinet"] for r in d["control"]], ["К1", "—"])


class RNPCabinetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = DraftStore(f"sqlite:///{Path(temp.name) / 'db.sqlite3'}")
        self.r = RNP(self.store, lambda: [], lambda: {"cabinets": {
            "Shop A": {"days": {}, "stocks": {"A-1": 5}, "prices": {}}, "Shop B": {"days": {}, "stocks": {"B-1": 7}, "prices": {}}},
            "catalog": {}}, lambda: [])
        self.day = (date.today() - timedelta(days=1)).isoformat()
        with self.store._connect() as c:
            c.executemany("INSERT INTO rnp_orders(srid,cabinet,day,nm_id,article,price,spp,finished,is_cancel) VALUES(?,?,?,?,?,?,?,?,0)",
                          [("1", "Shop A", self.day, 1, "A-1", 100, 0, 100), ("2", "Shop A", self.day, 1, "A-1", 100, 0, 100),
                           ("3", "Shop B", self.day, 2, "B-1", 500, 0, 500)])

    def test_table_by_cabinet(self):
        t = self.r.table(self.day, self.day)
        self.assertEqual(t["total"]["orders"], 3)
        t = self.r.table(self.day, self.day, cabinets={"Shop A"})
        self.assertEqual((t["total"]["orders"], t["total"]["orders_sum"]), (2, 200))
        self.assertEqual([(r["key"], r["stock"]) for r in t["articles"]], [("A-1", 5)])
        t = self.r.table(self.day, self.day, cabinets={"Shop B"})
        self.assertEqual([r["key"] for r in t["articles"]], ["B-1"])

    def test_heatmap_by_cabinet(self):
        h = self.r.heatmap(self.day, self.day, "orders", cabinets={"Shop B"})
        self.assertEqual([r["key"] for r in h["rows"]], ["B-1"])


if __name__ == "__main__":
    unittest.main()
