"""Номенклатура: все товары из карточек WB, отчёта ОПиУ, остатков и справочника себестоимости.

Себестоимость и подготовка хранятся там же, где их берёт расчёт ОПиУ, — в `opiu/cost.csv`
(колонки «сс_за_ед» и «подготовка_за_ед», цена может меняться с даты «действует_с»).
Здесь их можно посмотреть и поправить; после правки ОПиУ нужно пересобрать (из кэша — быстро).
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import secrets
import threading
import time
import unicodedata
from collections import defaultdict
from datetime import date
from pathlib import Path
from zipfile import ZipFile

import httpx

COST_COL, PREP_COL = "сс_за_ед", "подготовка_за_ед"
PRICE_HEADER = ["артикул", "кратность", COST_COL, PREP_COL, "действует_с", "название (справочно)",
                "бренд", "категория", "маркетплейсы (справочно)", "продано шт. с 01.2026 (справочно)"]
RETRO_FILE, RETRO_COL = "retro_bonus.csv", "ретро_бонус_%"   # ретро-бонусы производителей по брендам (как в opiu_all.py)
CARDS_URL = "https://content-api.wildberries.ru/content/v2/get/cards/list"
MOVE_CARDS_URL = "https://content-api.wildberries.ru/content/v2/cards/moveNm"
MERGE_COLUMNS = ["Номер склейки", "nmID", "Артикул", "Название", "Бренд", "Предмет",
                 "subjectID", "Кабинет", "cabinet_id", "imtID", "Кошки/Собаки"]
# Загрузка справочника из Excel: поле -> начала заголовков колонки (в нижнем регистре)
IMPORT_COLUMNS = (("article", ("артикул", "vendor code", "offer_id")),
                  ("cost", ("себестоимость", "себес", COST_COL, "сс ")),
                  ("prep", ("подготовка", PREP_COL, "фф", "фулфилмент")),
                  ("brand", ("бренд",)),
                  ("category", ("категория", "предмет")),
                  ("name", ("наименование", "название")))
ALL_MP = "Все маркетплейсы"


class NomenclatureError(Exception):
    pass


class MergeUnknownError(NomenclatureError):
    """A move may have reached WB; never automatically retry it."""


def _read_csv(path: Path) -> tuple[list[str], list[dict]]:
    """Заголовок и строки CSV (разделитель «;» или «,»); ключи строк — в нижнем регистре."""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        delimiter = ";" if sample.count(";") >= sample.count(",") else ","
        reader = csv.DictReader(fh, delimiter=delimiter)
        header = [(h or "").strip() for h in (reader.fieldnames or [])]
        rows = [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items() if k} for r in reader]
    return header, rows


def art_key(value) -> str:
    """Как в opiu_all.py: рт-00028973 и РТ-00028973 — один товар."""
    return re.sub(r"\s+", "", str(value or "")).upper()


def brand_key(value) -> str:
    """Как в opiu_all.py: бренд без лишних пробелов и регистра."""
    return re.sub(r"\s+", " ", str(value or "")).strip().upper()


def pet_kind(title, subject="") -> str:
    """Use whole words/animal inflections, avoiding e.g. «котлеты» or «скотч»."""
    words = re.findall(r"[a-zа-яё]+", f"{title or ''} {subject or ''}".casefold().replace("ё", "е"))
    cats = any(re.fullmatch(r"кош(?:к(?:а|и|е|у|ой|ою|ам|ами|ах)|ек|ач[а-я]*)|"
                           r"кот(?:|а|у|ом|е|ы|ов|ам|ами|ах|ят[а-я]*|ен[а-я]*)|cat|cats|kitten|kittens|feline", w)
               for w in words)
    dogs = any(re.fullmatch(r"собак[а-я]*|собач[а-я]*|щен(?:ок|к[а-я]*|ят[а-я]*|яч[а-я]*|оч[а-я]*)|"
                           r"dog|dogs|puppy|puppies|canine", w) for w in words)
    return "Кошки и собаки" if cats and dogs else "Кошки" if cats else "Собаки" if dogs else "Не определено"


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
        self._missing_lock = threading.Lock()
        self._missing_stamp: float | None = None   # mtime cost_missing.csv, уже перенесённого в cost.csv
        self.missing_absorbed = 0
        self._cards_lock = threading.RLock()
        self._move_lock = threading.Lock()
        self._recent_moves = {}

    # ------------------------------------------------------------------ cost.csv
    def cost_path(self) -> Path:
        return Path(self.project_dir()) / "cost.csv"

    def read_cost_rows(self) -> tuple[list[str], list[dict]]:
        self.absorb_missing()
        return self._read_cost_file()

    def absorb_missing(self) -> int:
        """Цены, вписанные в cost_missing.csv (его выписывает расчёт ОПиУ), — в cost.csv. Сам расчёт переносит их
        только при сборке, а «Оцифровка», РНП и номенклатура читают cost.csv — поэтому переносим и здесь, по тем же
        правилам: дописываются только пустые ячейки, цену, уже стоящую в cost.csv, меняют в самом cost.csv.
        Файл перечитывается, только если изменился. Возвращает число дополненных артикулов."""
        try:
            path = Path(self.project_dir()) / "cost_missing.csv"
            stamp = path.stat().st_mtime
        except Exception:
            return 0
        with self._missing_lock:
            if stamp == self._missing_stamp:
                return 0
            columns = (COST_COL, PREP_COL, "бренд", "категория")
            try:
                filled = [r for r in _read_csv(path)[1] if r.get("артикул") and any(r.get(c) for c in columns)]
                header, rows = self._read_cost_file()
                index = {(art_key(r.get("артикул")), r.get("действует_с", "")): r for r in rows}
                changed = 0
                for m in filled:
                    key = (art_key(m["артикул"]), m.get("действует_с", ""))
                    target = index.get(key)
                    if target is None:
                        target = {"артикул": m["артикул"], "кратность": m.get("кратность") or "1",
                                  "действует_с": m.get("действует_с", ""),
                                  "маркетплейсы (справочно)": m.get("маркетплейс (справочно)", ""),
                                  "продано шт. с 01.2026 (справочно)": m.get("продано шт. (справочно)", "")}
                        rows.append(target)
                        index[key] = target
                    updated = False
                    for col in columns:
                        if m.get(col) and not target.get(col):
                            target[col] = m[col]
                            updated = True
                    changed += updated
                if changed:
                    self.write_cost_rows(header, rows)
            except (OSError, NomenclatureError, csv.Error):
                return 0                    # cost.csv открыт в Excel и т. п. — перенесём при следующем чтении
            self._missing_stamp = stamp
            self.missing_absorbed = changed
            return changed

    def _read_cost_file(self) -> tuple[list[str], list[dict]]:
        path = self.cost_path()
        if not path.exists():
            return list(PRICE_HEADER), []
        header, rows = _read_csv(path)
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

    # ------------------------------------------------------------------ retro_bonus.csv
    def retro_path(self) -> Path:
        return Path(self.project_dir()) / RETRO_FILE

    def retro_rows(self) -> list[dict]:
        """Ретро-бонусы производителей: [{brand, pct, since}] — % от себестоимости проданного по бренду, с даты."""
        path = self.retro_path()
        if not path.exists():
            return []
        out = []
        for r in _read_csv(path)[1]:
            pct = num(r.get(RETRO_COL))
            if r.get("бренд") and pct is not None:
                out.append({"brand": r["бренд"], "pct": pct, "since": r.get("действует_с", "")})
        return sorted(out, key=lambda r: (brand_key(r["brand"]), r["since"]))

    def retro_at(self, rows: list[dict] | None = None, day: str | None = None) -> dict[str, float]:
        """{бренд (brand_key): %}, действующие на дату (по умолчанию — сегодня)."""
        month = (day or date.today().isoformat())[:7]
        out = {}
        for r in rows if rows is not None else self.retro_rows():
            if not r["since"] or r["since"][:7] <= month:
                out[brand_key(r["brand"])] = r["pct"]
        return {k: v for k, v in out.items() if v}

    def set_retro(self, brand: str, pct: float | None, effective_from: str | None = None) -> list[dict]:
        """Задать % ретро-бонуса бренда (с даты или действующий). pct=None — убрать строку этой даты."""
        brand = re.sub(r"\s+", " ", str(brand or "")).strip()
        if not brand:
            raise NomenclatureError("Не указан бренд")
        if pct is not None and not 0 <= pct <= 100:
            raise NomenclatureError("Ретро-бонус: процент от 0 до 100")
        since = norm_date(effective_from) if effective_from else ""
        rows = self.retro_rows()
        own = [r for r in rows if brand_key(r["brand"]) == brand_key(brand)]
        if not effective_from and own:          # без даты — правим действующую строку
            current = [r for r in own if not r["since"] or r["since"][:7] <= date.today().isoformat()[:7]]
            since = (current or own)[-1]["since"]
        rows = [r for r in rows if not (brand_key(r["brand"]) == brand_key(brand) and r["since"] == since)]
        if pct is not None:
            rows.append({"brand": brand, "pct": pct, "since": since})
        rows.sort(key=lambda r: (brand_key(r["brand"]), r["since"]))
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
        writer.writerow(["бренд", RETRO_COL, "действует_с"])
        writer.writerows([r["brand"], fmt(r["pct"]), r["since"]] for r in rows)
        path = self.retro_path()
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(buf.getvalue(), encoding="utf-8-sig", newline="")
        try:
            os.replace(tmp, path)
        except PermissionError as exc:
            tmp.unlink(missing_ok=True)
            raise NomenclatureError(f"{RETRO_FILE} открыт в другой программе (например, Excel) — закройте его и повторите") from exc
        return [r for r in rows if brand_key(r["brand"]) == brand_key(brand)]

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

    def fill_missing_cost(self, value: float = 1000) -> int:
        """Задаёт себестоимость только артикулам, у которых она не указана."""
        if not (0 <= value <= 1_000_000):
            raise NomenclatureError("Себестоимость: число от 0 до 1 000 000")
        missing = [item["article"] for item in self.items()["items"] if not item["has_cost"]]
        done = 0
        for article in missing:
            # Повторно проверяем текущую цену перед записью: параллельное ручное
            # изменение не будет перезаписано значением по умолчанию.
            _, rows = self.read_cost_rows()
            own = [r for r in rows if art_key(r.get("артикул")) == art_key(article)]
            current = self.current_price(own)
            if current and num(current.get(COST_COL)) is not None:
                continue
            self.update_price(article, cost=value)
            done += 1
        return done

    def set_prep_all(self, value: float = 85) -> int:
        """Задаёт подготовку всем артикулам справочника без ограничения размера пачки."""
        if not (0 <= value <= 100_000):
            raise NomenclatureError("Подготовка: число от 0 до 100 000")
        articles = [item["article"] for item in self.items()["items"] if item["article"]]
        for article in articles:
            self.update_price(article, prep=value)
        return len(articles)

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

    def import_table(self, table: list[list]) -> dict:
        """Единый справочник из таблицы (первая строка — заголовки): себестоимость и подготовка по артикулу —
        одна на все кабинеты и юрлица. Цена и подготовка из файла заменяют действующие; пустая ячейка
        подготовки оставляет прежнюю; бренд, категория и название дописываются только в пустые ячейки.
        Кратность не трогаем: в cost.csv это число единиц, на которое умножается себестоимость, а не штук в упаковке."""
        if not table:
            raise NomenclatureError("Файл пустой")
        cols = {}
        for i, h in enumerate(table[0]):
            text = re.sub(r"\s+", " ", str(h or "")).strip().lower()
            for field, words in IMPORT_COLUMNS:
                if field not in cols and any(text == w or text.startswith(w) for w in words):
                    cols[field] = i
                    break
        if "article" not in cols or "cost" not in cols:
            raise NomenclatureError("Нужны колонки «Артикул» и «Себестоимость» в первой строке")
        cell = lambda r, f: r[cols[f]] if f in cols and cols[f] < len(r) else None  # noqa: E731
        header, rows = self.read_cost_rows()
        by_key = defaultdict(list)
        for r in rows:
            by_key[art_key(r.get("артикул"))].append(r)
        added, updated, same, skipped, changes, seen = 0, 0, 0, [], [], set()
        for raw in table[1:]:
            article = str(cell(raw, "article") or "").strip()
            key = art_key(article)
            if not key or key in seen:
                continue
            seen.add(key)
            cost, prep = num(cell(raw, "cost")), num(cell(raw, "prep"))
            if cost is None or not 0 <= cost <= 1_000_000 or (prep is not None and not 0 <= prep <= 100_000):
                skipped.append(article)
                continue
            own = by_key.get(key) or []
            target = self.current_price(own) or (own[-1] if own else None)
            if target is None:
                target = {"артикул": article, "кратность": "1", "действует_с": "", PREP_COL: fmt(prep or 0)}
                rows.append(target)
                by_key[key] = own = [target]
                added += 1
                before = None
            else:
                before = (num(target.get(COST_COL)), num(target.get(PREP_COL)))
            target[COST_COL] = fmt(cost)
            if prep is not None:
                target[PREP_COL] = fmt(prep)
            for field, col in (("brand", "бренд"), ("category", "категория"), ("name", "название (справочно)")):
                value = re.sub(r"\s+", " ", str(cell(raw, field) or "")).strip()[:120]
                if value:
                    for r in own:
                        if not r.get(col):
                            r[col] = value
            if before is not None:
                after = (num(target.get(COST_COL)), num(target.get(PREP_COL)))
                if before != after:
                    updated += 1
                    changes.append({"article": article, "cost": [before[0], after[0]], "prep": [before[1], after[1]]})
                else:
                    same += 1
        if seen:
            self.write_cost_rows(header, rows)
            self._drop_missing(*seen)
        return {"added": added, "updated": updated, "unchanged": same, "skipped": skipped, "changes": changes,
                "total": len(by_key)}

    def import_file(self, filename: str, content: bytes) -> dict:
        """Excel (.xlsx) или CSV с себестоимостью → import_table."""
        if (filename or "").lower().endswith((".xlsx", ".xlsm")):
            from openpyxl import load_workbook
            try:
                ws = load_workbook(io.BytesIO(content), read_only=True, data_only=True).worksheets[0]
            except Exception as exc:
                raise NomenclatureError(f"Не удалось прочитать Excel: {exc}") from exc
            table = [list(r) for r in ws.iter_rows(values_only=True)]
        else:
            text = content.decode("utf-8-sig", errors="replace")
            delimiter = ";" if text[:4096].count(";") >= text[:4096].count(",") else ","
            table = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        return self.import_table([r for r in table if any(c not in (None, "") for c in r)])

    def _drop_missing(self, *keys: str) -> None:
        """Артикулы с ценой больше не «без себестоимости»: убираем их из cost_missing.csv."""
        keys = set(keys)
        path = Path(self.project_dir()) / "cost_missing.csv"
        if not path.exists():
            return
        with open(path, encoding="utf-8-sig", newline="") as fh:
            lines = list(csv.reader(fh, delimiter=";"))
        if not lines:
            return
        kept = [lines[0]] + [r for r in lines[1:] if r and art_key(r[0]) not in keys]
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
            data = json.loads(self.cards_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"cards": [], "updated_at": None, "errors": []}
        labels = self._merge_labels()
        counts = defaultdict(int)
        for c in data.get("cards", []):
            if c.get("cabinet_id") and c.get("imt_id"):
                counts[(c["cabinet_id"], c["imt_id"])] += 1
        for c in data.get("cards", []):
            c["merge_label"] = labels.get(str(c.get("cabinet_id")), {}).get(str(c.get("nm")), "")
            c["pet_kind"] = pet_kind(c.get("title"), c.get("subject"))
            size = counts.get((c.get("cabinet_id"), c.get("imt_id")))
            c["merge_size"] = size
            c["is_single"] = size == 1 if size else None
        return data

    def _merge_labels(self):
        try:
            data = json.loads(self.cards_path.with_name("wb_merge_labels.json").read_text(encoding="utf-8"))
            if not isinstance(data, dict) or any(not isinstance(v, dict) for v in data.values()):
                raise ValueError("invalid labels")
            return data
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise NomenclatureError("Не удалось прочитать сохранённые номера склеек") from exc

    def _update_merge_labels(self, cabinet, nm_ids, label=""):
        """Caller holds the shared move lock; replace atomically for all workers."""
        data = self._merge_labels()
        values = data.setdefault(str(cabinet), {})
        for nm in nm_ids:
            if label:
                values[str(nm)] = label
            else:
                values.pop(str(nm), None)
        path = self.cards_path.with_name("wb_merge_labels.json")
        tmp = path.with_suffix(".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            raise NomenclatureError("Результат WB получен, но номера склеек не сохранены; обновите карточки") from exc

    def _card_accounts(self, extra_tokens=None):
        accounts, seen = [], set()
        for c in self.cabinets():
            if getattr(c, "marketplace", "wildberries") == "wildberries" and c.token and c.token not in seen:
                accounts.append((str(c.id), c.name, c.token))
                seen.add(c.token)
        for name, token in extra_tokens or []:
            if token and token not in seen:
                accounts.append(("opiu:" + name, name, token))
                seen.add(token)
        return accounts

    @staticmethod
    def _fetch_cards(client, cabinet_id, name, token):
        cards, cursor = [], {"limit": 100}
        for _ in range(500):
            r = client.post(CARDS_URL, headers={"Authorization": token},
                            json={"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}})
            if r.status_code in (401, 403):
                raise NomenclatureError("Токен кабинета не даёт доступа к категории «Контент»")
            if r.status_code == 429:
                raise NomenclatureError("WB ограничил частоту запросов. Повторите позднее")
            r.raise_for_status()
            res = r.json()
            if res.get("error"):
                raise NomenclatureError("WB не смог загрузить карточки кабинета")
            page = res.get("cards") or []
            for card in page:
                photo = ((card.get("photos") or [{}])[0] or {})
                cards.append({"nm": card.get("nmID"), "imt_id": card.get("imtID"),
                              "subject_id": card.get("subjectID"), "vendor": card.get("vendorCode") or "",
                              "title": card.get("title") or "", "brand": card.get("brand") or "",
                              "subject": card.get("subjectName") or "", "cabinet_id": cabinet_id, "cabinet": name,
                              "photo": photo.get("c246x328") or photo.get("tm") or photo.get("big") or "",
                              "sizes": [s.get("techSize") for s in card.get("sizes") or [] if s.get("techSize")]})
            nxt = res.get("cursor") or {}
            if len(page) < 100:
                return cards
            if not nxt.get("updatedAt") or not nxt.get("nmID"):
                raise NomenclatureError("WB вернул неполный список карточек; повторите обновление")
            cursor = {"limit": 100, "updatedAt": nxt["updatedAt"], "nmID": nxt["nmID"]}
            time.sleep(0.7)
        raise NomenclatureError("Слишком большой список карточек; загрузка не завершена")

    def _save_cards(self, data):
        self.cards_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cards_path.with_suffix(f".{secrets.token_hex(8)}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.cards_path)

    def refresh_cards(self, extra_tokens: list[tuple[str, str]] | None = None) -> dict:
        with self._cards_lock:
            return self._refresh_cards(extra_tokens)

    def _refresh_cards(self, extra_tokens=None) -> dict:
        """Скачать карточки товаров WB (Контент API) по всем кабинетам с ключом."""
        tokens = self._card_accounts(extra_tokens)
        if not tokens:
            raise NomenclatureError("Нет кабинетов Wildberries с API-токеном")
        cards, errors = [], []
        with httpx.Client(timeout=30) as client:
            for cabinet_id, name, token in tokens:
                try:
                    cards.extend(self._fetch_cards(client, cabinet_id, name, token))
                except NomenclatureError as exc:
                    errors.append(f"{name}: {exc}")
                except (httpx.HTTPError, ValueError) as exc:
                    errors.append(f"{name}: {type(exc).__name__}")
        if not cards and errors:
            raise NomenclatureError("Карточки не загружены — " + "; ".join(errors))
        data = {"cards": cards, "updated_at": time.strftime("%Y-%m-%d %H:%M"), "errors": errors}
        self._save_cards(data)
        return {"cards": len(cards), "errors": errors, "updated_at": data["updated_at"]}

    def export_merge_excel(self) -> bytes:
        """One row per WB card, including cards with the same vendor code."""
        from openpyxl import Workbook
        from openpyxl.cell import WriteOnlyCell

        wb = Workbook(write_only=True)
        sheet = wb.create_sheet("Склейки WB")
        sheet.append(MERGE_COLUMNS)
        for c in self.cards().get("cards", []):
            values = [c.get("merge_label", ""), c.get("nm"), c.get("vendor"), c.get("title"), c.get("brand"),
                      c.get("subject"), c.get("subject_id"), c.get("cabinet"), c.get("cabinet_id"), c.get("imt_id"),
                      c.get("pet_kind")]
            cells = []
            for index, value in enumerate(values):
                cell = WriteOnlyCell(sheet, value=value)
                if isinstance(value, str):
                    cell.data_type = "s"  # WB titles/vendor codes must never become Excel formulas.
                if index in (1, 6, 9):
                    cell.number_format = "0"
                cells.append(cell)
            sheet.append(cells)
        out = io.BytesIO()
        wb.save(out)
        return out.getvalue()

    @staticmethod
    def _merge_index(cards):
        index = defaultdict(list)
        for card in cards:
            index[card.get("nm")].append(card)
        return index

    @staticmethod
    def _merge_group(number, nm_ids, cached):
        group = {"number": number, "nm_ids": nm_ids, "valid": False, "error": ""}
        try:
            if not 2 <= len(nm_ids) <= 30:
                raise NomenclatureError("В группе должно быть от 2 до 30 карточек")
            if any(type(n) is not int or n <= 0 for n in nm_ids) or len(set(nm_ids)) != len(nm_ids):
                raise NomenclatureError("nmID должны быть уникальными положительными целыми числами")
            index = cached if isinstance(cached, dict) else Nomenclature._merge_index(cached)
            if any(len(index[n]) != 1 for n in nm_ids):
                raise NomenclatureError("nmID отсутствует или неоднозначен в актуальном кэше; обновите карточки WB")
            chosen = [index[n][0] for n in nm_ids]
            first = chosen[0]
            cabinet = first.get("cabinet_id")
            if not cabinet or any(c.get("cabinet_id") != cabinet for c in chosen):
                raise NomenclatureError("Все карточки группы должны принадлежать одному кабинету")
            subjects = {c.get("subject_id") for c in chosen}
            if len(subjects) != 1 or type(first.get("subject_id")) is not int or first["subject_id"] <= 0:
                raise NomenclatureError("Склеивать можно только карточки одного предмета (subjectID)")
            if type(first.get("imt_id")) is not int or first["imt_id"] <= 0:
                raise NomenclatureError("Первая карточка должна иметь существующий imtID; обновите карточки WB")
            extras = [c for values in index.values() for c in values
                      if c.get("cabinet_id") == cabinet and c.get("imt_id") == first["imt_id"]
                      and c.get("nm") not in nm_ids]
            join_needed = any(c.get("imt_id") != first["imt_id"] for c in chosen)
            group.update(cabinet_id=cabinet, cabinet=first.get("cabinet", ""), subject_id=first["subject_id"],
                         subject=first.get("subject", ""), target_nm=nm_ids[0], target_imt=first["imt_id"],
                         cards=chosen, desired_members=nm_ids, exits=[c["nm"] for c in extras], exit_cards=extras,
                         join_needed=join_needed, no_api=not extras and not join_needed, kind="group", valid=True)
        except NomenclatureError as exc:
            group["error"] = str(exc)
        return group

    @staticmethod
    def _single_group(nm, cached):
        group = {"number": "", "nm_ids": [nm], "desired_members": [nm], "kind": "single",
                 "valid": False, "error": ""}
        cards = cached.get(nm, [])
        if len(cards) != 1:
            group["error"] = "nmID отсутствует или неоднозначен; обновите карточки WB"
            return group
        card = cards[0]
        cabinet, imt, subject = (card.get(k) for k in ("cabinet_id", "imt_id", "subject_id"))
        if not cabinet or type(imt) is not int or imt <= 0 or type(subject) is not int or subject <= 0:
            group["error"] = "Для отделения карточки не хватает данных; обновите карточки WB"
            return group
        peers = [c for values in cached.values() for c in values
                 if c.get("cabinet_id") == cabinet and c.get("imt_id") == imt]
        exits = [nm] if len(peers) > 1 else []
        group.update(cabinet_id=cabinet, cabinet=card.get("cabinet", ""), subject_id=subject,
                     subject=card.get("subject", ""), target_nm=nm, target_imt=imt, cards=[card],
                     exits=exits, exit_cards=[card] if exits else [], join_needed=False,
                     no_api=not exits, valid=True)
        return group

    @staticmethod
    def _merge_baseline(group, index):
        """Actual WB state, separate from the desired Excel membership and UI cards.

        Store primitive identities once per card. In particular, the six expected
        exits in an 11-of-17 plan are part of the original 17, not desired members.
        Include peers for explicit singles as well, so composition changes are caught.
        """
        cabinet, target = group["cabinet_id"], group["target_imt"]
        desired = set(group["nm_ids"])
        cards = [c for values in index.values() for c in values if c.get("nm") in desired or
                 (c.get("cabinet_id") == cabinet and c.get("imt_id") == target)]
        return {"cabinet_id": cabinet, "subject_id": group["subject_id"],
                "target_nm": group["target_nm"], "target_imt": target,
                "desired_members": list(group["nm_ids"]),
                "actual_members": sorted([c.get("nm"), c.get("cabinet_id"), c.get("imt_id"), c.get("subject_id")]
                                         for c in cards)}

    @staticmethod
    def _merge_excel_header(value):
        text = unicodedata.normalize("NFKC", str(value or "")).casefold()
        key = re.sub(r"[\s_()./–—-]+", "", text).lstrip("\ufeff")
        if key == "номерсклейки":
            return "label"
        if key in {"nmid", "nmidwb", "wbnmid", "nmidwildberries", "wildberriesnmid",
                   "артикулwb", "артикулвб", "артикулwildberries", "нмид",
                   "nmidартикулwb", "артикулwbnmid", "артикулwildberriesnmid"}:
            return "nm"
        return None

    @staticmethod
    def _merge_excel_value(value, field):
        """The same strict nmID interpretation for duplicates and actual import."""
        if value is None or isinstance(value, str) and not value.strip():
            return None
        if field == "label":
            return str(value).strip()
        if type(value) in (int, float) and value > 0:
            try:
                if value == int(value):
                    return int(value)
            except (ValueError, OverflowError):
                pass
        if isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
            nm = int(value.strip())
            if nm > 0:
                return nm
        # Invalid cells must not compare equal to blanks or valid numeric cells.
        return ("invalid", type(value).__name__, str(value))

    @classmethod
    def _merge_excel_table(cls, wb):
        """Find one unambiguous data table, independently of the active worksheet."""
        from openpyxl.utils import get_column_letter

        tables, scanned = [], 0
        for sheet in wb.worksheets:
            # Excel/LibreOffice can retain stale dimensions after moving the table.
            sheet.reset_dimensions()
            columns, data, header_line = None, [], None
            for line, row in enumerate(sheet.iter_rows(values_only=True), 1):
                scanned += 1
                if scanned > 200000:
                    raise NomenclatureError("В Excel слишком много строк; оставьте один лист с таблицей до 100000 строк")
                if columns is None:
                    fields = [cls._merge_excel_header(v) for v in row]
                    if "label" not in fields or "nm" not in fields:
                        continue
                    columns = {field: [i for i, name in enumerate(fields) if name == field]
                               for field in ("label", "nm")}
                    header_line = line
                    continue
                if line - header_line > 100000:
                    raise NomenclatureError(f"Лист «{sheet.title}»: больше 100000 строк данных")
                data.append((line, {field: [row[i] if i < len(row) else None for i in indices]
                                   for field, indices in columns.items()}))
            if columns is None or not any(cls._merge_excel_value(v, field) is not None
                                          for _, values in data for field in columns for v in values[field]):
                continue  # Empty sheets and empty service templates do not select a table.
            primary = {}
            for field, indices in columns.items():
                vectors = [[cls._merge_excel_value(values[field][j], field) for _, values in data]
                           for j in range(len(indices))]
                nonempty = [j for j, vector in enumerate(vectors) if any(v is not None for v in vector)]
                chosen = nonempty[0] if nonempty else 0
                for duplicate in nonempty[1:]:
                    if vectors[duplicate] != vectors[chosen]:
                        bad = next(i for i, (a, b) in enumerate(zip(vectors[chosen], vectors[duplicate])) if a != b)
                        first_col, second_col = (get_column_letter(indices[j] + 1) for j in (chosen, duplicate))
                        name = "Номер склейки" if field == "label" else "nmID"
                        raise NomenclatureError(f"Лист «{sheet.title}», строка {data[bad][0]}: конфликт дублирующих колонок «{name}» {first_col} и {second_col}; оставьте одну колонку")
                primary[field] = chosen
            rows = [(line, values["label"][primary["label"]], values["nm"][primary["nm"]])
                    for line, values in data]
            tables.append((sheet.title, rows))
        if not tables:
            raise NomenclatureError("Не найдена заполненная таблица с колонками «Номер склейки» и nmID; проверьте заголовки на листах Excel")
        first = [(cls._merge_excel_value(label, "label"), cls._merge_excel_value(nm, "nm"))
                 for _, label, nm in tables[0][1] if label is not None or nm is not None]
        for title, rows in tables[1:]:
            other = [(cls._merge_excel_value(label, "label"), cls._merge_excel_value(nm, "nm"))
                     for _, label, nm in rows if label is not None or nm is not None]
            if other != first:
                raise NomenclatureError(f"Найдено несколько разных таблиц на листах «{tables[0][0]}» и «{title}»; оставьте один лист для импорта")
        return tables[0][1]

    def preview_merge_excel(self, filename, content) -> dict:
        """Read only group labels and nmIDs. All identity comes from the current cache."""
        from openpyxl import load_workbook

        if Path(filename or "").suffix.lower() != ".xlsx":
            raise NomenclatureError("Загрузите файл .xlsx")
        if len(content) > 20 * 1024 * 1024:
            raise NomenclatureError("Файл больше 20 МБ")
        try:
            with ZipFile(io.BytesIO(content)) as archive:
                if sum(e.file_size for e in archive.infolist()) > 100 * 1024 * 1024:
                    raise NomenclatureError("Слишком большой распакованный Excel")
            wb = load_workbook(io.BytesIO(content), read_only=True, data_only=False, keep_links=False)
            try:
                assignments, blank_ids, all_ids = {}, [], []
                for line, label, raw in self._merge_excel_table(wb):
                    label = "" if label is None else str(label).strip()
                    if label and (len(label) > 256 or label.startswith("=")):
                        raise NomenclatureError(f"Строка {line}: номер склейки должен быть значением, не формулой (до 256 символов)")
                    nm = self._merge_excel_value(raw, "nm")
                    if type(nm) is not int or nm <= 0:
                        nm = None
                    if nm is not None:
                        all_ids.append(nm)
                    if label:
                        assignments.setdefault(label, []).append(nm)
                    elif nm is not None:
                        blank_ids.append(nm)
            finally:
                wb.close()
        except NomenclatureError:
            raise
        except Exception as exc:
            raise NomenclatureError("Не удалось прочитать .xlsx; проверьте файл") from exc
        with self._cards_lock:
            cached = self._merge_index(self.cards().get("cards", []))
            labels = self._merge_labels()
            counts = defaultdict(int)
            for nm in all_ids:
                counts[nm] += 1
            groups = [self._merge_group(label, ids, cached) for label, ids in assignments.items()]
            for nm in dict.fromkeys(blank_ids):
                if any(labels.get(str(c.get("cabinet_id")), {}).get(str(nm)) for c in cached.get(nm, [])):
                    groups.append(self._single_group(nm, cached))
            if not groups:
                raise NomenclatureError("Заполните «Номер склейки» у 2–30 карточек или очистите ранее сохранённый номер")
            for group in groups:
                if any(counts[n] > 1 for n in group["nm_ids"] if n is not None):
                    group.update(valid=False, error="nmID повторяется в файле; карточка может участвовать только в одной группе")
                # A target detached by another group has a new, as yet unknown imtID.
                # Do not submit a join with the old target: it would undo exact membership.
                if group.get("kind") == "group" and any(
                        other is not group and other.get("valid") and group.get("target_nm") in other.get("exits", [])
                        for other in groups):
                    group.update(valid=False, error="Цель этой группы должна выйти из другой склейки; сначала отделите её, обновите WB и загрузите Excel заново")
                if group["valid"]:
                    group["baseline"] = self._merge_baseline(group, cached)
            preview_id = secrets.token_urlsafe(24)
            # Workers share the same state directory. Only an opaque id reaches the client.
            directory = self.cards_path.parent / "wb_merge_previews"
            try:
                directory.mkdir(parents=True, exist_ok=True)
                now = time.time()
                for old in directory.glob("*.json"):
                    try:
                        if now - old.stat().st_mtime > 1800:
                            old.unlink(missing_ok=True)
                    except OSError:
                        pass
                path = directory / (preview_id + ".json")
                with path.open("x", encoding="utf-8") as handle:
                    json.dump({"expires_at": now + 1800, "groups": groups}, handle, ensure_ascii=False)
            except OSError as exc:
                raise NomenclatureError("Не удалось сохранить предварительный просмотр; повторите загрузку") from exc
        return {"preview_id": preview_id, "groups": groups, "valid_count": sum(g["valid"] for g in groups),
                "invalid_count": sum(not g["valid"] for g in groups)}

    def _move_ledger(self):
        path = self.cards_path.with_name("wb_merge_requests.json")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("invalid ledger")
            return data
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise NomenclatureError("Не удалось прочитать журнал склеек; отправка остановлена") from exc

    def _acquire_move_file_lock(self):
        """One writer across Uvicorn workers; OS releases the lock on process exit."""
        handle = None
        try:
            self.cards_path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.cards_path.with_name("wb_merge.lock").open("a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError as exc:
            if handle:
                handle.close()
            raise NomenclatureError("Операция склейки уже выполняется или журнал недоступен; повторите позднее") from exc

    def _save_move_ledger(self, data):
        path = self.cards_path.with_name("wb_merge_requests.json")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            raise NomenclatureError("Не удалось записать журнал склеек; проверьте состояние WB перед повтором") from exc

    def _check_unknown_moves(self, cabinet_id, nm_ids):
        for request in self._move_ledger().values():
            if request.get("cabinet_id") == cabinet_id and request.get("status") == "unknown" and set(nm_ids) & set(request.get("nm_ids", [])):
                raise MergeUnknownError("Этот запрос уже отправлен WB; статус неизвестен. Проверьте карточки в WB перед новой операцией")

    def _post_move(self, client, cabinet_id, token, nm_ids, target_imt, source_imt=None):
        ledger = self._move_ledger()
        operation = "detach" if target_imt is None else "join"
        signature = hashlib.sha256(json.dumps([operation, cabinet_id, target_imt, source_imt, sorted(nm_ids)]).encode()).hexdigest()
        self._check_unknown_moves(cabinet_id, nm_ids)
        if signature in ledger:
            raise NomenclatureError("Этот запрос уже отправлен WB. Обновите карточки перед новой операцией")
        ledger[signature] = {"cabinet_id": cabinet_id, "nm_ids": nm_ids, "target_imt": target_imt,
                             "source_imt": source_imt, "operation": operation, "status": "unknown", "at": time.time()}
        self._save_move_ledger(ledger)  # Durable intent must precede the external side effect.
        # Content API: 100/minute, minimum interval 600 ms; never retry a move automatically.
        time.sleep(0.7)
        try:
            payload = {"nmIDs": nm_ids} if target_imt is None else {"targetIMT": target_imt, "nmIDs": nm_ids[1:]}
            response = client.post(MOVE_CARDS_URL, headers={"Authorization": token}, json=payload)
        except httpx.HTTPError as exc:
            raise MergeUnknownError("WB не ответил вовремя или соединение прервано. Статус операции неизвестен; проверьте карточки перед повтором") from exc
        rejected = response.status_code >= 400
        if not rejected:
            try:
                body = response.json()
                if not isinstance(body, dict) or "error" not in body:
                    raise ValueError("no acceptance flag")
                rejected = bool(body["error"])
            except ValueError as exc:
                raise MergeUnknownError("WB вернул некорректный ответ. Статус операции неизвестен; проверьте карточки") from exc
        if rejected:
            ledger.pop(signature)
            self._save_move_ledger(ledger)
            if response.status_code in (401, 403):
                raise NomenclatureError("Токен кабинета не разрешает изменение карточек (Контент API)")
            if response.status_code == 429:
                raise NomenclatureError("WB ограничил частоту запросов. Повторите позднее")
            raise NomenclatureError(f"WB отклонил склейку (HTTP {response.status_code}); проверьте ограничения карточек")
        ledger[signature]["status"] = "success"
        try:
            self._save_move_ledger(ledger)
        except NomenclatureError as exc:
            # Durable intent remains unknown; do not proceed to a dependent join.
            raise MergeUnknownError("WB принял запрос, но запись результата в журнал не удалась; дальнейшие операции остановлены, проверьте карточки") from exc
        return "WB принял запрос; обновление imtID может занять время"

    def merge_batch(self, preview_id, extra_tokens=None, audit=None) -> dict:
        if not isinstance(preview_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32}", preview_id):
            raise NomenclatureError("Загрузите Excel и проверьте предварительный просмотр")
        if not self._move_lock.acquire(blocking=False):
            raise NomenclatureError("Операция склейки уже выполняется; дождитесь результата")
        file_lock = None
        try:
            file_lock = self._acquire_move_file_lock()
            with self._cards_lock:
                path = self.cards_path.parent / "wb_merge_previews" / (preview_id + ".json")
                try:
                    preview = json.loads(path.read_text(encoding="utf-8"))
                    path.unlink()  # Claim once while the shared OS lock is held.
                except (OSError, ValueError):
                    preview = None
                if not preview or time.time() >= preview["expires_at"]:
                    raise NomenclatureError("Предварительный просмотр устарел или уже использован; загрузите Excel заново")
                groups = preview["groups"]
                cached = self._merge_index(self.cards().get("cards", []))
                accounts = {a[0]: a for a in self._card_accounts(extra_tokens)}
                fresh, indexes, failures, results, detached, touched = {}, {}, {}, [], {}, defaultdict(set)

                def emit(event, cabinet, details, required=False):
                    if not audit:
                        return True
                    try:
                        audit(event, cabinet, json.dumps(details, ensure_ascii=False))
                        return True
                    except Exception as exc:
                        if required:
                            raise NomenclatureError("Не удалось записать аудит; операция не отправлена WB") from exc
                        return False

                with httpx.Client(timeout=30) as client:
                    # One complete preflight snapshot per involved cabinet, not one per group.
                    for cabinet in dict.fromkeys(g.get("cabinet_id") for g in groups if g["valid"]):
                        try:
                            if cabinet not in accounts:
                                raise NomenclatureError("Кабинет не найден или не имеет токена Контент API")
                            time.sleep(0.7)
                            fresh[cabinet] = self._fetch_cards(client, *accounts[cabinet])
                            indexes[cabinet] = self._merge_index(fresh[cabinet])
                        except (NomenclatureError, httpx.HTTPError, ValueError) as exc:
                            failures[cabinet] = str(exc) if isinstance(exc, NomenclatureError) else "Не удалось загрузить свежие карточки WB"
                    for group in groups:
                        result = {k: group.get(k) for k in ("number", "kind", "cabinet_id", "nm_ids", "target_imt", "exits", "no_api")}
                        result.update(status="error", operations=[])
                        try:
                            if not group["valid"]:
                                raise NomenclatureError(group["error"])
                            cabinet = group["cabinet_id"]
                            if cabinet in failures:
                                raise NomenclatureError(failures[cabinet])
                            if not group.get("baseline"):
                                raise NomenclatureError("Формат предварительного просмотра устарел; загрузите Excel заново")
                            for index in (cached, indexes[cabinet]):
                                state = (self._single_group(group["nm_ids"][0], index) if group.get("kind") == "single"
                                         else self._merge_group(group["number"], group["nm_ids"], index))
                                if not state["valid"]:
                                    raise NomenclatureError(state["error"])
                                if self._merge_baseline(state, index) != group["baseline"]:
                                    raise NomenclatureError("Кабинет, предмет, imtID или состав склейки изменились; загрузите Excel заново")
                            self._check_unknown_moves(cabinet, group["nm_ids"] + group["exits"])
                            emit("wb_cards_merge_submitted", cabinet, result, required=True)
                            for nm in group["exits"]:
                                if (cabinet, nm) in detached:
                                    result["operations"].append(dict(detached[(cabinet, nm)], reused=True))
                                    continue
                                operation = {"operation": "detach", "nm_ids": [nm], "status": "error"}
                                result["operations"].append(operation)
                                emit("wb_cards_detach_submitted", cabinet, operation, required=True)
                                source_imt = indexes[cabinet][nm][0]["imt_id"]
                                touched[cabinet].add(nm)
                                try:
                                    operation["warning"] = self._post_move(client, cabinet, accounts[cabinet][2], [nm], None, source_imt)
                                    operation["status"] = "success"
                                    detached[(cabinet, nm)] = dict(operation)
                                    self._update_merge_labels(cabinet, [nm])
                                except MergeUnknownError as exc:
                                    operation.update(status="unknown", error=str(exc))
                                    raise
                                except NomenclatureError as exc:
                                    operation["error"] = str(exc)
                                    raise
                                finally:
                                    if not emit("wb_cards_detach_" + operation["status"], cabinet, operation):
                                        result["warning"] = "Результат отделения не записан в аудит; проверьте журнал"
                            if group.get("join_needed"):
                                operation = {"operation": "join", "nm_ids": group["nm_ids"],
                                             "target_imt": group["target_imt"], "status": "error"}
                                result["operations"].append(operation)
                                emit("wb_cards_join_submitted", cabinet, operation, required=True)
                                touched[cabinet].update(group["nm_ids"])
                                try:
                                    operation["warning"] = self._post_move(client, cabinet, accounts[cabinet][2], group["nm_ids"], group["target_imt"])
                                    operation["status"] = "success"
                                except MergeUnknownError as exc:
                                    operation.update(status="unknown", error=str(exc))
                                    raise
                                except NomenclatureError as exc:
                                    operation["error"] = str(exc)
                                    raise
                                finally:
                                    if not emit("wb_cards_join_" + operation["status"], cabinet, operation):
                                        result["warning"] = "Результат склейки не записан в аудит; проверьте журнал"
                            self._update_merge_labels(cabinet, group["nm_ids"], group["number"])
                            result["status"] = "success"
                            result.setdefault("warning", "API-действий не требуется; номер сохранён" if group["no_api"] else
                                              "WB принял операции; фактический состав проверяйте по обновлённым карточкам")
                        except MergeUnknownError as exc:
                            result.update(status="unknown", error=str(exc))
                        except NomenclatureError as exc:
                            result["error"] = str(exc)
                        if not emit("wb_cards_merge_" + result["status"], result.get("cabinet_id") or "", result):
                            result["warning"] = "Результат группы не записан в аудит; проверьте журнал и карточки WB"
                        results.append(result)
                    # An accepted request is not evidence that WB has applied it. Read again.
                    for cabinet in touched:
                        try:
                            time.sleep(0.7)
                            fresh[cabinet] = self._fetch_cards(client, *accounts[cabinet])
                        except (NomenclatureError, httpx.HTTPError, ValueError):
                            for card in fresh[cabinet]:
                                if card.get("nm") in touched[cabinet]:
                                    card["imt_id"] = None  # Do not publish the stale pre-move group as current.
                            for result in results:
                                if result.get("cabinet_id") == cabinet:
                                    result["warning"] = "Операции обработаны, но обновление карточек не удалось; фактический состав неизвестен, обновите WB позднее"
                if fresh:
                    data = self.cards()
                    data["cards"] = [c for c in data.get("cards", []) if c.get("cabinet_id") not in fresh]
                    data["cards"].extend(c for cabinet in fresh.values() for c in cabinet)
                    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M")
                    try:
                        self._save_cards(data)
                    except OSError:
                        for result in results:
                            result["warning"] = "Локальный кэш не записан; обновите список карточек WB"
                return {"groups": results, "summary": {"total": len(results), **{s: sum(r["status"] == s for r in results)
                                                                               for s in ("success", "error", "unknown")}}}
        finally:
            if file_lock:
                file_lock.close()
            self._move_lock.release()

    def merge_cards(self, cabinet_id, nm_ids, target_imt, extra_tokens=None) -> dict:
        """Fresh ownership/subject checks use only the selected cabinet's token."""
        if not isinstance(nm_ids, list) or not 2 <= len(nm_ids) <= 30:
            raise NomenclatureError("Выберите от 2 до 30 карточек")
        if any(type(n) is not int or n <= 0 for n in nm_ids) or len(set(nm_ids)) != len(nm_ids):
            raise NomenclatureError("nmID должны быть уникальными положительными целыми числами")
        if type(target_imt) is not int or target_imt <= 0:
            raise NomenclatureError("Целевая карточка должна иметь существующий imtID; обновите карточки")
        accounts = [a for a in self._card_accounts(extra_tokens) if a[0] == cabinet_id]
        if len(accounts) != 1:
            raise NomenclatureError("Кабинет не найден, неоднозначен или не имеет токена Контент API")
        if not self._move_lock.acquire(blocking=False):
            raise NomenclatureError("Операция склейки уже выполняется; дождитесь результата")
        file_lock = None
        try:
            file_lock = self._acquire_move_file_lock()
            with self._cards_lock:
                signature = (cabinet_id, target_imt, tuple(sorted(nm_ids)))
                now = time.monotonic()
                self._recent_moves = {k: v for k, v in self._recent_moves.items() if now - v < 300}
                if signature in self._recent_moves:
                    raise NomenclatureError("Этот запрос уже отправлен WB. Обновите карточки перед новой операцией")
                cached = self.cards().get("cards", [])
                for nm in nm_ids:
                    if not any(c.get("nm") == nm and c.get("cabinet_id") == cabinet_id for c in cached):
                        raise NomenclatureError("Все карточки должны принадлежать одному кабинету; обновите список")
                cabinet_id, name, token = accounts[0]
                with httpx.Client(timeout=30) as client:
                    try:
                        fresh = self._fetch_cards(client, cabinet_id, name, token)
                        by_nm = {c["nm"]: c for c in fresh}
                        if any(n not in by_nm for n in nm_ids):
                            raise NomenclatureError("Токен кабинета не соответствует выбранным карточкам либо карточки удалены")
                        chosen = [by_nm[n] for n in nm_ids]
                        if chosen[0].get("imt_id") != target_imt:
                            raise NomenclatureError("imtID первой карточки изменился; обновите список и подтвердите выбор заново")
                        subjects = {c.get("subject_id") for c in chosen}
                        if len(subjects) != 1 or not next(iter(subjects)):
                            raise NomenclatureError("Склеивать можно только карточки одного предмета")
                        if all(c.get("imt_id") == target_imt for c in chosen):
                            raise NomenclatureError("Выбранные карточки уже находятся в одной склейке")
                        self._post_move(client, cabinet_id, token, nm_ids, target_imt)
                        self._recent_moves[signature] = now
                    except httpx.TimeoutException as exc:
                        raise NomenclatureError("WB не ответил вовремя. Статус операции неизвестен; обновите карточки перед повтором") from exc
                    except (httpx.HTTPError, ValueError) as exc:
                        raise NomenclatureError("Не удалось получить корректный ответ WB; обновите карточки и проверьте результат") from exc
                    warning = None
                    try:
                        time.sleep(0.7)
                        fresh = self._fetch_cards(client, cabinet_id, name, token)
                    except (NomenclatureError, httpx.HTTPError, ValueError):
                        warning = "WB принял склейку, но обновление карточек не удалось. Обновите список позднее"
                data = self.cards()
                data["cards"] = [c for c in data.get("cards", []) if c.get("cabinet_id") != cabinet_id] + fresh
                data["updated_at"] = time.strftime("%Y-%m-%d %H:%M")
                try:
                    self._save_cards(data)
                except OSError:
                    warning = "WB принял склейку, но локальный кэш не записан. Обновите список позднее"
                return {"accepted": True, "cabinet_id": cabinet_id, "target_imt": target_imt,
                        "nm_ids": nm_ids, "warning": warning or "WB принял запрос; обновление imtID может занять время"}
        finally:
            if file_lock:
                file_lock.close()
            self._move_lock.release()

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
        models_by_article = {}
        models_by_nm = {}
        for nm, vendor in links.items():
            key = art_key(vendor)
            if key in rows:
                rows[key]["nm_ids"].add(int(nm))
        if self.models is not None:
            for model_key, model in self.models.all().items():
                key = art_key(model_key)
                models_by_article[key] = model_key
                models_by_nm.update({int(n): model_key for n in model.get("nm_ids", [])})
                if key in rows:
                    rows[key]["nm_ids"] |= {int(n) for n in model.get("nm_ids", [])}
        today = date.today().isoformat()
        out = []
        for key, item in rows.items():
            linked_models = {models_by_nm[nm] for nm in item["nm_ids"] if nm in models_by_nm}
            model_key = models_by_article.get(key) or (next(iter(linked_models)) if len(linked_models) == 1 else "")
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
                "model_key": model_key,
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
                "goods_at": goods["at"], "cost_file": str(self.cost_path()), "retro": self.retro_rows()}
