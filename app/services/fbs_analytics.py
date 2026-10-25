"""Аналитика FBS Wildberries: лента сборочных заданий со статусами, сроки отгрузки, склады, артикулы, % выкупа
и оценка скидки/штрафа к комиссии за скорость отгрузки.

Источники (API WB):
  • «Маркетплейс»: сборочные задания /api/v3/orders, статусы /api/v3/orders/status, поставки /api/v3/supplies/{id}
    (scanDt — когда WB принял поставку: это момент «отгрузки» и «до СЦ»);
  • «Статистика»: продажи /api/v1/supplier/sales — дата выкупа (по srid = rid задания).
Скидка/штраф к комиссии WB за скорость отгрузки (с 31.08.2026): до 13 ч −5%, 13–42 ч −3,5%, 42–48 ч без изменений,
после 48 ч — прибавка к комиссии за каждый час с 48-го: 0,3%/ч (48–54 ч), 0,35%/ч (54–60 ч), 0,45%/ч (свыше 60 ч).
"""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import httpx

from app.services.nomenclature import art_key

MARKET = "https://marketplace-api.wildberries.ru"
STATS = "https://statistics-api.wildberries.ru"
MSK = timezone(timedelta(hours=3))
FIRST_LOAD_DAYS = 90
FINAL = {"sold", "canceled", "canceled_by_client", "declined_by_client", "defect", "canceled_by_carrier"}
STATUS_TITLES = {"new": "Новые", "assembly": "На сборке", "transit": "В пути", "pvz": "В ПВЗ", "sold": "Выкуплены",
                 "refused": "Отказ при получении", "client_cancel": "Отменены покупателем", "seller_cancel": "Отменены продавцом", "defect": "Брак"}
BANDS = [("lt13", "до 13 ч"), ("13_42", "13–42 ч"), ("42_48", "42–48 ч"), ("48_54", "48–54 ч"), ("54_60", "54–60 ч"), ("gt60", "больше 60 ч")]


class FbsError(Exception):
    pass


def status_group(supplier: str | None, wb: str | None) -> str:
    wb, supplier = wb or "", supplier or ""
    if wb == "sold":
        return "sold"
    if wb == "canceled_by_client":
        return "refused"
    if wb == "declined_by_client":
        return "client_cancel"
    if wb == "defect":
        return "defect"
    if wb in ("canceled", "canceled_by_carrier") or supplier in ("cancel", "cancel_carrier"):
        return "seller_cancel"
    if wb == "ready_for_pickup":
        return "pvz"
    if supplier == "complete" or wb in ("sorted", "accepted_by_carrier", "sent_to_carrier", "postponed_delivery"):
        return "transit"
    if supplier == "confirm":
        return "assembly"
    return "new"


def band(hours: float) -> str:
    if hours < 13:
        return "lt13"
    if hours < 42:
        return "13_42"
    if hours < 48:
        return "42_48"
    if hours < 54:
        return "48_54"
    if hours < 60:
        return "54_60"
    return "gt60"


def commission_effect(hours: float | None) -> float | None:
    """Изменение комиссии WB в процентных пунктах: минус — скидка, плюс — штраф."""
    if hours is None:
        return None
    if hours < 13:
        return -5.0
    if hours < 42:
        return -3.5
    if hours < 48:
        return 0.0
    rate = 0.3 if hours < 54 else 0.35 if hours < 60 else 0.45
    return round(rate * (hours - 48), 2)


def _dt(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=MSK)


