import io
import base64
import json
import os
import re
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import httpx

from app.services import repricer as rp_module
from app.services.cabinets import Cabinet
from app.services.repricer import Repricer, RepricerError
from app.storage import DraftStore

GOODS = [{"nmID": 111, "vendorCode": "de-90", "discount": 50, "clubDiscount": 0, "editableSizePrice": False,
          "sizes": [{"sizeID": 1, "price": 10000, "discountedPrice": 5000, "techSizeName": "32"}, {"sizeID": 2, "price": 10000, "discountedPrice": 5000, "techSizeName": "34"}]},
         {"nmID": 222, "vendorCode": "B012", "discount": 0, "sizes": [{"sizeID": 3, "price": 3000, "discountedPrice": 3000, "techSizeName": "0"}]}]
ECON = {"articles": {"DE-90": {"buyout": 0.5, "commission": 0.3, "acquiring": 0.02, "logistics_per_order": 100, "paid_share": 0.8, "ads_share": 0.05}},
        "cabinet": {"buyout": 0.5, "commission": 0.3, "acquiring": 0.02, "logistics_per_order": 100, "paid_share": 0.8, "ads_share": 0.05}, "tax_rate": 6}
CATALOG = {"DE-90": {"name": "Джинсы", "brand": "Deemivis", "category": "Джинсы", "stock": 10, "cost": 1000, "prep": 100},
           "B012": {"name": "Футболка", "brand": "X", "category": "Футболки", "stock": 0, "cost": None, "prep": 0}}


class Fake:
    def __init__(self):
        self.uploads, self.status = [], 3
        self.goods_status, self.with_details = 2, True
        self.goods = json.loads(json.dumps(GOODS))

    def __call__(self, request):
        p = request.url.path
        if p == "/api/v2/list/goods/filter":
            return httpx.Response(200, json={"data": {"listGoods": self.goods}})
        if "card.wb.ru" in str(request.url):
            return httpx.Response(200, json={"products": [{"id": 111, "sizes": [{"price": {"basic": 1000000, "product": 400000}}]}]})
        if p == "/api/v2/upload/task":
            self.uploads.append(json.loads(request.content))
            return httpx.Response(200, json={"data": {"id": 55, "alreadyExists": False}, "error": False})
        if p == "/api/v2/history/tasks":
            return httpx.Response(200, json={"data": {"uploadID": 55, "status": self.status}})
        if p == "/api/v2/history/goods/task":
            details = [{"nmID": row["nmID"], "status": 3 if self.status == 5 else self.goods_status,
                        "errorText": "Товар в акции" if self.status == 5 else ""} for row in self.uploads[-1]["data"]]
            return httpx.Response(200, json={"data": {"historyGoods": details if self.with_details else []}})
        return httpx.Response(404)


class RepricerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DraftStore(f"sqlite:///{Path(self.temp.name) / 'db.sqlite3'}")
        self.fake = Fake()
        real = httpx.Client
        for p in (mock.patch.object(rp_module.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(self.fake), timeout=5)),
                  mock.patch.object(rp_module.time, "sleep", lambda *_: None)):
            p.start()
            self.addCleanup(p.stop)
        self.r = Repricer(self.store, lambda: [Cabinet(id="wb1", name="ИП", token="t")], lambda: ECON, lambda: CATALOG)
        self.r.refresh()

    def row(self, nm):
        return next(x for x in self.r.rows() if x["nm_id"] == nm)

    def test_rows_spp_and_margin(self):
        r = self.row(111)
        self.assertEqual((r["price"], r["discount"], r["price_before_spp"]), (10000, 50, 5000))
        self.assertAlmostEqual(r["spp"], 20)
        self.assertEqual((r["client_price"], r["spp_source"]), (4000, "витрина"))
        # 5000 − 1500 комиссия − 100 эквайринг − 200 логистика − 250 реклама − 240 налог − 1100 себестоимость = 1610
        self.assertEqual(r["profit"], 1610)
        self.assertAlmostEqual(r["margin"], 32.2)
        b = self.row(222)
        self.assertFalse(b["in_stock"])
        self.assertIsNone(b["margin"])
        self.r.save_settings({"wallet": {"wb1": 2}})
        self.assertEqual(self.row(111)["client_wallet"], 3920)

    def test_preview_limits(self):
        res = self.r.preview(mass={"kind": "before", "action": "up", "unit": "pct", "value": 10}, ids=["wb1:111"])[0]
        self.assertEqual((res["new_base"], res["new_price"]), (11000, 5500))
        self.assertFalse(res["errors"])
        self.assertGreater(res["new_margin"], res["old_margin"])
        res = self.r.preview(changes=[{"id": "wb1:111", "new_price": 7000}])[0]
        self.assertTrue(res["confirm"])                                       # +40% > 20% за операцию
        self.assertTrue(any("дневного лимита" in e for e in res["errors"]))   # и > 30% за сутки
        self.r.save_settings({"min_price": 4800})
        self.r.set_items(["wb1:111"], min_price=4000)                         # индивидуальный диапазон главнее
        res = self.r.preview(changes=[{"id": "wb1:111", "new_price": 4500}])[0]
        self.assertFalse(res["errors"], res)
        res = self.r.preview(changes=[{"id": "wb1:111", "new_price": 3900}])[0]
        self.assertTrue(any("артикула" in e for e in res["errors"]))
        res = self.r.preview(changes=[{"id": "wb1:111", "new_client": 4400}])[0]  # цена для клиента → до СПП: 4400 / 0.8
        self.assertEqual(res["new_price"], 5500)
        with self.assertRaises(RepricerError):
            self.r.save_settings({"min_price": 9000, "max_price": 100})

    def wait_op(self, op):
        for _ in range(500):
            if self.r.op(op["id"])["status"] != "running":
                return self.r.op(op["id"])
            threading.Event().wait(0.01)
        self.fail("операция не закончилась")

    def test_apply_and_history(self):
        with self.assertRaises(RepricerError):
            self.r.apply([{"id": "wb1:111", "new_price": 6500}], confirmed_large=False)       # +30% — нужно подтверждение
        op = self.wait_op(self.r.apply([{"id": "wb1:111", "new_price": 5500}], confirmed_large=False, author="Анна"))
        self.assertEqual((op["status"], op["ok"]), ("Выполнено", 1))
        self.assertEqual(self.fake.uploads[0], {"data": [{"nmID": 111, "price": 11000, "discount": 50}]})
        self.assertEqual(self.row(111)["price_before_spp"], 5500)              # кэш обновился
        self.assertEqual(self.r.item_history("wb1:111")[0]["author"], "Анна")
        self.assertEqual(self.r.history()[0]["status"], "Выполнено")
        self.r.save_settings({"min_interval_hours": 2})
        res = self.r.preview(changes=[{"id": "wb1:111", "new_price": 5600}])[0]
        self.assertTrue(any("следующее изменение" in e for e in res["errors"]))
        self.r.save_settings({"min_interval_hours": 0})
        self.fake.status = 5
        op = self.wait_op(self.r.apply([{"id": "wb1:111", "new_price": 5600}], confirmed_large=False))
        self.assertEqual(op["status"], "Ошибка")
        self.assertEqual(op["items"]["wb1:111"], "Товар в акции")

    def test_refresh_rate_limit(self):
        with self.assertRaises(RepricerError):
            self.r.refresh(force=True)
        self.assertEqual(self.r.refresh(), {"wb1": "свежие"})

    def test_running_operation_from_another_worker_keeps_database_status(self):
        with self.store._connect() as connection:
            op_id = connection.execute("INSERT INTO repricer_ops(status, count, ok, errors, details) VALUES('running',1,0,0,'{}')").lastrowid
        self.assertNotIn(op_id, self.r.ops)
        self.assertEqual(self.r.op(op_id)["status"], "running")

    def test_steps_preserve_discount_and_require_confirmation(self):
        steps = self.r.price_steps(100000, 10000, 50)
        self.assertEqual([s["new_price"] for s in steps], [33333.5, 22222.5, 14815, 9877, 6585, 5000])
        before = 50000
        for step in steps:
            self.assertLessEqual(before / step["new_price"], 1.5)
            before = step["new_price"]
        # Граничные суммы также округляются в сторону меньшего снижения.
        self.assertEqual(self.r.price_steps(101, 10, 0)[0]["new_base"], 68)
        self.assertEqual([s["new_base"] for s in self.r.price_steps(2800, 1000, 0)], [1867, 1245, 1000])
        for goods in self.r.cache["wb1"]["goods"]:
            if goods["nmID"] == 111:
                for size in goods["sizes"]:
                    size.update(price=100000, discountedPrice=50000)
        preview = self.r.preview(changes=[{"id": "wb1:111", "new_price": 5000}])[0]
        self.assertFalse(preview["errors"], preview)
        self.assertTrue(preview["confirm"])
        self.assertEqual(preview["steps"], steps)
        with self.assertRaises(RepricerError):
            self.r.apply([{"id": "wb1:111", "new_price": 5000}], False)
        op = self.wait_op(self.r.apply([{"id": "wb1:111", "new_price": 5000}], True))
        self.assertEqual((op["status"], op["ok"]), ("Выполнено", 1))
        self.assertEqual([u["data"][0]["price"] for u in self.fake.uploads], [66667, 44445, 29630, 19754, 13170, 10000])
        self.assertTrue(all(u["data"][0]["discount"] == 50 for u in self.fake.uploads))
        self.assertEqual(self.row(111)["price_before_spp"], 5000)
        self.assertEqual([h["new_price"] for h in self.r.item_history("wb1:111")], [33333.5, 22222.5, 14815, 9877, 6585, 5000])

    def test_chain_stops_at_error_and_keeps_applied_intermediate_price(self):
        uploads = []

        def upload(cab, items):
            uploads.append(items[0]["new_price"])
            return {items[0]["id"]: "" if len(uploads) == 1 else "Товар в карантине"}

        with mock.patch.object(self.r, "_upload", upload):
            op = self.wait_op(self.r.apply([{"id": "wb1:111", "new_price": 500}], True))
        self.assertEqual(uploads, [3333.5, 2222.5])
        self.assertEqual((op["status"], op["ok"]), ("Ошибка", 0))
        self.assertIn("Товар в карантине", op["items"]["wb1:111"])
        self.assertEqual(self.row(111)["price_before_spp"], 3333.5)
        self.assertEqual([h["new_price"] for h in self.r.item_history("wb1:111") if h["status"] == "ok"], [3333.5])

    def test_timeout_and_missing_or_unknown_details_never_succeed(self):
        cab = self.r._cabinet("wb1")
        items = [{"id": "wb1:111", "nm_id": 111, "new_base": 11000, "discount": 50}]
        self.fake.status = 1
        self.assertIn("не подтверждено", self.r._upload(cab, items)["wb1:111"])
        self.fake.status, self.fake.with_details = 3, False
        self.assertIn("не подтверждено", self.r._upload(cab, items)["wb1:111"])
        self.fake.with_details, self.fake.goods_status = True, 4
        self.assertIn("не подтвердил", self.r._upload(cab, items)["wb1:111"])

    def test_nonfinite_or_nonpositive_price_rejected(self):
        for price in (float("nan"), float("inf"), 0, -1, 0.01):
            preview = self.r.preview(changes=[{"id": "wb1:111", "new_price": price}])[0]
            self.assertTrue(preview["errors"])

    def test_first_price_uses_fresh_after_price_and_minimal_base_adjustment(self):
        goods = self.fake.goods[0]
        goods["discount"] = 0
        for size in goods["sizes"]:
            size.update(price=50000, discountedPrice=50000)
        change = {"id": "wb1:111", "first_price": True, "expected_current": 40000, "target_base": 15000, "target_price": 9706}
        item = self.r.preview([change], fresh=True)[0]
        self.assertEqual(item["old_price"], 50000)
        self.assertIn("40000", item["current_warning"])
        self.assertIn("50000", item["current_warning"])
        self.assertEqual((item["discount"], item["new_base"], item["new_price"]), (35, 14932, 9705.8))
        self.assertLess(abs(item["new_price"] - 9706), 0.5)
        before = 50000
        for step in item["steps"]:
            self.assertLessEqual(before / step["new_price"], 1.5)
            before = step["new_price"]
        self.assertEqual(item["steps"][-1]["new_price"], 9705.8)
        self.assertEqual(len(item["steps"]), 5)
        self.assertFalse(item["errors"], item)
        with self.assertRaises(RepricerError):
            self.r.apply([change], False)
        operation = self.wait_op(self.r.apply([change], True))
        self.assertEqual(operation["status"], "Выполнено")
        self.assertEqual(len(self.fake.uploads), 5)
        previous = 50000
        for upload in self.fake.uploads:
            payload = upload["data"][0]
            after = round(payload["price"] * (100 - payload["discount"]) / 100, 2)
            self.assertLessEqual(previous / after, 1.5)
            previous = after
        self.assertEqual(self.fake.uploads[-1]["data"][0], {"nmID": 111, "price": 14932, "discount": 35})
        self.assertEqual(self.row(111)["price_before_spp"], 9705.8)
        self.r.ops.clear()
        persisted = self.r.op(operation["id"])
        self.assertEqual(persisted["prices"]["wb1:111"], 9705.8)
        self.assertEqual(persisted["items"]["wb1:111"], "ok")

    def test_first_price_bypasses_daily_limit_for_nonstaged_change_only_when_flagged(self):
        goods = self.fake.goods[0]
        goods["discount"] = 0
        for size in goods["sizes"]:
            size.update(price=15000, discountedPrice=15000)
        change = {"id": "wb1:111", "expected_current": 15000, "target_base": 15000, "target_price": 10050}
        ordinary = self.r.preview([change], fresh=True)[0]
        self.assertTrue(any("дневного лимита" in error for error in ordinary["errors"]))
        first = self.r.preview([{**change, "first_price": True}], fresh=True)[0]
        self.assertFalse(first["errors"], first)
        self.assertTrue(first["confirm"])
        self.assertEqual(len(first["steps"]), 1)

    def test_first_price_current_is_optional_and_invalid_control_values_are_ignored(self):
        change = {"id": "wb1:111", "first_price": True, "target_base": 10000, "target_price": 5500}
        for current in (None, "", "не заполнять", 0, -1, float("nan")):
            supplied = {**change, "expected_current": current} if current is not None else change
            item = self.r.preview([supplied])[0]
            self.assertFalse(item["errors"], item)
            self.assertEqual(item["expected_current"], 5000)
            self.assertNotIn("current_warning", item)
        item = self.r.preview([{**change, "expected_current": 4000}])[0]
        self.assertIn("current_warning", item)

    def test_first_price_already_done_skips_upload_and_preserves_legacy_error(self):
        change = {"id": "wb1:111", "first_price": True, "target_base": 10000, "target_price": 5000}
        item = self.r.preview([change])[0]
        self.assertTrue(item["already_done"])
        self.assertFalse(item["errors"])
        self.assertFalse(item["confirm"])
        legacy = self.r.preview([{"id": "wb1:111", "new_price": 5000}])[0]
        self.assertIn("цена не меняется", legacy["errors"])
        operation = self.r.apply([change], False)
        self.assertIsNone(operation["id"])
        self.assertEqual(operation["already_done"], ["wb1:111"])
        self.assertFalse(self.fake.uploads)
        with self.store._connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM repricer_ops").fetchone()[0], 0)
        # В смешанном пакете на WB уходит только действительно изменяемый товар.
        operation = self.wait_op(self.r.apply([change, {"id": "wb1:222", "new_price": 3300}], False))
        self.assertEqual(operation["ok"], 2)
        self.assertEqual(self.fake.uploads, [{"data": [{"nmID": 222, "price": 3300, "discount": 0}]}])
        self.assertEqual(operation["prices"]["wb1:111"], 5000)


