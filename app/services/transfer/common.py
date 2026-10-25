"""Общие функции переноса карточек между маркетплейсами (WB, Ozon, Яндекс Маркет).

Карточка источника приводится к нейтральному виду (dict):
  key            — базовый артикул (vendorCode / общий offer_id)
  source_mp      — wildberries | ozon | yandex
  title, description, brand
  category       — название предмета/типа/категории на площадке-источнике
  chars          — [{"name", "values": [str], "id"?}]   характеристики «как есть»
  photos, videos — ссылки
  dims           — {"length", "width", "height"} см, {"weight"} кг (брутто)
  variants       — [{"id", "size", "ru_size", "barcodes", "price", "old_price", "chars"}]
  native         — исходные данные площадки (для переноса WB→WB, Ozon→Ozon, ЯМ→ЯМ без потерь)
"""
from __future__ import annotations

import difflib
import re
import time

import httpx

# Синонимы: характеристика площадки-приёмника (нижний регистр) -> как может называться у источника
ALIASES = {
    "цвет товара": ["цвет", "основной цвет", "название цвета"],
    "название цвета": ["цвет", "цвет товара"],
    "цвет": ["цвет товара", "название цвета", "основной цвет"],
    "страна изготовитель": ["страна производства", "страна изготовления", "страна изготовитель"],
    "страна производства": ["страна изготовитель", "страна изготовления"],
    "материал": ["состав", "материал изделия", "материал верха", "состав ткани", "состав материала"],
    "состав": ["состав материала", "материал", "состав ткани"],
    "состав материала": ["состав"],
    "пол": ["пол", "целевая аудитория"],
    "сезон": ["сезон"],
    "комплектация": ["комплектация"],
    "гарантийный срок": ["гарантийный срок", "гарантия"],
    "тн вэд коды еаэс": ["тнвэд", "тн вэд", "код тн вэд"],
    "тнвэд": ["тн вэд коды еаэс", "тн вэд"],
    "количество в упаковке шт": ["количество предметов в упаковке", "количество в упаковке"],
    "размер производителя": ["размер", "размер на бирке"],
    "российский размер": ["рос размер", "размер российский", "российский размер"],
}


class TransferError(Exception):
    pass


def norm(s) -> str:
    s = str(s or "").lower().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9]+", " ", s).strip()


def stem(s) -> str:
    """Грубое приведение к основе: убираем типичные окончания."""
    words = []
    for w in norm(s).split():
        for end in ("ами", "ями", "ки", "ги", "хи", "ы", "и", "а", "я", "ов", "ев", "ей", "о", "е"):
            if len(w) > 4 and w.endswith(end):
                w = w[: -len(end)]
                break
        words.append(w)
    return " ".join(words)


def sim(a, b) -> float:
    return max(difflib.SequenceMatcher(None, norm(a), norm(b)).ratio(),
               difflib.SequenceMatcher(None, stem(a), stem(b)).ratio())


def to_number(v, integer=False):
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(v))
    if not m:
        return None
    n = float(m.group(0).replace(",", "."))
    return int(round(n)) if integer else (int(n) if n.is_integer() else round(n, 3))


def find_values(target_name: str, chars: list) -> list | None:
    """Значения характеристики источника для характеристики приёмника: точное имя, синонимы, похожее имя."""
    by_name = {norm(c["name"]): c["values"] for c in chars if c.get("values")}
    n = norm(target_name)
    for cand in [n] + [norm(x) for x in ALIASES.get(n, [])]:
        if cand in by_name:
            return by_name[cand]
    best, best_s = None, 0.0
    for k, v in by_name.items():
        s = sim(n, k)
        if s > best_s:
            best, best_s = v, s
    return best if best_s >= 0.82 else None


def rank(name: str, options: list, key: str, top=6) -> list:
    scored = sorted(((sim(name, o[key]), o) for o in options), key=lambda x: -x[0])[:top]
    return [dict(o, score=round(s, 2)) for s, o in scored]


class Http:
    """Запросы с повтором при 429/5xx. Паузы между запросами — по ключу (ограничения площадок)."""

    def __init__(self, headers: dict, pause: float = 0.0, log=None):
        self.headers = headers
        self.pause = pause
        self.last = {}
        self.log = log or (lambda *_: None)
        self.client = httpx.Client(timeout=90)

    def close(self):
        self.client.close()

    def request(self, method, url, *, key="default", pause=None, ok404=False, **kw):
        wait = self.pause if pause is None else pause
        for attempt in range(6):
            gap = wait - (time.monotonic() - self.last.get(key, 0))
            if gap > 0:
                time.sleep(gap)
            self.last[key] = time.monotonic()
            try:
                r = self.client.request(method, url, headers={**self.headers, **kw.pop("headers", {})}, **kw)
            except httpx.HTTPError as exc:
                if attempt < 5:
                    time.sleep(3)
                    continue
                raise TransferError(f"нет связи ({type(exc).__name__})")
            if r.status_code == 429 or r.status_code >= 500:
                if attempt < 5:
                    time.sleep(min(30, 3 * (attempt + 1)))
                    continue
            if r.status_code == 404 and ok404:
                return None
            if r.status_code in (401, 403):
                raise TransferError(f"ключ API не подходит или у него нет доступа ({r.status_code}): {r.text[:200]}")
            if r.status_code >= 400:
                raise TransferError(f"{r.status_code}: {r.text[:500]}")
            if not r.content.strip():
                return {}
            return r.json()
        raise TransferError("площадка ограничила частоту запросов — попробуйте позже")

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, body=None, **kw):
        return self.request("POST", url, json=body, **kw)


def clean_offer_id(s: str) -> str:
    return re.sub(r"\s+", "-", str(s or "").strip())
