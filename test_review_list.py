import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.models import InboxItem, ReplyDraft
from app.services.review_list import ReviewList
from app.services.wb_images import WBImages, image_url
from app.storage import DraftStore


def review(i, day, rating, text, **kw):
    return InboxItem(id=f"r{i}", cabinet_id=kw.get("cabinet", "main"), item_type="review", text=text, rating=rating, product_id=kw.get("product", 111),
                     product_name="Джинсы", author_name=kw.get("name", "Анна"), order_status=kw.get("order", "buyout"),
                     created_at=f"{day}T09:00:00+00:00")


class ReviewListTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DraftStore(f"sqlite:///{Path(self.temp.name) / 'db.sqlite3'}")
        self.store.save_inbox([
            review(1, "2026-10-01", 5, "Отличное качество, сели идеально"),
            review(2, "2026-10-02", 1, "Маломерят, размер маленький", order="rejected", name="Борис"),
            review(3, "2026-10-03", 2, "Маломерит на размер", cabinet="other"),
            review(4, "2026-10-04", 5, ""),
        ])
        for item_id, source, text in (("r1", "ai", "Спасибо!"), ("r2", "individual", "Жаль"), ("r3", "individual", "Жаль")):
            self.store.save_generated(ReplyDraft(item_id=item_id, text=text, mode="draft", source=source))
        with self.store._connect() as c:
            c.execute("UPDATE reply_drafts SET published=1 WHERE item_id='r1'")
            c.execute("UPDATE reply_drafts SET text='Жаль, что не подошли' WHERE item_id='r2'")
            c.execute("UPDATE reply_drafts SET moderation_status='rework' WHERE item_id='r3'")
        self.list = ReviewList(self.store, cabinet_names=lambda: {"main": "ИП Тест"}, vendor_path=str(Path(self.temp.name) / "none.json"))

    def test_statuses_counts_and_methods(self):
        d = self.list.query()
        self.assertEqual(d["total"], 4)
        self.assertEqual(d["counts"]["published"], 1)
        self.assertEqual(d["counts"]["unanswered"], 3)
        self.assertEqual(d["counts"]["nodraft"], 1)
        by_id = {i["id"]: i for i in d["items"]}
        self.assertEqual(by_id["r1"]["method_title"], "Ответ ИИ")
        self.assertEqual(by_id["r2"]["method"], "manual")              # оператор изменил черновик
        self.assertEqual(by_id["r3"]["status"], "rework")
        self.assertEqual(by_id["r1"]["cabinet_name"], "ИП Тест")
        self.assertEqual([i["id"] for i in d["items"]], ["r4", "r3", "r2", "r1"])
        self.assertEqual([i["id"] for i in self.list.query(sort="asc")["items"]][0], "r1")

    def test_wb_snapshot_excludes_stale_reviews_without_faking_publication(self):
        before = self.store.get("r2")
        self.store.reconcile_wb_unanswered("main", ["review"], ["r4"])
        data = self.list.query(answer="unanswered")
        self.assertEqual({r["id"] for r in data["items"]}, {"r3", "r4"})
        self.assertEqual(data["counts"]["unanswered"], 2)
        self.assertEqual(self.store.get("r2"), before)
        self.assertEqual(self.list.query()["total"], 4)
        self.assertEqual(self.list.query()["counts"]["published"], 1)
        legacy = self.store.list_inbox_overview(status="new")
        self.assertEqual({r["id"] for r in legacy}, {"r3", "r4"})
        self.store.reconcile_wb_unanswered("main", ["review"], ["r2", "r4"])
        self.assertEqual(self.list.query(answer="unanswered")["total"], 3)

    def test_empty_snapshot_is_scoped_to_enabled_kind_and_cabinet(self):
        self.store.save_inbox([InboxItem(id="q1", cabinet_id="main", item_type="question", text="Вопрос")])
        self.store.reconcile_wb_unanswered("main", ["review"], [])
        self.assertEqual(self.list.query(answer="unanswered")["total"], 1)
        self.assertEqual(self.list.query(item_type="question", answer="unanswered")["total"], 1)
        self.assertEqual(self.list.query(cabinet_id="other", answer="unanswered")["total"], 1)

    def test_failed_sync_preserves_snapshot_and_successful_empty_sync_reconciles(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from app import main
        from test_wb_client import _run
        from app.services.wb_client import WBApiError
        cabinet = SimpleNamespace(id="main", enabled=True, marketplace="wildberries", reviews=True,
                                  questions=False, token="test")
        patches = (patch.object(main, "configured_cabinets", return_value=[cabinet]),
                   patch.object(main, "draft_store", self.store))
        with patches[0], patches[1], patch.object(main.WBFeedbackClient, "fetch_unanswered", new=AsyncMock(side_effect=WBApiError("429"))):
            with self.assertRaises(WBApiError):
                _run(main.perform_sync("main"))
        self.assertEqual(self.list.query(answer="unanswered")["total"], 3)
        with patch.object(main, "configured_cabinets", return_value=[cabinet]), patch.object(main, "draft_store", self.store), \
                patch.object(main.WBFeedbackClient, "fetch_unanswered", new=AsyncMock(return_value=[])):
            _run(main.perform_sync("main"))
        self.assertEqual(self.list.query(answer="unanswered")["total"], 1)

    def test_filters(self):
        q = self.list.query
        self.assertEqual(q(answer="published")["total"], 1)
        self.assertEqual(q(answer="unanswered")["total"], 3)
        self.assertEqual(q(ratings=[1, 2, 3])["total"], 2)
        self.assertEqual(q(cabinet_id="other")["total"], 1)
        self.assertEqual(q(order_status="rejected")["items"][0]["id"], "r2")
        self.assertEqual(q(search="борис")["total"], 1)
        self.assertEqual(q(date_from="2026-10-02", date_to="2026-10-03")["total"], 2)
        self.assertEqual(q(tags=["size:negative"])["total"], 2)
        self.assertEqual(q(tags=["quality:positive"])["items"][0]["id"], "r1")
        self.assertEqual(q(tags=["quality:negative"])["total"], 0)
        page = q(limit=2, offset=2)
        self.assertEqual((len(page["items"]), page["total"]), (2, 4))

    def test_warnings_and_export(self):
        warn = lambda text, item: ["нельзя"] if item.id == "r2" else []
        items = {i["id"]: i for i in self.list.query(with_warnings=warn)["items"]}
        self.assertEqual(items["r2"]["warnings"], ["нельзя"])
        self.assertEqual(items["r1"]["warnings"], [])                  # отправленные не проверяем
        rows = self.list.export_rows(answer="unanswered")
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0][0], "Дата")


class ImageTests(unittest.TestCase):
    def test_url_and_offline_backoff(self):
        self.assertEqual(image_url("basket-12.wbbasket.ru", 123456789), "https://basket-12.wbbasket.ru/vol1234/part123456/123456789/images/c246x328/1.webp")
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = DraftStore(f"sqlite:///{Path(temp.name) / 'db.sqlite3'}")
        images = WBImages(store)

        async def fail(*a, **k):
            raise __import__("httpx").ConnectError("offline")
        with mock.patch("httpx.AsyncClient.head", new=fail):
            self.assertEqual(asyncio.run(images.urls([123456789, 223456789])), {"123456789": None, "223456789": None})
        self.assertGreater(images._down_until, 0)                       # сеть недоступна — пауза

        async def ok(self_client, url, **k):
            return mock.Mock(status_code=200 if "basket-03" in url else 404)
        images._down_until = 0
        with mock.patch("httpx.AsyncClient.head", new=ok):
            urls = asyncio.run(images.urls([123456789]))
        self.assertIn("basket-03", urls["123456789"])
        self.assertEqual(store.get_setting("wb_basket:1234", ""), "basket-03.wbbasket.ru")


if __name__ == "__main__":
    unittest.main()
