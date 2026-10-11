import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import HTTPException

from app.models import InboxItem, ReplyDraft
from app.services.publishing import ReplyPublisher
from app.services.wb_client import WBApiError, WBDeliveryUncertain, WBFeedbackClient
from app.storage import DraftStore

class SendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = DraftStore("sqlite:///" + str(Path(self.temp.name) / "test.db"))
        self.item = InboxItem(id="review", cabinet_id="main", item_type="review", text="Отзыв", order_status="buyout", rating=5)
        self.store.save_inbox([self.item])
        self.store.save_generated(ReplyDraft(item_id="review", text="Старый черновик"))
        self.settings = SimpleNamespace(dry_run=False, cabinets_json="[]", feedbacks_api_base_url="https://example.test")
        self.sent = []
        sent = self.sent
        class FakeWB:
            def __init__(self, *args):
                pass
            async def publish(self, item, text):
                sent.append((item.id, text))
                await asyncio.sleep(0)
        self.publisher = ReplyPublisher(self.store, self.settings, lambda _: [SimpleNamespace(id="main", token="fake-token")], FakeWB)

    def tearDown(self):
        self.temp.cleanup()

    async def test_sends_exact_edited_text_and_removes_from_inbox(self):
        response = await self.publisher.send("review", "  Спасибо за отзыв!  ")
        self.assertTrue(response["published"])
        self.assertEqual(self.sent, [("review", "Спасибо за отзыв!")])
        self.assertTrue(self.store.get("review").published)
        self.assertTrue(self.store.get("review").confirmed)
        self.assertEqual(self.store.list_inbox(), [])
        self.assertEqual(self.store.list_audit()[0]["event"], "published")

    async def test_double_click_only_one_request(self):
        results = await asyncio.gather(self.publisher.send("review", "Первый ответ"), self.publisher.send("review", "Второй ответ"), return_exceptions=True)
        self.assertEqual(len(self.sent), 1)
        self.assertIsInstance(results[1], HTTPException)
        self.assertEqual(self.store.get("review").text, "Первый ответ")

    async def test_second_process_sqlite_claim(self):
        other = ReplyPublisher(self.store, self.settings, self.publisher.cabinet_loader, self.publisher.client_factory)
        results = await asyncio.gather(self.publisher.send("review", "Первый ответ"), other.send("review", "Второй ответ"), return_exceptions=True)
        self.assertEqual(len(self.sent), 1)
        self.assertIsInstance(results[1], HTTPException)
        self.assertEqual(self.store.get("review").text, "Первый ответ")

    async def test_dry_run_blocks_send(self):
        self.settings.dry_run = True
        with self.assertRaises(HTTPException) as error:
            await self.publisher.send("review", "Спасибо")
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.sent, [])

    async def test_manual_mode_only_enables_explicit_text_send(self):
        self.settings.dry_run = True
        self.publisher.manual_send_enabled = True
        with self.assertRaises(HTTPException):
            await self.publisher.send("review")
        result = await self.publisher.send("review", "Ручной ответ")
        self.assertTrue(result["published"])
        self.assertEqual(self.sent, [("review", "Ручной ответ")])

    async def test_empty_too_short_too_long(self):
        for text in ("", " ", "Я", "x" * 5001):
            with self.assertRaises(HTTPException):
                await self.publisher.send("review", text)
        self.assertEqual(self.sent, [])

    async def test_missing_id(self):
        with self.assertRaises(HTTPException) as error:
            await self.publisher.send("absent", "Спасибо")
        self.assertEqual(error.exception.status_code, 404)
        self.assertEqual(self.sent, [])

    async def test_legacy_publish_requires_confirmation(self):
        with self.assertRaises(HTTPException):
            await self.publisher.send("review")
        self.assertEqual(self.sent, [])

    async def test_api_failure_keeps_draft_and_allows_retry(self):
        class FailingWB:
            def __init__(self, *args):
                pass
            async def publish(self, *args):
                raise WBApiError("WB API 403: Нет доступа")
        self.publisher.client_factory = FailingWB
        with self.assertRaises(HTTPException):
            await self.publisher.send("review", "Новый ответ")
        self.assertFalse(self.store.get("review").published)
        self.assertEqual(self.store.get("review").text, "Новый ответ")
        self.assertEqual(self.store.get_setting("reply_delivery:review", ""), "")

    async def test_uncertain_delivery_persistently_blocks_retry(self):
        class UncertainWB:
            def __init__(self, *args):
                pass
            async def publish(self, *args):
                raise WBDeliveryUncertain("Timeout")
        self.publisher.client_factory = UncertainWB
        with self.assertRaises(HTTPException):
            await self.publisher.send("review", "Новый ответ")
        another = ReplyPublisher(self.store, self.settings, self.publisher.cabinet_loader, self.publisher.client_factory)
        with self.assertRaises(HTTPException) as error:
            await another.send("review", "Ещё один")
        self.assertEqual(error.exception.status_code, 409)
        self.assertFalse(self.store.get("review").published)

class WBTransportTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, item_type, handler):
        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        def factory(**kwargs):
            return real_client(transport=transport, **kwargs)
        item = InboxItem(id="test", cabinet_id="main", item_type=item_type, text="Текст")
        with patch("app.services.wb_client.httpx.AsyncClient", factory):
            await WBFeedbackClient("fake", "https://example.test").publish(item, "Ответ клиента")

    async def test_review_and_question_paths(self):
        for kind, verb, path in (("review", "POST", "/api/v1/feedbacks/answer"), ("question", "PATCH", "/api/v1/questions")):
            def handler(request):
                self.assertEqual(request.method, verb)
                self.assertEqual(request.url.path, path)
                self.assertIn("Ответ клиента".encode(), request.content)
                return httpx.Response(204)
            await self.run_case(kind, handler)

    async def test_api_error_in_200_is_not_success(self):
        with self.assertRaises(WBApiError):
            await self.run_case("question", lambda _: httpx.Response(200, json={"error": True, "errorText": "Ошибка"}))

    async def test_request_timeout_is_uncertain(self):
        def handler(request):
            raise httpx.ReadTimeout("Timeout", request=request)
        with self.assertRaises(WBDeliveryUncertain):
            await self.run_case("review", handler)

if __name__ == "__main__":
    unittest.main()
