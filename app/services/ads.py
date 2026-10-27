"""Аналитика рекламы Wildberries.

Источник — API продвижения WB (категория ключа «Продвижение»):
  • /adv/v1/promotion/count + /api/advert/v2/adverts — кампании: название, статус, оплата (CPM/CPC), ставка, артикулы;
  • /adv/v3/fullstats — статистика по дням, площадкам (сайт/Android/iOS) и артикулам;
  • /adv/v1/normquery/stats — поисковые кластеры по дням (запросы покупателей);
  • /api/advert/v2/budget, /adv/v1/balance — бюджеты кампаний и баланс;
  • /adv/v1/upd — списания WB за рекламу (для сверки).
Все заказы и их сумма (для общего ДРР) — из таблицы заказов РНП (API статистики).
Одна и та же кампания из двух кабинетов (один продавец, разные ключи) учитывается один раз.
"""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import httpx

from app.services.nomenclature import art_key

HOST = "https://advert-api.wildberries.ru"
COUNT_URL = HOST + "/adv/v1/promotion/count"
ADVERTS_URL = HOST + "/api/advert/v2/adverts"
STATS_URL = HOST + "/adv/v3/fullstats"
CLUSTERS_URL = HOST + "/adv/v1/normquery/stats"
BUDGET_URL = HOST + "/api/advert/v2/budget"
BALANCE_URL = HOST + "/adv/v1/balance"
UPD_URL = HOST + "/adv/v1/upd"
MSK = timezone(timedelta(hours=3))
FIRST_DAYS = 90          # первая загрузка статистики
CLUSTER_DAYS = 30        # поисковые кластеры — за 30 дней
KEEP_DAYS = 400
STATUS = {4: "Готова к запуску", 7: "Завершена", 8: "Отклонена", 9: "Активна", 11: "На паузе", -1: "Удаляется"}
APPS = {1: "Сайт", 32: "Android", 64: "iOS"}


def _r(v) -> str:
    """Число по-русски: 56 152."""
    return f"{round(v or 0):,}".replace(",", "\u00a0")


def _x(v) -> str:
    return f"{v:.1f}".replace(".", ",")


class AdsError(Exception):
    pass


