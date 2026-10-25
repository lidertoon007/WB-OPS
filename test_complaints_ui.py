import tempfile
import unittest
from pathlib import Path

from app.models import ComplaintCase, ComplaintStatus, ComplaintUpdate, InboxItem, ReplyDraft
from app.services.complaints import ComplaintService
from app.storage import DraftStore


class ComplaintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = DraftStore("sqlite:///" + str(Path(self.temp.name) / "db"))
        self.service = ComplaintService()
        self.item = InboxItem(id="review", cabinet_id="main", item_type="review", text="Отказ, не дали примерить на почте", rating=1, product_id=101, order_status="rejected")
        self.store.save_inbox([self.item])
        self.case = self.service.evaluate(self.item)
        self.store.save_complaint(self.case)

    def tearDown(self):
        self.temp.cleanup()

    def test_full_details_for_noneligible_review(self):
        details = self.service.details(self.case, self.item, ReplyDraft(item_id="review",text="Ответ",published=True))
        self.assertEqual(details.review_text, self.item.text)
        self.assertEqual(details.product_id, 101)
        self.assertEqual(details.order_status, "rejected")
        self.assertTrue(details.reply_published)
        self.assertEqual(details.complaint_draft, "")
        self.assertEqual(details.status, "not_eligible")

    def test_missing_review_safe(self):
        details = self.service.details(self.case, None)
        self.assertFalse(details.review_exists)
        self.assertEqual(details.review_text, "")
        self.assertEqual(details.complaint_draft, "")

    def test_candidate_has_conservative_draft(self):
        self.item.text = "Купите товар на другом сайте"
        case = self.service.evaluate(self.item)
        details = self.service.details(case, self.item)
        self.assertEqual(case.status, ComplaintStatus.READY_MANUAL)
        self.assertIn("Возможное основание", details.complaint_draft)
        self.assertIn("Купите", details.complaint_draft)
        self.assertLessEqual(len(details.complaint_draft), 1000)

    def test_submitted_and_closed_preserved_during_sync(self):
        for status in (ComplaintStatus.SUBMITTED_MANUAL, ComplaintStatus.DISMISSED):
            self.store.update_complaint_status(self.item.id, status)
            updated = self.store.save_complaint(self.service.evaluate(self.item))
            self.assertEqual(updated.status, status)

    def test_reopen_local_case(self):
        self.store.update_complaint_status("review", ComplaintStatus.DISMISSED)
        case = self.store.update_complaint_status("review", ComplaintStatus.READY_MANUAL)
        self.assertEqual(case.status, ComplaintStatus.READY_MANUAL)
        self.assertEqual(len(self.store.list_complaints("ready_manual")),1)

    def test_no_candidates_for_high_rating_or_questions(self):
        self.item.rating = 5
        self.assertIsNone(self.service.evaluate(self.item))
        self.item.item_type = "question"
        self.item.rating = 1
        self.assertIsNone(self.service.evaluate(self.item))

    def test_status_validation(self):
        for status in ("ready_manual","not_eligible","submitted_manual","dismissed"):
            self.assertEqual(ComplaintUpdate(status=status).status.value,status)
        with self.assertRaises(ValueError):
            ComplaintUpdate(status="invalid")


class ComplaintGroundsTests(unittest.TestCase):
    def check(self, text):
        item = InboxItem(id="r", cabinet_id="main", item_type="review", text=text, rating=1, product_id=1)
        return ComplaintService().evaluate(item)

    def test_delivery_and_wrong_item_grounds(self):
        case = self.check("Товар мы даже не увидели, он пришел в отделение Почта России и валяется на полу")
        self.assertEqual((case.status, case.reason_code), (ComplaintStatus.READY_MANUAL, "delivery"))
        self.assertEqual(self.check("Подмена товара. Заказала одно, пришло другое").reason_code, "wrong_item")
        details = ComplaintService().details(self.check("Подмена товара, совсем не то"), InboxItem(id="r", cabinet_id="m", item_type="review", text="Подмена товара, совсем не то", rating=1, product_id=1))
        self.assertIn("не тот товар", details.complaint_draft)

    def test_quality_or_shade_complaints_are_not_grounds(self):
        for text in ("Заказывал синие, пришли другого цвета, темно-синие", "Плохое качество, швы кривые", "Маломерят, не подошли"):
            self.assertEqual(self.check(text).status, ComplaintStatus.NOT_ELIGIBLE, text)


class PrepareRequestTests(unittest.TestCase):
    def test_reason_and_text_rules(self):
        from app.services.complaint_browser import ComplaintPrepareRequest, radio_selector
        self.assertEqual(ComplaintPrepareRequest().reason, "Другое")
        self.assertEqual(ComplaintPrepareRequest(reason="Нецензурная лексика", text="").text, "")
        self.assertEqual(radio_selector('Отзыв "не о товаре"'), 'input[type=radio][name="Отзыв \\"не о товаре\\""]')


if __name__ == "__main__":
    unittest.main()
