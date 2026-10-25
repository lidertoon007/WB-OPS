"""Ozon: чтение товаров (источник) и создание товаров (приёмник). Логика приёмника — из wb2ozon.py."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .common import Http, TransferError, find_values, norm, rank, sim, to_number

API = "https://api-seller.ozon.ru"
NO_BRAND_ID = 126745801            # «Нет бренда» в справочнике Ozon
SIZE_SEPARATORS = ("_", "-", "/", " ")


class Ozon:
    mp = "ozon"

    def __init__(self, cabinet, cache_dir: Path, log=None):
        if not (cabinet.token and cabinet.client_id):
            raise TransferError(f"У кабинета Ozon «{cabinet.name}» нужны Client ID и ключ API")
        self.cabinet = cabinet
        self.log = log or (lambda *_: None)
        self.http = Http({"Client-Id": cabinet.client_id, "Api-Key": cabinet.token}, pause=0.2, log=self.log)
        self.cache_dir = Path(cache_dir)
        self._attrs, self._values, self._tree, self._all_offers = {}, {}, None, None

    def close(self):
        self.http.close()

    def post(self, path, body):
        return self.http.post(API + path, body)

    # ------------------------------------------------------------------ дерево категорий
    def tree(self) -> list:
        if self._tree is not None:
            return self._tree
        path = self.cache_dir / "ozon_tree.json"
        if path.exists() and time.time() - path.stat().st_mtime < 14 * 86400:
            self._tree = json.loads(path.read_text(encoding="utf-8"))
            return self._tree
        tree = self.post("/v1/description-category/tree", {"language": "DEFAULT"})["result"]
        flat = []

        def walk(nodes, cat_id, trail):
            for n in nodes:
                if n.get("disabled"):
                    continue
                if n.get("type_id"):
                    flat.append({"description_category_id": cat_id, "type_id": n["type_id"], "type_name": n["type_name"], "path": " / ".join(trail)})
                else:
                    walk(n.get("children") or [], n.get("description_category_id"), trail + [n.get("category_name", "")])
        walk(tree, None, [])
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(flat, ensure_ascii=False), encoding="utf-8")
        self._tree = flat
        return flat

    def search_categories(self, q: str) -> list:
        nq = norm(q)
        hits = [t for t in self.tree() if nq in norm(t["type_name"])] or self.tree()
        return [_cat(t) for t in rank(q, hits, "type_name", top=30)]

    def match_category(self, name: str) -> list:
        return [_cat(t) for t in rank(name, self.tree(), "type_name")]

    def attributes(self, cat_id, type_id) -> list:
        key = (cat_id, type_id)
        if key not in self._attrs:
            self._attrs[key] = self.post("/v1/description-category/attribute",
                                         {"description_category_id": cat_id, "type_id": type_id, "language": "DEFAULT"})["result"]
        return self._attrs[key]

    def dict_value(self, cat_id, type_id, attr_id, text):
        key = (cat_id, type_id, attr_id, norm(text))
        if key in self._values:
            return self._values[key]
        found = None
        try:
            res = self.post("/v1/description-category/attribute/values/search",
                            {"attribute_id": attr_id, "description_category_id": cat_id, "type_id": type_id,
                             "value": str(text)[:100], "limit": 100}).get("result") or []
            best, best_s = None, 0.0
            nt = norm(text)
            for r in res:
                nv = norm(r["value"])
                if nv == nt:
                    s = 1.0
                elif len(nt) >= 4 and nt[0].isdigit() and (nv.startswith(nt + " ") or nv.replace(" ", "").startswith(nt.replace(" ", ""))):
                    s = 0.95          # коды (ТН ВЭД и т.п.): значение справочника начинается с кода
                else:
                    s = sim(text, r["value"])
                if s > best_s:
                    best, best_s = r, s
            if best and best_s >= 0.85:
                found = {"dictionary_value_id": best["id"], "value": best["value"]}
        except TransferError as exc:
            self.log(f"    поиск значения «{text}»: {exc}")
        self._values[key] = found
        return found

    # ------------------------------------------------------------------ источник
    def _find_offers(self, art: str) -> list:
        r = self.post("/v3/product/list", {"filter": {"offer_id": [art], "visibility": "ALL"}, "limit": 100})
        items = (r.get("result") or {}).get("items") or []
        if items:
            return [i["offer_id"] for i in items]
        if self._all_offers is None:
            self._all_offers, last = [], ""
            for _ in range(200):
                r = self.post("/v3/product/list", {"filter": {"visibility": "ALL"}, "last_id": last, "limit": 1000})["result"]
                self._all_offers += [i["offer_id"] for i in r.get("items") or []]
                last = r.get("last_id") or ""
                if not last or not r.get("items"):
                    break
        low = art.lower()
        return [o for o in self._all_offers if o.lower() == low or any(o.lower().startswith(low + s) for s in SIZE_SEPARATORS)]

    def fetch(self, art: str):
        offers = self._find_offers(art)
        if not offers:
            return None
        info = {i["offer_id"]: i for i in (self.post("/v3/product/info/list", {"offer_id": offers}).get("items") or [])}
        attrs = {}
        last = ""
        for _ in range(20):
            r = self.post("/v4/product/info/attributes", {"filter": {"offer_id": offers, "visibility": "ALL"}, "limit": 100, "last_id": last})
            for a in r.get("result") or []:
                attrs[a["offer_id"]] = a
            last = r.get("last_id") or ""
            if not last or not r.get("result"):
                break
        first = attrs.get(offers[0]) or {}
        cat_id = first.get("description_category_id") or (info.get(offers[0]) or {}).get("description_category_id")
        type_id = first.get("type_id") or (info.get(offers[0]) or {}).get("type_id")
        names = {a["id"]: a["name"] for a in self.attributes(cat_id, type_id)} if cat_id and type_id else {}
        type_name = next((t["type_name"] for t in self.tree() if t["type_id"] == type_id), "")

        def chars_of(a):
            out = []
            for at in a.get("attributes") or []:
                vals = [str(v.get("value")).strip() for v in at.get("values") or [] if str(v.get("value") or "").strip()]
                if vals:
                    out.append({"name": names.get(at["id"], str(at["id"])), "values": vals, "id": at["id"]})
            return out

        def one(a, ids):
            for c in chars_of(a):
                if c["id"] in ids:
                    return c["values"][0]
            return ""

        variants = []
        for oid in offers:
            a, i = attrs.get(oid) or {}, info.get(oid) or {}
            vc = chars_of(a)
            size = next((c["values"][0] for c in vc if norm(c["name"]) in ("размер производителя", "размер")), "")
            ru = next((c["values"][0] for c in vc if norm(c["name"]) == "российский размер"), "")
            price = float(i.get("price") or 0) or None
            old = float(i.get("old_price") or 0) or None
            variants.append({"id": oid, "size": size, "ru_size": ru, "barcodes": list(a.get("barcodes") or i.get("barcodes") or []),
                             "price": price, "old_price": old if old and price and old > price else None, "chars": vc})
        unit = {"mm": 0.1, "cm": 1, "in": 2.54}.get(first.get("dimension_unit") or "mm", 0.1)
        wunit = {"g": 0.001, "kg": 1, "lb": 0.4536}.get(first.get("weight_unit") or "g", 0.001)
        photos = [p for p in [first.get("primary_image")] + list(first.get("images") or []) if p]
        base = offers[0] if len(offers) == 1 else (one(first, {9048, 8292}) or art)
        return {"key": base, "source_mp": self.mp, "source_ids": offers,
                "title": first.get("name") or (info.get(offers[0]) or {}).get("name") or "", "description": one(first, {4191}),
                "brand": one(first, {85, 31}), "category": type_name, "chars": chars_of(first),
                "photos": list(dict.fromkeys(photos)), "videos": [],
                "dims": {"length": (first.get("depth") or 0) * unit, "width": (first.get("width") or 0) * unit,
                         "height": (first.get("height") or 0) * unit, "weight": (first.get("weight") or 0) * wunit},
                "variants": variants,
                "native": {"description_category_id": cat_id, "type_id": type_id,
                           "attributes": {oid: (attrs.get(oid) or {}).get("attributes") or [] for oid in offers}}}

    # ------------------------------------------------------------------ приёмник
    def prepare_variant(self, card: dict, variant: dict, cfg: dict, category: dict, extras: dict, offer_id: str, model: str) -> dict:
        cat, tid = category["description_category_id"], category["type_id"]
        attrs = self.attributes(cat, tid)
        same = card["source_mp"] == self.mp and card["native"].get("type_id") == tid
        missing, filled, out = [], {}, []
        if same:
            native = card["native"]["attributes"].get(variant["id"]) or []
            out = [{"id": a["id"], "complex_id": a.get("complex_id", 0), "values": a.get("values") or []} for a in native]
            filled = {next((x["name"] for x in attrs if x["id"] == a["id"]), str(a["id"])): [v.get("value") for v in a["values"]] for a in out}
        have = {a["id"] for a in out}
        chars = _merge(card["chars"], variant.get("chars") or [])
        ex = {norm(k): v for k, v in extras.items()}
        for a in attrs:
            if a.get("attribute_complex_id") or a["id"] in have:
                continue
            aid, name = a["id"], a["name"]
            ln = norm(name)
            values, from_extras = None, ln in ex
            if from_extras:
                if ex[ln] is None:
                    continue
                values = ex[ln] if isinstance(ex[ln], list) else [ex[ln]]
            elif same:
                if aid not in (9048, 8292):
                    continue
                values = [model]
            elif aid in (85, 31) or ln == "бренд" or ln.startswith("бренд в"):
                values = [card["brand"]]
            elif aid == 4180 or ln == "название":
                values = [card["title"]]
            elif aid == 4191 or ln == "аннотация":
                values = [card["description"]]
            elif aid in (9048, 8292) or ln.startswith("название модели") or ln.startswith("объединить на одной карточке"):
                values = [model]
            elif "размер" in ln and (variant.get("size") or variant.get("ru_size")) and ("российский" in ln or ln == "размер производителя" or ln == "размер"):
                values = [variant.get("ru_size") or variant.get("size")] if "российский" in ln else [variant.get("size") or variant.get("ru_size")]
            elif aid == 8229 or ln == "тип":
                continue
            if values is None:
                values = find_values(name, chars)
            values = [v for v in values or [] if str(v).strip()]
            if not values:
                if a.get("is_required"):
                    missing.append(name)
                continue
            if not a.get("is_collection"):
                values = values[:1]
            elif a.get("max_value_count"):
                values = values[: a["max_value_count"]]
            vals = []
            for v in values:
                v = str(v).strip()
                if a.get("dictionary_id"):
                    dv = self.dict_value(cat, tid, aid, v)
                    if dv is None and re.fullmatch(r"\d+\s*-\s*\d+", v):
                        dv = self.dict_value(cat, tid, aid, v.split("-")[0].strip())
                    if dv is None and not from_extras and (aid in (85, 31) or ln.startswith("бренд")):
                        dv = self.dict_value(cat, tid, aid, "Нет бренда") or {"dictionary_value_id": NO_BRAND_ID, "value": "Нет бренда"}
                    if dv:
                        vals.append(dv)
                else:
                    t = (a.get("type") or "").lower()
                    if t == "integer":
                        n = to_number(v, True)
                        v = str(n) if n is not None else ""
                    elif t == "decimal":
                        n = to_number(v)
                        v = str(n) if n is not None else ""
                    elif t == "boolean":
                        v = "true" if norm(v) in ("да", "есть", "true", "1") else "false"
                    if v:
                        vals.append({"value": v})
            if vals:
                out.append({"id": aid, "complex_id": 0, "values": vals})
                filled[name] = [x["value"] for x in vals]
            elif a.get("is_required"):
                missing.append(f"{name} (значение «{values[0]}» не найдено в справочнике Ozon)")
        # Ozon: «большие размеры» нельзя для российского размера до 50 включительно
        ru_attr = next((x["id"] for x in attrs if norm(x["name"]) == "российский размер"), None)
        ru = next((a for a in out if a["id"] == ru_attr), None)
        ru_n = to_number(ru["values"][0]["value"]) if ru and ru["values"] else None
        if ru_n and float(ru_n) <= 50:
            for a in out:
                a["values"] = [v for v in a["values"] if norm(v.get("value")) != "большие размеры"]
            out = [a for a in out if a["values"]]

        price, old = _prices(variant, cfg)
        d = card["dims"]
        item = {"offer_id": offer_id, "name": card["title"], "description_category_id": cat, "type_id": tid,
                "price": str(price or ""), "old_price": str(old or ""), "vat": str(cfg.get("vat", "0")), "currency_code": "RUB",
                "barcode": (variant.get("barcodes") or [""])[0],
                "depth": int(round(float(d.get("length") or 0) * 10)), "width": int(round(float(d.get("width") or 0) * 10)),
                "height": int(round(float(d.get("height") or 0) * 10)), "dimension_unit": "mm",
                "weight": int(round(float(d.get("weight") or 0) * 1000)), "weight_unit": "g",
                "images": card["photos"][1:15], "primary_image": card["photos"][0] if card["photos"] else "", "attributes": out}
        if not price:
            missing.append("цена")
        if not card["photos"]:
            missing.append("фото")
        if not item["weight"] or not item["depth"]:
            missing.append("габариты/вес")
        return {"offer_id": offer_id, "title": card["title"], "price": price, "old_price": old, "sizes": [variant.get("size") or "—"],
                "missing": missing, "filled": filled, "notes": [], "payload": item}

    def upload(self, items: list, cfg: dict) -> list:
        results = []
        payloads = [{k: v for k, v in it["payload"].items() if v not in ("", None, [])} for it in items]
        for i in range(0, len(payloads), 100):
            batch = payloads[i:i + 100]
            try:
                task = self.post("/v3/product/import", {"items": batch})["result"]["task_id"]
            except TransferError as exc:
                results += [{"offer_id": p["offer_id"], "status": "error", "message": str(exc)} for p in batch]
                continue
            self.log(f"  пакет {i // 100 + 1}: задача Ozon {task}, жду результат…")
            info = []
            for _ in range(60):
                time.sleep(5)
                info = self.post("/v1/product/import/info", {"task_id": task})["result"]["items"]
                if info and all(x.get("status") not in ("pending", "") for x in info):
                    break
            for x in info:
                errs = "; ".join(f'{e.get("attribute_name") or e.get("field") or ""}: {e.get("description") or e.get("message") or e.get("code")}'
                                 for e in x.get("errors") or [])
                ok = x.get("status") == "imported"
                results.append({"offer_id": x["offer_id"], "status": "ok" if ok else ("pending" if x.get("status") == "pending" else "error"),
                                "message": ("создан" if ok else x.get("status", "")) + (f" · {errs}" if errs else ""), "target_id": x.get("product_id")})
        ok_ids = {r["offer_id"] for r in results if r["status"] == "ok"}
        problems = self.set_min_prices([p for p in payloads if p["offer_id"] in ok_ids])
        for r in results:
            if r["offer_id"] in problems:
                r["message"] += f" · мин. цена: {problems[r['offer_id']]}"
        return results

    def set_min_prices(self, payloads: list) -> dict:
        """Минимальная цена для акций = цене продажи."""
        problems = {}
        prices = [{"offer_id": p["offer_id"], "price": p["price"], "old_price": p.get("old_price", "0"), "min_price": p["price"], "currency_code": "RUB"}
                  for p in payloads if p.get("price")]
        for i in range(0, len(prices), 1000):
            try:
                res = self.post("/v1/product/import/prices", {"prices": prices[i:i + 1000]}).get("result") or []
            except TransferError as exc:
                problems.update({p["offer_id"]: str(exc) for p in prices[i:i + 1000]})
                continue
            for r in res:
                if not r.get("updated"):
                    problems[r.get("offer_id")] = "; ".join(f'{e.get("code")}: {e.get("message")}' for e in r.get("errors") or [])
        return problems

    # ------------------------------------------------------------------ декларация
    def bind_certificate(self, cfg: dict, offer_ids: list, files: list) -> str:
        """Создать декларацию (если ещё не создана) и привязать к товарам. Возвращает certificate_id."""
        import httpx
        cert_id = cfg.get("certificate_id")
        if not cert_id:
            if not files:
                raise TransferError("Загрузите скан декларации в настройках переноса")
            types = _list(self.http.get(API + "/v1/product/certificate/types"))
            acc = []
            for path in ("/v2/product/certificate/accordance-types/list", "/v1/product/certificate/accordance-types"):
                try:
                    acc = _list(self.http.get(API + path))
                    if acc:
                        break
                except TransferError:
                    continue
            t = _pick(types, ["декларац", "declaration"]) or {}
            a = _pick(acc, ["017", "ТР ТС", "ТР ЕАЭС"]) or {}
            data = {"name": cfg.get("name") or "Декларация соответствия", "number": cfg["number"],
                    "type_code": t.get("code") or t.get("value") or "", "accordance_type_code": a.get("code") or a.get("value") or "",
                    "issue_date": cfg["issue_date"] + "T00:00:00Z", "expire_date": cfg["expire_date"] + "T00:00:00Z"}
            r = httpx.post(API + "/v1/product/certificate/create", headers={k: v for k, v in self.http.headers.items()}, data=data,
                           files=[("files", (Path(f).name, open(f, "rb"))) for f in files], timeout=120)
            if r.status_code >= 400:
                raise TransferError(f"Ozon не принял декларацию: {r.text[:300]}")
            j = r.json()
            res = j.get("result")
            cert_id = j.get("id") or (res.get("id") if isinstance(res, dict) else res)
        ids = []
        for i in range(0, len(offer_ids), 1000):
            res = self.post("/v3/product/info/list", {"offer_id": offer_ids[i:i + 1000]})
            ids += [it["id"] for it in res.get("items") or []]
        if ids:
            self.post("/v1/product/certificate/bind", {"certificate_id": int(cert_id), "product_id": ids})
        return str(cert_id)


def _cat(t: dict) -> dict:
    return {"id": f'{t["description_category_id"]}:{t["type_id"]}', "name": t["type_name"], "path": t.get("path", ""),
            "description_category_id": t["description_category_id"], "type_id": t["type_id"], "score": t.get("score")}


def _merge(base: list, extra: list) -> list:
    names = {norm(c["name"]) for c in extra}
    return list(extra) + [c for c in base if norm(c["name"]) not in names]


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


def _pick(items, words):
    for w in words:
        for it in items:
            if w.lower() in (str(it.get("name", "")) + " " + str(it.get("code", ""))).lower():
                return it
    return items[0] if items else None


def _list(res):
    if isinstance(res, list):
        return res
    r = res.get("result", res)
    if isinstance(r, dict):
        for k in ("items", "certificate_types", "types", "accordance_types"):
            if isinstance(r.get(k), list):
                return r[k]
    return r if isinstance(r, list) else []
