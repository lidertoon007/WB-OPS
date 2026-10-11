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


if __name__ == "__main__":
    unittest.main()
