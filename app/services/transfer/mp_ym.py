"""Яндекс Маркет: чтение товаров (источник) и создание товаров (приёмник)."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .common import Http, TransferError, find_values, norm, rank, sim, to_number

API = "https://api.partner.market.yandex.ru"
SIZE_SEPARATORS = ("_", "-", "/", " ")


class YM:
    mp = "yandex"

    def __init__(self, cabinet, cache_dir: Path, log=None):
        if not cabinet.token:
            raise TransferError(f"У кабинета Маркета «{cabinet.name}» нет ключа API")
        self.cabinet = cabinet
        self.log = log or (lambda *_: None)
        self.http = Http({"Api-Key": cabinet.token}, pause=0.3, log=self.log)
        self.cache_dir = Path(cache_dir)
        self._business, self._tree, self._params, self._all = None, None, {}, None

    def close(self):
        self.http.close()

    @property
    def business(self) -> int:
        """ID кабинета (businessId). В настройках может быть указан ID кабинета или магазина."""
        if self._business:
            return self._business
        cid = str(self.cabinet.client_id or "").strip()
        if cid.isdigit():
            try:
                r = self.http.get(f"{API}/v2/campaigns/{cid}", ok404=True)
                biz = ((r or {}).get("campaign") or {}).get("business") or {}
                self._business = int(biz.get("id") or cid)
            except TransferError:
                self._business = int(cid)
        else:
            r = self.http.get(f"{API}/v2/campaigns")
            camps = r.get("campaigns") or []
            if not camps:
                raise TransferError("Не удалось определить ID кабинета Маркета — укажите его в настройках кабинета")
            self._business = int(camps[0]["business"]["id"])
        return self._business

    # ------------------------------------------------------------------ справочники
    def tree(self) -> list:
        if self._tree is not None:
            return self._tree
        path = self.cache_dir / "ym_tree.json"
        if path.exists() and time.time() - path.stat().st_mtime < 14 * 86400:
            self._tree = json.loads(path.read_text(encoding="utf-8"))
            return self._tree
        root = self.http.post(f"{API}/v2/categories/tree", {"language": "RU"})["result"]
        flat = []

        def walk(node, trail):
            kids = node.get("children") or []
            if not kids and node.get("id"):
                flat.append({"id": node["id"], "name": node["name"], "path": " / ".join(trail)})
            for k in kids:
                walk(k, trail + ([node["name"]] if node.get("name") else []))
        walk(root, [])
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(flat, ensure_ascii=False), encoding="utf-8")
        self._tree = flat
        return flat

    def search_categories(self, q: str) -> list:
        nq = norm(q)
        hits = [t for t in self.tree() if nq in norm(t["name"])] or self.tree()
        return rank(q, hits, "name", top=30)

    def match_category(self, name: str) -> list:
        return rank(name, self.tree(), "name")

    def parameters(self, category_id: int) -> list:
        if category_id not in self._params:
            r = self.http.post(f"{API}/v2/category/{category_id}/parameters", {})
            self._params[category_id] = (r.get("result") or {}).get("parameters") or []
        return self._params[category_id]

    # ------------------------------------------------------------------ источник
    def _mappings(self, offer_ids: list) -> list:
        r = self.http.post(f"{API}/v2/businesses/{self.business}/offer-mappings", {"offerIds": offer_ids})
        return (r.get("result") or {}).get("offerMappings") or []

    def _find(self, art: str) -> list:
        found = self._mappings([art])
        if found:
            return found
        if self._all is None:
            self._all, token = [], ""
            for _ in range(500):
                params = {"limit": 200, **({"page_token": token} if token else {})}
                r = self.http.post(f"{API}/v2/businesses/{self.business}/offer-mappings", {}, params=params)["result"]
                self._all += [m["offer"]["offerId"] for m in r.get("offerMappings") or []]
                token = (r.get("paging") or {}).get("nextPageToken") or ""
                if not token:
                    break
        low = art.lower()
        ids = [o for o in self._all if o.lower() == low or any(o.lower().startswith(low + s) for s in SIZE_SEPARATORS)]
        out = []
        for i in range(0, len(ids), 100):
            out += self._mappings(ids[i:i + 100])
        return out

    def fetch(self, art: str):
        maps = self._find(art)
        if not maps:
            return None
        ids = [m["offer"]["offerId"] for m in maps]
        cards = {}
        for i in range(0, len(ids), 100):
            r = self.http.post(f"{API}/v2/businesses/{self.business}/offer-cards", {"offerIds": ids[i:i + 100]})
            for c in (r.get("result") or {}).get("offerCards") or []:
                cards[c["offerId"]] = c
        first = maps[0]["offer"]
        cat_id = first.get("marketCategoryId") or (maps[0].get("mapping") or {}).get("marketCategoryId")
        params = {p["id"]: p for p in self.parameters(cat_id)} if cat_id else {}

        def chars_of(oid):
            grouped = {}
            for pv in (cards.get(oid) or {}).get("parameterValues") or []:
                p = params.get(pv.get("parameterId")) or {}
                val = pv.get("value")
                if not val and pv.get("valueId"):
                    val = next((v["value"] for v in p.get("values") or [] if v["id"] == pv["valueId"]), "")
                if val not in (None, ""):
                    grouped.setdefault(pv["parameterId"], {"name": p.get("name") or str(pv["parameterId"]), "values": [], "id": pv["parameterId"]})["values"].append(str(val))
            return list(grouped.values())

        variants = []
        for m in maps:
            o = m["offer"]
            vc = chars_of(o["offerId"])
            size = next((c["values"][0] for c in vc if norm(c["name"]) in ("размер производителя", "размер")), "")
            ru = next((c["values"][0] for c in vc if norm(c["name"]) == "российский размер"), "")
            bp = o.get("basicPrice") or {}
            price = float(bp.get("value") or 0) or None
            old = float(bp.get("discountBase") or 0) or None
            variants.append({"id": o["offerId"], "size": size, "ru_size": ru, "barcodes": list(o.get("barcodes") or []),
                             "price": price, "old_price": old if old and price and old > price else None, "chars": vc})
        chars = chars_of(first["offerId"])
        if first.get("manufacturerCountries"):
            chars.append({"name": "Страна производства", "values": list(first["manufacturerCountries"])})
        wd = first.get("weightDimensions") or {}
        category = (maps[0].get("mapping") or {}).get("marketCategoryName") or first.get("category") or ""
        if not category and cat_id:
            category = next((t["name"] for t in self.tree() if t["id"] == cat_id), "")
        base = ids[0] if len(ids) == 1 else art
        return {"key": first.get("vendorCode") if len(ids) > 1 and first.get("vendorCode") else base, "source_mp": self.mp, "source_ids": ids,
                "title": first.get("name") or "", "description": first.get("description") or "", "brand": first.get("vendor") or "",
                "category": category, "chars": chars, "photos": list(first.get("pictures") or []), "videos": list(first.get("videos") or []),
                "dims": {"length": wd.get("length"), "width": wd.get("width"), "height": wd.get("height"), "weight": wd.get("weight")},
                "variants": variants,
                "native": {"category_id": cat_id, "parameter_values": {oid: (cards.get(oid) or {}).get("parameterValues") or [] for oid in ids}}}

    # ------------------------------------------------------------------ приёмник
    def prepare_variant(self, card: dict, variant: dict, cfg: dict, category: dict, extras: dict, offer_id: str, model: str) -> dict:
        cid = category["id"]
        params = self.parameters(cid)
        same = card["source_mp"] == self.mp and card["native"].get("category_id") == cid
        missing, filled, values_out = [], {}, []
        if same:
            values_out = [dict(pv) for pv in card["native"]["parameter_values"].get(variant["id"]) or []]
        have = {pv["parameterId"] for pv in values_out}
        chars = list(variant.get("chars") or []) + [c for c in card["chars"] if norm(c["name"]) not in {norm(x["name"]) for x in variant.get("chars") or []}]
        ex = {norm(k): v for k, v in extras.items()}
        for p in params:
            pid, name = p["id"], p.get("name") or ""
            ln = norm(name)
            if pid in have:
                filled[name] = [pv.get("value") or pv.get("valueId") for pv in values_out if pv["parameterId"] == pid]
                continue
            if ln in ex:
                if ex[ln] is None:
                    continue
                values = ex[ln] if isinstance(ex[ln], list) else [ex[ln]]
            elif same:
                continue
            elif "размер" in ln and (variant.get("size") or variant.get("ru_size")) and ("российск" in ln or ln in ("размер", "размер производителя")):
                values = [variant.get("ru_size") or variant.get("size")] if "российск" in ln else [variant.get("size") or variant.get("ru_size")]
            elif ln in ("бренд", "производитель"):
                values = [card["brand"]]
            elif ln.startswith("название модели") or ln == "модель":
                values = [model]
            else:
                values = find_values(name, chars)
            values = [str(v).strip() for v in values or [] if str(v).strip()]
            if not values:
                if p.get("required"):
                    missing.append(name)
                continue
            if not p.get("multivalue"):
                values = values[:1]
            got = []
            for v in values:
                t = p.get("type")
                if t == "ENUM":
                    opts = p.get("values") or []
                    hit = next((o for o in opts if norm(o["value"]) == norm(v)), None)
                    if not hit and opts:
                        best = max(opts, key=lambda o: sim(v, o["value"]))
                        hit = best if sim(v, best["value"]) >= 0.85 else None
                    if hit:
                        got.append({"parameterId": pid, "valueId": hit["id"], "value": hit["value"]})
                    elif p.get("allowCustomValues"):
                        got.append({"parameterId": pid, "value": v})
                elif t == "NUMERIC":
                    n = to_number(v)
                    if n is not None:
                        pv = {"parameterId": pid, "value": str(n)}
                        unit = (p.get("unit") or {}).get("defaultUnitId")
                        if unit:
                            pv["unitId"] = unit
                        got.append(pv)
                elif t == "BOOLEAN":
                    got.append({"parameterId": pid, "value": "true" if norm(v) in ("да", "есть", "true", "1") else "false"})
                else:
                    got.append({"parameterId": pid, "value": v})
            if got:
                values_out += got
                filled[name] = [g["value"] for g in got]
            elif p.get("required"):
                missing.append(f"{name} (значение «{values[0]}» не найдено в справочнике Маркета)")

        price, old = _prices(variant, cfg)
        d = card["dims"]
        countries = next((c["values"] for c in card["chars"] if norm(c["name"]) in ("страна производства", "страна изготовитель")), [])
        offer = {"offerId": offer_id, "name": card["title"], "marketCategoryId": cid, "pictures": card["photos"][:30], "videos": card["videos"][:5],
                 "vendor": card["brand"], "vendorCode": model, "description": card["description"][:6000],
                 "barcodes": list(variant.get("barcodes") or []), "manufacturerCountries": countries[:5],
                 "weightDimensions": {"length": to_number(d.get("length")) or 0, "width": to_number(d.get("width")) or 0,
                                      "height": to_number(d.get("height")) or 0, "weight": to_number(d.get("weight")) or 0},
                 "parameterValues": values_out}
        if price:
            offer["basicPrice"] = {"value": price, "currencyId": "RUR", **({"discountBase": old} if old else {})}
        offer = {k: v for k, v in offer.items() if v not in ("", None, [])}
        if not price:
            missing.append("цена")
        if not card["photos"]:
            missing.append("фото")
        if not (d.get("length") and d.get("weight")):
            missing.append("габариты/вес")
        return {"offer_id": offer_id, "title": card["title"], "price": price, "old_price": old, "sizes": [variant.get("size") or "—"],
                "missing": missing, "filled": filled, "notes": [], "payload": offer}

    def upload(self, items: list, cfg: dict) -> list:
        results = []
        for i in range(0, len(items), 100):
            batch = items[i:i + 100]
            try:
                r = self.http.post(f"{API}/v2/businesses/{self.business}/offer-mappings/update",
                                   {"offerMappings": [{"offer": it["payload"]} for it in batch]})
            except TransferError as exc:
                results += [{"offer_id": it["offer_id"], "status": "error", "message": str(exc)} for it in batch]
                continue
            by = {x.get("offerId"): x for x in r.get("results") or []}
            for it in batch:
                x = by.get(it["offer_id"]) or {}
                errs = "; ".join(e.get("message") or e.get("type") or "" for e in x.get("errors") or [])
                warns = "; ".join(e.get("message") or e.get("type") or "" for e in x.get("warnings") or [])
                results.append({"offer_id": it["offer_id"], "status": "error" if errs else "ok",
                                "message": errs or ("отправлен в Маркет" + (f" · замечания: {warns}" if warns else ""))})
        return results


def _prices(v: dict, cfg: dict):
    mult = float(cfg.get("price_multiplier") or 1)
    price = round(float(v["price"]) * mult) if v.get("price") else None
    old = round(float(v["old_price"]) * mult) if v.get("old_price") else None
    if old and price and old <= price:
        old = None
    fixed = float(cfg.get("old_price_fixed") or 0)
    if fixed and price:
        old = int(fixed) if fixed > price else None
    return price, old
