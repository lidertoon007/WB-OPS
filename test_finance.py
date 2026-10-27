import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from openpyxl import Workbook

from app.services import finance
from app.services.cabinets import Cabinet


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


class SyncAccountsTests(unittest.TestCase):
    """Ключи API для ОПиУ берутся из кабинетов панели."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)

    def read(self, rel):
        return json.loads((self.dir / rel).read_text(encoding="utf-8-sig"))

    def test_creates_configs_from_cabinets(self):
        cabinets = [Cabinet("main", "Основной", "wb-token", tax_percent=6.0),
                    Cabinet("oz", "Озон", "oz-key", marketplace="ozon", client_id="123"),
                    Cabinet("ym", "Маркет", "ym-key", marketplace="yandex", client_id="777"),
                    Cabinet("empty", "Без ключа", "", marketplace="ozon")]
        self.assertEqual(finance.sync_accounts(self.dir, cabinets), {"wildberries": 1, "ozon": 1, "yandex": 1})
        wb = self.read("Wildberries/wb_accounts.json")
        self.assertEqual(wb["period"], {"from": finance.DEFAULT_PERIOD_FROM})
        self.assertEqual(wb["accounts"], [{"name": "Основной", "token": "wb-token", "enabled": True, "tax_percent": 6}])
        self.assertEqual(self.read("Ozon/ozon_accounts.json")["accounts"],
                         [{"name": "Озон", "client_id": "123", "api_key": "oz-key", "enabled": True}])
        self.assertEqual(self.read("YandexMarket/ym_accounts.json")["accounts"],
                         [{"name": "Маркет", "api_key": "ym-key", "enabled": True, "business_id": 777}])

    def test_updates_existing_config_keeping_name_and_fields(self):
        path = self.dir / "Wildberries" / "wb_accounts.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"period": {"from": "2025-09"}, "tax_percent": 1, "accounts": [
            {"name": "ИП Петров", "token": "old", "enabled": True, "extra": 1},
            {"name": "Старый кабинет", "token": "x", "enabled": True}]}, ensure_ascii=False), encoding="utf-8")
        cabinets = [Cabinet("main", "Петров", "new-token"), Cabinet("off", "Второй", "t2", enabled=False)]
        finance.sync_accounts(self.dir, cabinets)
        cfg = self.read("Wildberries/wb_accounts.json")
        self.assertEqual((cfg["period"], cfg["tax_percent"]), ({"from": "2025-09"}, 1))
        self.assertEqual(cfg["accounts"], [
            {"name": "ИП Петров", "token": "new-token", "enabled": True, "extra": 1},   # имя — ключ кэша
            {"name": "Старый кабинет", "token": "x", "enabled": False},                # нет в панели
            {"name": "Второй", "token": "t2", "enabled": False}])
        self.assertTrue((path.parent / "wb_accounts.json.bak").exists())
        self.assertFalse((self.dir / "Ozon").exists())   # площадку без кабинетов в панели не трогаем
        mtime = path.stat().st_mtime_ns
        (path.parent / "wb_accounts.json.bak").unlink()
        finance.sync_accounts(self.dir, cabinets)
        self.assertEqual(path.stat().st_mtime_ns, mtime)   # без изменений файл не переписывается
        self.assertFalse((path.parent / "wb_accounts.json.bak").exists())

    def test_runner_cache_mode_fills_missing(self):
        runner = finance.ReportRunner()
        (self.dir / "opiu_all.py").write_text("import sys; print(' '.join(sys.argv[1:]))", encoding="utf-8")
        runner.start(self.dir, sys.executable, "cache", note="Ключи API из кабинетов панели: WB — 1")
        runner.process.wait()
        for _ in range(100):
            if runner.status()["status"] != "running":
                break
            import time; time.sleep(0.05)
        self.assertEqual(runner.status()["log"], ["Ключи API из кабинетов панели: WB — 1",
                                                  "--no-unit --from-cache --fill-missing"])


class FillMissingTests(unittest.TestCase):
    """«Собрать из кэша»: кабинеты, которых нет в кэше, загружаются из API."""

    def setUp(self):
        sys.path.insert(0, str(finance.ENGINE_DIR))
        self.addCleanup(sys.path.remove, str(finance.ENGINE_DIR))
        import opiu_all
        self.opiu = opiu_all
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        (self.dir / "WB").mkdir()
        (self.dir / "WB" / "wb.py").write_text("", encoding="utf-8")
        (self.dir / "WB" / "wb.json").write_text(json.dumps({"period": {"from": "2026-01"}, "accounts": [
            {"name": "A", "token": "a"}, {"name": "B", "token": "b"}, {"name": "C", "token": "c", "enabled": False}]}),
            encoding="utf-8")
        self.mp = {"key": "wb", "name": "Wildberries", "script": "WB/wb.py", "config": "WB/wb.json"}
        old_here, old_run = opiu_all.HERE, opiu_all.run_script
        opiu_all.HERE = self.dir
        self.addCleanup(setattr, opiu_all, "HERE", old_here)
        self.addCleanup(setattr, opiu_all, "run_script", old_run)
        self.calls = []

        def fake_run(mp, dump, from_cache, grain="month", config=None):
            names = [a["name"] for a in json.loads(Path(config).read_text(encoding="utf-8"))["accounts"]] if config else None
            self.calls.append((from_cache, names))
            if from_cache:
                return {"months": ["2026-09"], "cabinets": {"A": {"checks": []}}}, 1, ["[B] ОШИБКА: B: нет кэша"]
            return {"months": ["2026-10"], "cabinets": {n: {"checks": []} for n in names}}, 0, []
        opiu_all.run_script = fake_run

    def args(self, **kw):
        return Namespace(**{"from_cache": True, "fill_missing": False, "cache_for": None, "grain": "month", **kw})

    def test_loads_only_missing_cabinets_from_api(self):
        data, note, errors = self.opiu.run_marketplace(self.mp, self.args(fill_missing=True), str(self.dir))
        self.assertEqual(self.calls, [(True, None), (False, ["B"])])
        self.assertEqual(sorted(data["cabinets"]), ["A", "B"])
        self.assertEqual(data["months"], ["2026-09", "2026-10"])
        self.assertEqual(data["cabinets"]["B"]["checks"][0], ["Данные из API: кабинета не было в кэше", "да"])
        self.assertEqual(note, "не было в кэше, загружено из API: B")
        self.assertEqual(errors, [])

    def test_plain_from_cache_does_not_call_api(self):
        data, note, _ = self.opiu.run_marketplace(self.mp, self.args(), str(self.dir))
        self.assertEqual(self.calls, [(True, None)])
        self.assertEqual(sorted(data["cabinets"]), ["A"])
        self.assertIn("нет в отчёте кабинетов: B", note)


if __name__ == "__main__":
    unittest.main()
