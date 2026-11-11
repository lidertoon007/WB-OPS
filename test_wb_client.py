import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app.services import wb_client


def _run(coroutine):
    """Run fake I/O coroutines without an event loop."""
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    finally:
        coroutine.close()
    raise AssertionError("Test coroutine unexpectedly suspended")


class _Response:
    status_code = 200
    text = ""

    def __init__(self, payload, status_code=200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self.payload


class _AsyncClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, path, params):
        self.calls.append((path, params.copy()))
        response = self.responses[(path, params["isAnswered"])]
        return response if isinstance(response, _Response) else _Response(response)


class WBHistoryTests(unittest.TestCase):
    def test_invalid_queue_entry_aborts_instead_of_truncating_snapshot(self):
        client = _AsyncClient({("/api/v1/feedbacks", "false"): {"data": {"feedbacks": [{"text": "no id"}]}}})
        with self.assertRaises(wb_client.WBApiError):
            _run(wb_client.WBFeedbackClient("token")._get_all(client, "/api/v1/feedbacks", "feedbacks", is_answered=False))

    def test_live_unanswered_count_uses_total_and_rejects_bad_payload(self):
        class CountClient:
            def __init__(self, payload):
                self.payload = payload
                self.path = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def get(self, path):
                self.path = path
                return _Response(self.payload)

        for kind, path in (("review", "/api/v1/feedbacks/count-unanswered"),
                           ("question", "/api/v1/questions/count-unanswered")):
            client = CountClient({"data": {"countUnanswered": 7, "countUnansweredToday": 2}, "error": False})
            with patch.object(wb_client.httpx, "AsyncClient", return_value=client):
                self.assertEqual(_run(wb_client.WBFeedbackClient("token").count_unanswered(kind)), 7)
            self.assertEqual(client.path, path)
        for payload in ({"data": {"countUnanswered": 0}, "error": True},
                        {"data": {"countUnanswered": "327"}, "error": False},
                        {"data": {}, "error": False}):
            with patch.object(wb_client.httpx, "AsyncClient", return_value=CountClient(payload)):
                with self.assertRaises(wb_client.WBApiError):
                    _run(wb_client.WBFeedbackClient("token").count_unanswered("review"))

    def test_rate_limit_uses_retry_header_without_waiting_or_pagination(self):
        client = _AsyncClient({("/api/v1/questions", "false"): _Response({}, 429, {"X-RateLimit-Retry": "2.2"})})
        with self.assertRaisesRegex(wb_client.WBApiError, r"WB API 429.*через 3 сек"):
            _run(wb_client.WBFeedbackClient("token")._get_all(client, "/api/v1/questions", "questions"))
        self.assertEqual(len(client.calls), 1)

    def test_rate_limit_without_valid_retry_header_has_fallback(self):
        api = wb_client.WBFeedbackClient("token")
        for header in ("", "not-a-number", "nan", "-5"):
            with self.subTest(header=header):
                client = _AsyncClient({("/api/v1/feedbacks", "false"): _Response({}, 429, {"X-RateLimit-Retry": header})})
                with self.assertRaisesRegex(wb_client.WBApiError, r"WB API 429.*Повторите импорт позже"):
                    _run(api._get_all(client, "/api/v1/feedbacks", "feedbacks"))

    def test_history_requests_both_partitions_with_timestamps_and_deduplicates(self):
        responses = {
            ("/api/v1/questions", "true"): {"data": {"questions": [{"id": "q1", "text": "answered"}]}},
            ("/api/v1/questions", "false"): {"data": {"questions": [{"id": "q1", "text": "duplicate"}, {"questionId": "q2", "text": "open"}]}},
            ("/api/v1/feedbacks", "true"): {"data": {"feedbacks": [{"id": "r1", "text": "answered"}]}},
            ("/api/v1/feedbacks", "false"): {"data": {"feedbacks": [{"id": "r1", "text": "duplicate"}, {"feedbackId": "r2", "text": "open"}]}},
        }
        for response in responses.values():
            for batch in response["data"].values():
                for item in batch:
                    item["createdDate"] = "2026-01-12T10:00:00Z"
        client = _AsyncClient(responses)
        with patch.object(wb_client.httpx, "AsyncClient", return_value=client):
            items = _run(wb_client.WBFeedbackClient("token").fetch_history("2026-01-01", "2026-01-31"))

        self.assertEqual([(item.item_type.value, item.id) for item in items],
                         [("question", "q1"), ("question", "q2"), ("review", "r1"), ("review", "r2")])
        self.assertEqual([item.answered_in_wb for item in items], [True, False, True, False])
        self.assertEqual(len(client.calls), 4)
        self.assertEqual({(path, params["isAnswered"]) for path, params in client.calls}, set(responses))
        start = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
        end = int(datetime(2026, 1, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp())
        for _, params in client.calls:
            self.assertEqual((params["dateFrom"], params["dateTo"]), (start, end))
            self.assertEqual((params["take"], params["skip"], params["order"]), (100, 0, "dateDesc"))

    def test_without_answered_and_dates_uses_only_false_partition(self):
        responses = {
            ("/api/v1/questions", "false"): {"data": {"questions": [{"id": "q1", "createdDate": "2026-01-12T10:00:00Z"}]}},
            ("/api/v1/feedbacks", "false"): {"data": {"feedbacks": [{"id": "r1", "createdDate": "2026-01-12T10:00:00Z"}]}},
        }
        client = _AsyncClient(responses)
        with patch.object(wb_client.httpx, "AsyncClient", return_value=client):
            items = _run(wb_client.WBFeedbackClient("token").fetch_history(with_answered=False))

        self.assertEqual([item.id for item in items], ["q1", "r1"])
        self.assertEqual({(path, params["isAnswered"]) for path, params in client.calls}, set(responses))
        self.assertTrue(all("dateFrom" not in params and "dateTo" not in params for _, params in client.calls))

    def test_api_payload_error_and_missing_created_date_are_not_silent_success(self):
        api = wb_client.WBFeedbackClient("token")
        client = _AsyncClient({("/api/v1/questions", "false"): {"error": True, "errorText": "Bad token", "data": None}})
        with self.assertRaisesRegex(wb_client.WBApiError, "Bad token"):
            _run(api._get_all(client, "/api/v1/questions", "questions"))
        with self.assertRaisesRegex(wb_client.WBApiError, "даты создания"):
            api._question({"id": "q1", "_wb_answered": True})

    def test_single_date_boundaries(self):
        client = _AsyncClient({("/api/v1/questions", "false"): {"data": {"questions": []}}})
        api = wb_client.WBFeedbackClient("token")
        _run(api._get_all(client, "/api/v1/questions", "questions", date_from="2026-02-02"))
        _run(api._get_all(client, "/api/v1/questions", "questions", date_to="2026-02-02"))
        self.assertEqual(client.calls[0][1]["dateFrom"], int(datetime(2026, 2, 2, tzinfo=timezone.utc).timestamp()))
        self.assertNotIn("dateTo", client.calls[0][1])
        self.assertEqual(client.calls[1][1]["dateTo"], int(datetime(2026, 2, 2, 23, 59, 59, tzinfo=timezone.utc).timestamp()))
        self.assertNotIn("dateFrom", client.calls[1][1])

    def test_invalid_dates_are_rejected_before_request(self):
        client = _AsyncClient({})
        api = wb_client.WBFeedbackClient("token")
        for date_from, date_to in (("2026-02-30", None), (None, "2026-2-2"), ("", None),
                                   ("2026-02-03", "2026-02-02")):
            with self.subTest(date_from=date_from, date_to=date_to):
                with self.assertRaises(wb_client.WBApiError):
                    _run(api._get_all(client, "/api/v1/questions", "questions",
                                      date_from=date_from, date_to=date_to))
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
