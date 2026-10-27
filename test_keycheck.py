import asyncio
import base64
import json
import time
import unittest
from unittest import mock

import httpx

from app.services import keycheck
from app.services.cabinets import Cabinet


def wb_token(exp: float, s: int) -> str:
    part = base64.urlsafe_b64encode(json.dumps({"exp": int(exp), "s": s}).encode()).decode().rstrip("=")
    return f"hdr.{part}.sig"


def patched_client(handler):
    real = httpx.AsyncClient
    return mock.patch.object(keycheck.httpx, "AsyncClient",
                             lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


class KeyCheckTests(unittest.TestCase):
    def run_check(self, cabinet, handler):
        with patched_client(handler):
            return asyncio.run(keycheck.check(cabinet))

    def test_wb_partial_access_depends_on_toggles(self):
        token = wb_token(time.time() + 86400 * 100, (1 << 30) | (1 << 5) | (1 << 4))
        allowed = {"statistics-api", "marketplace-api"}
        seen = []

        def handler(request):
            seen.append((request.url.host, request.headers["Authorization"]))
            return httpx.Response(200 if request.url.host.split(".")[0] in allowed else 403)

        cabinet = Cabinet("main", "Еврозоо", token)
        result = self.run_check(cabinet, handler)
        self.assertEqual(len(seen), len(keycheck.WB_SCOPES))
        self.assertTrue(all(auth == token for _, auth in seen))
        self.assertEqual((result["status"], result["read_only"]), ("ok", True))   # сохраняется «сырой» результат
        shown = keycheck.present(result, cabinet)
        self.assertEqual(shown["status"], "partial")
        self.assertEqual(shown["missing"], ["Вопросы и отзывы", "Финансы", "запись"])
        self.assertNotIn("fingerprint", shown)
        # Без отзывов, вопросов и статистики ничего обязательного не требуется
        quiet = cabinet.with_changes(reviews=False, questions=False, stats=False)
        self.assertEqual(keycheck.present(result, quiet)["status"], "ok")
        # Новый ключ — прежний результат не показывается
        self.assertIsNone(keycheck.present(result, cabinet.with_changes(token=wb_token(time.time() + 9e6, 0))))

    def test_wb_ads_token_used_for_advert(self):
        cabinet = Cabinet("main", "К", wb_token(time.time() + 1000, 0), ads_token="ADS")
        hosts = {}

        def handler(request):
            hosts[request.url.host] = request.headers["Authorization"]
            return httpx.Response(200)

        result = self.run_check(cabinet, handler)
        self.assertEqual(hosts["advert-api.wildberries.ru"], "ADS")
        self.assertEqual(keycheck.present(result, cabinet)["status"], "ok")

    def test_wb_invalid_expired_and_offline(self):
        cabinet = Cabinet("main", "К", wb_token(time.time() + 1000, 0))
        self.assertEqual(self.run_check(cabinet, lambda r: httpx.Response(401))["status"], "error")

        def offline(request):
            raise httpx.ConnectError("нет сети", request=request)
        self.assertEqual(self.run_check(cabinet, offline)["status"], "unknown")
        expired = cabinet.with_changes(token=wb_token(time.time() - 10, 0))
        result = self.run_check(expired, lambda r: self.fail("истёкший ключ не проверяется по сети"))
        self.assertEqual(result["status"], "error")
        self.assertIn("истёк", result["message"])

    def test_ozon_and_yandex(self):
        ozon = Cabinet("oz", "Озон", "KEY", marketplace="ozon", client_id="42")

        def ozon_ok(request):
            self.assertEqual((request.headers["Client-Id"], request.headers["Api-Key"]), ("42", "KEY"))
            return httpx.Response(200, json={"roles": [{"name": "Admin"}], "expires_at": "2027-01-31T00:00:00Z"})
        result = self.run_check(ozon, ozon_ok)
        self.assertEqual((result["status"], result["expires_at"], result["scopes"][0]["title"]), ("ok", "2027-01-31", "Admin"))
        self.assertEqual(self.run_check(ozon, lambda r: httpx.Response(401))["status"], "error")
        self.assertEqual(asyncio.run(keycheck.check(ozon.with_changes(client_id="")))["status"], "error")

        ym = Cabinet("ym", "Маркет", "YM", marketplace="yandex")
        result = self.run_check(ym, lambda r: httpx.Response(200, json={"campaigns": [{"id": 1}]}))
        self.assertEqual((result["status"], result["message"]), ("ok", "Магазинов: 1"))
        self.assertEqual(asyncio.run(keycheck.check(ym.with_changes(token="")))["status"], "error")


if __name__ == "__main__":
    unittest.main()
