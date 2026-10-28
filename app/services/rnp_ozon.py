"""РНП по Ozon: заказы, воронка карточек и расходы на продвижение по дням и артикулам (offer_id).

Источники — Seller API, те же методы и разбор ответов, что в расчёте ОПиУ (opiu/Ozon/ozon_finance.py):
  • заказы — /v3/posting/fbo/list и /v4/posting/fbs/list (отправления с отменами; последние 2 недели
    перечитываются при каждой загрузке — статус отправления меняется);
  • воронка — /v1/analytics/data, разрез SKU × день: показы и добавления в корзину (часть метрик Ozon
    отдаёт только с подпиской Premium — тогда воронки нет, это видно в интерфейсе);
  • реклама — начисления /v1/finance/accrual/by-day, группа «Реклама и продвижение»: только расходы.
    Показы и клики рекламы есть лишь в Ozon Performance API (отдельные ключи performance.ozon.ru) — его здесь нет.
Прогноз прибыли — как у WB (RNP): юнит-экономика артикула за 60 дней из дневной выгрузки Ozon-дашборда.
Свои таблицы rnp_ozon_*: таблицы WB (rnp_orders, rnp_ads) читают другие разделы, sku Ozon туда не пишем.
"""
from __future__ import annotations

import importlib.util
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import httpx

from app.services.nomenclature import art_key
from app.services.rnp import FIRST_LOAD_DAYS, KEEP_DAYS, RNP, RNPError

API_HOST = "https://api-seller.ozon.ru"
REFRESH_DAYS = 14                  # отправления за последние 2 недели перечитываются: появляются отмены
FUNNEL_METRICS = ["hits_view", "hits_tocart"]
NO_ARTICLE = "Без артикула"        # начисления за продвижение без товара (например, оплата кампании целиком)
FINANCE_SCRIPT = Path(__file__).resolve().parents[2] / "opiu" / "Ozon" / "ozon_finance.py"
_finance = None


