"""Догрузка отчётов реализации WB к кэшу (opiu/Wildberries/wb_finance.py: closed_weeks, load_account)."""
import io
import gzip
import json
import sys
import tempfile
import tracemalloc
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

from app.services import finance

sys.path.insert(0, str(finance.ENGINE_DIR / "Wildberries"))
import wb_finance as w  # noqa: E402

D_FROM = date(2026, 1, 1)
ACC = {"name": "Кабинет", "token": "t"}


def report(rid, d_from, d_to):
    return {"reportId": rid, "dateFrom": d_from, "dateTo": d_to, "bankPaymentSum": 0}


def row(rrd, rid, day):
    return {"rrdId": rrd, "reportId": rid, "rrDate": day}


WEEK1, WEEK2 = report(1, "2026-10-12", "2026-10-18"), report(2, "2026-10-19", "2026-10-25")
DAY = report(250001, "2026-10-26", "2026-10-26")
CACHE = {"rows": [row(10, 1, "2026-10-13"), row(20, 2, "2026-10-20"), row(30, 250001, "2026-10-26")],
         "reports": [WEEK1, WEEK2, DAY], "daily": ["2026-10-26", "2026-10-26"]}


class ClosedWeeksTests(unittest.TestCase):
    def test_keeps_weekly_and_drops_daily(self):
        rows, reports = w.closed_weeks(CACHE, date(2026, 10, 27))
        self.assertEqual([r["reportId"] for r in reports], [1, 2])
        self.assertEqual([r["rrdId"] for r in rows], [10, 20])

    def test_daily_ids_from_new_cache(self):
        data = {**CACHE, "daily": None, "daily_ids": [250001]}
        self.assertEqual([r["reportId"] for r in w.closed_weeks(data, date(2026, 10, 27))[1]], [1, 2])

    def test_reports_after_period_end_are_not_kept(self):
        self.assertEqual([r["reportId"] for r in w.closed_weeks(CACHE, date(2026, 10, 20))[1]], [1])

    def test_nothing_to_continue(self):
        self.assertIsNone(w.closed_weeks({"rows": [row(1, 1, "2026-10-13")], "reports": []}, date(2026, 10, 27)))


class StreamingCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "cache.json"

    def test_roundtrip_unicode_and_chunk_boundaries(self):
        rows = [dict(row(1, 1, "2026-10-13"), text='Кавычки " / \\ / \n 😀' * 8000),
                row(2, 2, "2026-10-20")]
        w.write_cache(self.path, rows, [WEEK1, WEEK2], None, [])
        data = w.read_cache(self.path)
        self.assertEqual(list(data["rows"]), rows)
        self.assertEqual(list(data["rows"]), rows)  # aggregation/cache/CSV each iterate independently
        self.assertEqual(data["reports"], [WEEK1, WEEK2])
        self.assertIsNone(data["daily"])

    def test_rejects_truncated_or_trailing_cache(self):
        for text in ('{"rows":[{"rrdId":1}', '{"rows":[]} unexpected'):
            self.path.write_text(text, encoding="utf-8")
            with self.assertRaises(ValueError):
                w.read_cache(self.path)

    def test_large_cache_peak_memory_is_bounded(self):
        # 40 MiB of raw rows must not become a full Unicode string/list of dicts.
        count = 20000
        w.write_cache(self.path, (dict(row(i, 1, "2026-10-13"), payload="я" * 1000)
                                 for i in range(count)), [WEEK1], None, [])
        self.assertGreater(self.path.stat().st_size, 40_000_000)
        tracemalloc.start()
        try:
            data = w.read_cache(self.path)
            selected, reports = w.closed_weeks(data, date(2026, 10, 27))
            self.assertEqual(len(selected), count)
            self.assertEqual(sum(1 for _ in selected), count)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 4 * 1024 * 1024, f"streaming cache peak={peak} bytes")
        self.assertEqual(reports, [WEEK1])

    def test_api_dedup_preserves_last_value_and_original_order(self):
        class Client:
            log = staticmethod(lambda _: None)
            def post(self, _):
                return [row(1, 1, "2026-10-13"), row(2, 1, "2026-10-14"),
                        row(1, 1, "2026-10-15")]
        result = w.fetch_rows(Client(), D_FROM, date(2026, 10, 27))
        self.assertEqual([(r["rrdId"], r["rrDate"]) for r in result],
                         [(1, "2026-10-15"), (2, "2026-10-14")])

    def test_http_finance_pages_stream_both_plain_and_gzip(self):
        expected = [dict(row(i, 1, "2026-10-13"), text="Кавычки \\\" 😀" * 100)
                    for i in range(300)]
        raw = json.dumps(expected, ensure_ascii=False).encode("utf-8")
        class Response(io.BytesIO):
            status = 200
        for compressed in (False, True):
            response = Response(gzip.compress(raw) if compressed else raw)
            response.headers = {"Content-Encoding": "gzip" if compressed else ""}
            with mock.patch.object(w.urllib.request, "urlopen", return_value=response):
                actual = w.WBClient("fake").post({})
            self.assertIsInstance(actual, w.DiskRows)
            self.assertEqual(list(actual), expected)

    def test_disk_rows_temp_database_is_removed_when_released(self):
        rows = w.DiskRows([row(1, 1, "2026-10-13")])
        folder = Path(rows._temp.name)
        self.assertTrue(folder.exists())
        self.assertEqual(len(list(rows)), 1)
        del rows
        self.assertFalse(folder.exists())


class LoadAccountTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cache = Path(temp.name)
        (self.cache / f"Кабинет_{D_FROM}_2026-10-27.json").write_text(json.dumps(CACHE), encoding="utf-8")
        self.calls = []

    def fake_rows(self, client, d_from, d_to, period="weekly"):
        self.calls.append(("rows", period, d_from, d_to))
        if period == "daily":
            return [row(40, 250002, "2026-10-27")]
        return [row(20, 2, "2026-10-20"), row(50, 3, "2026-10-27")]   # повтор старой недели + новая

    def fake_reports(self, client, d_from, d_to, period="weekly"):
        self.calls.append(("reports", period, d_from, d_to))
        if period == "daily":
            return [report(250002, "2026-10-27", "2026-10-27")]
        return [report(2, "2026-10-19", "2026-10-25"), report(3, "2026-10-26", "2026-11-01")]

    def load(self, d_to, reload=False):
        with mock.patch.object(w, "fetch_rows", self.fake_rows), mock.patch.object(w, "fetch_reports", self.fake_reports), \
                redirect_stdout(io.StringIO()):
            return w.load_account(ACC, D_FROM, d_to, self.cache, False, reload)

    def test_fetches_only_weeks_after_cache(self):
        rows, reports, daily = self.load(date(2026, 11, 2))
        weekly_calls = [c for c in self.calls if c[1] == "weekly"]
        self.assertEqual({c[2] for c in weekly_calls}, {date(2026, 10, 26)})        # не с 01.01
        self.assertEqual(sorted(r["reportId"] for r in reports), [1, 2, 3, 250002])  # без дублей и без старого дня
        self.assertEqual(sorted(r["rrdId"] for r in rows), [10, 20, 40, 50])
        saved = json.loads((self.cache / f"Кабинет_{D_FROM}_2026-11-02.json").read_text(encoding="utf-8"))
        self.assertEqual(len(saved["rows"]), 4)                                     # + ежедневный хвост 02.11
        self.assertFalse((self.cache / f"Кабинет_{D_FROM}_2026-10-27.json").exists())  # старый кэш заменён
        self.assertEqual(daily, ["2026-10-27", "2026-10-27"])

    def test_only_daily_tail_when_no_new_week(self):
        cache = dict(CACHE, reports=[WEEK1, WEEK2])
        (self.cache / f"Кабинет_{D_FROM}_2026-10-27.json").write_text(json.dumps(cache), encoding="utf-8")
        self.fake_rows = lambda client, a, b, period="weekly": (self.calls.append(("rows", period, a, b))
                                                                  or ([row(40, 250002, "2026-10-27")]
                                                                      if period == "daily" else []))
        self.fake_reports = lambda client, a, b, period="weekly": (self.calls.append(("reports", period, a, b))
                                                                     or ([report(250002, "2026-10-27", "2026-10-27")]
                                                                         if period == "daily" else []))
        rows, reports, daily = self.load(date(2026, 10, 27))
        self.assertEqual({c[1] for c in self.calls}, {"daily"})    # неделя 26.10 не закрыта — о ней не спрашиваем
        self.assertEqual(daily, ["2026-10-27", "2026-10-27"])
        self.assertEqual(sorted(r["reportId"] for r in reports), [1, 2, 250002])
        saved = json.loads((self.cache / f"Кабинет_{D_FROM}_2026-10-27.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["daily_ids"], [250002])

    def test_reload_fetches_whole_period(self):
        self.load(date(2026, 10, 28), reload=True)
        self.assertIn(("rows", "weekly", D_FROM, date(2026, 10, 28)), self.calls)

    def test_from_cache_uses_disk_rows_and_preserves_daily(self):
        rows, reports, daily = w.load_account(ACC, D_FROM, date(2026, 10, 27), self.cache, True)
        self.assertIsInstance(rows, w.DiskRows)
        self.assertEqual(list(rows), CACHE["rows"])
        self.assertEqual(reports, CACHE["reports"])
        self.assertEqual(daily, CACHE["daily"])


if __name__ == "__main__":
    unittest.main()
