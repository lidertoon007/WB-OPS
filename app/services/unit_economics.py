"""Read the per-cabinet unit economics workbook produced by opiu_all.py."""
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime
from functools import lru_cache
from math import floor, fsum, isfinite
from pathlib import Path
import re
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from app.services.finance import FinanceError
from app.services.finance_worker import UNIT_REPORT_NAME

MARKETPLACE, CABINET, SKU, NAME, BRAND, CATEGORY = (
    "Маркетплейс", "Кабинет", "Артикул", "Название", "Бренд", "Категория",
)
IDENTITY = (MARKETPLACE, CABINET, SKU, NAME, BRAND, CATEGORY, "Год", "Месяц", "Неделя (ISO)", "Даты")
REVENUE, EXPENSES, PROFIT, NET_QTY = "Выручка нетто, ₽", "Расходы МП итого, ₽", "Прибыль, ₽", "Нетто, шт"
SHOWS, CLICKS, CART = "Показы", "Переходы в карточку", "В корзину, шт"
ORDERS, BUYOUT = "Заказано (воронка), шт", "Выкуплено (воронка), шт"
ORDER_QTY, ORDER_DAY, STOCK = "Заказано, шт", "Заказов в день, шт", "Остаток всего, шт"
SOLD, RETURNED, SALES, REFUNDS = "Продано, шт", "Возвращено, шт", "Продажи, ₽", "Возвраты, ₽"
PAYOUT, ORDER_SUM, ORDER_CANCEL = "К перечислению от МП, ₽", "Заказано, ₽", "Отменено, шт"
CTR_CLICKS = "Переходы при известных показах (для CTR)"
SHOW_ORDERS = "Заказано при известных показах (для сквозной конверсии)"
CART_CLICKS = "Переходы при известной корзине (для конверсии в корзину)"
CART_ORDERS = "Заказано при известной корзине (для конверсии в заказ)"
FINISHED = "Завершённые заказы: выкуплено + отменено + возвращено (воронка)"
NO_ARTICLE = "Без артикула"
MONTH = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])")

GROUPS = {
    "finance": ("Кратность на МП, шт", "Продано, шт", "Возвращено, шт", NET_QTY, "Продажи, ₽", "Возвраты, ₽",
                REVENUE, "Комиссия", "Логистика", "Обратная логистика", "Эквайринг", "Реклама", "Хранение",
                "Штрафы", "Прочие услуги", EXPENSES, "Расходы МП, % выручки", "Компенсации", "Авансы",
                "К перечислению от МП, ₽", "Себестоимость", "Ретро-бонус", "Подготовка", "Налог",
                PROFIT, "Маржа, % выручки"),
    "per_unit": ("Цена за 1 шт", "Расходы МП на 1 шт", "Себестоимость на 1 шт", "Прибыль на 1 шт"),
    "prices": ("Цена до скидки, ₽", "Скидка, %", "Текущая цена, ₽", "Цена с акциями, ₽"),
    "funnel": (SHOWS, CLICKS, "CTR, %", CART, "Карточка → корзина, %", ORDERS, "Корзина → заказ, %",
               "Карточка → заказ, %", "Сквозная конверсия (показ → заказ), %", BUYOUT,
               "Выкуп, % заказов", "Выкуп по завершённым, %", "Отмены, % заказов", "Возвраты, % продаж"),
    "inventory": (ORDER_QTY, "Заказано, ₽", "Отменено, шт", ORDER_DAY, "На складах МП, шт",
                  "На своём складе, шт", STOCK, "Остаток по себестоимости, ₽", "Запас, дней",
                  "В пути к покупателям, шт", "Возвраты в пути, шт"),
}
VISIBLE_GROUPS = {key: "identity" for key in IDENTITY} | {
    key: group for group, keys in GROUPS.items() for key in keys
}

