"""
Сводный ОПиУ по всем маркетплейсам и кабинетам.

Оркестратор запускает скрипты маркетплейсов, забирает их таблицы (--dump), сопоставляет
статьи с единым планом статей и собирает один Excel:
  - «Свод»                 — ОПиУ по всем маркетплейсам вместе, месяцы в колонках, с разбивкой статей по МП
  - «Сравнение МП»         — статьи за период по маркетплейсам: суммы и % от выручки
  - лист на каждый маркетплейс — ОПиУ в единых статьях с исходными строками
  - лист на каждый кабинет  — если в маркетплейсе больше одного кабинета
  - «Сопоставление статей» — какая исходная строка в какую единую статью попала
  - «Контроль»             — сходится ли итог с отчётом маркетплейса, сверки выплат, пропуски данных
Отдельный файл unit_economics_*.xlsx — юнит-экономика по артикулам в разрезе МП и кабинетов и реестр
для сводных таблиц (--no-unit — не собирать).

Разрез колонок задаёт --grain: месяцы (по умолчанию), недели за весь период свода или дни одного
месяца. Для недель и дней скрипты маркетплейсов запускаются с --grain day и отдают ОПиУ по датам.

Если последний месяц (неделя) отчёта ещё не закончился, на листах ОПиУ справа добавляется колонка
прогноза на его конец: выручка, итог МП, себестоимость, прибыль, маржинальность — факт по вчерашний
день + среднедневной темп × оставшиеся дни (см. period_forecast).

Запуск (из папки проекта):
  python opiu_all.py                         # обновить данные всех МП и собрать сводный Excel
  python opiu_all.py --from-cache            # без запросов к API — только уже скачанные данные
  python opiu_all.py --from-cache --fill-missing   # из кэша, а кабинеты без кэша — загрузить из API
  python opiu_all.py --reload                # WB: загрузить все недели заново, а не только новые к кэшу
  python opiu_all.py --from 2026-01 --to 2026-08
  python opiu_all.py --only wb,ym            # только выбранные маркетплейсы (ozon, wb, ym)
  python opiu_all.py --grain week            # ОПиУ по неделям (пн-вс) за весь период свода
  python opiu_all.py --grain day --period-month 2026-09    # ОПиУ по дням одного месяца
  Обновить_отчёт.bat                         # то же, что python opiu_all.py --open, двойным щелчком
  ОПиУ_по_неделям.bat, ОПиУ_по_дням.bat      # те же отчёты в разрезе недель и дней

Данные загружаются до текущего месяца. Если API маркетплейса не ответил по какому-то кабинету
(например, Ozon при включённом VPN), кабинет берётся из кэша и помечается на листе «Контроль».

Скрипты маркетплейсов остаются самостоятельными: у каждого свой конфиг, кэш и подробный отчёт.
"""

import argparse
import json
import os
import csv
import re
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

HERE = Path(__file__).resolve().parent
DEFAULT_FROM = "2026-01"   # свод по умолчанию начинается с января 2026
COST_FILE = "cost.csv"     # цены по артикулам: себестоимость и подготовка товара за единицу
COST_MISSING_FILE = "cost_missing.csv"   # артикулы без цены: сюда выписываются, оттуда переносятся в cost.csv
COST_COL, PREP_COL = "сс_за_ед", "подготовка_за_ед"
BRAND_COL, CATEGORY_COL = "бренд", "категория"   # единый бренд и категория артикула для юнит-экономики
PRICE_HEADER = ["артикул", "кратность", COST_COL, PREP_COL, "действует_с", "название (справочно)",
                BRAND_COL, CATEGORY_COL, "маркетплейсы (справочно)", "продано шт. с 01.2026 (справочно)"]

MARKETPLACES = [
    {"key": "ozon", "name": "Ozon", "script": "Ozon/ozon_finance.py", "config": "Ozon/ozon_accounts.json"},
    {"key": "wb", "name": "Wildberries", "script": "Wildberries/wb_finance.py", "config": "Wildberries/wb_accounts.json"},
    {"key": "ym", "name": "Яндекс Маркет", "script": "YandexMarket/ym_finance.py", "config": "YandexMarket/ym_accounts.json"},
]

# ---------------------------------------------------------------------------
# Единый план статей
# ---------------------------------------------------------------------------
REV_SALES = "Продажи (по цене продавца)"
REV_RETURNS = "Возвраты"
COMMISSION = "Комиссия маркетплейса"
LOGISTICS = "Логистика и доставка"
RETURNS = "Обратная логистика, отмены и невыкупы"
PAYMENTS = "Эквайринг и платёжные услуги"
ADS = "Реклама и продвижение"
STORAGE = "Хранение и складские операции"
PENALTY = "Штрафы"
OTHER = "Подписки и прочие услуги"
COMP = "Компенсации и прочие доходы"
ADVANCE = "Авансы за услуги (списано − возвращено)"
COGS = "Себестоимость проданных товаров"
GROSS = "ВАЛОВАЯ ПРИБЫЛЬ (после себестоимости)"
PREP = "Подготовка товара"
TAX = "Налог с выручки"
PROFIT = "ПРИБЫЛЬ (после себестоимости, подготовки и налога)"
RETRO = "Ретро-бонусы производителей"
RETRO_FILE = "retro_bonus.csv"   # ретро-бонусы по брендам: бренд;ретро_бонус_%;действует_с (заполняется в WB OPS)
RETRO_COL = "ретро_бонус_%"

REVENUE_ARTICLES = [REV_SALES, REV_RETURNS]
EXPENSE_ARTICLES = [COMMISSION, LOGISTICS, RETURNS, PAYMENTS, ADS, STORAGE, PENALTY, OTHER]
ARTICLE_DESCRIPTIONS = {
    REV_SALES: "Выручка по цене продавца (до скидок, которые компенсирует маркетплейс)",
    REV_RETURNS: "Возвраты выручки",
    COMMISSION: "Комиссия / вознаграждение за продажу, размещение, агентское вознаграждение",
    LOGISTICS: "Доставка покупателю, магистраль, средняя и последняя миля, обработка отправлений, упаковка",
    RETURNS: "Обратная логистика, доставка и обработка отмен, невыкупов, возвратов и брака",
    PAYMENTS: "Эквайринг, перевод платежей, гибкий график и досрочные выплаты, рассрочка",
    ADS: "Продвижение, клики, бусты, отзывы, баллы и программы лояльности, бейджи",
    STORAGE: "Хранение, приёмка, размещение, утилизация, маркировка",
    PENALTY: "Штрафы и сторно штрафов, нарушения, индекс ошибок, нерекомендованные слоты",
    OTHER: "Подписки, страхование, перенос карточек, прочие удержания",
    COMP: "Компенсации за утерю и повреждение, коррекции продаж, прочие доходы",
    ADVANCE: "Авансы за услуги и возврат неиспользованного остатка (WB «Баллы за отзывы») — движение денег",
    COGS: "Себестоимость проданных товаров из cost.csv: штук × кратность × себестоимость за единицу; возвраты сторнируются",
    PREP: "Подготовка товара из cost.csv (подготовка_за_ед): штук × кратность × стоимость подготовки за единицу",
    RETRO: "Ретро-бонусы производителей из retro_bonus.csv: себестоимость проданного × % бренда; возвраты сторнируются",
    TAX: "Налог с выручки: ставка tax_percent × фактическая цена покупки после скидок маркетплейса, за вычетом возвратов",
}

# Группы скриптов маркетплейсов -> единые статьи
GROUP_MAP = {
    "Ozon": {
        "Комиссия Ozon": COMMISSION,
        "Логистика и доставка": LOGISTICS,
        "Обработка возвратов и отмен": RETURNS,
        "Эквайринг и платёжные услуги": PAYMENTS,
        "Эквайринг": PAYMENTS,
        "Реклама и продвижение": ADS,
        "Хранение и складские операции": STORAGE,
        "Штрафы и нарушения": PENALTY,
        "Подписки и прочие услуги Ozon": OTHER,
        "Прочие услуги Ozon": OTHER,
        "Компенсации и прочие начисления": COMP,
    },
    "Wildberries": {
        "Вознаграждение Wildberries": COMMISSION,
        "Логистика": LOGISTICS,
        "Эквайринг": PAYMENTS,
        "Реклама и продвижение": ADS,
        "Хранение и приёмка": STORAGE,
        "Штрафы": PENALTY,
        "Прочие удержания": OTHER,
        "Компенсации и доплаты": COMP,
        "Авансы за услуги WB и их возврат": ADVANCE,
    },
    "Яндекс Маркет": {
        "Вознаграждение Маркета": COMMISSION,
        "Логистика и обработка заказов": LOGISTICS,
        "Приём и перевод платежей": PAYMENTS,
        "Продвижение и лояльность": ADS,
        "Хранение и склад": STORAGE,
        "Подписки и прочие услуги": OTHER,
        "Компенсации": COMP,
    },
}

# Если группа незнакома (например, скрипт маркетплейса добавил новую) — по ключевым словам в названии группы
GROUP_KEYWORDS = [
    (ADVANCE, ["аванс"]),
    (COMP, ["компенсац", "доплат", "доход"]),
    (PENALTY, ["штраф", "нарушен"]),
    (PAYMENTS, ["эквайр", "платеж", "платёж"]),
    (RETURNS, ["возврат", "невыкуп", "отмен"]),
    (ADS, ["реклам", "продвиж", "лояльн"]),
    (STORAGE, ["хранен", "склад", "приёмк", "приемк"]),
    (LOGISTICS, ["логист", "доставк"]),
    (COMMISSION, ["комисс", "вознагражд"]),
    (OTHER, ["подписк", "прочи", "услуг"]),
]

# Логистика отмен, невыкупов и возвратов выносится в отдельную статью
RETURNS_LABEL = re.compile(r"невыкуп|возврат(?!\s+за\s+доставку)|от клиента|обратн|брак|при отмене|отмен[её]нн", re.I)


def map_row(marketplace, group, label, info_group):
    """(единая статья | None для справочных строк, признак «сопоставлено по ключевым словам»)."""
    if group == info_group:
        return None, False
    if group == "Выручка":
        return (REV_RETURNS if re.search(r"возврат|отмен", label, re.I) else REV_SALES), False
    article = GROUP_MAP.get(marketplace, {}).get(group)
    guessed = False
    if article is None:
        g = group.lower()
        article = next((a for a, words in GROUP_KEYWORDS if any(w in g for w in words)), OTHER)
        guessed = True
    if article == LOGISTICS and RETURNS_LABEL.search(label):
        article = RETURNS
    return article, guessed


def art_key(value):
    """Артикулы у маркетплейсов пишутся по-разному: рт-00028973 и РТ-00028973 — это один товар."""
    return re.sub(r"\s+", "", str(value or "")).upper()


