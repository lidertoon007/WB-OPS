from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from uuid import uuid4


class RecommendationStore:
    def __init__(self, path: str = "app/knowledge/recommendations.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = Lock()
        if not self.path.exists():
            self.path.write_text(json.dumps({"items": []}, ensure_ascii=False, indent=2), encoding="utf-8")

    def all(self) -> list[dict[str, object]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return [item for item in data.get("items", []) if isinstance(item, dict)]
        except (OSError, json.JSONDecodeError):
            return []

    def create(self, value: dict[str, object]) -> dict[str, object]:
        item = {"id": uuid4().hex, **value}
        with self.lock:
            values = self.all(); values.append(item)
            self.path.write_text(json.dumps({"items": values}, ensure_ascii=False, indent=2), encoding="utf-8")
        return item

    def update(self, item_id: str, value: dict[str, object]) -> dict[str, object]:
        with self.lock:
            values = self.all()
            for index, item in enumerate(values):
                if item.get("id") == item_id:
                    values[index] = {**item, **value, "id": item_id}
                    self.path.write_text(json.dumps({"items": values}, ensure_ascii=False, indent=2), encoding="utf-8")
                    return values[index]
        raise KeyError(item_id)

    def delete(self, item_id: str) -> None:
        with self.lock:
            values = self.all(); filtered = [item for item in values if item.get("id") != item_id]
            if len(values) == len(filtered):
                raise KeyError(item_id)
            self.path.write_text(json.dumps({"items": filtered}, ensure_ascii=False, indent=2), encoding="utf-8")