# Each ratio uses the same numerator and denominator as the Excel subtotal row.
RATIOS = {
    "Расходы МП, % выручки": (EXPENSES, REVENUE),
    "Маржа, % выручки": (PROFIT, REVENUE),
    "Цена за 1 шт": (REVENUE, NET_QTY),
    "Расходы МП на 1 шт": (EXPENSES, NET_QTY),
    "Себестоимость на 1 шт": ("Себестоимость", NET_QTY),
    "Прибыль на 1 шт": (PROFIT, NET_QTY),
    "Цена до скидки, ₽": ("Сумма цен до скидки (для средней)", "Число цен до скидки (для средней)"),
    "Текущая цена, ₽": ("Сумма текущих цен (для средней)", "Число текущих цен (для средней)"),
    "Цена с акциями, ₽": ("Сумма цен с акциями (для средней)", "Число цен с акциями (для средней)"),
    "Скидка, %": ("Сумма текущих цен при известной цене до скидки (для скидки)", "Сумма цен до скидки (для средней)"),
    "CTR, %": (CTR_CLICKS, SHOWS),
    "Карточка → корзина, %": (CART, CART_CLICKS),
    "Корзина → заказ, %": (CART_ORDERS, CART),
    "Карточка → заказ, %": (ORDERS, CLICKS),
    "Сквозная конверсия (показ → заказ), %": (SHOW_ORDERS, SHOWS),
    "Выкуп, % заказов": (BUYOUT, ORDERS),
    "Выкуп по завершённым, %": (BUYOUT, FINISHED),
    "Отмены, % заказов": (ORDER_CANCEL, ORDER_QTY),
    "Возвраты, % продаж": (RETURNED, SOLD),
    "Запас, дней": (STOCK, ORDER_DAY),
}
RATIO_INPUTS = {key for pair in RATIOS.values() for key in pair}
NO_SUM = set(IDENTITY) | set(RATIOS) | {"Кратность на МП, шт"}
SHEET_NAMES = ("Юнит-экономика", "Юнит по неделям")
ITEM_FIELDS = (MARKETPLACE, CABINET, SKU)
MP_COSTS = {
    "Комиссия маркетплейса": "Комиссия",
    "Логистика и доставка": "Логистика",
    "Обратная логистика, отмены и невыкупы": "Обратная логистика",
    "Эквайринг и платёжные услуги": "Эквайринг",
    "Реклама и продвижение": "Реклама",
    "Хранение и складские операции": "Хранение",
    "Штрафы": "Штрафы",
    "Подписки и прочие услуги": "Прочие услуги",
}
PAYOUT_ADJUSTMENTS = {"Компенсации и прочие доходы": "Компенсации", "Авансы за услуги (списано − возвращено)": "Авансы"}
SELLER_COSTS = {"Себестоимость проданных товаров": "Себестоимость", "Ретро-бонусы производителей": "Ретро-бонус",
                "Подготовка товара": "Подготовка", "Налог с выручки": "Налог"}
ARTICLES = {"Продажи (по цене продавца)": SALES, "Возвраты": REFUNDS, SOLD: SOLD, RETURNED: RETURNED,
            **MP_COSTS, **PAYOUT_ADJUSTMENTS, **SELLER_COSTS}
FUNNEL_FIELDS = {SHOWS: SHOWS, CLICKS: CLICKS, "В корзину": CART, ORDER_QTY: ORDERS, "Выкуплено, шт": BUYOUT}
SNAPSHOT_COLUMNS = set(GROUPS["prices"]) | set(GROUPS["inventory"][4:])
SNAPSHOT_FIELDS = ((SNAPSHOT_COLUMNS - {"Запас, дней"}) | {"Кратность на МП, шт"}
                   | {key for col in GROUPS["prices"] for key in RATIOS[col]})


def validate_period(date_from: str | None, date_to: str | None) -> None:
    for value in (date_from, date_to):
        if value is not None and (not MONTH.fullmatch(value) or value.startswith("0000")):
            raise ValueError("Укажите месяц в формате ГГГГ-ММ.")
    if date_from and date_to and date_from > date_to:
        raise ValueError("Начало периода должно быть не позже конца.")


