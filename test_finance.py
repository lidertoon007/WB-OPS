import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from app.services import finance


def make_report(path: Path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Свод"
    ws.append(["ОПиУ маркетплейсов — свод"])
    ws.append(["Сен 2026 — Окт 2026. Кабинеты — Wildberries: К1."])
    ws.append([])
    ws.append(["Статья", "Итого", "% выручки", "Сен 2026", "Окт 2026", "Прогноз на 31.10"])
    ws.append(["ВЫРУЧКА"])
    ws.append(["Продажи (по цене продавца)", 1200, 1.2, 700, 500, 900])
    ws.append(["Wildberries", 1200, 1.2, 700, 500, 900])
    ws.append(["Выручка за вычетом возвратов", 1000, 1, 600, 400, 800])
    ws.append(["ИТОГО: РЕЗУЛЬТАТ ПО ДАННЫМ МАРКЕТПЛЕЙСОВ", 300, 0.3, 200, 100, 250])
    ws.append(["Налог с выручки, ₽", -40, -0.04, -24, -16, -30])
    ws.append(["Налог с выручки, % от выручки", -0.04, None, -0.04, -0.04, -0.04])
    ws.append(["ПРИБЫЛЬ (после себестоимости, подготовки и налога)", 100, 0.1, 80, 20, 60])
    ws.append(["ДОЛЯ ОТ ВЫРУЧКИ"])
    ws.append(["Результат", 0.3, None, 0.33, 0.25, 0.31])
    c = wb.create_sheet("Контроль")
    c.append(["Маркетплейс", "Кабинет", "Показатель", "Значение", "Статус"])
    c.append(["Wildberries", "К1", "Итог в сводном отчёте, ₽", 300, "OK"])
    c.append(["Ozon", "—", "Загрузка", "нет данных", "НЕТ ДАННЫХ"])
    g = wb.create_sheet("Товары")
    g.append(["Товары"])
    g.append(["Артикул", "Маркетплейс", "Штук", "Выручка"])
    g.append(["A", "Все маркетплейсы", 2, 1000])
    wb.save(path)


class FinanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        (self.dir / "opiu_all.py").write_text("print('ok')", encoding="utf-8")
        make_report(self.dir / "opiu_all_2026-09_2026-10.xlsx")

    def test_project_and_latest_report(self):
        self.assertEqual(finance.find_project_dir(str(self.dir)), self.dir.resolve())
        self.assertIsNone(finance.find_project_dir(str(self.dir / "missing")))
        # По умолчанию — папка opiu внутри WB OPS, без ссылок на внешние проекты
        self.assertEqual(finance.DEFAULT_DIR_CANDIDATES, (finance.ENGINE_DIR,))
        self.assertEqual(finance.ENGINE_DIR.name, "opiu")
        (self.dir / "~$opiu_all_lock.xlsx").write_bytes(b"")
        self.assertEqual(finance.latest_report(self.dir).name, "opiu_all_2026-09_2026-10.xlsx")

    def test_parse_summary_control_goods(self):
        data = finance.read_report(self.dir / "opiu_all_2026-09_2026-10.xlsx")
        s = data["summary"]
        self.assertEqual(s["periods"], ["Сен 2026", "Окт 2026"])
        self.assertEqual(s["forecast_title"], "Прогноз на 31.10")
        kinds = {r["label"]: r["kind"] for r in s["rows"] if r["kind"] != "detail"}
        self.assertEqual(kinds["Продажи (по цене продавца)"], "article")
        self.assertEqual(kinds["Выручка за вычетом возвратов"], "total")
        self.assertEqual(kinds["Налог с выручки, % от выручки"], "share")
        self.assertEqual(kinds["Результат"], "share")
        detail = next(r for r in s["rows"] if r["kind"] == "detail")
        self.assertEqual((detail["label"], detail["parent"], detail["forecast"]), ("Wildberries", "Продажи (по цене продавца)", 900))
        self.assertEqual(s["kpi"]["profit"]["total"], 100)
        self.assertEqual(s["kpi"]["tax"]["last"], -16)
        self.assertAlmostEqual(s["kpi"]["margin"]["total"], 0.1)
        self.assertEqual([r["problem"] for r in data["control"]], [False, True])
        self.assertEqual(data["goods"]["header"], ["Артикул", "Маркетплейс", "Штук", "Выручка"])
        self.assertEqual(data["goods"]["rows"], [["A", "Все маркетплейсы", 2, 1000]])


if __name__ == "__main__":
    unittest.main()
