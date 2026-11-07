"""A/B-тесты главного фото карточки Wildberries (ручной режим: свои фото или фото из карточки).

Как работает:
  • вариант — это фото, которое по очереди становится главным (первым) в карточке. Остальные фото карточки
    остаются на своих местах. Свои фото при запуске загружаются в конец карточки (/content/v3/media/file),
    дальше смена главного — это перестановка ссылок (/content/v3/media/save);
  • трафик — показы привязанной рекламной кампании: снимки накопительной статистики /adv/v3/fullstats
    по артикулу, прирост между снимками засчитывается варианту, который стоял главным;
  • смена — по таймеру (часы) или по числу показов; варианты чередуются, у кого меньше показов — тот следующий;
  • вердикт — статистический: вариант исключается, когда с уверенностью ≥95 % видно, что он хуже лидера
    (z-тест долей по выбранной метрике: CTR или «CTR × конверсия в корзину» = корзины / показы);
  • кампания остановилась — тест встаёт на паузу и продолжает с того же места, когда кампания снова активна;
  • в конце победитель становится главным фото, загруженные проигравшие можно убрать из карточки.
"""
from __future__ import annotations

import json
import math
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

CONTENT = "https://content-api.wildberries.ru"
ADV = "https://advert-api.wildberries.ru"
MSK = timezone(timedelta(hours=3))
MIN_INTERVAL_H = 2          # статистика WB обновляется с задержкой — чаще менять фото бессмысленно
MIN_VIEWS_STEP = 100
MIN_VIEWS_VERDICT = 1000    # показов у варианта, прежде чем его можно исключить
RETRO_DAYS = 30             # окно ретро-статистики для текущего главного фото
Z95 = 1.645                 # односторонний 95 %
FIELDS = ("views", "clicks", "atbs", "orders", "spend", "sum_price")
DEFAULT_CLUSTERS = ("Джинсы мужские классические", "Джинсы мужские прямые", "Джинсы мужские прямые классические")


class ABError(Exception):
    pass


def now_iso() -> str:
    return datetime.now(MSK).isoformat(timespec="seconds")


def metric_rate(v: dict, metric: str) -> float | None:
    views = v.get("views") or 0
    if not views:
        return None
    if metric == "cart":
        value = v.get("atbs") or 0
    elif metric == "order_rate":
        value = v.get("orders") or 0
    elif metric == "revenue_rate":
        value = v.get("sum_price") or 0
    elif metric == "profit_rate":
        value = (v.get("sum_price") or 0) - (v.get("spend") or 0)
    else:
        value = v.get("clicks") or 0
    return value / views


