import calendar
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.services.planfact import TOTAL, PlanFact, status
from app.storage import DraftStore

PER_DAY = {"A-1": {"orders": 10, "orders_sum": 10000, "revenue": 5000, "ads": 500, "profit": 1000},
           "B-2": {"orders": 2, "orders_sum": 3000, "revenue": 1500, "ads": 600, "profit": -100}}


class FakeRNP:
    def __init__(self):
        self.saved = {}

    def table(self, start, end, brand=None, category=None, articles=None):
        a, b = date.fromisoformat(start), date.fromisoformat(end)
        n = (b - a).days + 1
        rows, total, by_day = [], {k: 0.0 for k in PER_DAY["A-1"]}, {}
        for key, v in PER_DAY.items():
            r = {k: x * n for k, x in v.items()}
            r.update({"key": key, "article": key.lower(), "brand": "Br", "category": "Джинсы", "nm_id": 1,
                      "margin": r["profit"] / r["revenue"] * 100, "drr": r["ads"] / r["orders_sum"] * 100})
            rows.append(r)
            for k in total:
                total[k] += r[k]
        d = a
        while d <= b:
            by_day[d.isoformat()] = {k: sum(v[k] for v in PER_DAY.values()) for k in total}
            d += timedelta(days=1)
        total.update({"margin": total["profit"] / total["revenue"] * 100, "drr": total["ads"] / total["orders_sum"] * 100})
        return {"articles": rows, "total": total, "by_day": by_day, "sources": {}, "synced_at": "", "state": {}}

    def plans(self):
        return self.saved

    def set_plan(self, article, month, orders_per_day=None, margin=None):
        self.saved[(article, month)] = {"orders_per_day": orders_per_day, "margin": margin}


class PlanFactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DraftStore(f"sqlite:///{Path(self.temp.name) / 'db.sqlite3'}")
        self.rnp = FakeRNP()
        self.pf = PlanFact(self.store, self.rnp)
        self.month = datetime.now(timezone(timedelta(hours=3))).strftime("%Y-%m")
        self.days = calendar.monthrange(*map(int, self.month.split("-")))[1]

    def test_status(self):
        self.assertEqual(status("orders", 100, 105), "good")
        self.assertEqual(status("orders", 100, 92), "warn")
        self.assertEqual(status("orders", 100, 80), "bad")
        self.assertEqual(status("ads", 1000, 1050), "warn")
        self.assertEqual(status("ads", 1000, 1300), "bad")
        self.assertEqual(status("drr", 10, 9), "good")
        self.assertEqual(status("margin", 20, 17), "bad")

    def test_report_and_plans(self):
        d = self.days
        self.pf.set_plan(self.month, "", "orders", 12 * d)                  # итог: 12 в день, факт 12 → 100%
        self.pf.set_plan(self.month, "a-1", "orders", 12 * d)               # артикул: 12 в день, факт 10 → 83% (плохо)
        self.pf.set_plan(self.month, "b-2", "profit", 0)
        self.pf.set_plan(self.month, "", "ads", 30000)
        self.pf.set_plan(self.month, "", "margin", 20)
        self.assertEqual(self.rnp.saved[("A-1", self.month)]["orders_per_day"], 12.0)   # план заказов ушёл и в РНП
        r = self.pf.report(self.month)
        t = r["total"]
        self.assertEqual(t["orders"]["forecast"], 12 * d)
        self.assertEqual(t["orders"]["forecast_pct"], 100.0)
        self.assertEqual(t["orders"]["status"], "good")
        self.assertEqual(t["orders"]["plan_source"], "set")
        self.assertEqual(t["ads"]["forecast"], 1100 * d)
        self.assertEqual(t["ads"]["status"], "bad" if 1100 * d > 33000 else "warn" if 1100 * d > 30000 else "good")
        self.assertEqual(t["margin"]["forecast"], round(900 / 6500 * 100, 2))
        self.assertEqual(t["margin"]["status"], "bad")
        self.assertEqual(t["profit"]["plan"], 0)                               # сумма планов артикулов
        self.assertEqual(t["profit"]["plan_source"], "articles")
        arts = {a["key"]: a for a in r["articles"]}
        self.assertEqual(arts["A-1"]["metrics"]["orders"]["status"], "bad")
        self.assertEqual(arts["B-2"]["metrics"]["profit"]["status"], "bad")
        self.assertTrue(any(a["scope"] == "A-1" for a in r["alerts"]))
        self.assertEqual(len(r["daily"]), d)
        self.pf.set_plan(self.month, "a-1", "orders", None)
        self.assertIsNone(self.rnp.saved[("A-1", self.month)]["orders_per_day"])
        self.assertNotIn("orders", self.pf.plans(self.month).get("A-1", {}))

    def test_autofill(self):
        res = self.pf.autofill(self.month, growth=10)
        self.assertGreater(res["written"], 0)
        p = self.pf.plans(self.month)
        y, m = map(int, self.month.split("-"))
        prev_days = (date(y, m, 1) - timedelta(days=1)).day
        self.assertAlmostEqual(p[TOTAL]["orders"], round(12 * prev_days * (self.days / prev_days) * 1.1), delta=1)
        self.assertAlmostEqual(p[TOTAL]["ads"], round(1100 * self.days), delta=1)          # реклама без роста
        self.assertIn("A-1", p)
        again = self.pf.autofill(self.month, growth=50)                                     # без перезаписи
        self.assertEqual(again["written"], 0)

    def test_digest(self):
        self.assertIsNone(self.pf.digest())
        self.pf.set_plan(self.month, "", "orders", 100)
        subject, text = self.pf.digest()
        self.assertIn("Заказы", text)


if __name__ == "__main__":
    unittest.main()
