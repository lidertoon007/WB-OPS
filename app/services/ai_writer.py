"""Черновики ответов на отзывы через ИИ (Claude API или совместимый с OpenAI сервис).

Ключ хранится только в локальной базе WB OPS. Если ИИ не ответил, ответ не прошёл проверку или
ключ не задан — write() возвращает None, и ReplyService берёт ответ сборщика. Публикация не
меняется: ИИ готовит только черновик, отправка — как и раньше, после проверки оператором.
"""
from __future__ import annotations

import json
import re
import time

import httpx

from app.models import InboxItem
from app.services.review_composer import PHRASE_TITLES, analyze

ANTHROPIC_URL = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
STATUS_TEXT = {"buyout": "покупатель забрал товар (выкуп)", "rejected": "покупатель отказался от товара при получении",
               "returned": "покупатель вернул товар", "unknown": "статус покупки неизвестен"}

SYSTEM_PROMPT = """Ты — менеджер магазина одежды на Wildberries (бренды Deemivis, VALOUR KING). Пишешь ответ на отзыв покупателя от имени магазина.

Как писать:
- По-русски, тепло и по-человечески, без канцелярита. 2–4 предложения, обычно 200–450 символов.
- Начни с «Здравствуйте, {имя}!» если имя дано, иначе «Здравствуйте!». Обращайся на «вы».
- Отвечай на то, что написал покупатель: упомяни 1–2 конкретные детали из отзыва (посадка, размер, рост, ткань, цвет, кому купили, повторная покупка). Не пересказывай отзыв целиком.
- Каждый ответ уникален: не используй шаблонные связки подряд («Спасибо за высокую оценку и обратную связь»).
- Без эмодзи, если не сказано иное. Без ссылок, телефонов и промокодов.

Жёсткие правила:
- Если статус «отказ», «возврат» или «неизвестен» — НЕ желай «комфортной носки», не пиши, что товар подошёл или что покупатель его носит.
- Никогда не предлагай оформить возврат или отказ и не обвиняй покупателя.
- Не обещай того, чего магазин не может гарантировать (компенсации, замену, сроки). Можно: «передадим в отдел контроля качества», «проверим партию».
- Вопросы доставки, ПВЗ, примерки конкретного заказа — направь в поддержку Wildberries.
- Размер: посоветуй таблицу замеров в карточке и чат продавца (там помогут подобрать по меркам).
- Цвет на фото может отличаться из-за освещения и экрана — можно сказать это мягко.
- Не придумывай факты о товаре (состав, страну, плотность), если их нет в данных.

Ответь только текстом ответа, без кавычек и пояснений."""

ANALYSIS_PROMPT = """Ты — аналитик отзывов магазина одежды на маркетплейсе. Тебе дают отзывы покупателей об одном товаре.
Выдели, что покупатели хвалят и на что жалуются, и дай продавцу практичные рекомендации (карточка товара, таблица размеров, фото, качество партии, упаковка).
Опирайся только на текст отзывов, ничего не придумывай; если пункт встречается редко — не включай его. Пиши по-русски, коротко: каждый пункт — одно предложение, с указанием, насколько часто это встречается («часто», «несколько покупателей»).
Ответь ТОЛЬКО JSON-объектом без пояснений:
{"summary": "2–3 предложения общего вывода", "pros": ["..."], "cons": ["..."], "advice": ["..."]}
В каждом списке не больше 6 пунктов."""

EXAMPLES = """Примеры ответов магазина в нужном тоне:
— «Ольга, благодарим за отзыв! Мы очень рады, что джинсы вас порадовали — и посадка, и внешний вид, и качество. Желаем комфортной носки и ждём вас снова!»
— «Здравствуйте, Николай. Спасибо за отзыв. Нам жаль, что джинсы оказались вам малы. Мы обязательно проверим соответствие размерной сетки этой модели. Надеемся, следующие покупки вас только порадуют.»
— «Здравствуйте, Валерия! Приносим искренние извинения: нам очень жаль, что вам пришлось столкнуться с неаккуратно пришитыми карманами. Мы передадим информацию на производство и усилим контроль качества.»"""