def norm_cluster(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def z_better(a: dict, b: dict, metric: str) -> float | None:
    """z-статистика гипотезы «a лучше b» по доле на показ."""
    if metric in ("order_rate", "revenue_rate", "profit_rate"):
        return None
    na, nb = a.get("views") or 0, b.get("views") or 0
    if not na or not nb:
        return None
    xa = (a.get("atbs") if metric == "cart" else a.get("clicks")) or 0
    xb = (b.get("atbs") if metric == "cart" else b.get("clicks")) or 0
    p = (xa + xb) / (na + nb)
    se = math.sqrt(p * (1 - p) * (1 / na + 1 / nb)) if 0 < p < 1 else 0
    return (xa / na - xb / nb) / se if se else None


class ABTests:
    def __init__(self, store, tokens, files_dir: Path):
        self.store = store
        self.tokens = tokens            # callable -> [(кабинет, ключ, ключ рекламы)]
        self.files_dir = Path(files_dir)
        self._lock = threading.Lock()
        self._last = {}
        with store._connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS ab_tests (id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT, nm_id INTEGER, article TEXT, title TEXT,
                advert_id INTEGER, status TEXT, mode TEXT, interval_hours REAL, views_per_step REAL, metric TEXT, max_days REAL,
                original TEXT, video TEXT, current_variant INTEGER, switched_at TEXT, views_at_switch REAL, snapshot TEXT,
                created_at TEXT, started_at TEXT, finished_at TEXT, winner INTEGER, pause_reason TEXT, note TEXT, clusters TEXT,
                cluster_snapshot TEXT);
            CREATE TABLE IF NOT EXISTS ab_variants (id INTEGER PRIMARY KEY AUTOINCREMENT, test_id INTEGER, url TEXT, source TEXT, local_file TEXT,
                label TEXT, position INTEGER, excluded INTEGER DEFAULT 0, excluded_reason TEXT, uploaded INTEGER DEFAULT 0,
                views REAL DEFAULT 0, clicks REAL DEFAULT 0, atbs REAL DEFAULT 0, orders REAL DEFAULT 0, spend REAL DEFAULT 0, sum_price REAL DEFAULT 0,
                shown_seconds REAL DEFAULT 0, turns INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS ab_log (id INTEGER PRIMARY KEY AUTOINCREMENT, test_id INTEGER, at TEXT, kind TEXT, text TEXT, variant_id INTEGER);
            CREATE TABLE IF NOT EXISTS ab_variant_clusters (id INTEGER PRIMARY KEY AUTOINCREMENT, test_id INTEGER NOT NULL, variant_id INTEGER NOT NULL,
                cluster TEXT NOT NULL, views REAL DEFAULT 0, clicks REAL DEFAULT 0, atbs REAL DEFAULT 0, orders REAL DEFAULT 0,
                spend REAL DEFAULT 0, sum_price REAL DEFAULT 0, UNIQUE(test_id, variant_id, cluster));
            """)
            try:
                c.execute("ALTER TABLE ab_tests ADD COLUMN clusters TEXT")
            except Exception:
                pass
            try:
                c.execute("ALTER TABLE ab_tests ADD COLUMN cluster_snapshot TEXT")
            except Exception:
                pass

    # ------------------------------------------------------------------ WB
    def _req(self, client, method, url, token, key, interval, **kw):
        for attempt in range(4):
            last = self._last.get(key, 0)
            pause = interval - (time.monotonic() - last)
            if last and pause > 0:
                time.sleep(pause)
            self._last[key] = time.monotonic()
            try:
                r = client.request(method, url, headers={"Authorization": token}, **kw)
            except httpx.HTTPError as exc:
                if attempt < 3:
                    time.sleep(3)
                    continue
                raise ABError(f"нет связи с WB ({type(exc).__name__})")
            if r.status_code == 429 and attempt < 3:
                time.sleep(float(r.headers.get("X-Ratelimit-Retry") or 20))
                continue
            if r.status_code in (500, 502, 503, 504) and attempt < 3:
                time.sleep(5)
                continue
            if r.status_code in (401, 403):
                raise ABError("у ключа нет доступа (нужны категории «Контент» и «Продвижение» с правом записи)")
            if r.status_code >= 400:
                raise ABError(f"WB ответил {r.status_code}: {r.text[:200]}")
            return r.json() if r.content.strip() else {}
        raise ABError("WB ограничил частоту запросов — попробуйте позже")

    def _token(self, account: str) -> tuple[str, str]:
        for name, token, ads in self.tokens():
            if name == account:
                return token, ads or token
        raise ABError(f"Нет ключа API кабинета «{account}»")

    def card(self, nm_id: int, account: str | None = None) -> dict:
        """Карточка: фото по порядку, видео, название. Ищет по всем кабинетам, если кабинет не указан."""
        accounts = [(account, *self._token(account))] if account else [(n, t, a) for n, t, a in self.tokens()]
        with httpx.Client(timeout=60) as client:
            for name, token, _ads in accounts:
                body = {"settings": {"filter": {"textSearch": str(nm_id), "withPhoto": -1}, "cursor": {"limit": 10}}}
                try:
                    d = self._req(client, "POST", CONTENT + "/content/v2/get/cards/list?locale=ru", token, f"cards:{token[-8:]}", 0.7, json=body)
                except ABError:
                    continue
                for c in d.get("cards") or []:
                    if int(c.get("nmID") or 0) == int(nm_id):
                        video = c.get("video")
                        return {"account": name, "nm_id": nm_id, "article": str(c.get("vendorCode") or ""), "title": c.get("title") or "",
                                "subject": c.get("subjectName") or "", "photos": [p.get("big") for p in c.get("photos") or [] if p.get("big")],
                                "thumbs": [p.get("c246x328") or p.get("tm") or p.get("big") for p in c.get("photos") or [] if p.get("big")],
                                "video": video if isinstance(video, str) else ""}
        raise ABError("Карточка не найдена ни в одном кабинете WB")

    def _save_media(self, client, token, nm_id, photos: list, video: str = ""):
        data = list(photos) + ([video] if video else [])
        self._req(client, "POST", CONTENT + "/content/v3/media/save", token, f"media:{token[-8:]}", 1.0, json={"nmId": int(nm_id), "data": data})

    def _upload_file(self, client, token, nm_id, number: int, path: Path):
        ext = path.suffix.lower()
        mime = "image/png" if ext == ".png" else "image/webp" if ext == ".webp" else "image/jpeg"
        key = f"media:{token[-8:]}"
        r = None
        for attempt in range(4):
            last = self._last.get(key, 0)
            pause = 1.0 - (time.monotonic() - last)
            if last and pause > 0:
                time.sleep(pause)
            self._last[key] = time.monotonic()
            with open(path, "rb") as fh:
                r = client.post(CONTENT + "/content/v3/media/file", files={"uploadfile": (path.name, fh, mime)},
                                headers={"Authorization": token, "X-Nm-Id": str(nm_id), "X-Photo-Number": str(number)})
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(10)
                continue
            break
        if r is None or r.status_code >= 400:
            raise ABError(f"WB не принял фото: {(r.text if r is not None else '')[:200]}")

    def _stats(self, client, token, advert_id, nm_id, since: date) -> dict:
        """Накопительная статистика артикула в кампании по дням: {день: {views, clicks, ...}}."""
        d = self._req(client, "GET", ADV + "/adv/v3/fullstats", token, f"stats:{token[-8:]}", 20.5,
                      params={"ids": str(advert_id), "beginDate": since.isoformat(), "endDate": datetime.now(MSK).date().isoformat()}) or []
        out = {}
        for adv in d if isinstance(d, list) else []:
            for day in adv.get("days") or []:
                key = str(day.get("date") or "")[:10]
                acc = out.setdefault(key, {k: 0.0 for k in FIELDS})
                for app in day.get("apps") or []:
                    for nm in app.get("nms") or []:
                        if int(nm.get("nmId") or 0) == int(nm_id):
                            for k, src in (("views", "views"), ("clicks", "clicks"), ("atbs", "atbs"), ("orders", "orders"), ("spend", "sum"), ("sum_price", "sum_price")):
                                acc[k] += float(nm.get(src) or 0)
        return out

    def _cluster_stats(self, client, token, advert_id, nm_id, since: date) -> dict:
        """Статистика рекламных поисковых кластеров по дням.

        Ключом результата является нормализованное имя кластера. Это позволяет
        сопоставить введённые продавцом фразы с регистром и пробелами в ответе WB.
        """
        out = self._stored_cluster_stats(advert_id, nm_id, since)
        today = datetime.now(MSK).date().isoformat()
        stored_days = [day for days in out.values() for day in days]
        # The hourly ads sync already stores this endpoint's data. Use it first,
        # and ask WB directly only when today's data is missing.
        if stored_days:
            return out
        body = {"from": since.isoformat(), "to": today,
                "items": [{"advertId": int(advert_id), "nmId": int(nm_id)}]}
        try:
            d = self._req(client, "POST", ADV + "/adv/v1/normquery/stats", token,
                          f"cluster-stats:{token[-8:]}", 6.0, json=body) or {}
        except ABError:
            return out
        items = d.get("items") if isinstance(d, dict) else []
        remote: dict[str, dict[str, dict[str, float]]] = {}
        for item in items or []:
            for day in item.get("dailyStats") or item.get("stats") or []:
                stat = day.get("stat") or day
                cluster = norm_cluster(stat.get("normQuery") or stat.get("norm_query"))
                day_key = str(day.get("date") or stat.get("date") or "")[:10]
                if not cluster or not day_key:
                    continue
                acc = remote.setdefault(cluster, {}).setdefault(day_key, {k: 0.0 for k in FIELDS})
                for key, aliases in {
                    "views": ("views",), "clicks": ("clicks",), "atbs": ("atbs",),
                    "orders": ("orders",), "spend": ("spend", "sum"), "sum_price": ("sum_price", "sumPrice"),
                }.items():
                    for alias in aliases:
                        if alias in stat:
                            acc[key] += float(stat.get(alias) or 0)
                            break
        for cluster, days in remote.items():
            for day, values in days.items():
                existing = out.setdefault(cluster, {}).setdefault(day, {k: 0.0 for k in FIELDS})
                for field in FIELDS:
                    # WB's daily values are cumulative for the day; merging by
                    # max prevents the stored sync and direct response doubling.
                    existing[field] = max(existing[field], values[field])
        return out

    def _stored_cluster_stats(self, advert_id: int, nm_id: int, since: date) -> dict:
        out: dict[str, dict[str, dict[str, float]]] = {}
        try:
            with self.store._connect() as c:
                rows = c.execute("SELECT day, query, views, clicks, atbs, orders, spend FROM ads_clusters "
                                 "WHERE advert_id=? AND nm_id=? AND day>=?", (advert_id, nm_id, since.isoformat())).fetchall()
        except Exception:
            return out
        for day, query, views, clicks, atbs, orders, spend in rows:
            cluster = norm_cluster(query)
            if not cluster:
                continue
            out.setdefault(cluster, {})[str(day)[:10]] = {
                "views": float(views or 0), "clicks": float(clicks or 0), "atbs": float(atbs or 0),
                "orders": float(orders or 0), "spend": float(spend or 0), "sum_price": 0.0,
            }
        return out

    def _cluster_snapshot_totals(self, snapshot: dict, clusters: list[str]) -> dict[str, dict[str, float]]:
        """Суммирует дневной snapshot по выбранным кластерам для ретро-базы."""
        totals = {norm_cluster(cluster): {field: 0.0 for field in FIELDS} for cluster in clusters}
        for key in totals:
            for values in (snapshot.get(key) or {}).values():
                for field in FIELDS:
                    totals[key][field] += float(values.get(field) or 0)
        return totals

    # ------------------------------------------------------------------ данные
    def _test(self, c, test_id) -> dict:
        r = c.execute("SELECT * FROM ab_tests WHERE id=?", (test_id,)).fetchone()
        if not r:
            raise ABError("Тест не найден")
        cols = [d[0] for d in c.execute("SELECT * FROM ab_tests LIMIT 0").description]
        t = dict(zip(cols, r))
        t["original"] = json.loads(t["original"] or "[]")
        t["snapshot"] = json.loads(t["snapshot"] or "{}")
        t["clusters"] = json.loads(t.get("clusters") or "[]")
        t["cluster_snapshot"] = json.loads(t.get("cluster_snapshot") or "{}")
        return t

    def _variants(self, c, test_id) -> list:
        cols = [d[0] for d in c.execute("SELECT * FROM ab_variants LIMIT 0").description]
        return [dict(zip(cols, r)) for r in c.execute("SELECT * FROM ab_variants WHERE test_id=? ORDER BY position, id", (test_id,))]

    def log(self, c, test_id, kind, text, variant_id=None):
        c.execute("INSERT INTO ab_log(test_id, at, kind, text, variant_id) VALUES(?,?,?,?,?)", (test_id, now_iso(), kind, text, variant_id))

    def create(self, nm_id: int, advert_id: int, photo_indexes: list[int], mode: str = "timer", interval_hours: float = 6,
               views_per_step: float = 100, metric: str = "ctr", max_days: float = 14, clusters: list[str] | None = None) -> dict:
        if mode not in ("timer", "views", "clusters"):
            raise ABError("Режим: по таймеру, по показам или по кластерам")
        raw_clusters = clusters if clusters is not None else DEFAULT_CLUSTERS
        clusters = list(dict.fromkeys(str(x).strip() for x in raw_clusters if str(x).strip()))[:50]
        if not clusters:
            raise ABError("Добавьте хотя бы один поисковый кластер")
        if metric not in ("ctr", "cart", "order_rate", "revenue_rate", "profit_rate"):
            raise ABError("Метрика: CTR или CTR × конверсия в корзину")
        with self.store._connect() as c:
            busy = c.execute("SELECT id FROM ab_tests WHERE nm_id=? AND status IN ('running', 'paused', 'auto_paused')", (nm_id,)).fetchone()
        if busy:
            raise ABError(f"По этому артикулу уже идёт тест №{busy[0]} — сначала завершите его")
        card = self.card(nm_id)
        if not card["photos"]:
            raise ABError("В карточке нет фото")
        idx = sorted({i for i in photo_indexes if 0 <= i < len(card["photos"])} | {0})     # текущее главное — всегда контроль
        with self.store._connect() as c:
            cur = c.execute("INSERT INTO ab_tests(account, nm_id, article, title, advert_id, status, mode, interval_hours, views_per_step, metric, max_days, "
                            "original, video, created_at, snapshot, clusters, cluster_snapshot) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (card["account"], nm_id, card["article"], card["title"], advert_id, "draft", mode, max(MIN_INTERVAL_H, float(interval_hours)),
                             max(MIN_VIEWS_STEP, float(views_per_step)), metric, max(1.0, float(max_days)), json.dumps(card["photos"]), card["video"], now_iso(), "{}", json.dumps(clusters, ensure_ascii=False), "{}"))
            tid = cur.lastrowid
            for pos, i in enumerate(idx):
                vcur = c.execute("INSERT INTO ab_variants(test_id, url, source, label, position, uploaded) VALUES(?,?,?,?,?,1)",
                          (tid, card["photos"][i], "card", "Текущее главное (контроль)" if i == 0 else f"Фото №{i + 1} из карточки", pos))
                for cluster in clusters:
                    c.execute("INSERT INTO ab_variant_clusters(test_id, variant_id, cluster) VALUES(?,?,?)", (tid, vcur.lastrowid, cluster))
            self.log(c, tid, "create", f"Тест создан: {len(idx)} вариантов из карточки")
        return self.detail(tid)

    def add_upload(self, test_id: int, filename: str, content: bytes) -> dict:
        ext = Path(filename or "").suffix.lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp"):
            raise ABError("Нужен файл JPG, PNG или WEBP")
        if len(content) > 10 * 1024 * 1024:
            raise ABError("Файл больше 10 МБ")
        with self.store._connect() as c:
            t = self._test(c, test_id)
            if t["status"] != "draft":
                raise ABError("Свои фото добавляются до запуска теста")
            vs = self._variants(c, test_id)
            if len(vs) >= 20:
                raise ABError("Не больше 20 вариантов")
            folder = self.files_dir / str(test_id)
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"v{len(vs) + 1}_{int(time.time())}{ext}"
            path.write_bytes(content)
            vcur = c.execute("INSERT INTO ab_variants(test_id, url, source, local_file, label, position, uploaded) VALUES(?,?,?,?,?,?,0)",
                      (test_id, "", "upload", str(path), f"Своё фото: {Path(filename).name}"[:80], len(vs)))
            for cluster in (t.get("clusters") or DEFAULT_CLUSTERS):
                c.execute("INSERT INTO ab_variant_clusters(test_id, variant_id, cluster) VALUES(?,?,?)", (test_id, vcur.lastrowid, cluster))
            self.log(c, test_id, "upload", f"Добавлено своё фото {Path(filename).name}")
        return self.detail(test_id)

    def remove_variant(self, test_id: int, variant_id: int) -> dict:
        with self.store._connect() as c:
            t = self._test(c, test_id)
            if t["status"] != "draft":
                raise ABError("Удалять варианты можно до запуска; во время теста — исключите вариант")
            v = c.execute("SELECT position, local_file FROM ab_variants WHERE id=? AND test_id=?", (variant_id, test_id)).fetchone()
            if not v:
                raise ABError("Вариант не найден")
            if v[0] == 0:
                raise ABError("Контроль (текущее главное фото) убрать нельзя")
            c.execute("DELETE FROM ab_variants WHERE id=?", (variant_id,))
            c.execute("DELETE FROM ab_variant_clusters WHERE test_id=? AND variant_id=?", (test_id, variant_id))
            if v[1]:
                Path(v[1]).unlink(missing_ok=True)
        return self.detail(test_id)

    # ------------------------------------------------------------------ управление
    def start(self, test_id: int) -> dict:
        with self.store._connect() as c:
            t = self._test(c, test_id)
            vs = self._variants(c, test_id)
        if t["status"] not in ("draft", "paused"):
            raise ABError("Тест уже идёт или завершён")
        if len(vs) < 2:
            raise ABError("Нужно минимум 2 варианта")
        token, ads = self._token(t["account"])
        with httpx.Client(timeout=120) as client:
            if t["status"] == "draft":
                card = self.card(t["nm_id"], t["account"])
                pending = [v for v in vs if not v["uploaded"]]
                if pending:                                   # свои фото — в конец карточки
                    known = set(card["photos"])
                    n = len(card["photos"])
                    for i, v in enumerate(pending):
                        self._upload_file(client, token, t["nm_id"], n + i + 1, Path(v["local_file"]))
                    urls = []
                    for _ in range(12):                       # WB обрабатывает фото не сразу
                        time.sleep(10)
                        card = self.card(t["nm_id"], t["account"])
                        urls = [u for u in card["photos"] if u not in known]
                        if len(urls) >= len(pending):
                            break
                    if len(urls) < len(pending):
                        raise ABError("WB ещё обрабатывает загруженные фото — нажмите «Запустить» ещё раз через пару минут")
                    with self.store._connect() as c:
                        for v, u in zip(pending, urls):
                            c.execute("UPDATE ab_variants SET url=?, uploaded=1 WHERE id=?", (u, v["id"]))
                        c.execute("UPDATE ab_tests SET original=?, video=? WHERE id=?", (json.dumps(card["photos"]), card["video"], test_id))
                        self.log(c, test_id, "upload", f"Загружено в карточку фото: {len(urls)}")
                today = datetime.now(MSK).date()
                full_since = today
                cluster_since = today - timedelta(days=RETRO_DAYS)
                snap = self._stats(client, ads, t["advert_id"], t["nm_id"], full_since) if t["mode"] != "clusters" else {}
                cluster_snap = self._cluster_stats(client, ads, t["advert_id"], t["nm_id"], cluster_since)
                with self.store._connect() as c:
                    first = self._variants(c, test_id)[0]
                    c.execute("UPDATE ab_tests SET status='running', started_at=?, current_variant=?, switched_at=?, views_at_switch=0, snapshot=?, cluster_snapshot=?, pause_reason=NULL "
                              "WHERE id=?", (now_iso(), first["id"], now_iso(), json.dumps(snap),
                                               json.dumps(cluster_snap, ensure_ascii=False), test_id))
                    c.execute("UPDATE ab_variants SET turns=1 WHERE id=?", (first["id"],))
                    self.log(c, test_id, "start", "Тест запущен, первым главным стоит контроль", first["id"])
                    retro_complete = False
                    if t["mode"] == "clusters":
                        totals = self._cluster_snapshot_totals(cluster_snap, t["clusters"])
                        aggregate = {field: 0.0 for field in FIELDS}
                        configured_keys = {norm_cluster(cluster) for cluster in t["clusters"]}
                        for cluster in t["clusters"]:
                            values = totals.get(norm_cluster(cluster), {field: 0.0 for field in FIELDS})
                            aggregate = {field: aggregate[field] + values[field] for field in FIELDS}
                            c.execute("INSERT OR IGNORE INTO ab_variant_clusters(test_id, variant_id, cluster) VALUES(?,?,?)",
                                      (test_id, first["id"], cluster))
                            c.execute(f"UPDATE ab_variant_clusters SET {', '.join(f'{field}=?' for field in FIELDS)} WHERE test_id=? AND variant_id=? AND cluster=?",
                                      (*[values[field] for field in FIELDS], test_id, first["id"], cluster))
                        # Keep every observed cluster for the report.  Only the
                        # configured clusters participate in the test target.
                        for key, days in cluster_snap.items():
                            if key in configured_keys:
                                continue
                            values = {field: sum(float(day.get(field) or 0) for day in days.values()) for field in FIELDS}
                            if values["views"] <= 0:
                                continue
                            c.execute("INSERT OR IGNORE INTO ab_variant_clusters(test_id, variant_id, cluster) VALUES(?,?,?)",
                                      (test_id, first["id"], key))
                            c.execute(f"UPDATE ab_variant_clusters SET {', '.join(f'{field}=?' for field in FIELDS)} WHERE test_id=? AND variant_id=? AND cluster=?",
                                      (*[values[field] for field in FIELDS], test_id, first["id"], key))
                        c.execute(f"UPDATE ab_variants SET {', '.join(f'{field}=?' for field in FIELDS)} WHERE id=?",
                                  (*[aggregate[field] for field in FIELDS], first["id"]))
                        retro_views = sum(values["views"] for values in totals.values())
                        if retro_views > 0:
                            self.log(c, test_id, "retro", f"Ретро-статистика текущего главного фото использована за последние {RETRO_DAYS} дней: {retro_views:.0f} показов по кластерам", first["id"])
                            retro_complete = all(values["views"] >= float(t["views_per_step"]) for values in totals.values())
                    else:
                        # Keep the historical cluster report for timer/views
                        # tests without changing their aggregate metrics.
                        for key, days in cluster_snap.items():
                            values = {field: sum(float(day.get(field) or 0) for day in days.values()) for field in FIELDS}
                            if values["views"] <= 0:
                                continue
                            c.execute("INSERT OR IGNORE INTO ab_variant_clusters(test_id, variant_id, cluster) VALUES(?,?,?)",
                                      (test_id, first["id"], key))
                            c.execute(f"UPDATE ab_variant_clusters SET {', '.join(f'{field}=?' for field in FIELDS)} WHERE test_id=? AND variant_id=? AND cluster=?",
                                      (*[values[field] for field in FIELDS], test_id, first["id"], key))
                if t["mode"] == "clusters" and retro_complete:
                    self._switch(test_id)
                    with self.store._connect() as c:
                        self.log(c, test_id, "switch", "Первый вариант пропущен: по текущему главному уже достаточно ретро-показов, тест продолжен со следующего фото")
            else:
                with self.store._connect() as c:
                    c.execute("UPDATE ab_tests SET status='running', pause_reason=NULL, switched_at=? WHERE id=?", (now_iso(), test_id))
                    self.log(c, test_id, "resume", "Тест продолжен")
        return self.detail(test_id)

    def refresh(self, test_id: int) -> dict:
        """Принудительно подтягивает ретро и свежую статистику для открытого теста."""
        with self.store._connect() as c:
            t = self._test(c, test_id)
            variants = self._variants(c, test_id)
        if t["mode"] != "clusters":
            self._collect(test_id)
            return self.detail(test_id)
        _token, ads = self._token(t["account"])
        today = datetime.now(MSK).date()
        with httpx.Client(timeout=120) as client:
            snap = self._cluster_stats(client, ads, t["advert_id"], t["nm_id"], today - timedelta(days=RETRO_DAYS))
        first = variants[0] if variants else None
        current = next((v for v in variants if v["id"] == t["current_variant"]), None)
        with self.store._connect() as c:
            existing = c.execute("SELECT COALESCE(SUM(views),0) FROM ab_variant_clusters WHERE test_id=? AND variant_id=?",
                                 (test_id, t["current_variant"])).fetchone()[0] if current else 0
        retro_complete = False
        if first and current and current["id"] == first["id"] and not existing:
            totals = self._cluster_snapshot_totals(snap, t["clusters"])
            aggregate = {field: 0.0 for field in FIELDS}
            with self.store._connect() as c:
                for cluster in t["clusters"]:
                    values = totals.get(norm_cluster(cluster), {field: 0.0 for field in FIELDS})
                    aggregate = {field: aggregate[field] + values[field] for field in FIELDS}
                    c.execute("INSERT OR IGNORE INTO ab_variant_clusters(test_id, variant_id, cluster) VALUES(?,?,?)",
                              (test_id, first["id"], cluster))
                    c.execute(f"UPDATE ab_variant_clusters SET {', '.join(f'{field}=?' for field in FIELDS)} WHERE test_id=? AND variant_id=? AND cluster=?",
                              (*[values[field] for field in FIELDS], test_id, first["id"], cluster))
                c.execute(f"UPDATE ab_variants SET {', '.join(f'{field}=?' for field in FIELDS)} WHERE id=?",
                          (*[aggregate[field] for field in FIELDS], first["id"]))
                c.execute("UPDATE ab_tests SET snapshot='{}', cluster_snapshot=? WHERE id=?",
                          (json.dumps(snap, ensure_ascii=False), test_id))
                retro_views = sum(values["views"] for values in totals.values())
                if retro_views:
                    self.log(c, test_id, "retro", f"Ретро-статистика подтянута вручную: {retro_views:.0f} показов по кластерам", first["id"])
                retro_complete = all(values["views"] >= float(t["views_per_step"]) for values in totals.values())
        if retro_complete and t["status"] == "running":
            self._switch(test_id)
            with self.store._connect() as c:
                self.log(c, test_id, "switch", "Текущий главный закрыт по ретро-данным, тест продолжен со следующего фото")
        elif current and t["status"] == "running" and self._variant_clusters_complete(test_id, current["id"], t["clusters"], t["views_per_step"]):
            self._switch(test_id)
        elif t["status"] == "running":
            self._collect(test_id)
        return self.detail(test_id)

    def pause(self, test_id: int, reason: str = "Пауза вручную") -> dict:
        self._collect(test_id)
        with self.store._connect() as c:
            c.execute("UPDATE ab_tests SET status='paused', pause_reason=? WHERE id=? AND status='running'", (reason, test_id))
            self.log(c, test_id, "pause", reason)
        return self.detail(test_id)

    def exclude(self, test_id: int, variant_id: int, reason: str = "Исключён вручную") -> dict:
        with self.store._connect() as c:
            vs = [v for v in self._variants(c, test_id) if not v["excluded"]]
            if len(vs) <= 1:
                raise ABError("Должен остаться хотя бы один вариант")
            c.execute("UPDATE ab_variants SET excluded=1, excluded_reason=? WHERE id=? AND test_id=?", (reason, variant_id, test_id))
            self.log(c, test_id, "exclude", reason, variant_id)
            t = self._test(c, test_id)
        if t["status"] == "running" and t["current_variant"] == variant_id:
            self._switch(test_id)
        return self.detail(test_id)

    def finish(self, test_id: int, winner_id: int | None = None, remove_uploaded: bool = True) -> dict:
        with self.store._connect() as c:
            t = self._test(c, test_id)
        if t["status"] == "finished":
            raise ABError("Тест уже завершён")
        if t["status"] == "running":
            self._collect(test_id)
        with self.store._connect() as c:
            t = self._test(c, test_id)
            vs = self._variants(c, test_id)
        if t["status"] == "draft":
            with self.store._connect() as c:
                c.execute("UPDATE ab_tests SET status='finished', finished_at=? WHERE id=?", (now_iso(), test_id))
                self.log(c, test_id, "finish", "Черновик закрыт без запуска")
            return self.detail(test_id)
        alive = [v for v in vs if not v["excluded"]] or vs
        win = next((v for v in vs if v["id"] == winner_id), None) or max(alive, key=lambda v: (metric_rate(v, t["metric"]) or 0, v["views"]))
        token, _ads = self._token(t["account"])
        card = self.card(t["nm_id"], t["account"])
        drop = {v["url"] for v in vs if v["source"] == "upload" and v["id"] != win["id"]} if remove_uploaded else set()
        rest = [u for u in card["photos"] if u != win["url"] and u not in drop]
        with httpx.Client(timeout=60) as client:
            self._save_media(client, token, t["nm_id"], [win["url"]] + rest, card["video"])
        with self.store._connect() as c:
            c.execute("UPDATE ab_tests SET status='finished', finished_at=?, winner=?, current_variant=? WHERE id=?", (now_iso(), win["id"], win["id"], test_id))
            self.log(c, test_id, "finish", f"Тест завершён. Главным поставлено: {win['label']}" + (f"; из карточки убрано своих фото: {len(drop)}" if drop else ""), win["id"])
        return self.detail(test_id)

    def delete(self, test_id: int) -> None:
        with self.store._connect() as c:
            t = self._test(c, test_id)
            if t["status"] == "running":
                raise ABError("Сначала завершите тест")
            for v in self._variants(c, test_id):
                if v["local_file"]:
                    Path(v["local_file"]).unlink(missing_ok=True)
            for tbl, col in (("ab_variant_clusters", "test_id"), ("ab_variants", "test_id"), ("ab_log", "test_id"), ("ab_tests", "id")):
                c.execute(f"DELETE FROM {tbl} WHERE {col}=?", (test_id,))

    # ------------------------------------------------------------------ цикл
    def _collect(self, test_id: int) -> dict | None:
        with self.store._connect() as c:
            t = self._test(c, test_id)
        if t["mode"] == "clusters":
            return self._collect_clusters(test_id)
        # Aggregate campaign metrics and the independent report of search
        # clusters.  Cluster rows are informational for timer/views modes.
        full = self._collect_full(test_id)
        self._collect_clusters(test_id)
        return full

    def _collect_full(self, test_id: int) -> dict | None:
        """Снимок общей статистики кампании: прирост с прошлого снимка — текущему варианту."""
        with self.store._connect() as c:
            t = self._test(c, test_id)
        if not t["current_variant"]:
            return None
        _token, ads = self._token(t["account"])
        snap_old = t["snapshot"] or {}
        since = min([date.fromisoformat(d) for d in snap_old] or [datetime.now(MSK).date()])
        since = max(since, datetime.now(MSK).date() - timedelta(days=30))
        with httpx.Client(timeout=120) as client:
            snap = self._stats(client, ads, t["advert_id"], t["nm_id"], since)
        delta = {k: 0.0 for k in FIELDS}
        for day, vals in snap.items():
            old = snap_old.get(day) or {}
            for k in FIELDS:
                delta[k] += max(0.0, vals.get(k, 0) - (old.get(k) or 0))
        merged = {**snap_old, **snap}
        keep = (datetime.now(MSK).date() - timedelta(days=3)).isoformat()
        merged = {d: v for d, v in merged.items() if d >= keep}
        with self.store._connect() as c:
            c.execute(f"UPDATE ab_variants SET {', '.join(f'{k}={k}+?' for k in FIELDS)} WHERE id=?", (*[delta[k] for k in FIELDS], t["current_variant"]))
            c.execute("UPDATE ab_tests SET snapshot=?, views_at_switch=COALESCE(views_at_switch,0)+? WHERE id=?", (json.dumps(merged), delta["views"], test_id))
        return delta

    def _collect_clusters(self, test_id: int) -> dict | None:
        """Собирает прирост по выбранным поисковым кластерам и текущему фото."""
        with self.store._connect() as c:
            t = self._test(c, test_id)
        if not t["current_variant"]:
            return None
        _token, ads = self._token(t["account"])
        snap_old = t.get("cluster_snapshot") or {}
        # Existing tests created before cluster reporting have no snapshot;
        # backfill the same historical window used at test creation.
        since = min([date.fromisoformat(d) for days in snap_old.values() for d in days]
                    or [datetime.now(MSK).date() - timedelta(days=RETRO_DAYS)])
        since = max(since, datetime.now(MSK).date() - timedelta(days=30))
        with httpx.Client(timeout=120) as client:
            snap = self._cluster_stats(client, ads, t["advert_id"], t["nm_id"], since)
        configured = ({norm_cluster(x): x for x in (t.get("clusters") or DEFAULT_CLUSTERS)}
                      if t["mode"] == "clusters" else {})
        all_keys = set(configured) | set(snap_old) | set(snap)
        delta_total = {k: 0.0 for k in FIELDS}
        merged = {key: dict(days) for key, days in snap_old.items()}
        for key in sorted(all_keys):
            old_days = snap_old.get(key) or {}
            new_days = snap.get(key) or {}
            merged_days = dict(old_days)
            for day, vals in new_days.items():
                old = old_days.get(day) or {}
                for field in FIELDS:
                    delta = max(0.0, float(vals.get(field, 0)) - float(old.get(field, 0) or 0))
                    if key in configured:
                        delta_total[field] += delta
                merged_days[day] = vals
            merged[key] = merged_days
        keep = (datetime.now(MSK).date() - timedelta(days=3)).isoformat()
        merged = {key: {day: vals for day, vals in days.items() if day >= keep} for key, days in merged.items()}
        with self.store._connect() as c:
            for key in sorted(all_keys):
                cluster_name = configured.get(key, key)
                vals = {k: 0.0 for k in FIELDS}
                old_days = snap_old.get(key) or {}
                new_days = snap.get(key) or {}
                for day, current in new_days.items():
                    old = old_days.get(day) or {}
                    for field in FIELDS:
                        vals[field] += max(0.0, float(current.get(field, 0)) - float(old.get(field, 0) or 0))
                # Configured rows are kept even at zero for progress.  Extra
                # rows are retained only once WB reports at least one view.
                if key not in configured and vals["views"] <= 0:
                    existing = c.execute("SELECT id FROM ab_variant_clusters WHERE test_id=? AND variant_id=? AND cluster=?",
                                         (test_id, t["current_variant"], cluster_name)).fetchone()
                    if not existing:
                        continue
                row = c.execute("SELECT id FROM ab_variant_clusters WHERE test_id=? AND variant_id=? AND cluster=?",
                                (test_id, t["current_variant"], cluster_name)).fetchone()
                if row:
                    c.execute(f"UPDATE ab_variant_clusters SET {', '.join(f'{k}={k}+?' for k in FIELDS)} WHERE id=?",
                              (*[vals[k] for k in FIELDS], row[0]))
                else:
                    c.execute("INSERT INTO ab_variant_clusters(test_id, variant_id, cluster, views, clicks, atbs, orders, spend, sum_price) VALUES(?,?,?,?,?,?,?,?,?)",
                              (test_id, t["current_variant"], cluster_name, *[vals[k] for k in FIELDS]))
            c.execute(f"UPDATE ab_variants SET {', '.join(f'{k}={k}+?' for k in FIELDS)} WHERE id=?",
                      (*[delta_total[k] for k in FIELDS], t["current_variant"]))
            c.execute("UPDATE ab_tests SET cluster_snapshot=?, views_at_switch=COALESCE(views_at_switch,0)+? WHERE id=?",
                      (json.dumps(merged, ensure_ascii=False), delta_total["views"], test_id))
        return delta_total

    def _verdict(self, test_id: int) -> list:
        """Исключить варианты, которые с уверенностью ≥95 % хуже лидера."""
        with self.store._connect() as c:
            t = self._test(c, test_id)
            vs = [v for v in self._variants(c, test_id) if not v["excluded"]]
        if t["mode"] == "clusters":
            return []
        ready = [v for v in vs if v["views"] >= MIN_VIEWS_VERDICT]
        if len(ready) < 2:
            return []
        leader = max(ready, key=lambda v: metric_rate(v, t["metric"]) or 0)
        out = []
        for v in ready:
            if v["id"] == leader["id"]:
                continue
            z = z_better(leader, v, t["metric"])
            if z is not None and z >= Z95:
                name = "CTR" if t["metric"] == "ctr" else "корзины на показ"
                reason = (f"Хуже лидера по {name} с уверенностью ≥95 % ({(metric_rate(v, t['metric']) or 0) * 100:.2f}% против "
                          f"{(metric_rate(leader, t['metric']) or 0) * 100:.2f}%)").replace(".", ",")
                with self.store._connect() as c:
                    c.execute("UPDATE ab_variants SET excluded=1, excluded_reason=? WHERE id=?", (reason, v["id"]))
                    self.log(c, test_id, "exclude", f"{v['label']}: {reason}", v["id"])
                out.append(v["id"])
        return out

    def _switch(self, test_id: int) -> None:
        with self.store._connect() as c:
            t = self._test(c, test_id)
            vs = [v for v in self._variants(c, test_id) if not v["excluded"]]
        if not vs:
            return
        others = [v for v in vs if v["id"] != t["current_variant"]] or vs
        nxt = min(others, key=lambda v: (v["views"], v["turns"], v["position"]))
        cur = next((v for v in vs if v["id"] == t["current_variant"]), None)
        token, _ads = self._token(t["account"])
        card = self.card(t["nm_id"], t["account"])
        if nxt["url"] not in card["photos"]:
            raise ABError(f"Фото варианта «{nxt['label']}» пропало из карточки — проверьте карточку")
        rest = [u for u in card["photos"] if u != nxt["url"]]
        with httpx.Client(timeout=60) as client:
            self._save_media(client, token, t["nm_id"], [nxt["url"]] + rest, card["video"])
        shown = (datetime.now(MSK) - datetime.fromisoformat(t["switched_at"])).total_seconds() if t["switched_at"] else 0
        with self.store._connect() as c:
            if cur:
                c.execute("UPDATE ab_variants SET shown_seconds=shown_seconds+? WHERE id=?", (shown, cur["id"]))
            c.execute("UPDATE ab_variants SET turns=turns+1 WHERE id=?", (nxt["id"],))
            c.execute("UPDATE ab_tests SET current_variant=?, switched_at=?, views_at_switch=0 WHERE id=?", (nxt["id"], now_iso(), test_id))
            self.log(c, test_id, "switch", f"Главным поставлено: {nxt['label']}", nxt["id"])

    def tick(self, campaign_status=lambda advert_id: None) -> None:
        """Раз в несколько минут: пауза/продолжение по кампании, сбор статистики, вердикт, смена фото, завершение."""
        if not self._lock.acquire(blocking=False):
            return
        try:
            with self.store._connect() as c:
                ids = [r[0] for r in c.execute("SELECT id FROM ab_tests WHERE status IN ('running', 'auto_paused')")]
            for tid in ids:
                try:
                    self._tick_one(tid, campaign_status)
                except ABError as exc:
                    with self.store._connect() as c:
                        self.log(c, tid, "error", str(exc))
        finally:
            self._lock.release()

    def _tick_one(self, tid, campaign_status):
        with self.store._connect() as c:
            t = self._test(c, tid)
        st = campaign_status(t["advert_id"])
        if t["status"] == "running" and st is not None and st != 9:
            self._collect(tid)
            with self.store._connect() as c:
                c.execute("UPDATE ab_tests SET status='auto_paused', pause_reason=? WHERE id=?",
                          ("Рекламная кампания не активна — тест продолжится, когда она снова заработает", tid))
                self.log(c, tid, "pause", "Кампания не активна — пауза")
            return
        if t["status"] == "auto_paused":
            if st == 9:
                with self.store._connect() as c:
                    c.execute("UPDATE ab_tests SET status='running', pause_reason=NULL, switched_at=? WHERE id=?", (now_iso(), tid))
                    self.log(c, tid, "resume", "Кампания снова активна — тест продолжен")
            return
        self._collect(tid)
        with self.store._connect() as c:
            t = self._test(c, tid)
            alive = [v for v in self._variants(c, tid) if not v["excluded"]]
        started = datetime.fromisoformat(t["started_at"])
        if t["mode"] == "clusters" and self._clusters_complete(tid, alive, t["clusters"], t["views_per_step"]):
            self.finish(tid)
            return
        self._verdict(tid)
        with self.store._connect() as c:
            t = self._test(c, tid)
            alive = [v for v in self._variants(c, tid) if not v["excluded"]]
        if len(alive) <= 1 or (datetime.now(MSK) - started).total_seconds() >= t["max_days"] * 86400:
            self.finish(tid, alive[0]["id"] if len(alive) == 1 else None)
            return
        since = (datetime.now(MSK) - datetime.fromisoformat(t["switched_at"])).total_seconds() / 3600
        if t["mode"] == "timer":
            due = since >= t["interval_hours"]
        elif t["mode"] == "clusters":
            due = self._variant_clusters_complete(tid, t["current_variant"], t["clusters"], t["views_per_step"])
        else:
            due = (t["views_at_switch"] or 0) >= t["views_per_step"] or since >= 48
        if due and since >= MIN_INTERVAL_H:
            self._switch(tid)

    def _cluster_rows(self, c, test_id: int, variant_id: int) -> list[dict]:
        cols = [d[0] for d in c.execute("SELECT * FROM ab_variant_clusters LIMIT 0").description]
        return [dict(zip(cols, row)) for row in c.execute(
            "SELECT * FROM ab_variant_clusters WHERE test_id=? AND variant_id=? ORDER BY id", (test_id, variant_id))]

    def _clusters_complete(self, test_id: int, variants: list[dict], clusters: list[str], target: float) -> bool:
        if not variants or not clusters:
            return False
        with self.store._connect() as c:
            for v in variants:
                rows = {norm_cluster(r["cluster"]): r for r in self._cluster_rows(c, test_id, v["id"])}
                if any((rows.get(norm_cluster(cluster), {}).get("views") or 0) < target for cluster in clusters):
                    return False
        return True

    def _variant_clusters_complete(self, test_id: int, variant_id: int, clusters: list[str], target: float) -> bool:
        with self.store._connect() as c:
            rows = {norm_cluster(r["cluster"]): r for r in self._cluster_rows(c, test_id, variant_id)}
        return bool(clusters) and all((rows.get(norm_cluster(cluster), {}).get("views") or 0) >= target for cluster in clusters)

    # ------------------------------------------------------------------ отдача
    def _summary(self, t: dict, vs: list) -> dict:
        control = next((v for v in vs if v["position"] == 0), vs[0] if vs else None)
        alive = [v for v in vs if not v["excluded"]]
        best = max(alive or vs, key=lambda v: metric_rate(v, t["metric"]) or 0) if vs else None
        tot = {k: sum(v[k] for v in vs) for k in FIELDS}

        def pct(v, key):
            return round(v[key] / v["views"] * 100, 3) if v and v.get("views") else None
        lift = None
        if control and best and pct(control, "clicks") and pct(best, "clicks"):
            lift = round((pct(best, "clicks") / pct(control, "clicks") - 1) * 100, 1)
        return {"views": tot["views"], "clicks": tot["clicks"], "atbs": tot["atbs"], "orders": tot["orders"], "spend": round(tot["spend"], 2),
                "ctr": round(tot["clicks"] / tot["views"] * 100, 2) if tot["views"] else None,
                "control_ctr": pct(control, "clicks"), "best_ctr": pct(best, "clicks"), "lift": lift,
                "control_cart": round(control["atbs"] / control["clicks"] * 100, 2) if control and control["clicks"] else None,
                "best_cart": round(best["atbs"] / best["clicks"] * 100, 2) if best and best["clicks"] else None,
                "best_id": best["id"] if best else None, "variants": len(vs), "alive": len(alive)}

    def _cluster_progress(self, t: dict, v: dict) -> list[dict]:
        with self.store._connect() as c:
            rows = {norm_cluster(r["cluster"]): r for r in self._cluster_rows(c, t["id"], v["id"])}
        configured = ({norm_cluster(cluster): cluster for cluster in (t.get("clusters") or DEFAULT_CLUSTERS)}
                      if t.get("mode") == "clusters" else {})
        out = []
        # Show configured clusters first, including zero-view rows needed to
        # understand progress, then every additional cluster that received a
        # view from WB.
        for key, cluster in configured.items():
            row = rows.get(key, {})
            views = float(row.get("views") or 0)
            clicks = float(row.get("clicks") or 0)
            out.append({"cluster": cluster, "views": round(views, 2), "clicks": round(clicks, 2),
                        "atbs": round(float(row.get("atbs") or 0), 2), "orders": round(float(row.get("orders") or 0), 2),
                        "ctr": round(clicks / views * 100, 2) if views else None,
                        "target": float(t["views_per_step"]), "done": views >= float(t["views_per_step"]), "configured": True})
        for key, row in rows.items():
            if key in configured:
                continue
            views = float(row.get("views") or 0)
            if views <= 0:
                continue
            clicks = float(row.get("clicks") or 0)
            out.append({"cluster": row.get("cluster") or key, "views": round(views, 2), "clicks": round(clicks, 2),
                        "atbs": round(float(row.get("atbs") or 0), 2), "orders": round(float(row.get("orders") or 0), 2),
                        "ctr": round(clicks / views * 100, 2) if views else None,
                        "target": None, "done": False, "configured": False})
        return out

    def _variant_out(self, t, v, leader):
        views = v["views"] or 0
        out = {**{k: v[k] for k in ("id", "url", "source", "label", "position", "excluded", "excluded_reason", "uploaded", "turns")},
               **{k: round(v[k], 2) for k in FIELDS}, "shown_hours": round(v["shown_seconds"] / 3600, 1),
               "ctr": round(v["clicks"] / views * 100, 2) if views else None,
               "cart_cr": round(v["atbs"] / v["clicks"] * 100, 2) if v["clicks"] else None,
               "ctr_cart": round(v["atbs"] / views * 100, 3) if views else None,
               "order_cr": round(v["orders"] / v["clicks"] * 100, 2) if v["clicks"] else None,
               "order_rate": round(v["orders"] / views * 1000, 3) if views else None,
               "revenue_rate": round(v["sum_price"] / views * 1000, 2) if views else None,
               "profit_rate": round(((v["sum_price"] or 0) - (v["spend"] or 0)) / views * 1000, 2) if views else None,
               "is_current": v["id"] == t["current_variant"], "is_winner": v["id"] == t.get("winner"),
               "has_file": bool(v["local_file"])}
        out["cluster_progress"] = self._cluster_progress(t, v)
        if leader and leader["id"] != v["id"] and views and leader["views"]:
            z = z_better(leader, v, t["metric"])
            out["confidence_worse"] = round(min(99.9, 100 * 0.5 * (1 + math.erf((z or 0) / math.sqrt(2)))), 1) if z is not None else None
        out["needs_views"] = max(0, MIN_VIEWS_VERDICT - views)
        return out

    def detail(self, test_id: int) -> dict:
        with self.store._connect() as c:
            t = self._test(c, test_id)
            vs = self._variants(c, test_id)
            log = [{"at": a, "kind": k, "text": x, "variant_id": vid} for a, k, x, vid in
                   c.execute("SELECT at, kind, text, variant_id FROM ab_log WHERE test_id=? ORDER BY id DESC LIMIT 200", (test_id,))]
        alive = [v for v in vs if not v["excluded"] and v["views"] >= MIN_VIEWS_VERDICT]
        leader = max(alive, key=lambda v: metric_rate(v, t["metric"]) or 0) if alive else None
        out = {k: t[k] for k in ("id", "account", "nm_id", "article", "title", "advert_id", "status", "mode", "interval_hours", "views_per_step", "metric",
                                 "max_days", "current_variant", "switched_at", "created_at", "started_at", "finished_at", "winner", "pause_reason")}
        out["clusters"] = t.get("clusters") or []
        out["target_views_per_cluster"] = float(t["views_per_step"]) if t["mode"] == "clusters" else None
        out["original_main"] = (t["original"] or [None])[0]
        out["variants"] = [self._variant_out(t, v, leader) for v in vs]
        out["summary"] = self._summary(t, vs)
        out["log"] = log
        if t["status"] == "running" and t["switched_at"]:
            hours = (datetime.now(MSK) - datetime.fromisoformat(t["switched_at"])).total_seconds() / 3600
            if t["mode"] == "timer":
                out["next_switch"] = f"через {max(0, t['interval_hours'] - hours):.1f} ч"
            elif t["mode"] == "clusters":
                current = next((v for v in vs if v["id"] == t["current_variant"]), None)
                progress = self._cluster_progress(t, current) if current else []
                remaining = max([max(0, p["target"] - p["views"]) for p in progress if p.get("configured") and p.get("target") is not None] or [0])
                out["next_switch"] = f"после ещё {remaining:.0f} показов в самом отстающем кластере"
            else:
                out["next_switch"] = f"после ещё {max(0, t['views_per_step'] - (t['views_at_switch'] or 0)):.0f} показов"
        return out

    def list(self) -> list:
        with self.store._connect() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM ab_tests ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'auto_paused' THEN 1 WHEN 'paused' THEN 2 "
                                            "WHEN 'draft' THEN 3 ELSE 4 END, id DESC")]
        out = []
        for tid in ids:
            d = self.detail(tid)
            cur = next((v for v in d["variants"] if v["is_current"]), None)
            out.append({k: d[k] for k in ("id", "nm_id", "article", "title", "status", "mode", "metric", "advert_id", "started_at", "finished_at", "pause_reason", "account")}
                       | {"summary": d["summary"], "clusters": d.get("clusters", []), "thumb": (cur or (d["variants"][0] if d["variants"] else {})).get("url"), "variants": len(d["variants"])})
        return out
