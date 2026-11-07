from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Callable

import httpx

CONTENT = "https://content-api.wildberries.ru"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_nm_ids(value) -> list[int]:
    vals = value if isinstance(value, (list, tuple, set)) else re.split(r"[\s,;]+", str(value or ""))
    out = []
    for item in vals:
        text = str(item).strip()
        if not text:
            continue
        if not text.isdigit() or int(text) <= 0:
            raise ValueError(f"Некорректный WB ID: {text}")
        n = int(text)
        if n not in out:
            out.append(n)
    if not out:
        raise ValueError("Добавьте хотя бы один WB ID")
    if len(out) > 30:
        raise ValueError("В одном сравнении можно указать не больше 30 карточек")
    return out


def _value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(_value(x) for x in value if _value(x))
    if isinstance(value, dict):
        return "; ".join(f"{k}: {_value(v)}" for k, v in value.items() if _value(v))
    return str(value).strip()


def normalize_card(card: dict) -> dict:
    nm_id = int(card.get("nmID") or card.get("nmId") or 0)
    chars = {}
    for item in card.get("characteristics") or []:
        if not isinstance(item, dict):
            continue
        name = _value(item.get("name") or item.get("charcName") or item.get("characteristicName"))
        if not name:
            continue
        value = item.get("value")
        if value is None:
            value = item.get("values")
        text = _value(value)
        if name in chars and text and text not in chars[name].split("; "):
            chars[name] += "; " + text
        elif name not in chars:
            chars[name] = text
    dims = card.get("dimensions") or {}
    dimensions = {str(k): _value(v) for k, v in dims.items() if _value(v)} if isinstance(dims, dict) else {"value": _value(dims)}
    sizes = []
    for item in card.get("sizes") or []:
        if not isinstance(item, dict):
            continue
        sizes.append({"techSize": _value(item.get("techSize")), "wbSize": _value(item.get("wbSize")),
                      "skus": item.get("skus") or []})
    photos = []
    for item in card.get("photos") or []:
        if isinstance(item, str):
            photos.append({"tm": item, "c246x328": item, "big": item})
        elif isinstance(item, dict):
            photos.append({"tm": item.get("tm") or "", "c246x328": item.get("c246x328") or item.get("c516x688") or item.get("big") or "", "big": item.get("big") or item.get("c516x688") or item.get("c246x328") or item.get("tm") or ""})
    return {"nm_id": nm_id, "vendor_code": _value(card.get("vendorCode")), "title": _value(card.get("title")),
            "brand": _value(card.get("brand")), "subject": _value(card.get("subjectName")),
            "description": _value(card.get("description")), "dimensions": dimensions, "characteristics": chars,
            "sizes": sizes, "photos": photos}


class CardCompareError(Exception):
    pass


