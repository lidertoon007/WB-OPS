import tempfile
import unittest
from unittest.mock import patch
from datetime import date
from pathlib import Path

from app.models import InboxItem, ReplyDraft
from app.services.analytics import Analytics, dashboard_tables, period_keys, report_tables, to_csv
from app.storage import DraftStore
from app.services.cabinets import Cabinet
from app.services.wb_client import WBApiError, WBFeedbackClient


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
        self.assertIsNone(d["kpi"]["waiting"])  # Local statuses are not a current WB count.
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

    def test_imported_history_is_analytics_only_and_deduplicated_with_inbox(self):
        historical = [
            review(1, "2026-10-01", 5, "Дубликат рабочего отзыва"),
            review(20, "2026-10-06", 4, "Исторический отзыв", cabinet="other"),
            InboxItem(id="q20", cabinet_id="other", item_type="question", text="Исторический вопрос",
                      product_id=222, created_at="2026-10-06T10:00:00+00:00", answered_in_wb=False),
        ]
        historical[0].answered_in_wb = True
        historical[1].answered_in_wb = True
        self.store.save_analytics_history(historical)
        self.store.save_analytics_history(historical)

        dashboard = self.a.dashboard("review", date(2026, 10, 1), date(2026, 10, 10))
        self.assertEqual(dashboard["kpi"]["new"], 5)
        self.assertEqual(dashboard["kpi"]["answered"], 3)
        self.assertIsNone(dashboard["kpi"]["waiting"])
        self.assertEqual(self.a.dashboard("review", date(2026, 10, 1), date(2026, 10, 10), cabinet="other")["kpi"]["new"], 1)
        self.assertEqual(self.a.dashboard("question", date(2026, 10, 1), date(2026, 10, 10), cabinet="other")["kpi"]["new"], 1)
        self.assertEqual(len(self.a.filtered("review", date(2026, 10, 1), date(2026, 10, 10), marketplace="ozon")), 0)
        self.assertEqual(self.a.products(date(2026, 10, 1), date(2026, 10, 10))[0]["reviews"], 5)
        with self.store._connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM inbox_items WHERE id IN ('r20','q20')").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM reply_drafts WHERE item_id IN ('r20','q20')").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM analytics_history").fetchone()[0], 3)


class DashboardLiveCountTests(unittest.TestCase):
    def test_dashboard_uses_live_count_and_hides_partial_or_unsupported_counts(self):
        import importlib

        main = importlib.import_module("app.main")

        class LocalAnalytics:
            def dashboard(self, *args, **kwargs):
                return {"kpi": {"waiting": 327}}

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, *args):
                return self

            def fetchall(self):
                return [("nguyen", "wildberries"), ("other", "wildberries")]

        class Store:
            def _connect(self):
                return Connection()

        async def count(self, kind):
            if self.token == "limited":
                raise WBApiError("WB API 429: превышен лимит запросов")
            return {"nguyen-token": 7, "other-token": 3}[self.token]

        cabinets = [Cabinet("nguyen", "Нгуен", "nguyen-token"), Cabinet("other", "Другой", "other-token")]

        def run(**filters):
            coroutine = main.analytics_dashboard(kind="review", date_from="2026-10-01", date_to="2026-10-10",
                                                 grain="day", format=None, **filters)
            try:
                coroutine.send(None)
            except StopIteration as result:
                return result.value["kpi"]
            finally:
                coroutine.close()
            self.fail("Dashboard coroutine unexpectedly suspended")

        with patch.object(main, "analytics", LocalAnalytics()), patch.object(main, "draft_store", Store()), \
             patch.object(main, "configured_cabinets", return_value=cabinets), \
             patch.object(WBFeedbackClient, "count_unanswered", count):
            self.assertEqual(run(cabinet="nguyen", marketplace="wildberries")["waiting"], 7)
            self.assertEqual(run(marketplace="wildberries")["waiting"], 10)
            self.assertIsNone(run(cabinet="nguyen", marketplace="ozon")["waiting"])
            cabinets[1] = Cabinet("other", "Другой", "limited")
            failed = run(marketplace="wildberries")
            self.assertIsNone(failed["waiting"])
            self.assertIn("429", failed["waiting_note"])


if __name__ == "__main__":
    unittest.main()
