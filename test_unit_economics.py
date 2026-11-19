import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import chdir
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

from app.services import finance, unit_economics
from opiu import opiu_all as engine


def make_report(path):
    book = Workbook()
    sheet = book.active
    sheet.title = "Юнит-экономика"
    engine.write_unit_sheet(sheet, [
        (("Ozon", "К1", "A-01", "Кошачий корм", "Бренд & Ко", "Корм"),
         {engine.QTY_SOLD: 2, engine.REV_SALES: 100, engine.COMMISSION: -20, engine.COGS: -20,
          engine.F_SHOWS: 100, engine.F_CLICKS: 120, engine.F_CLICKS_CTR: 20,
          engine.PR_BASE_S: 200, engine.PR_BASE_N: 1, engine.PR_DISC_S: 160}),
        (("Wildberries", "К1", "B-002", "Корм для собак", "Бренд & Ко", "Корм"),
         {engine.QTY_SOLD: 8, engine.REV_SALES: 900, engine.COMMISSION: -180, engine.COGS: -120,
          engine.F_SHOWS: 300, engine.F_CLICKS: 140, engine.F_CLICKS_CTR: 40,
          engine.PR_BASE_S: 600, engine.PR_BASE_N: 2, engine.PR_DISC_S: 520}),
        (("Ozon", "К2", "A-03", "Игрушка", "Другой", "Игрушки"),
         {engine.QTY_SOLD: 1, engine.REV_SALES: 500}),
    ], "Юнит-экономика", "Окт 2026")
    sheet.cell(4, 11, "=2+2")  # Excel has no cached formula for this cell.
    index = 1 + sheet.max_column
    sheet.cell(3, index, "Новая служебная колонка")
    sheet.cell(4, index, 42)
    sheet.column_dimensions[sheet.cell(3, index).column_letter].hidden = True
    book.save(path)


def make_period_report(path, *, unassigned_quantity=0):
    book = Workbook()
    sheet = book.active
    sheet.title = "Юнит-экономика"
    a = ("Ozon", "К1", "A-01", "Кошачий корм", "Бренд", "Корм")
    b = ("Wildberries", "К2", "B-02", "Собачий корм", "Бренд", "Корм")
    unassigned = ("Ozon", "К1", engine.NO_ARTICLE, "", "", "")
    snapshot = ("Ozon", "К1", "S-03", "Только остатки", "Снимок", "Игрушки")
    engine.write_unit_sheet(sheet, [
        (a, {engine.REV_SALES: 400, engine.PR_PRICE_S: 220, engine.PR_PRICE_N: 1, engine.ST_TOTAL: 90}),
        (b, {engine.REV_SALES: 900}),
        (unassigned, {engine.ADS: -10, engine.QTY_SOLD: unassigned_quantity}),
        (snapshot, {engine.ST_TOTAL: 3}),
    ], "Юнит-экономика", "Сен — Ноя 2026")
    registry = book.create_sheet("Реестр")
    registry.append(engine.REGISTRY_HEADER)

    def record(item, period, values):
        for article, value in values.items():
            is_quantity = article in (engine.QTY_SOLD, engine.QTY_RETURNED)
            registry.append([*item[:2], period, item[2], item[3], "", article, "", "",
                             0 if is_quantity else value, value if is_quantity else None])

    record(a, "2026-09", {engine.REV_SALES: 100, engine.REV_RETURNS: -10, engine.QTY_SOLD: 2,
                           engine.QTY_RETURNED: -1, engine.COMMISSION: -20, engine.COGS: -20})
    record(a, "2026-10", {engine.REV_SALES: 300, engine.REV_RETURNS: -20, engine.QTY_SOLD: 3,
                           engine.QTY_RETURNED: -1, engine.COMMISSION: -50, engine.COGS: -90,
                           engine.PREP: -5, engine.RETRO: 10, engine.TAX: -20, engine.COMP: 15, engine.ADVANCE: -5})
    record(b, "2026-09", {engine.REV_SALES: 900, engine.QTY_SOLD: 8, engine.COMMISSION: -180, engine.COGS: -120})
    record(unassigned, "2026-09", {engine.ADS: -10})
    record(unassigned, "2026-10", {engine.ADS: 0, engine.QTY_SOLD: unassigned_quantity})
    orders = book.create_sheet("Заказы")
    orders.append(engine.ORDERS_HEADER)
    orders.append([*a[:2], "2026-09", *a[2:4], 40, 4000, 10, 1000, 30, 3000])
    orders.append([*a[:2], "2026-10", *a[2:4], 20, 2000, 5, 500, 15, 1500])
    orders.append([*b[:2], "2026-09", *b[2:4], 10, 2000, 2, 400, 8, 1600])
    engine.write_funnel_sheet(book.create_sheet("Воронка"), [
        [*a[:2], "2026-09", *a[2:4], 100, 20, 5, 8, 800, 6, 1, 1],
        [*a[:2], "2026-10", *a[2:4], 400, 120, 10, 12, 1200, 8, 2, 1],
        [*b[:2], "2026-09", *b[2:4], None, 400, None, 20, 4000, 2, 1, 0],
    ], "Сен — Ноя 2026")
    book.save(path)
    stamp = datetime(2026, 12, 1, 12).timestamp()
    os.utime(path, (stamp, stamp))


class UnitEconomicsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name)
        self.path = self.project / "unit_economics_2026-10.xlsx"

    def test_weighted_totals_cover_all_matching_rows_and_hide_helpers(self):
        make_report(self.path)
        data = unit_economics.report_view(self.path, brand="Бренд & Ко", category="Корм", q="КоРм", page=2, page_size=1)
        keys = [c["key"] for c in data["columns"]]
        totals = dict(zip(keys, data["totals"]))
        row = dict(zip(keys, data["rows"][0]))
        self.assertEqual((data["page"], data["total_rows"], data["period"]), (2, 2, "2026-10"))
        self.assertEqual(row["Артикул"], "B-002")
        self.assertEqual(set(keys), {key for key, _, _ in engine.UNIT_HEADER} - engine.UNIT_HIDDEN)
        self.assertAlmostEqual(totals["Выручка нетто, ₽"], 1000)
        self.assertAlmostEqual(totals["Маржа, % выручки"], 0.66)
        self.assertAlmostEqual(totals["Расходы МП, % выручки"], 0.20)
        self.assertAlmostEqual(totals["Прибыль на 1 шт"], 66)
        self.assertAlmostEqual(totals["CTR, %"], 0.15)
        self.assertAlmostEqual(totals["Цена до скидки, ₽"], 800 / 3)
        self.assertAlmostEqual(totals["Скидка, %"], 0.15)
        self.assertAlmostEqual(totals["Продажи, ₽"], 900)
        self.assertIsNone(totals["Кратность на МП, шт"])
        self.assertIsNone(totals["Запас, дней"])
        first = unit_economics.report_view(self.path, cabinet="Ozon|К1", q="a-01", page=99)
        self.assertEqual((first["page"], first["total_rows"]), (1, 1))
        self.assertIsNone(first["rows"][0][keys.index("Продажи, ₽")])
        self.assertEqual([c["label"] for c in first["filters"]["cabinet"]], ["К1 · Ozon", "К1 · Wildberries", "К2 · Ozon"])
        json.dumps(data, allow_nan=False)

    def test_latest_chooses_only_regular_workbooks_and_supports_weekly(self):
        self.assertIsNone(unit_economics.latest_report(self.project))
        make_report(self.path)
        weekly = self.project / "unit_economics_weekly_2026-09_2026-10.xlsx"
        book = Workbook()
        sheet = book.active
        sheet.title = "Юнит по неделям"
        engine.write_unit_sheet(sheet, [
            ((2026, "Октябрь", 40, "1–4.10", "Ozon", "К1", 12, "Товар", "", ""), {engine.REV_SALES: 70}),
        ], "Юнит по неделям", "Сен 2026 — Окт 2026", header=engine.UNIT_WEEK_HEADER, keys=engine.UNIT_WEEK_KEYS)
        book.save(weekly)
        new = time.time() + 100
        os.utime(weekly, (new, new))
        (self.project / "unit_economics_readme.xlsx").touch()
        (self.project / "unit_economics_2026-11.xlsx").mkdir()
        try:
            (self.project / "unit_economics_2026-12.xlsx").symlink_to(weekly)
        except OSError:  # Some Windows accounts cannot create symlinks.
            pass
        self.assertEqual(unit_economics.latest_report(self.project), weekly)
        data = unit_economics.report_view(weekly, date_from="2026-10")
        self.assertEqual(data["period"], "2026-09 — 2026-10")
        self.assertEqual(data["total_rows"], 1)
        self.assertFalse(data["period_filter"])
        self.assertEqual(data["available_periods"], [])
        self.assertIsNone(data["active_period"]["date_from"])
        self.assertNotIn("prices", {column["group"] for column in data["columns"]})
        self.assertNotIn("Показы", {column["key"] for column in data["columns"]})

    def test_read_handles_missing_columns_and_invalid_workbook(self):
        book = Workbook()
        sheet = book.active
        sheet.title = "Юнит-экономика"
        sheet.append(["Период"])
        sheet.append(["Итого"])
        sheet.append(["Маркетплейс", "Кабинет", "Артикул", "CTR, %"])
        sheet.append(["Ozon", "К1", "00123", 0.25])
        book.save(self.path)
        data = unit_economics.report_view(self.path, marketplace="Ozon")
        self.assertEqual(data["rows"], [["Ozon", "К1", "00123", 0.25]])
        self.assertEqual(data["filters"]["brand"], [])
        self.assertIsNone(data["totals"][-1])
        self.path.write_bytes(b"not an Excel file")
        with self.assertRaisesRegex(finance.FinanceError, "Не удалось прочитать Excel"):
            unit_economics.report_view(self.path)

    def test_month_filter_recomputes_finances_orders_and_snapshots(self):
        path = self.project / "unit_economics_2026-09_2026-11.xlsx"
        make_period_report(path)
        data = unit_economics.report_view(path, date_from="2026-10", date_to="2026-10")
        self.assertEqual(data["available_periods"], ["2026-09", "2026-10", "2026-11"])
        self.assertEqual(data["active_period"], {"date_from": "2026-10", "date_to": "2026-10"})
        self.assertTrue(data["period_filtered"])
        self.assertEqual(data["total_rows"], 1)  # A-01 has October activity; snapshots and zero amounts do not qualify.
        keys = [col["key"] for col in data["columns"]]
        row = dict(zip(keys, data["rows"][0]))
        self.assertEqual((row["Артикул"], row["Бренд"], row["Категория"]), ("A-01", "Бренд", "Корм"))
        self.assertEqual((row["Продано, шт"], row["Возвращено, шт"], row["Нетто, шт"]), (3, 1, 2))
        self.assertEqual((row["Выручка нетто, ₽"], row["К перечислению от МП, ₽"], row["Прибыль, ₽"]), (280, 240, 135))
        self.assertEqual(row["Прибыль на 1 шт"], 67.5)
        self.assertAlmostEqual(row["Маржа, % выручки"], 135 / 280)
        self.assertEqual((row["Заказано, шт"], row["Заказано, ₽"], row["Отменено, шт"]), (20, 2000, 5))
        self.assertAlmostEqual(row["Заказов в день, шт"], 15 / 31)
        self.assertEqual((row["Текущая цена, ₽"], row["Остаток всего, шт"], row["Запас, дней"]), (220, 90, 186))
        self.assertIn("Текущая цена, ₽", {col["key"] for col in data["columns"] if col["snapshot"]})
        self.assertEqual(unit_economics.report_view(path)["total_rows"], 4)
        self.assertEqual(unit_economics.report_view(path, date_from="2026-11")["total_rows"], 0)

    def test_effective_full_range_matches_the_workbook_summary(self):
        path = self.project / "unit_economics_2026-09_2026-11.xlsx"
        make_period_report(path)
        baseline = unit_economics.report_view(path)
        for date_from, date_to in (("2026-09", "2026-11"), ("2026-01", "2026-12"),
                                   ("2026-09", None), (None, "2026-12")):
            with self.subTest(date_from=date_from, date_to=date_to):
                data = unit_economics.report_view(path, date_from=date_from, date_to=date_to)
                self.assertEqual(data["active_period"], {"date_from": date_from, "date_to": date_to})
                self.assertFalse(data["period_filtered"])
                for key in ("columns", "rows", "totals", "total_rows", "filters"):
                    self.assertEqual(data[key], baseline[key])

    def test_unassigned_nonzero_quantity_qualifies_when_money_is_zero(self):
        path = self.project / "unit_economics_2026-09_2026-11.xlsx"
        make_period_report(path, unassigned_quantity=2)
        data = unit_economics.report_view(path, q="Без артикула", date_from="2026-10", date_to="2026-10")
        self.assertEqual(data["total_rows"], 1)
        totals = dict(zip((col["key"] for col in data["columns"]), data["totals"]))
        self.assertEqual((totals["Продано, шт"], totals["Нетто, шт"], totals["Прибыль, ₽"]), (2, 2, 0))

    def test_range_and_page_recompute_weighted_totals_across_selected_items(self):
        path = self.project / "unit_economics_2026-09_2026-11.xlsx"
        make_period_report(path)
        september = unit_economics.report_view(path, date_to="2026-09", brand="Бренд", page=2, page_size=1)
        keys = [col["key"] for col in september["columns"]]
        self.assertEqual((september["total_rows"], len(september["rows"]), september["page"]), (2, 1, 2))
        totals = dict(zip(keys, september["totals"]))
        self.assertEqual(totals["Выручка нетто, ₽"], 990)
        self.assertAlmostEqual(totals["Маржа, % выручки"], 650 / 990)
        self.assertAlmostEqual(totals["Прибыль на 1 шт"], 650 / 9)
        self.assertAlmostEqual(totals["CTR, %"], 0.2)
        self.assertAlmostEqual(totals["Карточка → корзина, %"], 0.25)
        self.assertAlmostEqual(totals["Карточка → заказ, %"], 28 / 420)
        self.assertAlmostEqual(totals["Выкуп по завершённым, %"], 8 / 11)

        selected = unit_economics.report_view(path, marketplace="Ozon", cabinet="Ozon|К1", brand="Бренд",
                                             category="Корм", q="КОШАЧИЙ", date_from="2026-09", date_to="2026-10")
        totals = dict(zip(keys, selected["totals"]))
        self.assertEqual(selected["total_rows"], 1)
        self.assertEqual(totals["Выручка нетто, ₽"], 370)
        self.assertAlmostEqual(totals["Прибыль на 1 шт"], 185 / 3)
        self.assertAlmostEqual(totals["CTR, %"], 140 / 500)
        self.assertAlmostEqual(totals["Отмены, % заказов"], 15 / 60)
        self.assertAlmostEqual(totals["Заказов в день, шт"], 45 / 61)
        unassigned = unit_economics.report_view(path, date_to="2026-09", q="Без артикула")
        self.assertEqual(unassigned["total_rows"], 1)
        self.assertEqual(unassigned["totals"][keys.index("Прибыль, ₽")], -10)

    def test_api_validates_month_format_and_order(self):
        from fastapi.testclient import TestClient

        with chdir(self.project):
            from app import main

            with patch.object(main, "finance_project", return_value=self.project):
                client = TestClient(main.app)
                endpoint = "/api/unit-economics/report"
                for query in ({"date_from": "2026-9"}, {"date_to": "2026-00"}, {"date_to": "2026-13"},
                              {"date_to": "2026-10-01"}, {"date_from": "0000-01"},
                              {"date_from": "2026-10", "date_to": "2026-09"}):
                    with self.subTest(query=query):
                        self.assertEqual(client.get(endpoint, params=query).status_code, 422)
                self.assertEqual(client.get(endpoint).json()["available_periods"], [])
                make_period_report(self.project / "unit_economics_2026-09_2026-11.xlsx")
                response = client.get(endpoint, params={"date_from": "2026-10", "date_to": "2026-10"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["total_rows"], 1)

    def wait_for_worker(self, runner):
        runner.process.wait(timeout=10)
        for _ in range(100):
            state = runner.status()
            if state["status"] != "running":
                return state
            time.sleep(0.01)
        self.fail("unit economics worker did not finish")

    def test_worker_only_publishes_unit_workbook_with_strict_cache_flags(self):
        pnl = self.project / "opiu_all_2026-10_2026-10.xlsx"
        pnl.write_bytes(b"original pnl")
        cost = self.project / "cost.csv"
        cost.write_bytes(b"original cost")
        missing = self.project / "cost_missing.csv"
        missing.write_bytes(b"original cost_missing")
        (self.project / "opiu_all.py").write_text(
            "import sys\nfrom pathlib import Path\n"
            "assert sys.argv[1:] == ['--only-unit', '--from-cache']\n"
            f"Path({self.path.name!r}).write_bytes(b'new unit')\n"
            f"Path({pnl.name!r}).write_bytes(b'changed pnl')\n"
            "Path('cost.csv').write_bytes(b'changed cost')\n"
            "Path('cost_missing.csv').unlink()\n"
            "Path('Ozon/cache').mkdir(parents=True)\n"
            "Path('Ozon/cache/new.json').touch()\n",
            encoding="utf-8",
        )
        runner = finance.ReportRunner(self.project / "status.json")
        self.addCleanup(runner.stop)
        runner.start(self.project, sys.executable, "unit")
        state = self.wait_for_worker(runner)
        self.assertEqual((state["mode"], state["status"]), ("unit", "done"), state)
        self.assertEqual(self.path.read_bytes(), b"new unit")
        self.assertEqual(pnl.read_bytes(), b"original pnl")
        self.assertEqual(cost.read_bytes(), b"original cost")
        self.assertEqual(missing.read_bytes(), b"original cost_missing")
        self.assertFalse((self.project / "Ozon/cache/new.json").exists())

    def test_unit_progress_and_outcomes_preserve_previous_pnl_status_across_workers(self):
        status_file = self.project / "finance_status.json"
        previous = finance.ReportRunner(status_file)
        previous.state.update(status="done", mode="full", code=0,
                              cabinets={"К1": {"status": "complete", "rows": 15}})
        previous.log.append("ОПиУ собран")
        previous._persist()
        last_pnl = previous.status()

        release = self.project / "release"
        script = self.project / "opiu_all.py"
        script.write_text(
            "import time\nfrom pathlib import Path\n"
            f"while not Path({str(release)!r}).exists():\n    time.sleep(0.02)\n"
            f"Path({self.path.name!r}).write_bytes(b'new unit')\n", encoding="utf-8",
        )
        runner = finance.ReportRunner(status_file)
        observer = finance.ReportRunner(status_file)
        self.addCleanup(runner.stop)
        runner.start(self.project, sys.executable, "unit")
        try:
            self.assertEqual(observer.status()["mode"], "unit")
            self.assertEqual(observer.status()["status"], "running")
            self.assertEqual(observer.pnl_status(), last_pnl)
            busy = observer.pnl_status(show_busy=True)
            self.assertEqual((busy["status"], busy["mode"], busy["log"]), ("running", None, []))
            self.assertIn("Собирается юнит-экономика", busy["message"])
            with self.assertRaisesRegex(finance.FinanceError, "уже собирается"):
                observer.start(self.project, sys.executable, "full")
        finally:
            release.touch()
        self.assertEqual(self.wait_for_worker(runner)["status"], "done")
        restarted = finance.ReportRunner(status_file)
        self.assertEqual(restarted.status()["status"], "done")
        self.assertEqual(restarted.pnl_status(show_busy=True), last_pnl)

        script.write_text("raise SystemExit(1)", encoding="utf-8")
        runner.start(self.project, sys.executable, "unit")
        self.assertEqual(self.wait_for_worker(runner)["status"], "error")
        self.assertEqual(restarted.status()["status"], "error")
        self.assertEqual(restarted.pnl_status(show_busy=True), last_pnl)
