"""Баллы программы лояльности WB в «Итого к оплате» (opiu/Wildberries/wb_finance.py)."""
import sys
import unittest

from app.services import finance

sys.path.insert(0, str(finance.ENGINE_DIR / "Wildberries"))
import wb_finance as w  # noqa: E402


def net(row):
    return round(sum(p[2] for p in w.split_row(row) if p[0] != w.S_INFO), 2)


class LoyaltyReturnTests(unittest.TestCase):
    # Строки из отчёта WB за 05–11.10.2026: продажа и возврат по одному заказу
    def test_sale_withholds_points(self):
        self.assertEqual(net({"docTypeName": "Продажа", "cashbackAmount": 301}), -301)
        self.assertEqual(net({"docTypeName": "Продажа", "cashbackCommissionChange": 30.1}), -30.1)

    def test_return_gives_points_back(self):
        self.assertEqual(net({"docTypeName": "Возврат", "cashbackAmount": 301}), 301)
        self.assertEqual(net({"docTypeName": "Возврат", "cashbackCommissionChange": 30.1}), 30.1)

    def test_review_points_keep_own_sign(self):
        # «Баллы за отзывы» приходят без вида документа, знак уже в сумме
        self.assertEqual(net({"docTypeName": "", "cashbackAmount": 200}), -200)
        self.assertEqual(net({"docTypeName": "", "cashbackAmount": -200}), 200)

    def test_sale_and_return_cancel_out(self):
        rows = [{"reportId": 1, "rrDate": "2026-10-09", "docTypeName": d, "cashbackAmount": 301}
                for d in ("Продажа", "Возврат")]
        _table, _ops, report_net, *_ = w.aggregate(rows, {"2026-10"})
        self.assertAlmostEqual(report_net[1], 0.0)


if __name__ == "__main__":
    unittest.main()
