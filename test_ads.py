import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import httpx

from app.services import ads as am
from app.services.ads import AdsAnalytics
from app.storage import DraftStore

TODAY = datetime.now(timezone(timedelta(hours=3))).date()
D1, D2 = (TODAY - timedelta(days=2)).isoformat(), (TODAY - timedelta(days=1)).isoformat()


class Fake:
    def __init__(self):
        self.calls = []
        self.count_by_token = {}
        self.balance_by_token = {}
        self.cashback_by_token = {}

    def __call__(self, request):
        p = request.url.path
        self.calls.append(p)
        if p == "/adv/v1/promotion/count":
            custom = self.count_by_token.get(request.headers.get("Authorization"))
            if custom is not None:
                return httpx.Response(200, json=custom)
            return httpx.Response(200, json={"adverts": [{"type": 9, "status": 9, "advert_list": [{"advertId": 10, "changeTime": D1}]},
                                                         {"type": 9, "status": 11, "advert_list": [{"advertId": 20, "changeTime": D1}]}]})
        if p == "/api/advert/v2/adverts":
            return httpx.Response(200, json={"adverts": [
                {"id": 10, "bid_type": "manual", "status": 9, "settings": {"name": "Поиск джинсы", "payment_type": "cpm", "placements": {"search": True, "recommendations": False}},
                 "nm_settings": [{"nm_id": 111, "bids_kopecks": {"search": 25000, "recommendations": 0}, "subject": {"name": "Джинсы"}}], "timestamps": {}},
                {"id": 20, "bid_type": "unified", "status": 11, "settings": {"name": "Авто", "payment_type": "cpc", "placements": {"search": True, "recommendations": True}},
                 "nm_settings": [{"nm_id": 222, "bids_kopecks": {}, "subject": {"name": "Джинсы"}}], "timestamps": {}}]})
        if p == "/adv/v3/fullstats":
            def nm(nm_id, v, c, o, s, sp):
                return {"nmId": nm_id, "name": f"Товар {nm_id}", "views": v, "clicks": c, "atbs": c // 2, "orders": o, "shks": o, "sum": s, "sum_price": sp}
            return httpx.Response(200, json=[
                {"advertId": 10, "days": [{"date": D1 + "T00:00:00Z", "apps": [{"appType": 1, "nms": [nm(111, 1000, 50, 2, 500, 6000)]},
                                                                              {"appType": 64, "nms": [nm(111, 1000, 30, 1, 300, 3000)]}]}]},
                {"advertId": 20, "days": [{"date": D2 + "T00:00:00Z", "apps": [{"appType": 32, "nms": [nm(222, 4000, 40, 0, 1200, 0)]}]}]}])
        if p == "/adv/v1/normquery/stats":
            body = json.loads(request.content)
            items = []
            for it in body["items"]:
                if it["advertId"] == 10:
                    items.append({"advertId": 10, "nmId": 111, "dailyStats": [
                        {"date": D1, "stat": {"normQuery": "джинсы мужские", "views": 800, "clicks": 40, "atbs": 10, "orders": 3, "shks": 3, "spend": 400, "avgPos": 4}},
                        {"date": D1, "stat": {"normQuery": "джинсы бесплатно", "views": 500, "clicks": 20, "atbs": 0, "orders": 0, "shks": 0, "spend": 350, "avgPos": 9}}]})
            return httpx.Response(200, json={"items": items})
        if p == "/api/advert/v2/budget":
            return httpx.Response(200, json={"adverts": [{"advertId": 10, "total": 300}, {"advertId": 20, "total": 5000}]})
        if p == "/adv/v1/balance":
            custom = self.balance_by_token.get(request.headers.get("Authorization"))
            if isinstance(custom, list):
                return httpx.Response(200, json=custom.pop(0))
            if custom is not None:
                return httpx.Response(200, json=custom)
            return httpx.Response(200, json={"balance": 1000, "net": 2000, "bonus": 50})
        if p == "/adv/v1/upd":
            rows = [{"advertId": 10, "updTime": D1 + "T10:00:00+03:00", "updNum": 1, "updSum": 800,
                     "campName": "Поиск джинсы", "paymentType": "Баланс"}]
            cashback = self.cashback_by_token.get(request.headers.get("Authorization"), 0)
            if cashback:
                rows.append({"advertId": 10, "updTime": D1 + "T12:00:00+03:00", "updNum": 2,
                             "updSum": cashback, "campName": "Поиск джинсы", "paymentType": "Кэшбэк"})
            return httpx.Response(200, json=rows)
        return httpx.Response(404)


class AdsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DraftStore(f"sqlite:///{Path(self.temp.name) / 'db.sqlite3'}")
        self.fake = Fake()
        real = httpx.Client
        for p in (mock.patch.object(am.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(self.fake), timeout=5)),
                  mock.patch.object(am.time, "sleep", lambda *_: None)):
            p.start()
            self.addCleanup(p.stop)
        with self.store._connect() as c:
            c.execute("CREATE TABLE IF NOT EXISTS rnp_orders (srid TEXT PRIMARY KEY, cabinet TEXT, day TEXT, last_change TEXT, nm_id INTEGER, "
                      "article TEXT, brand TEXT, subject TEXT, price REAL, spp REAL, finished REAL, total REAL, is_cancel INTEGER)")
            for i in range(4):
                c.execute("INSERT INTO rnp_orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"o{i}", "ИП", D1, "", 111, "3140-D1", "Deemivis", "Джинсы", 5000, 0, 0, 0, 0))
        # Один продавец подключён дважды — кампании не должны задвоиться
        self.ads = AdsAnalytics(self.store, lambda: [("ИП", "tok-a", "ads-a"), ("МИП", "tok-b", "ads-b")])

    def test_sync_and_report(self):
        st = self.ads.sync()
        self.assertEqual(st["status"], "done", st)
        r = self.ads.report((TODAY - timedelta(days=6)).isoformat(), TODAY.isoformat(), {}, ((TODAY - timedelta(days=13)).isoformat(), (TODAY - timedelta(days=7)).isoformat()))
        s = r["summary"]
        self.assertEqual((s["spend"], s["views"], s["clicks"], s["orders"]), (2000.0, 6000.0, 120.0, 3.0))
        self.assertEqual(s["cpo"], 666.67)
        self.assertEqual(s["all_orders"], 4)
        self.assertEqual(s["all_orders_sum"], 20000.0)
        self.assertEqual(s["drr_total"], 10.0)
        self.assertEqual((s["active"], s["paused"], s["budget_left"]), (1, 1, 5300.0))
        camps = {c["id"]: c for c in r["campaigns"]}
        self.assertEqual(camps[10]["drr"], round(800 / 9000 * 100, 2))
        self.assertEqual(camps[10]["articles"], ["3140-D1"])
        self.assertEqual(len(r["campaigns"]), 2)
        self.assertEqual({p["title"] for p in r["platforms"]}, {"Сайт", "iOS", "Android"})
        cl = {c["query"]: c for c in r["clusters"]}
        self.assertEqual(cl["джинсы мужские"]["cpo"], 133.33)
        self.assertEqual(r["spending"]["total"], 800.0)
        self.assertEqual(r["spending"]["balances"]["ИП"]["net"], 2000)
        kinds = {i["title"] for i in r["insights"]}
        self.assertIn("«Авто» тратит без заказов", kinds)
        self.assertIn("«Поиск джинсы»: заканчивается бюджет", kinds)
        self.assertTrue(any("поисковых запросов без заказов" in k for k in kinds))
        only = self.ads.report((TODAY - timedelta(days=6)).isoformat(), TODAY.isoformat(), {"campaign": "10"})
        self.assertEqual(only["summary"]["spend"], 800.0)
        self.assertEqual(self.fake.calls.count("/adv/v1/promotion/count"), 2)
        self.assertEqual(self.fake.calls.count("/adv/v3/fullstats"), 3)   # 90 дней = 3 запроса; второй кабинет — те же кампании, повторно не грузим

    def test_spend_for_dashboard(self):
        """Затраты для дашборда: по дням — история затрат WB, по артикулам — статистика кампаний."""
        self.assertFalse(self.ads.spend(D1, D2)["available"])             # история не загружена; есть только ledger
        self.ads.sync()
        with self.store._connect() as c:                                   # то же списание под вторым ключом продавца
            c.execute("INSERT INTO ads_upd VALUES ('МИП', 10, ?, 1, 'Поиск джинсы', 800, 'Баланс')", (D1 + "T10:00:00+03:00",))
        s = self.ads.spend(D1, D2)
        self.assertEqual(s["by_day"], {D1: 800.0})
        self.assertEqual(s["by_article"], {(D1, "3140-D1"): 800.0, (D2, "NMID222"): 1200.0})
        self.assertEqual(self.ads.spend(D1, D2, "ИП")["by_day"], {D1: 800.0})
        self.assertIsNone(self.ads.spend(D1, D2, "Другой"))                # у кабинета нет истории затрат
        self.assertFalse(self.ads.spend((TODAY - timedelta(days=400)).isoformat(), D2)["available"])

    def test_cashback_spend_is_dated_and_deduplicated(self):
        """Кэшбэк оплачивает часть уже учтённых списаний, два ключа продавца не удваивают его."""
        self.ads.sync()
        with self.store._connect() as c:
            for account in ("ИП", "МИП"):
                c.execute("INSERT INTO ads_upd VALUES (?,?,?,?,?,?,?)",
                          (account, 10, D1 + "T12:00:00+03:00", 2, "Поиск джинсы", 250, "Кэшбэк"))
        all_spend = self.ads.spend(D1, D2)
        self.assertEqual(all_spend["by_day"], {D1: 1050})
        self.assertEqual(all_spend["bonus_by_day"], {D1: 250})
        self.assertEqual(self.ads.spend(D1, D2, "ИП")["bonus_by_day"], {D1: 250})
        self.assertEqual(self.ads.spend(D2, D2, "ИП")["bonus_by_day"], {})

    def test_promo_balance_uses_full_history_for_credited_cabinet(self):
        """Остаток от подтверждённых 2 млн не зависит от периода и не берётся из net WB."""
        self.ads.sync()
        with self.store._connect() as c:
            c.execute("INSERT INTO ads_upd VALUES (?,?,?,?,?,?,?)",
                      ("ИП Нгуен", 10, "2026-08-26T12:00:00+03:00", 1, "Реклама", 100, "Кэшбэк"))
            c.execute("INSERT INTO ads_upd VALUES (?,?,?,?,?,?,?)",
                      ("ИП Нгуен", 10, D1 + "T12:00:00+03:00", 2, "Реклама", 1_533_483, "Кэшбэк"))
        current = self.ads.spend(D1, D2, "ИП Нгуен")
        self.assertEqual(current["bonus_balance"], 466_517)
        self.assertEqual(self.ads.spend(D1, D2)["bonus_balance"], 466_567)
        self.assertEqual(self.ads.spend(D1, D2, "ИП")["bonus_balance"], 50)
        old = self.ads.spend("2026-01-01", "2026-01-02", "ИП Нгуен")
        self.assertFalse(old["available"])
        self.assertEqual(old["bonus_balance"], 466_517)
        with self.store._connect() as c:
            c.execute("INSERT INTO ads_upd VALUES (?,?,?,?,?,?,?)",
                      ("ИП Нгуен", 10, D2 + "T12:00:00+03:00", 3, "Реклама", 500_000, "Кэшбэк"))
        self.assertEqual(self.ads.spend(D1, D2, "ИП Нгуен")["bonus_balance"], 0)

    def test_api_snapshots_detect_new_credit_once_and_keep_packages(self):
        future = (TODAY + timedelta(days=10)).isoformat() + "T23:59:59+03:00"
        expired = (TODAY - timedelta(days=1)).isoformat() + "T00:00:00+03:00"
        self.fake.balance_by_token["ads-a"] = [
            {"bonus": 999, "cashbacks": [{"sum": 500, "percent": 50, "expiration_date": future},
                                           {"sum": 200, "percent": 20, "expiration_date": expired}]},
            {"bonus": 999, "cashbacks": [{"sum": 1_000_300, "percent": 50, "expiration_date": future}]},
            {"bonus": 999, "cashbacks": [{"sum": 1_000_300, "percent": 50, "expiration_date": future}]},
            {"bonus": None, "cashbacks": None},
        ]
        self.assertEqual(self.ads.sync()["status"], "done")
        initial = self.ads.spend(D1, D2, "ИП")
        self.assertEqual((initial["bonus_balance"], initial["promo_inflow"], initial["promo_source"]),
                         (500, 0, "api"))
        self.assertEqual(len(initial["promo_packages"]), 1)  # истёкший пакет не входит в текущий остаток
        self.assertEqual(initial["promo_packages"][0]["percent"], 50)
        with self.store._connect() as c:
            after_expiry = self.ads._promo_state(c, "ИП", datetime.now(timezone(timedelta(hours=3))) + timedelta(days=20))
        self.assertEqual((after_expiry["balance"], after_expiry["packages"]), (0, []))
        self.fake.cashback_by_token["ads-a"] = 200
        self.assertEqual(self.ads.sync()["status"], "done")
        credited = self.ads.spend(D1, D2, "ИП")
        self.assertEqual((credited["bonus_balance"], credited["promo_inflow"]), (1_000_300, 1_000_000))
        self.assertEqual(self.ads.sync()["status"], "done")
        repeated = self.ads.spend(D1, D2, "ИП")
        self.assertEqual((repeated["bonus_balance"], repeated["promo_inflow"]), (1_000_300, 0))
        self.assertEqual(self.ads.sync()["status"], "done")
        unavailable = self.ads.spend(D1, D2, "ИП")
        self.assertEqual(unavailable["bonus_balance"], 1_000_300)  # null не возвращает ручной остаток
        self.assertEqual(unavailable["promo_updated_at"], repeated["promo_updated_at"])
        with self.store._connect() as c:
            rows = c.execute("SELECT source, inflow FROM ads_promo_snapshots WHERE account=? ORDER BY at", ("ИП",)).fetchall()
        self.assertEqual([r[0] for r in rows], ["cashbacks", "cashbacks", "cashbacks", "unavailable"])
        self.assertEqual([r[1] for r in rows], [0, 1_000_000, 0, 0])

    def test_bootstrap_switches_to_api_and_multiple_accounts_sum(self):
        owner = AdsAnalytics(self.store, lambda: [("ИП Нгуен", "owner", "owner")])
        self.fake.balance_by_token["owner"] = {"bonus": None, "cashbacks": [], "net": 9_000_000}
        self.fake.cashback_by_token["owner"] = 1_533_483
        self.assertEqual(owner.sync()["status"], "done")
        bootstrap = owner.spend(D1, D2, "ИП Нгуен")
        self.assertEqual((bootstrap["bonus_balance"], bootstrap["promo_source"]), (466_517, "bootstrap"))
        self.fake.balance_by_token["owner"] = {"bonus": 700_000, "cashbacks": None, "net": 9_000_000}
        self.assertEqual(owner.sync()["status"], "done")
        self.assertEqual((owner.spend(D1, D2, "ИП Нгуен")["bonus_balance"],
                          owner.spend(D1, D2, "ИП Нгуен")["promo_inflow"]), (700_000, 0))
        owner._record_promo_snapshot("Второй ИП", {"bonus": 300_000})
        all_accounts = owner.spend(D1, D2)
        self.assertEqual(all_accounts["bonus_balance"], 1_000_000)
        self.assertEqual(owner.spend(D1, D2, "Второй ИП")["bonus_balance"], 300_000)

    def test_bonus_is_snapshotted_before_first_campaign(self):
        self.fake.count_by_token["new"] = {"adverts": []}
        self.fake.balance_by_token["new"] = {"bonus": 42_000, "cashbacks": None}
        newcomer = AdsAnalytics(self.store, lambda: [("Новый ИП", "new", "new")])
        self.assertEqual(newcomer.sync()["status"], "done")
        state = newcomer.spend(D1, D2, "Новый ИП")
        self.assertFalse(state["available"])
        self.assertEqual((state["bonus_balance"], state["promo_inflow"]), (42_000, 0))


if __name__ == "__main__":
    unittest.main()