def _months_between(first, last):
    start_year, start_month = map(int, first.split("-"))
    end_year, end_month = map(int, last.split("-"))
    return [f"{i // 12:04}-{i % 12 + 1:02}"
            for i in range(start_year * 12 + start_month - 1, end_year * 12 + end_month)]


def latest_report(project: Path) -> Path | None:
    candidates = []
    for path in project.glob("unit_economics_*.xlsx"):
        if UNIT_REPORT_NAME.fullmatch(path.name) and not path.is_symlink() and path.is_file():
            try:
                candidates.append((path.stat().st_mtime_ns, path.name, path))
            except FileNotFoundError:
                continue
    return max(candidates)[-1] if candidates else None


def _cell(key, value):
    if key in IDENTITY:
        return str(value).strip() if value is not None else ""
    if type(value) in (int, float) and isfinite(value):
        return value
    return None


@lru_cache(maxsize=2)
def _read(path: Path, inode: int, modified_ns: int, size: int):
    book = load_workbook(path, read_only=True, data_only=True, keep_links=False)
    try:
        sheet = next((book[name] for name in SHEET_NAMES if name in book.sheetnames), None)
        if sheet is None:
            raise FinanceError("В Excel нет листа с фактической юнит-экономикой.")
        source = sheet.iter_rows(min_row=3, values_only=True)
        raw_headers = next(source, ())
        headers = tuple((i, key) for i, key in enumerate(raw_headers) if isinstance(key, str)
                        and (key in VISIBLE_GROUPS or key in RATIO_INPUTS))
        index = {key: i for i, (_, key) in enumerate(headers)}
        if not {MARKETPLACE, CABINET, SKU} <= index.keys():
            raise FinanceError("На листе юнит-экономики отсутствуют маркетплейс, кабинет или артикул.")
        columns = [{"key": key, "group": VISIBLE_GROUPS[key], "snapshot": key in SNAPSHOT_COLUMNS,
                    "format": "text" if key in IDENTITY else "percent" if "%" in key else "number"}
                   for _, key in headers if key in VISIBLE_GROUPS]
        rows = [tuple(_cell(key, row[i]) for i, key in headers)
                for row in source if row[headers[index[SKU]][0]] not in (None, "")]
        period_filter = sheet.title == "Юнит-экономика" and "Реестр" in book.sheetnames
        return columns, index, rows, period_filter
    finally:
        book.close()


def _ratios(sums, *, row=False):
    ratios = {}
    for key, (numerator, denominator) in RATIOS.items():
        value, divisor = sums.get(numerator), sums.get(denominator)
        ratio = value / divisor if value is not None and divisor else None
        if row and denominator == NET_QTY and divisor and divisor < 0:
            ratio = None
        if ratio is not None:
            if key == "Расходы МП, % выручки":
                ratio = -ratio
            elif key == "Скидка, %":
                ratio = 1 - ratio
            elif key == "Запас, дней":
                ratio = floor(ratio + 0.5)
        ratios[key] = ratio
    return ratios


def _totals(rows, index):
    def column_sum(key):
        i = index[key]
        values = [row[i] for row in rows if row[i] is not None]
        return fsum(values) if values else None

    sums = {key: column_sum(key) for key in index if key in RATIO_INPUTS or key not in NO_SUM}
    return sums | _ratios(sums)


def _detail_rows(sheet, header_row, fields):
    source = sheet.iter_rows(min_row=header_row, values_only=True)
    index = {key: i for i, key in enumerate(next(source, ())) if isinstance(key, str)}
    axes = (*ITEM_FIELDS, "Месяц")
    if not set(axes) <= index.keys():
        raise FinanceError(f"На листе «{sheet.title}» нет колонок для выбора периода.")
    positions = [(key, index[key]) for key in fields if key in index]
    for row in source:
        market, cabinet, sku, month = (_cell(key, row[index[key]]) for key in axes)
        if market and cabinet and sku and MONTH.fullmatch(month):
            yield month, (market, cabinet, sku), {key: row[i] for key, i in positions}


