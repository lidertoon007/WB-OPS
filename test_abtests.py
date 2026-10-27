import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import httpx

from app.services import abtests as ab
from app.services.abtests import ABError, ABTests, z_better
from app.storage import DraftStore

MSK = timezone(timedelta(hours=3))


class FakeWB:
    """Карточка 111 с тремя фото; реклама даёт 2000 показов за снимок, CTR зависит от главного фото."""
    CTR = {"https://wb/p1.jpg": 0.02, "https://wb/p2.jpg": 0.05, "https://wb/p3.jpg": 0.021}

    def __init__(self):
        self.photos = ["https://wb/p1.jpg", "https://wb/p2.jpg", "https://wb/p3.jpg"]
        self.video = "https://wb/v.mp4"
        self.cum = {"views": 0.0, "clicks": 0.0}
        self.saves = []

    def __call__(self, request):
        p = request.url.path
        if p == "/content/v2/get/cards/list":
            return httpx.Response(200, json={"cards": [{"nmID": 111, "vendorCode": "3140-D1", "title": "Джинсы", "video": self.video,
                                                        "photos": [{"big": u} for u in self.photos]}]})
        if p == "/content/v3/media/save":
            body = json.loads(request.content)
            self.saves.append(body["data"])
            self.photos = [u for u in body["data"] if not u.endswith(".mp4")]
            return httpx.Response(200, json={})
        if p == "/content/v3/media/file":
            self.photos.append(f"https://wb/up{len(self.photos) + 1}.jpg")
            self.CTR[self.photos[-1]] = 0.03
            return httpx.Response(200, json={})
        if p == "/adv/v3/fullstats":
            main = self.photos[0]
            self.cum["views"] += 2000
            self.cum["clicks"] += 2000 * self.CTR.get(main, 0.02)
            today = datetime.now(MSK).date().isoformat()
            return httpx.Response(200, json=[{"advertId": 77, "days": [{"date": today, "apps": [{"appType": 1, "nms": [
                {"nmId": 111, "views": self.cum["views"], "clicks": self.cum["clicks"], "atbs": self.cum["clicks"] / 4, "orders": 0, "sum": 0, "sum_price": 0}]}]}]}])
        return httpx.Response(404)


class ABTestsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DraftStore(f"sqlite:///{Path(self.temp.name) / 'db.sqlite3'}")
        self.wb = FakeWB()
        real = httpx.Client
        for p in (mock.patch.object(ab.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(self.wb), timeout=5)),
                  mock.patch.object(ab.time, "sleep", lambda *_: None)):
            p.start()
            self.addCleanup(p.stop)
        self.ab = ABTests(self.store, lambda: [("ИП", "tok-12345678", "ads-12345678")], Path(self.temp.name) / "ab")

    def age(self, tid, hours):
        with self.store._connect() as c:
            past = (datetime.now(MSK) - timedelta(hours=hours)).isoformat(timespec="seconds")
            c.execute("UPDATE ab_tests SET switched_at=? WHERE id=?", (past, tid))

    def test_z(self):
        self.assertGreater(z_better({"views": 5000, "clicks": 250}, {"views": 5000, "clicks": 100}, "ctr"), 1.645)
        self.assertIsNone(z_better({"views": 0, "clicks": 0}, {"views": 10, "clicks": 1}, "ctr"))

    def test_full_cycle(self):
        t = self.ab.create(111, 77, [1, 2], mode="timer", interval_hours=2)
        self.assertEqual([v["label"] for v in t["variants"]][0], "Текущее главное (контроль)")
        t = self.ab.add_upload(t["id"], "new.jpg", b"\xff\xd8fake")
        self.assertEqual(len(t["variants"]), 4)
        with self.assertRaises(ABError):
            self.ab.add_upload(t["id"], "x.gif", b"1")
        t = self.ab.start(t["id"])
        tid = t["id"]
        self.assertEqual(t["status"], "running")
        self.assertIn("https://wb/up4.jpg", self.wb.photos)                    # своё фото загружено в конец карточки
        self.assertTrue(all(v["url"] for v in t["variants"]))
        # несколько циклов: снимок → смена
        for _ in range(16):
            self.age(tid, 3)
            self.ab.tick(lambda _a: 9)
            if self.ab.detail(tid)["status"] == "finished":
                break
        d = self.ab.detail(tid)
        self.assertEqual(d["status"], "finished")
        win = next(v for v in d["variants"] if v["is_winner"])
        self.assertEqual(win["url"], "https://wb/p2.jpg")                        # лучший CTR
        self.assertEqual(self.wb.photos[0], "https://wb/p2.jpg")                  # победитель — главное фото
        self.assertNotIn("https://wb/up4.jpg", self.wb.photos)                   # загруженный проигравший убран
        self.assertTrue(all(s[-1] == "https://wb/v.mp4" for s in self.wb.saves))  # видео не потерялось
        self.assertTrue(any(v["excluded"] for v in d["variants"]))
        self.assertGreater(d["summary"]["lift"], 50)

    def test_campaign_pause(self):
        t = self.ab.create(111, 77, [1])
        tid = self.ab.start(t["id"])["id"]
        self.ab.tick(lambda _a: 11)
        d = self.ab.detail(tid)
        self.assertEqual(d["status"], "auto_paused")
        self.assertIn("кампания", d["pause_reason"].lower())
        self.ab.tick(lambda _a: 9)
        self.assertEqual(self.ab.detail(tid)["status"], "running")

    def test_manual_finish_without_data(self):
        t = self.ab.create(111, 77, [2])
        tid = self.ab.start(t["id"])["id"]
        d = self.ab.finish(tid, winner_id=t["variants"][1]["id"])
        self.assertEqual(self.wb.photos[0], "https://wb/p3.jpg")
        self.assertEqual(d["status"], "finished")
        self.ab.delete(tid)
        self.assertEqual(self.ab.list(), [])


if __name__ == "__main__":
    unittest.main()
