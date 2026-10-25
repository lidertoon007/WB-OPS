"""Репрайсер Wildberries: ручное управление ценами с прогнозом маржи и лимитами безопасности.

Цены — API «Цены и скидки» WB. Цена до СПП = цена продавца со скидкой продавца (discountedPrice).
Цена для клиента = цена до СПП × (1 − СПП) × (1 − скидка WB Кошелька). СПП берётся с витрины WB
(публичная карточка товара), если витрина недоступна — средняя СПП по заказам из РНП за 14 дней.
Автоматических стратегий нет: цена уходит на WB только после подтверждения пользователем.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timedelta, timezone

import httpx

from app.services.nomenclature import art_key

PRICES = "https://discounts-prices-api.wildberries.ru"
SHOWCASE = ("https://card.wb.ru/cards/v4/detail", "https://card.wb.ru/cards/v2/detail")
DEFAULTS = {"min_price": None, "max_price": None, "max_change_pct": 20.0, "daily_limit_pct": 30.0, "min_interval_hours": 0.0, "wallet": {}}
REFRESH_EVERY = 60          # обновлять данные с WB не чаще раза в минуту


class RepricerError(Exception):
    pass


class Repricer:
    def __init__(self, store, cabinets, economics, catalog):
        self.store = store
        self.cabinets = cabinets          # callable -> [Cabinet]
        self.economics = economics        # callable -> RNP.unit_economics()
        self.catalog = catalog            # callable -> {art_key: {name, brand, category, stock, cost, prep}}
        self.cache: dict[str, dict] = {}  # cabinet_id -> {"at", "goods", "showcase", "error"}
        self.ops: dict[int, dict] = {}
        self._lock = threading.Lock()
        with store._connect() as c:
            c.execute("CREATE TABLE IF NOT EXISTS repricer_items (cabinet_id TEXT, nm_id INTEGER, included INTEGER DEFAULT 0, "
                      "min_price REAL, max_price REAL, PRIMARY KEY(cabinet_id, nm_id))")
            c.execute("CREATE TABLE IF NOT EXISTS repricer_ops (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, author TEXT, action TEXT, "
                      "count INTEGER, ok INTEGER, errors INTEGER, status TEXT, details TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS repricer_log (id INTEGER PRIMARY KEY AUTOINCREMENT, op_id INTEGER, at TEXT, cabinet_id TEXT, nm_id INTEGER, "
                      "vendor TEXT, old_price REAL, new_price REAL, old_client REAL, new_client REAL, author TEXT, status TEXT, error TEXT)")
            c.execute("CREATE INDEX IF NOT EXISTS repricer_log_nm ON repricer_log(cabinet_id, nm_id, at)")

    # ------------------------------------------------------------------ настройки
    def settings(self) -> dict:
        try:
            saved = json.loads(self.store.get_setting("repricer_settings", "") or "{}")
        except ValueError:
            saved = {}
        return {**DEFAULTS, **saved, "wallet": {**DEFAULTS["wallet"], **(saved.get("wallet") or {})}}

    def save_settings(self, patch: dict) -> dict:
        cur = self.settings()
        for k in ("min_price", "max_price", "max_change_pct", "daily_limit_pct", "min_interval_hours"):
            if k in patch:
                v = patch[k]
                cur[k] = None if v in (None, "") else float(v)
                if cur[k] is not None and cur[k] < 0:
                    raise RepricerError("Значения не могут быть отрицательными")
        if cur["min_price"] and cur["max_price"] and cur["min_price"] > cur["max_price"]:
            raise RepricerError("Минимальная цена больше максимальной")
        if "wallet" in patch:
            for cid, pct in (patch["wallet"] or {}).items():
                pct = float(pct or 0)
                if not 0 <= pct < 50:
                    raise RepricerError("Скидка WB Кошелька — от 0 до 50%")
                cur["wallet"][cid] = pct
        self.store.set_setting("repricer_settings", json.dumps(cur, ensure_ascii=False))
        return cur

    def wb_cabinets(self) -> list:
        return [c for c in self.cabinets() if c.marketplace == "wildberries" and c.token and getattr(c, "enabled", True)]

    def _cabinet(self, cid):
        c = next((c for c in self.wb_cabinets() if c.id == cid), None)
        if not c:
            raise RepricerError("Кабинет WB не найден или без ключа API")
        return c

    # ------------------------------------------------------------------ данные с WB
    def refresh(self, force: bool = False) -> dict:
        out = {}
        for cab in self.wb_cabinets():
            cache = self.cache.get(cab.id) or {}
            if cache.get("at") and time.time() - cache["at"] < REFRESH_EVERY and not force:
                out[cab.id] = "свежие"
                continue
            if cache.get("at") and time.time() - cache["at"] < REFRESH_EVERY:
                raise RepricerError("Обновлять данные можно раз в минуту")
            try:
                goods = self._load_goods(cab)
                showcase = self._load_showcase([g["nmID"] for g in goods])
                self.cache[cab.id] = {"at": time.time(), "goods": goods, "showcase": showcase, "error": ""}
                out[cab.id] = "ok"
            except RepricerError as exc:
                self.cache[cab.id] = {**cache, "error": str(exc), "at": cache.get("at")}
                out[cab.id] = str(exc)
        return out

    def _get(self, client, url, token, params):
        for attempt in range(5):
            try:
                r = client.get(url, params=params, headers={"Authorization": token})
            except httpx.HTTPError as exc:
                if attempt < 4:
                    time.sleep(2)
                    continue
                raise RepricerError(f"нет связи с WB ({type(exc).__name__})")
            if r.status_code == 429 and attempt < 4:
                time.sleep(3)
                continue
            if r.status_code in (401, 403):
                raise RepricerError("у ключа API нет доступа к категории «Цены и скидки»")
            if r.status_code >= 400:
                raise RepricerError(f"WB ответил {r.status_code}: {r.text[:200]}")
            return r.json()
        raise RepricerError("WB ограничил частоту запросов")

    def _load_goods(self, cab) -> list:
        goods, offset = [], 0
        with httpx.Client(timeout=60) as client:
            for _ in range(100):
                d = self._get(client, PRICES + "/api/v2/list/goods/filter", cab.token, {"limit": 1000, "offset": offset})
                page = ((d or {}).get("data") or {}).get("listGoods") or []
                goods += page
                if len(page) < 1000:
                    break
                offset += 1000
                time.sleep(0.7)
        return goods

    def _load_showcase(self, nm_ids: list) -> dict:
        """nmID -> цена для клиента (после СПП, без Кошелька) с витрины WB."""
        out = {}
        with httpx.Client(timeout=20, headers={"User-Agent": "Mozilla/5.0"}) as client:
            for i in range(0, len(nm_ids), 100):
                part = ";".join(str(n) for n in nm_ids[i:i + 100])
                for url in SHOWCASE:
                    try:
                        r = client.get(url, params={"appType": 1, "curr": "rub", "dest": -1257786, "spp": 30, "nm": part})
                        if r.status_code != 200:
                            continue
                        j = r.json()
                    except (httpx.HTTPError, ValueError):
                        continue
                    products = j.get("products") or (j.get("data") or {}).get("products") or []
                    for p in products:
                        prices = [((s.get("price") or {}).get("product")) for s in p.get("sizes") or [] if (s.get("price") or {}).get("product")]
                        client_price = min(prices) / 100 if prices else (p.get("salePriceU") or 0) / 100
                        if client_price:
                            out[int(p["id"])] = client_price
                    if products:
                        break
        return out

    def _orders_spp(self) -> dict:
        try:
            with self.store._connect() as c:
                return {nm: spp for nm, spp in c.execute(
                    "SELECT nm_id, AVG(spp) FROM rnp_orders WHERE day>=? AND is_cancel=0 GROUP BY nm_id", ((date.today() - timedelta(days=14)).isoformat(),))}
        except Exception:
            return {}

    # ------------------------------------------------------------------ таблица
    def _items_meta(self) -> dict:
        with self.store._connect() as c:
            return {(cid, nm): {"included": bool(inc), "min_price": mn, "max_price": mx}
                    for cid, nm, inc, mn, mx in c.execute("SELECT cabinet_id, nm_id, included, min_price, max_price FROM repricer_items")}

    def _last_changes(self) -> dict:
        with self.store._connect() as c:
            return {(cid, nm): at for cid, nm, at in c.execute(
                "SELECT cabinet_id, nm_id, MAX(at) FROM repricer_log WHERE status='ok' GROUP BY cabinet_id, nm_id")}

    def rows(self) -> list:
        econ = self._econ()
        catalog = self.catalog()
        meta, last = self._items_meta(), self._last_changes()
        spp_orders = self._orders_spp()
        cfg = self.settings()
        out = []
        for cab in self.wb_cabinets():
            cache = self.cache.get(cab.id) or {}
            wallet = float(cfg["wallet"].get(cab.id) or 0)
            for g in cache.get("goods") or []:
                nm, vendor = g["nmID"], str(g.get("vendorCode") or "")
                key = art_key(vendor)
                sizes = g.get("sizes") or []
                base = max((s.get("price") or 0) for s in sizes) if sizes else 0
                before = min((s.get("discountedPrice") or 0) for s in sizes) if sizes else 0
                info = catalog.get(key) or {}
                stock = info.get("stock")
                in_stock = stock is None or stock > 0
                client = cache.get("showcase", {}).get(nm)
                spp, spp_src = None, ""
                if client and before:
                    spp, spp_src = max(0.0, min(99.0, (1 - client / before) * 100)), "витрина"
                elif nm in spp_orders and spp_orders[nm] is not None:
                    spp, spp_src = float(spp_orders[nm]), "заказы"
                    client = before * (1 - spp / 100)
                m = meta.get((cab.id, nm)) or {}
                row = {"id": f"{cab.id}:{nm}", "cabinet_id": cab.id, "cabinet": cab.name, "nm_id": nm, "vendor": vendor, "key": key,
                       "name": info.get("name") or "", "brand": info.get("brand") or "", "category": info.get("category") or "",
                       "price": base, "discount": g.get("discount") or 0, "club_discount": g.get("clubDiscount") or 0,
                       "price_before_spp": before, "spp": spp, "spp_source": spp_src,
                       "client_price": round(client, 2) if client and in_stock else None, "wallet": wallet,
                       "client_wallet": round(client * (1 - wallet / 100), 2) if client and in_stock else None,
                       "editable_size_price": bool(g.get("editableSizePrice")), "sizes": len(sizes),
                       "stock": stock, "in_stock": in_stock, "cost": info.get("cost"), "prep": info.get("prep") or 0,
                       "included": m.get("included", False), "min_price": m.get("min_price"), "max_price": m.get("max_price"),
                       "last_change": last.get((cab.id, nm)), "bad_turnover": bool(g.get("isBadTurnover"))}
                row.update(self.unit(row, before, econ))
                out.append(row)
        return out

    def state(self) -> dict:
        return {cid: {"at": c.get("at"), "error": c.get("error", ""), "count": len(c.get("goods") or [])} for cid, c in self.cache.items()}

    def _econ(self):
        try:
            return self.economics() or {}
        except Exception:
            return {}

    @staticmethod
    def unit(row, price, econ) -> dict:
        """Юнит-экономика проданной штуки при цене до СПП = price."""
        if not price:
            return {"profit": None, "margin": None}
        e = (econ.get("articles") or {}).get(row["key"]) or econ.get("cabinet") or {}
        if row.get("cost") is None:
            return {"profit": None, "margin": None, "no_cost": True}
        buyout = e.get("buyout") or 1
        tax_rate = econ.get("tax_rate") or 0
        costs = {"commission": price * (e.get("commission") or 0), "acquiring": price * (e.get("acquiring") or 0),
                 "logistics": (e.get("logistics_per_order") or 0) / max(buyout, 0.05), "ads": price * (e.get("ads_share") or 0),
                 "tax": price * (e.get("paid_share") or 1) * tax_rate / 100, "cogs": (row.get("cost") or 0) + (row.get("prep") or 0)}
        profit = price - sum(costs.values())
        return {"profit": round(profit, 2), "margin": round(profit / price * 100, 2), "costs": {k: round(v, 2) for k, v in costs.items()}}

    # ------------------------------------------------------------------ контур управления
    def set_items(self, ids: list, included: bool | None = None, min_price=..., max_price=...):
        with self.store._connect() as c:
            for rid in ids:
                cid, nm = rid.split(":", 1)
                c.execute("INSERT OR IGNORE INTO repricer_items(cabinet_id, nm_id, included) VALUES(?,?,0)", (cid, int(nm)))
                if included is not None:
                    c.execute("UPDATE repricer_items SET included=? WHERE cabinet_id=? AND nm_id=?", (int(included), cid, int(nm)))
                if min_price is not ...:
                    c.execute("UPDATE repricer_items SET min_price=? WHERE cabinet_id=? AND nm_id=?", (min_price, cid, int(nm)))
                if max_price is not ...:
                    c.execute("UPDATE repricer_items SET max_price=? WHERE cabinet_id=? AND nm_id=?", (max_price, cid, int(nm)))

    # ------------------------------------------------------------------ расчёт изменений
    def preview(self, changes: list | None = None, mass: dict | None = None, ids: list | None = None) -> list:
        """changes: [{id, new_price | new_client}] или mass: {kind: before|client, action: set|up|down, unit: rub|pct, value} для ids."""
        rows = {r["id"]: r for r in self.rows()}
        econ, cfg = self._econ(), self.settings()
        todo = []
        if mass:
            value = float(mass.get("value") or 0)
            for rid in ids or []:
                r = rows.get(rid)
                if not r:
                    continue
                kind = mass.get("kind", "before")
                cur = r["price_before_spp"] if kind == "before" else r["client_wallet"]
                if cur is None:
                    todo.append({"id": rid, "error": "нет цены для клиента — измените цену до СПП"})
                    continue
                if mass.get("action") == "set":
                    new = value
                else:
                    delta = cur * value / 100 if mass.get("unit") == "pct" else value
                    new = cur + delta if mass.get("action") == "up" else cur - delta
                todo.append({"id": rid, ("new_price" if kind == "before" else "new_client"): round(new)})
        else:
            todo = changes or []
        out = []
        for ch in todo:
            r = rows.get(ch.get("id"))
            if not r:
                out.append({"id": ch.get("id"), "error": "товар не найден — обновите данные"})
                continue
            out.append(self._check(r, ch, econ, cfg))
        return out

    def _check(self, r, ch, econ, cfg) -> dict:
        res = {"id": r["id"], "cabinet": r["cabinet"], "nm_id": r["nm_id"], "vendor": r["vendor"], "name": r["name"],
               "old_price": r["price_before_spp"], "old_client": r["client_wallet"], "old_margin": r.get("margin"),
               "discount": r["discount"], "errors": [], "warnings": [], "confirm": False}
        if ch.get("error"):
            res["errors"].append(ch["error"])
            return res
        target = ch.get("new_price")
        if target in (None, "") and ch.get("new_client") not in (None, ""):
            if not r.get("spp") and r.get("spp") != 0:
                res["errors"].append("нет данных о СПП — задайте цену до СПП")
                return res
            target = float(ch["new_client"]) / ((1 - (r["spp"] or 0) / 100) * (1 - r["wallet"] / 100))
        try:
            target = float(target)
        except (TypeError, ValueError):
            res["errors"].append("не указана новая цена")
            return res
        d = r["discount"] or 0
        base = int(round(target / (1 - d / 100))) if d < 100 else int(round(target))
        new_before = round(base * (1 - d / 100), 2)
        res.update({"new_base": base, "new_price": new_before,
                    "new_client": round(new_before * (1 - (r["spp"] or 0) / 100) * (1 - r["wallet"] / 100), 2) if r.get("spp") is not None and r["in_stock"] else None})
        res.update({"new_" + k: v for k, v in self.unit(r, new_before, econ).items() if k in ("profit", "margin")})
        if base < 1:
            res["errors"].append("цена должна быть больше нуля")
        lo = r["min_price"] if r.get("min_price") is not None else cfg.get("min_price")
        hi = r["max_price"] if r.get("max_price") is not None else cfg.get("max_price")
        scope = "артикула" if r.get("min_price") is not None or r.get("max_price") is not None else "общего диапазона"
        if lo and new_before < lo:
            res["errors"].append(f"ниже минимума {scope}: {lo:,.0f} ₽".replace(",", " "))
        if hi and new_before > hi:
            res["errors"].append(f"выше максимума {scope}: {hi:,.0f} ₽".replace(",", " "))
        old = r["price_before_spp"] or 0
        change = abs(new_before - old) / old * 100 if old else 0
        res["change_pct"] = round((new_before - old) / old * 100, 2) if old else None
        if old and abs(new_before - old) < 0.5:
            res["errors"].append("цена не меняется")
        if cfg.get("max_change_pct") and change > cfg["max_change_pct"]:
            res["confirm"] = True
            res["warnings"].append(f"изменение {change:.1f}% больше лимита {cfg['max_change_pct']:g}% за операцию — нужно подтверждение")
        start = self._day_start_price(r)
        if cfg.get("daily_limit_pct") and start:
            day_change = abs(new_before - start) / start * 100
            if day_change > cfg["daily_limit_pct"]:
                res["errors"].append(f"за сутки цена изменится на {day_change:.1f}% — больше дневного лимита {cfg['daily_limit_pct']:g}%")
        if cfg.get("min_interval_hours") and r.get("last_change"):
            last = datetime.fromisoformat(r["last_change"])
            wait = last + timedelta(hours=cfg["min_interval_hours"]) - datetime.now(timezone.utc)
            if wait.total_seconds() > 0:
                res["errors"].append(f"цену меняли {last.astimezone():%d.%m %H:%M}; следующее изменение не раньше чем через {wait.total_seconds() / 3600:.1f} ч")
        if not r["in_stock"]:
            res["warnings"].append("нет в наличии")
        if r.get("no_cost") or r.get("cost") is None:
            res["warnings"].append("нет себестоимости — маржа не посчитана")
        if r["editable_size_price"] and r["sizes"] > 1:
            res["warnings"].append("цена меняется для всех размеров")
        return res

    def _day_start_price(self, r):
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        with self.store._connect() as c:
            row = c.execute("SELECT old_price FROM repricer_log WHERE cabinet_id=? AND nm_id=? AND status='ok' AND at>=? ORDER BY at LIMIT 1",
                            (r["cabinet_id"], r["nm_id"], since.isoformat())).fetchone()
        return row[0] if row else r["price_before_spp"]

    # ------------------------------------------------------------------ отправка на WB
    def apply(self, changes: list, confirmed_large: bool, author: str = "", action: str = "Изменение цены") -> dict:
        checked = self.preview(changes=changes)
        bad = [c for c in checked if c["errors"]]
        if bad:
            raise RepricerError(f"Есть ошибки у {len(bad)} товаров — исправьте или уберите их из списка")
        if any(c["confirm"] for c in checked) and not confirmed_large:
            raise RepricerError("Есть изменения больше лимита за операцию — подтвердите их")
        if not checked:
            raise RepricerError("Нет изменений")
        if len(checked) > 1000:
            raise RepricerError("За раз — не больше 1000 цен")
        with self.store._connect() as c:
            op_id = c.execute("INSERT INTO repricer_ops(created_at, author, action, count, ok, errors, status, details) VALUES(?,?,?,?,0,0,'running','')",
                              (datetime.now(timezone.utc).isoformat(), author, action, len(checked))).lastrowid
        op = {"id": op_id, "status": "running", "count": len(checked), "ok": 0, "errors": 0, "items": {c["id"]: "sending" for c in checked}}
        self.ops[op_id] = op
        threading.Thread(target=self._send, args=(op, checked, author), daemon=True).start()
        return op

    def _send(self, op, checked, author):
        by_cab = {}
        for c in checked:
            by_cab.setdefault(c["id"].split(":", 1)[0], []).append(c)
        results = {}
        for cid, items in by_cab.items():
            try:
                cab = self._cabinet(cid)
                results.update(self._upload(cab, items))
            except RepricerError as exc:
                results.update({c["id"]: str(exc) for c in items})
        now = datetime.now(timezone.utc).isoformat()
        ok = sum(1 for v in results.values() if v == "")
        with self.store._connect() as c:
            for it in checked:
                err = results.get(it["id"], "нет ответа WB")
                c.execute("INSERT INTO repricer_log(op_id, at, cabinet_id, nm_id, vendor, old_price, new_price, old_client, new_client, author, status, error) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (op["id"], now, it["id"].split(":", 1)[0], it["nm_id"], it["vendor"], it["old_price"],
                                                            it["new_price"], it["old_client"], it.get("new_client"), author, "ok" if err == "" else "error", err))
            status = "Выполнено" if ok == len(checked) else ("Ошибка" if ok == 0 else "Выполнено частично")
            c.execute("UPDATE repricer_ops SET ok=?, errors=?, status=?, details=? WHERE id=?",
                      (ok, len(checked) - ok, status, json.dumps({k: v for k, v in results.items() if v}, ensure_ascii=False), op["id"]))
        for it in checked:                         # обновить кэш, чтобы таблица сразу показала новую цену
            if results.get(it["id"]) == "":
                cache = self.cache.get(it["id"].split(":", 1)[0]) or {}
                for g in cache.get("goods") or []:
                    if g["nmID"] == it["nm_id"]:
                        g["discount"] = it["discount"]
                        old_client = cache.get("showcase", {}).get(it["nm_id"])
                        if old_client and it["old_price"]:      # СПП прежняя, пока витрина не обновится
                            cache["showcase"][it["nm_id"]] = round(it["new_price"] * old_client / it["old_price"], 2)
                        for s in g.get("sizes") or []:
                            s["price"], s["discountedPrice"] = it["new_base"], it["new_price"]
        op.update({"status": status, "ok": ok, "errors": len(checked) - ok,
                   "items": {k: ("ok" if v == "" else v) for k, v in results.items()}})

    def _upload(self, cab, items) -> dict:
        data = [{"nmID": it["nm_id"], "price": it["new_base"], "discount": int(it["discount"] or 0)} for it in items]
        out = {}
        with httpx.Client(timeout=60) as client:
            r = client.post(PRICES + "/api/v2/upload/task", json={"data": data}, headers={"Authorization": cab.token})
            if r.status_code in (401, 403):
                raise RepricerError("у ключа API нет права на запись в «Цены и скидки»")
            body = r.json() if r.content else {}
            if r.status_code >= 400 or body.get("error"):
                raise RepricerError(body.get("errorText") or f"WB ответил {r.status_code}")
            upload_id = (body.get("data") or {}).get("id")
            status = None
            for _ in range(20):
                time.sleep(3)
                h = client.get(PRICES + "/api/v2/history/tasks", params={"uploadID": upload_id}, headers={"Authorization": cab.token})
                status = ((h.json() if h.status_code == 200 else {}).get("data") or {}).get("status")
                if status in (3, 4, 5, 6):
                    break
            if status == 3:
                return {it["id"]: "" for it in items}
            if status in (5, 6):
                g = client.get(PRICES + "/api/v2/history/goods/task", params={"uploadID": upload_id, "limit": 1000}, headers={"Authorization": cab.token})
                errs = {x["nmID"]: x.get("errorText") or "ошибка WB" for x in ((g.json() if g.status_code == 200 else {}).get("data") or {}).get("historyGoods") or []
                        if x.get("status") == 3}
                for it in items:
                    out[it["id"]] = errs.get(it["nm_id"], "")
                return out
            if status == 4:
                return {it["id"]: "загрузка отменена WB" for it in items}
            return {it["id"]: "" for it in items}      # WB ещё обрабатывает — цена применится в течение нескольких минут

    # ------------------------------------------------------------------ история
    def history(self, author: str = "", status: str = "", since: str = "") -> list:
        q, args = "SELECT id, created_at, author, action, count, ok, errors, status FROM repricer_ops WHERE 1=1", []
        if author:
            q += " AND author=?"
            args.append(author)
        if status:
            q += " AND status=?"
            args.append(status)
        if since:
            q += " AND created_at>=?"
            args.append(since)
        with self.store._connect() as c:
            rows = c.execute(q + " ORDER BY id DESC LIMIT 200", args).fetchall()
        return [{"id": i, "created_at": ca, "author": a, "action": ac, "count": n, "ok": ok, "errors": er,
                 "status": (self.ops.get(i) or {}).get("status") if st == "running" and i in self.ops else ("Прервано" if st == "running" else st)}
                for i, ca, a, ac, n, ok, er, st in rows]

    def op(self, op_id: int) -> dict:
        if op_id in self.ops:
            return self.ops[op_id]
        with self.store._connect() as c:
            row = c.execute("SELECT id, status, count, ok, errors, details FROM repricer_ops WHERE id=?", (op_id,)).fetchone()
        if not row:
            raise RepricerError("Операция не найдена")
        return {"id": row[0], "status": row[1], "count": row[2], "ok": row[3], "errors": row[4], "items": json.loads(row[5] or "{}")}

    def item_history(self, item_id: str, days: int = 30) -> list:
        cid, nm = item_id.split(":", 1)
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self.store._connect() as c:
            return [{"at": at, "old_price": op, "new_price": np, "old_client": oc, "new_client": nc, "author": a, "status": s, "error": e}
                    for at, op, np, oc, nc, a, s, e in c.execute(
                        "SELECT at, old_price, new_price, old_client, new_client, author, status, error FROM repricer_log "
                        "WHERE cabinet_id=? AND nm_id=? AND at>=? ORDER BY at", (cid, int(nm), since))]