@lru_cache(maxsize=2)
def _read_period_inputs(path: Path, inode: int, modified_ns: int, size: int):
    book = load_workbook(path, read_only=True, data_only=True, keep_links=False)
    months = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    try:
        for month, item, values in _detail_rows(book["Реестр"], 1, ("Статья", "Сумма, ₽", "Кол-во, шт")):
            metric = ARTICLES.get(values.get("Статья"))
            amount = _cell("Сумма, ₽", values.get("Сумма, ₽"))
            value = _cell(metric, values.get("Кол-во, шт")) if metric in (SOLD, RETURNED) else amount
            if metric and value is not None and (item[2] != NO_ARTICLE or value != 0):
                months[month][item][metric] += -value if metric == RETURNED else value

        if "Заказы" in book.sheetnames:
            for month, item, values in _detail_rows(book["Заказы"], 1, (ORDER_QTY, ORDER_SUM, ORDER_CANCEL)):
                if item[2] == NO_ARTICLE and item not in months[month]:
                    continue
                for key, v in values.items():
                    value = _cell(key, v)
                    if value is not None:
                        months[month][item][key] += value

        if "Воронка" in book.sheetnames:
            fields = (*FUNNEL_FIELDS, ORDER_CANCEL, RETURNED)
            for month, item, values in _detail_rows(book["Воронка"], 3, fields):
                if item[2] == NO_ARTICLE and item not in months[month]:
                    continue
                agg = months[month][item]
                data = {key: _cell(key, values.get(key)) for key in fields}
                for key, metric in FUNNEL_FIELDS.items():
                    if data[key] is not None:
                        agg[metric] += data[key]
                if data[SHOWS] is not None:
                    agg[CTR_CLICKS] += data[CLICKS] or 0
                    agg[SHOW_ORDERS] += data[ORDER_QTY] or 0
                if data["В корзину"] is not None:
                    agg[CART_CLICKS] += data[CLICKS] or 0
                    agg[CART_ORDERS] += data[ORDER_QTY] or 0
                agg[FINISHED] += sum(data[key] or 0 for key in ("Выкуплено, шт", ORDER_CANCEL, RETURNED))
        return months
    finally:
        book.close()


def _period_rows(rows, index, inputs, first, last, built_on):
    start = date.fromisoformat(first + "-01")
    year, month = map(int, last.split("-"))
    end = min(date(year, month, monthrange(year, month)[1]), built_on)
    days = max(0, (end - start).days + 1)
    selected = defaultdict(lambda: defaultdict(float))
    for month, items in inputs.items():
        if first <= month <= last:
            for item, amounts in items.items():
                for key, value in amounts.items():
                    selected[item][key] += value
    result = []
    for base in rows:
        item = tuple(base[index[key]] for key in ITEM_FIELDS)
        if item not in selected:
            continue
        values = selected[item]
        for key in ARTICLES.values():
            values.setdefault(key, 0)
        values[NET_QTY] = values[SOLD] - values[RETURNED]
        values[REVENUE] = values[SALES] + values[REFUNDS]
        values[EXPENSES] = fsum(values[key] for key in MP_COSTS.values())
        values[PAYOUT] = values[REVENUE] + values[EXPENSES] + fsum(values[key] for key in PAYOUT_ADJUSTMENTS.values())
        values[PROFIT] = values[PAYOUT] + fsum(values[key] for key in SELLER_COSTS.values())
        if days and ORDER_QTY in values:
            values[ORDER_DAY] = (values[ORDER_QTY] - values.get(ORDER_CANCEL, 0)) / days
        for key in SNAPSHOT_FIELDS:
            if key in index:
                values[key] = base[index[key]]
        ratios = {key: ratio for key, ratio in _ratios(values, row=True).items() if key not in SNAPSHOT_FIELDS}
        values.update(ratios)
        result.append(tuple(base[i] if key in IDENTITY else values.get(key) for key, i in index.items()))
    return result


