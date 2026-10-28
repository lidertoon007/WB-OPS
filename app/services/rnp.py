"""РНП — «рука на пульсе»: оперативные показатели по артикулам и дням и прогноз прибыли по заказам.

Данные свежие, не из еженедельного отчёта:
  • заказы — API статистики WB (каждый заказ: цена продавца, СПП, цена для покупателя, отмена);
  • реклама — API продвижения WB (показы, клики, корзины, заказы и расходы по артикулам за день);
  • остатки — из кэша расчёта ОПиУ (обновляются вместе с «Финансами»).
Прогноз прибыли по заказам строится на юнит-экономике каждого артикула за прошлые 60 дней из отчёта
реализации (та же выгрузка, что у «Дашборда»): процент выкупа, комиссия, эквайринг, логистика на заказ.
Плюс себестоимость/подготовка из «Номенклатуры» и налог. План заказов и маржи, заметки к дням — свои.
"""
from __future__ import annotations

import re
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import httpx

from app.services.nomenclature import COST_COL, PREP_COL, art_key, brand_key, num

ORDERS_URL = "https://statistics-api.wildberries.ru/api/v1/supplier/orders"
ADV_COUNT_URL = "https://advert-api.wildberries.ru/adv/v1/promotion/count"
ADV_STATS_URL = "https://advert-api.wildberries.ru/adv/v3/fullstats"
KEEP_DAYS = 180
FIRST_LOAD_DAYS = 45
HISTORY_DAYS, HISTORY_LAG = 60, 10     # юнит-экономика: 60 дней, без последних 10 (выкупы ещё не дошли)


class RNPError(Exception):
    pass


class _Limiter:
    def __init__(self):
        self.last = {}

    def wait(self, key, interval):
        last = self.last.get(key, 0)
        pause = interval - (time.monotonic() - last)
        if last and pause > 0:
            time.sleep(pause)
        self.last[key] = time.monotonic()


