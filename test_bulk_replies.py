import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from app.models import InboxItem, ReplyDraft, OrderStatus
from app.services.bulk_replies import BulkPreviewRequest, BulkReplySender
from app.services.publishing import ReplyPublisher
from app.storage import DraftStore


class BulkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = DraftStore("sqlite:///" + str(Path(self.temp.name) / "db"))
        self.sent = []
        sent = self.sent
        class FakeWB:
            def __init__(self, *args):
                pass
            async def publish(self, item, text):
                sent.append((item.id, text))
        settings = SimpleNamespace(dry_run=True, cabinets_json="[]", feedbacks_api_base_url="https://example.test")
        publisher = ReplyPublisher(self.store, settings, lambda _: [SimpleNamespace(id="main",token="fake")], FakeWB, manual_send_enabled=True)
        self.bulk = BulkReplySender(self.store, publisher, delay=0)
        for id, kind, rating, status, product in (
            ("r1","review",5,"buyout",101), ("r2","review",5,"rejected",101),
            ("r3","review",4,"buyout",102), ("q","question",None,"notSpecified",101)):
            item = InboxItem(id=id,cabinet_id="main",item_type=kind,text="Текст обращения",rating=rating,order_status=status,product_id=product)
            self.store.save_inbox([item])
            self.store.save_generated(ReplyDraft(item_id=id,text="Ответ "+id))

    def tearDown(self):
        self.temp.cleanup()

    async def test_filters_preview_never_sends(self):
        p=self.bulk.preview(BulkPreviewRequest(filters={"rating":5,"order_status":"buyout","product_id":101}))
        self.assertEqual([x["item_id"] for x in p["items"]],["r1"])
        self.assertEqual(self.sent,[])

    async def test_only_reviews_and_current_edited_text(self):
        p=self.bulk.preview(BulkPreviewRequest(texts={"r1":"Изменённый ответ"}))
        self.assertEqual(p["total"],3)
        self.bulk.start(p["token"])
        await asyncio.gather(*self.bulk.tasks)
        self.assertEqual(dict(self.sent)["r1"],"Изменённый ответ")
        self.assertNotIn("q",dict(self.sent))
        self.assertEqual(self.bulk.get(p["token"])["sent"],3)

    async def test_new_reviews_not_added_to_confirmed_snapshot(self):
        p=self.bulk.preview(BulkPreviewRequest())
        item=InboxItem(id="new",cabinet_id="main",item_type="review",text="Новый отзыв")
        self.store.save_inbox([item])
        self.store.save_generated(ReplyDraft(item_id="new",text="Новый ответ"))
        self.bulk.start(p["token"])
        await asyncio.gather(*self.bulk.tasks)
        self.assertNotIn("new",dict(self.sent))

    async def test_repeat_start_blocked(self):
        p=self.bulk.preview(BulkPreviewRequest())
        self.bulk.start(p["token"])
        with self.assertRaises(HTTPException):
            self.bulk.start(p["token"])
        await asyncio.gather(*self.bulk.tasks)
        self.assertEqual(len(self.sent),3)

    async def test_changed_status_after_preview_skips_review(self):
        p=self.bulk.preview(BulkPreviewRequest(filters={"order_status":"buyout"}))
        item=self.store.get_inbox("r1")
        item.order_status=OrderStatus.REJECTED
        self.store.save_inbox([item])
        self.bulk.start(p["token"])
        await asyncio.gather(*self.bulk.tasks)
        self.assertNotIn("r1",dict(self.sent))
        self.assertEqual(self.bulk.get(p["token"])["sent"],1)

    async def test_uncertain_and_invalid_drafts_excluded(self):
        self.store.set_setting("reply_delivery:r1","uncertain")
        p=self.bulk.preview(BulkPreviewRequest(texts={"r2":""}))
        self.assertEqual(p["total"],1)
        self.assertEqual(len(p["skipped"]),2)

    async def test_expired_preview_cannot_send(self):
        p=self.bulk.preview(BulkPreviewRequest())
        p["created_at"]-=601
        self.bulk._save(p["token"],p)
        with self.assertRaises(HTTPException):
            self.bulk.start(p["token"])
        self.assertEqual(self.sent,[])

    async def test_concurrent_batches_blocked(self):
        p=self.bulk.preview(BulkPreviewRequest())
        second=self.bulk.preview(BulkPreviewRequest())
        self.bulk.start(p["token"])
        with self.assertRaises(HTTPException):
            self.bulk.start(second["token"])
        await asyncio.gather(*self.bulk.tasks)

    async def test_changed_draft_after_preview_preserved(self):
        p=self.bulk.preview(BulkPreviewRequest())
        self.store.update_text("r1","Другой оператор исправил ответ")
        self.bulk.start(p["token"])
        await asyncio.gather(*self.bulk.tasks)
        self.assertNotIn("r1",dict(self.sent))
        self.assertEqual(self.store.get("r1").text,"Другой оператор исправил ответ")

    async def test_texts_edited_in_review_dialog_are_sent(self):
        p=self.bulk.preview(BulkPreviewRequest())
        self.bulk.start(p["token"],{"r2":"  Отредактировано в окне проверки  "})
        await asyncio.gather(*self.bulk.tasks)
        self.assertEqual(dict(self.sent)["r2"],"Отредактировано в окне проверки")
        self.assertEqual(dict(self.sent)["r1"],"Ответ r1")
        self.assertEqual(self.bulk.get(p["token"])["sent"],3)

    async def test_dialog_edits_validated_before_start(self):
        p=self.bulk.preview(BulkPreviewRequest())
        with self.assertRaises(HTTPException):
            self.bulk.start(p["token"],{"r1":" "})
        with self.assertRaises(HTTPException):
            self.bulk.start(p["token"],{"q":"Чужой элемент"})
        self.assertEqual(self.bulk.get(p["token"])["status"],"preview")
        self.assertEqual(self.sent,[])

if __name__=="__main__":
    unittest.main()
