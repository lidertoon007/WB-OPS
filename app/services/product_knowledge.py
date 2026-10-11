import json
from pathlib import Path
from threading import Lock


class ProductKnowledge:
    """Small editable catalogue of product facts keyed by WB nm_id.

    Facts are deliberately plain text so an operator can maintain them without
    a migration. The file is local and can later be moved to PostgreSQL.
    """

    def __init__(self, path: str = "app/knowledge/products.json"):
        self.path = Path(path)
        self._lock = Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(json.dumps({"products": {}}, ensure_ascii=False, indent=2), encoding="utf-8")

    def all(self) -> dict[str, dict[str, object]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data.get("products", {})

    def get(self, product_id: int | str | None) -> dict[str, object] | None:
        if product_id is None:
            return None
        return self.all().get(str(product_id))

    def upsert(self, product_id: int | str, facts: dict[str, object]) -> dict[str, object]:
        key = str(product_id)
        with self._lock:
            products = self.all()
            current = products.get(key, {})
            current.update({k: v for k, v in facts.items() if v is not None})
            products[key] = current
            self.path.write_text(json.dumps({"products": products}, ensure_ascii=False, indent=2), encoding="utf-8")
            return current