def finance():
    """Модуль расчёта ОПиУ Ozon: адреса методов, разбор отправлений и начислений."""
    global _finance
    if _finance is None:
        spec = importlib.util.spec_from_file_location("wbops_ozon_finance", FINANCE_SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _finance = module
    return _finance


class _Refused(RNPError):
    def __init__(self, status, text):
        super().__init__(text)
        self.status = status


class OzonRNP(RNP):
    MP, TITLE = "ozon", "Ozon"
    PLANS_TABLE = "rnp_ozon_plans"
    SYNCED_KEY = "rnp_ozon_synced_at"
    NO_TOKENS = "Нет кабинетов Ozon с Client ID и API-ключом (раздел «Кабинеты маркетплейсов»)"

    def _init_db(self, c):
        c.execute("CREATE TABLE IF NOT EXISTS rnp_ozon_orders (posting TEXT, offer_id TEXT, cabinet TEXT, day TEXT, sku INTEGER, "
                  "qty REAL, amount REAL, is_cancel INTEGER, PRIMARY KEY(posting, offer_id))")
        c.execute("CREATE INDEX IF NOT EXISTS rnp_ozon_orders_day ON rnp_ozon_orders(day)")
        c.execute("CREATE TABLE IF NOT EXISTS rnp_ozon_skus (cabinet TEXT, sku INTEGER, offer_id TEXT, PRIMARY KEY(cabinet, sku))")
        c.execute("CREATE TABLE IF NOT EXISTS rnp_ozon_funnel (cabinet TEXT, day TEXT, sku INTEGER, views REAL, tocart REAL, "
                  "PRIMARY KEY(cabinet, day, sku))")
        c.execute("CREATE TABLE IF NOT EXISTS rnp_ozon_ads (cabinet TEXT, day TEXT, article TEXT, spend REAL, PRIMARY KEY(cabinet, day, article))")

    # ------------------------------------------------------------------ загрузка из Ozon
    def _sync_steps(self, entry):
        acc = {"client_id": str(entry[1]), "api_key": entry[2]}
        return [("заказы", self._sync_orders, acc), ("воронка", self._sync_funnel, acc), ("реклама", self._sync_ads, acc)]

    def _post(self, client, acc, path, body, interval=0.0):
        for attempt in range(5):
            self.limiter.wait(f"{path}:{acc['client_id']}", interval)
            try:
                r = client.post(API_HOST + path, json=body, headers={"Client-Id": acc["client_id"], "Api-Key": acc["api_key"]})
            except httpx.HTTPError as exc:
                if attempt < 4:
                    time.sleep(3)
                    continue
                raise RNPError(f"нет связи с Ozon ({type(exc).__name__}); если включён VPN — отключите его")
            if r.status_code == 429 and attempt < 4:
                try:
                    time.sleep(min(max(float(r.headers.get("Retry-After") or 0), 1.0), 120.0))
                except ValueError:
                    time.sleep(5)
                continue
            if r.status_code in (500, 502, 503, 504) and attempt < 4:
                time.sleep(5)
                continue
            if r.status_code in (401, 403):
                raise _Refused(r.status_code, f"Ozon не дал доступ к {path} — проверьте Client ID и роль API-ключа")
            if r.status_code >= 400:
                raise _Refused(r.status_code, f"Ozon ответил {r.status_code} на {path}: {r.text[:200]}")
            data = r.json() if r.content.strip() else {}
            return data.get("result", data) if isinstance(data, dict) else {}
        raise RNPError("Ozon ограничил частоту запросов — попробуйте позже")

    def _start(self, key, days, back=0):
        """Первый день загрузки: за days дней, иначе с прошлой загрузки (минус back дней), впервые — FIRST_LOAD_DAYS."""
        today = date.today()
        if days:
            return today - timedelta(days=days)
        stored = self.store.get_setting(key, "")
        if stored:
            return max(today - timedelta(days=KEEP_DAYS), min(date.fromisoformat(stored[:10]), today) - timedelta(days=back))
        return today - timedelta(days=FIRST_LOAD_DAYS)

    def _sync_orders(self, client, cabinet, acc, days):
        fin = finance()
        key = f"rnp_ozon_orders_since:{cabinet}"
        first, today = self._start(key, days, REFRESH_DAYS), date.today()
        flt = {"since": f"{first}T00:00:00.000Z", "to": f"{today}T23:59:59.999Z"}
        rows = defaultdict(lambda: ["", 0.0, 0.0, 0])          # (отправление, артикул) -> день, шт, сумма, отменён
        skus = {}
        for path in (fin.URL_FBO_LIST, fin.URL_FBS_LIST):
            cursor = ""
            for _ in range(5000):
                res = self._post(client, acc, path, {"cursor": cursor, "filter": flt, "limit": fin.POSTINGS_LIMIT, "sort_dir": "ASC",
                                                     "with": {"analytics_data": False, "financial_data": False}})
                postings = res.get("postings") or []
                for day, posting, article, qty, price, cancelled in fin.posting_rows(postings):
                    row = rows[(posting, article)]
                    row[0], row[3] = day, int(bool(cancelled))
                    row[1] += qty
                    row[2] += qty * price
                for p in postings:
                    for pr in p.get("products") or []:
                        if fin.as_int(pr.get("sku")) and pr.get("offer_id"):
                            skus[fin.as_int(pr.get("sku"))] = pr["offer_id"]
                cursor = res.get("cursor") or ""
                if not res.get("has_next") or not cursor:
                    break
        sku_of = {v: k for k, v in skus.items()}
        with self.store._connect() as c:
            c.executemany("INSERT OR REPLACE INTO rnp_ozon_orders(posting,offer_id,cabinet,day,sku,qty,amount,is_cancel) VALUES(?,?,?,?,?,?,?,?)",
                          [(posting, article, cabinet, day, sku_of.get(article), qty, amount, cancel)
                           for (posting, article), (day, qty, amount, cancel) in rows.items() if day])
            c.executemany("INSERT OR REPLACE INTO rnp_ozon_skus(cabinet, sku, offer_id) VALUES(?,?,?)",
                          [(cabinet, sku, offer) for sku, offer in skus.items()])
        self.store.set_setting(key, today.isoformat())

    def _sku_map(self, cabinet) -> dict:
        with self.store._connect() as c:
            return dict(c.execute("SELECT sku, offer_id FROM rnp_ozon_skus WHERE cabinet=?", (cabinet,)).fetchall())

    def _load_products(self, client, cabinet, acc):
        """Дополнить справочник SKU → артикул товарами без заказов (/v3/product/list, /v3/product/info/list)."""
        ids, last_id = [], ""
        for _ in range(200):
            res = self._post(client, acc, "/v3/product/list", {"filter": {"visibility": "ALL"}, "limit": 1000, "last_id": last_id})
            items = res.get("items") or []
            ids += [pr.get("product_id") or pr.get("id") for pr in items if pr.get("product_id") or pr.get("id")]
            last_id = res.get("last_id") or ""
            if not items or not last_id:
                break
        mapping = {}
        for i in range(0, len(ids), 1000):
            res = self._post(client, acc, "/v3/product/info/list", {"product_id": ids[i:i + 1000]})
            for item in res.get("items") or []:
                for sku in [item.get("sku")] + [src.get("sku") for src in item.get("sources") or []]:
                    if sku and item.get("offer_id"):
                        mapping[int(sku)] = item["offer_id"]
        with self.store._connect() as c:
            c.executemany("INSERT OR IGNORE INTO rnp_ozon_skus(cabinet, sku, offer_id) VALUES(?,?,?)",
                          [(cabinet, sku, offer) for sku, offer in mapping.items()])

    def _sync_funnel(self, client, cabinet, acc, days):
        fin = finance()
        key = f"rnp_ozon_funnel_since:{cabinet}"
        start, end = self._start(key, days, 2), date.today()
        data, offset = [], 0
        try:
            for _ in range(200):
                res = self._post(client, acc, fin.URL_ANALYTICS, {
                    "date_from": start.isoformat(), "date_to": end.isoformat(), "metrics": FUNNEL_METRICS, "dimension": ["sku", "day"],
                    "filters": [], "limit": 1000, "offset": offset, "sort": [{"key": FUNNEL_METRICS[0], "order": "DESC"}]}, interval=20)
                batch = res.get("data") or []
                data += batch
                offset += len(batch)
                if len(batch) < 1000:
                    break
        except _Refused as exc:
            if exc.status not in (400, 403):
                raise
            data = None
        # Показы и корзину Ozon отдаёт только с Premium: отказывает или молча возвращает не все метрики
        # (так же проверяет fetch_funnel в ozon_finance.py; наборы FUNNEL_FALLBACK показов не содержат).
        if data is None or any(len(r.get("metrics") or []) != len(FUNNEL_METRICS) for r in data):
            self.store.set_setting(f"rnp_ozon_funnel_off:{cabinet}", "1")
            return
        self.store.set_setting(f"rnp_ozon_funnel_off:{cabinet}", "")
        rows = defaultdict(lambda: [0.0, 0.0])
        for r in data:
            dims = r.get("dimensions") or []
            sku = fin.as_int(dims[0].get("id")) if dims else None
            day = next((str(d.get("id"))[:10] for d in dims[1:] if str(d.get("id") or "")[:4].isdigit()), None)
            if sku is None or not day:
                continue
            for i, value in enumerate(r.get("metrics") or []):
                rows[(day, sku)][i] += fin.money(value)
        if {sku for _, sku in rows} - set(self._sku_map(cabinet)):
            try:
                self._load_products(client, cabinet, acc)
            except RNPError:
                pass                                        # без справочника строки останутся «SKU …»
        with self.store._connect() as c:
            c.execute("DELETE FROM rnp_ozon_funnel WHERE cabinet=? AND day>=?", (cabinet, start.isoformat()))
            c.executemany("INSERT OR REPLACE INTO rnp_ozon_funnel(cabinet, day, sku, views, tocart) VALUES(?,?,?,?,?)",
                          [(cabinet, day, sku, v, t) for (day, sku), (v, t) in rows.items()])
        self.store.set_setting(key, end.isoformat())

    def _sync_ads(self, client, cabinet, acc, days):
        fin = finance()
        key = f"rnp_ozon_ads_since:{cabinet}"
        start, end = self._start(key, days, 2), date.today()
        types = {}           # {type_id: {name, description}} — как fetch_types в ozon_finance.py
        res = self._post(client, acc, fin.URL_TYPES, {})
        for entry in res.get("accrual_types") or res.get("types") or []:
            tid = fin.as_int(entry.get("id", entry.get("type_id")))
            if tid is not None:
                types[tid] = {"name": entry.get("name") or "", "description": entry.get("description") or ""}
        skus = self._sku_map(cabinet)
        rows = defaultdict(float)
        day = start
        while day <= end:
            last_id = ""
            for _ in range(500):
                body = {"date": day.isoformat(), **({"last_id": last_id} if last_id else {})}
                res = self._post(client, acc, fin.URL_BY_DAY, body)
                batch = res.get("accruals") or []
                for accrual in batch:
                    for group, _label, value, _tid, sku in fin.split_accrual(accrual, types)[1]:
                        if group == fin.G_ADS and value:
                            article = (skus.get(sku) or f"SKU {sku}") if sku else NO_ARTICLE
                            rows[(day.isoformat(), article)] -= value          # начисление со знаком «−» — расход
                new_last = res.get("last_id") or ""
                if not batch or not new_last or new_last == last_id:
                    break
                last_id = new_last
            day += timedelta(days=1)
        with self.store._connect() as c:
            c.execute("DELETE FROM rnp_ozon_ads WHERE cabinet=? AND day>=?", (cabinet, start.isoformat()))
            c.executemany("INSERT OR REPLACE INTO rnp_ozon_ads(cabinet, day, article, spend) VALUES(?,?,?,?)",
                          [(cabinet, d, art, v) for (d, art), v in rows.items()])
        self.store.set_setting(key, end.isoformat())

    def _trim(self):
        edge = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
        with self.store._connect() as c:
            for table in ("rnp_ozon_orders", "rnp_ozon_funnel", "rnp_ozon_ads"):
                c.execute(f"DELETE FROM {table} WHERE day<?", (edge,))

    def _order_counts(self, start, end):
        with self.store._connect() as c:
            return c.execute("SELECT offer_id, SUM(qty) FROM rnp_ozon_orders WHERE day BETWEEN ? AND ? GROUP BY offer_id", (start, end)).fetchall()

    # ------------------------------------------------------------------ факты
    def facts(self, start: str, end: str, d=None, cabinets: set | None = None) -> dict:
        """{(день, артикул): метрики}: заказы, воронка и расходы на продвижение из своей загрузки; за дни без неё — из выгрузки Ozon-дашборда."""
        out = defaultdict(lambda: defaultdict(float))
        info = {}
        own_days, has_ads, has_funnel = set(), False, False
        cab_sql, cab_args = "", ()
        if cabinets:
            cab_sql, cab_args = f" AND cabinet IN ({','.join('?' * len(cabinets))})", tuple(sorted(cabinets))
        d = self._only(d, cabinets)
        with self.store._connect() as c:
            for day, art, sku, qty, amount, cancels in c.execute(
                    "SELECT day, offer_id, MAX(sku), SUM(qty), SUM(amount), SUM(CASE WHEN is_cancel=1 THEN qty ELSE 0 END) "
                    f"FROM rnp_ozon_orders WHERE day BETWEEN ? AND ?{cab_sql} GROUP BY day, offer_id", (start, end, *cab_args)):
                key = art_key(art)
                m = out[(day, key)]
                m["orders"] += qty or 0
                m["orders_sum"] += amount or 0
                m["cancels"] += cancels or 0
                own_days.add(day)
                info.setdefault(key, {"article": art, "nm_id": sku, "brand": "", "subject": ""})
            for day, art, sku, views, tocart in c.execute(
                    "SELECT f.day, COALESCE(s.offer_id, 'SKU ' || f.sku), MAX(f.sku), SUM(f.views), SUM(f.tocart) FROM rnp_ozon_funnel f "
                    "LEFT JOIN rnp_ozon_skus s ON s.cabinet=f.cabinet AND s.sku=f.sku "
                    f"WHERE f.day BETWEEN ? AND ?{cab_sql.replace('cabinet', 'f.cabinet')} GROUP BY 1, 2", (start, end, *cab_args)):
                has_funnel = True
                key = art_key(art)
                out[(day, key)]["card_views"] += views or 0
                out[(day, key)]["tocart"] += tocart or 0
                info.setdefault(key, {"article": art, "nm_id": sku, "brand": "", "subject": ""})
            for day, art, spend in c.execute(f"SELECT day, article, SUM(spend) FROM rnp_ozon_ads WHERE day BETWEEN ? AND ?{cab_sql} GROUP BY day, article",
                                             (start, end, *cab_args)):
                has_ads = True
                key = art_key(art) if art != NO_ARTICLE else NO_ARTICLE
                out[(day, key)]["ads"] += spend or 0
                info.setdefault(key, {"article": art, "nm_id": None, "brand": "", "subject": ""})
            has_ads = has_ads or bool(c.execute("SELECT 1 FROM rnp_ozon_ads LIMIT 1").fetchone())
            funnel_off = [k.split(":", 1)[1] for k, v in c.execute("SELECT key, value FROM app_settings WHERE key LIKE 'rnp_ozon_funnel_off:%'") if v]
        cache_days = self._from_cache(out, d, start, end, own_days, has_ads)
        return {"rows": out, "info": info,
                "sources": {"orders": "api" if own_days and not cache_days else ("mixed" if own_days else ("cache" if cache_days else "none")),
                            "ads": "api" if has_ads else "report", "ad_clicks": False, "funnel": has_funnel,
                            "funnel_off": sorted(set(funnel_off) & set(cabinets)) if cabinets else sorted(funnel_off)}}
