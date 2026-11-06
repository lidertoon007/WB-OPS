import json
import tempfile
import unittest
from pathlib import Path

from app.services.ab_hypotheses import ABHypotheses, ABHypothesisError
from app.storage import DraftStore


class HypothesisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DraftStore(f"sqlite:///{Path(self.temp.name) / 'db.sqlite3'}")
        with self.store._connect() as c:
            c.execute("CREATE TABLE ads_campaigns (account TEXT,id INTEGER PRIMARY KEY,name TEXT,status INTEGER,payment_type TEXT,nms TEXT,budget REAL)")
            c.execute("INSERT INTO ads_campaigns VALUES(?,?,?,?,?,?,?)", ("ИП", 77, "Кампания", 9, "cpc", json.dumps([{"nm_id": 111}]), 1000))
        self.card = {"account": "ИП", "nm_id": 111, "article": "ART-111", "title": "Товар", "photos": ["p1", "p2", "p3"], "video": "v1"}
        self.s = ABHypotheses(self.store, lambda: [("ИП", "token", "ads")], card_loader=lambda nm, account=None: dict(self.card))

    def test_preflight_and_create(self):
        pf = self.s.preflight(111, 77)
        self.assertTrue(pf["ok"])
        d = self.s.create(nm_id=111, advert_id=77, hypothesis="Фото с моделью повысит CTR", owner="Анна", min_views=100)
        self.assertEqual((d["status"], d["account"], d["original_photos"]), ("ready", "ИП", ["p1", "p2", "p3"]))
        self.assertEqual(d["freshness_status"], "нет данных")

    def test_invalid_campaign_link_and_duplicate(self):
        self.assertFalse(self.s.preflight(999, 77)["ok"])
        self.s.create(nm_id=111, advert_id=77, hypothesis="Гипотеза", owner="Анна", min_views=100)
        with self.assertRaises(ABHypothesisError):
            self.s.create(nm_id=111, advert_id=77, hypothesis="Вторая", owner="Олег", min_views=100)

    def test_no_decision_and_rollback_require_runner(self):
        d = self.s.create(nm_id=111, advert_id=77, hypothesis="Гипотеза", owner="Анна", min_views=100)
        d = self.s.no_decision(d["id"], "Трафика недостаточно")
        self.assertEqual(d["status"], "no_decision")
        with self.assertRaises(ABHypothesisError):
            self.s.rollback(d["id"])

    def test_lifecycle_rejects_early_finish_and_accepts_metric_alias(self):
        class Runner:
            def __init__(self): self.next = 10; self.started = set()
            def create(self, *args, **kwargs): self.next += 1; return {"id": self.next}
            def start(self, rid): self.started.add(rid)
            def pause(self, rid, reason): self.started.discard(rid)
            def detail(self, rid): return {"summary": {"views": 0}}
            def finish(self, *args, **kwargs): return {}
        runner = Runner()
        s = ABHypotheses(self.store, lambda: [("ИП", "token", "ads")], card_loader=lambda nm, account=None: dict(self.card), runner=runner)
        d = s.create(nm_id=111, advert_id=77, hypothesis="Корзины вырастут", owner="Анна", primary_metric="cart_rate", min_views=100)
        self.assertEqual(d["primary_metric"], "atb_rate")
        d = s.start(d["id"])
        self.assertEqual(d["status"], "running")
        d = s.pause(d["id"], "Проверка")
        self.assertEqual(d["status"], "paused")
        with self.assertRaises(ABHypothesisError):
            s.finish(d["id"], "Решение")


if __name__ == "__main__":
    unittest.main()
