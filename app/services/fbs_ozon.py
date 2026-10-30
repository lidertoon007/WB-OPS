"""Аналитика FBS Ozon: отправления со статусами, время до передачи в доставку, опоздания и просрочки относительно
срока отгрузки (shipment_date), склады, артикулы, отмены и выкуп, скидка Ozon за скорость отгрузки и штрафы за сроки.

Источники (Seller API):
  • отправления — /v4/posting/fbs/list (/v3/posting/fbs/list Ozon отключил: 404), курсор, limit ≤ 100;
    отправления за последние REFRESH_DAYS дней и все незавершённые перечитываются — статус меняется;
  • детали — /v3/posting/fbs/get: fact_delivery_date — фактическая передача в доставку (точнее delivering_date);
  • скидка за скорость — tariffication_fact отправления (discount — скидка, tariff_rate 0.02 = 2% от цены);
  • штрафы — начисления /v1/finance/accrual/by-day (NON_ITEM, unit_number = номер отправления), виды FINE_NAMES.
    Закрытые дни берутся из кэша ОПиУ (opiu/Ozon/cache/<Client ID>/<день>.json), если он есть.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import httpx

from app.services.nomenclature import art_key
from app.services.rnp_ozon import FINANCE_SCRIPT, finance

API_HOST = "https://api-seller.ozon.ru"
URL_LIST = "/v4/posting/fbs/list"
URL_GET = "/v3/posting/fbs/get"
MSK = timezone(timedelta(hours=3))
FIRST_LOAD_DAYS = 90
REFRESH_DAYS = 21
DETAILS_PER_SYNC = 1500            # новые первыми; остальные дойдут в следующие загрузки (до тех пор — delivering_date)
FINES_EVERY_HOURS = 3
CACHE_AFTER_DAYS = 7                 # как в ozon_finance.py: дни старше недели в кэше ОПиУ закрыты
SYNCED_KEY = "fbs_ozon_synced_at"
FINAL = {"delivered", "cancelled"}
SHIPPED = {"delivering", "driver_pickup", "delivered", "sent_by_seller", "arbitration", "client_arbitration"}
OPEN = {"awaiting_registration", "acceptance_in_progress", "awaiting_approve", "awaiting_packaging", "awaiting_deliver"}
# Штрафы Ozon, связанные со сроками и ошибками отгрузки (справочник /v1/finance/accrual/types)
FINE_NAMES = {"DefectRate", "DefectFineShipmentDelayRate", "DefectFineErrors"}
FINE_WORDS = re.compile(r"просроч|опоздан|срок\w* отгруз|наруш\w* срок", re.I)
STATUS_TITLES = {"new": "Новые", "assembly": "На сборке", "ready": "Ждут отгрузки", "transit": "В доставке", "pvz": "В ПВЗ",
                 "sold": "Доставлены", "refused": "Невыкуп / возврат", "client_cancel": "Отменены покупателем",
                 "seller_cancel": "Отменены продавцом", "ozon_cancel": "Отменены Ozon", "arbitration": "Спор"}
BANDS = [("lt12", "до 12 ч"), ("12_24", "12–24 ч"), ("24_48", "24–48 ч"), ("gt48", "больше 48 ч")]
LATE_BANDS = [("early", "раньше срока"), ("l2", "опоздание до 2 ч"), ("l24", "2–24 ч"), ("gt24", "больше суток")]


class FbsOzonError(Exception):
    pass


def status_group(status: str | None, substatus: str | None = "", cancel_type: str | None = "", after_ship: bool = False) -> str:
    status, substatus, cancel_type = status or "", substatus or "", (cancel_type or "").lower()
    if status == "delivered":
        return "sold"
    if status == "cancelled":
        if after_ship:
            return "refused"
        if cancel_type in ("client", "customer", "buyer"):
            return "client_cancel"
        if cancel_type == "ozon":
            return "ozon_cancel"
        return "seller_cancel"
    if status in ("arbitration", "client_arbitration", "not_accepted"):
        return "arbitration"
    if substatus == "posting_in_pickup_point":
        return "pvz"
    if status in ("delivering", "driver_pickup", "sent_by_seller"):
        return "transit"
    if status == "awaiting_deliver":
        return "ready"
    if status == "awaiting_packaging":
        return "assembly"
    return "new"


def band(hours: float) -> str:
    return "lt12" if hours < 12 else "12_24" if hours < 24 else "24_48" if hours < 48 else "gt48"


def late_band(late_hours: float) -> str:
    return "early" if late_hours <= 0 else "l2" if late_hours <= 2 else "l24" if late_hours <= 24 else "gt24"


def tariff_effect(tariff_type: str | None, rate) -> float | None:
    """Изменение стоимости за скорость отгрузки, % от цены: минус — скидка Ozon, плюс — наценка. None — тарифа нет."""
    if not tariff_type:
        return None
    try:
        r = abs(float(rate or 0))
    except (TypeError, ValueError):
        r = 0.0
    pct = r * 100 if r <= 0.5 else r            # в tariffication_fact доля (0.02), в tariffication — проценты (3)
    if tariff_type == "no_discount" or not pct:
        return 0.0
    return round(-pct if tariff_type == "discount" else pct, 2)


def is_fine(info: dict) -> bool:
    return info.get("name") in FINE_NAMES or bool(FINE_WORDS.search(f"{info.get('name', '')} {info.get('description', '')}"))


def _dt(s):
    if not s or str(s).startswith("1970"):
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _iso(d):
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if d else None


class FbsOzon:
    def __init__(self, store, cabinets, catalog=lambda: {}, cache_root=None):
        self.store = store
        self.cabinets = cabinets
        self.catalog = catalog
        self.cache_root = cache_root if cache_root is not None else FINANCE_SCRIPT.parent / "cache"
        self.state = {"status": "idle", "message": "", "at": None, "errors": []}
        self._lock = threading.Lock()
        with store._connect() as c:
            c.execute("CREATE TABLE IF NOT EXISTS fbs_ozon_postings (cabinet_id TEXT, posting TEXT, order_number TEXT, created_at TEXT, shipment_date TEXT, "
                      "delivering_date TEXT, fact_delivery_date TEXT, status TEXT, substatus TEXT, cancel_type TEXT, cancel_reason TEXT, "
                      "cancel_after_ship INTEGER, warehouse_id INTEGER, warehouse TEXT, offer_id TEXT, sku INTEGER, name TEXT, qty INTEGER, price REAL, "
                      "products TEXT, tariff_type TEXT, tariff_rate REAL, detail_checked TEXT, PRIMARY KEY(cabinet_id, posting))")
            c.execute("CREATE INDEX IF NOT EXISTS fbs_ozon_postings_created ON fbs_ozon_postings(created_at)")
            c.execute("CREATE TABLE IF NOT EXISTS fbs_ozon_fines (cabinet_id TEXT, accrual_id TEXT, type_id INTEGER, day TEXT, posting TEXT, "
                      "type_name TEXT, label TEXT, amount REAL, PRIMARY KEY(cabinet_id, accrual_id, type_id))")
            c.execute("CREATE INDEX IF NOT EXISTS fbs_ozon_fines_posting ON fbs_ozon_fines(posting)")

    def ozon_cabinets(self):
        out, seen = [], set()
        for c in self.cabinets():
            if c.marketplace == "ozon" and c.token and c.client_id and getattr(c, "enabled", True) and c.client_id not in seen:
                seen.add(c.client_id)
                out.append(c)
        return out

    # ------------------------------------------------------------------ загрузка из Ozon
    def _post(self, client, cab, path, body):
        for attempt in range(6):
            try:
                r = client.post(API_HOST + path, json=body, headers={"Client-Id": str(cab.client_id), "Api-Key": cab.token})
            except httpx.HTTPError as exc:
                if attempt < 5:
                    time.sleep(3)
                    continue
                raise FbsOzonError(f"нет связи с Ozon ({type(exc).__name__}); если включён VPN — отключите его")
            if r.status_code == 429 and attempt < 5:
                try:
                    time.sleep(min(max(float(r.headers.get("Retry-After") or 0), 1.0), 120.0))
                except ValueError:
                    time.sleep(5)
                continue
            if r.status_code >= 500 and attempt < 5:
                time.sleep(5)
                continue
            if r.status_code in (401, 403):
                raise FbsOzonError(f"Ozon не дал доступ к {path} — проверьте Client ID и роль API-ключа")
            if r.status_code >= 400:
                raise FbsOzonError(f"Ozon ответил {r.status_code} на {path}: {r.text[:200]}")
            data = r.json() if r.content.strip() else {}
            return data.get("result", data) if isinstance(data, dict) else {}
        raise FbsOzonError("Ozon ограничил частоту запросов — попробуйте позже")

    def sync(self) -> dict:
        with self._lock:
            if self.state["status"] == "running":
                return dict(self.state)
            self.state = {"status": "running", "message": "Загружаю отправления FBS…", "at": None, "errors": []}
        errors = []
        try:
            cabs = self.ozon_cabinets()
            if not cabs:
                raise FbsOzonError("Нет кабинета Ozon с Client ID и API-ключом")
            with httpx.Client(timeout=90) as client:
                for cab in cabs:
                    for part, fn in (("отправления", self._sync_postings), ("детали", self._sync_details), ("штрафы", self._sync_fines)):
                        self.state["message"] = f"{cab.name}: {part}…"
                        try:
                            fn(client, cab)
                        except FbsOzonError as exc:
                            errors.append(f"{cab.name}, {part}: {exc}")
                            if part == "отправления":
                                break
            self.store.set_setting(SYNCED_KEY, datetime.now(timezone.utc).isoformat())
            self.state = {"status": "done" if not errors else "partial", "message": "Готово" if not errors else "Загружено не всё",
                          "at": time.time(), "errors": errors}
        except Exception as exc:
            self.state = {"status": "error", "message": str(exc)[:300], "at": time.time(), "errors": errors}
        return dict(self.state)

    @staticmethod
    def _row(cab, p) -> tuple:
        fin = finance()
        products = p.get("products") or []
        price = sum(fin.money(pr.get("price")) * (fin.as_int(pr.get("quantity")) or 1) for pr in products)
        main = products[0] if products else {}
        dm = p.get("delivery_method") or {}
        cancel = p.get("cancellation") or {}
        fact = p.get("tariffication_fact") if isinstance(p.get("tariffication_fact"), dict) else {}
        return (cab.id, p.get("posting_number") or "", p.get("order_number") or "", _iso(_dt(p.get("in_process_at") or p.get("created_at"))) or "",
                _iso(_dt(p.get("shipment_date"))), _iso(_dt(p.get("delivering_date"))), p.get("status") or "", p.get("substatus") or "",
                cancel.get("cancellation_type") or "", cancel.get("cancel_reason") or "", int(bool(cancel.get("cancelled_after_ship"))),
                fin.as_int(dm.get("warehouse_id")), dm.get("warehouse") or "", main.get("offer_id") or (f"SKU {main.get('sku')}" if main else ""),
                fin.as_int(main.get("sku")), main.get("name") or "", sum(fin.as_int(pr.get("quantity")) or 1 for pr in products), round(price, 2),
                json.dumps([{"offer_id": pr.get("offer_id"), "sku": pr.get("sku"), "qty": fin.as_int(pr.get("quantity")) or 1,
                             "price": fin.money(pr.get("price"))} for pr in products], ensure_ascii=False),
                fact.get("tariff_type") or None, fin.money(fact.get("tariff_rate")) if fact else None)

    def _sync_postings(self, client, cab):
        now = datetime.now(timezone.utc)
        with self.store._connect() as c:
            last = c.execute("SELECT MAX(created_at) FROM fbs_ozon_postings WHERE cabinet_id=?", (cab.id,)).fetchone()[0]
            open_ = c.execute(f"SELECT MIN(created_at) FROM fbs_ozon_postings WHERE cabinet_id=? AND created_at>=? AND status NOT IN "
                              f"({','.join('?' * len(FINAL))})", (cab.id, _iso(now - timedelta(days=FIRST_LOAD_DAYS)), *FINAL)).fetchone()[0]
        start = (_dt(last) - timedelta(days=REFRESH_DAYS)) if last else now - timedelta(days=FIRST_LOAD_DAYS)
        if open_ and _dt(open_) and _dt(open_) < start:
            start = _dt(open_) - timedelta(minutes=1)
        while start < now:
            chunk_end = min(now, start + timedelta(days=30))
            cursor = ""
            for _ in range(5000):
                res = self._post(client, cab, URL_LIST, {"cursor": cursor, "limit": 100, "sort_dir": "ASC",
                                                         "filter": {"since": _iso(start), "to": _iso(chunk_end)}, "with": {}})
                rows = [self._row(cab, p) for p in res.get("postings") or [] if p.get("posting_number")]
                with self.store._connect() as c:
                    c.executemany(
                        "INSERT INTO fbs_ozon_postings(cabinet_id, posting, order_number, created_at, shipment_date, delivering_date, status, substatus, "
                        "cancel_type, cancel_reason, cancel_after_ship, warehouse_id, warehouse, offer_id, sku, name, qty, price, products, tariff_type, tariff_rate) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(cabinet_id, posting) DO UPDATE SET "
                        "shipment_date=excluded.shipment_date, delivering_date=COALESCE(excluded.delivering_date, fbs_ozon_postings.delivering_date), "
                        "status=excluded.status, substatus=excluded.substatus, cancel_type=excluded.cancel_type, cancel_reason=excluded.cancel_reason, "
                        "cancel_after_ship=excluded.cancel_after_ship, warehouse_id=excluded.warehouse_id, warehouse=excluded.warehouse, price=excluded.price, "
                        "products=excluded.products, tariff_type=COALESCE(excluded.tariff_type, fbs_ozon_postings.tariff_type), "
                        "tariff_rate=COALESCE(excluded.tariff_rate, fbs_ozon_postings.tariff_rate)", rows)
                cursor = res.get("cursor") or ""
                if not res.get("has_next") or not cursor:
                    break
                time.sleep(0.1)
            start = chunk_end

    def _sync_details(self, client, cab):
        """fact_delivery_date из /v3/posting/fbs/get — для отгруженных отправлений, у которых её ещё нет."""
        edge = _iso(datetime.now(timezone.utc) - timedelta(hours=6))
        with self.store._connect() as c:
            todo = [p for (p,) in c.execute(
                f"SELECT posting FROM fbs_ozon_postings WHERE cabinet_id=? AND fact_delivery_date IS NULL AND "
                f"(delivering_date IS NOT NULL OR status IN ({','.join('?' * len(SHIPPED))}) OR cancel_after_ship=1) "
                f"AND COALESCE(detail_checked,'')<? ORDER BY created_at DESC LIMIT ?", (cab.id, *SHIPPED, edge, DETAILS_PER_SYNC))]
        for posting in todo:
            try:
                g = self._post(client, cab, URL_GET, {"posting_number": posting, "with": {}})
            except FbsOzonError as exc:
                if "404" in str(exc):
                    continue
                raise
            with self.store._connect() as c:
                c.execute("UPDATE fbs_ozon_postings SET fact_delivery_date=?, delivering_date=COALESCE(delivering_date, ?), detail_checked=? "
                          "WHERE cabinet_id=? AND posting=?", (_iso(_dt(g.get("fact_delivery_date"))), _iso(_dt(g.get("delivering_date"))),
                                                               _iso(datetime.now(timezone.utc)), cab.id, posting))
            time.sleep(0.05)

    def _accruals(self, client, cab, day: date) -> list:
        fin = finance()
        cached = self.cache_root / re.sub(r"[^\w-]", "_", str(cab.client_id)) / f"{day.isoformat()}.json"
        if day <= date.today() - timedelta(days=CACHE_AFTER_DAYS) and cached.exists():
            try:
                return json.loads(cached.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        out, last_id = [], ""
        for _ in range(500):
            res = self._post(client, cab, fin.URL_BY_DAY, {"date": day.isoformat(), **({"last_id": last_id} if last_id else {})})
            batch = res.get("accruals") or []
            out += batch
            new_last = res.get("last_id") or ""
            if not batch or not new_last or new_last == last_id:
                break
            last_id = new_last
        return out

    def _sync_fines(self, client, cab):
        key = f"fbs_ozon_fines_since:{cab.id}"
        stored = self.store.get_setting(key, "")
        at = self.store.get_setting(f"fbs_ozon_fines_at:{cab.id}", "")
        if at and _dt(at) and datetime.now(timezone.utc) - _dt(at) < timedelta(hours=FINES_EVERY_HOURS):
            return
        fin = finance()
        res = self._post(client, cab, fin.URL_TYPES, {})
        types = {}
        for entry in res.get("accrual_types") or res.get("types") or []:
            tid = fin.as_int(entry.get("id", entry.get("type_id")))
            if tid is not None:
                types[tid] = {"name": entry.get("name") or "", "description": entry.get("description") or ""}
        fines = {t for t, info in types.items() if is_fine(info)}
        today = date.today()
        day = max(today - timedelta(days=FIRST_LOAD_DAYS), date.fromisoformat(stored[:10]) - timedelta(days=CACHE_AFTER_DAYS)) if stored \
            else today - timedelta(days=FIRST_LOAD_DAYS)
        first = day
        rows = {}
        while day <= today:
            for a in self._accruals(client, cab, day):
                posting = fin.posting_number(a)
                for tid, amount, _sku in fin.components(a):
                    if tid in fines:
                        k = (str(a.get("accrual_id") or f"{day}:{posting}"), tid)
                        prev = rows.get(k)
                        rows[k] = (cab.id, k[0], tid, str(a.get("date") or day.isoformat())[:10], posting, types[tid]["name"],
                                   types[tid]["description"] or types[tid]["name"], round((prev[7] if prev else 0) + amount, 2))
            day += timedelta(days=1)
        with self.store._connect() as c:
            c.execute("DELETE FROM fbs_ozon_fines WHERE cabinet_id=? AND day>=?", (cab.id, first.isoformat()))
            c.executemany("INSERT OR REPLACE INTO fbs_ozon_fines(cabinet_id, accrual_id, type_id, day, posting, type_name, label, amount) "
                          "VALUES(?,?,?,?,?,?,?,?)", list(rows.values()))
        self.store.set_setting(key, today.isoformat())
        self.store.set_setting(f"fbs_ozon_fines_at:{cab.id}", datetime.now(timezone.utc).isoformat())

    # ------------------------------------------------------------------ выборка
    @staticmethod
    def _bounds(start: str, end: str):
        lo = datetime.fromisoformat(start).replace(tzinfo=MSK).astimezone(timezone.utc)
        hi = (datetime.fromisoformat(end) + timedelta(days=1)).replace(tzinfo=MSK).astimezone(timezone.utc)
        return lo, hi

    def _fine_map(self) -> dict:
        """{(кабинет, отправление): штрафы, ₽ (положительное число)}"""
        with self.store._connect() as c:
            return {(cid, p): -s for cid, p, s in c.execute("SELECT cabinet_id, posting, SUM(amount) FROM fbs_ozon_fines GROUP BY 1, 2")}

    def _rows(self, start: str, end: str, f: dict, now: datetime | None = None) -> list:
        """Отправления, созданные с start по end (даты по Москве) с фильтрами."""
        lo, hi = self._bounds(start, end)
        now = now or datetime.now(timezone.utc)
        names = {c.id: c.name for c in self.cabinets()}
        with self.store._connect() as c:
            data = c.execute("SELECT cabinet_id, posting, order_number, created_at, shipment_date, delivering_date, fact_delivery_date, status, substatus, "
                             "cancel_type, cancel_reason, cancel_after_ship, warehouse_id, warehouse, offer_id, sku, name, qty, price, tariff_type, tariff_rate, "
                             "products FROM fbs_ozon_postings WHERE created_at>=? AND created_at<?", (_iso(lo), _iso(hi))).fetchall()
        fines = self._fine_map()
        cat = self.catalog() or {}
        q = (f.get("q") or "").strip().lower()
        out = []
        for (cid, posting, order, created, ship_by, delivering, fact, status, sub, ctype, creason, after, wh, whn, offer, sku, name, qty, price,
             ttype, trate, products) in data:
            if f.get("cabinet") and cid != f["cabinet"]:
                continue
            if f.get("warehouse") and str(wh) != str(f["warehouse"]):
                continue
            info = cat.get(art_key(offer)) or {}
            if f.get("brand") and info.get("brand") != f["brand"]:
                continue
            if f.get("category") and info.get("category") != f["category"]:
                continue
            if q and q not in f"{posting} {order} {offer} {sku} {name} {products or ''}".lower():          # и остальные товары отправления
                continue
            c_dt, deadline, sent = _dt(created), _dt(ship_by), _dt(fact) or _dt(delivering)
            g = status_group(status, sub, ctype, bool(after))
            hours = (sent - c_dt).total_seconds() / 3600 if c_dt and sent and sent > c_dt else None
            late_h = (sent - deadline).total_seconds() / 3600 if sent and deadline and hours is not None else None
            overdue = sent is None and status in OPEN and deadline is not None and now > deadline
            eff = tariff_effect(ttype, trate)
            fine = round(fines.get((cid, posting), 0.0), 2)
            out.append({"cabinet_id": cid, "cabinet": names.get(cid, cid), "posting": posting, "order_number": order, "created_at": created,
                        "day": c_dt.astimezone(MSK).date().isoformat() if c_dt else "", "weekday": c_dt.astimezone(MSK).weekday() if c_dt else None,
                        "warehouse_id": wh, "warehouse": whn or str(wh or ""), "offer_id": offer, "sku": sku, "article": offer, "key": art_key(offer),
                        "brand": info.get("brand") or "", "category": info.get("category") or "", "title": name or info.get("name") or "",
                        "qty": qty or 0, "price": price or 0, "status": g, "ozon_status": status, "substatus": sub, "cancel_reason": creason,
                        "shipment_date": ship_by, "shipped_at": _iso(sent), "hours_to_ship": round(hours, 1) if hours is not None else None,
                        "band": band(hours) if hours is not None else None, "late_hours": round(late_h, 1) if late_h is not None else None,
                        "late": late_h is not None and late_h > 0, "late_band": late_band(late_h) if late_h is not None else None,
                        "overdue": overdue, "overdue_hours": round((now - deadline).total_seconds() / 3600, 1) if overdue else None,
                        "tariff_type": ttype or "", "effect_pct": eff, "effect_rub": round((price or 0) * eff / 100, 2) if eff is not None else None,
                        "fine_rub": fine})
        return out

    @staticmethod
    def _summary(rows: list) -> dict:
        n = len(rows)
        st = defaultdict(int)
        for r in rows:
            st[r["status"]] += 1
        shipped = [r for r in rows if r["hours_to_ship"] is not None]
        late = [r for r in shipped if r["late"]]
        finished = st["sold"] + st["refused"]
        bands, lbands = defaultdict(int), defaultdict(int)
        for r in shipped:
            bands[r["band"]] += 1
            lbands[r["late_band"]] += 1
        eff = [r for r in rows if r["effect_rub"] is not None]
        fined = [r for r in rows if r["fine_rub"]]
        cancels = st["seller_cancel"] + st["client_cancel"] + st["ozon_cancel"]
        return {"orders": n, "orders_sum": round(sum(r["price"] for r in rows), 2), "statuses": {k: v for k, v in st.items() if v},
                "sold": st["sold"], "sold_sum": round(sum(r["price"] for r in rows if r["status"] == "sold"), 2),
                "buyout_pct": round(st["sold"] / finished * 100, 1) if finished else None,
                "cancel_pct": round(cancels / n * 100, 1) if n else None, "cancels": cancels,
                "seller_cancel_pct": round(st["seller_cancel"] / n * 100, 1) if n else None,
                "in_progress": sum(st[k] for k in ("new", "assembly", "ready", "transit", "pvz", "arbitration")),
                "avg_hours_to_ship": round(sum(r["hours_to_ship"] for r in shipped) / len(shipped), 1) if shipped else None,
                "shipped": len(shipped), "bands": dict(bands), "late_bands": dict(lbands),
                "late": len(late), "on_time_pct": round((len(shipped) - len(late)) / len(shipped) * 100, 1) if shipped else None,
                "avg_late_hours": round(sum(r["late_hours"] for r in late) / len(late), 1) if late else None,
                "overdue": sum(1 for r in rows if r["overdue"]),
                "discount_pct": round(sum(1 for r in eff if r["effect_pct"] < 0) / len(eff) * 100, 1) if eff else None,
                "effect_rub": round(sum(r["effect_rub"] for r in eff), 2) if eff else None, "tariffed": len(eff),
                "fines_rub": round(sum(r["fine_rub"] for r in fined), 2), "fined": len(fined)}

    def _fines(self, start: str, end: str, f: dict) -> dict:
        """Штрафы за сроки по дате начисления: по видам, по дням и список."""
        with self.store._connect() as c:
            data = c.execute("SELECT f.cabinet_id, f.day, f.posting, f.type_id, f.type_name, f.label, f.amount, p.offer_id, p.warehouse_id, p.warehouse "
                             "FROM fbs_ozon_fines f LEFT JOIN fbs_ozon_postings p ON p.cabinet_id=f.cabinet_id AND p.posting=f.posting "
                             "WHERE f.day BETWEEN ? AND ?", (start, end)).fetchall()
        by_type = defaultdict(lambda: {"count": 0, "sum": 0.0})
        by_day = defaultdict(float)
        items = []
        for cid, day, posting, tid, tname, label, amount, offer, wh, whn in data:
            if f.get("cabinet") and cid != f["cabinet"]:
                continue
            if f.get("warehouse") and str(wh) != str(f["warehouse"]):
                continue
            t = by_type[(tid, label, tname)]
            t["count"] += 1
            t["sum"] -= amount
            by_day[day] -= amount
            items.append({"day": day, "posting": posting, "type": label, "amount": round(-amount, 2), "article": offer or "", "warehouse": whn or "",
                          "fbs": offer is not None})
        items.sort(key=lambda x: (x["day"], x["amount"]), reverse=True)
        return {"total": round(sum(x["amount"] for x in items), 2), "count": len(items), "fbs_total": round(sum(x["amount"] for x in items if x["fbs"]), 2),
                "by_type": sorted([{"type_id": k[0], "label": k[1], "name": k[2], "count": v["count"], "sum": round(v["sum"], 2)} for k, v in by_type.items()],
                                  key=lambda x: -x["sum"]),
                "by_day": {d: round(v, 2) for d, v in sorted(by_day.items())}, "items": items[:300]}

    def report(self, start: str, end: str, f: dict, prev: tuple | None = None) -> dict:
        rows = self._rows(start, end, f)
        days = []
        d = date.fromisoformat(start)
        while d <= date.fromisoformat(end):
            days.append(d.isoformat())
            d += timedelta(days=1)
        by_day = {x: defaultdict(int) for x in days}
        hours_day, late_day = defaultdict(list), defaultdict(int)
        for r in rows:
            if r["day"] in by_day:
                by_day[r["day"]][r["status"]] += 1
                by_day[r["day"]]["orders"] += 1
                if r["hours_to_ship"] is not None:
                    hours_day[r["day"]].append(r["hours_to_ship"])
                    late_day[r["day"]] += r["late"]
        fines = self._fines(start, end, f)
        day_rows = []
        for x in days:
            m, h = by_day[x], hours_day.get(x) or []
            fin = m["sold"] + m["refused"]
            day_rows.append({"day": x, "orders": m["orders"], "statuses": {k: m[k] for k in STATUS_TITLES if m[k]},
                             "buyout_pct": round(m["sold"] / fin * 100, 1) if fin else None, "avg_hours_to_ship": round(sum(h) / len(h), 1) if h else None,
                             "shipped": len(h), "late": late_day[x], "late_pct": round(late_day[x] / len(h) * 100, 1) if h else None,
                             "fines": fines["by_day"].get(x, 0.0)})
        wd = defaultdict(lambda: {"orders": 0, "hours": [], "late": 0, "sold": 0, "fin": 0})
        for r in rows:
            if r["weekday"] is None:
                continue
            w = wd[r["weekday"]]
            w["orders"] += 1
            if r["hours_to_ship"] is not None:
                w["hours"].append(r["hours_to_ship"])
                w["late"] += r["late"]
            if r["status"] in ("sold", "refused"):
                w["fin"] += 1
                w["sold"] += r["status"] == "sold"
        weekdays = [{"weekday": i, "orders": wd[i]["orders"], "avg_hours_to_ship": round(sum(wd[i]["hours"]) / len(wd[i]["hours"]), 1) if wd[i]["hours"] else None,
                     "late_pct": round(wd[i]["late"] / len(wd[i]["hours"]) * 100, 1) if wd[i]["hours"] else None,
                     "buyout_pct": round(wd[i]["sold"] / wd[i]["fin"] * 100, 1) if wd[i]["fin"] else None} for i in range(7)]

        def group(keyf, meta):
            gs = defaultdict(list)
            for r in rows:
                gs[keyf(r)].append(r)
            return sorted([{**meta(rs[0]), **self._summary(rs)} for rs in gs.values()], key=lambda x: -x["orders"])
        warehouses = group(lambda r: (r["cabinet_id"], r["warehouse_id"]), lambda r: {"cabinet": r["cabinet"], "warehouse_id": r["warehouse_id"], "warehouse": r["warehouse"]})
        articles = group(lambda r: (r["cabinet_id"], r["key"]), lambda r: {"cabinet": r["cabinet"], "sku": r["sku"], "article": r["article"], "title": r["title"],
                                                                         "brand": r["brand"], "category": r["category"]})
        prev_summary = self._summary(self._rows(prev[0], prev[1], f)) if prev else None
        with self.store._connect() as c:
            whs = [{"cabinet_id": cid, "id": i, "name": n} for cid, i, n in
                   c.execute("SELECT DISTINCT cabinet_id, warehouse_id, warehouse FROM fbs_ozon_postings WHERE warehouse_id IS NOT NULL ORDER BY warehouse")]
        cat = self.catalog() or {}
        return {"mp": "ozon", "days": days, "summary": self._summary(rows), "previous": prev_summary, "prev_period": list(prev) if prev else None,
                "by_day": day_rows, "weekdays": weekdays, "warehouses": warehouses, "articles": articles, "fines": fines,
                "options": {"cabinets": [{"id": c.id, "name": c.name} for c in self.ozon_cabinets()], "warehouses": whs,
                            "brands": sorted({v.get("brand") for v in cat.values() if v.get("brand")}),
                            "categories": sorted({v.get("category") for v in cat.values() if v.get("category")})},
                "titles": STATUS_TITLES, "bands": BANDS, "late_bands": LATE_BANDS, "state": dict(self.state),
                "synced_at": self.store.get_setting(SYNCED_KEY, "")}

    def feed(self, start: str, end: str, f: dict, status: str = "", sort: str = "created_at", desc: bool = True, limit: int = 100, offset: int = 0) -> dict:
        rows = self._rows(start, end, f)
        counts = defaultdict(int)
        for r in rows:
            counts[r["status"]] += 1
        extra = {"late": sum(r["late"] for r in rows), "overdue": sum(r["overdue"] for r in rows), "fined": sum(1 for r in rows if r["fine_rub"])}
        if status == "late":
            rows = [r for r in rows if r["late"]]
        elif status == "overdue":
            rows = [r for r in rows if r["overdue"]]
        elif status == "fined":
            rows = [r for r in rows if r["fine_rub"]]
        elif status:
            rows = [r for r in rows if r["status"] == status]
        keyf = {"created_at": lambda r: r["created_at"], "price": lambda r: r["price"], "article": lambda r: r["article"], "warehouse": lambda r: r["warehouse"],
                "hours_to_ship": lambda r: r["hours_to_ship"] if r["hours_to_ship"] is not None else -1,
                "late_hours": lambda r: r["late_hours"] if r["late_hours"] is not None else (r["overdue_hours"] if r["overdue"] else -1e9),
                "shipment_date": lambda r: r["shipment_date"] or "", "effect_rub": lambda r: r["effect_rub"] if r["effect_rub"] is not None else -1e9,
                "fine_rub": lambda r: r["fine_rub"]}
        rows.sort(key=keyf.get(sort, keyf["created_at"]), reverse=desc)
        return {"total": len(rows), "counts": dict(counts), "extra": extra, "items": rows[offset:offset + limit]}
