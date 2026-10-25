import tempfile
import unittest
from datetime import date
from pathlib import Path

from app.models import InboxItem, ReplyDraft
from app.services.analytics import Analytics, dashboard_tables, period_keys, report_tables, to_csv
from app.storage import DraftStore


class Chats:
    def all(self):
        return [{"id": "c1", "created_at": "2026-10-05T10:00:00", "status": "answered", "marketplace": "wildberries", "messages": [{"text": "привет"}]}]


def review(i, day, rating, text, product=111, cabinet="main"):
    return InboxItem(id=f"r{i}", cabinet_id=cabinet, item_type="review", text=text, rating=rating, product_id=product,
                     product_name="Джинсы", created_at=f"{day}T09:00:00+00:00")


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DraftStore(f"sqlite:///{Path(self.temp.name) / 'db.sqlite3'}")
        items = [
            review(1, "2026-10-01", 5, "Отличное качество, сели идеально"),
            review(2, "2026-10-02", 1, "Маломерят, размер маленький, вернула"),
            review(3, "2026-10-03", 2, "Маломерит на размер"),
            review(4, "2026-10-03", 5, ""),
            review(5, "2026-09-25", 5, "Хорошее качество", cabinet="other"),
            InboxItem(id="q1", cabinet_id="main", item_type="question", text="Какой размер?", product_id=111, created_at="2026-10-02T10:00:00+00:00"),
        ]
        self.store.save_inbox(items)
        for item, source, text in ((items[0], "individual", "Спасибо!"), (items[1], "ai", "Жаль")):
            self.store.save_generated(ReplyDraft(item_id=item.id, text=text, mode="draft", source=source))
        with self.store._connect() as c:
            c.execute("UPDATE reply_drafts SET published=1, user_edited=0")
            c.execute("UPDATE reply_drafts SET text='Жаль, что не подошли' WHERE item_id='r2'")   # оператор поправил
            for item_id in ("r1", "r2"):
                c.execute("INSERT INTO audit_log(event,item_id,details,created_at) VALUES('published',?,'', '2026-10-04T09:00:00+00:00')", (item_id,))
        self.a = Analytics(self.store, Chats())

    def test_dashboard(self):
        d = self.a.dashboard("review", date(2026, 10, 1), date(2026, 10, 10))
        self.assertEqual(d["kpi"]["new"], 4)
        self.assertEqual(d["kpi"]["answered"], 2)
        self.assertEqual(d["kpi"]["prev"], 1)                 # 25.09 попадает в прошлые 10 дней
        self.assertEqual(d["ratings"]["5"], 2)
        self.assertEqual(d["text"], {"with_text": 3, "without_text": 1})
        self.assertEqual(d["cabinets"][0]["cabinet"], "main")
        only = self.a.dashboard("review", date(2026, 9, 1), date(2026, 10, 10), cabinet="other")
        self.assertEqual(only["kpi"]["new"], 1)
        self.assertEqual(self.a.dashboard("chat", date(2026, 10, 1), date(2026, 10, 10))["kpi"]["new"], 1)
        self.assertIn("Сводка", dashboard_tables(d))

    def test_methods_and_tags(self):
        d = self.a.detailed("review", date(2026, 10, 1), date(2026, 10, 10))
        self.assertEqual(d["methods"]["total"], {"individual": 1, "manual": 1})
        tags = {t["tag"]: t for t in self.a.tags(date(2026, 10, 1), date(2026, 10, 10))["tags"]}
        self.assertEqual(tags["size"]["neg_total"], 2)
        self.assertGreaterEqual(tags["quality"]["pos_total"], 1)

    def test_product_and_reports(self):
        p = self.a.product(111, date(2026, 9, 1), date(2026, 10, 10))
        self.assertEqual(p["stats"]["reviews"], 5)
        self.assertEqual(p["stats"]["questions"], 1)
        self.assertTrue(any("маломерит" in a for a in p["advice"]))
        self.assertEqual(p["cons"][0]["tag"], "size")
        saved = self.a.save_report(p)
        self.assertEqual(self.a.list_reports("джинсы")[0]["id"], saved["id"])
        self.assertEqual(self.a.get_report(saved["id"])["product_id"], 111)
        self.assertIn("Хвалят", report_tables(p))
        self.assertTrue(to_csv(report_tables(p)).startswith("﻿".encode()))
        self.assertTrue(self.a.delete_report(saved["id"]))
        self.assertEqual(self.a.list_reports(), [])

    def test_trends_and_periods(self):
        t = self.a.trends(date(2026, 10, 1), date(2026, 10, 10))
        self.assertEqual(t["growth_negative"][0]["product_id"], 111)
        self.assertEqual(period_keys(date(2026, 9, 29), date(2026, 10, 6), "week"), ["2026-09-28", "2026-10-05"])
        self.assertEqual(period_keys(date(2026, 9, 29), date(2026, 10, 6), "month"), ["2026-09", "2026-10"])


if __name__ == "__main__":
    unittest.main()