class RNP:
    MP, TITLE = "wb", "WB"
    PLANS_TABLE = "rnp_plans"
    SYNCED_KEY = "rnp_synced_at"
    NO_TOKENS = "Нет кабинетов WB с API-токеном (раздел «Кабинеты маркетплейсов»)"

    def __init__(self, store, tokens, economics, cost_rows, retro_rows=lambda: []):
        self.store = store
        self.tokens = tokens            # callable -> [(название кабинета, токен[, токен рекламы])]
        self.economics = economics      # callable -> данные дашборда (дневная выгрузка ОПиУ) или None
        self.cost_rows = cost_rows      # callable -> строки cost.csv
        self.retro_rows = retro_rows    # callable -> ретро-бонусы производителей [{brand, pct, since}]
        self._manual_brands = {}        # артикул -> бренд из cost.csv (как в ОПиУ — приоритетнее данных WB)
        self.state = {"status": "idle", "message": "", "at": None, "errors": []}
        self._lock = threading.Lock()
        self.limiter = _Limiter()
        with store._connect() as c:
            c.execute(f"CREATE TABLE IF NOT EXISTS {self.PLANS_TABLE} (article TEXT, month TEXT, orders_per_day REAL, margin REAL, PRIMARY KEY(article, month))")
            c.execute("CREATE TABLE IF NOT EXISTS rnp_notes (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, article TEXT, text TEXT, author TEXT, created_at TEXT)")
            self._init_db(c)

    def _init_db(self, c):
        """Таблицы загрузки из API маркетплейса (у Ozon — свои, см. rnp_ozon.py)."""
        c.execute("CREATE TABLE IF NOT EXISTS rnp_orders (srid TEXT PRIMARY KEY, cabinet TEXT, day TEXT, last_change TEXT, nm_id INTEGER, "
                  "article TEXT, brand TEXT, subject TEXT, price REAL, spp REAL, finished REAL, total REAL, is_cancel INTEGER)")
        c.execute("CREATE INDEX IF NOT EXISTS rnp_orders_day ON rnp_orders(day)")
        cols = {r[1] for r in c.execute("PRAGMA table_info(rnp_orders)")}
        if "barcode" not in cols:            # размер заказа — для заказов в день по размерам (вкладка «Остатки FBS»)
            c.execute("ALTER TABLE rnp_orders ADD COLUMN barcode TEXT")
            c.execute("ALTER TABLE rnp_orders ADD COLUMN tech_size TEXT")
            c.execute("DELETE FROM app_settings WHERE key LIKE 'rnp_orders_since:%'")     # перезагрузить заказы с размерами
        c.execute("CREATE TABLE IF NOT EXISTS rnp_ads (cabinet TEXT, day TEXT, nm_id INTEGER, views REAL, clicks REAL, spend REAL, atbs REAL, "
                  "orders REAL, shks REAL, sum_price REAL, PRIMARY KEY(cabinet, day, nm_id))")
        # Один продавец мог быть подключён дважды (кабинет панели и кабинет ОПиУ с другим ключом) —
        # реклама задваивалась. Один раз очищаем рекламу, чтобы она перезагрузилась без дублей.
        if not c.execute("SELECT 1 FROM app_settings WHERE key='rnp_ads_dedup_v1'").fetchone():
            c.execute("DELETE FROM rnp_ads")
            c.execute("DELETE FROM app_settings WHERE key LIKE 'rnp_ads_since:%'")
            c.execute("INSERT OR REPLACE INTO app_settings(key, value) VALUES('rnp_ads_dedup_v1', '1')")

    # ------------------------------------------------------------------ загрузка из WB
    def _get(self, client, url, token, params, key, interval, need):
        for attempt in range(5):
            self.limiter.wait(key, interval)
            try:
                r = client.get(url, params=params, headers={"Authorization": token})
            except httpx.HTTPError as exc:
                if attempt < 4:
                    time.sleep(3)
                    continue
                raise RNPError(f"нет связи с WB ({type(exc).__name__})")
            if r.status_code == 429 and attempt < 4:
                time.sleep(float(r.headers.get("X-Ratelimit-Retry") or interval or 20))
                continue
            if r.status_code in (500, 502, 503, 504) and attempt < 4:
                time.sleep(5)
                continue
            if r.status_code in (401, 403):
                raise RNPError(f"у токена нет доступа к категории «{need}»")
            if r.status_code == 204:
                return []
            if r.status_code >= 400:
                raise RNPError(f"WB ответил {r.status_code}: {r.text[:200]}")
            return r.json() if r.content.strip() else []
        raise RNPError("WB ограничил частоту запросов — попробуйте позже")

    def sync(self, days: int | None = None) -> dict:
        """Обновить заказы и рекламу по всем кабинетам. Медленно (лимиты WB: заказы — раз в минуту)."""
        with self._lock:
            if self.state["status"] == "running":
                return dict(self.state)
            self.state = {"status": "running", "message": f"Загрузка заказов и рекламы из {self.TITLE}…", "at": None, "errors": []}
        errors = []
        try:
            tokens = self.tokens()
            if not tokens:
                raise RNPError(self.NO_TOKENS)
            self._ads_seen = set()          # кампании уже загружены другим кабинетом (тот же продавец)
            with httpx.Client(timeout=120) as client:
                for entry in tokens:
                    name = entry[0]
                    for part, fn, key in self._sync_steps(entry):
                        self.state["message"] = f"{name}: {part}…"
                        try:
                            fn(client, name, key, days)
                        except RNPError as exc:
                            errors.append(f"{name}, {part}: {exc}")
            self._trim()
            self.store.set_setting(self.SYNCED_KEY, datetime.now(timezone.utc).isoformat())
            self.state = {"status": "done" if not errors else "partial", "message": "Готово" if not errors else "Загружено не всё",
                          "at": time.time(), "errors": errors}
        except Exception as exc:
            self.state = {"status": "error", "message": str(exc)[:300], "at": time.time(), "errors": errors}
        return dict(self.state)

    def _sync_steps(self, entry):
        """[(что загружаем, функция, ключ)] для кабинета из self.tokens()."""
        token = entry[1]
        ads_token = entry[2] if len(entry) > 2 and entry[2] else token
        return [("заказы", self._sync_orders, token), ("реклама", self._sync_ads, ads_token)]

    def _since(self, key, days):
        stored = self.store.get_setting(key, "")
        start = (date.today() - timedelta(days=days or FIRST_LOAD_DAYS)).isoformat()
        if stored and not days:
            return min(stored, (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds"))
        return start

    def _sync_orders(self, client, cabinet, token, days):
        key = f"rnp_orders_since:{cabinet}"
        since = self._since(key, days)
        last_seen = since
        for _ in range(50):
            rows = self._get(client, ORDERS_URL, token, {"dateFrom": since, "flag": 0}, f"orders:{token[-8:]}", 61, "Статистика") or []
            with self.store._connect() as c:
                c.executemany(
                    "INSERT INTO rnp_orders(srid,cabinet,day,last_change,nm_id,article,brand,subject,price,spp,finished,total,is_cancel,barcode,tech_size) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(srid) DO UPDATE SET last_change=excluded.last_change, "
                    "is_cancel=excluded.is_cancel, price=excluded.price, spp=excluded.spp, finished=excluded.finished, "
                    "barcode=excluded.barcode, tech_size=excluded.tech_size",
                    [(str(r.get("srid") or r.get("gNumber") or f"{r.get('date')}:{r.get('nmId')}:{i}"), cabinet, str(r.get("date") or "")[:10],
                      str(r.get("lastChangeDate") or ""), r.get("nmId"), str(r.get("supplierArticle") or "").strip(), r.get("brand") or "",
                      r.get("subject") or "", float(r.get("priceWithDisc") or 0), float(r.get("spp") or 0), float(r.get("finishedPrice") or 0),
                      float(r.get("totalPrice") or 0), int(bool(r.get("isCancel"))), str(r.get("barcode") or ""), str(r.get("techSize") or ""))
                     for i, r in enumerate(rows)])
            if rows:
                last_seen = max(last_seen, str(rows[-1].get("lastChangeDate") or ""))
            if len(rows) < 80000 or not rows or str(rows[-1].get("lastChangeDate")) == since:
                break
            since = str(rows[-1]["lastChangeDate"])
        self.store.set_setting(key, last_seen)

    def _sync_ads(self, client, cabinet, token, days):
        count = self._get(client, ADV_COUNT_URL, token, None, f"advcount:{token[-8:]}", 1, "Продвижение") or {}
        start = date.today() - timedelta(days=days or 0)
        stored = self.store.get_setting(f"rnp_ads_since:{cabinet}", "")
        if not days:
            start = date.fromisoformat(stored) - timedelta(days=2) if stored else date.today() - timedelta(days=FIRST_LOAD_DAYS)
        end = date.today()
        ids = []
        for group in count.get("adverts") or []:
            if group.get("status") not in (7, 9, 11):
                continue
            for adv in group.get("advert_list") or []:
                changed = str(adv.get("changeTime") or "")[:10]
                if adv.get("advertId") in getattr(self, "_ads_seen", set()):
                    continue
                self._ads_seen = getattr(self, "_ads_seen", set()) | {adv.get("advertId")}
                if group.get("status") != 7 or changed >= start.isoformat():   # завершённые — только если менялись в периоде
                    ids.append(adv.get("advertId"))
        rows = defaultdict(lambda: [0.0] * 7)
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(end, chunk_start + timedelta(days=30))
            for i in range(0, len(ids), 50):
                part = ids[i:i + 50]
                data = self._get(client, ADV_STATS_URL, token, {"ids": ",".join(map(str, part)), "beginDate": chunk_start.isoformat(),
                                                               "endDate": chunk_end.isoformat()}, f"advstats:{token[-8:]}", 20, "Продвижение") or []
                for adv in data if isinstance(data, list) else []:
                    for day in adv.get("days") or []:
                        d = str(day.get("date") or "")[:10]
                        for app in day.get("apps") or []:
                            for nm in app.get("nms") or []:
                                acc = rows[(d, nm.get("nmId"))]
                                for j, k in enumerate(("views", "clicks", "sum", "atbs", "orders", "shks", "sum_price")):
                                    acc[j] += float(nm.get(k) or 0)
            chunk_start = chunk_end + timedelta(days=1)
        with self.store._connect() as c:
            c.execute("DELETE FROM rnp_ads WHERE cabinet=? AND day>=?", (cabinet, start.isoformat()))
            c.executemany("INSERT OR REPLACE INTO rnp_ads(cabinet,day,nm_id,views,clicks,spend,atbs,orders,shks,sum_price) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          [(cabinet, d, nm, *vals) for (d, nm), vals in rows.items() if d and nm])
        self.store.set_setting(f"rnp_ads_since:{cabinet}", end.isoformat())

    def _trim(self):
        edge = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
        with self.store._connect() as c:
            c.execute("DELETE FROM rnp_orders WHERE day<?", (edge,))
            c.execute("DELETE FROM rnp_ads WHERE day<?", (edge,))

    # ------------------------------------------------------------------ юнит-экономика
    def _dash(self):
        try:
            return self.economics()
        except Exception:
            return None

    def unit_economics(self, d=None) -> dict:
        """Доли и ставки по артикулам за прошлые дни; где заказов мало — средние по кабинету."""
        end = date.today() - timedelta(days=HISTORY_LAG)
        start = end - timedelta(days=HISTORY_DAYS)
        s, e = start.isoformat(), end.isoformat()
        per, total = defaultdict(lambda: defaultdict(float)), defaultdict(float)
        rate = 0.0
        for cab in (d or {}).get("cabinets", {}).values():
            rate = cab.get("tax_rate") or rate
            for day, arts in cab["days"].items():
                if s <= day <= e:
                    for art, m in arts.items():
                        # «ads_promo» — удержание «WB Продвижение»: дашборд выделяет его отдельно, здесь это та же реклама
                        m = {**m, "ads": m.get("ads", 0) + m.get("ads_promo", 0)}
                        for k in ("revenue_sales", "revenue_returns", "commission", "acquiring", "logistics", "storage", "net_qty", "orders_qty", "paid", "ads"):
                            per[art][k] += m.get(k, 0)
                            total[k] += m.get(k, 0)
        if not total["orders_qty"]:                     # заказов в выгрузке нет — берём свои
            for art, n in self._order_counts(s, e):
                per[art_key(art)]["orders_qty"] += n
                total["orders_qty"] += n

        def ratios(m, fallback=None):
            revenue = m["revenue_sales"] + m["revenue_returns"]
            orders = m["orders_qty"]
            storage = -m["storage"] / HISTORY_DAYS
            if (orders < 20 or revenue <= 0) and fallback:
                return {**fallback, "storage_per_day": storage, "source": "кабинет"}
            return {"buyout": min(1.0, max(0.0, m["net_qty"] / orders)) if orders else None,
                    "commission": (-m["commission"] / revenue) if revenue else None,
                    "acquiring": (-m["acquiring"] / revenue) if revenue else None,
                    "logistics_per_order": (-m["logistics"] / orders) if orders else None,
                    "paid_share": (m["paid"] / revenue) if revenue and m["paid"] else None,
                    "ads_share": (-m["ads"] / revenue) if revenue else None,
                    "storage_per_day": storage, "source": "артикул"}
        base = ratios(total)
        base["storage_per_day"] = 0.0
        return {"articles": {a: ratios(m, base) for a, m in per.items()}, "cabinet": base, "tax_rate": rate, "window": [s, e]}

    def _order_counts(self, start, end):
        """[(артикул, заказов)] из своей загрузки заказов — если в выгрузке дашборда заказов нет."""
        with self.store._connect() as c:
            return c.execute("SELECT article, COUNT(*) FROM rnp_orders WHERE day BETWEEN ? AND ? GROUP BY article", (start, end)).fetchall()

    def _costs(self):
        out = defaultdict(list)
        self._manual_brands = {}
        for r in self.cost_rows():
            key = art_key(r.get("артикул"))
            if key:
                out[key].append((r.get("действует_с", ""), num(r.get("кратность")) or 1.0, num(r.get(COST_COL)), num(r.get(PREP_COL)) or 0.0))
                if r.get("бренд"):
                    self._manual_brands[key] = r["бренд"]
        for v in out.values():
            v.sort(key=lambda x: x[0])
        return out

    def _retro(self) -> dict:
        """{бренд: [(действует_с, %)]} — ретро-бонусы производителей по возрастанию даты."""
        out = defaultdict(list)
        for r in self.retro_rows() or []:
            out[brand_key(r["brand"])].append((r.get("since") or "", r["pct"]))
        for v in out.values():
            v.sort(key=lambda x: x[0])
        return out

    @staticmethod
    def _retro_at(rows, day):
        chosen = None
        for since, pct in rows or []:
            if not since or since[:7] <= day[:7]:
                chosen = pct
        return chosen

    @staticmethod
    def _cost_at(rows, day):
        chosen = None
        for since, mult, cost, prep in rows or []:
            if not since or since[:7] <= day[:7]:
                chosen = (mult, cost, prep)
        return chosen

    # ------------------------------------------------------------------ факты
    @staticmethod
    def _only(d, cabinets: set | None):
        """Выгрузка дашборда только по выбранным кабинетам (None — все)."""
        if not d or not cabinets:
            return d
        return {**d, "cabinets": {k: v for k, v in (d.get("cabinets") or {}).items() if k in cabinets}}

    def facts(self, start: str, end: str, d=None, cabinets: set | None = None) -> dict:
        """{(день, артикул): метрики}. Заказы — из API WB; за дни, где их нет, — из кэша ОПиУ.
        Реклама — из API продвижения; если её не загружали — из отчёта реализации (с задержкой).
        cabinets — названия кабинетов (в панели и в ОПиУ), None — все."""
        out = defaultdict(lambda: defaultdict(float))
        info = {}
        own_days, has_ads = set(), False
        cab_sql, cab_args = "", ()
        if cabinets:
            cab_sql, cab_args = f" AND cabinet IN ({','.join('?' * len(cabinets))})", tuple(sorted(cabinets))
        d = self._only(d, cabinets)
        with self.store._connect() as c:
            for day, art, nm, brand, subject, qty, price, spp, finished, cancels in c.execute(
                    "SELECT day, article, MAX(nm_id), MAX(brand), MAX(subject), COUNT(*), SUM(price), SUM(spp), SUM(finished), SUM(is_cancel) "
                    f"FROM rnp_orders WHERE day BETWEEN ? AND ?{cab_sql} GROUP BY day, article", (start, end, *cab_args)):
                key = art_key(art)
                m = out[(day, key)]
                m["orders"] += qty
                m["orders_sum"] += price or 0
                m["spp_sum"] += spp or 0
                m["spp_n"] += qty
                m["finished_sum"] += finished or 0
                m["cancels"] += cancels or 0
                own_days.add(day)
                info.setdefault(key, {"article": art, "nm_id": nm, "brand": brand, "subject": subject})
            nm_art = {nm: art_key(a) for nm, a in c.execute("SELECT nm_id, MAX(article) FROM rnp_orders GROUP BY nm_id")}
            for day, nm, views, clicks, spend, atbs, orders in c.execute(
                    "SELECT day, nm_id, SUM(views), SUM(clicks), SUM(spend), SUM(atbs), SUM(orders) "
                    f"FROM rnp_ads WHERE day BETWEEN ? AND ?{cab_sql} GROUP BY day, nm_id", (start, end, *cab_args)):
                has_ads = True
                key = nm_art.get(nm) or f"NM{nm}"
                m = out[(day, key)]
                m["views"] += views or 0
                m["clicks"] += clicks or 0
                m["ads"] += spend or 0
                m["ad_atbs"] += atbs or 0
                m["ad_orders"] += orders or 0
                info.setdefault(key, {"article": key, "nm_id": nm, "brand": "", "subject": ""})
            has_ads = has_ads or bool(c.execute("SELECT 1 FROM rnp_ads LIMIT 1").fetchone())
        cache_days = self._from_cache(out, d, start, end, own_days, has_ads)
        return {"rows": out, "info": info,
                "sources": {"orders": "api" if own_days and not cache_days else ("mixed" if own_days else ("cache" if cache_days else "none")),
                            "ads": "api" if has_ads else "report"}}

    @staticmethod
    def _from_cache(out, d, start, end, own_days, has_ads) -> set:
        """Дополнить факты выгрузкой дашборда: заказы за дни без своей загрузки, реклама — если её не загружали.
        Возвращает дни, заказы которых взяты из выгрузки."""
        cache_days = set()
        for cab in (d or {}).get("cabinets", {}).values():
            for day, arts in cab["days"].items():
                if not (start <= day <= end):
                    continue
                for art, m in arts.items():
                    if day not in own_days and m.get("orders_qty"):
                        x = out[(day, art)]
                        x["orders"] += m["orders_qty"]
                        x["orders_sum"] += m.get("orders_sum", 0)
                        x["finished_sum"] += m.get("orders_sum", 0)
                        x["cancels"] += m.get("cancel_qty", 0)
                        cache_days.add(day)
                    withheld = m.get("ads", 0) + m.get("ads_promo", 0)     # удержания за рекламу из отчёта реализации
                    if not has_ads and withheld:
                        out[(day, art)]["ads"] += -withheld
        return cache_days

    # ------------------------------------------------------------------ таблица
    @staticmethod
    def _derive(m, n_days=1):
        o, s = m.get("orders", 0), m.get("orders_sum", 0)
        rev = m.get("revenue", 0)
        spend = m.get("cogs", 0) + m.get("prep", 0)
        return {**{k: round(v, 2) for k, v in m.items()},
                "price": (s / o) if o else None,
                "spp": (m["spp_sum"] / m["spp_n"]) if m.get("spp_n") else None,
                "price_after_spp": (m.get("finished_sum", 0) / o) if o and m.get("spp_n") else None,
                "buyout": (m.get("sales_qty", 0) / o * 100) if o else None,
                "cancel_rate": (m.get("cancels", 0) / o * 100) if o else None,
                "drr": (m.get("ads", 0) / s * 100) if s else None,
                "drr_sales": (m.get("ads", 0) / rev * 100) if rev else None,
                "ctr": (m.get("clicks", 0) / m["views"] * 100) if m.get("views") else None,
                "cpc": (m.get("ads", 0) / m["clicks"]) if m.get("clicks") else None,
                "cpo": (m.get("ads", 0) / m["ad_orders"]) if m.get("ad_orders") else None,
                # воронка карточки (Ozon: /v1/analytics/data) — показы → корзина → заказ
                "cart_rate": (m.get("tocart", 0) / m["card_views"] * 100) if m.get("card_views") else None,
                "order_rate": (o / m["tocart"] * 100) if m.get("tocart") else None,
                "margin": (m.get("profit", 0) / rev * 100) if rev else None,
                "roi": (m.get("profit", 0) / spend * 100) if spend else None,
                "orders_per_day": o / max(1, n_days), "profit_per_day": m.get("profit", 0) / max(1, n_days)}

    def table(self, start: str, end: str, brand: str | None = None, category: str | None = None, articles: set | None = None,
              with_cells: bool = False, cabinets: set | None = None) -> dict:
        part = self.collect(start, end, brand=brand, category=category, articles=articles, with_cells=with_cells, cabinets=cabinets)
        return compose([part], with_cells=with_cells, notes=self.notes(start, end))

    def collect(self, start: str, end: str, brand: str | None = None, category: str | None = None, articles: set | None = None,
                with_cells: bool = False, cabinets: set | None = None) -> dict:
        """Факты и прогноз по дням и артикулам одного маркетплейса — суммы без производных показателей (их считает compose)."""
        d = self._dash()
        f = self.facts(start, end, d, cabinets)
        econ = self.unit_economics(d)      # доли расходов — по всем кабинетам, как и раньше
        d = self._only(d, cabinets)
        costs = self._costs()
        retro = self._retro()
        catalog = (d or {}).get("catalog", {})
        stocks, prices, stocks_at = defaultdict(float), {}, None
        for cab in (d or {}).get("cabinets", {}).values():
            for k, v in cab.get("stocks", {}).items():
                stocks[k] += v
            prices.update(cab.get("prices") or {})
            stocks_at = max(filter(None, [stocks_at, cab.get("stocks_at")]), default=None)
        plans = self.plans()
        days = []
        x = date.fromisoformat(start)
        while x <= date.fromisoformat(end):
            days.append(x.isoformat())
            x += timedelta(days=1)
        n = len(days)

        def meta(key):
            info = f["info"].get(key) or {}
            c = catalog.get(key) or {}
            return {"article": c.get("article") or info.get("article") or key, "nm_id": info.get("nm_id"),
                    "brand": c.get("brand") or info.get("brand") or "", "category": c.get("category") or info.get("subject") or ""}

        def match(key):
            mt = meta(key)
            return (not articles or key in articles) and (not brand or mt["brand"] == brand) and (not category or mt["category"] == category)

        by_day = {day: defaultdict(float) for day in days}
        by_art = defaultdict(lambda: defaultdict(float))
        cells = {}
        for (day, key), m in f["rows"].items():
            if day not in by_day or not match(key):
                continue
            e = econ["articles"].get(key) or econ["cabinet"]
            buyout = e.get("buyout") or 0
            revenue = m["orders_sum"] * buyout
            sales_qty = m["orders"] * buyout
            price = self._cost_at(costs.get(key), day)
            has_cost = bool(price and price[1] is not None)
            calc = {"sales_qty": sales_qty, "revenue": revenue,
                    "commission": revenue * (e.get("commission") or 0), "acquiring": revenue * (e.get("acquiring") or 0),
                    "logistics": m["orders"] * (e.get("logistics_per_order") or 0),
                    "cogs": sales_qty * price[0] * price[1] if has_cost else 0.0,
                    "prep": sales_qty * price[0] * (price[2] or 0) if price else 0.0,
                    "tax": revenue * (e.get("paid_share") or 1) * econ["tax_rate"] / 100,
                    "no_cost": m["orders"] if (m["orders"] and not has_cost) else 0}
            # Ретро-бонус производителя: % бренда от себестоимости проданного — доход, увеличивает прибыль
            pct = self._retro_at(retro.get(brand_key(self._manual_brands.get(key) or meta(key)["brand"])), day) if has_cost else None
            calc["retro"] = calc["cogs"] * pct / 100 if pct else 0.0
            calc["profit"] = (revenue - calc["commission"] - calc["acquiring"] - calc["logistics"] - m.get("ads", 0) - calc["cogs"]
                              + calc["retro"] - calc["prep"] - calc["tax"])
            for target in (by_day[day], by_art[key]):
                for k, v in list(m.items()) + list(calc.items()):
                    target[k] += v
            if with_cells:
                cells[(day, key)] = {**m, **calc}
        for key, qty in stocks.items():
            if qty and match(key):
                by_art[key]["stock"] = qty
        storage_day = 0.0
        for key, m in by_art.items():                    # хранение — равномерно по дням
            spd = (econ["articles"].get(key) or {}).get("storage_per_day") or 0
            if spd:
                m["storage"] += spd * n
                m["profit"] -= spd * n
                storage_day += spd
        for m in by_day.values():
            if storage_day:
                m["storage"] += storage_day
                m["profit"] -= storage_day
        month = end[:7]
        return {"mp": self.MP, "title": self.TITLE, "days": days, "by_day": by_day, "by_art": dict(by_art), "cells": cells,
                "meta": {key: meta(key) for key in by_art},
                "plans": {key: plans[(key, month)] for key in by_art if (key, month) in plans},
                "costs": {key: self._cost_at(costs.get(key), end) for key in by_art},
                "economics": econ, "prices": prices, "stocks_at": stocks_at, "sources": f["sources"],
                "synced_at": self.store.get_setting(self.SYNCED_KEY, ""), "state": dict(self.state)}

    HEAT_METRICS = ("orders", "orders_sum", "profit", "margin", "ads", "drr", "price", "spp", "buyout", "ctr", "views", "clicks", "cancel_rate",
                    "card_views", "tocart", "cart_rate")

    def heatmap(self, start: str, end: str, metric: str, **filters) -> dict:
        if metric not in self.HEAT_METRICS:
            raise RNPError("Неизвестный показатель")
        return heatmap_of(self.table(start, end, with_cells=True, **filters), metric)

    # ------------------------------------------------------------------ план и заметки
    def plans(self) -> dict:
        with self.store._connect() as c:
            return {(a, m): {"orders_per_day": o, "margin": mg} for a, m, o, mg in c.execute(f"SELECT article, month, orders_per_day, margin FROM {self.PLANS_TABLE}")}

    def set_plan(self, article: str, month: str, orders_per_day=None, margin=None):
        key = art_key(article)
        if not key or not re.fullmatch(r"\d{4}-\d{2}", month or ""):
            raise RNPError("Нужны артикул и месяц ГГГГ-ММ")
        with self.store._connect() as c:
            if orders_per_day in (None, "") and margin in (None, ""):
                c.execute(f"DELETE FROM {self.PLANS_TABLE} WHERE article=? AND month=?", (key, month))
            else:
                c.execute(f"INSERT OR REPLACE INTO {self.PLANS_TABLE}(article, month, orders_per_day, margin) VALUES(?,?,?,?)",
                          (key, month, orders_per_day, margin))

    def notes(self, start: str, end: str) -> list:
        with self.store._connect() as c:
            return [{"id": i, "day": d, "article": a, "text": t, "author": au, "created_at": ca} for i, d, a, t, au, ca in
                    c.execute("SELECT id, day, article, text, author, created_at FROM rnp_notes WHERE day BETWEEN ? AND ? ORDER BY day, id", (start, end))]

    def add_note(self, day: str, article: str, text: str, author: str = "") -> dict:
        text = " ".join(str(text or "").split())[:500]
        if not text or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or ""):
            raise RNPError("Нужны дата и текст заметки")
        with self.store._connect() as c:
            note_id = c.execute("INSERT INTO rnp_notes(day, article, text, author, created_at) VALUES(?,?,?,?,?)",
                                (day, art_key(article) if article else "", text, author, datetime.now(timezone.utc).isoformat())).lastrowid
        return {"id": note_id}

    def delete_note(self, note_id: int) -> bool:
        with self.store._connect() as c:
            return c.execute("DELETE FROM rnp_notes WHERE id=?", (note_id,)).rowcount > 0


def _add(target, m):
    for k, v in m.items():
        target[k] += v


def compose(parts: list, with_cells: bool = False, notes=()) -> dict:
    """Таблица РНП из частей RNP.collect: одного маркетплейса или нескольких (режим «все» — суммы по артикулу и дню)."""
    first = parts[0]
    days = first["days"]
    n = len(days)
    by_day = {day: defaultdict(float) for day in days}
    by_art = defaultdict(lambda: defaultdict(float))
    cells = defaultdict(lambda: defaultdict(float))
    meta, costs, econ_art, prices, plans = {}, {}, {}, {}, defaultdict(list)
    for p in parts:
        for day, m in p["by_day"].items():
            _add(by_day[day], m)
        for (day, key), m in p["cells"].items():
            _add(cells[(day, key)], m)
        econ = p["economics"]
        for key, m in p["by_art"].items():
            _add(by_art[key], m)
            meta.setdefault(key, p["meta"][key])
            if costs.get(key) is None:
                costs[key] = p["costs"].get(key)
            econ_art.setdefault(key, econ["articles"].get(key) or econ["cabinet"])
            if prices.get(key) is None and p["prices"].get(key) is not None:
                prices[key] = p["prices"][key]
            if key in p["plans"]:
                plans[key].append(p["plans"][key])

    def plan_of(key):
        """План артикула; в режиме «все» — сумма планов заказов по маркетплейсам, маржа — средняя по плану заказов."""
        rows = plans.get(key) or []
        opd = sum(r["orders_per_day"] or 0 for r in rows) or None
        with_margin = [r for r in rows if r["margin"] is not None]
        weight = sum(r["orders_per_day"] or 1 for r in with_margin)
        margin = sum(r["margin"] * (r["orders_per_day"] or 1) for r in with_margin) / weight if with_margin else None
        return {"orders_per_day": opd, "margin": margin}

    art_rows = []
    for key, m in by_art.items():
        if not (m.get("orders") or m.get("ads") or m.get("stock")):
            continue
        plan = plan_of(key)
        row = RNP._derive(m, n)
        cost = costs.get(key)
        row.update(meta[key])
        row.update({"key": key, "plan_orders_per_day": plan["orders_per_day"], "plan_margin": plan["margin"],
                    "plan_done": (row["orders_per_day"] / plan["orders_per_day"] * 100) if plan["orders_per_day"] else None,
                    "stock": m.get("stock", 0), "stock_days": (m.get("stock", 0) / row["orders_per_day"]) if row["orders_per_day"] else None,
                    "current_price": prices.get(key), "cost": cost[1] * cost[0] if cost and cost[1] is not None else None,
                    "prep_cost": (cost[2] or 0) * cost[0] if cost else None,
                    "economics": econ_art[key]})
        art_rows.append(row)
    art_rows.sort(key=lambda r: (-(r.get("orders_sum") or 0), -(r.get("stock") or 0), r["article"]))
    total = defaultdict(float, {k: 0.0 for k in ("orders", "orders_sum", "ads", "revenue", "profit", "sales_qty", "no_cost")})
    for r in by_day.values():
        for k, v in r.items():
            total[k] += v
    total_row = RNP._derive(total, n)
    total_row["stock"] = sum(r["stock"] for r in art_rows)
    plan_total = sum(r["plan_orders_per_day"] or 0 for r in art_rows)
    total_row["plan_orders_per_day"] = plan_total or None
    fact_planned = sum(r["orders_per_day"] for r in art_rows if r["plan_orders_per_day"])
    total_row["plan_fact_per_day"] = fact_planned if plan_total else None
    total_row["plan_done"] = (fact_planned / plan_total * 100) if plan_total else None
    planned = [r for r in art_rows if r["plan_orders_per_day"]]
    total_row["plan_margin"] = (sum((r["plan_margin"] or 0) * r["plan_orders_per_day"] for r in planned) /
                                sum(r["plan_orders_per_day"] for r in planned)) if planned and any(r["plan_margin"] for r in planned) else None
    econ = first["economics"]
    running = next((p["state"] for p in parts if p["state"].get("status") == "running"), first["state"])
    out = {"days": days, "by_day": {day: RNP._derive(m) for day, m in by_day.items()}, "articles": art_rows, "total": total_row,
           "economics": {"window": econ["window"], "cabinet": econ["cabinet"], "tax_rate": econ["tax_rate"]},
           "economics_by_mp": {p["mp"]: {"window": p["economics"]["window"], "tax_rate": p["economics"]["tax_rate"]} for p in parts},
           "sources": first["sources"] if len(parts) == 1 else _merge_sources([p["sources"] for p in parts]),
           "sources_by_mp": {p["mp"]: p["sources"] for p in parts},
           "stocks_at": max(filter(None, (p["stocks_at"] for p in parts)), default=None), "notes": list(notes),
           "synced_at": first["synced_at"], "synced_by_mp": {p["mp"]: p["synced_at"] for p in parts},
           "state": running, "states": {p["mp"]: p["state"] for p in parts}, "marketplaces": [p["mp"] for p in parts]}
    if with_cells:
        out["cells"] = {k: RNP._derive(m) for k, m in cells.items()}
    return out


def _merge_sources(items: list) -> dict:
    orders = {s["orders"] for s in items} - {"none"}
    return {"orders": orders.pop() if len(orders) == 1 else ("mixed" if orders else "none"),
            "ads": "api" if all(s["ads"] == "api" for s in items) else "report"}


def heatmap_of(t: dict, metric: str) -> dict:
    """Тепловая карта из таблицы РНП, собранной с with_cells=True."""
    if metric not in RNP.HEAT_METRICS:
        raise RNPError("Неизвестный показатель")
    rows = t["articles"][:80]
    keys = {r["key"] for r in rows}
    cells = {f"{day}|{key}": v.get(metric) for (day, key), v in t["cells"].items() if key in keys}
    return {"days": t["days"], "metric": metric, "cells": cells,
            "rows": [{"key": r["key"], "article": r["article"], "total": r.get(metric)} for r in rows]}