class RepricerApiTests(unittest.TestCase):
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

    def test_import_and_access(self):
        from fastapi.testclient import TestClient
        import openpyxl
        owner, remote = TestClient(self.m.app), TestClient(self.m.app, client=("192.168.1.20", 5000))
        self.assertIn(remote.get("/api/repricer").status_code, (401, 403))
        row = {"id": "wb1:111", "cabinet_id": "wb1", "cabinet": "ИП", "nm_id": 111, "price_before_spp": 50000}
        wb = openpyxl.Workbook()
        wb.active.append(["Артикул", "Цена сейчас", "Цена до скидки", "Цена после скидки"])
        wb.active.append([111, None, 15000, 9706])
        wb.active.append([999, 50000, 15000, 9706])
        wb.active.append(["", "", "", ""])
        buf = io.BytesIO()
        wb.save(buf)
        with mock.patch.object(self.m.repricer, "rows", lambda: [row]), \
                mock.patch.object(self.m.repricer, "refresh_prices") as refresh, \
                mock.patch.object(self.m.repricer, "preview", lambda changes=None, mass=None, ids=None, fresh=False: [{"id": c["id"], "new_price": c.get("target_price"), "errors": []} for c in changes]):
            res = owner.post("/api/repricer/import", files={"file": ("prices.xlsx", buf.getvalue())}).json()
            refresh.assert_called_once()
        self.assertEqual(res["skipped"], 1)
        self.assertEqual([i["id"] for i in res["items"]], ["wb1:111", "?:999"])
        self.assertEqual(res["items"][0]["new_price"], 9706)
        self.assertEqual(res["changes"], [{"id": "wb1:111", "expected_current": 50000, "target_base": 15000, "target_price": 9706, "first_price": True}])
        self.assertEqual(res["items"][1]["errors"], ["артикул не найден"])
        self.assertEqual([{key: row[key] for key in ("article", "expected_current", "target_base", "target_price")} for row in res["rows"]],
                         [{"article": "111", "expected_current": 50000, "target_base": 15000, "target_price": 9706},
                          {"article": "999", "expected_current": None, "target_base": 15000, "target_price": 9706}])
        self.assertEqual(res["rows"][1]["errors"], ["артикул не найден"])

    def import_csv(self, text, rows):
        from fastapi.testclient import TestClient
        with mock.patch.object(self.m.repricer, "rows", lambda: rows), \
                mock.patch.object(self.m.repricer, "refresh_prices"), \
                mock.patch.object(self.m.repricer, "preview", lambda changes=None, mass=None, ids=None, fresh=False: [
                    {"id": c["id"], "new_price": c.get("target_price"), "errors": []} for c in changes]):
            response = TestClient(self.m.app).post("/api/repricer/import", files={"file": ("prices.csv", text.encode("utf-8-sig"))})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_simple_import_vendor_or_nmid(self):
        rows = [{"id": "wb1:111", "cabinet_id": "wb1", "cabinet": "ИП", "nm_id": 111, "vendor": "de.90"},
                {"id": "wb1:222", "cabinet_id": "wb1", "cabinet": "ИП", "nm_id": 222, "vendor": "B012"}]
        result = self.import_csv("Артикул;Цена сейчас;Цена до скидки;Цена после скидки\n DE.90 ;50000;10000;5 200,50\n222;5000;5000;3000\n", rows)
        self.assertEqual([(i["id"], i["new_price"]) for i in result["items"]], [("wb1:111", 5200.5), ("wb1:222", 3000)])

    def test_ambiguous_unknown_shop_and_duplicate_rows_rejected(self):
        rows = [{"id": "wb1:111", "cabinet_id": "wb1", "cabinet": "ИП 1", "nm_id": 111, "vendor": "de-90"},
                {"id": "wb2:222", "cabinet_id": "wb2", "cabinet": "ИП 2", "nm_id": 222, "vendor": "de-90"}]
        result = self.import_csv("Артикул;Цена сейчас;Цена до скидки;Цена после скидки\nde-90;50000;15000;9706\n", rows)
        self.assertIn("несколько товаров", result["items"][0]["errors"][0])
        result = self.import_csv("Артикул;Цена сейчас;Цена до скидки;Цена после скидки\n222;50000;15000;9706\n", rows)
        self.assertEqual(result["items"][0]["id"], "wb2:222")
        result = self.import_csv("Артикул;Цена сейчас;Цена до скидки;Цена после скидки\n111;50000;15000;9706\n111;50000;15000;9706\n", rows)
        self.assertEqual(len(result["items"]), 2)
        self.assertTrue(all("несколько раз" in i["errors"][0] for i in result["items"]))

    def test_invalid_prices_rejected(self):
        row = {"id": "wb1:111", "cabinet_id": "wb1", "cabinet": "ИП", "nm_id": 111, "vendor": "de-90"}
        for price in ("nan", "inf", "-10", "0", "abc"):
            result = self.import_csv(f"Артикул;Цена сейчас;Цена до скидки;Цена после скидки\nde-90;50000;15000;{price}\n", [row])
            self.assertTrue(result["items"][0]["errors"])

    def test_csv_current_column_is_automatically_filled_from_wb(self):
        row = {"id": "wb1:111", "cabinet_id": "wb1", "cabinet": "ИП", "nm_id": 111, "vendor": "de-90", "price_before_spp": 50000}
        for current in ("", "любое значение", "-1", "123"):
            result = self.import_csv(f"Артикул;Цена сейчас;Цена до скидки;Цена после скидки\nde-90;{current};15000;9706\n", [row])
            self.assertEqual(result["changes"][0]["expected_current"], 50000)
            self.assertEqual(result["rows"][0]["expected_current"], 50000)
            self.assertFalse(result["rows"][0]["errors"])

    def test_template_and_prices_page(self):
        from fastapi.testclient import TestClient
        import openpyxl
        client = TestClient(self.m.app)
        for endpoint in ("/api/repricer/template.xlsx", "/api/repricer/template"):
            response = client.get(endpoint)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.assertIn('filename="shablon_cen_wb.xlsx"', response.headers["content-disposition"])
            self.assertTrue(response.content.startswith(b"PK"))
            workbook = openpyxl.load_workbook(io.BytesIO(response.content), read_only=True)
            self.assertEqual(list(next(workbook.active.iter_rows(values_only=True))), ["Артикул", "Цена сейчас", "Цена до скидки", "Цена после скидки"])
        response = client.get("/repricer")
        self.assertEqual(response.status_code, 200)
        self.assertIn('<span>Цены</span>', response.text)
        javascript = (Path(__file__).parent / "app/static/repricer.js").read_text(encoding="utf-8")
        for text in ("Артикул", "Цена сейчас", "Цена до скидки", "Цена после скидки", "Добавить строку", "Запустить все готовые", "shablon_cen_wb.xlsx"):
            self.assertIn(text, javascript)
        for text in ("Маржа", "СПП", "ступен", "role=\"tab\""):
            self.assertNotIn(text, javascript)
        fallback = base64.b64decode(re.search(r"const TEMPLATE_BASE64 = '([^']+)'", javascript).group(1))
        self.assertTrue(fallback.startswith(b"PK"))
        workbook = openpyxl.load_workbook(io.BytesIO(fallback), read_only=True)
        self.assertEqual(list(next(workbook.active.iter_rows(values_only=True))), ["Артикул", "Цена сейчас", "Цена до скидки", "Цена после скидки"])

    def test_multirow_table_preview_errors_persistence_poll_and_template_fallback(self):
        script = r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const source = fs.readFileSync('app/static/repricer.js', 'utf8');
