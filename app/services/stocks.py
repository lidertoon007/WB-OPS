"""Остатки FBS Wildberries: просмотр и быстрое пополнение по размерам, история, оповещения о заканчивающихся остатках.

API WB «Маркетплейс»: склады продавца (/api/v3/warehouses), остатки по размерам chrtId (/api/v3/stocks/{склад}).
Размеры и баркоды — из карточек (API «Контент»). Оповещение уходит один раз, когда остаток размера опускается
до порога (по умолчанию 5 шт.) и ещё раз — когда он закончился. После пополнения выше порога слежение начинается заново.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone

import httpx

from app.services.nomenclature import art_key

CONTENT = "https://content-api.wildberries.ru"
MARKET = "https://marketplace-api.wildberries.ru"
DEFAULTS = {"threshold": 5, "interval_min": 30, "monitor": True, "only_seen": True}
CARDS_TTL = 6 * 3600
DAYS = 14                    # «заказов в день» — среднее за столько полных дней


class StocksError(Exception):
    pass


class Stocks:
    def __init__(self, store, cabinets, catalog=lambda: {}):
        self.store = store
        self.cabinets = cabinets            # callable -> [Cabinet]
        self.catalog = catalog              # callable -> {art_key: {"name", "brand", "category", "wb_stock"}}
        self.cards: dict[str, dict] = {}    # cabinet_id -> {"at", "sizes": {chrt: {...}}}
        self.state: dict[str, dict] = {}    # cabinet_id -> {"at", "error", "warehouses"}
        self._lock = threading.Lock()
        with store._connect() as c:
            c.execute("CREATE TABLE IF NOT EXISTS fbs_sizes (cabinet_id TEXT, warehouse_id INTEGER, chrt_id INTEGER, nm_id INTEGER, vendor TEXT, "
                      "size TEXT, amount INTEGER, updated_at TEXT, threshold INTEGER, muted INTEGER DEFAULT 0, seen_positive INTEGER DEFAULT 0, "
                      "alert_state TEXT DEFAULT 'ok', alert_at TEXT, PRIMARY KEY(cabinet_id, warehouse_id, chrt_id))")
            c.execute("CREATE TABLE IF NOT EXISTS fbs_archive (cabinet_id TEXT, nm_id INTEGER, at TEXT, author TEXT, PRIMARY KEY(cabinet_id, nm_id))")
            c.execute("CREATE TABLE IF NOT EXISTS fbs_log (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, author TEXT, cabinet_id TEXT, warehouse_id INTEGER, "
                      "chrt_id INTEGER, nm_id INTEGER, vendor TEXT, size TEXT, old INTEGER, new INTEGER, status TEXT, error TEXT)")

    # ------------------------------------------------------------------ настройки
    def settings(self) -> dict:
        try:
            saved = json.loads(self.store.get_setting("fbs_settings", "") or "{}")
        except ValueError:
            saved = {}
        return {**DEFAULTS, **saved}

    def save_settings(self, patch: dict) -> dict:
        cur = self.settings()
        if "threshold" in patch:
            t = int(patch["threshold"])
            if not 0 <= t <= 10000:
                raise StocksError("Порог — от 0 до 10000 шт.")
            cur["threshold"] = t
        if "interval_min" in patch:
            i = int(patch["interval_min"])
            if not 5 <= i <= 1440:
                raise StocksError("Проверять — от 5 минут до суток")
            cur["interval_min"] = i
        for k in ("monitor", "only_seen"):
            if k in patch:
                cur[k] = bool(patch[k])
        self.store.set_setting("fbs_settings", json.dumps(cur))
        return cur

    def wb_cabinets(self) -> list:
        return [c for c in self.cabinets() if c.marketplace == "wildberries" and c.token and getattr(c, "enabled", True)]

    def _cabinet(self, cid):
        c = next((c for c in self.wb_cabinets() if c.id == cid), None)
        if not c:
            raise StocksError("Кабинет WB не найден или без ключа API")
        return c

    # ------------------------------------------------------------------ запросы к WB
    def _req(self, client, method, url, token, need, **kw):
        for attempt in range(5):
            try:
                r = client.request(method, url, headers={"Authorization": token}, **kw)
            except httpx.HTTPError as exc:
                if attempt < 4:
                    time.sleep(2)
                    continue
                raise StocksError(f"нет связи с WB ({type(exc).__name__})")
            if r.status_code == 429 and attempt < 4:
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code in (401, 403):
                raise StocksError(f"у ключа API нет доступа к категории «{need}»")
            if r.status_code >= 400:
                try:
                    body = r.json()
                    msg = body.get("message") or body.get("errorText") or body.get("detail") or r.text
                except ValueError:
                    msg = r.text
                raise StocksError(f"WB ответил {r.status_code}: {str(msg)[:300]}")
            return r.json() if r.content.strip() else {}
        raise StocksError("WB ограничил частоту запросов — попробуйте через минуту")

    def _load_cards(self, client, cab) -> dict:
        cached = self.cards.get(cab.id)
        if cached and time.time() - cached["at"] < CARDS_TTL:
            return cached["sizes"]
        sizes, cursor = {}, {"limit": 100}
        for _ in range(500):
            body = {"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}}
            d = self._req(client, "POST", CONTENT + "/content/v2/get/cards/list?locale=ru", cab.token, "Контент", json=body)
            cards = d.get("cards") or []
            for card in cards:
                photo = next((p.get("tm") or p.get("c246x328") or p.get("big") for p in card.get("photos") or []), "")
                for s in card.get("sizes") or []:
                    if s.get("chrtID"):
                        sizes[int(s["chrtID"])] = {"nm_id": card.get("nmID"), "vendor": str(card.get("vendorCode") or ""), "title": card.get("title") or "",
                                                   "brand": card.get("brand") or "", "subject": card.get("subjectName") or "", "photo": photo,
                                                   "size": "" if str(s.get("techSize") or "") == "0" else str(s.get("techSize") or ""),
                                                   "ru_size": str(s.get("wbSize") or ""), "barcodes": list(s.get("skus") or [])}
            cur = d.get("cursor") or {}
            if len(cards) < cursor["limit"] or not cur.get("nmID"):
                break
            cursor = {"limit": 100, "updatedAt": cur.get("updatedAt"), "nmID": cur.get("nmID")}
            time.sleep(0.7)
        self.cards[cab.id] = {"at": time.time(), "sizes": sizes}
        return sizes

    def refresh(self, cabinet_ids: list | None = None, reload_cards: bool = False) -> dict:
        out = {}
        for cab in self.wb_cabinets():
            if cabinet_ids and cab.id not in cabinet_ids:
                continue
            if reload_cards:
                self.cards.pop(cab.id, None)
            try:
                with httpx.Client(timeout=60) as client:
                    sizes = self._load_cards(client, cab)
                    whs = self._req(client, "GET", MARKET + "/api/v3/warehouses", cab.token, "Маркетплейс") or []
                    now = datetime.now(timezone.utc).isoformat()
                    chrts = list(sizes)
                    rows = []
                    for wh in whs:
                        amounts = {}
                        for i in range(0, len(chrts), 1000):
                            d = self._req(client, "POST", f"{MARKET}/api/v3/stocks/{wh['id']}", cab.token, "Маркетплейс", json={"chrtIds": chrts[i:i + 1000]})
                            for s in d.get("stocks") or []:
                                amounts[int(s["chrtId"])] = int(s.get("amount") or 0)
                            time.sleep(0.25)
                        for chrt in chrts:
                            info = sizes[chrt]
                            rows.append((cab.id, wh["id"], chrt, info["nm_id"], info["vendor"], info["size"], amounts.get(chrt, 0), now))
                with self.store._connect() as c:
                    c.executemany("INSERT INTO fbs_sizes(cabinet_id, warehouse_id, chrt_id, nm_id, vendor, size, amount, updated_at, seen_positive) "
                                  "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(cabinet_id, warehouse_id, chrt_id) DO UPDATE SET nm_id=excluded.nm_id, "
                                  "vendor=excluded.vendor, size=excluded.size, amount=excluded.amount, updated_at=excluded.updated_at, "
                                  "seen_positive=MAX(fbs_sizes.seen_positive, excluded.seen_positive)",
                                  [r + (1 if r[6] > 0 else 0,) for r in rows])
                self.state[cab.id] = {"at": time.time(), "error": "",
                                      "warehouses": [{"id": w["id"], "name": w.get("name") or str(w["id"])} for w in whs]}
                out[cab.id] = "ok" if whs else "нет складов FBS"
            except StocksError as exc:
                self.state[cab.id] = {**self.state.get(cab.id, {}), "error": str(exc)}
                out[cab.id] = str(exc)
        return out

    # ------------------------------------------------------------------ таблица
    def _velocity(self):
        """Заказов в день — среднее за последние 14 полных дней (вчера и 13 дней до него, все склады, без отмен):
        по артикулу, по баркоду и по (артикул, размер). Сегодняшний неполный день не учитывается."""
        since, until = (date.today() - timedelta(days=DAYS)).isoformat(), (date.today() - timedelta(days=1)).isoformat()
        by_nm, by_bc, by_size = {}, {}, {}
        try:
            with self.store._connect() as c:
                by_nm = {nm: n / DAYS for nm, n in c.execute(
                    "SELECT nm_id, COUNT(*) FROM rnp_orders WHERE day BETWEEN ? AND ? AND is_cancel=0 GROUP BY nm_id", (since, until))}
                for nm, bc, size, n in c.execute("SELECT nm_id, barcode, tech_size, COUNT(*) FROM rnp_orders WHERE day BETWEEN ? AND ? AND is_cancel=0 "
                                                 "AND (COALESCE(barcode,'')<>'' OR COALESCE(tech_size,'')<>'') GROUP BY nm_id, barcode, tech_size", (since, until)):
                    if bc:
                        by_bc[bc] = by_bc.get(bc, 0) + n / DAYS
                    if size:
                        by_size[(nm, str(size))] = by_size.get((nm, str(size)), 0) + n / DAYS
        except Exception:
            pass
        return by_nm, by_bc, by_size

    def archived(self) -> set:
        with self.store._connect() as c:
            return {(cid, nm) for cid, nm in c.execute("SELECT cabinet_id, nm_id FROM fbs_archive")}

    def set_archive(self, keys: list, archived: bool, author: str = ""):
        """keys: ["кабинет:nmID"]. Архив — артикулы, которые больше не пополняем: скрыты из списка и без оповещений."""
        now = datetime.now(timezone.utc).isoformat()
        with self.store._connect() as c:
            for k in keys:
                cid, nm = k.rsplit(":", 1)
                if archived:
                    c.execute("INSERT OR REPLACE INTO fbs_archive(cabinet_id, nm_id, at, author) VALUES(?,?,?,?)", (cid, int(nm), now, author))
                else:
                    c.execute("DELETE FROM fbs_archive WHERE cabinet_id=? AND nm_id=?", (cid, int(nm)))

    def rows(self) -> list:
        cfg = self.settings()
        catalog = self.catalog() or {}
        vel, vel_bc, vel_size = self._velocity()
        arch = self.archived()
        has_sizes = bool(vel_bc or vel_size)
        names = {c.id: c.name for c in self.wb_cabinets()}
        wh_names = {(cid, w["id"]): w["name"] for cid, s in self.state.items() for w in s.get("warehouses") or []}
        with self.store._connect() as c:
            data = c.execute("SELECT cabinet_id, warehouse_id, chrt_id, nm_id, vendor, size, amount, updated_at, threshold, muted, seen_positive, alert_state "
                             "FROM fbs_sizes").fetchall()
        out = []
        for cid, wh, chrt, nm, vendor, size, amount, upd, thr, muted, seen, state in data:
            if cid not in names:
                continue
            info = (self.cards.get(cid) or {}).get("sizes", {}).get(chrt) or {}
            cat = catalog.get(art_key(vendor)) or {}
            limit = thr if thr is not None else cfg["threshold"]
            status = "zero" if amount <= 0 else "low" if amount <= limit else "ok"
            archived = (cid, nm) in arch
            watched = not muted and not archived and (seen or not cfg["only_seen"])
            bcs = info.get("barcodes") or []
            size_vel = sum(vel_bc.get(b, 0) for b in bcs) or vel_size.get((nm, str(size or "")), 0)
            out.append({"id": f"{cid}:{wh}:{chrt}", "cabinet_id": cid, "cabinet": names[cid], "warehouse_id": wh,
                        "warehouse": wh_names.get((cid, wh), str(wh)), "chrt_id": chrt, "nm_id": nm, "vendor": vendor, "key": art_key(vendor),
                        "size": size or "", "ru_size": info.get("ru_size", ""), "barcode": (info.get("barcodes") or [""])[0],
                        "title": info.get("title") or cat.get("name") or "", "brand": info.get("brand") or cat.get("brand") or "",
                        "category": info.get("subject") or cat.get("category") or "", "photo": info.get("photo", ""),
                        "amount": amount, "updated_at": upd, "threshold": thr, "limit": limit, "muted": bool(muted), "watched": watched,
                        "status": status, "wb_stock": cat.get("wb_stock"), "orders_per_day": round(vel.get(nm, 0), 2),
                        "size_orders_per_day": round(size_vel, 2) if has_sizes else None, "archived": archived})
        return out

    def set_watch(self, ids: list, threshold=..., muted=None):
        with self.store._connect() as c:
            for rid in ids:
                cid, wh, chrt = rid.split(":")
                if threshold is not ...:
                    c.execute("UPDATE fbs_sizes SET threshold=? WHERE cabinet_id=? AND warehouse_id=? AND chrt_id=?", (threshold, cid, int(wh), int(chrt)))
                if muted is not None:
                    c.execute("UPDATE fbs_sizes SET muted=? WHERE cabinet_id=? AND warehouse_id=? AND chrt_id=?", (int(muted), cid, int(wh), int(chrt)))

    # ------------------------------------------------------------------ изменение остатков
    def update(self, changes: list, author: str = "") -> dict:
        """changes: [{id: "кабинет:склад:chrt", amount: новое значение}]."""
        current = {r["id"]: r for r in self.rows()}
        groups, errors = {}, {}
        for ch in changes:
            r = current.get(ch.get("id"))
            if not r:
                errors[ch.get("id")] = "размер не найден — обновите данные"
                continue
            try:
                amount = int(ch["amount"])
            except (TypeError, ValueError, KeyError):
                errors[r["id"]] = "остаток — целое число"
                continue
            if not 0 <= amount <= 100000:
                errors[r["id"]] = "остаток — от 0 до 100 000"
                continue
            groups.setdefault((r["cabinet_id"], r["warehouse_id"]), []).append((r, amount))
        if not groups and not errors:
            raise StocksError("Нет изменений")
        done = {}
        with httpx.Client(timeout=60) as client:
            for (cid, wh), items in groups.items():
                cab = self._cabinet(cid)
                for i in range(0, len(items), 1000):
                    part = items[i:i + 1000]
                    try:
                        self._req(client, "PUT", f"{MARKET}/api/v3/stocks/{wh}", cab.token, "Маркетплейс",
                                  json={"stocks": [{"chrtId": r["chrt_id"], "amount": a} for r, a in part]})
                        for r, a in part:
                            done[r["id"]] = (r, a, "")
                    except StocksError as exc:
                        for r, a in part:
                            done[r["id"]] = (r, a, str(exc))
        now = datetime.now(timezone.utc).isoformat()
        with self.store._connect() as c:
            for rid, (r, a, err) in done.items():
                c.execute("INSERT INTO fbs_log(at, author, cabinet_id, warehouse_id, chrt_id, nm_id, vendor, size, old, new, status, error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                          (now, author, r["cabinet_id"], r["warehouse_id"], r["chrt_id"], r["nm_id"], r["vendor"], r["size"], r["amount"], a, "ok" if not err else "error", err))
                if not err:
                    c.execute("UPDATE fbs_sizes SET amount=?, updated_at=?, seen_positive=MAX(seen_positive, ?), "
                              "alert_state=CASE WHEN ? > COALESCE(threshold, ?) THEN 'ok' ELSE alert_state END WHERE cabinet_id=? AND warehouse_id=? AND chrt_id=?",
                              (a, now, 1 if a > 0 else 0, a, self.settings()["threshold"], r["cabinet_id"], r["warehouse_id"], r["chrt_id"]))
        errors.update({rid: err for rid, (_, _, err) in done.items() if err})
        ok = sum(1 for _, _, err in done.values() if not err)
        return {"ok": ok, "errors": errors}

    def history(self, days: int = 30, item_id: str = "") -> list:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        q, args = "SELECT at, author, cabinet_id, warehouse_id, chrt_id, vendor, size, old, new, status, error FROM fbs_log WHERE at>=?", [since]
        if item_id:
            cid, wh, chrt = item_id.split(":")
            q += " AND cabinet_id=? AND warehouse_id=? AND chrt_id=?"
            args += [cid, int(wh), int(chrt)]
        with self.store._connect() as c:
            rows = c.execute(q + " ORDER BY id DESC LIMIT 500", args).fetchall()
        names = {c.id: c.name for c in self.cabinets()}
        return [{"at": at, "author": a, "cabinet": names.get(cid, cid), "warehouse_id": wh, "chrt_id": ch, "vendor": v, "size": s, "old": o, "new": n,
                 "status": st, "error": e} for at, a, cid, wh, ch, v, s, o, n, st, e in rows]

    # ------------------------------------------------------------------ оповещения
    def check_alerts(self, force: bool = False) -> dict:
        """Найти размеры, у которых остаток дошёл до порога или закончился. Возвращает сообщения (одно на кабинет)."""
        cfg = self.settings()
        if not cfg["monitor"] and not force:
            return {"messages": [], "low": 0, "zero": 0}
        rows = [r for r in self.rows() if r["watched"]]
        fresh_low, fresh_zero, keep = [], [], []
        now = datetime.now(timezone.utc).isoformat()
        with self.store._connect() as c:
            states = {(cid, wh, ch): s for cid, wh, ch, s in c.execute("SELECT cabinet_id, warehouse_id, chrt_id, alert_state FROM fbs_sizes")}
            for r in rows:
                key = (r["cabinet_id"], r["warehouse_id"], r["chrt_id"])
                prev = states.get(key) or "ok"
                new = r["status"]
                if new == prev and not force:
                    continue
                if new == "ok":
                    c.execute("UPDATE fbs_sizes SET alert_state='ok' WHERE cabinet_id=? AND warehouse_id=? AND chrt_id=?", key)
                    continue
                if force or (prev == "ok") or (prev == "low" and new == "zero"):
                    (fresh_zero if new == "zero" else fresh_low).append(r)
                c.execute("UPDATE fbs_sizes SET alert_state=?, alert_at=? WHERE cabinet_id=? AND warehouse_id=? AND chrt_id=?", (new, now) + key)
        if not (fresh_low or fresh_zero):
            return {"messages": [], "low": 0, "zero": 0}
        by_cab = {}
        for r in fresh_zero + fresh_low:
            by_cab.setdefault(r["cabinet"], []).append(r)
        return {"messages": [(f"Остатки FBS · {cab}", self.message(items)) for cab, items in by_cab.items()],
                "low": len(fresh_low), "zero": len(fresh_zero)}

    @staticmethod
    def message(items: list) -> str:
        def line(r):
            size = r["size"] + (f" ({r['ru_size']})" if r["ru_size"] and r["ru_size"] != r["size"] else "")
            sale = f", заказов в день ≈ {r['orders_per_day']:g}" if r.get("orders_per_day") else ""
            return f"• {r['vendor']}{(' · размер ' + size) if size else ''} — {r['amount']} шт.{sale}"
        zero = [r for r in items if r["status"] == "zero"]
        low = [r for r in items if r["status"] == "low"]
        parts = []
        if zero:
            parts.append("Закончились:\n" + "\n".join(line(r) for r in zero[:30]) + (f"\n…и ещё {len(zero) - 30}" if len(zero) > 30 else ""))
        if low:
            parts.append(f"Осталось мало (порог {low[0]['limit']} шт.):\n" + "\n".join(line(r) for r in sorted(low, key=lambda x: x["amount"])[:30])
                         + (f"\n…и ещё {len(low) - 30}" if len(low) > 30 else ""))
        whs = sorted({r["warehouse"] for r in items})
        parts.append(f"Склад: {', '.join(whs)}. Пополнить: WB OPS → «Остатки FBS».")
        return "\n\n".join(parts)

