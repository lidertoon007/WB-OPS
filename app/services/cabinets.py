import json
from dataclasses import dataclass, replace

MARKETPLACES = {"wildberries": "Wildberries", "ozon": "Ozon", "yandex": "Яндекс.Маркет"}
# Страна регистрации продавца → валюта расчётов
COUNTRIES = {"RU": ("Россия", "RUB", "Российский рубль"), "BY": ("Беларусь", "BYN", "Белорусский рубль"),
             "KZ": ("Казахстан", "KZT", "Казахстанский тенге"), "AM": ("Армения", "AMD", "Армянский драм"),
             "KG": ("Киргизия", "KGS", "Киргизский сом"), "UZ": ("Узбекистан", "UZS", "Узбекский сум")}


@dataclass(frozen=True)
class Cabinet:
    id: str
    name: str
    token: str
    marketplace: str = "wildberries"
    legal_name: str = ""
    client_id: str = ""          # Ozon: Client ID; Яндекс Маркет: ID кабинета/кампании
    reviews: bool = True         # загружать и обрабатывать отзывы
    questions: bool = True       # загружать и обрабатывать вопросы
    chats: bool = False          # чаты с покупателями
    tax_percent: float | None = None   # ставка налога, % (None — как в настройках ОПиУ)
    enabled: bool = True         # выключенный кабинет не загружается и не попадает в РНП
    country: str = "RU"          # страна регистрации продавца
    ads_token: str = ""          # отдельный ключ API для рекламы (если пусто — основной)
    stats: bool = True           # собирать статистику и продажи: ОПиУ, дашборд, РНП

    def public(self) -> dict:
        """Данные для интерфейса — без токена."""
        return {"id": self.id, "name": self.name, "marketplace": self.marketplace,
                "marketplace_title": MARKETPLACES.get(self.marketplace, self.marketplace), "legal_name": self.legal_name,
                "client_id": self.client_id, "token_configured": bool(self.token), "ads_token_configured": bool(self.ads_token),
                "enabled": self.enabled, "country": self.country, "country_title": COUNTRIES.get(self.country, (self.country,))[0],
                "currency": COUNTRIES.get(self.country, ("", "", ""))[1], "currency_title": COUNTRIES.get(self.country, ("", "", ""))[2],
                "reviews": self.reviews, "questions": self.questions, "chats": self.chats, "stats": self.stats,
                "tax_percent": self.tax_percent,
                "supported": self.marketplace == "wildberries"}

    def to_json(self) -> dict:
        return {"id": self.id, "name": self.name, "token": self.token, "marketplace": self.marketplace, "legal_name": self.legal_name,
                "client_id": self.client_id, "reviews": self.reviews, "questions": self.questions, "chats": self.chats,
                "tax_percent": self.tax_percent, "enabled": self.enabled, "country": self.country, "ads_token": self.ads_token,
                "stats": self.stats}

    def with_changes(self, **changes) -> "Cabinet":
        return replace(self, **changes)


def _flag(value, default: bool) -> bool:
    return default if value is None else bool(value)


def _rate(value):
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


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
        marketplace = str(value.get("marketplace") or "wildberries")
        cabinets.append(Cabinet(
            id=str(value["id"]), name=str(value.get("name") or value["id"]), token=str(value.get("token") or ""),
            marketplace=marketplace if marketplace in MARKETPLACES else "wildberries",
            legal_name=str(value.get("legal_name") or ""), client_id=str(value.get("client_id") or ""),
            reviews=_flag(value.get("reviews"), True), questions=_flag(value.get("questions"), True),
            chats=_flag(value.get("chats"), False), tax_percent=_rate(value.get("tax_percent")),
            enabled=_flag(value.get("enabled"), True), country=str(value.get("country") or "RU").upper()[:2],
            ads_token=str(value.get("ads_token") or ""), stats=_flag(value.get("stats"), True)))
    return cabinets


def dump_cabinets(cabinets: list[Cabinet]) -> str:
    return json.dumps([c.to_json() for c in cabinets], ensure_ascii=False)
