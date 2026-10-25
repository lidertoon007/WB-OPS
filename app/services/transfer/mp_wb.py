"""Wildberries: чтение карточки (источник) и создание карточки (приёмник)."""
from __future__ import annotations

import time

from .common import Http, TransferError, find_values, norm, rank, to_number

CONTENT = "https://content-api.wildberries.ru"
PRICES = "https://discounts-prices-api.wildberries.ru"
TITLE_MAX = 60
SIZE_CHARCS = {"размер", "рос размер", "российский размер"}


class WB:
    mp = "wildberries"

    def __init__(self, cabinet, log=None):
        if not cabinet.token:
            raise TransferError(f"У кабинета «{cabinet.name}» нет ключа API")
        self.cabinet = cabinet
        self.log = log or (lambda *_: None)
        self.http = Http({"Authorization": cabinet.token}, pause=0.7, log=self.log)
        self._charcs = {}

    def close(self):
        self.http.close()

    # ------------------------------------------------------------------ источник
    def find_card(self, art: str):
        body = {"settings": {"sort": {"ascending": False}, "filter": {"textSearch": art, "withPhoto": -1}, "cursor": {"limit": 100}}}
        data = self.http.post(CONTENT + "/content/v2/get/cards/list?locale=ru", body, key="content")
        for c in data.get("cards") or []:
            if str(c.get("nmID")) == art or str(c.get("vendorCode", "")).strip().lower() == art.strip().lower():
                return c
        return None

    def prices(self, nm_id) -> dict:
        """techSize -> (цена до скидки, цена со скидкой продавца)."""
        try:
            d = self.http.get(PRICES + "/api/v2/list/goods/filter", params={"limit": 10, "filterNmID": nm_id}, key="prices")
        except TransferError as exc:
            self.log(f"  цены WB не получены (нужна категория токена «Цены и скидки»): {exc}")
            return {}
        out = {}
        for g in (d.get("data") or {}).get("listGoods") or []:
            for s in g.get("sizes") or []:
                out[str(s.get("techSizeName") or "")] = (s.get("price"), s.get("discountedPrice"))
        return out

    def fetch(self, art: str):
        card = self.find_card(art)
        if not card:
            return None
        prices = self.prices(card.get("nmID"))
        chars = []
        for ch in card.get("characteristics") or []:
            v = ch.get("value")
            vals = [str(x).strip() for x in (v if isinstance(v, list) else [v]) if str(x).strip()]
            if vals:
                chars.append({"name": ch.get("name") or "", "values": vals, "id": ch.get("id")})
        dims = card.get("dimensions") or {}
        variants = []
        for sz in card.get("sizes") or [{}]:
            ts = str(sz.get("techSize") or "")
            base, disc = prices.get(ts) or (next(iter(prices.values())) if prices else (None, None))
            price = float(disc or base or 0) or None
            old = float(base) if base and disc and float(base) > float(disc) else None
            variants.append({"id": sz.get("chrtID"), "size": "" if ts == "0" else ts, "ru_size": str(sz.get("wbSize") or ""),
                             "barcodes": list(sz.get("skus") or []), "price": price, "old_price": old, "chars": []})
        video = card.get("video")
        return {"key": str(card.get("vendorCode") or card.get("nmID")), "source_mp": self.mp, "source_ids": [card.get("nmID")],
                "title": card.get("title") or "", "description": card.get("description") or "", "brand": card.get("brand") or "",
                "category": card.get("subjectName") or "", "chars": chars,
                "photos": [p.get("big") for p in card.get("photos") or [] if p.get("big")], "videos": [video] if isinstance(video, str) and video else [],
                "dims": {"length": dims.get("length"), "width": dims.get("width"), "height": dims.get("height"), "weight": dims.get("weightBrutto")},
                "variants": variants,
                "native": {"subject_id": card.get("subjectID"), "subject": card.get("subjectName"), "characteristics": card.get("characteristics") or []}}

    # ------------------------------------------------------------------ приёмник: справочники
    def search_categories(self, q: str) -> list:
        data = self.http.get(CONTENT + "/content/v2/object/all", params={"name": q, "limit": 1000, "locale": "ru"}, key="content")
        return [{"id": s["subjectID"], "name": s["subjectName"], "path": s.get("parentName") or ""} for s in data.get("data") or []]

    def match_category(self, name: str) -> list:
        found = []
        for q in dict.fromkeys([name, name.split()[0] if name.split() else name, name[:5]]):
            if q:
                found += self.search_categories(q)
            if len(found) > 3:
                break
        uniq = list({c["id"]: c for c in found}.values())
        return rank(name, uniq, "name")

    def charcs(self, subject_id: int) -> list:
        if subject_id not in self._charcs:
            data = self.http.get(f"{CONTENT}/content/v2/object/charcs/{subject_id}", params={"locale": "ru"}, key="content")
            self._charcs[subject_id] = data.get("data") or []
        return self._charcs[subject_id]

    # ------------------------------------------------------------------ приёмник: подготовка
    def prepare(self, card: dict, cfg: dict, category: dict, extras: dict, vendor: str) -> dict:
        """Одна карточка WB (размеры — внутри). Возвращает item для предпросмотра и загрузки."""
        sid = category["id"]
        missing, filled, notes = [], {}, []
        same = card["source_mp"] == self.mp and card["native"].get("subject_id") == sid
        characteristics = []
        if same:
            characteristics = [{"id": c["id"], "value": c["value"]} for c in card["native"]["characteristics"] if c.get("id") is not None]
            filled = {c["name"]: c["value"] if isinstance(c["value"], list) else [c["value"]] for c in card["native"]["characteristics"]}
            known = {c["id"] for c in characteristics}
        else:
            known = set()
        ex = {norm(k): v for k, v in extras.items()}
        for ch in self.charcs(sid):
            cid, name = ch.get("charcID"), ch.get("name") or ""
            ln = norm(name)
            if cid in known or ln in SIZE_CHARCS:
                continue
            if ln in ex:
                if ex[ln] is None:
                    continue
                values = ex[ln] if isinstance(ex[ln], list) else [ex[ln]]
            elif same:
                values = None
            else:
                values = find_values(name, card["chars"])
            values = [str(v).strip() for v in values or [] if str(v).strip()]
            if not values:
                if ch.get("required"):
                    missing.append(name)
                continue
            if ch.get("charcType") == 4:
                n = to_number(values[0])
                if n is None:
                    if ch.get("required"):
                        missing.append(f"{name} (нужно число, есть «{values[0]}»)")
                    continue
                value = n
            else:
                value = values[: ch["maxCount"]] if ch.get("maxCount") else values
            characteristics = [c for c in characteristics if c["id"] != cid] + [{"id": cid, "value": value}]
            filled[name] = value if isinstance(value, list) else [value]

        mult = float(cfg.get("price_multiplier") or 1)
        skip = [w.strip().lower() for w in str(cfg.get("skip_size_words") or "").split(",") if w.strip()]
        sizes, prices = [], []
        for v in card["variants"]:
            if any(w in (v.get("size") or "").lower() for w in skip):
                notes.append(f"размер «{v['size']}» пропущен")
                continue
            price, old = _prices(v, mult, cfg)
            base = old or price
            size = {"skus": list(v.get("barcodes") or [])}
            if base:
                size["price"] = int(base)
            if v.get("size"):
                size["techSize"] = v["size"]
                if v.get("ru_size"):
                    size["wbSize"] = v["ru_size"]
            sizes.append(size)
            prices.append((price, old))
        if not sizes:
            missing.append("нет размеров для переноса")
        if not any(p for p, _ in prices):
            missing.append("цена")
        if not card["photos"]:
            missing.append("фото")
        d = card["dims"]
        if not (d.get("length") and d.get("width") and d.get("height") and d.get("weight")):
            missing.append("габариты/вес")
        title = card["title"]
        if len(title) > TITLE_MAX:
            notes.append(f"название сокращено до {TITLE_MAX} символов")
            title = title[:TITLE_MAX].rstrip()
        variant = {"vendorCode": vendor, "title": title, "description": card["description"][:5000], "brand": card["brand"],
                   "dimensions": {"length": to_number(d.get("length")) or 0, "width": to_number(d.get("width")) or 0,
                                  "height": to_number(d.get("height")) or 0, "weightBrutto": to_number(d.get("weight")) or 0},
                   "characteristics": characteristics, "sizes": sizes}
        price, old = prices[0] if prices else (None, None)
        return {"offer_id": vendor, "title": title, "price": price, "old_price": old, "sizes": [s.get("techSize") or "—" for s in sizes],
                "missing": missing, "filled": filled, "notes": notes, "photos": card["photos"][:30],
                "payload": {"subjectID": sid, "variants": [variant]}}

    # ------------------------------------------------------------------ приёмник: загрузка
    def upload(self, items: list, cfg: dict) -> list:
        results = []
        if cfg.get("new_barcodes", True):
            need = sum(len(s["skus"]) or 1 for it in items for s in it["payload"]["variants"][0]["sizes"])
            if need:
                codes = self.http.post(CONTENT + "/content/v2/barcodes", {"count": need}, key="content").get("data") or []
                if len(codes) < need:
                    raise TransferError("WB не выдал новые баркоды")
                for it in items:
                    for s in it["payload"]["variants"][0]["sizes"]:
                        s["skus"] = [codes.pop() for _ in range(max(1, len(s["skus"])))]
                self.log(f"  получено новых баркодов: {need}")
        for i in range(0, len(items), 50):
            batch = items[i:i + 50]
            try:
                r = self.http.post(CONTENT + "/content/v2/cards/upload", [it["payload"] for it in batch], key="upload", pause=6.5)
                if r.get("error"):
                    raise TransferError(r.get("errorText") or "WB вернул ошибку")
                for it in batch:
                    results.append({"offer_id": it["offer_id"], "status": "sent", "message": "отправлено в WB, ждём создания"})
            except TransferError as exc:
                for it in batch:
                    results.append({"offer_id": it["offer_id"], "status": "error", "message": str(exc)})
        sent = [it for it, r in zip(items, results) if r["status"] == "sent"]
        if sent:
            self.log("  жду, пока WB создаст карточки (до 3 минут)…")
            done = self.finalize(sent, cfg, wait=180)
            by = {r["offer_id"]: r for r in done}
            results = [by.get(r["offer_id"], r) for r in results]
        return results

    def wb_errors(self) -> dict:
        try:
            d = self.http.post(CONTENT + "/content/v2/cards/error/list?locale=ru", {"cursor": {"limit": 100}, "order": {"ascending": False}}, key="errors", pause=6.5)
        except TransferError:
            return {}
        out = {}
        for batch in ((d.get("data") or {}).get("items") or []):
            for vc, errs in (batch.get("errors") or {}).items():
                out.setdefault(vc, errs)
        return out

    def finalize(self, items: list, cfg: dict, wait: int = 0) -> list:
        """Найти созданные карточки, загрузить фото и скидку. Можно повторить позже («Догрузить фото и цены»)."""
        pending = {it["offer_id"]: it for it in items}
        found, deadline = {}, time.monotonic() + wait
        while pending:
            for vc in list(pending):
                card = self.find_card(vc)
                if card:
                    found[vc] = card
                    pending.pop(vc)
            if not pending or time.monotonic() > deadline:
                break
            time.sleep(15)
        errors = self.wb_errors() if pending else {}
        results = []
        for vc, it in {it["offer_id"]: it for it in items}.items():
            card = found.get(vc)
            if not card:
                if vc in errors:
                    results.append({"offer_id": vc, "status": "error", "message": "; ".join(errors[vc])})
                else:
                    results.append({"offer_id": vc, "status": "pending", "message": "WB ещё создаёт карточку — нажмите «Догрузить фото и цены» позже"})
                continue
            notes = []
            nm = card.get("nmID")
            if it.get("photos"):
                try:
                    self.http.post(CONTENT + "/content/v3/media/save", {"nmId": nm, "data": it["photos"]}, key="content")
                    notes.append(f"фото: {len(it['photos'])}")
                except TransferError as exc:
                    notes.append(f"фото не загружены: {exc}")
            price, old = it.get("price"), it.get("old_price")
            if price and old and old > price:
                try:
                    self.http.post(PRICES + "/api/v2/upload/task", {"data": [{"nmID": nm, "price": int(old), "discount": int(round((1 - price / old) * 100))}]},
                                   key="prices", pause=0.7)
                    notes.append("цена и скидка установлены")
                except TransferError as exc:
                    notes.append(f"скидка не установлена: {exc}")
            results.append({"offer_id": vc, "status": "ok", "message": f"создана, nmID {nm}" + (f" · {', '.join(notes)}" if notes else ""), "target_id": nm})
        return results


def _prices(v: dict, mult: float, cfg: dict):
    price = round(float(v["price"]) * mult) if v.get("price") else None
    old = round(float(v["old_price"]) * mult) if v.get("old_price") else None
    if old and price and old <= price:
        old = None
    fixed = float(cfg.get("old_price_fixed") or 0)
    if fixed and price:
        old = int(fixed) if fixed > price else None
    return price, old