def read_costs(path, price_columns=(COST_COL, "себестоимость")):
    """CSV с ценой по артикулам -> {артикул: [(действует_с, кратность, цена)]}, {артикул: название}."""
    costs, names = defaultdict(list), {}
    if not path.exists():
        return {}, {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        delimiter = ";" if sample.count(";") >= sample.count(",") else ","
        for raw in csv.DictReader(fh, delimiter=delimiter):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            article = art_key(row.get("артикул"))
            price = next((row[c] for c in price_columns if row.get(c)), "")
            price = price.replace(" ", "").replace("\u00a0", "").replace(",", ".")
            if not article or not price:
                continue
            try:
                cost = float(price)
                mult = float((row.get("кратность") or "1").replace(",", ".") or 1) or 1.0
            except ValueError:
                continue
            costs[article].append((row.get("действует_с", ""), mult, cost))
            names.setdefault(article, row.get("название (справочно)") or "")
    for article in costs:
        costs[article].sort(key=lambda x: x[0])
    return dict(costs), names


def read_price_rows(path):
    """Строки прайса как словари с ключами в нижнем регистре."""
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return [{(k or "").strip().lower(): (v or "").strip() for k, v in row.items() if k}
                for row in csv.DictReader(fh, delimiter=";")]


def write_price_rows(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(PRICE_HEADER)
        writer.writerows([row.get(c, "") for c in PRICE_HEADER] for row in rows)


def merge_missing_costs(cost_path, missing_path):
    """Переносит заполненные цены из cost_missing.csv в cost.csv.
    Дописываются только пустые ячейки: цену, уже стоящую в cost.csv, меняют в самом cost.csv."""
    columns = (COST_COL, PREP_COL, BRAND_COL, CATEGORY_COL)
    filled = [r for r in read_price_rows(missing_path)
              if r.get("артикул") and any(r.get(c) for c in columns)]
    if not filled:
        return 0
    rows = read_price_rows(cost_path)
    index = {(art_key(r.get("артикул")), r.get("действует_с", "")): r for r in rows}
    changed = 0
    for m in filled:
        key = (art_key(m["артикул"]), m.get("действует_с", ""))
        target = index.get(key)
        if target is None:
            target = {"артикул": m["артикул"], "кратность": m.get("кратность") or "1",
                      "действует_с": m.get("действует_с", ""),
                      "маркетплейсы (справочно)": m.get("маркетплейс (справочно)", ""),
                      "продано шт. с 01.2026 (справочно)": m.get("продано шт. (справочно)", "")}
            rows.append(target)
            index[key] = target
        updated = False
        for col in columns:
            if m.get(col) and not target.get(col):
                target[col] = m[col]
                updated = True
        changed += updated
    if changed:
        write_price_rows(cost_path, rows)
    return changed


def read_article_book(path):
    """Ручной справочник из cost.csv: {артикул: (бренд, категория)}. Пустые ячейки — «не заполнено»."""
    book = {}
    for row in read_price_rows(path):
        article = art_key(row.get("артикул"))
        if not article:
            continue
        have = book.setdefault(article, ["", ""])
        for i, col in enumerate((BRAND_COL, CATEGORY_COL)):
            have[i] = have[i] or (row.get(col) or "").strip()
    return {a: tuple(v) for a, v in book.items()}


def article_catalog(entities, manual):
    """Единый бренд и категория по артикулу для юнит-экономики.
    Приоритет — заполненное вручную в cost.csv; иначе самое частое значение из данных маркетплейсов
    (WB отдаёт бренд и предмет в строках отчёта; Ozon и Яндекс Маркет в финансовых отчётах их не дают)."""
    seen = defaultdict(lambda: (defaultdict(int), defaultdict(int)))
    for e in entities:
        for article, brand, category in e.get("catalog") or []:
            key = art_key(article)
            if not key:
                continue
            brands, categories = seen[key]
            if brand:
                brands[str(brand).strip()] += 1
            if category:
                categories[str(category).strip()] += 1

    def top(counter):
        return max(counter.items(), key=lambda kv: (kv[1], kv[0]))[0] if counter else ""

    catalog = {}
    for key in set(seen) | set(manual):
        brands, categories = seen.get(key, ({}, {}))
        own_brand, own_category = manual.get(key, ("", ""))
        catalog[key] = (own_brand or top(brands), own_category or top(categories))
    return catalog


def sync_article_book(path, catalog):
    """Дописывает бренд и категорию из данных маркетплейсов в пустые ячейки cost.csv.
    Заполненное вручную не трогаем — cost.csv остаётся главным справочником."""
    rows = read_price_rows(path)
    if not rows:
        return 0
    changed = 0
    for row in rows:
        brand, category = catalog.get(art_key(row.get("артикул")), ("", ""))
        for col, value in ((BRAND_COL, brand), (CATEGORY_COL, category)):
            if value and not (row.get(col) or "").strip():
                row[col] = value
                changed += 1
    if changed:
        write_price_rows(path, rows)
    return changed


def cost_for(costs, article, month):
    """Цена, действующая в этом месяце: строка с наибольшей датой «действует_с», не позже месяца."""
    rows = costs.get(art_key(article))
    if not rows:
        return None
    chosen = None
    for date_from, mult, cost in rows:
        if not date_from or date_from[:7] <= month:
            chosen = (mult, cost)
    return chosen


def brand_key(value):
    """Бренд для сопоставления: без лишних пробелов и регистра."""
    return re.sub(r"\s+", " ", str(value or "")).strip().upper()


def read_retro(path):
    """retro_bonus.csv -> {бренд: [(действует_с, %)]} по возрастанию даты."""
    out = defaultdict(list)
    if not path.exists():
        return {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        delimiter = ";" if sample.count(";") >= sample.count(",") else ","
        for raw in csv.DictReader(fh, delimiter=delimiter):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            brand = brand_key(row.get("бренд"))
            try:
                pct = float((row.get(RETRO_COL) or "").replace(" ", "").replace(",", "."))
            except ValueError:
                continue
            if brand:
                out[brand].append((row.get("действует_с", ""), pct))
    for rows in out.values():
        rows.sort(key=lambda x: x[0])
    return dict(out)


def retro_for(retro, brand, month):
    """% ретро-бонуса бренда, действующий в этом месяце (дне): строка с наибольшей датой «действует_с» не позже."""
    chosen = None
    for date_from, pct in retro.get(brand_key(brand)) or []:
        if not date_from or date_from[:7] <= month:
            chosen = pct
    return chosen


def apply_retro(entities, costs, retro, catalog, months):
    """(мп, кабинет) -> месяц -> ретро-бонус (доход, «+»): себестоимость проданного × % бренда артикула.
    Возвраты уменьшают бонус так же, как себестоимость; без себестоимости бонуса нет."""
    out = defaultdict(lambda: defaultdict(float))
    if not retro:
        return out
    for e in entities:
        for mon, article, qty, _revenue in e.get("sales", []):
            if mon not in months:
                continue
            pct = retro_for(retro, (catalog.get(art_key(article)) or ("", ""))[0], mon)
            found = cost_for(costs, article, mon) if pct else None
            if found:
                mult, cost = found
                out[(e["mp"], e["cabinet"])][mon] += qty * mult * cost * pct / 100.0
    return out


def apply_costs(entities, costs, preps, months):
    """Себестоимость и подготовка по месяцам, строки листа «Товары» и артикулы без цены."""
    cogs = defaultdict(lambda: defaultdict(float))         # (мп, кабинет) -> месяц -> себестоимость (минус)
    prep = defaultdict(lambda: defaultdict(float))         # (мп, кабинет) -> месяц -> подготовка (минус)
    goods = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])      # (мп, артикул) -> штук, выручка, себестоимость, подготовка
    missing = defaultdict(lambda: [0.0, 0.0])              # (мп, артикул) -> штук, выручка без себестоимости
    missing_prep = defaultdict(lambda: [0.0, 0.0])         # (мп, артикул) -> штук, выручка без подготовки
    covered = defaultdict(lambda: [0.0, 0.0])              # (мп, кабинет) -> выручка с себестоимостью, вся выручка
    for e in entities:
        for mon, article, qty, revenue in e.get("sales", []):
            if mon not in months:
                continue
            g = goods[(e["mp"], art_key(article))]
            g[0] += qty
            g[1] += revenue
            cov = covered[(e["mp"], e["cabinet"])]
            cov[1] += revenue
            found = cost_for(costs, article, mon)
            if found:
                mult, cost = found
                g[2] += qty * mult * cost
                cogs[(e["mp"], e["cabinet"])][mon] -= qty * mult * cost
                cov[0] += revenue
            else:
                m = missing[(e["mp"], article)]
                m[0] += qty
                m[1] += revenue
            found_prep = cost_for(preps, article, mon)
            if found_prep:
                mult, price = found_prep
                g[3] += qty * mult * price
                prep[(e["mp"], e["cabinet"])][mon] -= qty * mult * price
            elif preps:
                m = missing_prep[(e["mp"], article)]
                m[0] += qty
                m[1] += revenue
    return cogs, prep, goods, missing, missing_prep, covered


def order_costs(entities, costs, months):
    """Себестоимость по заказам: строки листа «Себестоимость по заказам» и итоги по кабинетам для сверки."""
    lines, totals = [], defaultdict(float)
    counter = Counter("расчёт по заказам", sum(len(e.get("orders", [])) for e in entities))
    for e in entities:
        for mon, day, order, article, qty, revenue in e.get("orders", []):
            counter.tick()
            if mon not in months:
                continue
            found = cost_for(costs, article, mon)
            mult, cost = found if found else (None, None)
            amount = qty * mult * cost if found else None
            if found:
                totals[(e["mp"], e["cabinet"])] += amount
            lines.append((e["mp"], e["cabinet"], mon, day, order, article, qty, mult, cost, amount))
    counter.finish()
    lines.sort(key=lambda r: (r[0], r[1], r[3] or r[2], r[4], r[5]))
    return lines, totals


def tax_rates(marketplaces):
    """Ставки налога из конфигов маркетплейсов: tax_percent в корне файла и/или у кабинета."""
    rates = {}
    for mp in marketplaces:
        path = HERE / mp["config"]
        if not path.exists():
            continue
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        default = cfg.get("tax_percent")
        for account in cfg.get("accounts") or []:
            name = account.get("name") or str(account.get("client_id") or "")
            rate = account.get("tax_percent", default)
            if rate is not None:
                rates[(mp["name"], name)] = float(rate)
        if default is not None:
            rates[(mp["name"], None)] = float(default)
    return rates


def seller_revenue(e, months):
    """Выручка по цене продавца — запасной вариант для старых кэшей без tax_base."""
    result = defaultdict(float)
    for article in REVENUE_ARTICLES:
        for vals in e["table"].get(article, {}).values():
            for mon, value in vals.items():
                if mon in months:
                    result[mon] += value
    return dict(result)


def purchase_tax_base(e, months):
    """Фактическая цена покупки после скидок маркетплейса, возвраты уменьшают базу."""
    if e.get("tax_base") is not None:
        return {mon: float(value) for mon, value in e["tax_base"].items() if mon in months}
    return seller_revenue(e, months)


def apply_tax(entities, rates, months):
    """Налог с выручки: ставка × фактическая цена покупки, по месяцам."""
    tax = defaultdict(lambda: defaultdict(float))
    used = {}
    for e in entities:
        rate = rates.get((e["mp"], e["cabinet"]), rates.get((e["mp"], None)))
        if not rate:
            continue
        used[(e["mp"], e["cabinet"])] = rate
        for mon, value in purchase_tax_base(e, months).items():
            tax[(e["mp"], e["cabinet"])][mon] -= value * rate / 100.0
    return tax, used


# ---------------------------------------------------------------------------
# Юнит-экономика по артикулам
# ---------------------------------------------------------------------------
NO_ARTICLE = "Без артикула"
QTY_SOLD, QTY_RETURNED = "Продано, шт", "Возвращено, шт"
SECTIONS = {REV_SALES: "Выручка", REV_RETURNS: "Выручка", COMP: "Компенсации", ADVANCE: "Авансы",
            COGS: "Себестоимость", RETRO: "Ретро-бонусы", PREP: "Подготовка", TAX: "Налог",
            QTY_SOLD: "Количество", QTY_RETURNED: "Количество",
            **{a: "Расходы МП" for a in EXPENSE_ARTICLES}}
REGISTRY_HEADER = ["Маркетплейс", "Кабинет", "Месяц", "Артикул", "Название", "Раздел", "Статья",
                   "Исходная строка отчёта МП", "Отнесение", "Сумма, ₽", "Кол-во, шт"]


def unit_registry(entities, costs, preps, rates, months, names, retro=None, catalog=None):
    """Строки реестра юнит-экономики: МП × кабинет × месяц × артикул × статья × исходная строка.
    Сумма: доходы «+», расходы «−»; по кабинету сумма реестра = прибыль в ОПиУ."""
    rows = []

    def add(e, mon, article, statya, label, amount, qty=None, direct=True):
        key = art_key(article) or NO_ARTICLE
        rows.append([e["mp"], e["cabinet"], mon, key, names.get(key, ""), SECTIONS[statya], statya, label,
                     "прямо" if direct and key != NO_ARTICLE else "без артикула",
                     round(amount, 2), qty])

    for e in entities:
        if e.get("by_article") is None:
            continue
        rate = rates.get((e["mp"], e["cabinet"]), rates.get((e["mp"], None))) or 0.0
        revenue = defaultdict(float)
        for mon, article, group, label, value in e["by_article"]:
            if mon not in months:
                continue
            statya, _ = map_row(e["mp"], group, label, e["info_group"])
            if statya is None:
                continue
            add(e, mon, article, statya, label, value)
            if statya in REVENUE_ARTICLES:
                revenue[(mon, art_key(article))] += value
        tax_base = defaultdict(float)
        if e.get("tax_base_by_article") is not None:
            for mon, article, value in e["tax_base_by_article"]:
                if mon in months:
                    tax_base[(mon, art_key(article))] += value
        else:
            tax_base.update(revenue)
        for (mon, article), value in tax_base.items():
            if rate and value:
                add(e, mon, article, TAX, f"Налог {rate:g}% с фактической цены покупки", -value * rate / 100.0)
        qty = defaultdict(lambda: [0.0, 0.0])
        for mon, _day, _order, article, q, _rev in e.get("orders") or []:
            if mon in months and q:
                qty[(mon, art_key(article))][0 if q > 0 else 1] += q
        for (mon, article), (sold, returned) in qty.items():
            if sold:
                add(e, mon, article, QTY_SOLD, "Количество по заказам", 0.0, round(sold, 3))
            if returned:
                add(e, mon, article, QTY_RETURNED, "Количество по заказам", 0.0, round(returned, 3))
        for mon, article, q, _rev in e.get("sales") or []:
            if mon not in months:
                continue
            for source, statya, label in ((costs, COGS, "cost.csv: сс_за_ед"), (preps, PREP, "cost.csv: подготовка_за_ед")):
                found = cost_for(source, article, mon)
                if found:
                    mult, price = found
                    add(e, mon, article, statya, label, -q * mult * price)
            pct = retro_for(retro, (catalog or {}).get(art_key(article), ("", ""))[0], mon) if retro else None
            found = cost_for(costs, article, mon) if pct else None
            if found:
                mult, price = found
                add(e, mon, article, RETRO, f"{RETRO_FILE}: {pct:g}% бренда от себестоимости", q * mult * price * pct / 100.0)
    rows.sort(key=lambda r: (r[0], r[1], r[2], r[3] == NO_ARTICLE, r[3]))
    return rows


# Заказы и остатки: в реестр (и в прибыль) не входят — отдельные колонки юнит-экономики и листы «Заказы», «Остатки»
ORD_QTY, ORD_SUM, ORD_CANCEL = "Заказано, шт", "Заказано, ₽", "Отменено, шт"
ORD_PER_DAY = "Заказов в день, шт"
ST_MP, ST_OWN, ST_TO_CLIENT, ST_RETURNING = ("На складах МП, шт", "На своём складе, шт", "В пути к покупателям, шт",
                                             "Возвраты в пути, шт")
ST_TOTAL, ST_COST, ST_DAYS = "Остаток всего, шт", "Остаток по себестоимости, ₽", "Запас, дней"
STOCK_KINDS = {"mp": ST_MP, "own": ST_OWN, "to_client": ST_TO_CLIENT, "returning": ST_RETURNING}
# Воронка продаж (аналитика маркетплейсов); CTR и конверсии считаются из сумм, поэтому верны и в итогах
F_SHOWS, F_CLICKS, F_CTR, F_CART = "Показы", "Переходы в карточку", "CTR, %", "В корзину, шт"
F_CART_CONV, F_ORDERS, F_ORDER_CONV = "Карточка → корзина, %", "Заказано (воронка), шт", "Корзина → заказ, %"
F_CARD_ORDER, F_THROUGH = "Карточка → заказ, %", "Сквозная конверсия (показ → заказ), %"
F_BUYOUT, F_BUYOUT_PCT, F_BUYOUT_DONE = "Выкуплено (воронка), шт", "Выкуп, % заказов", "Выкуп по завершённым, %"
F_CANCEL_PCT, F_RETURN_PCT = "Отмены, % заказов", "Возвраты, % продаж"
# Скрытые колонки для итогов: конверсия считается только по строкам, где маркетплейс даёт оба показателя
# (у WB нет показов, у Ozon без Premium — показов и корзины)
F_CLICKS_CTR = "Переходы при известных показах (для CTR)"
F_ORDERS_SHOWS = "Заказано при известных показах (для сквозной конверсии)"
F_CLICKS_CART = "Переходы при известной корзине (для конверсии в корзину)"
F_ORDERS_CART = "Заказано при известной корзине (для конверсии в заказ)"
F_DONE = "Завершённые заказы: выкуплено + отменено + возвращено (воронка)"
ORDERS_HEADER = ["Маркетплейс", "Кабинет", "Месяц", "Артикул", "Название", "Заказано, шт", "Заказано, ₽",
                 "Отменено, шт", "Отменено, ₽", "Заказано без отмен, шт", "Заказано без отмен, ₽"]
# Текущие цены на маркетплейсе (на дату загрузки). У артикула может быть несколько цен (размеры WB, несколько
# кабинетов в итоге «все МП») — показывается средняя; скрытые колонки-помощники: сумма цен и число цен
PR_BASE, PR_DISC, PR_PRICE, PR_PROMO = "Цена до скидки, ₽", "Скидка, %", "Текущая цена, ₽", "Цена с акциями, ₽"
PR_BASE_S, PR_BASE_N = "Сумма цен до скидки (для средней)", "Число цен до скидки (для средней)"
PR_PRICE_S, PR_PRICE_N = "Сумма текущих цен (для средней)", "Число текущих цен (для средней)"
PR_PROMO_S, PR_PROMO_N = "Сумма цен с акциями (для средней)", "Число цен с акциями (для средней)"
PR_DISC_S = "Сумма текущих цен при известной цене до скидки (для скидки)"
PRICE_FIELDS = {PR_BASE_S, PR_BASE_N, PR_PRICE_S, PR_PRICE_N, PR_PROMO_S, PR_PROMO_N, PR_DISC_S}
# Кратность продажи, указанная в карточке товара на маркетплейсе (сколько единиц в одном товаре: набор 15 паучей — 15).
# Не суммируется: в итогах по нескольким кабинетам — наибольшая
MP_MULT = "Кратность на МП, шт"
ATTACH_ONLY = PRICE_FIELDS | {MP_MULT}   # такие поля строку юнит-экономики не создают — только дополняют
MULT_HEADER = ["Маркетплейс", "Кабинет", "Артикул", "Название", "Кратность на МП, шт", "Характеристика на МП",
               "Кратность в cost.csv", "Совпадает", "Данные на"]
PRICES_HEADER = ["Маркетплейс", "Кабинет", "Артикул", "Название", "Цена до скидки, ₽", "Скидка, %",
                 "Текущая цена, ₽", "Цена с акциями, ₽", "Себестоимость + подготовка за шт", "Данные на"]
STOCKS_HEADER = ["Маркетплейс", "Кабинет", "Артикул", "Название", "Склад", "Где товар", "Остаток, шт",
                 "Себестоимость за шт", "Остаток по себестоимости, ₽", "Данные на"]


def month_bounds(months):
    """Первый и последний день периода юнит-экономики (последний — не позже сегодняшнего)."""
    first, last = min(months)[:7], max(months)[:7]
    y, m = map(int, last.split("-"))
    end = date(y + (m == 12), 1 if m == 12 else m + 1, 1) - timedelta(days=1)
    return date.fromisoformat(first + "-01"), min(end, date.today())


def goods_extras(entities, months, costs, names, preps=None):
    """Заказы за месяцы юнит-экономики и текущие остатки.
    Возвращает ({(МП, кабинет, артикул): {показатель: значение}}, строки листов «Заказы», «Остатки», «Цены»,
    «Кратность»).
    «Заказов в день» — заказы без отмен / дни периода, за которые у маркетплейса есть данные о заказах."""
    extra = defaultdict(lambda: defaultdict(float))
    order_rows, stock_rows, price_rows, mult_rows = [], [], [], []
    start, end = month_bounds(months)
    today_month = f"{date.today():%Y-%m}"
    for e in entities:
        mp, cab = e["mp"], e["cabinet"]
        if e.get("ordered") is not None:
            since = max(start, date.fromisoformat(e["ordered_from"][:10])) if e.get("ordered_from") else start
            days = max((end - since).days + 1, 0)
            agg = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
            for mon, article, qty, amount, c_qty, c_amount in e["ordered"]:
                if mon in months:
                    row = agg[(mon, art_key(article) or NO_ARTICLE)]
                    for i, v in enumerate((qty, amount, c_qty, c_amount)):
                        row[i] += v
            for (mon, article), (qty, amount, c_qty, c_amount) in sorted(agg.items()):
                x = extra[(mp, cab, article)]
                x[ORD_QTY] += qty
                x[ORD_SUM] += amount
                x[ORD_CANCEL] += c_qty
                if days:
                    x[ORD_PER_DAY] += (qty - c_qty) / days
                order_rows.append([mp, cab, mon, article, names.get(article, ""), qty, round(amount, 2), c_qty,
                                   round(c_amount, 2), qty - c_qty, round(amount - c_amount, 2)])
        for article, place, kind, qty in e.get("stocks") or []:
            article = art_key(article) or NO_ARTICLE
            column = STOCK_KINDS.get(kind, ST_MP)
            found = cost_for(costs, article, today_month)
            unit_cost = found[0] * found[1] if found else None
            x = extra[(mp, cab, article)]
            x[column] += qty
            if column in (ST_MP, ST_OWN):
                x[ST_TOTAL] += qty
                x[ST_COST] += qty * (unit_cost or 0.0)
            stock_rows.append([mp, cab, article, names.get(article, ""), place, column.replace(", шт", ""), qty,
                               unit_cost, round(qty * unit_cost, 2) if unit_cost else None, e.get("stocks_at") or ""])
        for article, base, price, promo in e.get("prices") or []:
            article = art_key(article) or NO_ARTICLE
            x = extra[(mp, cab, article)]
            x[PR_PRICE_S] += price
            x[PR_PRICE_N] += 1
            if base:
                x[PR_BASE_S] += base
                x[PR_BASE_N] += 1
                x[PR_DISC_S] += price
            if promo:
                x[PR_PROMO_S] += promo
                x[PR_PROMO_N] += 1
            found = cost_for(costs, article, today_month)
            prep = cost_for(preps, article, today_month) if preps else None
            unit_cost = (found[0] * found[1] if found else 0.0) + (prep[0] * prep[1] if prep else 0.0)
            price_rows.append([mp, cab, article, names.get(article, ""), base,
                               1 - price / base if base else None, price, promo, unit_cost or None,
                               e.get("prices_at") or ""])
        for article, qty, source in e.get("multiples") or []:
            article = art_key(article) or NO_ARTICLE
            x = extra[(mp, cab, article)]
            x[MP_MULT] = max(x.get(MP_MULT, 0.0), qty)
            found = cost_for(costs, article, today_month)
            own = found[0] if found else None
            mult_rows.append([mp, cab, article, names.get(article, ""), qty, source, own,
                              "" if own is None else ("да" if abs(own - qty) < 1e-9 else "НЕТ"),
                              e.get("multiples_at") or ""])
    return extra, order_rows, stock_rows, price_rows, mult_rows


def funnel_extras(entities, months, names):
    """Воронка за месяцы юнит-экономики: ({(МП, кабинет, артикул): {показатель: значение}}, строки листа «Воронка»).
    Строка воронки из скрипта: [месяц, артикул, показы, переходы, корзина, заказано шт, заказано ₽, выкуплено,
    отменено, возвращено]; None — маркетплейс этот показатель не отдаёт."""
    extra = defaultdict(lambda: defaultdict(float))
    rows = []
    for e in entities:
        mp, cab = e["mp"], e["cabinet"]
        agg = {}
        for mon, article, *values in e.get("funnel") or []:
            if mon not in months or not any(values):
                continue
            key = (mon, art_key(article) or NO_ARTICLE)
            have = agg.setdefault(key, [None] * len(values))
            for i, v in enumerate(values):
                if v is not None:
                    have[i] = (have[i] or 0.0) + v
        for (mon, article), (shows, clicks, cart, orders, amount, buyout, cancel, returned) in sorted(agg.items()):
            x = extra[(mp, cab, article)]
            for field, v in ((F_SHOWS, shows), (F_CLICKS, clicks), (F_CART, cart), (F_ORDERS, orders),
                             (F_BUYOUT, buyout)):
                x[field] += v or 0.0
            if shows is not None:
                x[F_CLICKS_CTR] += clicks or 0.0
                x[F_ORDERS_SHOWS] += orders or 0.0
            if cart is not None:
                x[F_CLICKS_CART] += clicks or 0.0
                x[F_ORDERS_CART] += orders or 0.0
            x[F_DONE] += (buyout or 0.0) + (cancel or 0.0) + (returned or 0.0)
            rows.append([mp, cab, mon, article, names.get(article, ""), shows, clicks, cart, orders,
                         round(amount, 2) if amount is not None else None, buyout, cancel, returned])
    return extra, rows


# ---------------------------------------------------------------------------
# Ход сборки в консоли
# ---------------------------------------------------------------------------
_STARTED = time.monotonic()


def step(text):
    """Сообщение об этапе сборки с временем от начала запуска."""
    elapsed = int(time.monotonic() - _STARTED)
    print(f"[{elapsed // 60:02d}:{elapsed % 60:02d}] {text}", flush=True)


class Counter:
    """Счётчик обработки в одной строке: «обработано N из M (x%), осталось ~…»."""

    def __init__(self, title, total):
        self.title, self.total, self.done = title, total, 0
        self.start = self.last = time.monotonic()

    def tick(self, n=1):
        self.done += n
        now = time.monotonic()
        if now - self.last >= 0.5 or self.done >= self.total:
            self.last = now
            self._show(now)

    def _show(self, now):
        share = self.done / self.total if self.total else 1
        left = ""
        if 0 < share < 1 and now - self.start > 1:
            sec = int((now - self.start) * (1 - share) / share)
            left = f", осталось ~{sec // 60} мин {sec % 60:02d} с" if sec >= 60 else f", осталось ~{sec} с"
        done, total = (f"{n:,}".replace(",", " ") for n in (self.done, self.total))
        print(f"\r    {self.title}: {done} из {total} ({share:.0%}){left}" + " " * 10, end="", flush=True)

    def finish(self):
        self.done = self.total
        self._show(time.monotonic())
        print(f" — за {time.monotonic() - self.start:.0f} с", flush=True)


# ---------------------------------------------------------------------------
# Запуск скриптов маркетплейсов
# ---------------------------------------------------------------------------
def enabled_accounts(mp):
    """Имена активных кабинетов из конфига маркетплейса."""
    try:
        cfg = json.loads((HERE / mp["config"]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(a.get("name") or a.get("client_id") or "") for a in cfg.get("accounts") or []
            if a.get("enabled", True)]


# Строки вывода скриптов, которые попадают в итоговую сводку ошибок
ERROR_LINE = re.compile(r"ОШИБКА|не получен|не удалось|пропущен|Traceback|Error:", re.I)
CABINET_LINE = re.compile(r"^\s*\[([^\]]+)\]")


RELOAD = False   # --reload: WB загружает все недели заново, а не только недостающие


def script_cmd(mp, dump, from_cache, grain="month", to_current=None, config=None):
    """Команда запуска скрипта маркетплейса. to_current — загрузка до текущего месяца (по умолчанию — когда
    данные берутся из API); кэш скриптов привязан к периоду загрузки, поэтому повторный запуск из кэша
    должен получить тот же --to, что и запуск, который этот кэш записал."""
    script = HERE / mp["script"]
    cmd = [sys.executable, str(script), "--dump", str(dump), "--no-excel"]
    if config:
        cmd += ["--config", str(config)]
    if grain != "month":
        cmd += ["--grain", "day"]   # недели и дни свод собирает из дневной выгрузки
    if from_cache:
        cmd.append("--from-cache")
    elif RELOAD and mp["key"] == "wb":
        cmd.append("--reload")   # WB по умолчанию догружает только новые недели к кэшу
    if to_current is None:
        # WB OPS: и из кэша тоже до текущего месяца. Иначе при period.to в конфиге раньше текущего месяца
        # «Собрать из кэша» отрезал последние месяцы (строки есть в кэше, но «вне периода»). Кэш с другой
        # датой конца находится запасным поиском скриптов по дате начала.
        to_current = True
    if to_current:
        # Загрузка всегда до текущего месяца, даже если в конфиге period.to стоит раньше
        cmd += ["--to", f"{date.today():%Y-%m}"]
    return cmd


def run_script(mp, dump, from_cache, grain="month", config=None):
    """Запускает скрипт маркетплейса, показывает его вывод и собирает строки с ошибками."""
    script = HERE / mp["script"]
    cmd = script_cmd(mp, dump, from_cache, grain, config=config)
    print(f"\n========== {mp['name']}{' (из кэша)' if from_cache else ''} ==========\n"
          f"> {' '.join(Path(c).name if i < 2 else c for i, c in enumerate(cmd))}", flush=True)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    errors, cabinet = [], ""
    proc = subprocess.Popen(cmd, cwd=script.parent, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    for line in proc.stdout:
        print(line, end="", flush=True)
        m = CABINET_LINE.match(line)
        if m:
            cabinet = m.group(1)
        if ERROR_LINE.search(line):
            text = line.strip()
            errors.append(f"{cabinet}: {text}" if cabinet and not text.startswith("[") else text)
    proc.wait()
    data = None
    if dump.exists():
        try:
            data = json.loads(dump.read_text(encoding="utf-8"))
        except ValueError as e:
            errors.append(f"выгрузка повреждена: {e}")
    return data, proc.returncode, errors


def merge_cabinets(data, extra, mark):
    """Добавляет в выгрузку data кабинеты из extra, которых в ней нет; помечает их на листе «Контроль».
    Возвращает (выгрузка, список добавленных кабинетов)."""
    added = []
    for cab, c in ((extra or {}).get("cabinets") or {}).items():
        if data and cab in data["cabinets"]:
            continue
        c["checks"] = [[mark, "да"]] + (c.get("checks") or [])
        if data is None:
            data = {**extra, "cabinets": {}}
        else:
            data["months"] = sorted(set(data["months"]) | set(extra["months"]))
        data["cabinets"][cab] = c
        added.append(cab)
    return data, added


def missing_config(mp, names):
    """Конфиг маркетплейса только с перечисленными кабинетами (период и ставка налога — как в исходном)."""
    cfg = json.loads((HERE / mp["config"]).read_text(encoding="utf-8"))
    cfg["accounts"] = [a for a in cfg.get("accounts") or []
                       if a.get("enabled", True) and str(a.get("name") or a.get("client_id") or "") in names]
    return cfg


def run_marketplace(mp, args, tmpdir):
    """(данные, пометка для «Контроля», строки ошибок из вывода скрипта)."""
    if not (HERE / mp["script"]).exists():
        return None, f"нет скрипта {mp['script']}", []
    if not (HERE / mp["config"]).exists():
        return None, f"нет конфига {mp['config']} — маркетплейс не настроен", []
    # Период загрузки задают конфиги маркетплейсов (там же ключи кэша); месяцы для свода отбирает оркестратор.
    cache_for = {k.strip().lower() for k in (args.cache_for or "").split(",") if k.strip()}
    from_cache = args.from_cache or mp["key"] in cache_for
    accounts = enabled_accounts(mp)
    data, code, errors = run_script(mp, Path(tmpdir) / f"{mp['key']}.json", from_cache, args.grain)
    notes = [] if code == 0 else [f"скрипт завершился с кодом {code}"]
    got = len((data or {}).get("cabinets") or {})
    if not from_cache and got < len(accounts):
        # API ответил не по всем кабинетам (VPN, сбой сети, лимиты) — недостающие берём из кэша,
        # чтобы отчёт собрался целиком, и помечаем их на листе «Контроль».
        print(f"\n  {mp['name']}: из API получено кабинетов {got} из {len(accounts)} — "
              "недостающие беру из кэша", flush=True)
        cached, _, cache_errors = run_script(mp, Path(tmpdir) / f"{mp['key']}_cache.json", True, args.grain)
        errors += [f"(кэш) {e}" for e in cache_errors]
        data, restored = merge_cabinets(data, cached, "Данные из кэша: API не ответил при обновлении")
        if restored:
            notes.insert(0, "API не ответил, из кэша: " + ", ".join(restored))
    elif from_cache and args.fill_missing:
        missing = [a for a in accounts if a and a not in ((data or {}).get("cabinets") or {})]
        if missing:
            # Кэша по кабинету нет (новый кабинет, первая сборка на этом компьютере) — загружаем его из API.
            # Скрипту отдаём копию конфига только с этими кабинетами: кэш остальных не трогаем.
            print(f"\n  {mp['name']}: нет в кэше кабинетов {len(missing)} из {len(accounts)} — "
                  "загружаю из API: " + ", ".join(missing), flush=True)
            config = Path(tmpdir) / f"{mp['key']}_missing.json"
            config.write_text(json.dumps(missing_config(mp, missing), ensure_ascii=False), encoding="utf-8")
            fresh, _, fresh_errors = run_script(mp, Path(tmpdir) / f"{mp['key']}_api.json", False, args.grain,
                                                config=config)
            data, loaded = merge_cabinets(data, fresh, "Данные из API: кабинета не было в кэше")
            # Ошибки «нет кэша» по догруженным кабинетам больше не актуальны
            errors = [e for e in errors if not any(cab in e for cab in loaded)] + [f"(API) {e}" for e in fresh_errors]
            if loaded:
                notes.insert(0, "не было в кэше, загружено из API: " + ", ".join(loaded))
                if code != 0 and not [a for a in accounts if a and a not in data["cabinets"]]:
                    notes = [n for n in notes if not n.startswith("скрипт завершился")]
    lost = [a for a in accounts if a and a not in ((data or {}).get("cabinets") or {})]
    if data is None:
        notes.append("нет данных (подробности в выводе выше)")
    elif lost:
        notes.append("нет в отчёте кабинетов: " + ", ".join(lost))
    return data, "; ".join(notes), list(dict.fromkeys(errors))


# ---------------------------------------------------------------------------
# Сборка единых таблиц
# ---------------------------------------------------------------------------
def build(dumps):
    """entities: список (маркетплейс, кабинет, {статья: {исходная строка: {месяц: сумма}}}, net, checks)."""
    entities, mapping = [], defaultdict(float)
    for data in dumps:
        mp, info_group = data["marketplace"], data.get("info_group", "Справочно")
        for cab, c in data["cabinets"].items():
            table = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
            info = defaultdict(lambda: defaultdict(float))
            for group, label, vals in c["rows"]:
                article, guessed = map_row(mp, group, label, info_group)
                total = sum(vals.values())
                if article is None:
                    for m, v in vals.items():
                        info[label][m] += v
                    continue
                mapping[(mp, group, label, article, guessed)] += total
                for m, v in vals.items():
                    table[article][label][m] += v
            entities.append({"mp": mp, "cabinet": cab, "table": table, "info": info,
                             "net": c.get("net") or {}, "checks": c.get("checks") or [],
                             "sales": c.get("sales") or [], "orders": c.get("orders") or [],
                             "by_article": c.get("by_article"), "info_group": info_group,
                             "tax_base": c.get("tax_base"),
                             "tax_base_by_article": c.get("tax_base_by_article"),
                             "catalog": c.get("catalog") or [],
                             # Заказы и остатки для юнит-экономики (None — скрипт МП их не выгрузил)
                             "ordered": c.get("ordered"), "ordered_from": c.get("ordered_from"),
                             "stocks": c.get("stocks"), "stocks_at": c.get("stocks_at"),
                             "prices": c.get("prices"), "prices_at": c.get("prices_at"),
                             "multiples": c.get("multiples"), "multiples_at": c.get("multiples_at"),
                             "funnel": c.get("funnel"), "funnel_from": c.get("funnel_from")})
    return entities, mapping


def merge_tables(tables, detail_key=None):
    """Складывает таблицы; detail_key(i) — префикс исходной строки (например, название МП)."""
    out = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    for i, t in enumerate(tables):
        prefix = detail_key(i) if detail_key else None
        for article, rows in t.items():
            for label, vals in rows.items():
                key = prefix if prefix else label
                for m, v in vals.items():
                    out[article][key][m] += v
    return out


def article_totals(table, months):
    return {a: {m: sum(rows.get(m, 0.0) for rows in table.get(a, {}).values()) for m in months}
            for a in REVENUE_ARTICLES + EXPENSE_ARTICLES + [COMP, ADVANCE]}


# ---------------------------------------------------------------------------
# Прогноз на конец текущего периода (месяца или недели)
# ---------------------------------------------------------------------------
FORECAST_WINDOW = {"month": 14, "week": 7}   # темп считается не меньше чем за столько последних дней
FORECAST_DAYS_BACK = 45                       # сколько дней дневной выгрузки хранить для прогноза
FORECAST_KEYS = ("revenue", "net", "cogs", "retro", "prep", "tax")


def day_slice(data, first_day):
    """Дневная выгрузка МП, урезанная до строк ОПиУ и продаж с first_day — для прогноза больше не нужно.
    Копия: regroup_dump потом сворачивает исходную выгрузку в недели."""
    cabinets = {}
    for cab, c in data.get("cabinets", {}).items():
        cabinets[cab] = {"rows": [[g, lbl, {d: v for d, v in (vals or {}).items() if d >= first_day}]
                                  for g, lbl, vals in c.get("rows") or []],
                         "sales": [list(s) for s in c.get("sales") or [] if s[0] >= first_day]}
        if c.get("tax_base") is not None:   # налог в прогнозе — с той же базы, что и в ОПиУ
            cabinets[cab]["tax_base"] = {d: v for d, v in c["tax_base"].items() if d >= first_day}
    return {"marketplace": data["marketplace"], "info_group": data.get("info_group", "Справочно"),
            "months": [d for d in data.get("months") or [] if d >= first_day], "cabinets": cabinets}


def load_daily_dumps(args, first_day):
    """Дневные выгрузки МП из кэша для прогноза в отчёте по месяцам. Кэш только что обновлён основным
    запуском, поэтому к API повторно не обращаемся; вывод скриптов не показываем — он уже был выше."""
    only = {s.strip().lower() for s in args.only.split(",")} if args.only else None
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    dumps = []
    with tempfile.TemporaryDirectory() as tmp:
        for mp in MARKETPLACES:
            script = HERE / mp["script"]
            if (only and mp["key"] not in only) or not script.exists() or not (HERE / mp["config"]).exists():
                continue
            dump = Path(tmp) / f"{mp['key']}_day.json"
            cmd = script_cmd(mp, dump, True, "day",
                             to_current=True)
            subprocess.run(cmd, cwd=script.parent, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                data = json.loads(dump.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                print(f"  {mp['name']}: дневные данные для прогноза не получены — прогноз без этого маркетплейса")
                continue
            if data.get("cabinets"):
                dumps.append(day_slice(data, first_day))
    return dumps


def forecast_series(day_dumps, costs, preps, rates, retro=None, catalog=None):
    """(мп, кабинет) -> {показатель: {день: сумма}, "last": последний день с данными маркетплейса}.
    Себестоимость, подготовка и налог считаются по дням теми же функциями, что и в ОПиУ.
    Последний день — по всем кабинетам МП: у маленького кабинета день без продаж — это ноль, а не нет данных."""
    entities, _ = build(day_dumps)
    days = sorted({d for data in day_dumps for d in data["months"]})
    cogs, prep = apply_costs(entities, costs, preps, set(days))[:2]
    bonus = apply_retro(entities, costs, retro or {}, catalog or {}, set(days))
    tax = apply_tax(entities, rates, set(days))[0]
    series, last = {}, {}
    for e in entities:
        key = (e["mp"], e["cabinet"])
        tot = article_totals(e["table"], days)
        seen = [d for rows in e["table"].values() for vals in rows.values() for d, v in vals.items() if abs(v) >= 0.5]
        if seen:
            last[e["mp"]] = max(last.get(e["mp"], ""), max(seen))
        series[key] = {"revenue": sum_vals([tot[a] for a in REVENUE_ARTICLES], days),
                       "net": sum_vals(list(tot.values()), days),
                       "cogs": dict(cogs.get(key, {})), "retro": dict(bonus.get(key, {})),
                       "prep": dict(prep.get(key, {})), "tax": dict(tax.get(key, {}))}
    for (mp, _cab), s in series.items():
        s["last"] = date.fromisoformat(last[mp]) if mp in last else None
    return series


def forecast_entity(s, start, end, window):
    """Факт с начала периода по день отсечки + среднедневной темп × оставшиеся дни периода.
    Отсечка — последний день с данными кабинета, но не позже вчерашнего: сегодняшний день ещё не полный.
    Темп — среднее за дни с начала периода, но не меньше чем за window последних дней (в начале периода
    дней мало, и темп по ним случаен — тогда добирается конец прошлого периода)."""
    cutoff = min(end, date.today() - timedelta(days=1), s["last"] or start)
    cutoff = max(cutoff, start - timedelta(days=1))
    span = max((cutoff - start).days + 1, window)
    first, last, win = start.isoformat(), cutoff.isoformat(), (cutoff - timedelta(days=span - 1)).isoformat()
    left = (end - cutoff).days
    out = {}
    for k in FORECAST_KEYS:
        fact = sum(v for d, v in s[k].items() if first <= d <= last)
        rate = sum(v for d, v in s[k].items() if win <= d <= last) / span
        out[k] = fact + rate * left
    return out, cutoff


def period_forecast(series, keys, start, end, window):
    """Прогноз по набору кабинетов: суммы показателей, прибыль, маржинальность и пояснение для листа."""
    vals, cutoffs = dict.fromkeys(FORECAST_KEYS, 0.0), defaultdict(list)
    for key in keys:
        if key not in series:
            continue
        fc, cutoff = forecast_entity(series[key], start, end, window)
        for k in FORECAST_KEYS:
            vals[k] += fc[k]
        if any(abs(fc[k]) >= 0.5 for k in FORECAST_KEYS):
            if key[0] not in cutoffs[cutoff]:
                cutoffs[cutoff].append(key[0])
    if not cutoffs:
        return None
    vals["gross"] = vals["net"] + vals["cogs"] + vals["retro"]
    vals["profit"] = vals["gross"] + vals["prep"] + vals["tax"]
    vals["margin"] = vals["profit"] / vals["revenue"] if vals["revenue"] else 0.0
    unit = "месяца" if window == FORECAST_WINDOW["month"] else "недели"
    if len(cutoffs) == 1:
        facts = f"факт по {next(iter(cutoffs)):%d.%m}"
    else:
        facts = "факт по " + "; ".join(f"{d:%d.%m} — {', '.join(n)}" for d, n in sorted(cutoffs.items(), reverse=True))
    note = (f"Прогноз на {end:%d.%m.%Y} (до конца {unit}): {facts} + среднедневной темп × оставшиеся дни. "
            f"Темп — среднее с начала {unit}, но не меньше чем за {window} последних дней. "
            "Себестоимость, подготовка и налог — по тем же правилам, что и в факте.")
    return {"title": f"Прогноз на {end:%d.%m}", "note": note, "values": vals}


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------
NUM = '#,##0;[Red]-#,##0;"–"'
PCT = '0.0%;[Red]-0.0%;"–"'
F_HEADER = PatternFill("solid", fgColor="305496")
F_SECTION = PatternFill("solid", fgColor="D9E1F2")
F_ARTICLE = PatternFill("solid", fgColor="F2F2F2")
F_TOTAL = PatternFill("solid", fgColor="FFF2CC")
F_RESULT = PatternFill("solid", fgColor="E2EFDA")
F_WARN = PatternFill("solid", fgColor="FCE4D6")
WHITE_BOLD = Font(bold=True, color="FFFFFF")
BOLD = Font(bold=True)
MONTHS_RU = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]


def month_title(key):
    y, m = key.split("-")[:2]
    return f"{MONTHS_RU[int(m) - 1]} {y}"


WEEKDAYS_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
GRAIN = "month"   # разрез колонок отчёта, задаётся ключом --grain в main()


def period_title(key, grain="month"):
    """Подпись колонки: месяц «Сен 2026», неделя «14–20.09», день «15.09 пн»."""
    if grain == "month":
        return month_title(key)
    day = date.fromisoformat(key)
    if grain == "unitweek":
        return f"{day:%d.%m}–{week_part_end(key):%d.%m}"
    if grain == "day":
        return f"{day:%d.%m} {WEEKDAYS_RU[day.weekday()]}"
    return f"{day:%d.%m}–{day + timedelta(days=6):%d.%m}"


def period_range(keys, grain="month"):
    """Период отчёта одной строкой для подзаголовка листа."""
    if grain == "month":
        return f"{month_title(keys[0])} — {month_title(keys[-1])}"
    first, last = date.fromisoformat(keys[0]), date.fromisoformat(keys[-1])
    if grain == "week":
        last += timedelta(days=6)
    if grain == "unitweek":
        last = week_part_end(keys[-1])
    return f"{first:%d.%m.%Y} — {last:%d.%m.%Y}"


def week_key(day):
    """Понедельник недели, в которую попал день, — ключ колонки недельного отчёта."""
    d = date.fromisoformat(day)
    return (d - timedelta(days=d.weekday())).isoformat()


def week_part(day):
    """Неделя, разрезанная по границе месяца: первый день части недели внутри месяца.
    Неделя 28.09–04.10 даёт две части: 2026-09-28 (28–30.09) и 2026-10-01 (01–04.10) —
    так у каждой строки понедельной юнитки один год и один месяц, а суммы месяца сходятся с месячной юниткой."""
    monday = week_key(day)
    return max(monday, str(day)[:8] + "01")


def week_part_end(key):
    """Последний день части недели (воскресенье или последний день месяца)."""
    d = date.fromisoformat(key)
    month_end = date(d.year + (d.month == 12), 1 if d.month == 12 else d.month + 1, 1) - timedelta(days=1)
    return min(d - timedelta(days=d.weekday()) + timedelta(days=6), month_end)


def regroup_dump(data, grain, month=None):
    """Выгрузка маркетплейса приходит по дням (--grain day) — сворачиваем её в недели
    или оставляем дни выбранного месяца. Ключи периода меняются везде: строки ОПиУ, итоги,
    продажи, заказы и строки по артикулам."""
    def key_of(day):
        if grain == "day":
            return day if str(day)[:7] == month else None
        if grain == "unitweek":
            return week_part(day)
        return week_key(day)

    def fold(vals):
        out = defaultdict(float)
        for day, value in (vals or {}).items():
            key = key_of(day)
            if key:
                out[key] += value
        return dict(out)

    def refold(items):
        """Списки, у которых первый элемент — период: продажи, заказы, строки по артикулам.
        Одинаковые ключи не склеиваем — потребители всё равно суммируют."""
        rows = []
        for item in items or []:
            key = key_of(item[0])
            if key:
                rows.append([key] + list(item[1:]))
        return rows

    data["months"] = sorted({k for k in (key_of(m) for m in data.get("months") or []) if k})
    for c in data.get("cabinets", {}).values():
        c["rows"] = [[g, lbl, folded] for g, lbl, vals in c.get("rows") or []
                     for folded in [fold(vals)] if folded]
        c["net"] = fold(c.get("net"))
        if c.get("tax_base") is not None:
            c["tax_base"] = fold(c["tax_base"])
        for field in ("sales", "orders", "by_article", "ordered", "tax_base_by_article"):
            if c.get(field) is not None:
                c[field] = refold(c[field])
    return data


def in_period(key, grain, date_from, date_to):
    """Попадает ли период в границы свода (границы задаются месяцами)."""
    months = {key[:7]}
    if grain == "week":
        start = date.fromisoformat(key)
        months = {(start + timedelta(days=i)).strftime("%Y-%m") for i in range(7)}
    return any(m >= date_from and (not date_to or m <= date_to) for m in months)


COL_TOTAL, COL_PCT, COL_MONTH0 = 2, 3, 4   # «Итого» и «% выручки» — сразу после названия статьи
F_TOTAL_HEAD = PatternFill("solid", fgColor="C65911")
F_TOTAL_COL = PatternFill("solid", fgColor="F8CBAD")
F_PCT_COL = PatternFill("solid", fgColor="FCE4D6")
F_TOTAL_COL_SECTION = PatternFill("solid", fgColor="F4B183")
F_FORECAST_HEAD = PatternFill("solid", fgColor="7030A0")
F_FORECAST = PatternFill("solid", fgColor="E4DFEC")


class Sheet:
    def __init__(self, ws, months, title, subtitle="", forecast=None):
        self.ws, self.months, self.row = ws, months, 1
        self.last = COL_MONTH0 + len(months) - 1
        self.fc_col = self.last + 1 if forecast else None   # колонка прогноза — сразу за последним периодом
        ws.cell(1, 1, title).font = Font(bold=True, size=14)
        if subtitle:
            ws.cell(2, 1, subtitle).font = Font(italic=True, color="666666")
        if forecast:
            ws.cell(3, 1, forecast["note"]).font = Font(italic=True, color="7030A0")
            cell = ws.cell(4, self.fc_col, forecast["title"])
            cell.font, cell.fill = WHITE_BOLD, F_FORECAST_HEAD
            cell.alignment = Alignment(horizontal="center", wrap_text=True)
            ws.column_dimensions[get_column_letter(self.fc_col)].width = 14
        self.row = 4
        headers = {1: "Статья", COL_TOTAL: "Итого", COL_PCT: "% выручки"}
        headers.update({COL_MONTH0 + i: period_title(m, GRAIN) for i, m in enumerate(months)})
        for c, v in headers.items():
            cell = ws.cell(self.row, c, v)
            cell.font = WHITE_BOLD
            cell.fill = F_TOTAL_HEAD if c in (COL_TOTAL, COL_PCT) else F_HEADER
            cell.alignment = Alignment(horizontal="center", wrap_text=True)
        ws.freeze_panes = ws.cell(self.row + 1, COL_MONTH0)
        ws.column_dimensions["A"].width = 58
        ws.column_dimensions[get_column_letter(COL_TOTAL)].width = 15
        ws.column_dimensions[get_column_letter(COL_PCT)].width = 10
        width = {"month": 13, "week": 12, "day": 11, "unitweek": 12}[GRAIN]
        for c in range(COL_MONTH0, self.last + 1):
            ws.column_dimensions[get_column_letter(c)].width = width
        self.row += 1
        self.revenue_total = 0.0

    def _paint_total_cols(self, row, section=False):
        self.ws.cell(row, COL_TOTAL).fill = F_TOTAL_COL_SECTION if section else F_TOTAL_COL
        self.ws.cell(row, COL_PCT).fill = F_TOTAL_COL_SECTION if section else F_PCT_COL

    def line(self, label, vals, fill=None, font=None, indent=0, pct=True, fmt=NUM, total=None):
        ws = self.ws
        ws.cell(self.row, 1, label).alignment = Alignment(indent=indent)
        if total is None:
            total = sum(vals.get(m, 0.0) for m in self.months)
        for i, m in enumerate(self.months):
            ws.cell(self.row, COL_MONTH0 + i, round(vals.get(m, 0.0), 8 if fmt == PCT else 2)).number_format = fmt
        ws.cell(self.row, COL_TOTAL, round(total, 8 if fmt == PCT else 2)).number_format = fmt
        if pct and self.revenue_total and fmt == NUM:
            ws.cell(self.row, COL_PCT, total / self.revenue_total).number_format = PCT
        for c in range(1, self.last + 1):
            if fill:
                ws.cell(self.row, c).fill = fill
            if font:
                ws.cell(self.row, c).font = font
        self._paint_total_cols(self.row)
        ws.cell(self.row, COL_TOTAL).font = Font(bold=True, size=font.size if font and font.size else 11)
        self.row += 1
        return total

    def section(self, title):
        for c in range(1, self.last + 1):
            self.ws.cell(self.row, c).fill = F_SECTION
            self.ws.cell(self.row, c).font = BOLD
        self._paint_total_cols(self.row, section=True)
        self.ws.cell(self.row, 1, title)
        self.row += 1

    def blank(self):
        self.row += 1

    def forecast(self, value, fmt=NUM):
        """Прогноз в строке, которую только что вывел line()."""
        if self.fc_col is None or value is None:
            return
        cell = self.ws.cell(self.row - 1, self.fc_col, round(value, 8 if fmt == PCT else 2))
        cell.number_format, cell.font = fmt, BOLD

    def paint_forecast(self):
        """Фон колонки прогноза по всей высоте таблицы."""
        if self.fc_col is not None:
            for r in range(5, self.row):
                self.ws.cell(r, self.fc_col).fill = F_FORECAST


def sum_vals(list_of_vals, months):
    return {m: sum(v.get(m, 0.0) for v in list_of_vals) for m in months}


def write_pnl(ws, table, months, title, subtitle="", info=None, detail_order=None, cogs=None, prep=None, tax=None,
              forecast=None, retro=None):
    """ОПиУ в единых статьях. table: {статья: {строка: {месяц: сумма}}}.
    forecast — прогноз на конец последнего периода (period_forecast): отдельная колонка справа."""
    sh = Sheet(ws, months, title, subtitle, forecast)
    fc = (forecast or {}).get("values") or {}
    fc_rev = fc.get("revenue")

    def fc_share(key, sign=1):
        return sign * fc[key] / fc_rev if fc and fc_rev else None
    tot = article_totals(table, months)
    revenue = sum_vals([tot[a] for a in REVENUE_ARTICLES], months)
    sh.revenue_total = sum(revenue.values())

    def details(article):
        rows = table.get(article, {})
        keys = detail_order(rows) if detail_order else sorted(rows, key=lambda k: -abs(sum(rows[k].values())))
        for k in keys:
            if any(abs(v) >= 0.5 for v in rows[k].values()):
                sh.line(k, rows[k], indent=3)

    sh.section("ВЫРУЧКА")
    for a in REVENUE_ARTICLES:
        if table.get(a):
            sh.line(a, tot[a], indent=1, font=BOLD, fill=F_ARTICLE)
            details(a)
    sh.line("Выручка за вычетом возвратов", revenue, fill=F_TOTAL, font=BOLD)
    sh.forecast(fc.get("revenue"))
    sh.blank()

    sh.section("РАСХОДЫ МАРКЕТПЛЕЙСОВ")
    for a in EXPENSE_ARTICLES:
        if table.get(a):
            sh.line(a, tot[a], indent=1, font=BOLD, fill=F_ARTICLE)
            details(a)
    expenses = sum_vals([tot[a] for a in EXPENSE_ARTICLES], months)
    sh.line("Итого расходы маркетплейсов", expenses, fill=F_TOTAL, font=BOLD)
    sh.blank()

    for a in (COMP, ADVANCE):
        if table.get(a):
            sh.line(a, tot[a], indent=1, font=BOLD, fill=F_ARTICLE)
            details(a)
    net = sum_vals([revenue, expenses, tot[COMP], tot[ADVANCE]], months)
    sh.line("ИТОГО: РЕЗУЛЬТАТ ПО ДАННЫМ МАРКЕТПЛЕЙСОВ", net, fill=F_RESULT, font=Font(bold=True, size=12))
    sh.forecast(fc.get("net"))
    sh.blank()

    gross, profit = None, None
    if cogs:
        sh.line(COGS, cogs, indent=1, font=BOLD, fill=F_ARTICLE)
        sh.forecast(fc.get("cogs"))
        if retro:
            sh.line(RETRO, retro, indent=1, font=BOLD, fill=F_ARTICLE)
            sh.forecast(fc.get("retro"))
        gross = sum_vals([net, cogs, retro or {}], months)
        sh.line(GROSS, gross, fill=F_RESULT, font=Font(bold=True, size=12))
        sh.forecast(fc.get("gross"))
        sh.blank()
    # Подготовка и налог — всегда отдельными строками: сумма и сразу под ней % от выручки
    base = gross or net
    prep, tax = prep or {}, tax or {}
    for title, vals, key in ((PREP, prep, "prep"), (TAX, tax, "tax")):
        sh.line(f"{title}, ₽", vals, indent=1, font=BOLD, fill=F_ARTICLE)
        sh.forecast(fc.get(key))
        ratio = {m: (vals.get(m, 0.0) / revenue[m] if revenue.get(m) else 0.0) for m in months}
        total_ratio = sum(vals.values()) / sh.revenue_total if sh.revenue_total else 0.0
        sh.line(f"{title}, % от выручки", ratio, indent=2, pct=False, fmt=PCT, total=total_ratio)
        sh.forecast(fc_share(key), PCT)
    profit = sum_vals([base, prep, tax], months)
    sh.line(PROFIT, profit, fill=F_RESULT, font=Font(bold=True, size=12))
    sh.forecast(fc.get("profit"))
    sh.blank()

    sh.section("ДОЛЯ ОТ ВЫРУЧКИ")
    share_lines = [("Все расходы маркетплейсов", expenses)] + [(a, tot[a]) for a in EXPENSE_ARTICLES if table.get(a)] \
        + [("Результат", net)]
    if cogs:
        share_lines += [(COGS, cogs)] + ([(RETRO, retro)] if retro else []) + [("Валовая маржа", gross)]
    share_lines += [(PREP, prep), (TAX, tax), ("Рентабельность после себестоимости, подготовки и налога", profit)]
    for label, vals in share_lines:
        sign = 1 if label in ("Результат", "Валовая маржа", RETRO,
                              "Рентабельность после себестоимости, подготовки и налога") else -1
        ratio = {m: (sign * vals.get(m, 0.0) / revenue[m] if revenue.get(m) else 0.0) for m in months}
        total_ratio = sign * sum(vals.values()) / sh.revenue_total if sh.revenue_total else 0.0
        sh.line(label, ratio, indent=1 if label in ("Все расходы маркетплейсов", "Результат", "Валовая маржа",
                "Рентабельность после себестоимости, подготовки и налога") else 2,
                pct=False, fmt=PCT, total=total_ratio)
        fc_key = {"Результат": "net", COGS: "cogs", RETRO: "retro", "Валовая маржа": "gross", PREP: "prep", TAX: "tax",
                  "Рентабельность после себестоимости, подготовки и налога": "profit"}.get(label)
        if fc_key:
            sh.forecast(fc_share(fc_key, sign), PCT)
    sh.paint_forecast()

    if info:
        sh.blank()
        sh.section("СПРАВОЧНО (в итоги не входит)")
        for label, vals in info.items():
            sh.line(label, vals, indent=2, pct=False)
    return net


def write_comparison(ws, per_mp, months, cogs_by_mp=None, prep_by_mp=None, tax_by_mp=None, retro_by_mp=None):
    """Статьи за период: сумма и % выручки по каждому маркетплейсу и всего."""
    names = list(per_mp) + ["Все маркетплейсы"]
    all_table = merge_tables([per_mp[n] for n in per_mp])
    tables = [per_mp[n] for n in per_mp] + [all_table]
    ws.cell(1, 1, "Сравнение маркетплейсов за период").font = Font(bold=True, size=14)
    ws.cell(2, 1, period_range(months, GRAIN)).font = Font(italic=True, color="666666")
    ws.cell(4, 1, "Статья").font = WHITE_BOLD
    ws.cell(4, 1).fill = F_HEADER
    ws.column_dimensions["A"].width = 44
    for i, n in enumerate(names):
        c = 2 + i * 2
        ws.merge_cells(start_row=3, start_column=c, end_row=3, end_column=c + 1)
        ws.cell(3, c, n).font = BOLD
        ws.cell(3, c).alignment = Alignment(horizontal="center")
        for j, h in enumerate(["Сумма", "% выручки"]):
            cell = ws.cell(4, c + j, h)
            cell.font, cell.fill = WHITE_BOLD, F_HEADER
            ws.column_dimensions[get_column_letter(c + j)].width = 15 if j == 0 else 10
    totals = [article_totals(t, months) for t in tables]
    period = [{a: sum(v.values()) for a, v in t.items()} for t in totals]
    revenue = [p[REV_SALES] + p[REV_RETURNS] for p in period]
    expenses = [sum(p[a] for a in EXPENSE_ARTICLES) for p in period]
    net = [revenue[i] + expenses[i] + period[i][COMP] + period[i][ADVANCE] for i in range(len(tables))]
    lines = ([(REV_SALES, [p[REV_SALES] for p in period], None), (REV_RETURNS, [p[REV_RETURNS] for p in period], None),
              ("Выручка за вычетом возвратов", revenue, F_TOTAL)]
             + [(a, [p[a] for p in period], None) for a in EXPENSE_ARTICLES]
             + [("Итого расходы маркетплейсов", expenses, F_TOTAL), (COMP, [p[COMP] for p in period], None),
                (ADVANCE, [p[ADVANCE] for p in period], None), ("Результат", net, F_RESULT)])
    result = net
    if cogs_by_mp:
        cogs_vals = [sum(cogs_by_mp.get(n, {}).values()) for n in names[:-1]]
        cogs_vals.append(sum(cogs_vals))
        retro_vals = [sum((retro_by_mp or {}).get(n, {}).values()) for n in names[:-1]]
        retro_vals.append(sum(retro_vals))
        gross = [net[i] + cogs_vals[i] + retro_vals[i] for i in range(len(tables))]
        lines += ([(COGS, cogs_vals, None)] + ([(RETRO, retro_vals, None)] if any(retro_vals) else [])
                  + [(GROSS, gross, F_RESULT)])
        result = gross
    extra = []
    for title, source in ((PREP, prep_by_mp), (TAX, tax_by_mp)):
        vals = [sum((source or {}).get(n, {}).values()) for n in names[:-1]]
        vals.append(sum(vals))
        extra.append((title, vals, None))
        result = [result[i] + vals[i] for i in range(len(tables))]
    lines += extra + [(PROFIT, result, F_RESULT)]
    row = 5
    for label, vals, fill in lines:
        ws.cell(row, 1, label)
        for i, v in enumerate(vals):
            c = 2 + i * 2
            ws.cell(row, c, round(v, 2)).number_format = NUM
            share = (v / revenue[i]) if revenue[i] else 0.0
            if label not in (REV_SALES, "Выручка за вычетом возвратов"):
                ws.cell(row, c + 1, share if label != "Результат" else share).number_format = PCT
        if fill:
            for c in range(1, 2 + len(names) * 2):
                ws.cell(row, c).fill = fill
                ws.cell(row, c).font = BOLD
        row += 1
    ws.freeze_panes = "B5"
    ws.cell(row + 1, 1, "Проценты расходов — доля от выручки за вычетом возвратов своего маркетплейса.").font = \
        Font(italic=True, color="666666")


def write_goods(ws, goods, names, months):
    """Товары за период: штуки, выручка, себестоимость, валовая прибыль и маржа."""
    ws.append(["Артикул", "Название", "Маркетплейс", "Штук", "Выручка", "Себестоимость", "Подготовка",
               "Валовая прибыль", "Маржа, %"])
    totals = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for (mp, article), (qty, revenue, cost, prep) in goods.items():
        t = totals[article]
        t[0] += qty
        t[1] += revenue
        t[2] += cost
        t[3] += prep

    def row_for(article, mp, qty, revenue, cost, prep):
        gross = revenue - cost - prep
        return [article, names.get(article, ""), mp, round(qty, 2), round(revenue, 2), round(-cost, 2),
                round(-prep, 2), round(gross, 2), gross / revenue if revenue else 0]

    rows = []
    for article in sorted(totals, key=lambda a: -totals[a][1]):
        rows.append(row_for(article, "Все маркетплейсы", *totals[article]))
        for (mp, art), values in sorted(goods.items(), key=lambda kv: -kv[1][1]):
            if art == article:
                rows.append(row_for(article, mp, *values))
    for r in rows:
        ws.append(r)
        if r[2] == "Все маркетплейсы":
            for c in range(1, 10):
                ws.cell(ws._current_row, c).font = BOLD
                ws.cell(ws._current_row, c).fill = F_ARTICLE
    for c, wdt in enumerate([18, 62, 18, 10, 16, 16, 14, 16, 10], 1):
        ws.cell(1, c).font, ws.cell(1, c).fill = WHITE_BOLD, F_HEADER
        ws.column_dimensions[get_column_letter(c)].width = wdt
    for c in (5, 6, 7, 8):
        for (cell,) in ws.iter_rows(min_row=2, min_col=c, max_col=c):
            cell.number_format = NUM
    for (cell,) in ws.iter_rows(min_row=2, min_col=9, max_col=9):
        cell.number_format = PCT
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def write_order_costs(ws, lines, names):
    """Детализация себестоимости: заказ, артикул, штуки, себестоимость за единицу и за заказ."""
    header = ["Маркетплейс", "Кабинет", "Период", "Дата", "Номер заказа", "Артикул", "Название", "Кол-во, шт",
              "Кратность", "Себестоимость за ед., ₽", "Себестоимость заказа, ₽"]
    first, last = 4, 3 + len(lines)
    ws.cell(1, 1, "Себестоимость проданных товаров по заказам").font = Font(bold=True, size=12)
    ws.cell(2, 1, "Итого по видимым строкам (учитывает фильтр):").font = BOLD
    ws.cell(2, 8, f"=SUBTOTAL(9,H{first}:H{last})").number_format = '#,##0'
    ws.cell(2, 11, f"=SUBTOTAL(9,K{first}:K{last})").number_format = NUM
    ws.cell(2, 8).font = ws.cell(2, 11).font = BOLD
    ws.cell(2, 7, "Возвраты — отрицательное количество; себестоимость за ед. — из cost.csv на месяц продажи").font =         Font(italic=True, color="666666")
    for c, (title, wdt) in enumerate(zip(header, [14, 20, 9, 11, 26, 16, 50, 10, 10, 14, 16]), 1):
        cell = ws.cell(3, c, title)
        cell.font, cell.fill = WHITE_BOLD, F_HEADER
        ws.column_dimensions[get_column_letter(c)].width = wdt
    counter = Counter("запись строк на лист", len(lines))
    for mp, cab, mon, day, order, article, qty, mult, cost, amount in lines:
        counter.tick()
        ws.append([mp, cab, mon, day, order, article, names.get(art_key(article), ""), qty, mult,
                   cost, round(amount, 2) if amount is not None else "нет в cost.csv"])
        row = ws._current_row   # max_row пересчитывает весь лист — на 94 тыс. строк это десятки минут
        ws.cell(row, 10).number_format = '#,##0.00'
        if amount is None:
            ws.cell(row, 11).fill = F_WARN
        else:
            ws.cell(row, 11).number_format = '#,##0.00;[Red]-#,##0.00'
    counter.finish()
    ws.freeze_panes = "A4"
    ws.auto_filter.ref = f"A3:K{max(last, 3)}"


def write_mapping(ws, mapping):
    ws.append(["Маркетплейс", "Группа в отчёте маркетплейса", "Исходная строка", "Единая статья", "Сумма за период",
               "Как сопоставлено"])
    for (mp, group, label, article, guessed), total in sorted(mapping.items(),
                                                              key=lambda x: (x[0][0], x[0][3], -abs(x[1]))):
        ws.append([mp, group, label, article, round(total, 2), "по ключевым словам — проверьте" if guessed else "по группе"])
        if guessed:
            for c in range(1, 7):
                ws.cell(ws._current_row, c).fill = F_WARN
    for c, wdt in enumerate([16, 34, 70, 36, 16, 26], 1):
        ws.cell(1, c).font, ws.cell(1, c).fill = WHITE_BOLD, F_HEADER
        ws.column_dimensions[get_column_letter(c)].width = wdt
    for (cell,) in ws.iter_rows(min_row=2, min_col=5, max_col=5):
        cell.number_format = NUM
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    row = ws.max_row + 3
    ws.cell(row, 1, "Единый план статей").font = Font(bold=True, size=12)
    for a in REVENUE_ARTICLES + EXPENSE_ARTICLES + [COMP, ADVANCE]:
        row += 1
        ws.cell(row, 1, a).font = BOLD
        ws.cell(row, 3, ARTICLE_DESCRIPTIONS[a])


def write_control(ws, entities, notes, months, unified_net, covered=None, missing_costs=None, has_costs=True,
                  tax_used=None, prep_map=None, missing_prep=None, order_totals=None, cogs_map=None,
                  script_errors=None):
    """Пишет лист «Контроль»; возвращает строки, требующие внимания: (МП, кабинет, показатель, значение, статус)."""
    ws.append(["Маркетплейс", "Кабинет", "Показатель", "Значение", "Статус"])
    for e in entities:
        own = sum(v for m, v in e["net"].items() if m in months)
        ours = unified_net[(e["mp"], e["cabinet"])]
        diff = ours - own
        ws.append([e["mp"], e["cabinet"], "Итог в отчёте маркетплейса, ₽", round(own, 2), ""])
        ws.append([e["mp"], e["cabinet"], "Итог в сводном отчёте, ₽", round(ours, 2),
                   "OK" if abs(diff) < 1 else f"РАСХОЖДЕНИЕ {diff:,.2f}"])
        cov = (covered or {}).get((e["mp"], e["cabinet"]))
        if cov and cov[1]:
            share = cov[0] / cov[1]
            ws.append([e["mp"], e["cabinet"], "Выручка с себестоимостью, % от выручки товаров", round(share, 4),
                       "OK" if share > 0.999 else "НЕ ВСЯ СЕБЕСТОИМОСТЬ"])
            ws.cell(ws.max_row, 4).number_format = PCT
            if share <= 0.999:
                ws.append([e["mp"], e["cabinet"], "Выручка без себестоимости, ₽", round(cov[1] - cov[0], 2),
                           "дозаполните cost.csv"])
        elif not has_costs:
            ws.append([e["mp"], e["cabinet"], "Себестоимость", "нет файла cost.csv", "ВНИМАНИЕ"])
        if e.get("orders"):
            diff = (order_totals or {}).get((e["mp"], e["cabinet"]), 0.0) + sum(
                (cogs_map or {}).get((e["mp"], e["cabinet"]), {}).values())
            ws.append([e["mp"], e["cabinet"], "Себестоимость: лист по заказам минус ОПиУ, ₽", round(diff, 2),
                       "OK" if abs(diff) < 1 else "ПРОВЕРИТЬ"])
        elif has_costs:
            ws.append([e["mp"], e["cabinet"], "Себестоимость по заказам", "нет данных о заказах в выгрузке",
                       "ВНИМАНИЕ"])
        rate = (tax_used or {}).get((e["mp"], e["cabinet"]))
        ws.append([e["mp"], e["cabinet"], "Ставка налога с выручки, %", rate if rate is not None else "не задана",
                   "OK" if rate else "укажите tax_percent в конфиге маркетплейса"])
        base = sum(purchase_tax_base(e, set(months)).values())
        ws.append([e["mp"], e["cabinet"], "Налоговая база: фактическая цена покупки, ₽", round(base, 2), "OK"])
        prep_total = sum((prep_map or {}).get((e["mp"], e["cabinet"]), {}).values())
        if prep_total:
            ws.append([e["mp"], e["cabinet"], "Подготовка товара за период, ₽", round(-prep_total, 2), "OK"])
        for title, value in e["checks"]:
            status = ""
            if isinstance(value, (int, float)) and "асхожден" in title:
                status = "OK" if abs(value) < 1 else "ПРОВЕРИТЬ"
            if "не учтены" in title.lower() or "без расшифровки" in title.lower():
                status = "OK" if value in (0, 0.0) else "ВНИМАНИЕ"
            if "из кэша" in title.lower():
                status = "ВНИМАНИЕ"
            if isinstance(value, (int, float)) and "API-ключ действует до" in title:
                # Ключи Ozon с 03.09.2026 действуют 3 месяца — перевыпустить заранее
                status = "OK" if value > 14 else ("КЛЮЧ ИСТЁК" if value < 0 else "ПЕРЕВЫПУСТИТЬ КЛЮЧ")
            ws.append([e["mp"], e["cabinet"], title, value, status])
    for mp, errors in (script_errors or {}).items():
        for err in errors:
            ws.append([mp, "—", "Сообщение скрипта", err, "ОШИБКА"])
    for mp, note in notes.items():
        ws.append([mp, "—", "Загрузка", note, "НЕТ ДАННЫХ" if note.startswith(("нет", "скрипт завершился")) else "ВНИМАНИЕ"])
    for (mp, article), (qty, revenue) in sorted((missing_costs or {}).items(), key=lambda kv: -kv[1][1])[:40]:
        ws.append([mp, "—", f"Нет себестоимости: {article}", round(revenue, 2),
                   f"{round(qty)} шт. — заполните cost_missing.csv"])
    for (mp, article), (qty, revenue) in sorted((missing_prep or {}).items(), key=lambda kv: -kv[1][1])[:40]:
        ws.append([mp, "—", f"Нет стоимости подготовки: {article}", round(revenue, 2),
                   f"{round(qty)} шт. — заполните cost_missing.csv"])
    for row in ws.iter_rows(min_row=2):
        status = str(row[4].value or "")
        if status and status != "OK":
            for cell in row:
                cell.fill = F_WARN
        if isinstance(row[3].value, (int, float)):
            row[3].number_format = '#,##0.00'
    for c, wdt in enumerate([16, 22, 52, 22, 26], 1):
        ws.cell(1, c).font, ws.cell(1, c).fill = WHITE_BOLD, F_HEADER
        ws.column_dimensions[get_column_letter(c)].width = wdt
    ws.freeze_panes = "A2"
    return [tuple(c.value for c in row[:5]) for row in ws.iter_rows(min_row=2)
            if str(row[4].value or "") not in ("", "OK")]


def write_registry(ws, rows):
    """Лист «Реестр» — умная таблица Excel, источник для сводных таблиц."""
    ws.append(REGISTRY_HEADER)
    counter = Counter("запись реестра", len(rows))
    for r in rows:
        counter.tick()
        ws.append(r)
        row = ws._current_row
        ws.cell(row, 10).number_format = '#,##0.00;[Red]-#,##0.00'
    counter.finish()
    for c, wdt in enumerate([14, 20, 9, 18, 44, 14, 38, 50, 13, 14, 10], 1):
        ws.column_dimensions[get_column_letter(c)].width = wdt
    table = Table(displayName="Реестр", ref=f"A1:{get_column_letter(len(REGISTRY_HEADER))}{len(rows) + 1}")
    table.tableStyleInfo = TableStyleInfo(name="TableStyleLight9", showRowStripes=True)
    ws.add_table(table)
    ws.freeze_panes = "A2"


# Первые колонки — ключ строки (маркетплейс, кабинет, артикул, название, бренд, категория), дальше — суммы
UNIT_KEYS = 6
UNIT_HEADER = [
    ("Маркетплейс", 14, None), ("Кабинет", 20, None), ("Артикул", 18, None), ("Название", 40, None),
    ("Бренд", 18, None), ("Категория", 22, None), (MP_MULT, 9, '#,##0'),
    ("Продано, шт", 10, '#,##0'), ("Возвращено, шт", 11, '#,##0'), ("Нетто, шт", 10, '#,##0'),
    ("Продажи, ₽", 14, NUM), ("Возвраты, ₽", 12, NUM), ("Выручка нетто, ₽", 14, NUM),
    ("Комиссия", 13, NUM), ("Логистика", 13, NUM), ("Обратная логистика", 12, NUM), ("Эквайринг", 12, NUM),
    ("Реклама", 12, NUM), ("Хранение", 12, NUM), ("Штрафы", 11, NUM), ("Прочие услуги", 11, NUM),
    ("Расходы МП итого, ₽", 14, NUM), ("Расходы МП, % выручки", 11, PCT),
    ("Компенсации", 12, NUM), ("Авансы", 10, NUM), ("К перечислению от МП, ₽", 14, NUM),
    ("Себестоимость", 14, NUM), ("Ретро-бонус", 12, NUM), ("Подготовка", 12, NUM), ("Налог", 12, NUM),
    ("Прибыль, ₽", 14, NUM), ("Маржа, % выручки", 10, PCT),
    ("Цена за 1 шт", 11, NUM), ("Расходы МП на 1 шт", 11, NUM), ("Себестоимость на 1 шт", 12, NUM),
    ("Прибыль на 1 шт", 11, NUM),
    (PR_BASE, 11, NUM), (PR_DISC, 8, PCT), (PR_PRICE, 11, NUM), (PR_PROMO, 11, NUM),
    (F_SHOWS, 12, '#,##0'), (F_CLICKS, 11, '#,##0'), (F_CTR, 8, PCT), (F_CART, 10, '#,##0'), (F_CART_CONV, 10, PCT),
    (F_ORDERS, 10, '#,##0'), (F_ORDER_CONV, 10, PCT), (F_CARD_ORDER, 10, PCT), (F_THROUGH, 11, PCT),
    (F_BUYOUT, 10, '#,##0'), (F_BUYOUT_PCT, 9, PCT), (F_BUYOUT_DONE, 10, PCT),
    (F_CANCEL_PCT, 9, PCT), (F_RETURN_PCT, 9, PCT),
    (ORD_QTY, 10, '#,##0'), (ORD_SUM, 14, NUM), (ORD_CANCEL, 10, '#,##0'), (ORD_PER_DAY, 10, '#,##0.0'),
    (ST_MP, 11, '#,##0'), (ST_OWN, 11, '#,##0'), (ST_TOTAL, 11, '#,##0'), (ST_COST, 14, NUM),
    (ST_DAYS, 9, '#,##0'), (ST_TO_CLIENT, 11, '#,##0'), (ST_RETURNING, 10, '#,##0'),
    (F_CLICKS_CTR, 3, '#,##0'), (F_ORDERS_SHOWS, 3, '#,##0'), (F_CLICKS_CART, 3, '#,##0'),
    (F_ORDERS_CART, 3, '#,##0'), (F_DONE, 3, '#,##0'),
    (PR_BASE_S, 3, NUM), (PR_BASE_N, 3, '#,##0'), (PR_PRICE_S, 3, NUM), (PR_PRICE_N, 3, '#,##0'),
    (PR_PROMO_S, 3, NUM), (PR_PROMO_N, 3, '#,##0'), (PR_DISC_S, 3, NUM),
]
UNIT_HIDDEN = {F_CLICKS_CTR, F_ORDERS_SHOWS, F_CLICKS_CART, F_ORDERS_CART, F_DONE} | PRICE_FIELDS
UNIT_GOODS = [PR_BASE, PR_DISC, PR_PRICE, PR_PROMO, F_SHOWS, F_CLICKS, F_CTR, F_CART, F_CART_CONV, F_ORDERS, F_ORDER_CONV, F_CARD_ORDER, F_THROUGH,
              F_BUYOUT, F_BUYOUT_PCT, F_BUYOUT_DONE,
              F_CANCEL_PCT, F_RETURN_PCT, ORD_QTY, ORD_SUM, ORD_CANCEL, ORD_PER_DAY, ST_MP, ST_OWN, ST_TOTAL, ST_COST, ST_DAYS, ST_TO_CLIENT,
              ST_RETURNING, F_CLICKS_CTR, F_ORDERS_SHOWS, F_CLICKS_CART, F_ORDERS_CART, F_DONE,
              PR_BASE_S, PR_BASE_N, PR_PRICE_S, PR_PRICE_N, PR_PROMO_S, PR_PROMO_N, PR_DISC_S]
UNIT_EXPENSES = [COMMISSION, LOGISTICS, RETURNS, PAYMENTS, ADS, STORAGE, PENALTY, OTHER]
# Денежные колонки — от «Продано, шт» до «Прибыль на 1 шт» (порядок списка money в unit_values)
UNIT_MONEY = [n for n, _w, _f in UNIT_HEADER[UNIT_KEYS + 1:UNIT_HEADER.index((PR_BASE, 11, NUM))]]
UNIT_NO_TOTAL = {MP_MULT}   # не складываются в строке итогов


def unit_values(agg):
    """Значения строки юнит-экономики {колонка: значение} из сумм по статьям."""
    sold, returned = agg.get(QTY_SOLD, 0.0), agg.get(QTY_RETURNED, 0.0)
    net_qty = sold + returned
    sales, returns = agg.get(REV_SALES, 0.0), agg.get(REV_RETURNS, 0.0)
    revenue = sales + returns
    expenses = [agg.get(a, 0.0) for a in UNIT_EXPENSES]
    mp_total = sum(expenses)
    payout = revenue + mp_total + agg.get(COMP, 0.0) + agg.get(ADVANCE, 0.0)
    cogs, prep, tax = agg.get(COGS, 0.0), agg.get(PREP, 0.0), agg.get(TAX, 0.0)
    retro = agg.get(RETRO, 0.0)
    profit = payout + cogs + retro + prep + tax

    def per(v):
        return round(v / net_qty, 2) if net_qty > 0 else None

    per_day, stock = agg.get(ORD_PER_DAY, 0.0), agg.get(ST_TOTAL, 0.0)

    def share(a, b):
        return a / b if b else None

    def avg(total, n):
        return round(agg.get(total, 0.0) / agg[n], 2) if agg.get(n) else None

    goods = {MP_MULT: agg.get(MP_MULT) or None,
             ST_DAYS: round(stock / per_day) if per_day > 0 and stock else None,
             PR_BASE: avg(PR_BASE_S, PR_BASE_N), PR_PRICE: avg(PR_PRICE_S, PR_PRICE_N),
             PR_PROMO: avg(PR_PROMO_S, PR_PROMO_N),
             PR_DISC: 1 - agg[PR_DISC_S] / agg[PR_BASE_S] if agg.get(PR_BASE_S) else None,
             F_CTR: share(agg.get(F_CLICKS_CTR, 0.0), agg.get(F_SHOWS, 0.0)),
             F_CART_CONV: share(agg.get(F_CART, 0.0), agg.get(F_CLICKS_CART, 0.0)),
             F_ORDER_CONV: share(agg.get(F_ORDERS_CART, 0.0), agg.get(F_CART, 0.0)),
             F_CARD_ORDER: share(agg.get(F_ORDERS, 0.0), agg.get(F_CLICKS, 0.0)),
             F_THROUGH: share(agg.get(F_ORDERS_SHOWS, 0.0), agg.get(F_SHOWS, 0.0)),
             F_BUYOUT_DONE: share(agg.get(F_BUYOUT, 0.0), agg.get(F_DONE, 0.0)),
             F_BUYOUT_PCT: share(agg.get(F_BUYOUT, 0.0), agg.get(F_ORDERS, 0.0)),
             F_CANCEL_PCT: share(agg.get(ORD_CANCEL, 0.0), agg.get(ORD_QTY, 0.0)),
             F_RETURN_PCT: share(-returned, sold)}
    money = ([round(sold, 2), round(-returned, 2), round(net_qty, 2), round(sales, 2), round(returns, 2),
              round(revenue, 2)] + [round(v, 2) for v in expenses] +
             [round(mp_total, 2), -mp_total / revenue if revenue else None, round(agg.get(COMP, 0.0), 2),
              round(agg.get(ADVANCE, 0.0), 2), round(payout, 2), round(cogs, 2), round(retro, 2), round(prep, 2),
              round(tax, 2),
              round(profit, 2), profit / revenue if revenue else None,
              per(revenue), per(mp_total), per(cogs), per(profit)])
    out = dict(zip(UNIT_MONEY, money))
    out.update({g: goods[g] if g in goods else round(agg.get(g, 0.0), 2) for g in UNIT_GOODS})
    out[MP_MULT] = goods[MP_MULT]
    return out


def write_unit_sheet(ws, groups, title, subtitle, header=None, keys=UNIT_KEYS):
    """groups: [((МП, кабинет, артикул, название, бренд, категория), {статья: сумма})] — строка на группу.
    header — колонки листа (по умолчанию UNIT_HEADER), первые keys из них — ключ группы (артикул — 4-й с конца).
    Строка 2 — итоги по видимым строкам (учитывают фильтр)."""
    header = header or UNIT_HEADER
    ws.cell(1, 1, title).font = Font(bold=True, size=12)
    ws.cell(1, keys + 1, subtitle).font = Font(italic=True, color="666666")
    first, last = 4, 3 + len(groups)
    col = {name: i for i, (name, _w, _f) in enumerate(header, 1)}
    L = defaultdict(str, {name: get_column_letter(i) for name, i in col.items()})
    ws.cell(2, 1, "Итого по видимым строкам:").font = BOLD
    ratios = {"Расходы МП, % выручки": f"-{L['Расходы МП итого, ₽']}2/{L['Выручка нетто, ₽']}2",
              "Маржа, % выручки": f"{L['Прибыль, ₽']}2/{L['Выручка нетто, ₽']}2",
              "Цена за 1 шт": f"{L['Выручка нетто, ₽']}2/{L['Нетто, шт']}2",
              "Расходы МП на 1 шт": f"{L['Расходы МП итого, ₽']}2/{L['Нетто, шт']}2",
              "Себестоимость на 1 шт": f"{L['Себестоимость']}2/{L['Нетто, шт']}2",
              "Прибыль на 1 шт": f"{L['Прибыль, ₽']}2/{L['Нетто, шт']}2",
              ST_DAYS: f"ROUND({L[ST_TOTAL]}2/{L[ORD_PER_DAY]}2,0)",
              PR_BASE: f"{L[PR_BASE_S]}2/{L[PR_BASE_N]}2", PR_PRICE: f"{L[PR_PRICE_S]}2/{L[PR_PRICE_N]}2",
              PR_PROMO: f"{L[PR_PROMO_S]}2/{L[PR_PROMO_N]}2", PR_DISC: f"1-{L[PR_DISC_S]}2/{L[PR_BASE_S]}2",
              F_CTR: f"{L[F_CLICKS_CTR]}2/{L[F_SHOWS]}2",
              F_CART_CONV: f"{L[F_CART]}2/{L[F_CLICKS_CART]}2",
              F_ORDER_CONV: f"{L[F_ORDERS_CART]}2/{L[F_CART]}2",
              F_CARD_ORDER: f"{L[F_ORDERS]}2/{L[F_CLICKS]}2",
              F_THROUGH: f"{L[F_ORDERS_SHOWS]}2/{L[F_SHOWS]}2",
              F_BUYOUT_DONE: f"{L[F_BUYOUT]}2/{L[F_DONE]}2",
              F_BUYOUT_PCT: f"{L[F_BUYOUT]}2/{L[F_ORDERS]}2",
              F_CANCEL_PCT: f"{L[ORD_CANCEL]}2/{L[ORD_QTY]}2",
              F_RETURN_PCT: f"{L['Возвращено, шт']}2/{L['Продано, шт']}2"}
    # В сокращённом заголовке (юнитка по неделям) части колонок нет: формулы, которые на них ссылаются
    # (вместо буквы колонки — пусто, остаётся одна «2»), не нужны
    ratios = {name: f for name, f in ratios.items()
              if name in col and not re.search(r"(^|[^A-Z])2(?![0-9])", f)}
    for name, _w, fmt in header[keys:]:
        if fmt != PCT and name not in ratios and name not in UNIT_NO_TOTAL:
            ws.cell(2, col[name], f"=SUBTOTAL(9,{L[name]}{first}:{L[name]}{last})")
    for name, formula in ratios.items():
        ws.cell(2, col[name], f"=IFERROR({formula},\"\")")
    for name, wdt, fmt in header:
        c = col[name]
        head = ws.cell(3, c, name)
        head.font, head.fill = WHITE_BOLD, F_HEADER
        head.alignment = Alignment(wrap_text=True, vertical="top")
        ws.column_dimensions[get_column_letter(c)].width = wdt
        cell = ws.cell(2, c)
        cell.font, cell.fill = BOLD, F_TOTAL
        if fmt:
            cell.number_format = fmt
    ws.row_dimensions[3].height = 45
    fmts = [f for _n, _w, f in header]
    names = [n for n, _w, _f in header[keys:]]
    for key, agg in groups:
        values = unit_values(agg)
        ws.append(list(key) + [values[n] for n in names])
        article = key[keys - 4]
        row = ws._current_row
        for c, fmt in enumerate(fmts, 1):
            if fmt:
                ws.cell(row, c).number_format = fmt
        if article == NO_ARTICLE:
            for c in range(1, len(fmts) + 1):
                ws.cell(row, c).fill = F_WARN
    for name in UNIT_HIDDEN & set(col):
        ws.column_dimensions[L[name]].hidden = True
    ws.freeze_panes = f"{get_column_letter(keys + 1)}4"
    ws.auto_filter.ref = f"A3:{get_column_letter(len(header))}{max(last, 3)}"


def add_extra(agg, values):
    """Добавляет к строке юнит-экономики заказы, остатки, цены, воронку; кратность — наибольшая, а не сумма."""
    for field, value in values.items():
        if field == MP_MULT:
            agg[field] = max(agg.get(field, 0.0), value)
        else:
            agg[field] += value


def unit_groups(rows, catalog, by_cabinet=True, extra=None, known_names=None):
    """Суммы реестра по (МП, кабинет, артикул) или по артикулу по всем МП; по выручке по убыванию,
    «Без артикула» — в конце кабинета. extra — заказы, остатки и цены из goods_extras: добавляются колонками,
    артикулы без продаж, но с заказами или остатками, получают свою строку; только с ценой — нет."""
    aggs = defaultdict(lambda: defaultdict(float))
    names = dict(known_names or {})
    for mp, cab, _mon, article, name, _sec, statya, _label, _how, amount, qty in rows:
        key = (mp, cab, article) if by_cabinet else ("Все маркетплейсы", "Все кабинеты", article)
        aggs[key][statya] += qty if statya in (QTY_SOLD, QTY_RETURNED) else amount
        names[article] = name or names.get(article, "")
    for (mp, cab, article), values in (extra or {}).items():
        key = (mp, cab, article) if by_cabinet else ("Все маркетплейсы", "Все кабинеты", article)
        if set(values) <= ATTACH_ONLY:
            continue   # только цена и кратность (нет продаж, заказов и остатков) — строку не заводим, см. ниже
        add_extra(aggs[key], values)
    for (mp, cab, article), values in (extra or {}).items():
        key = (mp, cab, article) if by_cabinet else ("Все маркетплейсы", "Все кабинеты", article)
        if not set(values) <= ATTACH_ONLY or key not in aggs:
            continue
        add_extra(aggs[key], values)
    revenue = {k: v.get(REV_SALES, 0.0) + v.get(REV_RETURNS, 0.0) for k, v in aggs.items()}
    keys = sorted(aggs, key=lambda k: (k[0], k[1], k[2] == NO_ARTICLE, -revenue[k], k[2]))
    return [((mp, cab, article, names.get(article, ""), *catalog.get(article, ("", ""))),
             aggs[(mp, cab, article)]) for mp, cab, article in keys]


def write_unit_control(ws, rows, entities, expected, months, catalog=None, weekly=False):
    """Сверка реестра с ОПиУ по кабинетам. Возвращает проблемные строки для итога в консоли.
    weekly — юнитка по неделям: воронка, остатки и цены в ней не показываются, их строки не нужны."""
    ws.append(["Маркетплейс", "Кабинет", "Показатель", "Значение", "Статус"])
    total = defaultdict(float)
    mp_exp, mp_exp_no_art = defaultdict(float), defaultdict(float)
    with_revenue, has_cost, no_brand = defaultdict(set), defaultdict(set), defaultdict(set)
    for mp, cab, _mon, article, _name, section, statya, _label, _how, amount, _qty in rows:
        total[(mp, cab)] += amount
        if section == "Расходы МП":
            mp_exp[(mp, cab)] += amount
            if article == NO_ARTICLE:
                mp_exp_no_art[(mp, cab)] += amount
        if statya == COGS:
            has_cost[(mp, cab)].add(article)
        if statya in REVENUE_ARTICLES and article != NO_ARTICLE:
            with_revenue[(mp, cab)].add(article)
            if not all((catalog or {}).get(article, ("", ""))):
                no_brand[(mp, cab)].add(article)
    problems = []
    for e in entities:
        key = (e["mp"], e["cabinet"])
        if e.get("by_article") is None:
            ws.append([*key, "Данные по артикулам", "нет в выгрузке скрипта (обновите скрипт МП)", "НЕТ ДАННЫХ"])
            continue
        diff = total[key] - expected.get(key, 0.0)
        ws.append([*key, "Прибыль в ОПиУ, ₽", round(expected.get(key, 0.0), 2), ""])
        ws.append([*key, "Сумма реестра, ₽", round(total[key], 2),
                   "OK" if abs(diff) < 1 else f"РАСХОЖДЕНИЕ {diff:,.2f}"])
        share = mp_exp_no_art[key] / mp_exp[key] if mp_exp[key] else 0.0
        ws.append([*key, "Расходы МП без артикула, ₽", round(mp_exp_no_art[key], 2), ""])
        ws.append([*key, "Расходы МП без артикула, % от расходов МП", round(share, 4), ""])
        ws.cell(ws._current_row, 4).number_format = PCT
        missing = with_revenue[key] - has_cost[key]
        if missing:
            ws.append([*key, "Артикулов с выручкой, но без себестоимости", len(missing),
                       "прибыль по ним завышена — заполните cost_missing.csv"])
        if no_brand[key]:
            ws.append([*key, "Артикулов без бренда или категории", len(no_brand[key]),
                       "заполните колонки «бренд» и «категория» в cost.csv: "
                       + ", ".join(sorted(no_brand[key])[:10])
                       + (" и др." if len(no_brand[key]) > 10 else "")])
    start, _end = month_bounds(months)
    for e in entities:
        key = (e["mp"], e["cabinet"])
        if e.get("ordered") is None:
            ws.append([*key, "Заказы", "нет в выгрузке", "обновите скрипт маркетплейса"])
        elif not e.get("ordered_from"):
            ws.append([*key, "Заказы", "не загружались", "запустите обновление без --from-cache"])
        else:
            since = e["ordered_from"][:10]
            ws.append([*key, "Заказы: данные маркетплейса есть с", since,
                       f"заказы до {since} не учтены (маркетплейс отдаёт только последние дни; "
                       "история копится в кэше с каждым запуском)" if since > start.isoformat() else ""])
        if e.get("multiples") is None:
            ws.append([*key, "Кратность на МП", "не загружалась", "запустите обновление без --from-cache"])
        else:
            ws.append([*key, "Кратность на МП: данные на", e.get("multiples_at") or "",
                       f"товаров с кратностью в карточке: {len(e['multiples'])}"])
        if weekly:
            continue
        funnel = [r for r in e.get("funnel") or [] if r[0] in months]
        if e.get("funnel") is None:
            ws.append([*key, "Воронка продаж", "не загружалась", "запустите обновление без --from-cache"])
        else:
            since = (e.get("funnel_from") or "")[:10]
            ws.append([*key, "Воронка: данные маркетплейса есть с", since,
                       f"воронка до {since} не учтена (маркетплейс отдаёт ограниченную историю)"
                       if since and since > start.isoformat() else ""])
            if funnel and not any(r[2] for r in funnel):
                ws.append([*key, "Воронка: показы", "нет", "WB отдаёт показы только с подпиской «Джем»"
                           if e["mp"] == "Wildberries" else "маркетплейс не отдал показы (у Ozon — только с Premium)"])
        if e.get("stocks") is None:
            ws.append([*key, "Остатки", "не загружались", "запустите обновление без --from-cache"])
        else:
            ws.append([*key, "Остатки на", e.get("stocks_at") or "", ""])
            ws.append([*key, "Остаток всего (склады МП + свой склад), шт",
                       sum(q for _a, _p, kind, q in e["stocks"] if kind in ("mp", "own")), ""])
        if e.get("prices") is None:
            ws.append([*key, "Цены", "не загружались", "запустите обновление без --from-cache"])
        else:
            ws.append([*key, "Цены на", e.get("prices_at") or "", f"товаров с ценой: {len(e['prices'])}"])
    for row in ws.iter_rows(min_row=2):
        status = str(row[4].value or "")
        if status and status != "OK":
            for cell in row:
                cell.fill = F_WARN
            problems.append(tuple(c.value for c in row[:5]))
        if isinstance(row[3].value, float) and row[3].number_format != PCT:
            row[3].number_format = '#,##0.00'
    for c, wdt in enumerate([16, 22, 44, 20, 50], 1):
        ws.cell(1, c).font, ws.cell(1, c).fill = WHITE_BOLD, F_HEADER
        ws.column_dimensions[get_column_letter(c)].width = wdt
    ws.freeze_panes = "A2"
    return [p for p in problems if str(p[4]).startswith(("РАСХОЖДЕНИЕ", "НЕТ ДАННЫХ"))]


def write_unit_help(ws, period):
    lines = [
        ("Юнит-экономика по артикулам", True),
        (f"Период: {period}. Данные — те же, что в сводном ОПиУ (opiu_all_*.xlsx); по каждому кабинету сумма "
         "реестра равна прибыли в ОПиУ (проверка — на листе «Контроль»).", False),
        ("", False),
        ("Листы", True),
        ("Юнит-экономика — строка на маркетплейс × кабинет × артикул за весь период. Строка 2 — итоги по видимым "
         "строкам: работает вместе с фильтром (например, выберите один артикул или один кабинет).", False),
        ("Артикулы — все МП — то же по артикулу суммарно по всем маркетплейсам и кабинетам.", False),
        ("Реестр — все суммы в плоском виде для сводных таблиц: маркетплейс × кабинет × месяц × артикул × статья × "
         "исходная строка отчёта маркетплейса.", False),
        ("", False),
        ("Как построить сводную таблицу", True),
        ("Встаньте в таблицу на листе «Реестр» → Вставка → Сводная таблица. Например: Строки — Артикул, "
         "Столбцы — Раздел или Статья, Значения — Сумма, ₽; Фильтры — Маркетплейс, Кабинет, Месяц.", False),
        ("Штуки: Значения — Кол-во, шт, фильтр Раздел = Количество (Продано и Возвращено — отдельные статьи).", False),
        ("", False),
        ("Колонки реестра", True),
        ("Сумма, ₽ — доходы со знаком «+», расходы со знаком «−». Сумма всех строк = прибыль.", False),
        ("Раздел — Выручка, Расходы МП, Компенсации, Авансы, Себестоимость, Подготовка, Налог, Количество.", False),
        ("Статья — единый план статей (как в ОПиУ); Исходная строка — как называет начисление маркетплейс.", False),
        ("Отнесение — «прямо»: маркетплейс указал артикул в начислении; «без артикула»: начисление не относится к "
         "товару (реклама по кампаниям, подписки, часть хранения и штрафов, гибкий график выплат Ozon). Такие суммы "
         "НЕ распределяются по товарам — они собраны в строке «Без артикула» каждого кабинета.", False),
        ("Себестоимость и подготовка — из cost.csv по месяцу продажи; налог — ставка кабинета × фактическая цена покупки после скидок маркетплейса, возвраты уменьшают базу.", False),
        ("", False),
        ("Колонки листа «Юнит-экономика»", True),
        ("Выручка нетто = продажи + возвраты (по цене продавца). Расходы МП итого — комиссия ... прочие услуги. "
         "К перечислению = выручка нетто + расходы МП + компенсации + авансы. "
         "Прибыль = к перечислению + себестоимость + подготовка + налог.", False),
        ("Бренд и категория — единые по артикулу: если они заполнены в cost.csv, берутся оттуда, иначе — из "
         "данных маркетплейса (WB отдаёт бренд и предмет; Ozon и Яндекс Маркет в финансовых отчётах — нет, "
         "для них заполните колонки «бренд» и «категория» в cost.csv). Пустая ячейка — артикул нигде не размечен.",
         False),
        ("Значения «на 1 шт» — на нетто-штуку (продано − возвращено). Прибыль по артикулам не включает расходы "
         "«Без артикула» — смотрите их отдельной строкой в каждом кабинете.", False),
        ("", False),
        ("Заказы и остатки", True),
        ("Заказано — заказы покупателей, оформленные в месяцах периода (по дате заказа), по цене продавца со "
         "скидкой продавца; Отменено — из них отменённые. Это спрос, а не продажи: продажи и выручка — по "
         "финансовым отчётам (выкупленные и переданные покупателю товары), поэтому цифры не совпадают. "
         "Источники: WB — статистика заказов, Ozon — отправления FBO и FBS, Яндекс Маркет — отчёт по заказам. "
         "WB и Маркет отдают заказы только примерно за 90 дней — более ранние накапливаются в кэше при каждом "
         "обновлении; с какой даты есть данные — на листе «Контроль».", False),
        ("Заказов в день = (заказано − отменено) / число дней периода, за которые есть данные о заказах "
         "(текущий месяц — по сегодня).", False),
        ("Остатки — на момент обновления отчёта (дата — на листе «Остатки»), не на конец месяца. На складах МП — "
         "FBO / склады маркетплейса, на своём складе — FBS/DBS (сколько указано в кабинете маркетплейса), "
         "Остаток всего = склады МП + свой склад. В пути к покупателям и возвраты в пути (WB) показаны отдельно "
         "и в «Остаток всего» не входят. Если один свой склад подключён к нескольким кабинетам, его остаток "
         "попадёт в каждый из них.", False),
        ("Остаток по себестоимости — остаток всего × себестоимость из cost.csv (действующая сейчас). "
         "Запас, дней = остаток всего / заказов в день.", False),
        ("", False),
        ("Цены", True),
        ("Текущая цена — цена, установленная сейчас в кабинете маркетплейса (на момент обновления отчёта, дата — "
         "на листе «Цены»), а не средняя цена продаж за месяц: её показывает колонка «Цена за 1 шт». "
         "Цена до скидки — зачёркнутая цена; Скидка, % = 1 − текущая / до скидки. "
         "WB — цена со скидкой продавца (цены с СПП для покупателя в API WB нет), по размерам — средняя; "
         "Ozon — цена товара, «Цена с акциями» — с учётом акций; Яндекс Маркет — базовая цена кабинета "
         "(если её нет — цены магазинов).", False),
        ("Если у артикула несколько цен (размеры, несколько кабинетов на листе «Артикулы — все МП»), показана "
         "средняя. Лист «Цены» — все товары с ценой, в том числе без продаж, с себестоимостью и подготовкой из "
         "cost.csv для сравнения. Цены в прибыль и в реестр не входят.", False),
        ("", False),
        ("Воронка продаж", True),
        ("Показы, переходы в карточку, добавления в корзину, заказы и выкупы — из аналитики маркетплейсов "
         "(WB — воронка продаж, Ozon — аналитика по товарам, Яндекс Маркет — отчёт «Аналитика продаж»). "
         "Заказано и выкуплено здесь — по данным аналитики, поэтому могут немного отличаться от колонки «Заказано, шт» "
         "(заказы) и от продаж финансового отчёта.", False),
        ("Конверсии (на листах юнит-экономики и «Воронка»):", False),
        ("• CTR = переходы в карточку / показы. Показы есть не везде: у WB — только с подпиской «Джем», у Ozon — "
         "с Premium.", False),
        ("• Карточка → корзина = в корзину / переходы в карточку.  • Корзина → заказ = заказано / в корзину.", False),
        ("• Карточка → заказ = заказано / переходы в карточку.  • Сквозная конверсия = заказано / показы "
         "(= CTR × карточка→корзина × корзина→заказ).", False),
        ("• Выкуп, % заказов = выкуплено / заказано. Выкупы считаются по дате выкупа, заказы — по дате заказа, "
         "поэтому в отдельном месяце может быть больше 100%. Выкуп по завершённым = выкуплено / (выкуплено + "
         "отменено + возвращено) — не зависит от сдвига дат и не превышает 100% (у WB возвратов в воронке нет).",
         False),
        ("• Отмены, % заказов = отменено / заказано. Возвраты: в юнит-экономике — % продаж по финансовому отчёту "
         "(возвращено / продано), на листе «Воронка» — % выкупа по данным аналитики (у WB нет).  "
         "• Средняя цена заказа = заказано ₽ / заказано шт.", False),
        ("Проценты в строке итогов считаются из сумм (Σ переходов / Σ показов), а не как среднее процентов, и "
         "только по строкам, где маркетплейс даёт оба показателя, — для этого есть скрытые колонки-помощники. "
         "Пустая ячейка — делить не на что или показателя нет. Конверсия может быть больше 100%: переходы и "
         "корзина — это действия, а заказы — штуки.", False),
        ("История: WB — до 365 дней, Яндекс Маркет без подписки — 90 дней (дальше копится в кэше), Ozon — сколько "
         "позволяет тариф. С какой даты есть данные — на листе «Контроль». Лист «Воронка» — те же данные по месяцам "
         "для сводных таблиц.", False),
        ("", False),
        ("Листы «Заказы» и «Остатки» — те же данные в плоском виде (месяц × артикул и артикул × склад) "
         "для фильтров и сводных таблиц. Заказы и остатки в прибыль и в реестр не входят.", False),
    ]
    for text, bold in lines:
        ws.append([text])
        cell = ws.cell(ws._current_row, 1)
        cell.font = Font(bold=True, size=12) if bold else Font()
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.column_dimensions["A"].width = 130


def write_flat_table(ws, header, rows, widths, formats, name):
    """Плоский лист с умной таблицей Excel (для фильтров и сводных таблиц)."""
    ws.append(header)
    for r in rows:
        ws.append(r)
        row = ws._current_row
        for c, fmt in formats.items():
            ws.cell(row, c).number_format = fmt
    for c, wdt in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(c)].width = wdt
    if rows:
        table = Table(displayName=name, ref=f"A1:{get_column_letter(len(header))}{len(rows) + 1}")
        table.tableStyleInfo = TableStyleInfo(name="TableStyleLight9", showRowStripes=True)
        ws.add_table(table)
    else:
        ws.append(["Нет данных"])
    ws.freeze_panes = "A2"


# Лист «Воронка»: данные (колонки F–M) и конверсии формулами Excel. Скрытые колонки-помощники нужны строке итогов:
# конверсия считается только по строкам, где маркетплейс даёт и числитель, и знаменатель.
FUNNEL_DATA = [("Показы", 11), ("Переходы в карточку", 11), ("В корзину", 10), ("Заказано, шт", 10),
               ("Заказано, ₽", 13), ("Выкуплено, шт", 10), ("Отменено, шт", 10), ("Возвращено, шт", 10)]
# (название, формула строки, формула итога); {F}…{M} — колонки данных, {r} — строка, {h1}…{h5} — помощники
FUNNEL_CONV = [
    ("CTR, %", 'IF(AND(ISNUMBER({F}{r}),N({F}{r})>0),N({G}{r})/{F}{r},"")', "{h1}/{F}"),
    ("Карточка → корзина, %", 'IF(AND(ISNUMBER({H}{r}),N({G}{r})>0),{H}{r}/{G}{r},"")', "{H}/{h3}"),
    ("Корзина → заказ, %", 'IF(AND(ISNUMBER({H}{r}),N({H}{r})>0),N({I}{r})/{H}{r},"")', "{h4}/{H}"),
    ("Карточка → заказ, %", 'IF(N({G}{r})>0,N({I}{r})/{G}{r},"")', "{I}/{G}"),
    ("Сквозная конверсия (показ → заказ), %", 'IF(AND(ISNUMBER({F}{r}),N({F}{r})>0),N({I}{r})/{F}{r},"")',
     "{h2}/{F}"),
    ("Выкуп, % заказов", 'IF(N({I}{r})>0,N({K}{r})/{I}{r},"")', "{K}/{I}"),
    ("Выкуп по завершённым, %", 'IF(N({K}{r})+N({L}{r})+N({M}{r})>0,N({K}{r})/(N({K}{r})+N({L}{r})+N({M}{r})),"")',
     "{K}/({K}+{L}+{M})"),
    ("Отмены, % заказов", 'IF(N({I}{r})>0,N({L}{r})/{I}{r},"")', "{L}/{I}"),
    ("Возвраты, % выкупа", 'IF(AND(ISNUMBER({M}{r}),N({K}{r})>0),{M}{r}/{K}{r},"")', "{M}/{h5}"),
    ("Средняя цена заказа, ₽", 'IF(N({I}{r})>0,N({J}{r})/{I}{r},"")', "{J}/{I}"),
]
FUNNEL_HELPERS = [("Переходы при известных показах", 'IF(ISNUMBER({F}{r}),N({G}{r}),0)'),
                  ("Заказано при известных показах", 'IF(ISNUMBER({F}{r}),N({I}{r}),0)'),
                  ("Переходы при известной корзине", 'IF(ISNUMBER({H}{r}),N({G}{r}),0)'),
                  ("Заказано при известной корзине", 'IF(ISNUMBER({H}{r}),N({I}{r}),0)'),
                  ("Выкуплено при известных возвратах", 'IF(ISNUMBER({M}{r}),N({K}{r}),0)')]


def write_funnel_sheet(ws, rows, period):
    """Лист «Воронка»: строка на МП × кабинет × месяц × артикул, конверсии — формулами. Строка 2 — итоги по
    видимым строкам (работают с фильтром), проценты в ней считаются из сумм."""
    keys = ["Маркетплейс", "Кабинет", "Месяц", "Артикул", "Название"]
    header = keys + [n for n, _w in FUNNEL_DATA] + [n for n, *_f in FUNNEL_CONV] + [n for n, _f in FUNNEL_HELPERS]
    first_conv = len(keys) + len(FUNNEL_DATA) + 1
    first_help = first_conv + len(FUNNEL_CONV)
    letters = {k: get_column_letter(len(keys) + 1 + i) for i, k in enumerate("FGHIJKLM")}
    letters.update({f"h{i + 1}": get_column_letter(first_help + i) for i in range(len(FUNNEL_HELPERS))})
    top, bottom = 4, 3 + max(len(rows), 1)
    ws.cell(1, 1, "Воронка продаж по артикулам").font = Font(bold=True, size=12)
    ws.cell(1, len(keys) + 1, f"{period}. Пустая ячейка — маркетплейс этот показатель не отдаёт "
                              "(WB — показы; Ozon без Premium — показы и корзину)").font = Font(italic=True, color="666666")
    ws.cell(2, 1, "Итого по видимым строкам:").font = BOLD
    for c, name in enumerate(header, 1):
        head = ws.cell(3, c, name)
        head.font, head.fill = WHITE_BOLD, F_HEADER
        head.alignment = Alignment(wrap_text=True, vertical="top")
        col = get_column_letter(c)
        total = ws.cell(2, c)
        total.font, total.fill = BOLD, F_TOTAL
        if len(keys) < c < first_conv or c >= first_help:
            total.value = f"=SUBTOTAL(9,{col}{top}:{col}{bottom})"
            total.number_format = NUM if name.endswith("₽") else '#,##0'
        elif first_conv <= c < first_help:
            name_, _row_f, total_f = FUNNEL_CONV[c - first_conv]
            total.value = "=IFERROR(" + total_f.format(**{k: f"{v}2" for k, v in letters.items()}) + ',"")'
            total.number_format = NUM if name_.endswith("₽") else PCT
    ws.row_dimensions[3].height = 60
    for r_i, row in enumerate(rows, top):
        subst = {**letters, "r": r_i}
        values = list(row) + ["=" + f.format(**subst) for _n, f, _t in FUNNEL_CONV] + \
            ["=" + f.format(**subst) for _n, f in FUNNEL_HELPERS]
        ws.append(values)
        for c in range(len(keys) + 1, len(header) + 1):
            name = header[c - 1]
            ws.cell(r_i, c).number_format = (PCT if "%" in name else NUM if name.endswith("₽") else '#,##0')
    widths = [14, 20, 9, 18, 44] + [w for _n, w in FUNNEL_DATA] + [10] * len(FUNNEL_CONV) + [3] * len(FUNNEL_HELPERS)
    for c, wdt in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(c)].width = wdt
    for c in range(first_help, len(header) + 1):
        ws.column_dimensions[get_column_letter(c)].hidden = True
    if rows:
        table = Table(displayName="Воронка", ref=f"A3:{get_column_letter(len(header))}{bottom}")
        table.tableStyleInfo = TableStyleInfo(name="TableStyleLight9", showRowStripes=True)
        ws.add_table(table)
    else:
        ws.append(["Нет данных"])
    ws.freeze_panes = f"{get_column_letter(len(keys) + 1)}4"


# Юнитка по неделям: строка = часть недели внутри месяца × маркетплейс × кабинет × артикул
WEEK_KEYS_HEADER = [("Год", 6, '0'), ("Месяц", 9, None), ("Неделя", 7, '0'), ("Даты", 12, None)]
UNIT_WEEK_COLUMNS = {MP_MULT, *UNIT_MONEY, ORD_QTY, ORD_SUM, ORD_CANCEL, ORD_PER_DAY, F_CANCEL_PCT, F_RETURN_PCT}
UNIT_WEEK_HEADER = (WEEK_KEYS_HEADER + UNIT_HEADER[:UNIT_KEYS] +
                    [h for h in UNIT_HEADER[UNIT_KEYS:] if h[0] in UNIT_WEEK_COLUMNS])
UNIT_WEEK_KEYS = len(WEEK_KEYS_HEADER) + UNIT_KEYS


def week_columns(key):
    """(год, месяц, номер недели ISO, даты) части недели."""
    d = date.fromisoformat(key)
    monday = d - timedelta(days=d.weekday())
    return (d.year, f"{d.month:02d} {MONTHS_RU[d.month - 1]}", monday.isocalendar()[1],
            f"{d:%d.%m}–{week_part_end(key):%d.%m}")


def week_extras(entities, parts):
    """Заказы по частям недель: {(часть, МП, кабинет, артикул): {показатель: значение}} и кратность
    {(МП, кабинет, артикул): шт}. Заказов в день — по дням части недели, за которые у МП есть данные."""
    extra = defaultdict(lambda: defaultdict(float))
    mult, since = {}, {}
    today = date.today()
    for e in entities:
        mp, cab = e["mp"], e["cabinet"]
        if e.get("ordered_from"):
            since[(mp, cab)] = date.fromisoformat(e["ordered_from"][:10])
        for part, article, qty, amount, c_qty, _c_amount in e.get("ordered") or []:
            if part in parts:
                x = extra[(part, mp, cab, art_key(article) or NO_ARTICLE)]
                x[ORD_QTY] += qty
                x[ORD_SUM] += amount
                x[ORD_CANCEL] += c_qty
        for article, qty, _source in e.get("multiples") or []:
            key = (mp, cab, art_key(article) or NO_ARTICLE)
            mult[key] = max(mult.get(key, 0.0), qty)
    for (part, mp, cab, _article), x in extra.items():
        first = max(date.fromisoformat(part), since.get((mp, cab), date.min))
        days = (min(week_part_end(part), today) - first).days + 1
        if days > 0:
            x[ORD_PER_DAY] = (x[ORD_QTY] - x[ORD_CANCEL]) / days
    return extra, mult


def unit_week_groups(rows, catalog, extra, mult, known_names=None):
    """Суммы реестра по (часть недели, МП, кабинет, артикул). Порядок: кабинет → артикулы по выручке за весь
    период («Без артикула» — в конце кабинета) → недели по порядку, чтобы динамика товара шла подряд."""
    aggs = defaultdict(lambda: defaultdict(float))
    names = dict(known_names or {})
    for mp, cab, part, article, name, _sec, statya, _label, _how, amount, qty in rows:
        aggs[(part, mp, cab, article)][statya] += qty if statya in (QTY_SOLD, QTY_RETURNED) else amount
        names[article] = name or names.get(article, "")
    for key, values in extra.items():
        add_extra(aggs[key], values)
    revenue = defaultdict(float)
    for (part, mp, cab, article), agg in aggs.items():
        revenue[(mp, cab, article)] += agg.get(REV_SALES, 0.0) + agg.get(REV_RETURNS, 0.0)
        if mult.get((mp, cab, article)):
            agg[MP_MULT] = mult[(mp, cab, article)]
    keys = sorted(aggs, key=lambda k: (k[1], k[2], k[3] == NO_ARTICLE, -revenue[(k[1], k[2], k[3])], k[3], k[0]))
    return [((*week_columns(part), mp, cab, article, names.get(article, ""), *catalog.get(article, ("", ""))),
             aggs[(part, mp, cab, article)]) for part, mp, cab, article in keys]


def write_unit_week_help(ws, period):
    lines = [
        ("Юнит-экономика по неделям", True),
        (f"Период: {period}. Строка — неделя × маркетплейс × кабинет × артикул. Данные и формулы — те же, что в "
         "месячной юнит-экономике (unit_economics_ГГГГ-ММ.xlsx); по каждому кабинету сумма прибыли равна прибыли "
         "в ОПиУ (проверка — на листе «Контроль»).", False),
        ("", False),
        ("Колонки периода", True),
        ("Год, Месяц — месяц, к которому относятся дни строки. Неделя — номер недели по ISO (пн–вс). "
         "Даты — дни недели внутри месяца.", False),
        ("Неделя на стыке месяцев делится на две строки: например, 28.09–04.10 — это 28.09–30.09 в сентябре и "
         "01.10–04.10 в октябре (номер недели у обеих одинаковый). Поэтому фильтр по месяцу даёт ровно месяц, "
         "а суммы совпадают с месячной юнит-экономикой. Чтобы увидеть неделю целиком, фильтруйте по колонке "
         "«Неделя» (и году).", False),
        ("", False),
        ("Как смотреть", True),
        ("Строки отсортированы: маркетплейс → кабинет → артикулы по выручке за весь период → недели по порядку. "
         "Фильтр по артикулу и кабинету показывает продажи товара по неделям; строка 2 — итоги по видимым строкам "
         "(ПРОМЕЖУТОЧНЫЕ.ИТОГИ), работает вместе с фильтром.", False),
        ("Для сводной таблицы выделите таблицу с 3-й строки (заголовки) → Вставка → Сводная таблица: например, "
         "Строки — Артикул, Столбцы — Неделя, Значения — Нетто, шт или Прибыль, ₽; Фильтры — Кабинет, Месяц.", False),
        ("", False),
        ("Колонки", True),
        ("Продажи, возвраты, расходы МП, себестоимость, подготовка, налог, прибыль — по финансовым отчётам "
         "маркетплейсов по дате операции (как в ОПиУ). Расходы без артикула (реклама по кампаниям, подписки и т.п.) "
         "по товарам не распределяются — строка «Без артикула» каждого кабинета.", False),
        ("Заказано, Отменено — заказы покупателей по дате заказа (спрос, а не продажи). Заказов в день = "
         "(заказано − отменено) / дни недели (текущая неделя — по сегодня).", False),
        ("Кратность на МП — сколько единиц в одном товаре по карточке маркетплейса (текущее значение, не по "
         "неделям). Подробно — лист «Кратность».", False),
        ("Остатки, цены и воронка продаж — в месячной юнит-экономике (по неделям маркетплейсы их не отдают).", False),
    ]
    for text, bold in lines:
        ws.append([text])
        cell = ws.cell(ws._current_row, 1)
        cell.font = Font(bold=True, size=12) if bold else Font()
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.column_dimensions["A"].width = 130


def build_unit_week_workbook(rows, entities, expected, parts, period, catalog, costs=None, names=None, preps=None):
    wb = Workbook()
    used = set()
    ws = wb.active
    ws.title = safe_name("Юнит по неделям", used)
    step("Юнит-экономика по неделям: заказы и кратность")
    extra, mult = week_extras(entities, parts)
    *_rest, mult_rows = goods_extras(entities, parts, costs or {}, names or {}, preps)
    step("Юнит-экономика по неделям: лист")
    write_unit_sheet(ws, unit_week_groups(rows, catalog, extra, mult, names),
                     "Юнит-экономика по неделям — маркетплейсы, кабинеты, артикулы",
                     f"{period}. Недели пн–вс, на стыке месяцев разделены по месяцам. "
                     "Строки «Без артикула» — расходы МП, не относящиеся к товару",
                     header=UNIT_WEEK_HEADER, keys=UNIT_WEEK_KEYS)
    write_mult_sheet(wb, used, mult_rows)
    problems = write_unit_control(wb.create_sheet(safe_name("Контроль", used)), rows, entities, expected,
                                  parts, catalog, weekly=True)
    write_unit_week_help(wb.create_sheet(safe_name("Описание", used)), period)
    return wb, problems


def write_mult_sheet(wb, used, mult_rows):
    """Лист «Кратность»: кратность продажи из карточек маркетплейсов и сравнение с кратностью в cost.csv."""
    write_flat_table(wb.create_sheet(safe_name("Кратность", used)), MULT_HEADER,
                     sorted(mult_rows, key=lambda r: (r[7] != "НЕТ", r[0], r[1], r[2])),
                     [14, 20, 18, 44, 11, 32, 11, 10, 16], {5: '#,##0', 7: '#,##0'}, "Кратность")


def build_unit_workbook(rows, entities, expected, months, period, catalog, costs=None, names=None, preps=None):
    wb = Workbook()
    used = set()
    ws = wb.active
    ws.title = safe_name("Юнит-экономика", used)
    step("Юнит-экономика: заказы и остатки")
    extra, order_rows, stock_rows, price_rows, mult_rows = goods_extras(entities, months, costs or {}, names or {},
                                                                         preps)
    funnel, funnel_rows = funnel_extras(entities, months, names or {})
    for key, values in funnel.items():
        add_extra(extra[key], values)
    step("Юнит-экономика: листы по артикулам")
    write_unit_sheet(ws, unit_groups(rows, catalog, extra=extra, known_names=names),
                     "Юнит-экономика по артикулам — маркетплейсы и кабинеты",
                     f"{period}. Строки «Без артикула» — расходы МП, не относящиеся к товару. "
                     "Остатки и цены — на дату загрузки")
    write_unit_sheet(wb.create_sheet(safe_name("Артикулы — все МП", used)),
                     unit_groups(rows, catalog, by_cabinet=False, extra=extra, known_names=names),
                     "Юнит-экономика по артикулам — все маркетплейсы вместе", period)
    write_flat_table(wb.create_sheet(safe_name("Заказы", used)), ORDERS_HEADER,
                     sorted(order_rows, key=lambda r: (r[0], r[1], r[2], -r[6])),
                     [14, 20, 9, 18, 44, 11, 14, 11, 13, 12, 14],
                     {6: '#,##0', 7: NUM, 8: '#,##0', 9: NUM, 10: '#,##0', 11: NUM}, "Заказы")
    write_flat_table(wb.create_sheet(safe_name("Остатки", used)), STOCKS_HEADER,
                     sorted(stock_rows, key=lambda r: (r[0], r[1], r[2], r[4])),
                     [14, 20, 18, 44, 30, 22, 11, 12, 14, 16],
                     {7: '#,##0', 8: NUM, 9: NUM}, "Остатки")
    write_flat_table(wb.create_sheet(safe_name("Цены", used)), PRICES_HEADER,
                     sorted(price_rows, key=lambda r: (r[0], r[1], r[2])),
                     [14, 20, 18, 44, 13, 9, 13, 13, 14, 16],
                     {5: NUM, 6: PCT, 7: NUM, 8: NUM, 9: NUM}, "Цены")
    write_mult_sheet(wb, used, mult_rows)
    write_funnel_sheet(wb.create_sheet(safe_name("Воронка", used)),
                       sorted(funnel_rows, key=lambda r: (r[0], r[1], r[2], -(r[8] or 0))), period)
    step("Юнит-экономика: реестр")
    write_registry(wb.create_sheet(safe_name("Реестр", used)), rows)
    problems = write_unit_control(wb.create_sheet(safe_name("Контроль", used)), rows, entities, expected,
                                  months, catalog)
    write_unit_help(wb.create_sheet(safe_name("Описание", used)), period)
    return wb, problems


def save_workbook(wb, out):
    """Сохраняет книгу; если файл открыт в Excel — под другим именем. Возвращает итоговый путь."""
    try:
        wb.save(out)
    except PermissionError:
        out = out.with_name(f"{out.stem}_{datetime.now():%Y%m%d_%H%M}{out.suffix}")
        print(f"\nФайл открыт в Excel — сохраняю под другим именем.")
        wb.save(out)
    return out


def print_summary(out, problems, guessed, unit_out=None, saved=True):
    """Итог запуска в консоли: «Готово» и сводка ошибок и замечаний (подробно — на листе «Контроль»)."""
    no_price = [p for p in problems if str(p[2]).startswith(("Нет себестоимости:", "Нет стоимости подготовки:"))]
    rest = [p for p in problems if p not in no_price]
    print("\n" + "=" * 70)
    print(f"ГОТОВО. Отчёт: {out}" if saved else "ГОТОВО. Сводный ОПиУ не сохранялся (--only-unit)")
    if unit_out:
        print(f"Юнит-экономика: {unit_out}")
    if not rest and not no_price and not guessed:
        print("Ошибок нет — все данные загружены, сверки сошлись.")
    else:
        print(f"Есть замечания ({len(rest) + bool(no_price) + bool(guessed)}), подробности — на листе «Контроль»:")
        for mp, cab, title, value, status in rest:
            where = mp if cab in (None, "—") else f"{mp} / {cab}"
            if isinstance(value, float):
                value = f"{value:.1%}" if "%" in str(title) else f"{value:,.2f}"
            print(f"  [{status}] {where}: {title} — {value}")
        if no_price:
            print(f"  [ВНИМАНИЕ] Артикулов без себестоимости или подготовки: {len(no_price)} — "
                  f"заполните {COST_MISSING_FILE}")
        if guessed:
            print(f"  [ВНИМАНИЕ] {len(guessed)} строк сопоставлены со статьями по ключевым словам — "
                  "проверьте лист «Сопоставление статей»")
    print("=" * 70)


def safe_name(name, used, limit=31):
    base = re.sub(r'[\[\]:*?/\\]', "", name)[:limit] or "Лист"
    title, n = base, 2
    while title.lower() in used:
        suffix = f" ({n})"
        title, n = base[:limit - len(suffix)] + suffix, n + 1
    used.add(title.lower())
    return title


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Сводный ОПиУ по всем маркетплейсам и кабинетам")
    ap.add_argument("--from", dest="date_from", default=DEFAULT_FROM,
                    help=f"Первый месяц свода, ГГГГ-ММ (по умолчанию {DEFAULT_FROM})")
    ap.add_argument("--to", dest="date_to", help="Последний месяц свода, ГГГГ-ММ (по умолчанию — последний с данными)")
    ap.add_argument("--from-cache", action="store_true", help="Не обращаться к API — только скачанные данные")
    ap.add_argument("--fill-missing", action="store_true",
                    help="Вместе с --from-cache: кабинеты, которых нет в кэше, загрузить из API")
    ap.add_argument("--only", help="Через запятую: ozon, wb, ym")
    ap.add_argument("--cache-for", help="Для этих маркетплейсов взять данные только из кэша (через запятую: ozon, wb, ym), "
                                        "остальные обновить из API — например, --cache-for ozon при включённом VPN")
    ap.add_argument("--out", help="Путь к Excel-файлу")
    ap.add_argument("--open", action="store_true", help="Открыть готовый Excel")
    ap.add_argument("--no-unit", action="store_true", help="Не собирать файл юнит-экономики по артикулам")
    ap.add_argument("--grain", choices=("month", "week", "day"), default="month",
                    help="Разрез отчёта: month — по месяцам (по умолчанию), week — по неделям (пн–вс) "
                         "за весь период свода, day — по дням одного месяца (--period-month)")
    ap.add_argument("--period-month", help="Месяц дневного отчёта ГГГГ-ММ (по умолчанию — текущий)")
    ap.add_argument("--only-unit", action="store_true",
                    help="Собрать только файл юнит-экономики: сводный opiu_all_*.xlsx не сохранять "
                         "(данные маркетплейсов всё равно загружаются)")
    ap.add_argument("--unit-weekly", action="store_true",
                    help="Собрать только юнит-экономику по неделям за весь период свода (с --from): строка — "
                         "неделя × маркетплейс × кабинет × артикул, колонки Год, Месяц, Неделя")
    ap.add_argument("--unit-month", help="Месяц юнит-экономики ГГГГ-ММ (по умолчанию — весь период свода; "
                                         "«0» или «все» — тоже весь период)")
    ap.add_argument("--reload", action="store_true",
                    help="WB: загрузить все недели заново (по умолчанию закрытые недели берутся из кэша, "
                         "из API — только новые)")
    args = ap.parse_args()
    global RELOAD
    RELOAD = args.reload
    if args.only_unit and args.no_unit:
        sys.exit("\n--only-unit и --no-unit вместе не имеют смысла: собирать будет нечего.")
    if args.only_unit and args.grain != "month":
        sys.exit("\n--only-unit работает только с разрезом по месяцам: юнит-экономика считается "
                 "за календарный месяц.")
    if args.unit_weekly:
        if args.grain != "month" or args.no_unit or args.unit_month:
            sys.exit("\n--unit-weekly собирается сам по себе: без --grain, --no-unit и --unit-month.")
        # Данные МП — по дням, свёрнутые в части недель внутри месяцев; сводный ОПиУ не сохраняется
        args.grain, args.only_unit = "unitweek", True
    if args.grain == "day" and not args.period_month:
        args.period_month = f"{date.today():%Y-%m}"
    if args.period_month:
        args.period_month = args.period_month.strip().replace(".", "-")
        if not re.fullmatch(r"\d{4}-\d{2}", args.period_month):
            sys.exit(f"\nМесяц дневного отчёта задаётся как ГГГГ-ММ, а не «{args.period_month}».")
    if args.grain in ("week", "day"):
        # Юнит-экономика и список артикулов без цены считаются за календарные месяцы: их собирает
        # обычный запуск по месяцам, здесь они сбивали бы с толку неполным периодом.
        args.no_unit = True
    global GRAIN
    GRAIN = args.grain
    if args.unit_month is not None:
        args.unit_month = args.unit_month.strip().replace(".", "-")
        if args.unit_month.lower() in ("", "0", "все", "all"):
            args.unit_month = None
        elif not re.fullmatch(r"\d{4}-\d{2}", args.unit_month):
            m = re.fullmatch(r"(\d{1,2})-(\d{4})", args.unit_month)   # 09-2026 / 9.2026
            if not m:
                sys.exit(f"Месяц юнит-экономики «{args.unit_month}» не понят — нужно ГГГГ-ММ, например 2026-09")
            args.unit_month = f"{m.group(2)}-{int(m.group(1)):02d}"

    only = {s.strip().lower() for s in args.only.split(",")} if args.only else None
    dumps, notes, script_errors, day_dumps = [], {}, {}, []
    fc_first_day = (date.today() - timedelta(days=FORECAST_DAYS_BACK)).isoformat()
    with tempfile.TemporaryDirectory() as tmp:
        for mp in MARKETPLACES:
            if only and mp["key"] not in only:
                continue
            data, note, errors = run_marketplace(mp, args, tmp)
            if errors:
                script_errors[mp["name"]] = errors
            if note:
                notes[mp["name"]] = note
                print(f"  {mp['name']}: {note}")
            if data and data.get("cabinets"):
                if args.grain == "week":   # дни нужны прогнозу на конец недели, до того как свернём их в недели
                    day_dumps.append(day_slice(data, fc_first_day))
                dumps.append(regroup_dump(data, args.grain, args.period_month)
                             if args.grain != "month" else data)
    if not dumps:
        for mp, errors in script_errors.items():
            for err in errors:
                print(f"  {mp}: {err}")
        sys.exit("\nНет данных ни по одному маркетплейсу.")

    print()
    step("Все маркетплейсы загружены. Собираю отчёт...")
    entities, mapping = build(dumps)
    added = merge_missing_costs(HERE / COST_FILE, HERE / COST_MISSING_FILE)
    if added:
        print(f"\nПеренесено из {COST_MISSING_FILE} в {COST_FILE}: {added} артикулов")
    costs, cost_names = read_costs(HERE / COST_FILE, (COST_COL, "себестоимость"))
    preps, prep_names = read_costs(HERE / COST_FILE, (PREP_COL, "подготовка"))
    cost_names = {**prep_names, **cost_names}
    # Единый бренд и категория артикула: из cost.csv, чего там нет — из данных маркетплейсов (и наоборот
    # дописывается в cost.csv, чтобы справочник постепенно заполнялся сам)
    catalog = article_catalog(entities, read_article_book(HERE / COST_FILE))
    filled = sync_article_book(HERE / COST_FILE, catalog)
    if filled:
        print(f"\nБренд и категория: {filled} ячеек дописано в {COST_FILE} из данных маркетплейсов")
    months = sorted({m for d in dumps for m in d["months"]
                     if in_period(m, args.grain, args.date_from, args.date_to)})
    if not months and args.grain == "day":
        sys.exit(f"\nЗа {args.period_month} нет данных ни по одному маркетплейсу — "
                 "проверьте месяц или загрузите данные без --from-cache.")
    if not months:
        sys.exit(f"\nНет данных за период с {args.date_from}" + (f" по {args.date_to}" if args.date_to else ""))
    active = [m for m in months if any(abs(v.get(m, 0.0)) >= 0.5
                                       for e in entities for rows in e["table"].values() for v in rows.values())]
    if active:
        months = [m for m in months if active[0] <= m <= active[-1]]

    cogs_map, prep_map, goods, missing_costs, missing_prep, covered = apply_costs(
        entities, costs, preps, set(months))
    retro = read_retro(HERE / RETRO_FILE)
    retro_map = apply_retro(entities, costs, retro, catalog, set(months))
    rates = tax_rates(MARKETPLACES)

    # Прогноз на конец текущего месяца (недели) — если последний период отчёта ещё не закончился
    fc_series, fc_args = None, None
    today = date.today()
    if args.grain in ("month", "week") and not args.only_unit:
        if args.grain == "month" and months[-1] == f"{today:%Y-%m}":
            start = today.replace(day=1)
            end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
            step("Прогноз на конец месяца: дневные данные маркетплейсов из кэша")
            day_dumps = load_daily_dumps(args, fc_first_day)
            fc_args = (start, end, FORECAST_WINDOW["month"])
        elif args.grain == "week" and months[-1] == week_key(today.isoformat()):
            start = date.fromisoformat(months[-1])
            fc_args = (start, start + timedelta(days=6), FORECAST_WINDOW["week"])
        if fc_args and day_dumps:
            fc_series = forecast_series(day_dumps, costs, preps, rates, retro, catalog)

    def forecast_for(keys):
        return period_forecast(fc_series, keys, *fc_args) if fc_series else None

    tax_map, tax_used = apply_tax(entities, rates, set(months))
    if costs:
        print(f"\nСебестоимость: {len(costs)} артикулов в {COST_FILE}")
    else:
        print(f"\n{COST_FILE} не найден — отчёт без себестоимости")
    if preps:
        print(f"Подготовка товара: {len(preps)} артикулов в {COST_FILE}")
    elif costs:
        print(f"В {COST_FILE} не заполнена колонка {PREP_COL} — статья «Подготовка товара» пустая")
    if retro:
        total = sum(v for vals in retro_map.values() for v in vals.values())
        print(f"Ретро-бонусы: {len(retro)} брендов в {RETRO_FILE}, за период " + f"{total:,.0f} ₽".replace(",", " "))
    if tax_used:
        print("Налог с фактической цены покупки: " + ", ".join(f"{mp} / {cab} — {rate:g}%" for (mp, cab), rate in tax_used.items()))
    else:
        print("Ставка налога не задана (tax_percent в конфиге маркетплейса) — статья «Налог с выручки» пустая")

    # Таблицы по маркетплейсам (строки — исходные, с префиксом кабинета, если кабинетов несколько)
    by_mp = defaultdict(list)
    for e in entities:
        by_mp[e["mp"]].append(e)
    per_mp, per_mp_info = {}, {}
    for mp, ents in by_mp.items():
        if len(ents) == 1:
            per_mp[mp] = ents[0]["table"]
        else:
            per_mp[mp] = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
            for e in ents:
                for a, rows in e["table"].items():
                    for label, vals in rows.items():
                        for m, v in vals.items():
                            per_mp[mp][a][f"{e['cabinet']}: {label}"][m] += v
        info = defaultdict(lambda: defaultdict(float))
        for e in ents:
            for label, vals in e["info"].items():
                for m, v in vals.items():
                    info[label if len(ents) == 1 else f"{e['cabinet']}: {label}"][m] += v
        per_mp_info[mp] = info

    mp_names = list(per_mp)
    summary = merge_tables([per_mp[n] for n in mp_names], detail_key=lambda i: mp_names[i])

    def cogs_for(pairs):
        out = defaultdict(float)
        for key in pairs:
            for mon, value in cogs_map.get(key, {}).items():
                out[mon] += value
        return dict(out) if out else None

    def sum_by(source, pairs):
        out = defaultdict(float)
        for key in pairs:
            for mon, value in source.get(key, {}).items():
                out[mon] += value
        return dict(out)

    cogs_by_mp = {mp: sum_by(cogs_map, [(mp, e["cabinet"]) for e in ents]) for mp, ents in by_mp.items()}
    prep_by_mp = {mp: sum_by(prep_map, [(mp, e["cabinet"]) for e in ents]) for mp, ents in by_mp.items()}
    tax_by_mp = {mp: sum_by(tax_map, [(mp, e["cabinet"]) for e in ents]) for mp, ents in by_mp.items()}
    retro_by_mp = {mp: sum_by(retro_map, [(mp, e["cabinet"]) for e in ents]) for mp, ents in by_mp.items()}
    retro_all = sum_by(retro_map, list(retro_map)) or None
    cogs_all = cogs_for(list(cogs_map))
    prep_all = sum_by(prep_map, list(prep_map)) or None
    tax_all = sum_by(tax_map, list(tax_map)) or None
    # Маркетплейсы, где себестоимость покрыта не полностью: валовая прибыль по ним завышена
    uncovered = []
    for mp, ents in by_mp.items():
        total = sum(covered.get((mp, e["cabinet"]), [0.0, 0.0])[1] for e in ents)
        with_cost = sum(covered.get((mp, e["cabinet"]), [0.0, 0.0])[0] for e in ents)
        if total and with_cost / total < 0.999:
            uncovered.append(f"{mp} — {with_cost / total:.1%} выручки")
    cogs_note = (" ВНИМАНИЕ: себестоимость есть не по всем товарам (" + "; ".join(uncovered) +
                 "), валовая прибыль завышена.") if uncovered else ""

    wb = Workbook()
    used = set()
    ws = wb.active
    ws.title = safe_name("Свод", used)
    step("Лист «Свод» и «Сравнение МП»")
    period = period_range(months, GRAIN)
    grain_txt = {"month": "", "week": " по неделям", "day": " по дням", "unitweek": " по неделям"}[GRAIN]
    if GRAIN == "week":
        period += ". Недели пн–вс: первая и последняя могут захватывать дни соседних месяцев"
    cabinets_txt = "; ".join(f"{mp}: {', '.join(e['cabinet'] for e in ents)}" for mp, ents in by_mp.items())
    summary_fc = forecast_for([(e["mp"], e["cabinet"]) for e in entities])
    write_pnl(ws, summary, months, f"ОПиУ маркетплейсов — свод{grain_txt}",
              f"{period}. Кабинеты — {cabinets_txt}.{cogs_note}",
              detail_order=lambda rows: [n for n in mp_names if n in rows], cogs=cogs_all,
              prep=prep_all, tax=tax_all, forecast=summary_fc, retro=retro_all)
    write_comparison(wb.create_sheet(safe_name("Сравнение МП", used)), per_mp, months, cogs_by_mp,
                     prep_by_mp, tax_by_mp, retro_by_mp)

    unified_net = {}
    for mp in mp_names:
        ents = by_mp[mp]
        title = mp if len(ents) > 1 else f"{mp} — {ents[0]['cabinet']}"
        step(f"Листы ОПиУ: {mp}" + (f" (кабинетов: {len(ents)})" if len(ents) > 1 else ""))
        net = write_pnl(wb.create_sheet(safe_name(mp, used)), per_mp[mp], months, f"ОПиУ {title}{grain_txt}",
                        f"{period}. Строки — исходные статьи отчёта маркетплейса", info=per_mp_info[mp],
                        cogs=cogs_by_mp.get(mp) or None, prep=prep_by_mp.get(mp) or None,
                        tax=tax_by_mp.get(mp) or None, forecast=forecast_for([(mp, e["cabinet"]) for e in ents]),
                        retro=retro_by_mp.get(mp) or None)
        if len(ents) == 1:
            unified_net[(mp, ents[0]["cabinet"])] = net
        else:
            for e in ents:
                unified_net[(mp, e["cabinet"])] = write_pnl(
                    wb.create_sheet(safe_name(f"{mp[:10]} {e['cabinet']}", used)), e["table"], months,
                    f"ОПиУ {mp} — {e['cabinet']}{grain_txt}",
                    f"{period}. Строки — исходные статьи отчёта маркетплейса",
                    info=e["info"], cogs=cogs_map.get((mp, e["cabinet"])) or None,
                    prep=prep_map.get((mp, e["cabinet"])) or None, tax=tax_map.get((mp, e["cabinet"])) or None,
                    forecast=forecast_for([(mp, e["cabinet"])]), retro=retro_map.get((mp, e["cabinet"])) or None)
    net_by_month = dict(unified_net)   # (МП, кабинет) -> {месяц: итог} — для юнит-экономики за выбранный месяц
    unified_net = {k: sum(v.values()) for k, v in unified_net.items()}

    step("Себестоимость по заказам")
    orders, order_totals = order_costs(entities, costs, set(months))
    if orders and not args.only_unit:
        write_order_costs(wb.create_sheet(safe_name("Себестоимость по заказам", used)), orders, cost_names)
    step("Листы «Товары», «Сопоставление статей», «Контроль»")
    if goods and not args.only_unit:
        write_goods(wb.create_sheet(safe_name("Товары", used)), goods, cost_names, months)
    write_mapping(wb.create_sheet(safe_name("Сопоставление статей", used)), mapping)
    problems = write_control(wb.create_sheet(safe_name("Контроль", used)), entities, notes, months, unified_net,
                             covered, missing_costs, bool(costs), tax_used, prep_map, missing_prep, order_totals,
                             cogs_map, script_errors)

    # Один файл пропусков: артикул попадает туда, если у него нет себестоимости или подготовки.
    # Уже известные цены подставлены, чтобы было видно, какую колонку осталось заполнить.
    # Пропуски цен считаются за календарные месяцы — при разрезе по неделям и дням файл не трогаем
    miss_file = HERE / COST_MISSING_FILE if args.grain == "month" else None
    known = defaultdict(dict)
    for row in read_price_rows(HERE / COST_FILE):
        entry = known[art_key(row.get("артикул"))]
        for col in ("кратность", COST_COL, PREP_COL, BRAND_COL, CATEGORY_COL):
            if row.get(col):
                entry[col] = row[col]
    if miss_file and miss_file.exists():
        lost = [r for r in read_price_rows(miss_file) for col in (COST_COL, PREP_COL)
                if r.get(col) and r[col] != known[art_key(r.get("артикул"))].get(col)]
        if lost:   # заполненные цены не должны пропасть ни при каких условиях
            backup = miss_file.with_name(f"{miss_file.stem}_backup_{datetime.now():%Y%m%d_%H%M%S}.csv")
            backup.write_bytes(miss_file.read_bytes())
            print(f"  В {miss_file.name} есть цены, не перенесённые в {COST_FILE} (в cost.csv уже стоит другое "
                  f"значение) — копия файла: {backup.name}")
    miss_rows = {**missing_prep, **missing_costs} if miss_file else {}
    if not miss_rows:
        if miss_file and miss_file.exists():
            miss_file.unlink()   # всё заполнено — файл больше не нужен
    else:
        with open(miss_file, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh, delimiter=";")
            writer.writerow(["артикул", "кратность", COST_COL, PREP_COL, "действует_с", BRAND_COL, CATEGORY_COL,
                             "маркетплейс (справочно)", "продано шт. (справочно)", "выручка (справочно)",
                             "средняя цена продажи (справочно)"])
            for (mp, article), (qty, revenue) in sorted(miss_rows.items(), key=lambda kv: -kv[1][1]):
                have = known.get(art_key(article), {})
                writer.writerow([article, have.get("кратность", 1), have.get(COST_COL, ""), have.get(PREP_COL, ""),
                                 "", have.get(BRAND_COL, ""), have.get(CATEGORY_COL, ""),
                                 mp, round(qty), round(revenue, 2), round(revenue / qty, 2) if qty else ""])
        print(f"  Артикулы без цены ({len(miss_rows)}) выписаны в {miss_file.name} — заполните пустые ячейки, "
              f"при следующем запуске они переедут в {COST_FILE}")

    # Имя сводного файла нужно и при --only-unit: от него называется файл юнит-экономики при --out
    if args.out:
        out = Path(args.out)
    elif args.grain == "week":
        # Имя — по месяцам свода, а не по понедельнику первой недели (он может быть в прошлом месяце)
        last = (date.fromisoformat(months[-1]) + timedelta(days=6)).strftime("%Y-%m")
        out = HERE / f"opiu_weekly_{args.date_from}_{last}.xlsx"
    elif args.grain == "day":
        out = HERE / f"opiu_daily_{args.period_month}.xlsx"
    else:
        out = HERE / f"opiu_all_{months[0]}_{months[-1]}.xlsx"
    if args.only_unit:
        step("Сводный ОПиУ не сохраняю (--only-unit) — сверки по нему всё равно проверены")
    else:
        step(f"Сохраняю Excel ({len(orders):,} строк по заказам — это может занять несколько минут)..."
             .replace(",", " "))
        out = save_workbook(wb, out)
        step(f"Excel сохранён: {out}")
    del wb

    unit_out = None
    unit_months = [args.unit_month] if args.unit_month else months
    if not args.no_unit and args.unit_month and args.unit_month not in months:
        print(f"\nЮнит-экономика не собрана: за {args.unit_month} нет данных в своде "
              f"(есть {months[0]} — {months[-1]})")
        problems.append(("—", "—", "Юнит-экономика", f"нет данных за {args.unit_month}", "НЕТ ДАННЫХ"))
    elif not args.no_unit:
        step(f"Юнит-экономика по артикулам ({month_title(unit_months[0])}"
             + (f" — {month_title(unit_months[-1])}" if len(unit_months) > 1 else "") + "): реестр")
        rows = unit_registry(entities, costs, preps, rates, set(unit_months), cost_names, retro, catalog)
        expected = {}
        for e in entities:
            key = (e["mp"], e["cabinet"])
            expected[key] = sum(v for m, v in net_by_month.get(key, {}).items() if m in unit_months) + sum(
                v for src in (cogs_map, retro_map, prep_map, tax_map) for m, v in src.get(key, {}).items()
                if m in unit_months)
        unit_period = (month_title(unit_months[0]) if len(unit_months) == 1
                       else f"{month_title(unit_months[0])} — {month_title(unit_months[-1])}")
        if GRAIN == "unitweek":
            unit_period = period_range(unit_months, GRAIN)
            unit_wb, unit_problems = build_unit_week_workbook(rows, entities, expected, set(unit_months),
                                                              unit_period, catalog, costs, cost_names, preps)
        else:
            unit_wb, unit_problems = build_unit_workbook(rows, entities, expected, set(unit_months), unit_period,
                                                         catalog, costs, cost_names, preps)
        problems += [(mp, cab, f"Юнит-экономика: {title}", value, status)
                     for mp, cab, title, value, status in unit_problems]
        suffix = unit_months[0] if len(unit_months) == 1 else f"{unit_months[0]}_{unit_months[-1]}"
        if GRAIN == "unitweek":
            suffix = f"weekly_{unit_months[0][:7]}_{unit_months[-1][:7]}"
        unit_out = (out.with_name(f"{out.stem}_unit_{suffix}{out.suffix}") if args.out
                    else HERE / f"unit_economics_{suffix}.xlsx")
        step(f"Сохраняю юнит-экономику ({len(rows):,} строк реестра)...".replace(",", " "))
        unit_out = save_workbook(unit_wb, unit_out)
        step(f"Юнит-экономика сохранена: {unit_out}")
    for e in entities:
        own = sum(v for m, v in e["net"].items() if m in months)
        diff = unified_net[(e["mp"], e["cabinet"])] - own
        cogs_sum = sum(cogs_map.get((e["mp"], e["cabinet"]), {}).values())
        prep_sum = sum(prep_map.get((e["mp"], e["cabinet"]), {}).values())
        tax_sum = sum(tax_map.get((e["mp"], e["cabinet"]), {}).values())
        retro_sum = sum(retro_map.get((e["mp"], e["cabinet"]), {}).values())
        print(f"  {e['mp']} / {e['cabinet']}: итог {own:,.0f} ₽, себестоимость {-cogs_sum:,.0f} ₽, "
              + (f"ретро-бонусы {retro_sum:,.0f} ₽, " if retro_sum else "")
              + f"подготовка {-prep_sum:,.0f} ₽, налог {-tax_sum:,.0f} ₽, "
              f"прибыль {own + cogs_sum + retro_sum + prep_sum + tax_sum:,.0f} ₽ (расхождение с отчётом МП {diff:,.2f} ₽)")
    if uncovered:
        print("  Себестоимость не по всем товарам: " + "; ".join(uncovered))
    if summary_fc:
        v = summary_fc["values"]
        rub = {k: f"{v[k]:,.0f} ₽".replace(",", " ") for k in ("revenue", "net", "profit")}
        rub["cogs"] = f"{-v['cogs']:,.0f} ₽".replace(",", " ")
        print(f"\n  {summary_fc['title']} (все МП): выручка {rub['revenue']}, результат МП {rub['net']}, "
              f"себестоимость {rub['cogs']}, прибыль {rub['profit']}, маржинальность {v['margin']:.1%}")
    print_summary(out, problems, [k for k in mapping if k[4]], unit_out, not args.only_unit)
    if args.open and hasattr(os, "startfile"):
        if not args.only_unit:
            os.startfile(out)
        if unit_out:
            os.startfile(unit_out)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nПрервано пользователем. Отчёт не собран.")
    except SystemExit as e:
        if isinstance(e.code, str):   # sys.exit("причина") внутри main
            print(f"{e.code}\n\n" + "=" * 70 + "\nОШИБКА: отчёт не собран.\n" + "=" * 70)
            sys.exit(1)
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        print("\n" + "=" * 70 + "\nОШИБКА: отчёт не собран (подробности выше).\n" + "=" * 70)
        sys.exit(1)
