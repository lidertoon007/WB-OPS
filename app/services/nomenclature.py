"""Номенклатура: все товары из карточек WB, отчёта ОПиУ, остатков и справочника себестоимости.

Себестоимость и подготовка хранятся там же, где их берёт расчёт ОПиУ, — в `opiu/cost.csv`
(колонки «сс_за_ед» и «подготовка_за_ед», цена может меняться с даты «действует_с»).
Здесь их можно посмотреть и поправить; после правки ОПиУ нужно пересобрать (из кэша — быстро).
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

import httpx

COST_COL, PREP_COL = "сс_за_ед", "подготовка_за_ед"
PRICE_HEADER = ["артикул", "кратность", COST_COL, PREP_COL, "действует_с", "название (справочно)",
                "бренд", "категория", "маркетплейсы (справочно)", "продано шт. с 01.2026 (справочно)"]
CARDS_URL = "https://content-api.wildberries.ru/content/v2/get/cards/list"
ALL_MP = "Все маркетплейсы"


class NomenclatureError(Exception):
    pass


def art_key(value) -> str:
    """Как в opiu_all.py: рт-00028973 и РТ-00028973 — один товар."""
    return re.sub(r"\s+", "", str(value or "")).upper()


def num(value) -> float | None:
    text = str(value or "").replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return float(text) if text else None
    except ValueError:
        return None


def fmt(value: float) -> str:
    return f"{value:.2f}".replace(".", ",")


def norm_date(value) -> str:
    """«2026-11», «2026-11-05», «01.11.2026» → «2026-11-01» / «2026-11-05»; пусто — действует всегда."""
    text = str(value or "").strip()
    if not text:
        return ""
    m = re.fullmatch(r"(\d{4})-(\d{2})(?:-(\d{2}))?", text) or None
    if m:
        y, mo, d = int(m[1]), int(m[2]), int(m[3] or 1)
    else:
        m = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", text)
        if not m:
            raise NomenclatureError("Дата «действует с» — в формате ГГГГ-ММ или ДД.ММ.ГГГГ")
        d, mo, y = int(m[1]), int(m[2]), int(m[3])
    try:
        return date(y, mo, d).isoformat()
    except ValueError as exc:
        raise NomenclatureError("Неверная дата") from exc


class Nomenclature:
    def __init__(self, project_dir, cabinets=lambda: [], cards_path="app/knowledge/wb_cards.json",
                 vendor_path="app/knowledge/nm_vendor.json", models=None, product_names=lambda: {}):
        self.project_dir = project_dir          # callable -> Path папки opiu
        self.cabinets = cabinets                # callable -> [Cabinet]
        self.cards_path = Path(cards_path)
        self.vendor_path = Path(vendor_path)
        self.models = models
        self.product_names = product_names
        self._report_cache: tuple | None = None

    # ------------------------------------------------------------------ cost.csv
    def cost_path(self) -> Path:
        return Path(self.project_dir()) / "cost.csv"

    def read_cost_rows(self) -> tuple[list[str], list[dict]]:
        path = self.cost_path()
        if not path.exists():
            return list(PRICE_HEADER), []
        with open(path, encoding="utf-8-sig", newline="") as fh:
            sample = fh.read(4096)
            fh.seek(0)
            delimiter = ";" if sample.count(";") >= sample.count(",") else ","
            reader = csv.DictReader(fh, delimiter=delimiter)
            header = [(h or "").strip() for h in (reader.fieldnames or [])]
            rows = [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items() if k} for r in reader]
        header = [h for h in header if h] or list(PRICE_HEADER)
        for col in PRICE_HEADER:
            if col not in [h.lower() for h in header]:
                header.append(col)
        return header, rows

    def write_cost_rows(self, header: list[str], rows: list[dict]) -> None:
        path = self.cost_path()
        if path.exists():
            shutil.copy2(path, path.with_name("cost.csv.bak"))      # предыдущая версия на всякий случай
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
        writer.writerow(header)
        for r in rows:
            writer.writerow([r.get(h.lower(), "") for h in header])
        tmp = path.with_name("cost.csv.tmp")
        tmp.write_text(buf.getvalue(), encoding="utf-8-sig", newline="")
        try:
            os.replace(tmp, path)
        except PermissionError as exc:
            tmp.unlink(missing_ok=True)
            raise NomenclatureError("cost.csv открыт в другой программе (например, Excel) — закройте его и повторите") from exc

    @staticmethod
    def current_price(rows: list[dict], today: str | None = None) -> dict | None:
        """Строка цены, действующая сейчас: наибольшая дата «действует_с», не позже сегодняшнего месяца."""
        month = (today or date.today().isoformat())[:7]
        chosen = None
        for r in sorted(rows, key=lambda x: x.get("действует_с", "")):
            if not r.get("действует_с") or r["действует_с"][:7] <= month:
                chosen = r
        return chosen

    def update_price(self, article: str, *, cost=None, prep=None, multiplier=None, effective_from=None,
                     brand=None, category=None, name=None) -> dict:
        key = art_key(article)
        if not key:
            raise NomenclatureError("Не указан артикул")
        for label, value, top in (("Себестоимость", cost, 1_000_000), ("Подготовка", prep, 100_000), ("Кратность", multiplier, 1000)):
            if value is not None and not (0 <= value <= top):
                raise NomenclatureError(f"{label}: число от 0 до {top:,}".replace(",", " "))
        if multiplier is not None and multiplier <= 0:
            raise NomenclatureError("Кратность должна быть больше нуля")
        header, rows = self.read_cost_rows()
        own = [r for r in rows if art_key(r.get("артикул")) == key]
        if effective_from is not None:                         # новая цена с даты (или правка строки этой даты)
            day = norm_date(effective_from)
            target = next((r for r in own if r.get("действует_с", "") == day), None)
            if target is None:
                base = self.current_price(own) or (own[-1] if own else {})
                target = {**{k: v for k, v in base.items() if k not in ("действует_с",)}, "артикул": base.get("артикул") or article.strip(),
                          "действует_с": day, "кратность": base.get("кратность") or "1"}
                self._insert(rows, own, target)
        else:                                                   # правка действующей цены
            target = self.current_price(own) or (own[-1] if own else None)
            if target is None:
                target = {"артикул": article.strip(), "кратность": "1", "действует_с": ""}
                self._insert(rows, own, target)
        if cost is not None:
            target[COST_COL] = fmt(cost)
        if prep is not None:
            target[PREP_COL] = fmt(prep)
        if multiplier is not None:
            target["кратность"] = f"{multiplier:g}"
        for col, value in (("бренд", brand), ("категория", category), ("название (справочно)", name)):
            if value is not None:
                for r in own or [target]:          # справочные поля одинаковы во всех строках артикула
                    r[col] = str(value).strip()[:120]
                target[col] = str(value).strip()[:120]
        self.write_cost_rows(header, rows)
        self._drop_missing(key)
        return target

    @staticmethod
    def _insert(rows: list[dict], own: list[dict], new: dict) -> None:
        """Новая строка — сразу после строк того же артикула (порядок файла сохраняется)."""
        if own:
            rows.insert(max(rows.index(r) for r in own) + 1, new)
        else:
            rows.append(new)

    def delete_price(self, article: str, effective_from: str) -> None:
        key, day = art_key(article), norm_date(effective_from)
        if not day:
            raise NomenclatureError("Базовую цену (без даты) удалить нельзя — измените её")
        header, rows = self.read_cost_rows()
        left = [r for r in rows if not (art_key(r.get("артикул")) == key and r.get("действует_с", "") == day)]
        if len(left) == len(rows):
            raise NomenclatureError("Цена с этой датой не найдена")
        self.write_cost_rows(header, left)

    def _drop_missing(self, key: str) -> None:
        """Артикул с ценой больше не «без себестоимости»: убираем его из cost_missing.csv."""
        path = Path(self.project_dir()) / "cost_missing.csv"
        if not path.exists():
            return
        with open(path, encoding="utf-8-sig", newline="") as fh:
            lines = list(csv.reader(fh, delimiter=";"))
        if not lines:
            return
        kept = [lines[0]] + [r for r in lines[1:] if r and art_key(r[0]) != key]
        if len(kept) != len(lines):
            buf = io.StringIO()
            csv.writer(buf, delimiter=";", lineterminator="\r\n").writerows(kept)
            try:
                path.write_text(buf.getvalue(), encoding="utf-8-sig", newline="")
            except PermissionError:
                pass

    # ------------------------------------------------------------------ источники
    def report_goods(self) -> dict:
        """Продажи по артикулам из свежего отчёта ОПиУ (лист «Товары»)."""
        from app.services import finance
        try:
            project = Path(self.project_dir())
        except Exception:
            return {"rows": {}, "file": None}
        path = finance.latest_report(project)
        if path is None:
            return {"rows": {}, "file": None}
        stamp = path.stat().st_mtime
        if self._report_cache and self._report_cache[0] == (str(path), stamp):
            return self._report_cache[1]
        out = defaultdict(lambda: {"sold": 0, "revenue": 0.0, "marketplaces": set(), "name": ""})
        try:
            wb = finance._load_workbook(path)
            goods = finance.parse_goods(wb[finance.GOODS_SHEET], limit=100000) if finance.GOODS_SHEET in wb.sheetnames else {"header": [], "rows": []}
        except Exception:
            goods = {"header": [], "rows": []}
        h = {name: i for i, name in enumerate(goods["header"])}
        for r in goods["rows"]:
            get = lambda col: r[h[col]] if col in h and h[col] < len(r) else None
            key = art_key(get("Артикул"))
            if not key:
                continue
            mp = str(get("Маркетплейс") or "")
            item = out[key]
            item["display"] = str(get("Артикул"))
            item["name"] = item["name"] or str(get("Название") or "")
            if mp == ALL_MP or not mp:
                item["sold"] = get("Штук") or 0
                item["revenue"] = get("Выручка") or 0
            else:
                item["marketplaces"].add(mp)
                if not any(x == ALL_MP for x in [mp]) and not item["sold"]:
                    item["_sold_mp"] = item.get("_sold_mp", 0) + (get("Штук") or 0)
                    item["_rev_mp"] = item.get("_rev_mp", 0) + (get("Выручка") or 0)
        for item in out.values():
            if not item["sold"] and item.get("_sold_mp"):
                item["sold"], item["revenue"] = item["_sold_mp"], item["_rev_mp"]
        data = {"rows": dict(out), "file": path.name, "updated_at": stamp}
        self._report_cache = ((str(path), stamp), data)
        return data

    def goods_caches(self) -> dict:
        """Остатки и цены WB из кэша расчёта ОПиУ (opiu/Wildberries/cache/*_goods.json)."""
        stocks, prices, at, display = defaultdict(float), {}, None, {}
        try:
            folder = Path(self.project_dir()) / "Wildberries" / "cache"
        except Exception:
            return {"stocks": {}, "prices": {}, "at": None, "display": {}}
        for path in folder.glob("*_goods.json") if folder.exists() else []:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for row in data.get("stocks") or []:
                if len(row) >= 4:
                    display.setdefault(art_key(row[0]), str(row[0]).strip())
                    if row[2] not in ("to_client", "returning"):
                        stocks[art_key(row[0])] += float(row[3] or 0)
            for row in data.get("prices") or []:
                if row and len(row) >= 3:
                    prices[art_key(row[0])] = {"price": row[1], "sale_price": row[2]}
                    display.setdefault(art_key(row[0]), str(row[0]).strip())
            at = max(filter(None, [at, data.get("stocks_at"), data.get("prices_at")]), default=None)
        return {"stocks": dict(stocks), "prices": prices, "at": at, "display": display}

    def cards(self) -> dict:
        try:
            return json.loads(self.cards_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"cards": [], "updated_at": None, "errors": []}

    def refresh_cards(self, extra_tokens: list[tuple[str, str]] | None = None) -> dict:
        """Скачать карточки товаров WB (Контент API) по всем кабинетам с ключом."""
        tokens, seen = [], set()
        for c in self.cabinets():
            if getattr(c, "marketplace", "wildberries") == "wildberries" and c.token and c.token not in seen:
                tokens.append((c.name, c.token)); seen.add(c.token)
        for name, token in extra_tokens or []:
            if token and token not in seen:
                tokens.append((name, token)); seen.add(token)
        if not tokens:
            raise NomenclatureError("Нет кабинетов Wildberries с API-токеном")
        cards, errors = [], []
        with httpx.Client(timeout=30) as client:
            for name, token in tokens:
                cursor, got = {"limit": 100}, 0
                try:
                    for _ in range(500):
                        r = client.post(CARDS_URL, headers={"Authorization": token},
                                        json={"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}})
                        if r.status_code in (401, 403):
                            raise NomenclatureError("токен не даёт доступа к категории «Контент»")
                        if r.status_code == 429:
                            time.sleep(3)
                            continue
                        r.raise_for_status()
                        res = r.json()
                        page = res.get("cards") or []
                        for card in page:
                            photo = ((card.get("photos") or [{}])[0] or {})
                            cards.append({"nm": card.get("nmID"), "vendor": card.get("vendorCode") or "", "title": card.get("title") or "",
                                          "brand": card.get("brand") or "", "subject": card.get("subjectName") or "",
                                          "photo": photo.get("c246x328") or photo.get("tm") or photo.get("big") or "",
                                          "sizes": [s.get("techSize") for s in card.get("sizes") or [] if s.get("techSize")],
                                          "cabinet": name})
                        got += len(page)
                        nxt = res.get("cursor") or {}
                        if len(page) < 100 or not nxt.get("updatedAt"):
                            break
                        cursor = {"limit": 100, "updatedAt": nxt["updatedAt"], "nmID": nxt.get("nmID")}
                        time.sleep(0.7)
                except NomenclatureError as exc:
                    errors.append(f"{name}: {exc}")
                except (httpx.HTTPError, ValueError) as exc:
                    errors.append(f"{name}: {type(exc).__name__}")
        if not cards and errors:
            raise NomenclatureError("Карточки не загружены — " + "; ".join(errors))
        data = {"cards": cards, "updated_at": time.strftime("%Y-%m-%d %H:%M"), "errors": errors}
        self.cards_path.parent.mkdir(parents=True, exist_ok=True)
        self.cards_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return {"cards": len(cards), "errors": errors, "updated_at": data["updated_at"]}

    # ------------------------------------------------------------------ сводный список
    def items(self) -> dict:
        header, cost_rows = self.read_cost_rows()
        report = self.report_goods()
        goods = self.goods_caches()
        cards = self.cards()
        rows: dict[str, dict] = {}

        def row(key, display=""):
            if key not in rows:
                rows[key] = {"key": key, "article": display or key, "name": "", "brand": "", "category": "", "nm_ids": set(),
                             "marketplaces": set(), "photo": "", "sizes": [], "prices": [], "sources": set()}
            elif display and rows[key]["article"] == key:
                rows[key]["article"] = display
            return rows[key]

        by_article = defaultdict(list)
        for r in cost_rows:
            key = art_key(r.get("артикул"))
            if key:
                by_article[key].append(r)
        for key, own in by_article.items():
            item = row(key, own[0].get("артикул"))
            item["prices"] = sorted(own, key=lambda r: r.get("действует_с", ""))
            item["sources"].add("cost")
            for r in own:
                item["brand"] = item["brand"] or r.get("бренд", "")
                item["category"] = item["category"] or r.get("категория", "")
                item["name"] = item["name"] or r.get("название (справочно)", "")
        for c in cards.get("cards", []):
            key = art_key(c.get("vendor"))
            if not key:
                continue
            item = row(key, c.get("vendor"))
            item["sources"].add("wb")
            item["marketplaces"].add("Wildberries")
            if c.get("nm"):
                item["nm_ids"].add(int(c["nm"]))
            item["name"] = c.get("title") or item["name"]
            item["brand"] = item["brand"] or c.get("brand", "")
            item["category"] = item["category"] or c.get("subject", "")
            item["photo"] = item["photo"] or c.get("photo", "")
            item["sizes"] = item["sizes"] or c.get("sizes", [])
        for key, rep in report["rows"].items():
            item = row(key, rep.get("display"))
            item["sources"].add("report")
            item["marketplaces"] |= rep["marketplaces"]
            item["name"] = item["name"] or rep.get("name", "")
        for key in goods["stocks"].keys() | goods["prices"].keys():
            item = row(key, goods["display"].get(key, ""))
            item["sources"].add("stock")
            item["marketplaces"].add("Wildberries")
        try:
            links = json.loads(self.vendor_path.read_text(encoding="utf-8")).get("links", {})
        except (OSError, ValueError):
            links = {}
        names = self.product_names()
        for nm, vendor in links.items():
            key = art_key(vendor)
            if key in rows:
                rows[key]["nm_ids"].add(int(nm))
        if self.models is not None:
            for model_key, model in self.models.all().items():
                key = art_key(model_key)
                if key in rows:
                    rows[key]["nm_ids"] |= {int(n) for n in model.get("nm_ids", [])}
        today = date.today().isoformat()
        out = []
        for key, item in rows.items():
            if not item["name"]:
                item["name"] = next((names.get(nm) for nm in item["nm_ids"] if names.get(nm)), "")
            current = self.current_price(item["prices"], today)
            rep = report["rows"].get(key, {})
            price = goods["prices"].get(key) or {}
            cost = num(current.get(COST_COL)) if current else None
            prep = num(current.get(PREP_COL)) if current else None
            out.append({
                "key": key, "article": item["article"], "name": item["name"], "brand": item["brand"], "category": item["category"],
                "nm_ids": sorted(item["nm_ids"]), "marketplaces": sorted(item["marketplaces"]), "photo": item["photo"], "sizes": item["sizes"],
                "cost": cost, "prep": prep, "multiplier": num(current.get("кратность")) if current else None,
                "effective_from": current.get("действует_с", "") if current else "",
                "history": [{"from": r.get("действует_с", ""), "cost": num(r.get(COST_COL)), "prep": num(r.get(PREP_COL)),
                             "multiplier": num(r.get("кратность"))} for r in item["prices"]],
                "future": [r.get("действует_с") for r in item["prices"] if r.get("действует_с", "")[:7] > today[:7]],
                "sold": rep.get("sold") or 0, "revenue": rep.get("revenue") or 0,
                "stock": goods["stocks"].get(key), "price": price.get("sale_price") or price.get("price"),
                "has_cost": cost is not None, "sources": sorted(item["sources"]),
            })
        out.sort(key=lambda r: (-(r["sold"] or 0), r["article"].lower()))
        return {"items": out, "report": report.get("file"), "cards_at": cards.get("updated_at"), "cards_errors": cards.get("errors") or [],
                "goods_at": goods["at"], "cost_file": str(self.cost_path())}
