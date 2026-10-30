"""Аналитика FBS Ozon (app/services/fbs_ozon.py): моки Seller API, без настоящих запросов."""
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import httpx

from app.services import fbs_ozon as fo
from app.services.cabinets import Cabinet
from app.services.fbs_ozon import FbsOzon, is_fine, status_group, tariff_effect
from app.storage import DraftStore

REAL_CLIENT = httpx.Client

NOW = datetime.now(timezone.utc).replace(microsecond=0)
T = lambda h: (NOW - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")      # noqa: E731
CANCEL0 = {"cancel_reason_id": 0, "cancel_reason": "", "cancellation_type": "", "cancelled_after_ship": False, "cancellation_initiator": ""}


def posting(num, created, deadline, status, products, delivering=None, cancel=None, fact=None, substatus="", wh=(7, "Мой склад")):
    return {"posting_number": num, "order_number": num.rsplit("-", 1)[0], "status": status, "substatus": substatus, "in_process_at": created,
            "shipment_date": deadline, "delivering_date": delivering, "cancellation": cancel or CANCEL0, "tariffication_fact": fact,
            "delivery_method": {"warehouse_id": wh[0], "warehouse": wh[1]},
            "products": [{"sku": sku, "offer_id": offer, "name": f"Товар {offer}", "quantity": q, "price": {"amount": str(p), "currency": "RUB"}}
                         for sku, offer, q, p in products]}


POSTINGS = [
    # создан 100 ч назад, срок через 24 ч, передан через 18 ч (факт из /get) — в срок, скидка 3%
    posting("P1-0001-1", T(100), T(76), "delivered", [(11, "A-1", 1, 1000)], delivering=T(80),
            fact={"tariff_rate": 0.03, "tariff_type": "discount"}),
    # передан через 37 ч (опоздание 13 ч), затем невыкуп; два товара: 2 × 500 + 200
    posting("P2-0001-1", T(99), T(75), "cancelled", [(11, "A-1", 2, 500), (22, "B-2", 1, 200)], delivering=T(60),
            cancel={**CANCEL0, "cancellation_type": "client", "cancelled_after_ship": True}, fact={"tariff_rate": 0, "tariff_type": "no_discount"}),
    posting("P3-0001-1", T(98), T(74), "cancelled", [(22, "B-2", 1, 300)], cancel={**CANCEL0, "cancellation_type": "client", "cancel_reason": "Покупатель отменил заказ"}),
    # срок прошёл 6 ч назад, ещё на сборке — просрочено
    posting("P4-0001-1", T(30), T(6), "awaiting_packaging", [(11, "A-1", 1, 400)]),
    posting("P5-0001-1", T(5), T(-19), "awaiting_deliver", [(22, "B-2", 1, 500)]),
    # факта в /get нет — передача по delivering_date: 27 ч, опоздание 3 ч
    posting("P6-0001-1", T(97), T(73), "delivered", [(33, "C-3", 1, 600)], delivering=T(70), wh=(8, "Второй склад")),
]
FACT = {"P1-0001-1": T(82), "P2-0001-1": T(62), "P6-0001-1": None}
TYPES = [{"id": 1, "name": "Acquiring", "description": "Эквайринг"}, {"id": 14, "name": "DefectRate", "description": "Обработка операционных ошибок продавца"},
         {"id": 93, "name": "DefectFineErrors", "description": "Превышение индекса ошибок"},
         {"id": 200, "name": "Other", "description": "Штраф за просрочку отгрузки"}]
TODAY, YESTERDAY, CACHED = date.today(), date.today() - timedelta(days=1), date.today() - timedelta(days=10)


def fine(aid, day, unit, tid, amount):
    return {"accrual_id": aid, "date": day.isoformat(), "unit_number": unit, "accrued_category": "NON_ITEM", "posting": None, "item_fees": None,
            "total_amount": {"amount": str(amount), "currency": "RUB"}, "non_item_fee": {"type_id": tid, "accrued": {"amount": str(amount), "currency": "RUB"}},
            "container_fees": None}


ACCRUALS = {  # (день, last_id) -> страница
    (TODAY.isoformat(), ""): {"accruals": [fine(1, TODAY, "P2-0001-1", 93, -50), fine(2, TODAY, "P2-0001-1", 1, -5)], "last_id": "L1"},
    (TODAY.isoformat(), "L1"): {"accruals": [fine(3, TODAY, "FBO-0001-1", 200, -7)], "last_id": "L1"},
    (YESTERDAY.isoformat(), ""): {"accruals": [fine(4, YESTERDAY, "P6-0001-1", 14, -20)], "last_id": ""},
}


class Fake:
    def __init__(self):
        self.calls = []

    def __call__(self, request):
        p, body = request.url.path, json.loads(request.content or b"{}")
        self.calls.append((p, body))
        assert request.headers["Client-Id"] == "111" and request.headers["Api-Key"] == "key"
        if p == "/v4/posting/fbs/list":
            assert body["limit"] <= 100
            lo, hi = body["filter"]["since"], body["filter"]["to"]
            found = [x for x in POSTINGS if lo <= x["in_process_at"] < hi]
            start = int(body["cursor"] or 0)
            page = found[start:start + 2]                            # по 2 на страницу — проверка курсора
            more = start + 2 < len(found)
            return httpx.Response(200, json={"postings": page, "has_next": more, "cursor": str(start + 2) if more else ""})
        if p == "/v3/posting/fbs/get":
            return httpx.Response(200, json={"result": {"posting_number": body["posting_number"], "fact_delivery_date": FACT[body["posting_number"]]}})
        if p == "/v1/finance/accrual/types":
            return httpx.Response(200, json={"accrual_types": TYPES})
        if p == "/v1/finance/accrual/by-day":
            return httpx.Response(200, json=ACCRUALS.get((body["date"], body.get("last_id", "")), {"accruals": [], "last_id": ""}))
        return httpx.Response(404, json={"message": "not found"})


def patched(test, fake):
    real = REAL_CLIENT
    for p in (mock.patch.object(fo.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(fake), timeout=5)),
              mock.patch.object(fo.time, "sleep", lambda *_: None)):
        p.start()
        test.addCleanup(p.stop)