def report_view(path: Path, marketplace: str = "", cabinet: str = "", brand: str = "",
                category: str = "", q: str = "", page: int = 1, page_size: int = 50,
                date_from: str | None = None, date_to: str | None = None) -> dict:
    validate_period(date_from, date_to)
    first_month, last_month = UNIT_REPORT_NAME.fullmatch(path.name).groups()
    last_month = last_month or first_month
    try:
        stamp = path.stat()
        cache_key = (path, stamp.st_ino, stamp.st_mtime_ns, stamp.st_size)
        built_at = datetime.fromtimestamp(stamp.st_mtime).astimezone()
        columns, index, rows, period_filter = _read(*cache_key)
        option_rows = rows
        period_filtered = False
        if period_filter and (date_from or date_to):
            first = max(first_month, date_from or first_month)
            last = min(last_month, date_to or last_month)
            period_filtered = (first, last) != (first_month, last_month)
            if period_filtered:
                if first <= last:
                    rows = _period_rows(rows, index, _read_period_inputs(*cache_key), first, last, built_at.date())
                    option_rows = rows
                else:
                    rows = []
    except (BadZipFile, InvalidFileException, KeyError, OSError, ValueError) as exc:
        raise FinanceError("Не удалось прочитать Excel юнит-экономики. Соберите его заново.") from exc

    def cell(row, name):
        return row[index[name]] if name in index else ""

    def options(key):
        return sorted({cell(row, key) for row in option_rows} - {""}, key=str.casefold)

    cabins = {(cell(row, MARKETPLACE), cell(row, CABINET)) for row in option_rows
              if cell(row, CABINET) and (not marketplace or cell(row, MARKETPLACE) == marketplace)}
    filters = {"marketplace": options(MARKETPLACE), "brand": options(BRAND), "category": options(CATEGORY),
               "cabinet": [{"value": f"{mp}|{name}", "label": name if marketplace else f"{name} · {mp}"}
                           for mp, name in sorted(cabins, key=lambda item: (item[1].casefold(), item[0].casefold()))]}
    query = q.strip().casefold()
    matching = [row for row in rows if (not marketplace or cell(row, MARKETPLACE) == marketplace)
                and (not cabinet or f"{cell(row, MARKETPLACE)}|{cell(row, CABINET)}" == cabinet)
                and (not brand or cell(row, BRAND) == brand) and (not category or cell(row, CATEGORY) == category)
                and (not query or query in cell(row, SKU).casefold() or query in cell(row, NAME).casefold())]

    page = min(page, max(1, (len(matching) + page_size - 1) // page_size))
    start = (page - 1) * page_size
    totals = _totals(matching, index)
    filter_notice = ("Для недельного отчёта выбор периода недоступен." if path.name.startswith("unit_economics_weekly_")
                     else "В файле нет помесячной детализации.")
    return {"file": path.name, "period": first_month if last_month == first_month else f"{first_month} — {last_month}",
            "available_periods": _months_between(first_month, last_month) if period_filter else [],
            "active_period": {"date_from": date_from if period_filter else None, "date_to": date_to if period_filter else None},
            "period_filter": period_filter, "period_filtered": period_filtered,
            "period_filter_notice": None if period_filter else filter_notice,
            "updated_at": built_at.isoformat(timespec="minutes"),
            "columns": columns, "filters": filters, "total_rows": len(matching), "page": page, "page_size": page_size,
            "totals": [totals.get(column["key"]) for column in columns],
            "rows": [[row[index[column["key"]]] for column in columns] for row in matching[start:start + page_size]]}
