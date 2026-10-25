import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from app.models import InboxItem, ItemType, OrderStatus
from app.services.replies import ReplyService
from app.services.templates import TemplateStore
from app.services.wb_client import WBFeedbackClient
from app.storage import DraftStore

class NoKnowledge:
    def exact_answer(self, text, product_id):
        return None

class StatusTests(unittest.TestCase):
    """Режим «Шаблоны» (старое поведение)."""
    def setUp(self):
        # Isolated default templates: tests must not depend on the operator's edits.
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = ReplyService(NoKnowledge(), templates=TemplateStore(str(Path(self.temp.name) / "templates.json")),
                                    review_mode="templates", question_mode="templates")

    def item(self, status="notSpecified", text="Джинсы мужу понравились.", rating=5, id="test"):
        return InboxItem(id=id, cabinet_id="main", item_type="review", text=text, rating=rating, order_status=status)

    def test_positive_refusal_screenshot(self):
        text = self.service.draft(self.item("rejected")).text
        self.assertNotIn("носки", text)
        self.assertNotIn("вам подош", text)
        self.assertIn("высокую оценку", text)

    def test_all_non_buyout_statuses_and_ratings(self):
        for status in ("rejected", "returned", "notSpecified"):
            for rating in (None, 1, 2, 3, 4, 5):
                for text in ("", "Всё отлично, рекомендую", "Брак, порвана ткань", "Не подошёл размер", "Отказ на почте"):
                    with self.subTest(status=status, rating=rating, text=text):
                        answer = self.service.draft(self.item(status, text, rating)).text
                        self.assertTrue(answer)
                        self.assertNotIn("носки", answer)
                        self.assertNotIn("товар вам подош", answer)

    POSITIVE = "высокую оценку"

    def test_buyout_positive(self):
        # Positive buyout replies use the editable "review_positive" template.
        self.assertIn(self.POSITIVE, self.service.draft(self.item("buyout", "Все отлично")).text)

    def test_negative_cons_even_with_five_stars(self):
        self.assertNotIn(self.POSITIVE, self.service.draft(self.item("buyout", "Красивые\nНедостатки: Короткие")).text)
        self.assertIn(self.POSITIVE, self.service.draft(self.item("buyout", "Все отлично\nНедостатки: Нет")).text)

    def test_buyout_explicit_refusal_return_or_problem(self):
        for text in ("Красивые, но отказ", "Не выкупила", "Вернула", "Отказалась из-за брака", "Не подошел размер", "Товар кривой"):
            with self.subTest(text=text):
                self.assertNotIn("носки", self.service.draft(self.item("buyout", text)).text)

    def test_delivery_no_product_blame(self):
        answer = self.service.draft(self.item("rejected", "Товар не увидели, пришел на почту, отказ", 1)).text
        self.assertIn("поддержку Wildberries", answer)
        self.assertNotIn("товар не оправдал", answer)
        self.assertNotIn("оформите возврат", answer.lower())

    def test_unknown_never_assumes_receipt(self):
        answer = self.service.draft(self.item()).text
        self.assertNotIn("вам подош", answer)
        self.assertNotIn("носки", answer)

    def test_api_mapping(self):
        client = WBFeedbackClient("unused")
        for status in ("buyout", "rejected", "returned", "notSpecified", None, "future"):
            item = client._feedback({"id": "f", "text": "Отзыв", "pros": "Мягкие", "cons": "Короткие", "productDetails": {"nmId": 1585562726}, "orderStatus": status, "productValuation": 5})
            self.assertEqual(item.product_id, 1585562726)
            self.assertEqual(item.order_status.value, status if status in ("buyout", "rejected", "returned", "notSpecified") else "notSpecified")
            self.assertIn("Недостатки: Короткие", item.text)

    def test_question_unchanged(self):
        item = InboxItem(id="q", cabinet_id="main", item_type="question", text="Есть размер 50?")
        self.assertIn("таблицей замеров", self.service.draft(item).text)

    def test_legacy_migration_and_manual_preservation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "test.db"
            with sqlite3.connect(path) as c:
                c.execute("CREATE TABLE inbox_items (id TEXT PRIMARY KEY,cabinet_id TEXT NOT NULL,item_type TEXT NOT NULL,text TEXT NOT NULL,rating INTEGER,product_id INTEGER,created_at TEXT NOT NULL)")
            store = DraftStore("sqlite:///" + str(path))
            item = self.item("rejected")
            store.save_inbox([item])
            self.assertEqual(store.get_inbox(item.id).order_status, OrderStatus.REJECTED)
            store.save_generated(self.service.draft(item))
            store.confirm(item.id)
            store.update_text(item.id, "Ручной ответ")
            self.assertFalse(store.get(item.id).confirmed)
            store.save_generated(self.service.draft(self.item("buyout")))
            self.assertEqual(store.get(item.id).text, "Ручной ответ")
            self.assertEqual(len(store.list_inbox(order_status="rejected")), 1)
            self.assertEqual(len(store.list_inbox(order_status="buyout")), 0)

    def test_confirmed_and_published_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            store = DraftStore("sqlite:///" + str(Path(temp) / "db"))
            draft = self.service.draft(self.item("buyout"))
            store.save_generated(draft)
            store.confirm("test")
            store.save_generated(self.service.draft(self.item("rejected")))
            self.assertEqual(store.get("test").text, draft.text)
            store.mark_published("test")
            store.save_generated(self.service.draft(self.item("returned")))
            self.assertTrue(store.get("test").published)

if __name__ == "__main__":
    unittest.main()


class IndividualSafetyTests(StatusTests):
    """Те же правила безопасности для индивидуальных ответов (сборщика)."""

    def setUp(self):
        super().setUp()
        self.service.review_mode = "individual"
        self.service.question_mode = "individual"

    def test_question_unchanged(self):
        item = InboxItem(id="q", cabinet_id="main", item_type="question", text="Есть размер 50?")
        self.assertIn("Сообщить о поступлении", self.service.draft(item).text)

    # Тексты благодарности в индивидуальном режиме другие — проверяем смысл, а не формулировку
    def test_positive_refusal_screenshot(self):
        text = self.service.draft(self.item("rejected")).text
        self.assertNotIn("носки", text)
        self.assertNotIn("вам подош", text)

    def test_buyout_positive(self):
        self.assertRegex(self.service.draft(self.item("buyout", "Все отлично")).text, "носк|радуют|удовольств")

    def test_negative_cons_even_with_five_stars(self):
        self.assertNotIn("носки", self.service.draft(self.item("buyout", "Красивые\nНедостатки: Короткие")).text)
