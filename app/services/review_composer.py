"""Индивидуальные ответы на отзывы без ИИ.

Сборщик разбирает отзыв (что понравилось или нет: посадка, размер, ткань, цвет, качество, повторная
покупка, кому купили…) и собирает ответ из фраз под эти детали. Фразы редактируются в разделе
«Шаблоны ответов» (app/knowledge/review_phrases.json), у каждой детали несколько вариантов —
вариант выбирается по ID отзыва, поэтому соседние ответы не повторяются, а повторная генерация
того же отзыва даёт тот же текст.

Правила безопасности те же, что в ReplyService: при отказе, возврате или неизвестном статусе не
желаем «комфортной носки» и не пишем, что товар подошёл; не предлагаем оформить возврат; по
доставке и выдаче направляем в поддержку Wildberries; покупателя не обвиняем.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

from app.models import InboxItem, OrderStatus

# ---------------------------------------------------------------------------
# Имя покупателя
# ---------------------------------------------------------------------------
FIRST_NAMES = set("""
александр алексей анатолий андрей антон аркадий арсений артём артем артур богдан борис вадим валентин валерий
василий виктор виталий владимир владислав всеволод вячеслав габриэль геннадий георгий герман глеб григорий
давид даниил данил денис дмитрий евгений егор захар иван игорь илья кирилл константин лев леонид максим марат
марк матвей михаил никита николай олег павел пётр петр роберт родион роман руслан рустам семён семен сергей
станислав степан тимофей тимур фёдор федор филипп эдуард эльдар юрий ян ярослав азамат жасурбек нурлан ринат
ильдар айдар данияр ербол бахтияр фарид шамиль
александра алина алла алёна алена алиса анастасия ангелина анна антонина арина валентина валерия вера
вероника виктория галина дарья диана ева евгения екатерина елена елизавета жанна зинаида зарина злата зоя инна
ирина карина кира кристина ксения лариса лейла лилия любовь людмила майя маргарита марина мария милана
надежда наталья наталия нина оксана олеся ольга полина раиса регина римма светлана снежана софия софья
тамара татьяна ульяна эвелина эльвира юлия яна ярослава гульнара гульшат айгуль динара кунсулу алия асель
""".split())
PATRONYMIC = re.compile(r"(вич|вна|чна|оглы|кызы)$", re.I)
SURNAME = re.compile(r"(ов|ова|ев|ева|ёв|ёва|ин|ина|ын|ына|ский|ская|цкий|цкая|ко|енко|ук|юк|ян|дзе|швили)$", re.I)
NON_NAMES = {"пользователь", "wildberries", "покупатель", "клиент", "гость", "аноним"}


def first_name(raw: str | None) -> str:
    """«Духонина Алёна Викторовна» → «Алёна»; «Шевченко» → «» (только фамилия); «Пользователь Wildberries» → «»."""
    words = [w for w in re.findall(r"[A-Za-zА-Яа-яЁё-]+", raw or "") if len(w) > 1]
    if not words or any(w.lower() in NON_NAMES for w in words):
        return ""
    known = [w for w in words if w.lower().replace("ё", "е") in {n.replace("ё", "е") for n in FIRST_NAMES}]
    if known:
        name = known[0]
    elif len(words) == 1 and not SURNAME.search(words[0]) and not PATRONYMIC.search(words[0]):
        name = words[0]
    else:
        return ""
    return name[:1].upper() + name[1:].lower()


# ---------------------------------------------------------------------------
# Разбор отзыва
# ---------------------------------------------------------------------------
def _has(text: str, pattern: str) -> bool:
    return re.search(pattern, text) is not None


ASPECTS = {
    # хорошее
    "fit_good": r"сел[иа]? (?:как влит|идеальн|отличн|хорошо|прекрасн|замечательн|супер)|сидят? (?:хорошо|отличн|идеальн|замечательн|супер|по фигуре)|хорошо сидят|по фигуре|как влитые|идут в размер|размер в размер|в размер\b|размер (?:подош[её]л|соответству|отличн|подходит)|соответствуют? размеру|подошли (?:все|отлично|идеально|хорошо|по размеру)|подошл[иа] все хорошо|по размеру (?:очень )?хорошо",
    "length_ok": r"подшивать не (?:надо|нужно|пришлось)|по длине (?:идеальн|отличн|подош)|длина (?:идеальн|отличн|подош)",
    "length_long": r"длинноват|длинные штан|очень длинн|укорот|подшить|подшива(?:ть|ем) придется|длину (?:чуть |немного )?(?:уберем|укоротим)",
    "quality_good": r"качеств\w* (?:хорош|отличн|супер|прекрасн|на высоте|на уровне|приятн|норм)|(?:хорош|отличн|прекрасн|приятн|высок)\w* качеств|качественн|пошив (?:аккуратн|хорош|отличн)|аккуратн\w* пошив|швы|фурнитур",
    "stretch": r"тян[уе]т|стрейч|эластичн",
    "warm": r"т[её]пл|утепл|флис|фланел",
    "light": r"на лето|л[её]гк(?:ие|ая)|летн",
    "color_good": r"(?:красив|хорош|глубок|отличн|насыщенн|приятн|классн|трендов)\w* (?:цвет|оттен)|цвет (?:очень )?(?:хорош|красив|отличн|супер|насыщ|как на фото)|небесно",
    "looks_good": r"смотрятся|симпатичн|стильн|модн|трендов|красив",
    "wash_ok": r"после стирки (?:вс[её] )?(?:отличн|хорош|норм|не)",
    "delivery_fast": r"(?:пришл[иа]|доставили|привезли) (?:быстро|вовремя|в срок)|быстрая доставка",
    "repeat": r"втор(?:ую|ой|ые) (?:пар|раз)|ещё (?:одн|пар)|еще (?:одн|пар)|заказал[аи]? (?:ещё|еще|снова)|заказали такие же|перезаказ|не первый раз|уже (?:второй|третий)",
    "reorder_size": r"перезака|(?:взял[аи]?|возьм\w*|закаж\w*|заказал[аи]?)[^.!]{0,25}размер|размер взял[аи]? (?:другой|больше|меньше)",
    "recommend": r"рекоменд",
    "seller_thanks": r"спасибо (?:продавц|магазин)|продавц\w* спасибо|рекомендую продавц",
    "price_good": r"цен[аы]? (?:отличн|хорош|доступн|приятн|супер)|за такую цену|недорог|цена[^.!]{0,25}на отличном",
    "long_wait_wear": r"после (?:нескольких|пары) (?:месяц|недел)|время покажет|сказать точно можно только",
    "for_husband": r"муж[уа]?\b|мужу",
    "for_son": r"\bсын[уа]?\b|сыну",
    "for_teen": r"подрост|школ",
    "for_grandson": r"внук",
    "for_father": r"\bпап[еа]?\b|отц[ау]|\bотец",
    # проблемы
    "size_small": r"маломер|малы\b|маловат|узк(?:ие|ова|о\b)|тесн|не сходятся|бирк\w* \d+ размер",
    "size_big": r"большемер|велик(?:и|оват)|больш(?:ие|еваты|оват)\b|широк(?:ие|ова|ий очень)|очень широк|широкий очень",
    "size_wrong": r"не подош[её]л\w* (?:размер|ростовк|по размеру)|не подошли размером|размер не подош|ростовка не|не в размер|ошибл\w* с размером|не (?:подошл[иа]|подош[её]л)\b|сел[иа]? не|сидят не|не сел[иа]|не сидят",
    "length_short": r"коротк|подстрел",
    "thin": r"тонк(?:ие|ая|ий|ова)|как лосины|не джинсов|материал не",
    "color_diff": r"(?:другой|не тот|не такой)\s+цвет|цвет\w* (?:совсем )?(?:другой|не тот|не такой|отличает)|темнее|светлее|ярко[- ]син|цвет не (?:очень|соответств)|цвеи не",
    "defect": r"брак|порван|дыр[ак]|крив|протерл|протёрл|распол|нитк|затяжк|пятн|заломы|оторва|разош[её]лс|сломал",
    "not_as_described": r"подмен|не турци|китай|не (?:как|соответству\w*) (?:на )?(?:фото|картинк|карточк|описани)|не такие|другой товар|другая модель|пересорт|не тот товар|пришло другое|вид совсем другой|дешев",
    "price_bad": r"не за эту цену|цена (?:явно )?завышен|дорого|красная цена|переплат",
    "delivery_issue": r"\bпвз\b|пункт выдачи|на почт|почтой|не дали пример|примерять не|курьер|долго (?:шл|вез|достав)|не привез|потерял",
    "smell": r"запах|воня",
}


@dataclass
class ReviewFacts:
    name: str
    status: str                     # buyout / rejected / returned / unknown
    rating: int | None
    text: str
    product: str                    # «джинсы», «джоггеры», «брюки»
    warm_model: bool
    aspects: set[str] = field(default_factory=set)

    @property
    def negative(self) -> bool:
        return bool(self.aspects & {"defect", "not_as_described", "thin", "color_diff", "price_bad", "smell",
                                    "size_small", "size_big", "length_short", "delivery_issue"}) \
            or (self.rating is not None and self.rating <= 3)


def product_word(name: str | None) -> str:
    value = (name or "").lower()
    if "джоггер" in value and "джинс" not in value:
        return "джоггеры"
    if "брюк" in value and "джинс" not in value:
        return "брюки"
    return "джинсы"


def analyze(item: InboxItem) -> ReviewFacts:
    raw = (item.text or "").replace("ё", "е").replace("Ё", "Е")
    # «Недостатки: нет» — не жалоба; остальное разбираем вместе
    text = re.sub(r"недостатки:\s*(нет|не (?:обнаружено|нашла|нашел)|отсутствуют)\b\.?", " ", raw.lower())
    text = re.sub(r"\s+", " ", text).strip()
    aspects = {key for key, pattern in ASPECTS.items() if _has(text, pattern.replace("ё", "е"))}
    # «не тонкие», «не узкие» и т.п. — не жалоба
    for key, word in (("thin", "тонк"), ("size_small", "узк"), ("length_short", "коротк")):
        if key in aspects and re.search(rf"\bне\s+(?:очень\s+)?{word}", text) and not re.search(rf"(?<!не )(?<!не очень ){word}", text.replace(f"не {word}", "")):
            aspects.discard(key)
    if "length_short" in aspects and "хорошо, что не коротк" in text:
        aspects.discard("length_short")
    if "size_wrong" in aspects or "size_small" in aspects or "size_big" in aspects:
        aspects.discard("fit_good")
    if aspects & {"defect", "not_as_described"}:
        aspects -= {"quality_good", "recommend"}
    if "light" in aspects and "thin" in aspects and re.search(r"на лето|летн", text) and not re.search(r"слишком тонк|как лосины|очень тонк|для осени", text):
        aspects.discard("thin")
    status = {OrderStatus.BUYOUT: "buyout", OrderStatus.REJECTED: "rejected", OrderStatus.RETURNED: "returned"}.get(item.order_status, "unknown")
    if re.search(r"\b(вернул[аи]?|вернули|возврат|сдал[аи]? обратно)\b", text):
        status = "returned" if status in ("buyout", "unknown", "returned") else status
    elif re.search(r"\b(отказ\w*|не (?:забрал[аи]?|выкупил[аи]?|стал[аи]? брать))\b", text):
        status = "rejected"
    warm = bool(re.search(r"утепл|флис", (item.product_name or "").lower()))
    return ReviewFacts(name=first_name(item.author_name), status=status, rating=item.rating, text=text,
                       product=product_word(item.product_name), warm_model=warm, aspects=aspects)


# ---------------------------------------------------------------------------
# Фразы
# ---------------------------------------------------------------------------
# {product} — «джинсы/джоггеры/брюки», {name} — имя. Одна строка — один вариант.
DEFAULT_PHRASES: dict[str, list[str]] = {
    "greet_name": ["Здравствуйте, {name}!", "{name}, здравствуйте!"],
    "greet": ["Здравствуйте!"],
    "thanks_5": ["Спасибо за отзыв и пять звёзд!", "Благодарим за высокую оценку!", "Спасибо, что поделились впечатлениями!",
                 "Благодарим за отзыв и отличную оценку!", "Спасибо за тёплый отзыв!"],
    "thanks_long": ["Спасибо за такой подробный отзыв!", "Благодарим за развёрнутый отзыв — это очень ценно для нас!",
                    "Спасибо, что так подробно рассказали о покупке!"],
    "thanks_4": ["Спасибо за отзыв и хорошую оценку!", "Благодарим за отзыв и оценку!"],
    "thanks_neutral": ["Спасибо за обратную связь.", "Благодарим за отзыв.", "Спасибо, что поделились мнением."],
    "thanks_empty_5": ["Спасибо за высокую оценку!", "Благодарим за пять звёзд!", "Спасибо, что оценили покупку на «отлично»!",
                       "Благодарим за отличную оценку!"],
    "fit_good": ["Очень рады, что {product} сели как надо.", "Здорово, что с посадкой всё получилось.",
                 "Приятно знать, что {product} хорошо сидят по фигуре.", "Рады, что размер подошёл точно."],
    "length_ok": ["Хорошо, что и по длине всё подошло.", "И отдельно рады, что длина подошла без подгонки."],
    "length_long": ["Длину многие действительно немного подгоняют под себя — хорошо, что запас есть.",
                    "Спасибо, что отметили длину: запас по длине позволяет подогнать модель под свой рост."],
    "quality_good": ["Спасибо, что отметили качество — мы внимательно следим за пошивом.",
                     "Нам очень приятно, что вы оценили качество.", "Рады, что качество пошива и ткани вас порадовало."],
    "stretch": ["Ткань с эластаном как раз для того, чтобы {product} были удобными в движении.",
                "Хорошо, что вы оценили, как {product} тянутся — это делает их удобными на каждый день."],
    "warm": ["Отличный выбор на холодный сезон — утеплённые {product} будут греть.",
             "Рады, что утеплённая модель вас порадовала — в холода она особенно пригодится."],
    "light": ["Лёгкий деним как раз создан для тёплой погоды.", "Отличный вариант на тёплое время года."],
    "color_good": ["Цвет и правда получился удачным — спасибо, что заметили.", "Рады, что оттенок вам понравился."],
    "looks_good": ["Приятно, что {product} вам понравились и внешне."],
    "wash_ok": ["Спасибо, что поделились, как {product} выглядят после стирки, — это полезно другим покупателям."],
    "delivery_fast": ["Рады, что заказ пришёл быстро."],
    "repeat": ["Особенно приятно, что вы возвращаетесь за новыми покупками!", "Спасибо, что выбираете нас снова!",
               "Очень рады, что вы заказываете у нас повторно!"],
    "recommend": ["Спасибо за рекомендацию — это лучшая поддержка для нас.", "Благодарим, что рекомендуете нас другим покупателям."],
    "seller_thanks": ["И вам спасибо, что выбрали наш магазин!"],
    "price_good": ["И цена тоже порадовала — отлично!", "Приятно, что и цена вас устроила."],
    "long_wait_wear": ["Будем рады, если позже поделитесь, как {product} покажут себя в носке."],
    "for_husband": ["Здорово, что муж оценил обновку!", "Приятно, что покупка понравилась мужу."],
    "for_son": ["Рады, что {product} понравились сыну.", "Здорово, что сын доволен покупкой!"],
    "for_grandson": ["Здорово, что {product} подошли внуку."],
    "for_teen": ["Приятно, что {product} подошли подростку — угодить в этом возрасте непросто!"],
    "for_father": ["Рады, что {product} понравились папе."],
    "wish_buyout": ["Желаем комфортной носки!", "Носите с удовольствием!", "Пусть {product} радуют вас как можно дольше!",
                    "Желаем приятной носки и ждём вас снова!"],
    "wish_other": ["Будем рады видеть вас снова!", "Ждём вас снова в нашем магазине!", "Надеемся, следующая покупка порадует вас полностью."],
    # отказ, возврат, проблемы
    "size_wrong": ["Жаль, что размер не подошёл.", "Нам жаль, что с размером не получилось."],
    "size_small": ["Жаль, что {product} оказались маловаты.", "Нам жаль, что размер оказался меньше ожидаемого."],
    "size_big": ["Жаль, что {product} оказались великоваты.", "Нам жаль, что модель оказалась свободнее, чем хотелось."],
    "length_short": ["Жаль, что по длине модель не подошла.", "Нам жаль, что длины не хватило."],
    "size_advice": ["Для следующего заказа советуем сверить свои мерки с таблицей замеров в карточке — а если напишете нам в чат обхваты талии и бёдер и рост, поможем подобрать размер.",
                    "Чтобы не ошибиться с размером в следующий раз, сравните свои мерки с таблицей замеров в карточке или напишите нам в чат продавца — подскажем.",
                    "Подобрать размер поможет таблица замеров в карточке, а если останутся сомнения — напишите нам в чат, подскажем по вашим меркам."],
    "reorder": ["Рады, что вы решили заказать другой размер — надеемся, он подойдёт идеально!",
                "Здорово, что решили взять другой размер, — пусть в этот раз всё сядет как надо!"],
    "rejected_positive": ["Рады, что сама модель вам понравилась.", "Приятно, что модель пришлась вам по душе."],
    "rejected_quality": ["Приятно, что вы оценили качество.", "Спасибо, что отметили качество модели."],
    "thin": ["Мы учтём ваше мнение о плотности ткани.",
             "Нам жаль, что плотность ткани не совпала с ожиданиями. Плотность и сезонность модели указаны в карточке — обратите на них внимание при выборе."],
    "color_diff": ["Оттенок на фотографиях может немного отличаться из-за освещения и настроек экрана — нам жаль, что цвет оказался не таким, как вы ожидали.",
                   "Передача оттенка на фото зависит от освещения и экрана — проверим, насколько точно цвет показан в карточке."],
    "defect": ["Нам очень жаль, что вам попалось изделие с недостатком. Передадим информацию в отдел контроля качества — проверим партию.",
               "Приносим извинения: такого быть не должно. Мы передадим ваш отзыв в отдел контроля качества."],
    "not_as_described": ["Нам очень жаль, что полученный товар не совпал с ожиданиями. Мы проверим отгрузку и соответствие товара карточке.",
                         "Такого быть не должно — проверим партию и соответствие изделия описанию в карточке."],
    "price_bad": ["Цена формируется с учётом акций и скидок Wildberries и может меняться — следите за предложениями в карточке."],
    "delivery_issue": ["По вопросам доставки, выдачи и примерки конкретного заказа рекомендуем обратиться в поддержку Wildberries — там помогут быстрее всего."],
    "smell": ["Передадим информацию в отдел контроля качества и проверим партию."],
    "size_tip": ["Спасибо за подсказку по размеру — она поможет другим покупателям с выбором.",
                 "Отдельное спасибо за совет по размеру — другим покупателям это очень поможет."],
    "length_long_bad": ["Жаль, что модель оказалась длинновата.", "Нам жаль, что по длине модель не подошла."],
    "negative_general": ["Нам жаль, что покупка не оправдала ожиданий. Будем благодарны, если напишете в чат продавца, что именно не понравилось, — это поможет нам стать лучше.",
                         "Спасибо за честную оценку. Если расскажете подробнее, что не понравилось, мы обязательно учтём это."],
    "empty_low": ["Спасибо за оценку. Если расскажете, что не понравилось, — мы обязательно учтём ваше мнение.",
                  "Будем благодарны, если поделитесь, что можно улучшить, — напишите нам в чат продавца."],
}
PHRASE_TITLES = {
    "greet_name": "Приветствие с именем", "greet": "Приветствие без имени", "thanks_5": "Благодарность за 5★",
    "thanks_long": "Благодарность за подробный отзыв", "thanks_4": "Благодарность за 4★", "thanks_neutral": "Благодарность при замечаниях",
    "thanks_empty_5": "Отзыв без текста, 4–5★", "fit_good": "Хорошо сели", "length_ok": "Подшивать не надо",
    "length_long": "Длинноваты", "quality_good": "Хвалят качество", "stretch": "Тянутся, стрейч", "warm": "Тёплые, утеплённые",
    "light": "Лёгкие, на лето", "color_good": "Хвалят цвет", "looks_good": "Хорошо смотрятся", "wash_ok": "После стирки",
    "delivery_fast": "Быстрая доставка", "repeat": "Повторная покупка", "recommend": "Рекомендуют", "seller_thanks": "Благодарят продавца",
    "price_good": "Хвалят цену", "long_wait_wear": "Оценят позже в носке", "for_husband": "Купили мужу", "for_son": "Купили сыну",
    "for_grandson": "Купили внуку", "for_teen": "Купили подростку", "size_tip": "Совет по размеру от покупателя (выкуп, 4–5★)",
    "length_long_bad": "Длинноваты (отказ, возврат)", "for_father": "Купили папе", "wish_buyout": "Пожелание (выкуп)", "wish_other": "Прощание (отказ, возврат, нет статуса)",
    "size_wrong": "Не подошёл размер", "size_small": "Маломерят", "size_big": "Большемерят", "length_short": "Коротки",
    "size_advice": "Совет по размеру", "reorder": "Перезаказали другой размер", "rejected_quality": "Отказ, но хвалят качество", "rejected_positive": "Отказ, но модель понравилась", "thin": "Тонкие", "color_diff": "Не тот цвет",
    "defect": "Брак", "not_as_described": "Не соответствует описанию, подмена", "price_bad": "Дорого",
    "delivery_issue": "Доставка, ПВЗ", "smell": "Запах", "negative_general": "Негатив без деталей", "empty_low": "Отзыв без текста, 1–3★",
}

POSITIVE_ORDER = ["repeat", "fit_good", "length_ok", "quality_good", "stretch", "warm", "light", "color_good", "looks_good",
                  "wash_ok", "price_good", "delivery_fast", "length_long", "long_wait_wear"]
FOR_WHOM = ["for_husband", "for_son", "for_teen", "for_grandson", "for_father"]
SIZE_KEYS = ("size_small", "size_big", "length_short", "size_wrong")
TIP_KEYS = ("size_small", "size_big", "length_short")   # «маломерят, берите на размер больше» при выкупе и 4–5★
NEGATIVE_ORDER = ["defect", "not_as_described", "delivery_issue", "size_small", "size_big", "length_short", "size_wrong",
                  "thin", "color_diff", "price_bad", "smell"]


class PhraseStore:
    def __init__(self, path: str = "app/knowledge/review_phrases.json"):
        self.path = Path(path)
        self.lock = Lock()

    def all(self) -> dict[str, list[str]]:
        phrases = {key: list(values) for key, values in DEFAULT_PHRASES.items()}
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8")).get("phrases", {})
        except (OSError, ValueError):
            saved = {}
        for key, values in saved.items():
            cleaned = [str(v).strip() for v in values if str(v).strip()] if isinstance(values, list) else []
            if key in phrases and cleaned:
                phrases[key] = cleaned
        return phrases

    def update(self, key: str, values: list[str]) -> list[str]:
        if key not in DEFAULT_PHRASES:
            raise KeyError(key)
        cleaned = [v.strip() for v in values if v and v.strip()]
        if not cleaned:
            raise ValueError("Нужен хотя бы один вариант фразы")
        for value in cleaned:
            if len(value) > 500:
                raise ValueError("Фраза длиннее 500 символов")
            if re.search(r"(оформите|оформить|можете)\s+(?:возврат|отказ)|верните\s+товар", value.lower()):
                raise ValueError("Нельзя предлагать покупателю оформить возврат")
        with self.lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            saved = data.get("phrases", {}) if isinstance(data, dict) else {}
            saved[key] = cleaned
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"phrases": saved}, ensure_ascii=False, indent=2), encoding="utf-8")
        return cleaned

    def reset(self, key: str) -> list[str]:
        if key not in DEFAULT_PHRASES:
            raise KeyError(key)
        with self.lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            data.get("phrases", {}).pop(key, None)
            self.path.write_text(json.dumps({"phrases": data.get("phrases", {})}, ensure_ascii=False, indent=2), encoding="utf-8")
        return list(DEFAULT_PHRASES[key])


# ---------------------------------------------------------------------------
# Сборка ответа
# ---------------------------------------------------------------------------
class ReviewComposer:
    def __init__(self, phrases: PhraseStore | None = None):
        self.phrases = phrases or PhraseStore()

    @staticmethod
    def _seed(item_id: str, key: str) -> int:
        return int(hashlib.md5(f"{item_id}:{key}".encode("utf-8")).hexdigest()[:8], 16)

    def _pick(self, bank, item_id, key, facts) -> str:
        variants = bank.get(key) or DEFAULT_PHRASES.get(key) or [""]
        text = variants[self._seed(item_id, key) % len(variants)]
        return text.replace("{product}", facts.product).replace("{name}", facts.name)

    def compose(self, item: InboxItem) -> tuple[str, ReviewFacts]:
        facts = analyze(item)
        bank = self.phrases.all()
        pick = lambda key: self._pick(bank, item.id, key, facts)
        a = facts.aspects
        parts: list[str] = [pick("greet_name") if facts.name else pick("greet")]
        has_text = len(facts.text) >= 3 and re.search(r"[а-яa-z]", facts.text)
        problems = [k for k in NEGATIVE_ORDER if k in a]
        received = facts.status == "buyout"

        if not has_text:
            if facts.rating is not None and facts.rating <= 3:
                parts.append(pick("empty_low"))
            else:
                parts.append(pick("thanks_empty_5"))
                parts.append(pick("wish_buyout") if received else pick("wish_other"))
            return " ".join(p for p in parts if p), facts

        # Благодарность
        cons = re.search(r"недостатки:\s*(.+)", facts.text)
        has_cons = bool(cons and cons.group(1).strip(" .!,-"))
        tip_only = received and not has_cons and (facts.rating or 0) >= 4 and problems and "size_wrong" not in a \
            and all(k in TIP_KEYS for k in problems)
        if (problems and not tip_only) or (facts.rating is not None and facts.rating <= 3):
            parts.append(pick("thanks_neutral"))
        elif len(facts.text) > 140:
            parts.append(pick("thanks_long"))
        elif facts.rating == 4:
            parts.append(pick("thanks_4"))
        else:
            parts.append(pick("thanks_5"))

        body: list[str] = []
        size_tip = False
        if tip_only:
            # Купил, оставил 4–5★ и советует брать на размер больше/меньше — это подсказка, а не жалоба
            problems, size_tip = [], True
        if facts.status in ("rejected", "returned"):
            size_keys = [k for k in SIZE_KEYS if k in a]
            if not size_keys and "length_long" in a:
                size_keys = ["length_long_bad"]
            serious = [k for k in problems if k in ("defect", "not_as_described", "delivery_issue")]
            for key in serious[:2]:
                body.append(pick(key))
            if size_keys:
                body.append(pick(size_keys[0]))
            elif not serious:
                other = [k for k in problems if k not in size_keys][:1]
                body.extend(pick(k) for k in other)
            liked = a & {"quality_good", "color_good", "looks_good", "stretch", "warm", "light", "recommend", "fit_good"}
            praising = (facts.rating or 0) >= 4 and not problems
            if "quality_good" in a and not serious:
                body.append(pick("rejected_quality"))
            elif (liked or praising) and not serious:
                body.append(pick("rejected_positive"))
            if "reorder_size" in a or (size_keys and "repeat" in a):
                body.append(pick("reorder"))
            elif size_keys or "размер" in facts.text:
                body.append(pick("size_advice"))
            closing = pick("wish_other")
        elif problems:
            for key in problems[:2]:
                body.append(pick(key))
            if any(k in a for k in ("size_small", "size_big", "length_short", "size_wrong")):
                body.append(pick("size_advice"))
            positives = [k for k in POSITIVE_ORDER if k in a and k not in ("length_long",)]
            if positives and not (a & {"defect", "not_as_described"}):
                body.insert(0, pick(positives[0]))
            closing = pick("wish_other")
        elif facts.rating is not None and facts.rating <= 3:
            body.append(pick("negative_general"))
            closing = pick("wish_other")
        else:
            positives = [k for k in POSITIVE_ORDER if k in a]
            # тёплая модель: «тёплые» говорим только если речь про утеплённую модель или покупатель сам про тепло
            whom = next((k for k in FOR_WHOM if k in a), None)
            chosen = positives[:2]
            if whom and received:
                chosen = chosen[:1] + [whom]
            for key in chosen:
                body.append(pick(key))
            if size_tip:
                body.append(pick("size_tip"))
            if "recommend" in a:
                body.append(pick("recommend"))
            elif "seller_thanks" in a:
                body.append(pick("seller_thanks"))
            if not body and facts.rating is not None and facts.rating == 4 and len(facts.text) > 20:
                body.append("Будем рады узнать, что можно улучшить, чтобы в следующий раз заслужить пять звёзд.")
            if received:
                closing = "" if "repeat" in chosen and len(body) > 2 else pick("wish_buyout")
            else:
                closing = pick("wish_other")

        parts.extend(dict.fromkeys(body))     # без повторов
        if closing:
            parts.append(closing)
        text = " ".join(p.strip() for p in parts if p and p.strip())
        return re.sub(r"\s{2,}", " ", text), facts
