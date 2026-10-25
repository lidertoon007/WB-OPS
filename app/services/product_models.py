"""Модели товаров для ответов на вопросы: таблица замеров и факты, одна запись на модель (несколько WB ID).

Факты вносит оператор в разделе «Товары». Подсказки из прошлых ответов показываются рядом, но в
ответы покупателям попадают только подтверждённые оператором данные — так устаревшая или
противоречивая цифра из старого ответа не уйдёт клиенту.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from threading import Lock

FACT_FIELDS = {
    "name": "Название модели",
    "composition": "Состав",
    "stretch": "Тянется ли (стрейч)",
    "density": "Плотность ткани",
    "season": "Сезон",
    "warmth": "Тепло (до какой температуры)",
    "fit": "Фасон и посадка",
    "rise": "Высота посадки",
    "fastener": "Застёжка",
    "country": "Страна производства",
    "shrink": "Усадка после стирки",
    "care": "Уход",
    "size_note": "Совет по размеру (маломерит, большемерит…)",
    "height_note": "На какой рост (общее)",
    "marks": "Маркировки на этикетке (через запятую)",
    "notes": "Прочее для ответов",
}
SIZE_FIELDS = {
    "size": "Размер",
    "ru": "Рос. размер",
    "waist": "Обхват талии, см",
    "hips": "Обхват бёдер, см",
    "inseam": "Внутр. шов, см",
    "outseam": "Внешн. шов, см",
    "bottom": "Ширина низа, см",
    "height": "Рост, см",
}


def parse_range(value) -> tuple[float, float] | None:
    """«78–82», «78-82», «80» → (78, 82) / (80, 80)."""
    numbers = [float(x.replace(",", ".")) for x in re.findall(r"\d+(?:[.,]\d+)?", str(value or ""))]
    if not numbers:
        return None
    return (min(numbers[:2]), max(numbers[:2]))


def norm_size(value) -> str:
    """«W36», «36/L34», «36 (52)» → «36»."""
    match = re.search(r"\d{2}", str(value or ""))
    return match.group(0) if match else str(value or "").strip()


class ProductModels:
    def __init__(self, path: str = "app/knowledge/models.json"):
        self.path = Path(path)
        self.lock = Lock()

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data.get("models", {}) if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, models: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"models": models}, ensure_ascii=False, indent=2), encoding="utf-8")

    def all(self) -> dict[str, dict]:
        return self._read()

    def get(self, key: str) -> dict | None:
        return self._read().get(key)

    def for_product(self, nm_id) -> dict | None:
        if nm_id in (None, ""):
            return None
        nm = str(nm_id)
        for key, model in self._read().items():
            if nm in {str(x) for x in model.get("nm_ids", [])}:
                return {"key": key, **model}
        return None

    def by_mark(self, mark: str) -> dict | None:
        wanted = re.sub(r"[^A-Z0-9]", "", mark.upper())
        if len(wanted) < 4:
            return None
        for key, model in self._read().items():
            for value in str(model.get("marks", "")).split(","):
                if wanted and re.sub(r"[^A-Z0-9]", "", value.upper()) == wanted:
                    return {"key": key, **model}
        return None

    def save(self, key: str, value: dict) -> dict:
        key = re.sub(r"\s+", " ", str(key or "")).strip()[:80]
        if not key:
            raise ValueError("Укажите артикул модели")
        nm_ids = sorted({int(x) for x in re.findall(r"\d{3,12}", " ".join(map(str, value.get("nm_ids", []))))})
        sizes = []
        for row in value.get("sizes") or []:
            if not isinstance(row, dict):
                continue
            clean = {field: str(row.get(field) or "").strip()[:20] for field in SIZE_FIELDS}
            if clean["size"]:
                sizes.append(clean)
        record = {field: str(value.get(field) or "").strip()[:600] for field in FACT_FIELDS}
        record.update(nm_ids=nm_ids, sizes=sizes)
        with self.lock:
            models = self._read()
            taken = {str(nm): other for other, m in models.items() if other != key for nm in m.get("nm_ids", [])}
            clash = [str(nm) for nm in nm_ids if str(nm) in taken]
            if clash:
                raise ValueError(f"WB ID {clash[0]} уже привязан к модели {taken[clash[0]]}")
            models[key] = record
            self._write(models)
        return {"key": key, **record}

    def delete(self, key: str) -> None:
        with self.lock:
            models = self._read()
            if key not in models:
                raise KeyError(key)
            models.pop(key)
            self._write(models)

    def seed(self, links: dict[str, set], names: dict[str, str] | None = None) -> int:
        """Создать пустые модели для известных пар «артикул → WB ID» (из прошлых вопросов). Существующие не трогаем."""
        added = 0
        with self.lock:
            models = self._read()
            known = {str(nm) for m in models.values() for nm in m.get("nm_ids", [])}
            by_lower = {k.lower(): k for k in models}
            for vendor, nms in links.items():
                key = vendor.strip()
                fresh = sorted(int(n) for n in nms if str(n) not in known)
                if not key or not fresh:
                    continue
                existing = by_lower.get(key.lower())
                if existing:                   # тот же артикул в другом регистре — дописываем WB ID
                    models[existing]["nm_ids"] = sorted(set(models[existing].get("nm_ids", [])) | set(fresh))
                    known.update(str(n) for n in fresh)
                    added += 1
                    continue
                models[key] = {**{f: "" for f in FACT_FIELDS}, "name": (names or {}).get(key, ""), "nm_ids": fresh, "sizes": []}
                by_lower[key.lower()] = key
                known.update(str(n) for n in fresh)
                added += 1
            if added:
                self._write(models)
        return added


# ---------------------------------------------------------------------------
# Подбор размера по таблице
# ---------------------------------------------------------------------------
def _first_fitting(rows, value, index):
    """Наименьший размер, у которого верхняя граница диапазона не меньше значения покупателя."""
    for row, rng in ((r, parse_range(r.get(index))) for r in rows):
        if rng and value <= rng[1] + 1:
            return row
    return None


def pick_size(model: dict, waist: float | None = None, hips: float | None = None) -> dict | None:
    """Размер по обхватам: наименьший, в который проходят и талия, и бёдра.

    Возвращает row, exact (талия попадает в диапазон), by_waist / by_hips — размеры по каждому обхвату
    отдельно (чтобы объяснить покупателю, если они расходятся), largest — даже самый большой мал.
    """
    rows = [r for r in model.get("sizes", []) if parse_range(r.get("waist")) or parse_range(r.get("hips"))]
    if not rows or (waist is None and hips is None):
        return None
    by_waist = _first_fitting(rows, waist, "waist") if waist else None
    by_hips = _first_fitting(rows, hips, "hips") if hips else None
    order = {id(r): i for i, r in enumerate(rows)}
    candidates = [r for r in (by_waist, by_hips) if r is not None]
    if (waist and by_waist is None) or (hips and by_hips is None):
        return {"row": rows[-1], "exact": False, "largest": True, "by_waist": by_waist, "by_hips": by_hips}
    row = max(candidates, key=lambda r: order[id(r)])
    w = parse_range(row.get("waist"))
    exact = not waist or not w or w[0] - 1 <= waist
    return {"row": row, "exact": exact, "largest": False, "by_waist": by_waist, "by_hips": by_hips}


def rows_for_ru(model: dict, ru: str) -> list[dict]:
    result = []
    for row in model.get("sizes", []):
        numbers = re.findall(r"\d{2}", str(row.get("ru") or ""))
        if ru in numbers or (len(numbers) == 2 and int(numbers[0]) <= int(ru) <= int(numbers[1])):
            result.append(row)
    return result


def size_row(model: dict, size: str | None) -> dict | None:
    if not size:
        return None
    wanted = norm_size(size)
    for row in model.get("sizes", []):
        if norm_size(row.get("size")) == wanted or (row.get("ru") and str(size) in str(row.get("ru"))):
            return row
    return None


def rows_for_height(model: dict, height: float) -> list[dict]:
    result = []
    for row in model.get("sizes", []):
        rng = parse_range(row.get("height"))
        if rng and rng[0] - 1 <= height <= rng[1] + 1:
            result.append(row)
    return result
