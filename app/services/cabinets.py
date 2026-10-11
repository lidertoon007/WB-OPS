import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Cabinet:
    id: str
    name: str
    token: str


def load_cabinets(raw: str) -> list[Cabinet]:
    """Read cabinet definitions from env without exposing tokens in API output."""
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("WB_CABINETS_JSON содержит некорректный JSON") from exc
    if not isinstance(values, list):
        raise ValueError("WB_CABINETS_JSON должен быть массивом")
    cabinets: list[Cabinet] = []
    for value in values:
        if not isinstance(value, dict) or not value.get("id"):
            raise ValueError("Каждый кабинет должен содержать id")
        cabinets.append(Cabinet(id=str(value["id"]), name=str(value.get("name") or value["id"]), token=str(value.get("token") or "")))
    return cabinets