class FbsAnalytics:
    def __init__(self, store, cabinets, catalog=lambda: {}):
        self.store = store
        self.cabinets = cabinets
        self.catalog = catalog
        self.state = {"status": "idle", "message": "", "at": None, "errors": []}
        self.warehouses: dict[tuple, str] = {}
        self._lock = threading.Lock()
        with store._connect() as c:
            c.execute("CREATE TABLE IF NOT EXISTS fbs_orders (cabinet_id TEXT, id INTEGER, rid TEXT, created_at TEXT, warehouse_id INTEGER, office_id INTEGER, "
                      "nm_id INTEGER, chrt_id INTEGER, article TEXT, price REAL, supply_id TEXT, cargo_type INTEGER, supplier_status TEXT, wb_status TEXT, "
                      "status_checked TEXT, scan_dt TEXT, closed_at TEXT, sale_dt TEXT, PRIMARY KEY(cabinet_id, id))")
            c.execute("CREATE INDEX IF NOT EXISTS fbs_orders_created ON fbs_orders(created_at)")
            c.execute("CREATE TABLE IF NOT EXISTS fbs_supplies (cabinet_id TEXT, id TEXT, created_at TEXT, closed_at TEXT, scan_dt TEXT, done INTEGER, "
                      "fetched_at TEXT, PRIMARY KEY(cabinet_id, id))")
            c.execute("CREATE TABLE IF NOT EXISTS fbs_warehouses (cabinet_id TEXT, id INTEGER, name TEXT, PRIMARY KEY(cabinet_id, id))")

    def wb_cabinets(self):
        return [c for c in self.cabinets() if c.marketplace == "wildberries" and c.token and getattr(c, "enabled", True)]

    # ------------------------------------------------------------------ загрузка из WB
    def _req(self, client, method, url, token, need, **kw):
        for attempt in range(6):
            try:
                r = client.request(method, url, headers={"Authorization": token}, **kw)
            except httpx.HTTPError as exc:
                if attempt < 5:
                    time.sleep(3)
                    continue
                raise FbsError(f"нет связи с WB ({type(exc).__name__})")
            if r.status_code == 429 and attempt < 5:
                time.sleep(float(r.headers.get("X-Ratelimit-Retry") or 0) or (5 if need != "Статистика" else 61))
                continue
            if r.status_code in (401, 403):
                raise FbsError(f"у ключа API нет доступа к категории «{need}»")
            if r.status_code >= 500 and attempt < 5:
                time.sleep(5)
                continue
            if r.status_code >= 400:
                raise FbsError(f"WB ответил {r.status_code}: {r.text[:200]}")
            return r.json() if r.content.strip() else {}
        raise FbsError("WB ограничил частоту запросов — попробуйте позже")

    def sync(self) -> dict:
        with self._lock:
            if self.state["status"] == "running":
                return dict(self.state)
            self.state = {"status": "running", "message": "Загружаю сборочные задания…", "at": None, "errors": []}
        errors = []
        try:
            cabs = self.wb_cabinets()
            if not cabs:
                raise FbsError("Нет кабинета WB с ключом API")
            with httpx.Client(timeout=90) as client:
                for cab in cabs:
                    for part, fn in (("задания", self._sync_orders), ("статусы", self._sync_statuses), ("поставки", self._sync_supplies),
                                     ("выкупы", self._sync_sales)):
                        self.state["message"] = f"{cab.name}: {part}…"
                        try:
                            fn(client, cab)
                        except FbsError as exc:
                            errors.append(f"{cab.name}, {part}: {exc}")
                            if part == "задания":
                                break
            self.store.set_setting("fbs_analytics_synced_at", datetime.now(timezone.utc).isoformat())
            self.state = {"status": "done" if not errors else "partial", "message": "Готово" if not errors else "Загружено не всё",
                          "at": time.time(), "errors": errors}
        except Exception as exc:
            self.state = {"status": "error", "message": str(exc)[:300], "at": time.time(), "errors": errors}
        return dict(self.state)

    def _sync_orders(self, client, cab):
        whs = self._req(client, "GET", MARKET + "/api/v3/warehouses", cab.token, "Маркетплейс") or []
        with self.store._connect() as c:
            c.executemany("INSERT OR REPLACE INTO fbs_warehouses(cabinet_id, id, name) VALUES(?,?,?)", [(cab.id, w["id"], w.get("name") or str(w["id"])) for w in whs])
            last = c.execute("SELECT MAX(created_at) FROM fbs_orders WHERE cabinet_id=?", (cab.id,)).fetchone()[0]
            # задания без поставки, которые ещё в работе: поставку им назначают позже — перечитать с самого раннего
            open_ = c.execute(f"SELECT MIN(created_at) FROM fbs_orders WHERE cabinet_id=? AND supply_id IS NULL AND created_at>=? AND "
                              f"(wb_status IS NULL OR wb_status NOT IN ({','.join('?' * len(FINAL))}))",
                              (cab.id, (datetime.now(timezone.utc) - timedelta(days=29)).strftime("%Y-%m-%d"), *FINAL)).fetchone()[0]
        start = (_dt(last) - timedelta(days=2)) if last else datetime.now(timezone.utc) - timedelta(days=FIRST_LOAD_DAYS)
        if open_ and _dt(open_) and _dt(open_) < start:
            start = _dt(open_) - timedelta(minutes=1)
        end = datetime.now(timezone.utc)
        while start < end:
            chunk_end = min(end, start + timedelta(days=29))
            nxt = 0
            for _ in range(200):
                d = self._req(client, "GET", MARKET + "/api/v3/orders", cab.token, "Маркетплейс",
                              params={"limit": 1000, "next": nxt, "dateFrom": int(start.timestamp()), "dateTo": int(chunk_end.timestamp())})
                orders = d.get("orders") or []
                with self.store._connect() as c:
                    c.executemany(
                        "INSERT INTO fbs_orders(cabinet_id, id, rid, created_at, warehouse_id, office_id, nm_id, chrt_id, article, price, supply_id, cargo_type) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(cabinet_id, id) DO UPDATE SET supply_id=COALESCE(excluded.supply_id, fbs_orders.supply_id), "
                        "price=excluded.price",
                        [(cab.id, o["id"], o.get("rid") or "", o.get("createdAt") or "", o.get("warehouseId"), o.get("officeId"), o.get("nmId"), o.get("chrtId"),
                          str(o.get("article") or ""), (o.get("convertedPrice") or o.get("price") or 0) / 100, o.get("supplyId") or None, o.get("cargoType"))
                         for o in orders])
                nxt = d.get("next") or 0
                if len(orders) < 1000 or not nxt:
                    break
                time.sleep(0.25)
            start = chunk_end

    def _sync_statuses(self, client, cab):
        edge = (datetime.now(timezone.utc) - timedelta(days=FIRST_LOAD_DAYS)).isoformat()
        with self.store._connect() as c:
            ids = [i for (i,) in c.execute(f"SELECT id FROM fbs_orders WHERE cabinet_id=? AND created_at>=? AND (wb_status IS NULL OR wb_status NOT IN "
                                           f"({','.join('?' * len(FINAL))}))", (cab.id, edge, *FINAL))]
        now = datetime.now(timezone.utc).isoformat()
        for i in range(0, len(ids), 1000):
            d = self._req(client, "POST", MARKET + "/api/v3/orders/status", cab.token, "Маркетплейс", json={"orders": ids[i:i + 1000]})
            with self.store._connect() as c:
                c.executemany("UPDATE fbs_orders SET supplier_status=?, wb_status=?, status_checked=? WHERE cabinet_id=? AND id=?",
                              [(o.get("supplierStatus"), o.get("wbStatus"), now, cab.id, o["id"]) for o in d.get("orders") or []])
            time.sleep(0.25)

    def _sync_supplies(self, client, cab):
        with self.store._connect() as c:
            todo = [s for (s,) in c.execute(
                "SELECT DISTINCT o.supply_id FROM fbs_orders o LEFT JOIN fbs_supplies s ON s.cabinet_id=o.cabinet_id AND s.id=o.supply_id "
                "WHERE o.cabinet_id=? AND o.supply_id IS NOT NULL AND (s.id IS NULL OR (s.scan_dt IS NULL AND COALESCE(s.fetched_at,'')<?))",
                (cab.id, (datetime.now(timezone.utc) - timedelta(minutes=25)).isoformat()))]
        now = datetime.now(timezone.utc).isoformat()
        for sid in todo[:600]:
            try:
                s = self._req(client, "GET", f"{MARKET}/api/v3/supplies/{sid}", cab.token, "Маркетплейс")
            except FbsError as exc:
                if "404" in str(exc):
                    continue
                raise
            with self.store._connect() as c:
                c.execute("INSERT OR REPLACE INTO fbs_supplies(cabinet_id, id, created_at, closed_at, scan_dt, done, fetched_at) VALUES(?,?,?,?,?,?,?)",
                          (cab.id, sid, s.get("createdAt"), s.get("closedAt"), s.get("scanDt"), int(bool(s.get("done"))), now))
                c.execute("UPDATE fbs_orders SET scan_dt=?, closed_at=? WHERE cabinet_id=? AND supply_id=?", (s.get("scanDt"), s.get("closedAt"), cab.id, sid))
            time.sleep(0.22)

    def _sync_sales(self, client, cab):
        with self.store._connect() as c:
            first = c.execute("SELECT MIN(created_at) FROM fbs_orders WHERE cabinet_id=? AND wb_status='sold' AND sale_dt IS NULL", (cab.id,)).fetchone()[0]
        if not first:
            return
        since = (_dt(first).astimezone(MSK) - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
        rows = self._req(client, "GET", STATS + "/api/v1/supplier/sales", cab.token, "Статистика", params={"dateFrom": since, "flag": 0}) or []
        sold = {}
        for r in rows:
            if str(r.get("saleID") or "").startswith("S") and r.get("srid"):
                sold.setdefault(r["srid"], r.get("date"))
        if sold:
            with self.store._connect() as c:
                c.executemany("UPDATE fbs_orders SET sale_dt=? WHERE cabinet_id=? AND rid=? AND sale_dt IS NULL", [(d, cab.id, srid) for srid, d in sold.items()])

    # ------------------------------------------------------------------ выборка
    def _rows(self, start: str, end: str, f: dict) -> list:
        """Задания, созданные с start по end (даты по Москве) с фильтрами."""
        lo = datetime.fromisoformat(start).replace(tzinfo=MSK).astimezone(timezone.utc).isoformat()
        hi = (datetime.fromisoformat(end) + timedelta(days=1)).replace(tzinfo=MSK).astimezone(timezone.utc).isoformat()
        names = {c.id: c.name for c in self.cabinets()}
        with self.store._connect() as c:
            whs = {(cid, i): n for cid, i, n in c.execute("SELECT cabinet_id, id, name FROM fbs_warehouses")}
            data = c.execute("SELECT cabinet_id, id, rid, created_at, warehouse_id, nm_id, chrt_id, article, price, supply_id, supplier_status, wb_status, "
                             "scan_dt, closed_at, sale_dt FROM fbs_orders WHERE replace(created_at,'Z','+00:00')>=? AND replace(created_at,'Z','+00:00')<?",
                             (lo, hi)).fetchall()
        cat = self.catalog() or {}
        q = (f.get("q") or "").strip().lower()
        out = []
        for cid, oid, rid, created, wh, nm, chrt, art, price, sup, ss, ws, scan, closed, sale in data:
            if f.get("cabinet") and cid != f["cabinet"]:
                continue
            if f.get("warehouse") and str(wh) != str(f["warehouse"]):
                continue
            info = cat.get(art_key(art)) or {}
            if f.get("brand") and info.get("brand") != f["brand"]:
                continue
            if f.get("category") and info.get("category") != f["category"]:
                continue
            if q and q not in f"{oid} {rid} {art} {nm} {sup or ''}".lower():
                continue
            c_dt, s_dt, sale_d = _dt(created), _dt(scan), _dt(sale)
            hours = (s_dt - c_dt).total_seconds() / 3600 if c_dt and s_dt and s_dt > c_dt else None
            to_buy = (sale_d - s_dt).total_seconds() / 86400 if sale_d and s_dt and sale_d > s_dt else None
            g = status_group(ss, ws)
            eff = commission_effect(hours)
            out.append({"cabinet_id": cid, "cabinet": names.get(cid, cid), "id": oid, "rid": rid, "created_at": created,
                        "day": c_dt.astimezone(MSK).date().isoformat() if c_dt else "", "weekday": c_dt.astimezone(MSK).weekday() if c_dt else None,
                        "warehouse_id": wh, "warehouse": whs.get((cid, wh), str(wh)), "nm_id": nm, "chrt_id": chrt, "article": art, "key": art_key(art),
                        "brand": info.get("brand") or "", "category": info.get("category") or "", "title": info.get("name") or "",
                        "price": price or 0, "supply_id": sup or "", "supplier_status": ss or "", "wb_status": ws or "", "status": g,
                        "scan_dt": scan, "sale_dt": sale, "hours_to_sc": round(hours, 1) if hours is not None else None,
                        "band": band(hours) if hours is not None else None, "days_to_buyout": round(to_buy, 1) if to_buy is not None else None,
                        "effect_pct": eff, "effect_rub": round((price or 0) * eff / 100, 2) if eff is not None else None,
                        # упущенная выгода: сколько сэкономили бы, если бы WB принял задание быстрее 13 ч (−5%)
                        "lost_rub": round((price or 0) * (eff + 5) / 100, 2) if eff is not None else None})
        return out

    @staticmethod
    def _summary(rows: list) -> dict:
        n = len(rows)
        st = defaultdict(int)
        for r in rows:
            st[r["status"]] += 1
        shipped = [r for r in rows if r["hours_to_sc"] is not None]
        buy = [r for r in rows if r["days_to_buyout"] is not None]
        finished = st["sold"] + st["refused"]
        bands = defaultdict(int)
        for r in shipped:
            bands[r["band"]] += 1
        eff = [r["effect_rub"] for r in shipped if r["effect_rub"] is not None]
        return {"orders": n, "orders_sum": round(sum(r["price"] for r in rows), 2), "statuses": dict(st),
                "sold": st["sold"], "sold_sum": round(sum(r["price"] for r in rows if r["status"] == "sold"), 2),
                "buyout_pct": round(st["sold"] / finished * 100, 1) if finished else None,
                "cancel_pct": round((st["seller_cancel"] + st["client_cancel"]) / n * 100, 1) if n else None,
                "in_progress": st["new"] + st["assembly"] + st["transit"] + st["pvz"],
                "avg_hours_to_sc": round(sum(r["hours_to_sc"] for r in shipped) / len(shipped), 1) if shipped else None,
                "avg_days_to_buyout": round(sum(r["days_to_buyout"] for r in buy) / len(buy), 1) if buy else None,
                "shipped": len(shipped), "bands": dict(bands),
                "on_time_pct": round(sum(1 for r in shipped if r["hours_to_sc"] < 48) / len(shipped) * 100, 1) if shipped else None,
                "fast_pct": round(bands["lt13"] / len(shipped) * 100, 1) if shipped else None,
                "effect_rub": round(sum(eff), 2) if eff else None,
                "late": sum(1 for r in shipped if r["hours_to_sc"] >= 48),
                **FbsAnalytics._lost(shipped)}

    @staticmethod
    def _lost(shipped: list) -> dict:
        """Упущенная выгода против отгрузки до 13 ч: недополученная скидка (до 48 ч) + штрафы (после 48 ч)."""
        slow = [r for r in shipped if r["hours_to_sc"] >= 13]
        disc = sum(r["price"] * (5 + min(r["effect_pct"], 0)) / 100 for r in slow)
        pen = sum(r["price"] * max(r["effect_pct"], 0) / 100 for r in slow)
        by_band = defaultdict(lambda: {"orders": 0, "sum": 0.0, "lost": 0.0})
        for r in slow:
            b = by_band[r["band"]]
            b["orders"] += 1
            b["sum"] += r["price"]
            b["lost"] += r["lost_rub"]
        return {"lost_rub": round(disc + pen, 2), "lost_discount": round(disc, 2), "lost_penalty": round(pen, 2),
                "lost_orders": len(slow), "lost_sum": round(sum(r["price"] for r in slow), 2),
                "lost_pct": round(len(slow) / len(shipped) * 100, 1) if shipped else None,
                "potential_rub": round(-sum(r["price"] for r in shipped) * 0.05, 2),
                "avg_hours_slow": round(sum(r["hours_to_sc"] for r in slow) / len(slow), 1) if slow else None,
                "lost_bands": {k: {"orders": v["orders"], "sum": round(v["sum"], 2), "lost": round(v["lost"], 2)} for k, v in by_band.items()}}

    def report(self, start: str, end: str, f: dict, prev: tuple | None = None) -> dict:
        rows = self._rows(start, end, f)
        days = []
        d = date.fromisoformat(start)
        while d <= date.fromisoformat(end):
            days.append(d.isoformat())
            d += timedelta(days=1)
        by_day = {x: defaultdict(int) for x in days}
        hours_day = defaultdict(list)
        lost_day = defaultdict(lambda: [0.0, 0.0])
        for r in rows:
            if r["lost_rub"] is not None and r["day"] in by_day:
                lost_day[r["day"]][0 if r["effect_pct"] <= 0 else 1] += r["lost_rub"]
            if r["day"] in by_day:
                by_day[r["day"]][r["status"]] += 1
                by_day[r["day"]]["orders"] += 1
                if r["hours_to_sc"] is not None:
                    hours_day[r["day"]].append(r["hours_to_sc"])
        day_rows = []
        for x in days:
            m = by_day[x]
            fin = m["sold"] + m["refused"]
            h = hours_day.get(x) or []
            day_rows.append({"day": x, "orders": m["orders"], "statuses": {k: m[k] for k in STATUS_TITLES if m[k]},
                             "buyout_pct": round(m["sold"] / fin * 100, 1) if fin else None, "avg_hours_to_sc": round(sum(h) / len(h), 1) if h else None,
                             "lost_discount": round(lost_day[x][0], 2), "lost_penalty": round(lost_day[x][1], 2)})
        wd = defaultdict(lambda: {"orders": 0, "hours": [], "sold": 0, "fin": 0})
        for r in rows:
            if r["weekday"] is None:
                continue
            w = wd[r["weekday"]]
            w["orders"] += 1
            if r["hours_to_sc"] is not None:
                w["hours"].append(r["hours_to_sc"])
            if r["status"] in ("sold", "refused"):
                w["fin"] += 1
                w["sold"] += r["status"] == "sold"
        weekdays = [{"weekday": i, "orders": wd[i]["orders"], "avg_hours_to_sc": round(sum(wd[i]["hours"]) / len(wd[i]["hours"]), 1) if wd[i]["hours"] else None,
                     "buyout_pct": round(wd[i]["sold"] / wd[i]["fin"] * 100, 1) if wd[i]["fin"] else None} for i in range(7)]

        def group(keyf, meta):
            gs = defaultdict(list)
            for r in rows:
                gs[keyf(r)].append(r)
            out = []
            for k, rs in gs.items():
                out.append({**meta(rs[0]), **self._summary(rs)})
            return sorted(out, key=lambda x: -x["orders"])
        warehouses = group(lambda r: (r["cabinet_id"], r["warehouse_id"]), lambda r: {"cabinet": r["cabinet"], "warehouse_id": r["warehouse_id"], "warehouse": r["warehouse"]})
        articles = group(lambda r: (r["cabinet_id"], r["nm_id"]), lambda r: {"cabinet": r["cabinet"], "nm_id": r["nm_id"], "article": r["article"], "title": r["title"],
                                                                            "brand": r["brand"], "category": r["category"]})
        prev_summary = self._summary(self._rows(prev[0], prev[1], f)) if prev else None
        with self.store._connect() as c:
            whs = [{"cabinet_id": cid, "id": i, "name": n} for cid, i, n in c.execute("SELECT cabinet_id, id, name FROM fbs_warehouses ORDER BY name")]
        cat = self.catalog() or {}
        return {"days": days, "summary": self._summary(rows), "previous": prev_summary, "prev_period": list(prev) if prev else None,
                "by_day": day_rows, "weekdays": weekdays, "warehouses": warehouses, "articles": articles,
                "options": {"cabinets": [{"id": c.id, "name": c.name} for c in self.wb_cabinets()], "warehouses": whs,
                            "brands": sorted({v.get("brand") for v in cat.values() if v.get("brand")}),
                            "categories": sorted({v.get("category") for v in cat.values() if v.get("category")})},
                "titles": STATUS_TITLES, "bands": BANDS, "state": dict(self.state),
                "synced_at": self.store.get_setting("fbs_analytics_synced_at", "")}

    def feed(self, start: str, end: str, f: dict, status: str = "", sort: str = "created_at", desc: bool = True, limit: int = 100, offset: int = 0) -> dict:
        rows = self._rows(start, end, f)
        counts = defaultdict(int)
        for r in rows:
            counts[r["status"]] += 1
        if status == "late":
            rows = [r for r in rows if (r["hours_to_sc"] or 0) >= 48]
        elif status:
            rows = [r for r in rows if r["status"] == status]
        keyf = {"created_at": lambda r: r["created_at"], "price": lambda r: r["price"], "hours_to_sc": lambda r: r["hours_to_sc"] if r["hours_to_sc"] is not None else -1,
                "article": lambda r: r["article"], "warehouse": lambda r: r["warehouse"], "effect_rub": lambda r: r["effect_rub"] if r["effect_rub"] is not None else -1e9,
                "lost_rub": lambda r: r["lost_rub"] if r["lost_rub"] is not None else -1e9}
        rows.sort(key=keyf.get(sort, keyf["created_at"]), reverse=desc)
        return {"total": len(rows), "counts": dict(counts), "items": rows[offset:offset + limit]}
