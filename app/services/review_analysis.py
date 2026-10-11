from __future__ import annotations

import re
from dataclasses import dataclass


TOPIC_RULES: dict[str, tuple[str, ...]] = {
    "quality": ("качест", "брак", "слом", "порван", "дыр", "дефект", "крив", "шов", "молни", "пугов"),
    "size": ("размер", "маломер", "большемер", "посадк", "узк", "широк", "коротк", "длин"),
    "delivery": ("достав", "пвз", "курьер", "привез", "получен", "срок"),
    "packaging": ("упаков", "короб", "пакет", "мят", "поврежден"),
    "material": ("материал", "ткан", "состав", "хлоп", "деним", "эластан", "синтет"),
    "color": ("цвет", "оттен", "выцвет", "линя", "ярк"),
    "price": ("цен", "дорог", "дешев", "скидк", "стоимост"),
    "texture": ("текстур", "мягк", "жестк", "приятн", "колюч", "груб"),
    "smell": ("запах", "воня", "пахн"),
    "wrong_item": ("не тот", "другой товар", "пересорт", "не совпадает"),
}

POSITIVE_WORDS = (
    "отлич", "хорош", "супер", "понрав", "доволь", "рекоменд", "идеаль", "красив", "удоб", "спасибо", "люблю"
)
NEGATIVE_WORDS = (
    "плох", "ужас", "разочар", "брак", "не понрав", "не подош", "не соответств", "слом", "дефект", "воня", "возврат", "отказ"
)

TOPIC_LABELS = {
    "quality": "Качество", "size": "Размер", "delivery": "Доставка", "packaging": "Упаковка",
    "material": "Материал", "color": "Цвет", "price": "Цена", "texture": "Текстура",
    "smell": "Запах", "wrong_item": "Не тот товар", "general": "Общее",
}


@dataclass(frozen=True)
class ReviewMarkup:
    topics: list[str]
    sentiment: str


def analyze_review(text: str, rating: int | None = None) -> ReviewMarkup:
    value = (text or "").lower().replace("ё", "е")
    topics = [topic for topic, words in TOPIC_RULES.items() if any(word in value for word in words)]
    if not topics:
        topics = ["general"]
    positive = sum(len(re.findall(re.escape(word), value)) for word in POSITIVE_WORDS)
    negative = sum(len(re.findall(re.escape(word), value)) for word in NEGATIVE_WORDS)
    if rating is not None:
        positive += 2 if rating >= 4 else 0
        negative += 2 if rating <= 2 else 0
    sentiment = "positive" if positive > negative else "negative" if negative > positive else "neutral"
    return ReviewMarkup(topics=topics, sentiment=sentiment)


def topic_label(value: str) -> str:
    return TOPIC_LABELS.get(value, value)
