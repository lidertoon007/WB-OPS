"""Индивидуальные ответы на вопросы покупателей без ИИ.

Разбирает вопрос: что спрашивают (размер, рост, длина, ширина низа, состав, сезон, цена…) и какие
цифры назвал покупатель (рост, вес, обхваты, размер, ростовка L). Отвечает по данным модели из
раздела «Товары» (таблица замеров и факты). Если данных нет — не придумывает цифры: отвечает на
то, что можно сказать честно, повторяет параметры покупателя и говорит, что нужно для точного ответа.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from app.models import InboxItem
from app.services.product_models import ProductModels, norm_size, parse_range, pick_size, rows_for_height, rows_for_ru, size_row
from app.services.review_composer import first_name


@dataclass
class QuestionFacts:
    text: str
    name: str
    greeting: str
    intents: list[str] = field(default_factory=list)
    height: float | None = None
    weight: float | None = None
    waist: float | None = None
    hips: float | None = None
    inseam: float | None = None
    size: str | None = None
    length_l: str | None = None
    ru_size: str | None = None
    mark: str | None = None


INTENTS = [
    ("complaint", r"пришл[иа]? (?:не те|друг|темно|тёмно|черн|син|сер)|не турци|не похож|подмен|качеств\w* (?:не очень|плох)"),
    ("refund", r"деньги не|не вернул|не пришли деньги|деньги ещ[её]|вернул\w*[^.?]{0,25}деньги"),
    ("return", r"одобрит\w* возврат|оформ\w* возврат|вернуть (?:это|товар|изделие)|как вернуть|обмен|обменять|отменить (?:заказ|товар)"),
    ("price", r"цен[аыу]|дорожа|подорожа|в корзине|стоимост|\d+\s*₽|прыгает|\bруб"),
    ("find_mark", r"маркировк|этикетк|[A-ZА-Я]{1,2}\d{3,5}[A-ZА-Я]?\d?[-–][A-ZА-Я]\d|ссылк"),
    ("availability", r"будут ли|будет ли|в наличии|поступлен|появ[ия]тся|есть ли (?:эти|размер|в размере|модел)|есть размер|есть модель|когда будут"),
    ("units", r"единиц\w* измерен|в дюйм|сажен"),
    ("ru_map", r"(?:это|соответству\w*)\s+\d{2}\s*[-–/]\s*\d{2}\s*(?:размер|р\.?)?\??|\d{2}\s+это\s+\d{2}"),
    ("composition", r"состав|хлоп|эластан|спандекс|полиэст|процент\w* (?:содерж|состав)|материал"),
    ("stretch", r"тян[уе]т|стрейч|эластичн|плотные\??$|прям очень плотн"),
    ("density", r"плотност|унци|вес товара|тонк|плотн"),
    ("season", r"летн|лето|демисез|зим|минус|тепл|утепл|флис|холод|сезон"),
    ("shrink", r"усадк|садятся|сядут|после стирки"),
    ("country", r"страна|производств|турци|китай"),
    ("fastener", r"застеж|застёж|ширинк|молни|пуговиц"),
    ("rise", r"посадк|высота посадки|длину посадки|сидени"),
    ("bottom", r"ширин\w*[^.?]{0,20}(?:низ|брючин|штанин)|по низу|заужен|внизу"),
    ("length", r"длин|шаговому|внутренн\w* шв|внешн\w* шв|по шву|ростовк|коротк|\bl\s*-?\s*\d{2}|\bw\s*-?\s*\d{2}"),
    ("what_height", r"на (?:какой|какую) рост|какой рост|где посмотреть[^?]{0,20}рост|на рост рассчит|рост максимум"),
    ("size_pick", r"какой размер|как заказать|какой (?:мне|нам)? ?(?:надо|нужен|подойд)|размер (?:подойд|выбрать|заказать|брать|нужен|надо)|подобрать|подскажите размер|какой выбрать|что (?:брать|заказать)|подойд[её]т|подойдут|будут (?:не )?коротк|норм будет|будут норм"),
    ("waist_for_size", r"(?:какой|какая) обхват талии|талия у \d{2}|обхват талии размер"),
]


def _num(text: str, pattern: str) -> float | None:
    m = re.search(pattern, text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except (ValueError, IndexError):
        return None


def analyze_question(item: InboxItem) -> QuestionFacts:
    raw = (item.text or "").strip()
    t = raw.lower().replace("ё", "е")
    greet = "Добрый день!" if re.search(r"добр\w+ (?:день|дня)", t) else ("Добрый вечер!" if "добрый вечер" in t else "Здравствуйте!")
    f = QuestionFacts(text=t, name=first_name(item.author_name), greeting=greet)
    # рост: «рост 187», «на рост 190-191», «2м», «рост мужа 177»
    f.height = _num(t, r"рост\w*\D{0,12}(1\d{2}|2\d{2})(?!\d)") or _num(t, r"(1[5-9]\d|20\d)\s*(?:см)?\s*(?:рост)")
    if f.height is None and re.search(r"рост\w*[^.?]{0,10}\b2\s*м\b|\b2\s*м\b", t):
        f.height = 200.0
    f.weight = _num(t, r"вес\w*\D{0,6}(\d{2,3})") or _num(t, r"(\d{2,3})\s*кг")
    f.waist = _num(t, r"(?:от|обхват\w* талии|талия|талии|пояс)\s*[-–:]?\s*(?:под животиком\s*)?(\d{2,3}(?:[.,]\d)?)")
    f.hips = _num(t, r"(?:об|обхват\w* бедер|бедра|бедер|бедрам)\s*[-–:]?\s*(\d{2,3}(?:[.,]\d)?)")
    f.inseam = _num(t, r"внутренн\w*\s*(?:шв\w*|шов)?\D{0,8}(\d{2,3})")
    if f.waist and f.waist > 150:
        f.waist = None
    if f.hips and f.hips < 60:          # «бёдрам 106 мм» и прочие опечатки — просто не используем
        f.hips = None
    w = re.search(r"\bw\s*[-:]?\s*(\d{2})\b", t)
    lm = re.search(r"\bl\s*[-:]?\s*(2[6-9]|3[0-8])\b", t) or re.search(r"рост\w*\s*(2[6-9]|3[0-8])\b", t)
    f.length_l = f"L{lm.group(1)}" if lm else None
    size = None
    for pattern in (r"(?<!\d)(\d{2})\s*(?:-?го\s*)?(?:размер|разм|р\.|р\b)", r"размер\w*\s*[-:]?\s*(\d{2})(?!\d)", r"\bр\.?\s*(\d{2})\b"):
        m = re.search(pattern, t)
        if m:
            size = m.group(1)
            break
    if w:
        size = w.group(1)
    if size:
        if int(size) >= 42 and int(size) % 2 == 0 and int(size) <= 66:
            f.ru_size = size
            m = re.search(rf"(2[6-9]|3\d|4[0-2])\s*\(\s*{size}\s*\)|{size}\s*\(\s*(2[6-9]|3\d|4[0-2])\s*\)", t)
            f.size = (m.group(1) or m.group(2)) if m else None
        else:
            f.size = size
    mark = re.search(r"\b([A-Za-zА-Яа-я]{1,2}\d{3,5}[A-Za-zА-Яа-я]?\d?(?:[-–][A-Za-zА-Яа-я]\d{1,2}))\b", raw)
    f.mark = mark.group(1).upper() if mark else None
    for key, pattern in INTENTS:
        if re.search(pattern, t):
            f.intents.append(key)
    if (f.waist or f.hips or f.height or f.weight) and not set(f.intents) & {"size_pick", "length", "bottom", "what_height"}:
        f.intents.append("size_pick")
    if f.length_l and not f.intents:
        f.intents.append("length")
    if "complaint" in f.intents:
        f.intents = [i for i in f.intents if i in ("complaint", "return", "refund")]
    if "ru_map" in f.intents and "size_pick" in f.intents and not (f.waist or f.hips or f.height):
        f.intents.remove("size_pick")
    return f


def _cm(value) -> str:
    rng = parse_range(value)
    if not rng:
        return str(value)
    fmt = lambda x: str(int(x)) if float(x).is_integer() else str(x).replace(".", ",")
    return f"{fmt(rng[0])}–{fmt(rng[1])} см" if rng[0] != rng[1] else f"{fmt(rng[0])} см"


def inseam_for_height(height: float) -> tuple[int, int]:
    """Ориентир длины по внутреннему шву для роста: примерно 45–47 % роста."""
    return int(round(height * 0.45)), int(round(height * 0.47))


def wl_text(f) -> str:
    parts = []
    if f.size and f.length_l:
        cm = int(round(int(f.length_l[1:]) * 2.54))
        parts.append(f"W{f.size} L{f.length_l[1:]} — это размер по талии {f.size} и ростовка {f.length_l}: длина по внутреннему шву около {cm} см.")
    elif f.length_l:
        cm = int(round(int(f.length_l[1:]) * 2.54))
        parts.append(f"Ростовка {f.length_l} — это длина по внутреннему шву около {cm} см.")
    return " ".join(parts)


CLOSINGS = ["Если останутся вопросы — пишите, поможем!", "Будем рады помочь с выбором!", "Хорошего дня и удачной покупки!",
            "Если сомневаетесь — напишите нам в чат продавца, подскажем."]


class QuestionComposer:
    def __init__(self, models: ProductModels | None = None, products=None):
        self.models = models or ProductModels()
        self.products = products          # старая база фактов по WB ID (свободный текст)

    @staticmethod
    def _pick(item_id: str, key: str, variants: list[str]) -> str:
        return variants[int(hashlib.md5(f"{item_id}:{key}".encode()).hexdigest()[:8], 16) % len(variants)]

    def _model(self, item: InboxItem, facts: QuestionFacts) -> dict:
        model = self.models.for_product(item.product_id) or {}
        legacy = (self.products.get(item.product_id) if self.products else None) or {}
        merged = dict(model)
        for src, dst in (("composition", "composition"), ("season", "season"), ("care", "care"), ("details", "notes")):
            if legacy.get(src) and not merged.get(dst):
                merged[dst] = str(legacy[src])
        if legacy.get("measurements") and not merged.get("height_note"):
            merged["legacy_measurements"] = str(legacy["measurements"])
        return merged

    def _params(self, f: QuestionFacts) -> str:
        parts = []
        if f.height:
            parts.append(f"рост {int(f.height)} см")
        if f.waist:
            parts.append(f"обхват талии {int(f.waist)} см")
        if f.hips:
            parts.append(f"обхват бёдер {int(f.hips)} см")
        if f.weight:
            parts.append(f"вес {int(f.weight)} кг")
        return ", ".join(parts)

    # --- ответы по темам -----------------------------------------------------------
    def _size_pick(self, f, m, item) -> list[str]:
        out = []
        params = self._params(f)
        if m.get("sizes") and (f.waist or f.hips):
            found = pick_size(m, f.waist, f.hips)
            if found:
                row = found["row"]
                ru = f" ({row['ru']})" if row.get("ru") else ""
                why = []
                if row.get("waist"):
                    why.append(f"обхват талии {_cm(row['waist'])}")
                if row.get("hips"):
                    why.append(f"обхват бёдер {_cm(row['hips'])}")
                if found.get("largest"):
                    out.append(f"С вашими параметрами ({params}) самый подходящий в этой модели — {row['size']}{ru} размер, но он может оказаться тесноват: в таблице он рассчитан на {', '.join(why)}.")
                else:
                    out.append(f"С вашими параметрами ({params}) рекомендуем {row['size']}{ru} размер — он рассчитан на {', '.join(why)}.")
                    bw, bh = found.get("by_waist"), found.get("by_hips")
                    if bw is not None and bh is not None and bw is not bh:
                        if row is bh:
                            out.append(f"По талии вам подошёл бы {bw['size']}, а по бёдрам — {bh['size']}: советуем {row['size']}, чтобы джинсы не жали в бёдрах, а свободный пояс можно поддержать ремнём.")
                        else:
                            out.append(f"По бёдрам вам подошёл бы {bh['size']}, а по талии — {bw['size']}: советуем {row['size']}, чтобы было комфортно в поясе.")
                if f.height and row.get("height"):
                    rng = parse_range(row["height"])
                    if rng and f.height > rng[1] + 1:
                        out.append(f"По длине этот размер рассчитан на рост {_cm(row['height'])}, на ваш рост {int(f.height)} см может быть коротковат.")
                    elif rng and f.height < rng[0] - 1:
                        out.append(f"По длине он рассчитан на рост {_cm(row['height'])}, возможно, низ придётся немного подшить.")
                    else:
                        out.append(f"По длине на рост {int(f.height)} см он тоже подходит.")
                elif f.height and row.get("inseam"):
                    out.append(f"Длина по внутреннему шву у него — {_cm(row['inseam'])}.")
                if m.get("size_note"):
                    out.append(m["size_note"].rstrip(".") + ".")
                return out
        if m.get("sizes") and f.ru_size and not (f.waist or f.hips):
            rows = rows_for_ru(m, f.ru_size)
            if rows:
                names = " или ".join(f"{r['size']} ({r['ru']})" for r in rows[:2])
                out.append(f"Российскому {f.ru_size} размеру в этой модели соответствует {names}.")
                if f.height:
                    fit = [r for r in rows if parse_range(r.get("height")) and parse_range(r["height"])[0] - 1 <= f.height <= parse_range(r["height"])[1] + 1]
                    tall = max((parse_range(r.get("height"))[1] for r in m["sizes"] if parse_range(r.get("height"))), default=None)
                    if fit:
                        out.append(f"По длине на рост {int(f.height)} см подойдёт {fit[0]['size']}.")
                    elif tall and f.height > tall + 1:
                        out.append(f"Самые длинные размеры этой модели рассчитаны на рост до {int(tall)} см, поэтому на {int(f.height)} см джинсы могут оказаться коротковаты.")
                out.append("Для точного подбора по талии напишите обхват талии и бёдер — подскажем.")
                return out
        if m.get("sizes") and f.height and not (f.waist or f.hips):
            rows = rows_for_height(m, f.height)
            if rows:
                sizes = ", ".join(r["size"] for r in rows[:6])
                out.append(f"По длине на рост {int(f.height)} см в этой модели подходят размеры {sizes}.")
            out.append("Чтобы подобрать размер по талии, напишите, пожалуйста, обхват талии и бёдер в сантиметрах — подскажем точно.")
            return out
        if f.size and m.get("sizes") and size_row(m, f.size):
            return self._size_info(f, m, ask=f.height)
        # данных модели нет — не придумываем, но отвечаем по существу
        if params:
            out.append(f"Спасибо, что сразу написали параметры ({params}).")
        if f.length_l:
            out.append(wl_text(f))
            out.append(f"В карточке выберите {f.size + ' размер' if f.size else 'нужный размер'} — ростовка указана рядом с размером. Если варианта {f.length_l} нет в списке, значит, сейчас его нет в продаже: нажмите «Сообщить о поступлении».")
            return out
        if f.height:
            lo, hi = inseam_for_height(f.height)
            which = f"у {f.size} размера " if f.size else ""
            out.append(f"По длине на рост {int(f.height)} см обычно нужна длина по внутреннему шву примерно {lo}–{hi} см — сверьте её {which}в таблице замеров карточки.")
        if f.waist or f.hips:
            out.append("Размер по талии подбирайте по таблице замеров в карточке: ориентируйтесь на обхват талии, а при плотных бёдрах — на обхват бёдер; ткань немного тянется, поэтому при попадании между размерами обычно берут меньший.")
        elif not f.size:
            missing = "обхват талии и бёдер" + ("" if f.height else " и рост")
            out.append(f"Чтобы подсказать размер именно в этой модели, напишите, пожалуйста, {missing} в сантиметрах.")
        if m.get("height_note"):
            out.append(m["height_note"].rstrip(".") + ".")
        if m.get("size_note"):
            out.append(m["size_note"].rstrip(".") + ".")
        return out

    def _size_info(self, f, m, ask=None) -> list[str]:
        row = size_row(m, f.size or f.ru_size)
        out = []
        if row:
            bits = []
            for key, label in (("waist", "обхват талии"), ("hips", "обхват бёдер"), ("inseam", "длина по внутреннему шву"),
                               ("outseam", "по внешнему шву"), ("bottom", "ширина низа"), ("height", "рост")):
                if row.get(key):
                    bits.append(f"{label} {_cm(row[key])}")
            ru = f" ({row['ru']})" if row.get("ru") else ""
            out.append(f"У {row['size']}{ru} размера: {', '.join(bits)}.")
            if ask and row.get("height"):
                rng = parse_range(row["height"])
                if rng and ask > rng[1] + 1:
                    out.append(f"На рост {int(ask)} см этот размер может быть коротковат.")
                elif rng and ask >= rng[0] - 1:
                    out.append(f"На рост {int(ask)} см по длине подойдёт.")
        return out

    def _measure(self, f, m, key, label) -> list[str]:
        row = size_row(m, f.size or f.ru_size) if (f.size or f.ru_size) else None
        if row and row.get(key):
            return [f"{label[0].upper() + label[1:]} у {row['size']} размера — {_cm(row[key])}."]
        values = [(r["size"], r[key]) for r in m.get("sizes", []) if r.get(key)]
        if values:
            joined = "; ".join(f"{s} — {_cm(v)}" for s, v in values[:8])
            prefix = f"Для {f.size} размера точных данных нет, по другим размерам: " if f.size else f"{label[0].upper() + label[1:]} по размерам: "
            return [prefix + joined + "."]
        size = f" у {f.size} размера" if f.size else ""
        return [f"Точное значение ({label}{size}) уточним по замерам этой модели — напишите, пожалуйста, нам в чат продавца или посмотрите таблицу замеров в карточке."]

    def _fact(self, m, key, fallback) -> list[str]:
        value = str(m.get(key) or "").strip()
        return [value.rstrip(".") + "."] if value else [fallback]

    def compose(self, item: InboxItem) -> tuple[str, QuestionFacts]:
        f = analyze_question(item)
        m = self._model(item, f)
        body: list[str] = []
        priority = ["complaint", "refund", "return", "size_pick", "length", "what_height", "bottom", "waist_for_size", "ru_map",
                    "composition", "stretch", "density", "season", "shrink", "rise", "fastener", "country", "availability",
                    "find_mark", "price", "units"]
        intents = sorted(f.intents, key=lambda i: priority.index(i) if i in priority else 99) or ["general"]
        done = set()
        for intent in intents:
            if intent in done or len(body) >= 4:
                continue
            done.add(intent)
            if intent == "complaint":
                body.append("Нам очень жаль, что так получилось. Такого быть не должно — передадим информацию ответственным за отгрузку и проверим партию.")
                if "return" in intents:
                    body.append("Возврат оформляется в приложении Wildberries в разделе «Покупки»: при возврате укажите причину, чтобы товар проверили.")
                    done.add("return")
            elif intent == "refund":
                body.append("Сроки возврата денег продавец, к сожалению, не регулирует: обычно Wildberries возвращает их в течение нескольких дней после приёмки товара. Если срок прошёл, напишите, пожалуйста, в поддержку Wildberries — там проверят ваш заказ.")
            elif intent == "return":
                body.append("Возврат и отмену оформляет Wildberries: заявку можно подать в приложении в разделе «Покупки». Обменять размер напрямую нельзя — нужный размер оформляется отдельным заказом.")
            elif intent == "price":
                body.append("Цену формирует Wildberries: на неё влияют акции, скидка постоянного покупателя и способ оплаты, а у разных размеров и цветов цена может отличаться. Поэтому в корзине сумма иногда меняется — итоговая стоимость видна перед оплатой.")
            elif intent == "find_mark":
                found = self.models.by_mark(f.mark) if f.mark else None
                if found and found.get("nm_ids"):
                    body.append(f"Модель {f.mark} есть у нас — артикул Wildberries {found['nm_ids'][0]}. Найдите его через поиск в приложении.")
                else:
                    body.append(f"Спасибо, что написали маркировку{' ' + f.mark if f.mark else ''}! Проверим, какой артикул соответствует этой модели, и подскажем. Также её можно найти через поиск в приложении по бренду в нашем магазине.")
            elif intent == "availability":
                body.append(f"Наличие{' ' + f.size + ' размера' if f.size else ''} меняется после каждой поставки. Добавьте товар в «Избранное» или нажмите «Сообщить о поступлении» в карточке — Wildberries пришлёт уведомление, как только размер появится.")
            elif intent == "units":
                body.append("Замеры в таблице указаны в сантиметрах. Ширину (талия, бёдра, низ) часто меряют по изделию в разложенном виде — это половина обхвата, поэтому значение нужно умножить на 2.")
            elif intent == "ru_map":
                row = size_row(m, f.ru_size or f.size) if m.get("sizes") else None
                if row and row.get("ru"):
                    body.append(f"Да, {row['size']} размер в этой модели соответствует российскому {row['ru']}.")
                else:
                    body.append("Соответствие размеров указано в таблице размеров карточки; точнее подобрать поможет обхват талии и бёдер — напишите их, подскажем.")
            elif intent == "composition":
                body += ["Состав: " + m["composition"].rstrip(".") + "."] if m.get("composition") and not m["composition"].lower().startswith("состав") else self._fact(m, "composition", "Состав указан в карточке товара и на ярлыке изделия. Уточним точный процент для этой модели — напишите нам в чат продавца.")
            elif intent == "stretch":
                body += self._fact(m, "stretch", "Эластичность зависит от состава ткани: при наличии эластана джинсы немного тянутся. Уточним по этой модели в чате продавца.")
            elif intent == "density":
                body += self._fact(m, "density", "Плотность ткани указана в характеристиках карточки. Если нужна точная цифра — уточним по этой модели.")
            elif intent == "season":
                key = "warmth" if re.search(r"минус|холод|зим|тепл", f.text) and m.get("warmth") else "season"
                body += self._fact(m, key, "Сезонность модели указана в карточке товара. Подскажем, подойдёт ли она для вашей погоды, — напишите нам в чат.")
            elif intent == "shrink":
                body += self._fact(m, "shrink", "Если стирать по инструкции на ярлыке (деликатный режим до 30–40 °C, без сушки в барабане), джинсы сохраняют размер и форму.")
            elif intent == "country":
                body += self._fact(m, "country", "Страна производства указана в карточке товара и на ярлыке изделия.")
            elif intent == "fastener":
                body += self._fact(m, "fastener", "Тип застёжки видно на фото в карточке; уточним по этой модели, если нужно.")
            elif intent == "rise":
                body += self._fact(m, "rise", "") if m.get("rise") else self._fact(m, "fit", "Посадку модели уточним — напишите, какой размер вас интересует, и подскажем высоту посадки.")
            elif intent == "bottom":
                body += self._measure(f, m, "bottom", "ширина низа")
            elif intent == "length":
                if f.length_l:
                    body.append(wl_text(f))
                key, label = ("outseam", "длина по внешнему шву") if re.search(r"внешн|наружн", f.text) else ("inseam", "длина по внутреннему шву")
                if f.height and m.get("sizes") and not (f.size or f.ru_size):
                    body += self._size_pick(f, m, item)
                elif f.height and not m.get("sizes"):
                    lo, hi = inseam_for_height(f.height)
                    size = f"{f.size} размера " if f.size else ""
                    body.append(f"На рост {int(f.height)} см обычно нужна длина по внутреннему шву примерно {lo}–{hi} см (меряется от паха до пола без обуви). Сверьте её с длиной {size}в таблице замеров карточки — так сразу будет видно, не окажутся ли джинсы короткими.")
                elif not (f.length_l and not f.size and not f.ru_size and "длин" not in f.text):
                    body += self._measure(f, m, key, label)
                    if f.height and (f.size or f.ru_size):
                        row = size_row(m, f.size or f.ru_size)
                        if row and row.get("height"):
                            body += self._size_info(f, m, ask=f.height)[1:]
                done.add("size_pick")
            elif intent == "what_height":
                if m.get("sizes"):
                    rows = [r for r in m["sizes"] if r.get("height")]
                    if f.size or f.ru_size:
                        row = size_row(m, f.size or f.ru_size)
                        if row and row.get("height"):
                            body.append(f"{row['size']} размер рассчитан на рост {_cm(row['height'])}.")
                            continue
                    if rows:
                        body.append("По росту: " + "; ".join(f"{r['size']} — {_cm(r['height'])}" for r in rows[:8]) + ".")
                        continue
                body += self._fact(m, "height_note", "Ростовка зависит от длины по внутреннему шву в таблице замеров. Напишите ваш рост и размер — подскажем, подойдёт ли по длине.")
            elif intent == "waist_for_size":
                body += self._measure(f, m, "waist", "обхват талии")
            elif intent == "size_pick":
                body += self._size_pick(f, m, item)
            elif intent == "general":
                body.append("Спасибо за вопрос! Уточните, пожалуйста, что именно вас интересует — размер, длина, состав или наличие, — и мы подскажем по этой модели.")
        body = [b for b in dict.fromkeys(body) if b]
        if not body:
            body = ["Спасибо за вопрос! Уточните, пожалуйста, что именно вас интересует — размер, длина, состав или наличие, — и мы подскажем по этой модели."]
        greeting = f"Здравствуйте, {f.name}!" if f.name else f.greeting
        closings = [c for c in CLOSINGS if "чат" not in c] if any("чат" in b for b in body) else CLOSINGS
        if set(intents) & {"complaint", "refund", "return"}:
            closings = ["Если останутся вопросы — пишите, поможем!"]
        closing = self._pick(item.id, "closing", closings)
        text = " ".join([greeting, *body, closing])
        return re.sub(r"\s{2,}", " ", text).strip(), f