class AdsAnalytics:
    def __init__(self, store, tokens, catalog=lambda: {}):
        self.store = store
        self.tokens = tokens            # callable -> [(кабинет, ключ, ключ рекламы)]
        self.catalog = catalog          # callable -> {art_key: {brand, category, name}}
        self.state = {"status": "idle", "message": "", "finished_at": None, "errors": []}
        self._lock = threading.Lock()
        self._last = {}
        with store._connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS ads_campaigns (account TEXT, id INTEGER PRIMARY KEY, name TEXT, status INTEGER, payment_type TEXT,
                bid_type TEXT, placements TEXT, created TEXT, started TEXT, updated TEXT, nms TEXT, budget REAL, changed TEXT);
            CREATE TABLE IF NOT EXISTS ads_stats (advert_id INTEGER, day TEXT, nm_id INTEGER, app INTEGER, name TEXT, views REAL, clicks REAL,
                atbs REAL, orders REAL, shks REAL, spend REAL, sum_price REAL, PRIMARY KEY (advert_id, day, nm_id, app));
            CREATE INDEX IF NOT EXISTS ads_stats_day ON ads_stats(day);
            CREATE TABLE IF NOT EXISTS ads_clusters (advert_id INTEGER, nm_id INTEGER, day TEXT, query TEXT, views REAL, clicks REAL, atbs REAL,
                orders REAL, shks REAL, spend REAL, avg_pos REAL, PRIMARY KEY (advert_id, nm_id, day, query));
            CREATE INDEX IF NOT EXISTS ads_clusters_day ON ads_clusters(day);
            CREATE TABLE IF NOT EXISTS ads_upd (account TEXT, advert_id INTEGER, upd_time TEXT, upd_num INTEGER, camp_name TEXT, amount REAL,
                source TEXT, PRIMARY KEY (account, advert_id, upd_time, upd_num, amount));
            """)

    # ------------------------------------------------------------------ запросы к WB
    def _req(self, client, method, url, token, key, interval, **kw):
        for attempt in range(5):
            last = self._last.get(key, 0)
            pause = interval - (time.monotonic() - last)
            if last and pause > 0:
                time.sleep(pause)
            self._last[key] = time.monotonic()
            try:
                r = client.request(method, url, headers={"Authorization": token}, **kw)
            except httpx.HTTPError as exc:
                if attempt < 4:
                    time.sleep(3)
                    continue
                raise AdsError(f"нет связи с WB ({type(exc).__name__})")
            if r.status_code == 429 and attempt < 4:
                time.sleep(float(r.headers.get("X-Ratelimit-Retry") or max(interval, 10)))
                continue
            if r.status_code in (500, 502, 503, 504) and attempt < 4:
                time.sleep(5)
                continue
            if r.status_code in (401, 403):
                raise AdsError("у ключа нет доступа к категории «Продвижение»")
            if r.status_code in (204, 404):
                return None
            if r.status_code == 400:
                return None            # нет данных за период / кампания не подходит для метода
            if r.status_code >= 400:
                raise AdsError(f"WB ответил {r.status_code}: {r.text[:200]}")
            return r.json() if r.content.strip() else None
        raise AdsError("WB ограничил частоту запросов — попробуйте позже")

    # ------------------------------------------------------------------ синхронизация
    def sync(self, full: bool = False) -> dict:
        with self._lock:
            if self.state["status"] == "running":
                return dict(self.state)
            self.state = {"status": "running", "message": "Загружаю рекламные кампании…", "finished_at": None, "errors": []}
        errors = []
        try:
            tokens = self.tokens()
            if not tokens:
                raise AdsError("Нет кабинетов WB с ключом API (раздел «Кабинеты маркетплейсов»)")
            seen = set()
            with httpx.Client(timeout=120) as client:
                for entry in tokens:
                    name, token = entry[0], (entry[2] if len(entry) > 2 and entry[2] else entry[1])
                    try:
                        self._sync_account(client, name, token, seen, full)
                    except AdsError as exc:
                        errors.append(f"{name}: {exc}")
            self._trim()
            self.store.set_setting("ads_synced_at", datetime.now(MSK).isoformat(timespec="seconds"))
            self.state = {"status": "done" if not errors else "partial", "message": "Готово" if not errors else "Загружено не всё",
                          "finished_at": time.time(), "errors": errors}
        except Exception as exc:
            self.state = {"status": "error", "message": str(exc)[:300], "finished_at": time.time(), "errors": errors}
        return dict(self.state)

    def _sync_account(self, client, account, token, seen, full):
        tk = token[-8:]
        self.state["message"] = f"{account}: список кампаний…"
        count = self._req(client, "GET", COUNT_URL, token, f"count:{tk}", 0.3) or {}
        changes = {}
        for group in count.get("adverts") or []:
            for adv in group.get("advert_list") or []:
                aid = adv.get("advertId")
                if aid and aid not in seen:
                    changes[aid] = (group.get("status"), str(adv.get("changeTime") or ""))
        if not changes:
            return
        seen.update(changes)
        today = datetime.now(MSK).date()
        stored = self.store.get_setting(f"ads_since:{account}", "")
        start = today - timedelta(days=FIRST_DAYS) if full or not stored else date.fromisoformat(stored) - timedelta(days=3)

        # Кампании: подробности по всем (быстрый метод)
        ids = sorted(changes)
        rows = []
        for i in range(0, len(ids), 50):
            self.state["message"] = f"{account}: кампании {i + 1}–{min(i + 50, len(ids))} из {len(ids)}…"
            data = self._req(client, "GET", ADVERTS_URL, token, f"adverts:{tk}", 0.3, params={"ids": ",".join(map(str, ids[i:i + 50]))}) or {}
            for a in data.get("adverts") or []:
                st_ = a.get("settings") or {}
                ts = a.get("timestamps") or {}
                nms = [{"nm_id": n.get("nm_id"), "subject": (n.get("subject") or {}).get("name") or "",
                        "bid_search": ((n.get("bids_kopecks") or {}).get("search") or 0) / 100,
                        "bid_rec": ((n.get("bids_kopecks") or {}).get("recommendations") or 0) / 100} for n in a.get("nm_settings") or []]
                pl = st_.get("placements") or {}
                rows.append((account, a.get("id"), st_.get("name") or f"Кампания {a.get('id')}", a.get("status"), st_.get("payment_type") or "",
                             a.get("bid_type") or "", ",".join(k for k, v in pl.items() if v), ts.get("created"), ts.get("started"),
                             ts.get("updated"), json.dumps(nms, ensure_ascii=False), changes.get(a.get("id"), (None, ""))[1]))
        with self.store._connect() as c:
            c.executemany("INSERT INTO ads_campaigns(account,id,name,status,payment_type,bid_type,placements,created,started,updated,nms,changed) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET account=excluded.account, name=excluded.name, "
                          "status=excluded.status, payment_type=excluded.payment_type, bid_type=excluded.bid_type, placements=excluded.placements, "
                          "created=excluded.created, started=excluded.started, updated=excluded.updated, nms=excluded.nms, changed=excluded.changed", rows)

        # Статистика: активные/на паузе + завершённые, менявшиеся за период загрузки
        stat_ids = [aid for aid, (s, ch) in changes.items() if s in (9, 11) or (s == 7 and ch[:10] >= start.isoformat())]
        chunk = start
        while chunk <= today and stat_ids:
            end = min(today, chunk + timedelta(days=30))
            for i in range(0, len(stat_ids), 50):
                part = stat_ids[i:i + 50]
                self.state["message"] = f"{account}: статистика {chunk:%d.%m}–{end:%d.%m}, кампании {i + 1}–{i + len(part)} из {len(stat_ids)}…"
                data = self._req(client, "GET", STATS_URL, token, f"stats:{tk}", 20.5,
                                 params={"ids": ",".join(map(str, part)), "beginDate": chunk.isoformat(), "endDate": end.isoformat()}) or []
                acc = defaultdict(lambda: [None, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
                for adv in data if isinstance(data, list) else []:
                    aid = adv.get("advertId")
                    for day in adv.get("days") or []:
                        d = str(day.get("date") or "")[:10]
                        for app in day.get("apps") or []:
                            for nm in app.get("nms") or []:
                                x = acc[(aid, d, nm.get("nmId"), app.get("appType") or 0)]
                                x[0] = nm.get("name") or x[0]
                                for j, k in enumerate(("views", "clicks", "atbs", "orders", "shks", "sum", "sum_price"), 1):
                                    x[j] += float(nm.get(k) or 0)
                with self.store._connect() as c:
                    c.execute(f"DELETE FROM ads_stats WHERE day BETWEEN ? AND ? AND advert_id IN ({','.join('?' * len(part))})",
                              (chunk.isoformat(), end.isoformat(), *part))
                    c.executemany("INSERT OR REPLACE INTO ads_stats VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                  [(aid, d, nm, app, *v) for (aid, d, nm, app), v in acc.items() if d and nm])
            chunk = end + timedelta(days=1)

        # Поисковые кластеры: активные и на паузе кампании, по каждому артикулу
        with self.store._connect() as c:
            camp = {r[0]: (r[1], json.loads(r[2] or "[]")) for r in
                    c.execute(f"SELECT id, status, nms FROM ads_campaigns WHERE id IN ({','.join('?' * len(ids))})", ids)}
        items = [{"advertId": aid, "nmId": n["nm_id"]} for aid, (s, nms) in camp.items() if s in (9, 11) for n in nms if n.get("nm_id")]
        cstored = self.store.get_setting(f"ads_clusters_since:{account}", "")
        cstart = max(today - timedelta(days=CLUSTER_DAYS), date.fromisoformat(cstored) - timedelta(days=2) if cstored and not full else today - timedelta(days=CLUSTER_DAYS))
        for i in range(0, len(items), 20):
            part = items[i:i + 20]
            self.state["message"] = f"{account}: поисковые кластеры {i + 1}–{i + len(part)} из {len(items)}…"
            try:
                data = self._req(client, "POST", CLUSTERS_URL, token, f"clusters:{tk}", 6.2,
                                 json={"from": cstart.isoformat(), "to": today.isoformat(), "items": part}) or {}
            except AdsError:
                break
            out = defaultdict(lambda: [0.0] * 7)
            for it in data.get("items") or []:
                for ds in it.get("dailyStats") or []:
                    s = ds.get("stat") or {}
                    q = (s.get("normQuery") or "").strip()
                    if not q:
                        continue
                    x = out[(it.get("advertId"), it.get("nmId"), str(ds.get("date") or "")[:10], q)]
                    for j, k in enumerate(("views", "clicks", "atbs", "orders", "shks", "spend")):
                        x[j] += float(s.get(k) or 0)
                    x[6] = float(s.get("avgPos") or 0) or x[6]
            with self.store._connect() as c:
                for p in part:
                    c.execute("DELETE FROM ads_clusters WHERE advert_id=? AND nm_id=? AND day>=?", (p["advertId"], p["nmId"], cstart.isoformat()))
                c.executemany("INSERT OR REPLACE INTO ads_clusters VALUES (?,?,?,?,?,?,?,?,?,?,?)", [(*k, *v) for k, v in out.items()])
        self.store.set_setting(f"ads_clusters_since:{account}", today.isoformat())

        # Бюджеты активных кампаний и баланс
        live = [aid for aid, (s, _n) in camp.items() if s in (4, 9, 11)]
        for i in range(0, len(live), 50):
            data = self._req(client, "POST", BUDGET_URL, token, f"budget:{tk}", 3.2, json={"advertIds": live[i:i + 50]}) or {}
            with self.store._connect() as c:
                c.executemany("UPDATE ads_campaigns SET budget=? WHERE id=?",
                              [(float(a.get("total") or 0), a.get("advertId")) for a in data.get("adverts") or []])
        try:
            bal = self._req(client, "GET", BALANCE_URL, token, f"balance:{tk}", 1.1) or {}
            self.store.set_setting(f"ads_balance:{account}", json.dumps({"balance": bal.get("balance"), "net": bal.get("net"), "bonus": bal.get("bonus"),
                                                                       "at": datetime.now(MSK).isoformat(timespec="minutes")}))
        except AdsError:
            pass

        # Списания WB за рекламу
        ustart = start
        while ustart <= today:
            uend = min(today, ustart + timedelta(days=30))
            data = self._req(client, "GET", UPD_URL, token, f"upd:{tk}", 1.1, params={"from": ustart.isoformat(), "to": uend.isoformat()}) or []
            with self.store._connect() as c:
                c.execute("DELETE FROM ads_upd WHERE account=? AND substr(upd_time,1,10) BETWEEN ? AND ?", (account, ustart.isoformat(), uend.isoformat()))
                c.executemany("INSERT OR REPLACE INTO ads_upd VALUES (?,?,?,?,?,?,?)",
                              [(account, u.get("advertId"), str(u.get("updTime") or ustart.isoformat()), u.get("updNum") or 0, u.get("campName") or "",
                                float(u.get("updSum") or 0), u.get("paymentType") or "") for u in data if isinstance(u, dict)])
            ustart = uend + timedelta(days=1)
        self.store.set_setting(f"ads_since:{account}", today.isoformat())

    def _trim(self):
        edge = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
        with self.store._connect() as c:
            c.execute("DELETE FROM ads_stats WHERE day<?", (edge,))
            c.execute("DELETE FROM ads_clusters WHERE day<?", (edge,))

    # ------------------------------------------------------------------ выборка
    def _meta(self) -> dict:
        """nm -> артикул, бренд, предмет (из заказов РНП и каталога)."""
        out = {}
        cat = self.catalog() or {}
        with self.store._connect() as c:
            try:
                for nm, art, brand, subj in c.execute("SELECT nm_id, MAX(article), MAX(brand), MAX(subject) FROM rnp_orders GROUP BY nm_id"):
                    info = cat.get(art_key(art)) or {}
                    out[nm] = {"article": art or "", "brand": brand or info.get("brand") or "", "subject": subj or info.get("category") or ""}
            except Exception:
                pass
        return out

    def spend(self, start: str, end: str, account: str | None = None) -> dict | None:
        """Затраты на рекламу для дашборда: {"by_day": {день: ₽ по истории затрат WB}, "by_article": {(день, артикул): ₽
        по статистике кампаний}}. None — история затрат не загружена или не покрывает начало периода."""
        where, args = "", []
        if account:
            where, args = " AND account=?", [account]
        with self.store._connect() as c:
            first = c.execute(f"SELECT MIN(substr(upd_time,1,10)) FROM ads_upd WHERE 1=1{where}", args).fetchone()[0]
            if not first or first > start:
                return None
            # Один продавец под двумя ключами — одно и то же списание не считаем дважды
            by_day = dict(c.execute("SELECT substr(upd_time,1,10), SUM(amount) FROM (SELECT DISTINCT advert_id, upd_time, upd_num, amount "
                                    f"FROM ads_upd WHERE substr(upd_time,1,10) BETWEEN ? AND ?{where}) GROUP BY 1", [start, end, *args]).fetchall())
            stats = c.execute("SELECT s.day, s.nm_id, SUM(s.spend) FROM ads_stats s LEFT JOIN ads_campaigns k ON k.id=s.advert_id "
                              f"WHERE s.day BETWEEN ? AND ?{where.replace('account', 'k.account')} GROUP BY 1, 2", [start, end, *args]).fetchall()
        meta, by_article = self._meta(), defaultdict(float)
        for day, nm, value in stats:
            by_article[(day, art_key((meta.get(nm) or {}).get("article") or f"nmId {nm}"))] += value or 0
        return {"by_day": by_day, "by_article": dict(by_article)}

    def _campaigns(self) -> dict:
        with self.store._connect() as c:
            rows = c.execute("SELECT account, id, name, status, payment_type, bid_type, placements, created, started, updated, nms, budget FROM ads_campaigns").fetchall()
        return {r[1]: {"account": r[0], "id": r[1], "name": r[2], "status": r[3], "status_title": STATUS.get(r[3], str(r[3])), "payment_type": r[4],
                       "bid_type": r[5], "placements": [p for p in (r[6] or "").split(",") if p], "created": r[7], "started": r[8], "updated": r[9],
                       "nms": json.loads(r[10] or "[]"), "budget": r[11]} for r in rows}

    def _load(self, start: str, end: str, f: dict):
        camps, meta = self._campaigns(), self._meta()
        q = (f.get("q") or "").strip().lower()

        def keep_camp(aid):
            cp = camps.get(aid) or {}
            if f.get("cabinet") and cp.get("account") != f["cabinet"]:
                return False
            if f.get("status") and str(cp.get("status")) != str(f["status"]):
                return False
            if f.get("payment") and cp.get("payment_type") != f["payment"]:
                return False
            if f.get("campaign") and str(aid) != str(f["campaign"]):
                return False
            return True

        def keep_nm(nm, name=""):
            m = meta.get(nm) or {}
            if f.get("brand") and m.get("brand") != f["brand"]:
                return False
            if f.get("category") and m.get("subject") != f["category"]:
                return False
            return True

        with self.store._connect() as c:
            stats = c.execute("SELECT advert_id, day, nm_id, app, name, views, clicks, atbs, orders, shks, spend, sum_price FROM ads_stats "
                              "WHERE day BETWEEN ? AND ?", (start, end)).fetchall()
        rows = []
        for aid, day, nm, app, name, v, cl, atb, o, shk, sp, sm in stats:
            if not keep_camp(aid) or not keep_nm(nm):
                continue
            m = meta.get(nm) or {}
            if q and q not in f"{nm} {m.get('article', '')} {name or ''} {(camps.get(aid) or {}).get('name', '')} {aid}".lower():
                continue
            rows.append({"advert_id": aid, "day": day, "nm_id": nm, "app": app, "name": name or "", "views": v, "clicks": cl, "atbs": atb,
                         "orders": o, "shks": shk, "spend": sp, "sum_price": sm})
        return rows, camps, meta, keep_camp, keep_nm

    @staticmethod
    def _metrics(x: dict) -> dict:
        v, cl, sp, o = x.get("views", 0), x.get("clicks", 0), x.get("spend", 0), x.get("orders", 0)
        x["ctr"] = round(cl / v * 100, 2) if v else None
        x["cpc"] = round(sp / cl, 2) if cl else None
        x["cpm"] = round(sp / v * 1000, 2) if v else None
        x["cr"] = round(o / cl * 100, 2) if cl else None
        x["cart_cr"] = round(x.get("atbs", 0) / cl * 100, 2) if cl else None
        x["cpo"] = round(sp / o, 2) if o else None
        x["drr"] = round(sp / x["sum_price"] * 100, 2) if x.get("sum_price") else None
        for k in ("views", "clicks", "atbs", "orders", "shks", "spend", "sum_price"):
            if k in x:
                x[k] = round(x[k], 2)
        return x

    def _agg(self, rows, keyf) -> dict:
        out = defaultdict(lambda: defaultdict(float))
        for r in rows:
            a = out[keyf(r)]
            for k in ("views", "clicks", "atbs", "orders", "shks", "spend", "sum_price"):
                a[k] += r[k]
        return out

    def _all_orders(self, start: str, end: str, f: dict, meta: dict, nms: set | None = None) -> dict:
        """Все заказы (не только рекламные) по артикулу: шт и сумма — из РНП."""
        out = defaultdict(lambda: [0.0, 0.0])
        with self.store._connect() as c:
            try:
                for nm, cab, q, s in c.execute("SELECT nm_id, cabinet, COUNT(*), SUM(price) FROM rnp_orders WHERE day BETWEEN ? AND ? AND is_cancel=0 "
                                               "GROUP BY nm_id, cabinet", (start, end)):
                    if nms is not None and nm not in nms:
                        continue
                    m = meta.get(nm) or {}
                    if f.get("brand") and m.get("brand") != f["brand"]:
                        continue
                    if f.get("category") and m.get("subject") != f["category"]:
                        continue
                    out[nm][0] += q
                    out[nm][1] += s or 0
            except Exception:
                return {}
        return out

    def _summary(self, rows, start, end, f, meta) -> dict:
        tot = defaultdict(float)
        for r in rows:
            for k in ("views", "clicks", "atbs", "orders", "shks", "spend", "sum_price"):
                tot[k] += r[k]
        s = self._metrics(dict(tot))
        narrow = any(f.get(k) for k in ("campaign", "status", "payment", "q"))
        allo = self._all_orders(start, end, f, meta, {r["nm_id"] for r in rows} if narrow else None)
        s["all_orders"] = round(sum(v[0] for v in allo.values()))
        s["all_orders_sum"] = round(sum(v[1] for v in allo.values()), 2)
        s["drr_total"] = round(s.get("spend", 0) / s["all_orders_sum"] * 100, 2) if s["all_orders_sum"] else None
        s["ad_share"] = round(s.get("orders", 0) / s["all_orders"] * 100, 1) if s["all_orders"] else None
        s["campaigns"] = len({r["advert_id"] for r in rows if r["spend"] or r["views"]})
        s["articles"] = len({r["nm_id"] for r in rows if r["spend"] or r["views"]})
        return s

    def report(self, start: str, end: str, f: dict, prev: tuple | None = None) -> dict:
        rows, camps, meta, keep_camp, keep_nm = self._load(start, end, f)
        prev_rows = self._load(prev[0], prev[1], f)[0] if prev else []
        a, b = date.fromisoformat(start), date.fromisoformat(end)
        days = [(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]
        by_day = self._agg(rows, lambda r: r["day"])
        allo_day = defaultdict(float)
        with self.store._connect() as c:
            try:
                nms = {r["nm_id"] for r in rows} if any(f.get(k) for k in ("campaign", "status", "payment", "q")) else None
                for nm, d, s in c.execute("SELECT nm_id, day, SUM(price) FROM rnp_orders WHERE day BETWEEN ? AND ? AND is_cancel=0 GROUP BY nm_id, day", (start, end)):
                    if (nms is None or nm in nms) and keep_nm(nm):
                        allo_day[d] += s or 0
            except Exception:
                pass
        timeline = []
        for d in days:
            x = self._metrics({"day": d, **by_day.get(d, {})})
            x["all_orders_sum"] = round(allo_day.get(d, 0), 2)
            x["drr_total"] = round(x.get("spend", 0) / allo_day[d] * 100, 2) if allo_day.get(d) else None
            timeline.append(x)

        prev_camp = self._agg(prev_rows, lambda r: r["advert_id"])
        campaigns = []
        cagg = self._agg(rows, lambda r: r["advert_id"])
        cnms = defaultdict(set)
        for r in rows:
            if r["views"] or r["spend"]:
                cnms[r["advert_id"]].add(r["nm_id"])
        for aid, cp in camps.items():
            if not keep_camp(aid):
                continue
            x = cagg.get(aid)
            if not x and cp["status"] not in (9, 11):
                continue
            if f.get("q") and not x:
                continue
            m = self._metrics(dict(x or {}))
            m.update({"id": aid, "name": cp["name"], "account": cp["account"], "status": cp["status"], "status_title": cp["status_title"],
                      "payment_type": cp["payment_type"], "bid_type": cp["bid_type"], "placements": cp["placements"], "budget": cp["budget"],
                      "nms_total": len(cp["nms"]), "nms_active": len(cnms.get(aid, ())), "started": cp["started"],
                      "prev_spend": round(prev_camp[aid]["spend"], 2) if prev and aid in prev_camp else (0 if prev else None),
                      "articles": sorted({(meta.get(n) or {}).get("article") or str(n) for n in cnms.get(aid, ())})[:6]})
            campaigns.append(m)
        campaigns.sort(key=lambda x: -(x.get("spend") or 0))

        # Артикулы
        nagg = self._agg(rows, lambda r: r["nm_id"])
        prev_n = self._agg(prev_rows, lambda r: r["nm_id"])
        allo = self._all_orders(start, end, f, meta)
        names = {r["nm_id"]: r["name"] for r in rows if r["name"]}
        ncamps = defaultdict(set)
        for r in rows:
            if r["views"] or r["spend"]:
                ncamps[r["nm_id"]].add(r["advert_id"])
        articles = []
        for nm, x in nagg.items():
            m = self._metrics(dict(x))
            mt = meta.get(nm) or {}
            ao = allo.get(nm, [0, 0])
            m.update({"nm_id": nm, "article": mt.get("article") or "", "brand": mt.get("brand") or "", "subject": mt.get("subject") or "",
                      "title": names.get(nm, ""), "campaigns": len(ncamps.get(nm, ())), "all_orders": round(ao[0]), "all_orders_sum": round(ao[1], 2),
                      "drr_total": round(x["spend"] / ao[1] * 100, 2) if ao[1] else None,
                      "ad_share": round(x["orders"] / ao[0] * 100, 1) if ao[0] else None,
                      "prev_spend": round(prev_n[nm]["spend"], 2) if prev and nm in prev_n else (0 if prev else None)})
            articles.append(m)
        articles.sort(key=lambda x: -(x.get("spend") or 0))

        # Площадки
        papp = self._agg(rows, lambda r: r["app"])
        platforms = [{"app": k, "title": APPS.get(k, "Другое"), **self._metrics(dict(v))} for k, v in sorted(papp.items(), key=lambda kv: -kv[1]["spend"])]

        # Поисковые кластеры
        ids = {aid for aid in camps if keep_camp(aid)}
        with self.store._connect() as c:
            crow = c.execute("SELECT advert_id, nm_id, day, query, views, clicks, atbs, orders, shks, spend, avg_pos FROM ads_clusters WHERE day BETWEEN ? AND ?",
                             (start, end)).fetchall()
            cl_first = c.execute("SELECT MIN(day) FROM ads_clusters").fetchone()[0]
        q = (f.get("q") or "").strip().lower()
        cq = defaultdict(lambda: {"views": 0.0, "clicks": 0.0, "atbs": 0.0, "orders": 0.0, "shks": 0.0, "spend": 0.0, "pos_w": 0.0, "camps": set(), "nms": set()})
        for aid, nm, d, query, v, cl, atb, o, shk, sp, pos in crow:
            if aid not in ids or not keep_nm(nm):
                continue
            mt = meta.get(nm) or {}
            if q and q not in f"{query} {nm} {mt.get('article', '')} {(camps.get(aid) or {}).get('name', '')}".lower():
                continue
            x = cq[query]
            for k, val in (("views", v), ("clicks", cl), ("atbs", atb), ("orders", o), ("shks", shk), ("spend", sp)):
                x[k] += val or 0
            x["pos_w"] += (pos or 0) * (v or 0)
            x["camps"].add(aid)
            x["nms"].add(nm)
        clusters = []
        for query, x in cq.items():
            m = self._metrics({k: x[k] for k in ("views", "clicks", "atbs", "orders", "shks", "spend")})
            m.update({"query": query, "avg_pos": round(x["pos_w"] / x["views"], 1) if x["views"] else None, "campaigns": len(x["camps"]),
                      "nms": len(x["nms"]), "articles": sorted({(meta.get(n) or {}).get("article") or str(n) for n in x["nms"]})[:4]})
            clusters.append(m)
        clusters.sort(key=lambda x: -(x.get("spend") or 0))

        # Списания и баланс
        with self.store._connect() as c:
            upd = c.execute("SELECT account, advert_id, upd_time, camp_name, amount, source FROM ads_upd WHERE substr(upd_time,1,10) BETWEEN ? AND ? "
                            "ORDER BY upd_time DESC", (start, end)).fetchall()
            accounts = sorted({r[0] for r in c.execute("SELECT account FROM ads_campaigns")})
        upd = [u for u in upd if (not f.get("cabinet") or u[0] == f["cabinet"]) and (not f.get("campaign") or str(u[1]) == str(f["campaign"]))]
        balances = {}
        for acc in accounts:
            try:
                balances[acc] = json.loads(self.store.get_setting(f"ads_balance:{acc}", "") or "{}")
            except ValueError:
                pass
        by_src = defaultdict(float)
        for u in upd:
            by_src[u[5] or "—"] += u[4]
        spending = {"total": round(sum(u[4] for u in upd), 2), "count": len(upd), "by_source": {k: round(v, 2) for k, v in by_src.items()},
                    "items": [{"account": u[0], "advert_id": u[1], "time": u[2], "campaign": u[3], "amount": u[4], "source": u[5]} for u in upd[:300]],
                    "balances": balances}

        summary = self._summary(rows, start, end, f, meta)
        live = [cp for cp in camps.values() if cp["status"] in (9, 11) and keep_camp(cp["id"])]
        summary["active"] = sum(1 for cp in live if cp["status"] == 9)
        summary["paused"] = sum(1 for cp in live if cp["status"] == 11)
        summary["budget_left"] = round(sum(cp["budget"] or 0 for cp in live), 2)
        return {"days": len(days), "summary": summary, "previous": self._summary(prev_rows, prev[0], prev[1], f, meta) if prev else None,
                "prev_period": list(prev) if prev else None, "timeline": timeline, "campaigns": campaigns, "articles": articles,
                "platforms": platforms, "clusters": clusters[:500], "clusters_total": len(clusters), "clusters_from": cl_first,
                "spending": spending, "insights": self._insights(campaigns, articles, clusters, summary),
                "options": {"cabinets": accounts, "campaigns": sorted([{"id": cp["id"], "name": cp["name"], "status": cp["status"]} for cp in camps.values()
                                                                       if cp["status"] in (9, 11) or cagg.get(cp["id"])], key=lambda x: x["name"]),
                            "brands": sorted({m["brand"] for m in meta.values() if m.get("brand")}),
                            "categories": sorted({m["subject"] for m in meta.values() if m.get("subject")})},
                "state": dict(self.state), "synced_at": self.store.get_setting("ads_synced_at", "")}

    @staticmethod
    def _insights(campaigns, articles, clusters, s) -> list:
        """Подсказки: где реклама тратит деньги впустую или работает хуже среднего."""
        out = []
        avg_cpo = s.get("cpo")
        for c in campaigns:
            if (c.get("spend") or 0) >= 1000 and not c.get("orders"):
                out.append({"level": "bad", "kind": "campaign", "id": c["id"], "title": f"«{c['name']}» тратит без заказов",
                            "text": f"Расход {_r(c['spend'])} ₽, {_r(c.get('clicks'))} кликов и ни одного заказа. Проверьте ставку, карточку и минус-фразы или остановите кампанию."})
            elif avg_cpo and c.get("cpo") and c["cpo"] > avg_cpo * 2 and (c.get("spend") or 0) >= 3000:
                out.append({"level": "warn", "kind": "campaign", "id": c["id"], "title": f"«{c['name']}»: заказ в {_x(c['cpo'] / avg_cpo)} раза дороже среднего",
                            "text": f"Стоимость заказа {_r(c['cpo'])} ₽ при средней {_r(avg_cpo)} ₽."})
            if c.get("status") == 9 and c.get("budget") is not None and c["budget"] < 500 and (c.get("spend") or 0) > 0:
                out.append({"level": "warn", "kind": "campaign", "id": c["id"], "title": f"«{c['name']}»: заканчивается бюджет",
                            "text": f"Осталось {_r(c['budget'])} ₽ — кампания скоро остановится."})
        avg_ctr = s.get("ctr")
        for a in articles[:40]:
            if avg_ctr and a.get("ctr") is not None and a["views"] >= 5000 and a["ctr"] < avg_ctr / 2:
                out.append({"level": "warn", "kind": "article", "id": a["nm_id"], "title": f"{a['article'] or a['nm_id']}: низкий CTR {_x(a['ctr'])}%",
                            "text": f"Средний CTR по рекламе {_x(avg_ctr)}%. Покупатели видят товар, но не кликают — проверьте главное фото, цену и рейтинг."})
            if a.get("drr_total") and a["drr_total"] >= 25 and (a.get("spend") or 0) >= 3000:
                out.append({"level": "bad", "kind": "article", "id": a["nm_id"], "title": f"{a['article'] or a['nm_id']}: ДРР {_x(a['drr_total'])}%",
                            "text": "Реклама съедает четверть и больше от суммы всех заказов артикула. Проверьте маржу — возможно, реклама убыточна."})
        waste = [c for c in clusters if (c.get("spend") or 0) >= 300 and not c.get("orders")]
        if waste:
            total = sum(c["spend"] for c in waste)
            out.append({"level": "bad", "kind": "clusters", "id": None, "title": f"{len(waste)} поисковых запросов без заказов: {_r(total)} ₽",
                        "text": "Кандидаты в минус-фразы: " + ", ".join(f"«{c['query']}»" for c in waste[:6]) + ("…" if len(waste) > 6 else "")})
        order = {"bad": 0, "warn": 1}
        return sorted(out, key=lambda x: order.get(x["level"], 2))[:30]
