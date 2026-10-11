import json
from pathlib import Path
from threading import Lock
from uuid import uuid4


DEFAULT_TEMPLATES = {
    "review_positive": "Здравствуйте! Спасибо за высокую оценку и обратную связь!",
    "review_size": "Здравствуйте! Для выбора размера рекомендуем сравнить параметры с таблицей замеров в карточке товара. При необходимости напишите нам в чат продавца — поможем сориентироваться.",
    "review_defect": "Здравствуйте! Спасибо за обратную связь. Передадим информацию в отдел контроля качества и проверим модель.",
    "review_delivery": "Здравствуйте! По вопросам доставки, выдачи и примерки конкретного заказа рекомендуем обратиться в поддержку Wildberries.",
    "question_measurements": "Здравствуйте! Для точного подбора сравните параметры с таблицей замеров в карточке товара. Если останутся вопросы, напишите нам в чат продавца — поможем сориентироваться.",
    "question_details": "Здравствуйте! Проверьте характеристики и фотографии в карточке товара. Если уточните нужную деталь, мы проверим информацию по модели.",
}


class TemplateStore:
    def __init__(self, path: str = "app/knowledge/templates.json"):
        self.path = Path(path)
        self._lock = Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(json.dumps({"templates": DEFAULT_TEMPLATES}, ensure_ascii=False, indent=2), encoding="utf-8")
        self.custom_path = self.path.with_name("custom_templates.json")
        if not self.custom_path.exists():
            self.custom_path.write_text(json.dumps({"templates": []}, ensure_ascii=False, indent=2), encoding="utf-8")

    def all(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            values = data.get("templates", {})
            return {str(k): str(v) for k, v in values.items()}
        except (OSError, json.JSONDecodeError):
            return dict(DEFAULT_TEMPLATES)

    def update(self, key: str, text: str) -> str:
        if key not in DEFAULT_TEMPLATES:
            raise KeyError(key)
        value = text.strip()
        if len(value) < 5:
            raise ValueError("Шаблон слишком короткий")
        with self._lock:
            values = self.all()
            values[key] = value
            self.path.write_text(json.dumps({"templates": values}, ensure_ascii=False, indent=2), encoding="utf-8")
        return value

    def custom_all(self) -> list[dict[str, object]]:
        try:
            data = json.loads(self.custom_path.read_text(encoding="utf-8"))
            return [item for item in data.get("templates", []) if isinstance(item, dict)]
        except (OSError, json.JSONDecodeError):
            return []

    def custom_create(self, value: dict[str, object]) -> dict[str, object]:
        item = {"id": uuid4().hex, **value}
        with self._lock:
            items = self.custom_all()
            items.append(item)
            self.custom_path.write_text(json.dumps({"templates": items}, ensure_ascii=False, indent=2), encoding="utf-8")
        return item

    def custom_update(self, template_id: str, value: dict[str, object]) -> dict[str, object]:
        with self._lock:
            items = self.custom_all()
            for index, item in enumerate(items):
                if item.get("id") == template_id:
                    items[index] = {**item, **value, "id": template_id}
                    self.custom_path.write_text(json.dumps({"templates": items}, ensure_ascii=False, indent=2), encoding="utf-8")
                    return items[index]
        raise KeyError(template_id)

    def custom_delete(self, template_id: str) -> None:
        with self._lock:
            items = self.custom_all()
            filtered = [item for item in items if item.get("id") != template_id]
            if len(filtered) == len(items):
                raise KeyError(template_id)
            self.custom_path.write_text(json.dumps({"templates": filtered}, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def render(text: str, variables: dict[str, object]) -> str:
        result = text
        for key, value in variables.items():
            result = result.replace("{{" + key + "}}", str(value or ""))
        return result.strip()