const storage = new Map(), elements = new Map();
for (const name of ['data-table-body', 'data-articles', 'data-note', 'data-start', 'data-import', 'data-add', 'data-template', 'data-clear-done', 'data-clear-all']) elements.set('[' + name + ']', {});
const root = {querySelector: selector => elements.get(selector) || null};
const row = (uid, id, article) => ({uid, id, article, expected_current: '', target_base: 15000, target_price: 9706, state: 'ready', status: 'Готов к запуску'});
storage.set('wbops.prices.table.v1', JSON.stringify({rows: [row('r1', 'wb1:111', 'A'), row('r2', 'wb1:222', 'B')], job: null}));
global.localStorage = {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)};
global.window = {confirm: () => true};
global.setTimeout = () => 1; global.clearTimeout = () => {};
let capturedBlob, downloaded, previews = 0, applies = 0, polls = 0, catalogReads = 0, alreadyDone = 0, lastApplied = [];
URL.createObjectURL = blob => { capturedBlob = blob; return 'blob:test'; }; URL.revokeObjectURL = () => {};
global.document = {body: {append() {}}, createElement: () => ({click() { downloaded = this.download; }, remove() {}})};
global.FormData = class {append() {}};
const catalog = [{id: 'wb1:111', vendor: 'A', cabinet: 'ИП', nm_id: 111, price_before_spp: 50000}, {id: 'wb1:222', vendor: 'B', cabinet: 'ИП', nm_id: 222, price_before_spp: 50000}];
const response = (data, code = 200) => ({ok: code === 200, status: code, json: async () => data});
global.fetch = async (url, opt = {}) => {
  if (url.startsWith('/api/repricer/template')) return response({detail: 'Not Found'}, 404);
  if (url.startsWith('/api/repricer?') || url === '/api/repricer') { catalogReads++; return response({rows: catalog, cabinets: [{id: 'wb1'}]}); }
  if (url === '/api/repricer/preview') {
    previews++;
    const {changes} = JSON.parse(opt.body); assert.equal(changes.length, 2);
    assert.ok(changes.every(change => change.new_price === change.target_price && change.first_price));
    assert.ok(changes.every(change => change.expected_current === 50000));
    return response({items: changes.map((change, n) => ({id: change.id, already_done: n < alreadyDone, new_price: 9796, errors: previews === 1 && n === 1 ? ['Ошибка цены WB'] : []}))});
  }
  if (url === '/api/repricer/apply') { applies++; lastApplied = JSON.parse(opt.body).changes; return response({id: 21}); }
  if (url === '/api/repricer/ops/21') {
    polls++;
    return response(polls === 1 ? {status: 'running'} : {status: 'Выполнено частично', items: {'wb1:111': 'ok', 'wb1:222': 'Товар в карантине'}, prices: {'wb1:111': 9705.8}, errors: 1});
  }
  if (url === '/api/repricer/import') return response({rows: [
    {id: 'wb1:111', article: 'A', expected_current: 50000, target_base: 15000, target_price: 9706, errors: []},
    {id: '', article: 'unknown', expected_current: 20000, target_base: 10000, target_price: 9000, errors: ['артикул не найден']}
  ]});
  throw new Error('Unexpected URL: ' + url);
};
(async () => {
  vm.runInThisContext(source); await window.WBOPSRepricer.render(root);
  let body = elements.get('[data-table-body]');
  assert.equal((body.innerHTML.match(/data-row=/g) || []).length, 2);
  assert.match(body.innerHTML, /name="article"/); assert.match(root.innerHTML, /<th>Статус<\/th>/);
  assert.match(body.innerHTML, /name="expected_current"[^>]*readonly/);
  assert.match(root.innerHTML, /Цена сейчас загружается автоматически из WB/);
  body.oninput({target: {dataset: {field: 'expected_current'}, value: '1', closest: () => ({dataset: {row: 'r1'}})}});
  assert.equal(JSON.parse(storage.get('wbops.prices.table.v1')).rows[0].expected_current, 50000);
  await elements.get('[data-start]').onclick();
  assert.equal(applies, 0); assert.match(body.innerHTML, /Ошибка цены WB/);
  body.oninput({target: {dataset: {field: 'target_price'}, value: '9706', closest: () => ({dataset: {row: 'r2'}, querySelector: () => ({})})}});
  await elements.get('[data-start]').onclick(); await new Promise(setImmediate);
  assert.equal(applies, 1);
  assert.ok(catalogReads >= 3);
  let saved = JSON.parse(storage.get('wbops.prices.table.v1'));
  assert.equal(saved.job.id, 21); assert.ok(saved.rows.every(item => item.state === 'running'));
  assert.equal(elements.get('[data-clear-done]').disabled, true);
  assert.equal(elements.get('[data-clear-all]').disabled, true);
  elements.get('[data-clear-all]').onclick();
  assert.equal(JSON.parse(storage.get('wbops.prices.table.v1')).rows.length, 2);
  vm.runInThisContext(source); await window.WBOPSRepricer.render(root); await new Promise(setImmediate);
  saved = JSON.parse(storage.get('wbops.prices.table.v1'));
  assert.equal(saved.job, null); assert.match(saved.rows[0].status, /Готово · фактическая цена/);
  assert.equal(saved.rows[0].expected_current, 9705.8); assert.equal(saved.rows[1].status, 'Товар в карантине');
  elements.get('[data-add]').onclick(); assert.equal((body.innerHTML.match(/data-row=/g) || []).length, 3);
  await elements.get('[data-import]').onchange({target: {files: [{}], value: 'prices.xlsx'}});
  saved = JSON.parse(storage.get('wbops.prices.table.v1'));
  assert.equal(saved.rows.length, 4); assert.equal(saved.rows[3].target_price, 9000);
  assert.equal(saved.rows[3].status, 'артикул не найден');
  await elements.get('[data-template]').onclick({currentTarget: elements.get('[data-template]')});
  assert.equal(downloaded, 'shablon_cen_wb.xlsx');
  const bytes = new Uint8Array(await capturedBlob.arrayBuffer()); assert.equal(bytes[0], 80); assert.equal(bytes[1], 75);
  assert.match(elements.get('[data-note]').textContent, /Backend ещё не перезапущен/);
  alreadyDone = 1;
  storage.set('wbops.prices.table.v1', JSON.stringify({rows: [row('r1', 'wb1:111', 'A'), row('r2', 'wb1:222', 'B')], job: null}));
  vm.runInThisContext(source); await window.WBOPSRepricer.render(root);
  await elements.get('[data-start]').onclick(); await new Promise(setImmediate);
  assert.equal(applies, 2); assert.deepEqual(lastApplied.map(item => item.id), ['wb1:222']);
  saved = JSON.parse(storage.get('wbops.prices.table.v1'));
  assert.equal(saved.rows[0].state, 'done'); assert.match(saved.rows[0].status, /Готово · цена уже установлена/);
  alreadyDone = 2;
  storage.set('wbops.prices.table.v1', JSON.stringify({rows: [row('r1', 'wb1:111', 'A'), row('r2', 'wb1:222', 'B')], job: null}));
  vm.runInThisContext(source); await window.WBOPSRepricer.render(root);
  await elements.get('[data-start]').onclick(); await new Promise(setImmediate);
  saved = JSON.parse(storage.get('wbops.prices.table.v1'));
  assert.equal(applies, 2); assert.equal(saved.job, null);
  assert.ok(saved.rows.every(item => item.state === 'done'));
  assert.match(elements.get('[data-note]').textContent, /Все цены уже установлены/);
  storage.set('wbops.prices.table.v1', JSON.stringify({rows: [
    {...row('done', 'wb1:111', 'A'), state: 'done'}, {...row('success', 'wb1:111', 'A'), state: 'success'},
    {...row('error', 'wb1:222', 'B'), state: 'error', status: 'Ошибка WB'}, row('ready', 'wb1:222', 'B')
  ], job: null}));
  vm.runInThisContext(source); await window.WBOPSRepricer.render(root);
  assert.equal(elements.get('[data-clear-done]').disabled, false);
  elements.get('[data-clear-done]').onclick();
  saved = JSON.parse(storage.get('wbops.prices.table.v1'));
  assert.deepEqual(saved.rows.map(item => item.uid), ['error', 'ready']);
  assert.equal(saved.rows[0].status, 'Ошибка WB');
  elements.get('[data-clear-all]').onclick();
  saved = JSON.parse(storage.get('wbops.prices.table.v1'));
  assert.equal(saved.rows.length, 1); assert.equal(saved.rows[0].article, ''); assert.equal(saved.job, null);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(["node", "-e", script], cwd=Path(__file__).parent, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_local_launcher_checks_health_before_environment_and_uvicorn_start(self):
        launcher = (Path(__file__).parent / "start_wb_ops.bat").read_text(encoding="utf-8")
        health = launcher.index("http://127.0.0.1:8000/health")
        environment = launcher.index('if not exist ".venv\\Scripts\\python.exe"')
        launch = launcher.index("-m uvicorn app.main:app")
        self.assertLess(health, environment)
        self.assertLess(environment, launch)
        preflight = launcher[health:environment]
        for text in ("-TimeoutSec 2", "StatusCode -eq 200", "if not errorlevel 1", "exit /b 0", "Повторный запуск не требуется"):
            self.assertIn(text, preflight)
        self.assertIn('-m pip install -q -e .', launcher)
        self.assertIn('--host 127.0.0.1 --port 8000', launcher)


if __name__ == "__main__":
    unittest.main()