class AIWriter:
    def __init__(self, settings_provider):
        """settings_provider() -> dict(provider, api_key, base_url, model, tone, length, use_emojis)."""
        self.settings_provider = settings_provider
        self.last_error: str | None = None
        self.last_used_at: float | None = None

    # --- API ---------------------------------------------------------------
    def _anthropic(self, cfg, system, user, max_tokens=500):
        model = cfg.get("model") or self._auto_model(cfg)
        r = httpx.post(f"{ANTHROPIC_URL}/messages", timeout=30,
                       headers={"x-api-key": cfg["api_key"], "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"},
                       json={"model": model, "max_tokens": max_tokens, "system": system,
                             "messages": [{"role": "user", "content": user}]})
        if r.status_code >= 400:
            raise RuntimeError(f"Claude API {r.status_code}: {r.text[:200]}")
        data = r.json()
        return "".join(part.get("text", "") for part in data.get("content", []) if part.get("type") == "text")

    def _openai(self, cfg, system, user, max_tokens=500):
        base = (cfg.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        headers = {"Authorization": f"Bearer {cfg['api_key']}", "content-type": "application/json"}
        body = {"model": cfg.get("model") or self._auto_model(cfg), "max_tokens": max_tokens, "temperature": 0.8,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        r = httpx.post(f"{base}/chat/completions", timeout=60, headers=headers, json=body)
        if r.status_code == 400 and re.search(r"max_tokens|max_completion_tokens|temperature", r.text):
            # Новые модели OpenAI (рассуждающие) принимают max_completion_tokens и только температуру по умолчанию
            body.pop("max_tokens", None)
            body.pop("temperature", None)
            body["max_completion_tokens"] = max_tokens * 8
            r = httpx.post(f"{base}/chat/completions", timeout=90, headers=headers, json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"API {r.status_code}: {r.text[:200]}")
        return r.json()["choices"][0]["message"]["content"] or ""

    def list_models(self, cfg=None) -> list[str]:
        cfg = cfg or self.settings_provider()
        if not cfg.get("api_key"):
            raise RuntimeError("Не задан API-ключ")
        if cfg.get("provider") == "openai":
            base = (cfg.get("base_url") or "https://api.openai.com/v1").rstrip("/")
            r = httpx.get(f"{base}/models", timeout=20, headers={"Authorization": f"Bearer {cfg['api_key']}"})
        else:
            r = httpx.get(f"{ANTHROPIC_URL}/models", timeout=20,
                          headers={"x-api-key": cfg["api_key"], "anthropic-version": ANTHROPIC_VERSION})
        if r.status_code >= 400:
            raise RuntimeError(f"Ключ не принят ({r.status_code}): {r.text[:200]}")
        return [m.get("id") for m in r.json().get("data", []) if m.get("id")]

    def _auto_model(self, cfg) -> str:
        key = cfg.get("api_key", "")[-8:]
        if getattr(self, "_model_cache", (None, None))[0] != key:
            self._model_cache = (key, self.pick_model(cfg))
        return self._model_cache[1]

    def pick_model(self, cfg) -> str:
        """Модель по умолчанию из доступных ключу. Claude: Sonnet → Haiku → Opus. OpenAI: недорогие текстовые модели."""
        models = self.list_models(cfg)
        if not models:
            raise RuntimeError("Ключу не доступна ни одна модель")
        if cfg.get("provider") == "openai":
            skip = re.compile(r"audio|realtime|tts|transcri|embed|image|dall|whisper|moderation|search|codex|instruct|babbage|davinci|computer|vision", re.I)
            text_models = [m for m in models if not skip.search(m)]
            for name in ("gpt-4.1-mini", "gpt-4o-mini", "gpt-5-mini", "gpt-4.1", "gpt-4o", "gpt-5"):
                if name in text_models:
                    return name
            chat = [m for m in text_models if m.startswith(("gpt-", "o"))] or text_models or models
            return sorted(chat)[0]
        for family in ("sonnet", "haiku", "opus"):
            found = [m for m in models if family in m]
            if found:
                return found[0]
        return models[0]

    # --- Ответ на отзыв ------------------------------------------------------
    def prompt_for(self, item: InboxItem, cfg) -> str:
        facts = analyze(item)
        details = ", ".join(PHRASE_TITLES.get(k, k) for k in sorted(facts.aspects)) or "нет"
        style = []
        if cfg.get("tone"):
            style.append(f"Тон: {cfg['tone']}.")
        if cfg.get("length"):
            style.append(f"Длина ответа: {cfg['length']}.")
        if cfg.get("use_emojis"):
            style.append("Можно 1–2 уместных эмодзи в конце.")
        return "\n".join([
            EXAMPLES, "",
            "Отзыв:",
            f"Имя для обращения: {facts.name or 'нет (без имени)'}",
            f"Товар: {item.product_name or facts.product}",
            f"Оценка: {item.rating if item.rating is not None else 'без оценки'} из 5",
            f"Статус: {STATUS_TEXT[facts.status]}",
            f"Текст отзыва: {item.text.strip() or '(без текста)'}",
            f"Что распознано в отзыве: {details}",
            *style,
        ])

    def complete(self, system: str, user: str, max_tokens: int = 1200) -> str:
        """Произвольный запрос к подключённому ИИ (для аналитики). Ошибки пробрасываются."""
        cfg = self.settings_provider()
        if not cfg.get("api_key"):
            raise RuntimeError("ИИ не подключён: задайте API-ключ в разделе «Настройки ИИ»")
        call = self._openai if cfg.get("provider") == "openai" else self._anthropic
        text = call(cfg, system, user, max_tokens)
        self.last_used_at = time.time()
        return text

    def analyze_product(self, name: str, reviews: list[str]) -> dict:
        """ИИ-разбор отзывов о товаре → {summary, pros, cons, advice}."""
        if not reviews:
            raise RuntimeError("Нет отзывов с текстом за выбранный период")
        user = (f"Товар: {name or 'без названия'}\nОтзывы покупателей (оценка★: текст), всего {len(reviews)}:\n"
                + "\n".join(f"- {r}" for r in reviews))
        raw = self.complete(ANALYSIS_PROMPT, user)
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            raise RuntimeError("ИИ вернул ответ не в формате JSON")
        try:
            data = json.loads(match.group(0))
        except ValueError as exc:
            raise RuntimeError("ИИ вернул некорректный JSON") from exc
        clean = lambda v: [str(x).strip()[:300] for x in (v or []) if str(x).strip()][:8]
        return {"summary": str(data.get("summary") or "").strip()[:600], "pros": clean(data.get("pros")),
                "cons": clean(data.get("cons")), "advice": clean(data.get("advice"))}

    def write(self, item: InboxItem, validator=None) -> str | None:
        cfg = self.settings_provider()
        if not cfg.get("api_key"):
            self.last_error = "Не задан API-ключ"
            return None
        try:
            call = self._openai if cfg.get("provider") == "openai" else self._anthropic
            text = call(cfg, SYSTEM_PROMPT, self.prompt_for(item, cfg)).strip().strip('"«»').strip()
        except Exception as exc:  # сеть, ключ, лимиты — откат на сборщик
            self.last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            return None
        problem = self.check(text, item, validator)
        if problem:
            self.last_error = f"Ответ ИИ отклонён: {problem}"
            return None
        self.last_error = None
        self.last_used_at = time.time()
        return text

    def write_answer(self, item: InboxItem, composer, validator=None, history=None) -> str | None:
        """Ответ на вопрос: ИИ получает данные модели и черновик сборщика; числа сверх данных — отказ."""
        cfg = self.settings_provider()
        if not cfg.get("api_key"):
            self.last_error = "Не задан API-ключ"
            return None
        draft, facts = composer.compose(item)
        model = composer._model(item, facts)
        params = composer._params(facts)
        examples = ""
        if history:
            examples = "Прошлые ответы магазина на похожие вопросы (только для тона):\n" + "\n".join(f"— Вопрос: {q}\n  Ответ: {a}" for q, a in history[:3])
        style = []
        if cfg.get("tone"):
            style.append(f"Тон: {cfg['tone']}.")
        if cfg.get("use_emojis"):
            style.append("Можно 1 уместный эмодзи в конце.")
        prompt = "\n".join([examples, "",
                             f"Имя для обращения: {facts.name or 'нет'}",
                             f"Товар: {item.product_name or '—'} (WB ID {item.product_id or '—'})",
                             f"Вопрос: {item.text.strip()}",
                             f"Распознанные параметры покупателя: {params or 'нет'}" + (f"; размер {facts.size}" if facts.size else "") + (f"; ростовка {facts.length_l}" if facts.length_l else ""),
                             "Данные модели:", model_facts_text(model), "",
                             f"Черновик по данным (можно переписать своими словами, факты сохранить): {draft}", *style])
        try:
            call = self._openai if cfg.get("provider") == "openai" else self._anthropic
            text = call(cfg, QUESTION_PROMPT, prompt).strip().strip('"«»').strip()
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            return None
        allowed = set(re.findall(r"\d+", " ".join([item.text, draft, model_facts_text(model)])))
        extra = [n for n in re.findall(r"\d+", text) if n not in allowed and len(n) >= 2]
        if extra:
            self.last_error = f"Ответ ИИ отклонён: числа не из данных модели ({', '.join(extra[:3])})"
            return None
        problem = self.check(text, item, validator)
        if problem:
            self.last_error = f"Ответ ИИ отклонён: {problem}"
            return None
        self.last_error = None
        self.last_used_at = time.time()
        return text

    @staticmethod
    def check(text: str, item: InboxItem, validator=None) -> str | None:
        if len(text) < 20:
            return "слишком короткий"
        if len(text) > 1500:
            return "слишком длинный"
        if re.search(r"https?://|www\.|\+7\s?\d|8\s?\(?9\d{2}", text):
            return "ссылка или телефон"
        status = analyze(item).status if item.item_type.value == "review" else "buyout"
        if status != "buyout" and re.search(r"комфортн\w* носк|приятн\w* носк|носите с удовольств", text.lower()):
            return "пожелание носки при отказе/возврате"
        if validator:
            warnings = validator(text, item)
            if warnings:
                return "; ".join(warnings)
        return None


QUESTION_PROMPT = """Ты — менеджер магазина джинсов на Wildberries (бренды Deemivis, VALOUR KING). Отвечаешь на вопрос покупателя о товаре.

Как писать:
- По-русски, вежливо и по делу, 2–5 предложений. Начни с «Здравствуйте, {имя}!» или «Здравствуйте!» (если покупатель написал «Добрый день» — ответь «Добрый день!»).
- Отвечай именно на вопрос и используй цифры покупателя (рост, обхваты, размер).
- Без эмодзи, если не сказано иное. Без ссылок, телефонов, промокодов.

Жёсткие правила:
- Цифры о товаре (размеры, обхваты, длины, состав, проценты, рост) бери ТОЛЬКО из блока «Данные модели» и «Черновик по данным». Если нужной цифры там нет — не придумывай, скажи, что уточните, и попроси параметры (обхват талии и бёдер, рост).
- Прошлые ответы магазина — только для тона; их цифры могут быть устаревшими, не переноси их.
- Цена: её формирует Wildberries (акции, скидки, размер и цвет влияют на цену). Возврат денег, доставка, возврат товара — через Wildberries/поддержку, продавец их не одобряет.
- Не обещай поступлений, сроков и наличия, если этого нет в данных.

Ответь только текстом ответа."""


def model_facts_text(model: dict) -> str:
    from app.services.product_models import FACT_FIELDS, SIZE_FIELDS
    lines = []
    for key, title in FACT_FIELDS.items():
        if key not in ("marks",) and model.get(key):
            lines.append(f"{title}: {model[key]}")
    sizes = model.get("sizes") or []
    if sizes:
        lines.append("Таблица замеров (" + "; ".join(SIZE_FIELDS.values()) + "):")
        for row in sizes:
            lines.append(" | ".join(str(row.get(k) or "—") for k in SIZE_FIELDS))
    if model.get("legacy_measurements"):
        lines.append(f"Замеры (текстом): {model['legacy_measurements']}")
    return "\n".join(lines) or "нет данных"


def describe(cfg: dict) -> dict:
    """Настройки для панели — без самого ключа."""
    key = cfg.get("api_key") or ""
    return {"provider": cfg.get("provider") or "anthropic", "base_url": cfg.get("base_url") or "",
            "model": cfg.get("model") or "", "has_key": bool(key), "key_hint": ("…" + key[-4:]) if key else ""}


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)
