import json
from pathlib import Path
from threading import Lock
from uuid import uuid4


class KnowledgeGroupStore:
    def __init__(self, path: str = "app/knowledge/groups.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = Lock()
        if not self.path.exists():
            self.path.write_text(json.dumps({"groups": []}, ensure_ascii=False, indent=2), encoding="utf-8")

    def all(self) -> list[dict]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8")).get("groups", [])
        except (OSError, json.JSONDecodeError):
            return []

    def upsert(self, value: dict) -> dict:
        with self.lock:
            groups = self.all()
            item = {"id": value.get("id") or uuid4().hex, "name": value.get("name", "Новая группа"), "product_ids": value.get("product_ids", []), "text": value.get("text", ""), "updated_at": value.get("updated_at", "")}
            groups = [item if x.get("id") == item["id"] else x for x in groups]
            if not any(x.get("id") == item["id"] for x in groups):
                groups.append(item)
            self.path.write_text(json.dumps({"groups": groups}, ensure_ascii=False, indent=2), encoding="utf-8")
            return item