def cabinets():
    return [Cabinet(id="oz1", name="Ozon ЕВРОЗОО", marketplace="ozon", client_id="111", token="key"),
            Cabinet(id="oz-dup", name="Дубль", marketplace="ozon", client_id="111", token="key"),          # тот же Client ID — один раз
            Cabinet(id="wb1", name="WB", token="t")]


def make_cache(root: Path):
    folder = root / "111"
    folder.mkdir(parents=True)
    (folder / f"{CACHED.isoformat()}.json").write_text(json.dumps([fine(5, CACHED, "P1-0001-1", 93, -100)]), encoding="utf-8")


class RulesTests(unittest.TestCase):
    def test_status_group(self):
        self.assertEqual(status_group("delivered"), "sold")
        self.assertEqual(status_group("cancelled", "", "client", True), "refused")
        self.assertEqual(status_group("cancelled", "", "client"), "client_cancel")
        self.assertEqual(status_group("cancelled", "", "seller"), "seller_cancel")
        self.assertEqual(status_group("cancelled", "", "ozon"), "ozon_cancel")
        self.assertEqual(status_group("delivering", "posting_in_pickup_point"), "pvz")
        self.assertEqual(status_group("delivering", "posting_on_way_to_city"), "transit")
        self.assertEqual(status_group("awaiting_deliver"), "ready")
        self.assertEqual(status_group("awaiting_packaging"), "assembly")
        self.assertEqual(status_group("awaiting_registration"), "new")

    def test_tariff_and_fines(self):
        self.assertEqual(tariff_effect("discount", 0.02), -2.0)        # в tariffication_fact доля
        self.assertEqual(tariff_effect("discount", 3), -3.0)           # в tariffication — проценты
        self.assertEqual(tariff_effect("no_discount", 0), 0.0)
        self.assertEqual(tariff_effect("surcharge", 0.05), 5.0)
        self.assertIsNone(tariff_effect(None, 0.02))
        self.assertTrue(is_fine({"name": "DefectFineErrors", "description": ""}))
        self.assertTrue(is_fine({"name": "X", "description": "Штраф за просрочку отгрузки"}))
        self.assertFalse(is_fine({"name": "Acquiring", "description": "Эквайринг"}))


class FbsOzonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.store = DraftStore(f"sqlite:///{root / 'db.sqlite3'}")
        make_cache(root / "cache")
        self.fake = Fake()
        patched(self, self.fake)
        self.f = FbsOzon(self.store, cabinets, lambda: {"A-1": {"brand": "Blitz", "category": "Корм"}}, cache_root=root / "cache")
        self.period = ((datetime.now(fo.MSK).date() - timedelta(days=13)).isoformat(), datetime.now(fo.MSK).date().isoformat())

    def test_sync_and_report(self):
        st = self.f.sync()
        self.assertEqual((st["status"], st["errors"]), ("done", []))
        self.assertEqual([c.id for c in self.f.ozon_cabinets()], ["oz1"])
        # детали запрошены только у переданных в доставку
        self.assertEqual(sorted(b["posting_number"] for p, b in self.fake.calls if p == "/v3/posting/fbs/get"), ["P1-0001-1", "P2-0001-1", "P6-0001-1"])
        # закрытый день из кэша ОПиУ, в API за ним не ходили
        days = [b["date"] for p, b in self.fake.calls if p == "/v1/finance/accrual/by-day"]
        self.assertNotIn(CACHED.isoformat(), days)
        self.assertIn(TODAY.isoformat(), days)

        rep = self.f.report(*self.period, {})
        s = rep["summary"]
        self.assertEqual((s["orders"], s["orders_sum"], s["sold"], s["sold_sum"]), (6, 4000.0, 2, 1600.0))
        self.assertEqual(s["statuses"], {"sold": 2, "refused": 1, "client_cancel": 1, "assembly": 1, "ready": 1})
        self.assertEqual((s["buyout_pct"], s["cancel_pct"], s["in_progress"]), (66.7, 16.7, 2))
        # передача: 18 ч (факт /get), 37 ч (факт /get), 27 ч (delivering_date) → среднее 27,3
        self.assertEqual((s["shipped"], s["avg_hours_to_ship"], s["bands"]), (3, 27.3, {"12_24": 1, "24_48": 2}))
        self.assertEqual((s["late"], s["avg_late_hours"], s["on_time_pct"], s["overdue"]), (2, 8.0, 33.3, 1))
        self.assertEqual(s["late_bands"], {"early": 1, "l24": 2})
        # скидка за скорость: −3% × 1000 и 0 у no_discount
        self.assertEqual((s["effect_rub"], s["tariffed"], s["discount_pct"]), (-30.0, 2, 50.0))
        # штрафы по отправлениям: P1 100 (кэш), P2 50, P6 20; эквайринг не штраф
        self.assertEqual((s["fines_rub"], s["fined"]), (170.0, 3))
        fz = rep["fines"]
        self.assertEqual((fz["total"], fz["count"], fz["fbs_total"]), (177.0, 4, 170.0))
        self.assertEqual({t["name"]: t["sum"] for t in fz["by_type"]}, {"DefectFineErrors": 150.0, "DefectRate": 20.0, "Other": 7.0})
        self.assertFalse(next(x for x in fz["items"] if x["posting"] == "FBO-0001-1")["fbs"])
        self.assertEqual(sum(d["orders"] for d in rep["by_day"]), 6)
        self.assertEqual(sum(d["fines"] for d in rep["by_day"]), 177.0)         # по дате начисления, кэшированный день в периоде
        self.assertEqual({w["warehouse"]: w["orders"] for w in rep["warehouses"]}, {"Мой склад": 5, "Второй склад": 1})
        self.assertEqual(next(a for a in rep["articles"] if a["article"] == "A-1")["brand"], "Blitz")
        self.assertEqual(rep["options"]["cabinets"], [{"id": "oz1", "name": "Ozon ЕВРОЗОО"}])

        rows = {r["posting"]: r for r in self.f.feed(*self.period, {}, limit=100)["items"]}
        self.assertEqual((rows["P1-0001-1"]["hours_to_ship"], rows["P1-0001-1"]["late_hours"], rows["P1-0001-1"]["effect_rub"]), (18.0, -6.0, -30.0))
        self.assertEqual((rows["P2-0001-1"]["price"], rows["P2-0001-1"]["qty"], rows["P2-0001-1"]["late_hours"]), (1200.0, 3, 13.0))
        self.assertEqual((rows["P4-0001-1"]["overdue"], rows["P4-0001-1"]["overdue_hours"]), (True, 6.0))
        self.assertFalse(rows["P5-0001-1"]["overdue"])
        self.assertEqual(rows["P3-0001-1"]["cancel_reason"], "Покупатель отменил заказ")

    def test_feed_filters_and_cabinet(self):
        self.f.sync()
        feed = self.f.feed(*self.period, {}, status="late", sort="late_hours")
        self.assertEqual([r["posting"] for r in feed["items"]], ["P2-0001-1", "P6-0001-1"])
        self.assertEqual(feed["extra"], {"late": 2, "overdue": 1, "fined": 3})
        self.assertEqual([r["posting"] for r in self.f.feed(*self.period, {}, status="overdue")["items"]], ["P4-0001-1"])
        self.assertEqual(self.f.feed(*self.period, {}, status="fined")["total"], 3)
        self.assertEqual(self.f.feed(*self.period, {"warehouse": "8"})["total"], 1)
        self.assertEqual(self.f.feed(*self.period, {"q": "b-2"})["total"], 3)
        self.assertEqual(self.f.report(*self.period, {"cabinet": "other"})["summary"]["orders"], 0)
        self.assertEqual(self.f.report(*self.period, {"brand": "Blitz"})["summary"]["orders"], 3)

    def test_resync_updates_status(self):
        self.f.sync()
        POSTINGS[3]["status"], POSTINGS[3]["delivering_date"] = "delivering", T(1)
        FACT["P4-0001-1"] = T(2)
        self.addCleanup(lambda: (POSTINGS[3].update(status="awaiting_packaging", delivering_date=None), FACT.pop("P4-0001-1", None)))
        self.store.set_setting("fbs_ozon_fines_at:oz1", "")
        self.f.sync()
        r = next(x for x in self.f.feed(*self.period, {})["items"] if x["posting"] == "P4-0001-1")
        self.assertEqual((r["status"], r["overdue"], r["hours_to_ship"], r["late_hours"]), ("transit", False, 28.0, 4.0))
        self.assertEqual(self.f.report(*self.period, {})["summary"]["fines_rub"], 170.0)   # штрафы не задвоились

    def test_errors(self):
        f = FbsOzon(self.store, lambda: [Cabinet(id="wb1", name="WB", token="t")])
        self.assertEqual(f.sync()["status"], "error")

        def refuse(request):
            return httpx.Response(403, json={"message": "forbidden"})
        with mock.patch.object(fo.httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(refuse))):
            st = self.f.sync()
        self.assertEqual(st["status"], "partial")
        self.assertIn("Client ID", st["errors"][0])
        self.assertNotIn("key", " ".join(st["errors"]))                # ключ в сообщениях не светится


