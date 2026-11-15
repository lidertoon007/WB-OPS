import asyncio
import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services.finance import ReportRunner
from app.services.penalties import Penalties, classify, clean_reason
from app.storage import DraftStore


def row(rid, day, op, **kw):
    base = {"rrdId": rid, "rrDate": day, "sellerOperName": op, "nmId": 111, "vendorCode": "3140-D1", "brandName": "Deemivis",
            "subjectName": "Джинсы", "srid": f"s{rid}", "reportId": 1, "penalty": "0", "deduction": "0", "retailAmount": "0", "quantity": 0, "bonusTypeName": ""}
    base.update(kw)
    return base


class PenaltiesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.cache = root / "cache"
        self.cache.mkdir()
        self.store = DraftStore(f"sqlite:///{root / 'db.sqlite3'}")
        rows = [
            row(1, "2026-10-01", "Продажа", retailAmount="5000", quantity=1),
            row(2, "2026-10-02", "Продажа", retailAmount="5000", quantity=1),
            row(3, "2026-10-02", "Возврат", retailAmount="5000", quantity=1),
            row(4, "2026-10-03", "Штраф", penalty="300", bonusTypeName="Штраф за нарушение срока передачи товара"),
            row(5, "2026-10-04", "Штраф", penalty="700", bonusTypeName="Занижение фактических габаритов упаковки товара", nmId=222, vendorCode="B012"),
            row(6, "2026-10-05", "Удержание", deduction="1000", bonusTypeName="Оказание услуг «WB Продвижение», документ №123", nmId=0, vendorCode=""),
            row(7, "2026-09-20", "Штраф", penalty="100", bonusTypeName="Штраф за нарушение срока передачи товара"),
        ]
        reports = [{"reportId": 1, "dateFrom": "2026-09-28", "dateTo": "2026-10-04", "penaltySum": "1000", "deductionSum": "0", "retailAmountSum": "0"}]
        (self.cache / "ИП_2025-09-01_2026-10-20.json").write_text(json.dumps({"rows": rows, "reports": reports, "daily": None}, ensure_ascii=False), encoding="utf-8")
        with self.store._connect() as c:
            c.execute("CREATE TABLE IF NOT EXISTS fbs_orders (cabinet_id TEXT, id INTEGER, rid TEXT, created_at TEXT, warehouse_id INTEGER, scan_dt TEXT, supplier_status TEXT, wb_status TEXT, price REAL)")
            c.execute("CREATE TABLE IF NOT EXISTS fbs_warehouses (cabinet_id TEXT, id INTEGER, name TEXT)")
            c.execute("INSERT INTO fbs_orders VALUES ('wb1', 555, 's4', '2026-10-01T10:00:00Z', 7, '2026-10-03T10:00:00Z', 'complete', 'sold', 5000)")
            c.execute("INSERT INTO fbs_warehouses VALUES ('wb1', 7, 'Мой склад')")
        self.p = Penalties(self.store, lambda: self.cache)

    def test_rules(self):
        self.assertEqual(classify("Штраф МП. Невыполненный заказ (отправка товара отличного от заявленного)")[0], "wrong")
        self.assertEqual(classify("Платное хранение возвратов на ПВЗ более 3 дней")[0], "pvz")
        self.assertEqual(clean_reason("Штраф за недовоз по поставке, поставка 41601114"), "Штраф за недовоз по поставке")

    def test_report(self):
        self.assertEqual(self.p.sync()["status"], "done")
        self.assertFalse(self.p.stale())
        r = self.p.report("2026-09-28", "2026-10-04", {}, ("2026-09-14", "2026-09-27"))
        s = r["summary"]
        self.assertEqual((s["total"], s["count"], s["revenue"], s["sales_qty"]), (1000.0, 2, 5000.0, 1))
        self.assertEqual(s["pct_revenue"], 20.0)
        self.assertEqual(s["groups"], {"fbs": {"sum": 300.0, "count": 1}, "size": {"sum": 700.0, "count": 1}})
        self.assertEqual(r["previous"]["total"], 100.0)
        self.assertEqual(r["reconcile"], {"reports": 1, "wb": 1000.0, "ours": 1000.0})
        late = next(x for x in r["reasons"] if x["group"] == "fbs")
        self.assertEqual((late["fbs_linked"], late["prev_sum"]), (1, 100.0))
        art = {a["article"]: a for a in r["articles"]}
        self.assertEqual(art["3140-D1"]["pct_revenue"], 6.0)
        r2 = self.p.report("2026-09-28", "2026-10-05", {})
        self.assertEqual(r2["deductions"]["categories"][0]["key"], "ads")
        feed = self.p.feed("2026-09-28", "2026-10-04", {}, sort="amount")
        self.assertEqual([x["amount"] for x in feed["items"]], [700.0, 300.0])
        self.assertEqual(feed["items"][1]["fbs"]["warehouse"], "Мой склад")
        self.assertEqual(feed["items"][1]["fbs"]["hours_to_sc"], 48.0)
        only = self.p.report("2026-09-28", "2026-10-04", {"group": "size"})
        self.assertEqual(only["summary"]["total"], 700.0)


    def test_refresh_waits_for_finance_and_retries_after_release(self):
        from app.config import get_settings
        from fastapi.testclient import TestClient

        root = Path(self.temp.name)
        self.addCleanup(get_settings.cache_clear)
        with mock.patch.dict(os.environ, {"WBOPS_DATABASE_URL": f"sqlite:///{root / 'api.sqlite3'}"}):
            get_settings.cache_clear()
            from app import main

            main = importlib.reload(main)

        runner = ReportRunner(root / "finance_status.json")
        other = ReportRunner(runner.status_path)
        client = TestClient(main.app)
        url = "/api/penalties?from=2026-09-28&to=2026-10-04"
        with mock.patch.object(main, "penalties", self.p), mock.patch.object(main, "finance_runner", runner):
            with other.try_run_lock():
                response = client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()["index_pending"])
                self.assertEqual(response.json()["summary"]["total"], 0)
                self.assertEqual(client.post("/api/penalties/sync").status_code, 409)
                # Run one iteration of the background task while the other worker owns the lock.
                with mock.patch.object(main.asyncio, "sleep", side_effect=[None, asyncio.CancelledError()]):
                    with self.assertRaises(asyncio.CancelledError):
                        asyncio.run(main.penalties_loop())
                self.assertTrue(self.p.stale())
            response = client.get(url)
            self.assertFalse(response.json()["index_pending"])
            self.assertEqual(response.json()["summary"]["total"], 1000)
            self.assertFalse(self.p.stale())

            # The normal "Обновить из WB" path must start finance without holding a lock itself.
            (root / "opiu_all.py").write_text("print('ok')", encoding="utf-8")
            with mock.patch.object(main, "finance_project", return_value=root), \
                 mock.patch.object(main, "sync_opiu_keys", return_value={}):
                response = client.post("/api/penalties/sync", json={"download": True})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"], "downloading")
                runner.process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
