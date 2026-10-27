"""Заказы Ozon из /v3/posting/fbo/list и /v4/posting/fbs/list (opiu/Ozon/ozon_finance.py)."""
import sys
import unittest
from datetime import date
from unittest import mock

from app.services import finance

sys.path.insert(0, str(finance.ENGINE_DIR / "Ozon"))
import ozon_finance as oz  # noqa: E402


def posting(number, status, products, day="2026-09-02T17:23:34.991432Z"):
    return {"posting_number": number, "status": status, "in_process_at": day,
            "created_at": "2026-09-01T10:00:00Z", "products": products}


class OzonPostingsTests(unittest.TestCase):
    def test_posting_rows_new_price_format(self):
        rows = oz.posting_rows([
            posting("1-0001-1", "delivered", [
                {"sku": 111, "offer_id": "ART-1", "quantity": 2, "price": {"amount": "746", "currency": "RUB"}},
                {"sku": 222, "offer_id": "", "quantity": 0, "price": {"amount": "99.5", "currency": "RUB"}}]),
            posting("1-0002-1", "cancelled", [
                {"sku": 333, "offer_id": "ART-3", "quantity": 1, "price": "3471.00"}], day=None),
        ])
        self.assertEqual(rows, [
            ["2026-09-02", "1-0001-1", "ART-1", 2, 746.0, False],
            ["2026-09-02", "1-0001-1", "SKU 222", 1, 99.5, False],      # нет артикула и количества
            ["2026-09-01", "1-0002-1", "ART-3", 1, 3471.0, True],       # без in_process_at — по created_at
        ])
        self.assertEqual(oz.posting_rows(None), [])

    def test_fetch_postings_cursor_pagination(self):
        pages = {
            (oz.URL_FBO_LIST, ""): {"postings": [posting("F-1", "delivered", [
                {"sku": 1, "offer_id": "A", "quantity": 1, "price": {"amount": "10", "currency": "RUB"}}])],
                "cursor": "", "has_next": False},
            (oz.URL_FBS_LIST, ""): {"postings": [posting("S-1", "delivered", [
                {"sku": 2, "offer_id": "B", "quantity": 1, "price": {"amount": "20", "currency": "RUB"}}])],
                "cursor": "c2", "has_next": True},
            (oz.URL_FBS_LIST, "c2"): {"postings": [posting("S-2", "cancelled", [
                {"sku": 3, "offer_id": "C", "quantity": 3, "price": {"amount": "30", "currency": "RUB"}}])],
                "cursor": "c3", "has_next": False},
        }
        calls = []

        def fake_post(account, path, body):
            calls.append((path, body))
            return pages[(path, body["cursor"])]

        with mock.patch.object(oz, "api_post", fake_post):
            rows = oz.fetch_postings({}, date(2026, 9, 1), date(2026, 9, 30))
        self.assertEqual([r[1] for r in rows], ["F-1", "S-1", "S-2"])
        self.assertEqual([(p, b["cursor"]) for p, b in calls],
                         [(oz.URL_FBO_LIST, ""), (oz.URL_FBS_LIST, ""), (oz.URL_FBS_LIST, "c2")])
        for _path, body in calls:
            self.assertLessEqual(body["limit"], 100)                    # новые методы: limit в (0, 100]
            self.assertEqual(body["filter"], {"since": "2026-09-01T00:00:00.000Z", "to": "2026-09-30T23:59:59.999Z"})
            self.assertNotIn("offset", body)


if __name__ == "__main__":
    unittest.main()