class FbsOzonApiTests(unittest.TestCase):
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

    def test_endpoints(self):
        from fastapi.testclient import TestClient
        m = self.m
        root = Path(self.temp.name)
        make_cache(root / "cache")
        patched(self, Fake())
        service = FbsOzon(m.draft_store, cabinets, lambda: {}, cache_root=root / "cache")
        client = TestClient(m.app)
        today = datetime.now(fo.MSK).date()
        params = {"from": (today - timedelta(days=13)).isoformat(), "to": today.isoformat()}
        with mock.patch.object(m, "fbs_ozon", FbsOzon(m.draft_store, lambda: [], cache_root=root / "none")):
            self.assertEqual(client.post("/api/fbs-analytics/ozon/sync").status_code, 400)
            d = client.get("/api/fbs-analytics/ozon", params=params).json()
            self.assertEqual((d["mp"], d["summary"]["orders"], len(d["days"])), ("ozon", 0, 14))
        with mock.patch.object(m, "fbs_ozon", service):
            service.sync()
            d = client.get("/api/fbs-analytics/ozon", params=params).json()
            self.assertEqual((d["summary"]["orders"], d["summary"]["late"], d["fines"]["total"]), (6, 2, 177.0))
            self.assertEqual(client.get("/api/fbs-analytics/ozon/orders", params={**params, "status": "overdue"}).json()["total"], 1)
            csv = client.get("/api/fbs-analytics/ozon/orders.csv", params=params)
            self.assertEqual(csv.status_code, 200)
            self.assertIn("P2-0001-1", csv.content.decode("utf-8-sig"))
            self.assertEqual(client.get("/api/fbs-analytics/ozon/status").json()["status"], "done")
            self.assertEqual(client.get("/api/fbs-analytics/ozon", params={"from": "2026-10-10", "to": "2026-10-01"}).status_code, 400)
        page = client.get("/fbs-analytics")
        self.assertEqual(page.status_code, 200)
        self.assertIn("WBOPSFbsOzon", page.text)


if __name__ == "__main__":
    unittest.main()