class CardCompare:
    def __init__(self, store, tokens: Callable[[], list[tuple[str, str]]]):
        self.store = store
        self.tokens = tokens
        with store._connect() as c:
            c.execute("CREATE TABLE IF NOT EXISTS card_comparisons (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, nm_ids TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")

    def _row(self, row) -> dict:
        return {"id": row[0], "title": row[1], "nm_ids": json.loads(row[2] or "[]"), "created_at": row[3], "updated_at": row[4]}

    def list(self) -> list[dict]:
        with self.store._connect() as c:
            rows = c.execute("SELECT id,title,nm_ids,created_at,updated_at FROM card_comparisons ORDER BY id DESC").fetchall()
        return [self._row(r) for r in rows]

    def create(self, title: str, nm_ids) -> dict:
        ids = parse_nm_ids(nm_ids)
        title = str(title or "Сравнение карточек").strip()[:160] or "Сравнение карточек"
        now = _now()
        with self.store._connect() as c:
            cur = c.execute("INSERT INTO card_comparisons(title,nm_ids,created_at,updated_at) VALUES(?,?,?,?)", (title, json.dumps(ids), now, now))
            cid = cur.lastrowid
        return self.detail(cid)

    def _get(self, comparison_id: int) -> dict:
        with self.store._connect() as c:
            row = c.execute("SELECT id,title,nm_ids,created_at,updated_at FROM card_comparisons WHERE id=?", (comparison_id,)).fetchone()
        if not row:
            raise CardCompareError("Сравнение не найдено")
        return self._row(row)

    def add(self, comparison_id: int, nm_ids) -> dict:
        current = self._get(comparison_id)
        ids = current["nm_ids"] + [n for n in parse_nm_ids(nm_ids) if n not in current["nm_ids"]]
        if len(ids) > 30:
            raise CardCompareError("В одном сравнении можно указать не больше 30 карточек")
        with self.store._connect() as c:
            c.execute("UPDATE card_comparisons SET nm_ids=?,updated_at=? WHERE id=?", (json.dumps(ids), _now(), comparison_id))
        return self.detail(comparison_id)

    def delete(self, comparison_id: int) -> None:
        self._get(comparison_id)
        with self.store._connect() as c:
            c.execute("DELETE FROM card_comparisons WHERE id=?", (comparison_id,))

    def _fetch(self, nm_ids: list[int]) -> list[dict]:
        available = [(name, token) for name, token in self.tokens() if token]
        if not available:
            raise CardCompareError("Нет кабинета Wildberries с API-токеном")
        result = []
        with httpx.Client(timeout=45) as client:
            for nm_id in nm_ids:
                found = None
                error = ""
                for _name, token in available:
                    try:
                        response = client.post(CONTENT + "/content/v2/get/cards/list?locale=ru", headers={"Authorization": token}, json={"settings": {"cursor": {"limit": 10}, "filter": {"textSearch": str(nm_id), "withPhoto": -1}}})
                        if response.status_code >= 400:
                            error = f"WB ответил {response.status_code}"
                            continue
                        data = response.json() or {}
                        found = next((card for card in data.get("cards") or [] if int(card.get("nmID") or 0) == nm_id), None)
                        if found:
                            break
                    except (httpx.HTTPError, ValueError) as exc:
                        error = type(exc).__name__
                if found:
                    result.append(normalize_card(found))
                else:
                    result.append({"nm_id": nm_id, "error": error or "Карточка не найдена", "characteristics": {}, "dimensions": {}, "sizes": [], "photos": []})
        return result

    def detail(self, comparison_id: int) -> dict:
        meta = self._get(comparison_id)
        cards = self._fetch(meta["nm_ids"])
        keys = set()
        for card in cards:
            keys.update(card.get("characteristics", {}).keys())
        rows = [{"name": "Бренд", "kind": "metadata", "values": {str(c["nm_id"]): c.get("brand", "") for c in cards}},
                {"name": "Артикул продавца", "kind": "metadata", "values": {str(c["nm_id"]): c.get("vendor_code", "") for c in cards}},
                {"name": "Предмет", "kind": "metadata", "values": {str(c["nm_id"]): c.get("subject", "") for c in cards}},
                {"name": "Описание", "kind": "metadata", "values": {str(c["nm_id"]): c.get("description", "") for c in cards}}]
        for name in sorted(keys, key=str.casefold):
            rows.append({"name": name, "kind": "characteristic", "values": {str(c["nm_id"]): c.get("characteristics", {}).get(name, "") for c in cards}})
        rows.append({"name": "Габариты", "kind": "metadata", "values": {str(c["nm_id"]): c.get("dimensions", {}) for c in cards}})
        return {**meta, "cards": cards, "rows": rows}

    def refresh(self, comparison_id: int) -> dict:
        self._get(comparison_id)
        with self.store._connect() as c:
            c.execute("UPDATE card_comparisons SET updated_at=? WHERE id=?", (_now(), comparison_id))
        return self.detail(comparison_id)

