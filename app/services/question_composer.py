"""Индивидуальные ответы на вопросы покупателей без ИИ.

Разбирает вопрос: что спрашивают (размер, рост, длина, ширина низа, состав, сезон, цена…) и какие
цифры назвал покупатель (рост, вес, обхваты, размер, ростовка L). Отвечает по данным модели из
раздела «Товары» (таблица замеров и факты) и согласованным продавцом ориентирам.
Без этих данных не придумывает цифры: отвечает на то, что известно, и подсказывает, что уточнить.
"""
from __future__ import annotations

import hashlib
import html
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
    ("return", r"одобрит\w* возврат|оформ\w* возврат|вернуть (?:это|товар|изделие|заказ)|как вернуть|обмен|обменять|отменить (?:заказ|товар)|примерк\w* в пвз|отказ\w* от товар"),
    ("price", r"цен[аыу]|дорожа|подорожа|в корзине|стоимост|\d+\s*₽|прыгает|руб"),
    ("find_mark", r"маркировк|этикетк|[A-ZА-Я]{1,2}\d{3,5}[A-ZА-Я]?\d?[-–][A-ZА-Я]\d|ссылк"),
    ("availability", r"будут ли|будет ли|будут еще|в наличии|поступлен|появ[ия]тся|есть ли (?:эти|размер|в размере|модел)|есть размер|есть модель|когда будут"),
    ("units", r"единиц\w* измерен|в дюйм|сажен"),
    ("ru_map", r"(?:это|соответству\w*)\s+\d{2}\s*[-–/]\s*\d{2}\s*(?:размер|р\.?)?\??|\d{2}\s+это\s+\d{2}"),
    ("composition", r"состав|хлоп|эластан|спандекс|полиэст|процент\w* (?:содерж|состав)|материал"),
    ("stretch", r"тян[уе]т|стрейч|стр[еэ]ч|эластичн|плотные\??$|прям очень плотн"),
    ("density", r"плотност|унци|вес товара|тонк|плотн"),
    ("season", r"летн|лето|демисез|зим|минус|тепл|утепл|флис|холод|сезон"),
    ("shrink", r"усадк|садятся|сядут|после стирки"),
    ("country", r"страна|производств|турци|китай"),
    ("fastener", r"застеж|застёж|ширинк|молни|пуговиц"),
    ("rise", r"посадк|высота посадки|длину посадки|сидени"),
    ("bottom", r"ширин\w*[^.?]{0,20}(?:низ|брючин|штанин)|по низу|заужен|внизу"),
    ("length", r"длин|шаговому|внутренн\w* шв|внешн\w* шв|по шву|ростовк|коротк|\bl\s*-?\s*\d{2}|\bw\s*-?\s*\d{2}"),
    ("what_height", r"на (?:какой|какую) рост|какой рост|где посмотреть[^?]{0,20}рост|на рост рассчит|рост максимум"),
    ("size_pick", r"какой размер|как заказать|какой (?:мне|нам)? ?(?:надо|нужен|подойд)|какой брать номер|размер (?:подойд|выбрать|заказать|брать|нужен|надо)|подобрать|подскажите размер|какой выбрать|что (?:брать|заказать)|подойд[её]т|подойдут|будут (?:не )?коротк|норм будет|будут норм"),
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
    raw = html.unescape(item.text or "").strip()
    t = raw.lower().replace("ё", "е")
    greet = "Добрый день!" if re.search(r"добр\w+ (?:день|дня)", t) else ("Добрый вечер!" if "добрый вечер" in t else "Здравствуйте!")
    f = QuestionFacts(text=t, name=first_name(item.author_name), greeting=greet)
    # рост: «рост 187», «на рост 190-191», «2м», «рост мужа 177»
    f.height = _num(t, r"рост\w*\D{0,12}(1\d{2}|2\d{2})(?!\d)") or _num(t, r"\bрос\s+(1\d{2}|2\d{2})(?!\d)") or _num(t, r"(1[5-9]\d|20\d)\s*(?:см)?\s*(?:рост)")
    if f.height is None:
        metres = _num(t, r"(?:рост\w*\s*|на\s+)(1[.,][5-9]\d)(?!\d)")
        if metres:
            f.height = round(metres * 100)
    if f.height is None and re.search(r"рост\w*[^.?]{0,10}\b2\s*м\b|\b2\s*м\b", t):
        f.height = 200.0
    f.weight = _num(t, r"вес\w*\D{0,6}(\d{2,3})") or _num(t, r"(\d{2,3})\s*кг")
    f.waist = _num(t, r"(?:от|обхват\w* талии|талия|талии|пояс)\s*[-–:]?\s*(?:(?:под животиком|на уровне пупка)\s*)?(\d{2,3}(?:[.,]\d)?)(?!\d|\s*размер)")
    f.hips = _num(t, r"(?:об|обхват\w* бедер|бедра|бедер|бедрам)\s*[-–:]?\s*(?:по выступам ягодиц\s*)?(\d{2,3}(?:[.,]\d)?)")
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
        if not w and int(size) >= 42 and int(size) % 2 == 0 and int(size) <= 66:
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
    if re.search(r"замер|ширин\w* (?:по поясу|пояса|талии|бедер|бедра|бедре|колена)|обхват\w* (?:пояса|бедер)", t):
        f.intents.append("garment_dimensions")
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
        for src, dst in (("season", "season"), ("care", "care"), ("details", "notes")):
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
                    out.append(f"По таблице этой модели с вашими параметрами ({params}) нельзя уверенно рекомендовать размер: даже максимальный указанный размер {row['size']}{ru} может быть тесным. Проверьте замеры другого размера или модели.")
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
            out.append(f"Проверьте доступность ростовки {f.length_l} у нужного размера в актуальной карточке товара.")
            return out
        if f.waist or f.hips:
            out.append("Сверьте обхваты талии и бёдер с таблицей размеров именно этой модели в карточке товара. Без подтверждённых замеров модели конкретный размер назвать нельзя.")
            if not (f.waist and f.hips):
                out.append(f"Напишите, пожалуйста, обхват {'бёдер' if f.waist else 'талии'} в сантиметрах для более точного подбора.")
        elif not f.size:
            missing = "обхват талии и бёдер" + ("" if f.height else " и рост")
            out.append(f"Чтобы подсказать размер именно в этой модели, напишите, пожалуйста, {missing} в сантиметрах.")
        if f.height:
            out.append(f"Для роста {int(f.height)} см проверьте длину по внутреннему шву нужного размера в таблице модели.")
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

    def _garment_measurements(self, f, m) -> list[str]:
        row = size_row(m, f.size or f.ru_size)
        if not row or not row.get("garment_waist"):
            return []
        fields = (
            ("waist_width", "пояс в разложенном виде", r"пояс|талии"),
            ("garment_waist", "расчётный обхват пояса", r"обхват.*(?:пояс|талии)"),
            ("hips_width", "полуобхват бёдер", r"бедер"),
            ("thigh_width", "ширина одной штанины в бедре", r"бедра|бедре"),
            ("knee_width", "ширина колена", r"колен"),
            ("bottom", "ширина низа", r"низ"),
            ("front_rise", "передняя посадка", r"посадк"),
            ("back_rise", "задняя посадка", r"посадк"),
        )
        measurements = [f"{label} — {_cm(row[key])}" for key, label, pattern in fields
                        if row.get(key) and (re.search(pattern, f.text) or "замер" in f.text)]
        if re.search(r"обхват\w* бедер", f.text) and (hips := parse_range(row.get("hips_width"))):
            measurements.append(f"расчётный обхват бёдер — {_cm(round(hips[0] * 2, 5))}")
        if not measurements:
            return []
        return [f"Замеры изделия для размера {row['size']}: {', '.join(measurements)}."]

    def _length_measurements(self, f, m, key, label) -> list[str]:
        rows = m.get("sizes", [])
        row = size_row(m, f.size or f.ru_size) if f.size or f.ru_size else (rows[0] if key == "inseam" and rows else None)
        if not row or not row.get(f"{key}_l32"):
            return []
        lengths = [f"{length} — {_cm(row[field])}" for length in ("L32", "L33")
                   if (field := f"{key}_{length.lower()}") in row and row[field] and (not f.length_l or length == f.length_l)]
        if not lengths:
            return []
        size = f" у {row['size']} размера" if f.size or f.ru_size else ""
        return [f"{label.capitalize()}{size}: {', '.join(lengths)}."]

    def _fact(self, m, key, fallback) -> list[str]:
        value = str(m.get(key) or "").strip()
        return [value.rstrip(".") + "."] if value else [fallback]

    def _merchant_guidance(self, f, m, item) -> tuple[list[str], set[str]] | None:
        """Ответы, согласованные продавцом; таблица конкретной модели важнее общих ориентиров."""
        t = f.text
        def answer(body, *handled):
            return body, {"general", *handled}
        if "composition" in f.intents and re.search(r"карточк|описан", t) and re.search(r"фото|этикетк|ярлык|ответ", t) and re.search(r"3\s*%", t) and re.search(r"2\s*%", t):
            return answer(["Спасибо, что заметили расхождение. В описании карточки ошибка: верное содержание эластана — 2%. Исправим данные в карточке товара."], "composition", "find_mark")
        if re.search(r"slim\s*fit|слим\s*фит", t):
            return answer(["Сейчас моделей slim fit у нас нет. Следите за новинками в магазине продавца — ассортимент обновляется."], "availability")
        if re.search(r"продл\w*[^.?]{0,25}хранен|продл\w*[^.?]{0,25}срок", t):
            return answer(["Продавец не может продлить срок хранения заказа. Пожалуйста, обратитесь в поддержку Wildberries через приложение — там подскажут, доступно ли продление для вашего заказа."])
        if re.search(r"фамили\w*[^.?]{0,20}джин[сц]|джин[сц][^.?]{0,20}фамили", t):
            return answer(["Не совсем поняли, что вы имеете в виду под «фамилией» джинсов. Уточните, пожалуйста: нужен бренд, название модели или артикул? Поможем найти информацию."])
        if str(item.product_id) == "1916636501" and re.search(r"вес(?:ит|а)? товар|вес джинс|сколько вес", t):
            return answer(["Вес этих джинсов — примерно 600–700 г."])
        if "country" in f.intents and re.search(r"производств|производител|чь[её]", t):
            return answer(["Наши джинсы производятся по турецким технологиям. Это не означает конкретную страну производства; её для выбранного артикула смотрите в характеристиках карточки товара."], "country")
        if re.search(r"классическ|прям", t) and re.search(r"флис|утепл", t) and re.search(r"налич|есть", t):
            return answer(["Утеплённые прямые классические модели представлены в магазине продавца. Актуальное наличие нужного цвета и размера проверьте в карточке товара."], "availability", "season")
        if "price" in f.intents and re.search(r"при выкупе|после выкупа", t):
            return answer(["Стоимость фиксируется при оформлении заказа. Если при выкупе сумма отличается от указанной в вашем заказе, проверьте детали покупки и обратитесь в поддержку Wildberries."], "price")
        if re.search(r"где[^.?]{0,25}таблиц|не вижу[^.?]{0,25}таблиц|таблиц[^.?]{0,25}не (?:вижу|указан)", t):
            return answer(["Таблица размеров обычно находится на втором фото карточки товара. Если она не отображается или нужного замера там нет, напишите в чат продавца — поможем уточнить его."], "length", "what_height", "size_pick")
        if str(item.product_id) == "480554276" and re.search(r"рост|длин", t):
            return answer(["Эта модель рассчитана на высокий рост. Ростовка может различаться между старой и новой коллекциями; для нужного размера уточните её в чате продавца. Ширину низа тоже подскажем по конкретному размеру."], "length", "what_height", "bottom", "size_pick")
        if re.search(r"внутренн\w* шв|вгутренн\w* шв", t):
            return answer(["Длину по внутреннему шву для нужного размера смотрите в таблице размеров на втором фото карточки товара. Если нужного замера нет, напишите в чат продавца — уточним по модели."], "length")
        if "what_height" in f.intents or re.search(r"ростовк|ростовскую|почему не пишете рост", t):
            return answer(["Ростовка может различаться между старой и новой коллекциями. Напишите в чат продавца артикул и нужный размер — уточним длину именно этого варианта."], "what_height", "length")
        if "find_mark" in f.intents:
            found = self.models.by_mark(f.mark) if f.mark else None
            if not f.mark and not re.search(r"этикетк[^:]*:\s*\S", t):
                return answer(["Не совсем поняли вопрос. Уточните, пожалуйста, что указано на этикетке и что вы хотели узнать о джинсах."], "find_mark", "length", "size_pick")
            if not found and re.search(r"покупал|покупали|заказывал|нужны джинсы|этикетк", t):
                return answer(["Чтобы найти именно такие джинсы, проверьте историю покупок в приложении маркетплейса — там можно открыть карточку ранее заказанного товара."], "find_mark", "length", "size_pick")
        if re.search(r"джог[г]?ер", t) and re.search(r"сидени", t):
            return answer(["Джоггеры — это брюки свободного кроя, обычно с эластичным поясом и манжетами внизу. Увеличенная длина сидения не является обязательной особенностью джоггеров: всё зависит от конкретной модели и её посадки."], "rise", "length")
        if re.search(r"описан[^.?]{0,50}без застежки", t) and re.search(r"фото[^.?]{0,35}(?:пуговиц|молни)|фактически", t):
            return answer(self._fact(m, "fastener", "Фактически у этой модели есть застёжка: пуговица и молния."), "fastener")
        if "price" in f.intents and re.search(r"заказал|оформил|должны прийти|после заказа", t):
            return answer(["Итоговая цена видна в корзине перед оформлением заказа и может отличаться у разных размеров и цветов. После оформления заказа стоимость фиксируется: для вас действует цена, указанная на момент покупки. Благодарим за заказ!"], "price")
        if re.search(r"(?:жарко|жарковато|сезон|летом)", t) and re.search(r"[+]?(?:24)\s*(?:°|градус|$)", t):
            return answer(["При температуре +24 °C в этих джинсах может быть жарковато, особенно при длительном нахождении на солнце. Всё зависит от индивидуальных ощущений."], "season")
        if re.search(r"(?:светл|цвет)[^.?]{0,70}(?:плотн|синтет)|плотн[^.?]{0,30}не синтет", t):
            return answer(["Перейдите в наш магазин на маркетплейсе и выберите подходящий цвет. " + ("Состав этой модели: " + m["composition"].rstrip(".") + "." if m.get("composition") else "Точный состав выбранной модели смотрите в характеристиках её карточки товара.")], "composition", "density", "availability")
        if not m.get("sizes"):
            if f.height and f.height >= 190 and not (f.waist or f.hips) and ("size_pick" in f.intents):
                intro = f"Чтобы проверить, подойдёт ли {f.ru_size} размер на рост {int(f.height)} см, нужны ваши замеры." if f.ru_size else f"По одному росту {int(f.height)} см сложно определить подходящий размер."
                return answer([intro + " Пришлите, пожалуйста, обхват талии и бёдер, а также нужную длину по внутреннему шву в сантиметрах — поможем с выбором."], "size_pick", "length")
            if "length" in f.intents and f.size and not f.length_l and not (f.height or f.waist or f.hips):
                return answer(self._fact(m, "height_note", "Для ответа о длине нужен замер конкретного размера. Проверьте длину по внутреннему шву в таблице карточки товара."), "length", "size_pick")
        if re.search(r"долго[^.?]{0,35}(?:идет|идти|товар|достав|посыл)|(?:задерж|срок)[^.?]{0,25}достав|когда[^.?]{0,20}(?:придет|доставят)", t):
            return answer(["Логистикой и сроками доставки занимается маркетплейс. Пожалуйста, обратитесь в его поддержку через приложение — там смогут проверить, где находится ваш заказ и почему он задерживается."])
        if "резинк" in t and re.search(r"верев|шнур", t) and re.search(r"без\s+(?:застежки|пуговиц|молнии)", t):
            return answer(["Вариант только на резинке со шнурком, без застёжки и пуговицы, — это другая модель, пока её нет в продаже. Подпишитесь на наш магазин и следите за новинками: они появляются у нас часто."], "fastener", "availability")
        if "ширинк" in t:
            return answer(self._fact(m, "fastener", "У этой модели стандартная ширинка на молнии."), "fastener")
        if re.search(r"(?:обхват талии|талия)[^?]{0,55}(?:меньше|больше)[^?]{0,25}\d{2}|\d{2}[^?]{0,30}(?:меньше|больше)[^?]{0,25}\d{2}", t):
            return answer(["У фабрики предусмотрены допуски замеров 1–2 см, поэтому значения у соседних размеров могут немного отличаться. При выборе рекомендуем сверить параметры с размерной таблицей в карточке товара."], "waist_for_size", "garment_dimensions")
        if re.search(r"(?:широкие|широкая|фасон)[^?]{0,45}(?:фото|обычн)|на фото[^?]{0,25}(?:обычн|широк)", t):
            return answer(["На фотографиях в карточке показан актуальный фасон модели. Ориентируйтесь на фото и замеры в размерной таблице, чтобы оценить посадку."])
        if re.search(r"цвет[^.?]{0,45}(?:ярк|стар|стиран)|(?:старые|стираные)[^.?]{0,20}(?:цвет|джинс)|пришли цвет", t):
            return answer(["Понимаем ваши пожелания по цвету. Оттенок джинсов может немного отличаться от фотографий в карточке из-за освещения и настроек экрана. Рекомендуем ориентироваться на фотографии товара и отзывы покупателей."], "season")
        if re.search(r"какие (?:id|артикул)|как еще найти|как найти", t) and re.search(r"летн|тонк", t):
            return answer(["Перейдите в наш магазин: летние модели отмечены в названиях товаров. Длина зависит от выбранного размера — ростовку L32 и замеры проверьте в карточке конкретной модели."], "length", "season", "density", "find_mark")
        if re.search(r"есть (?:такие|такая)[^?]{0,20}(?:голуб|син)|(?:голубые|голубой)\??$", t):
            return answer(["Посмотрите доступные цвета в нашем магазине на маркетплейсе — там представлены актуальные варианты моделей."], "availability")
        if "availability" in f.intents or re.search(r"когда[^?]{0,35}продаже|будет в продаже|ответите[^?]{0,30}(?:р\b|размер)|насчет\s*\d{2}\s*р", t):
            return answer(["Проверяйте актуальное наличие нужного размера в карточке товара. Обычно мы пополняем ассортимент каждую неделю, но точную дату появления конкретного размера пока назвать нельзя. Подпишитесь на магазин и следите за обновлениями."], "availability")
        # Подтверждённые продавцом факты относятся к этим артикулам, а не ко всему каталогу.
        ids = {str(item.product_id), *re.findall(r"(?<!\d)\d{9}(?!\d)", t)}
        if "922327365" in ids and re.search(r"тонк|плотност|летн", t):
            return answer(self._fact(m, "density", "Это тонкие летние джинсы, для лета они хорошо подойдут."), "density", "season")
        if "922327387" in ids and re.search(r"цвет|син|состав|хлоп|синтет", t):
            return answer(["Цвет — синий.", *self._fact(m, "composition", "Точный состав смотрите в характеристиках карточки товара.")], "composition", "season")
        if "955536606" in ids and f.height and 185 <= f.height <= 187 and not m.get("sizes"):
            body = ["По длине эта модель подойдёт на рост 185–187 см. Для выбора размера сверьте замеры с размерной таблицей в карточке товара."]
            if "резинк" in t:
                body += self._fact(m, "fastener", "Наличие резинки в поясе проверьте по фото и описанию именно этой модели в карточке товара.")
            return answer(body, "size_pick", "length", "fastener")
        return None

    def compose(self, item: InboxItem) -> tuple[str, QuestionFacts]:
        f = analyze_question(item)
        m = self._model(item, f)
        merchant_body = self._merchant_guidance(f, m, item)
        body, handled = merchant_body if merchant_body else ([], set())
        priority = ["complaint", "refund", "return", "size_pick", "length", "what_height", "bottom", "waist_for_size", "garment_dimensions", "ru_map",
                    "composition", "stretch", "density", "season", "shrink", "rise", "fastener", "country", "availability",
                    "find_mark", "price", "units"]
        intents = sorted(f.intents, key=lambda i: priority.index(i) if i in priority else 99) or ["general"]
        done = set(handled)
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
                if re.search(r"забрал|забрали|получил|получили|после получен", f.text):
                    body.append("Если товар уже забрали, оформите возврат в разделе «Покупки» приложения Wildberries или обратитесь в поддержку площадки по этому заказу. Отказ при примерке в ПВЗ относится к ещё не полученному товару.")
                elif re.search(r"пвз|пункт\w* выдач|примерк", f.text):
                    body.append("При примерке в ПВЗ Wildberries можно отказаться от неподошедшего товара и выкупить подходящий размер. Условия конкретного заказа проверьте в приложении Wildberries.")
                else:
                    body.append("Возврат и отмену оформляет Wildberries: заявку можно подать в приложении в разделе «Покупки». Обменять размер напрямую нельзя — нужный размер оформляется отдельным заказом.")
            elif intent == "price":
                body.append("Итоговая цена видна в корзине Wildberries перед оформлением заказа. Цена может отличаться у разных размеров и цветов; после оформления заказа стоимость для вас фиксируется на момент покупки.")
            elif intent == "find_mark":
                found = self.models.by_mark(f.mark) if f.mark else None
                if found and found.get("nm_ids"):
                    body.append(f"Модель {f.mark} есть у нас — артикул Wildberries {found['nm_ids'][0]}. Найдите его через поиск в приложении.")
                else:
                    body.append(f"Спасибо, что написали маркировку{' ' + f.mark if f.mark else ''}! Проверим, какой артикул соответствует этой модели, и подскажем. Также её можно найти через поиск в приложении по бренду в нашем магазине.")
            elif intent == "availability":
                body.append(f"Актуальное наличие{' ' + f.size + ' размера' if f.size else ''} смотрите в карточке товара. Обычно ассортимент пополняется каждую неделю, но точную дату появления конкретного размера назвать нельзя. Следите за обновлениями карточки.")
            elif intent == "units":
                body.append("Замеры в таблице указаны в сантиметрах. Ширину (талия, бёдра, низ) часто меряют по изделию в разложенном виде — это половина обхвата, поэтому значение нужно умножить на 2.")
            elif intent == "ru_map":
                row = size_row(m, f.ru_size or f.size) if m.get("sizes") else None
                if row and row.get("ru"):
                    body.append(f"Да, {row['size']} размер в этой модели соответствует российскому {row['ru']}.")
                else:
                    body.append("Соответствие размеров указано в таблице размеров карточки; точнее подобрать поможет обхват талии и бёдер — напишите их, подскажем.")
            elif intent == "composition":
                body += ["Состав: " + m["composition"].rstrip(".") + "."] if m.get("composition") and not m["composition"].lower().startswith("состав") else self._fact(m, "composition", "Точный состав смотрите в характеристиках карточки товара.")
            elif intent == "stretch":
                body += self._fact(m, "stretch", "Да, наши джинсы немного тянутся: в составе 2% эластана.")
            elif intent == "density":
                summer = re.search(r"летн|тонк", str(m.get("season", "")) + " " + str(m.get("notes", "")) + " " + f.text, re.I)
                fallback = "Плотность ткани этой летней модели уточняйте в характеристиках карточки товара." if summer else "У классических джинсов плотность ткани — 400–475 г/м². Для конкретной модели сверьте характеристики в карточке товара."
                body += self._fact(m, "density", fallback)
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
                body += self._garment_measurements(f, m) or self._fact(m, "rise" if m.get("rise") else "fit", "Посадку модели уточним — напишите, какой размер вас интересует, и подскажем высоту посадки.")
            elif intent == "bottom":
                body += self._measure(f, m, "bottom", "ширина низа")
            elif intent == "length":
                key, label = ("outseam", "длина по внешнему шву") if re.search(r"внешн|наружн", f.text) else ("inseam", "длина по внутреннему шву")
                lengths = self._length_measurements(f, m, key, label)
                if lengths:
                    body += lengths
                    done.add("size_pick")
                    continue
                if f.length_l:
                    body.append(wl_text(f))
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
                body += self._fact(m, "height_note", "Для ответа по росту нужно сравнить длину по внутреннему шву нужного размера с таблицей этой модели. Напишите ваш рост и размер — поможем сориентироваться.")
            elif intent == "waist_for_size":
                body += self._garment_measurements(f, m) or self._measure(f, m, "waist", "обхват талии")
            elif intent == "garment_dimensions":
                body += self._garment_measurements(f, m)
            elif intent == "size_pick":
                body += self._size_pick(f, m, item)
            elif intent == "general":
                body.append("Спасибо за вопрос! Уточните, пожалуйста, что именно вас интересует — размер, длина, состав или наличие, — и мы подскажем по этой модели.")
        body = [b for b in dict.fromkeys(body) if b]
        if not body:
            body = ["Спасибо за вопрос! Уточните, пожалуйста, что именно вас интересует — размер, длина, состав или наличие, — и мы подскажем по этой модели."]
        greeting = f"Здравствуйте, {f.name}!" if f.name else f.greeting
        closings = [c for c in CLOSINGS if "чат" not in c] if any("чат" in b for b in body) else CLOSINGS
        if merchant_body:
            closings = ["Если останутся вопросы — пишите, поможем!", "Хорошего дня!"]
        if set(intents) & {"complaint", "refund", "return"}:
            closings = ["Если останутся вопросы — пишите, поможем!"]
        closing = self._pick(item.id, "closing", closings)
        text = " ".join([greeting, *body, closing])
        return re.sub(r"\s{2,}", " ", text).strip(), f
