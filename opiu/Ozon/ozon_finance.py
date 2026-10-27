"""
Финансовый отчёт Ozon по месяцам для нескольких кабинетов.

Источник данных (Seller API, актуальные методы после отключения
/v3/finance/transaction/list 06.07.2026):
  POST /v1/finance/accrual/by-day  — начисления за один день, пагинация по last_id
  POST /v1/finance/accrual/types   — справочник видов начислений (type_id -> название)

Результат: один Excel-файл
  - «Итого»        — сводный ОПиУ по всем магазинам, месяцы в колонках
  - «По магазинам» — ключевые показатели каждого магазина по месяцам
  - лист на каждый магазин — такой же ОПиУ, как «Итого»
  - «Начисления»   — все начисления построчно (для сверки и фильтров)
  - «Виды начислений» — справочник Ozon и группа, в которую попал каждый вид

Запуск:
  python ozon_finance.py                          # период из config / последние 12 мес.
  python ozon_finance.py --from 2026-01 --to 2026-08
  python ozon_finance.py --no-raw --out отчёт.xlsx
  python ozon_finance.py --probe 2026-08-15       # сохранить сырой ответ API за день для проверки

Ответы за прошедшие дни кэшируются в папке cache/ — повторный запуск быстрый.
Обновить кэш принудительно: --refresh

Нужен пакет openpyxl:  pip install openpyxl
ВАЖНО: API Ozon не отвечает на зарубежные IP — отключите VPN перед запуском.
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

API_HOST = "https://api-seller.ozon.ru"
URL_BY_DAY = "/v1/finance/accrual/by-day"
URL_TYPES = "/v1/finance/accrual/types"
URL_ROLES = "/v1/roles"  # роли и срок действия API-ключа
KEY_WARN_DAYS = 14       # предупреждать о перевыпуске ключа за столько дней
CACHE_AFTER_DAYS = 7  # дни старше недели считаем закрытыми и кэшируем
# Заказы и остатки для юнит-экономики
# /v2/posting/fbo/list и /v3/posting/fbs/list отключены (obsolete / 404): курсорная пагинация, limit ≤ 100
URL_FBO_LIST = "/v3/posting/fbo/list"
URL_FBS_LIST = "/v4/posting/fbs/list"
POSTINGS_LIMIT = 100
URL_STOCKS = "/v4/product/info/stocks"
URL_PRICES = "/v5/product/info/prices"
URL_ATTRIBUTES = "/v4/product/info/attributes"
# Кратность продажи из характеристик товара: «Единиц в одном товаре» (набор 15 шт по 80 г — 15),
# если не заполнена — «Количество заводских упаковок»
MULTIPLE_ATTRIBUTES = [(8962, "Единиц в одном товаре"), (11650, "Количество заводских упаковок")]
ORDERS_CLOSED_DAYS = 45   # отправления месяца, закончившегося раньше, уже не меняют статус — берутся из кэша
# Вид остатка (одинаковые коды во всех скриптах МП): mp — на складе маркетплейса, own — на своём складе
STOCK_PLACES = {"fbo": "Склады Ozon (FBO)", "fbs": "Свой склад (FBS)", "rfbs": "Свой склад (rFBS)",
                "fbp": "Склад партнёра (FBP)"}
# Воронка продаж по товарам: /v1/analytics/data, группировка SKU × месяц. Порядок метрик = порядок колонок воронки
# (показы, переходы в карточку, корзина, заказано шт, заказано ₽, доставлено, отменено, возвращено).
URL_ANALYTICS = "/v1/analytics/data"
FUNNEL_METRICS = ["hits_view", "hits_view_pdp", "hits_tocart", "ordered_units", "revenue", "delivered_units",
                  "cancellations", "returns"]
# Часть метрик Ozon отдаёт только с подпиской Premium — при отказе пробуем наборы поменьше
FUNNEL_FALLBACK = [["ordered_units", "revenue", "delivered_units", "cancellations", "returns"],
                   ["ordered_units", "revenue"]]
FUNNEL_DAYS = 365
FUNNEL_CLOSED_DAYS = 35
FUNNEL_CACHE_VERSION = 2   # 2 — проверяется, что Ozon вернул все запрошенные метрики

# ---------------------------------------------------------------------------
# Группы статей ОПиУ
# ---------------------------------------------------------------------------
S_REVENUE = "Выручка"
S_COMMISSION = "Комиссия Ozon"
G_LOGISTICS = "Логистика и доставка"
G_RETURNS = "Обработка возвратов и отмен"
G_ACQUIRING = "Эквайринг и платёжные услуги"
G_ADS = "Реклама и продвижение"
G_STORAGE = "Хранение и складские операции"
G_PENALTY = "Штрафы и нарушения"
G_OTHER = "Подписки и прочие услуги Ozon"
G_COMPENSATION = "Компенсации и прочие начисления"
S_INFO = "Справочно"  # в итоги не входит

EXPENSE_GROUPS = [G_LOGISTICS, G_RETURNS, G_ACQUIRING, G_ADS, G_STORAGE, G_PENALTY, G_OTHER]

# Ключевые слова для названий видов начислений из /v1/finance/accrual/types (порядок важен).
# Проверено на реальном справочнике Ozon (сентябрь 2026).
KEYWORDS = [
    (G_PENALTY, ["штраф", "превышение индекса", "нерекомендованный слот", "операционных ошибок",
                 "жалоб", "нарушен", "penalty"]),
    (G_ACQUIRING, ["эквайр", "acquiring", "график выплат", "досрочная выплата", "рассрочк"]),
    (G_COMPENSATION, ["компенсац", "возмещ", "от покупателя"]),
    (G_RETURNS, ["обратн", "невыкуп", "отмен", "возврат", "невостребован", "return", "cancel"]),
    (G_OTHER, ["подписк", "premium", "страхован", "перенос карточ"]),
    (G_ADS, ["продвиж", "реклам", "клик", "трафарет", "отзыв", "бонус", "балл", "акци", "promo",
             "advert", "cashback", "звёзд", "звезд", "бейдж", "бренд"]),
    (G_STORAGE, ["размещ", "хранен", "утилиз", "излиш", "брак", "маркир", "расхожд", "складск",
                 "storage", "disposal"]),
    (G_LOGISTICS, ["логист", "доставк", "магистр", "кросс", "сборк", "drop-off", "pick-up", "упаков",
                   "последняя миля", "отгрузк", "logistic", "delivery", "обработка отправл", "овх"]),
    (S_COMMISSION, ["вознагражд", "комисси", "commission"]),
    (S_REVENUE, ["выручк", "реализац", "продаж", "за товар", "sale", "revenue"]),
]


def classify(text, default):
    t = (text or "").lower()
    for group, words in KEYWORDS:
        if any(w in t for w in words):
            return group
    return default


# ---------------------------------------------------------------------------
# Разбор значений API
# ---------------------------------------------------------------------------
_PROTOBUF_WRAPPED = re.compile(r'^value:"(.*)"$')


def money(value):
    """Суммы приходят как {"amount": "-21.75", "currency": "RUB"}, числом или 'value:"…"'."""
    if isinstance(value, dict):
        for key in ("amount", "value", "units"):
            if key in value:
                return money(value[key])
        return 0.0
    if isinstance(value, str):
        m = _PROTOBUF_WRAPPED.match(value)
        value = m.group(1) if m else value
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
_OFFLINE = []   # API уже не ответил в этом запуске — дальше не ждём повторов, берём кэш


def api_post(account, path, body, retries=7, timeout=90):
    if _OFFLINE:
        raise RuntimeError(_OFFLINE[0])
    headers = {
        "Client-Id": str(account["client_id"]),
        "Api-Key": account["api_key"],
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode("utf-8")
    delay = 2
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(API_HOST + path, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                # С 25.09.2026 Ozon при 429 сообщает в Retry-After, через сколько секунд повторить
                retry_after = e.headers.get("Retry-After") if e.code == 429 else None
                try:
                    wait = min(max(float(retry_after), 1.0), 120.0) if retry_after else delay
                except ValueError:
                    wait = delay
                time.sleep(wait)
                delay = min(delay * 2, 60)
                continue
            raise RuntimeError(f"{path}: HTTP {e.code}: {text}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < retries:
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            _OFFLINE.append(f"Нет соединения с API Ozon ({e}). Если включён VPN — отключите его: "
                            "Ozon не отвечает на зарубежные IP.")
            raise RuntimeError(_OFFLINE[0]) from None


def fetch_types(account):
    """{type_id: {"name": ..., "description": ...}}"""
    # Короткий запрос первым в кабинете: если API недоступен (VPN), это выясняется за минуту, а не за 10
    res = api_post(account, URL_TYPES, {}, retries=3, timeout=20)
    res = res.get("result", res) if isinstance(res, dict) else {}
    types = {}
    for entry in res.get("accrual_types") or res.get("types") or []:
        tid = as_int(entry.get("id", entry.get("type_id")))
        if tid is not None:
            types[tid] = {"name": entry.get("name") or "", "description": entry.get("description") or ""}
    return types


def _find_key(node, key):
    if isinstance(node, dict):
        if node.get(key):
            return node[key]
        node = list(node.values())
    if isinstance(node, list):
        for item in node:
            found = _find_key(item, key)
            if found:
                return found
    return None


def load_key_expiry(acc, cache_dir, from_cache):
    """Дата окончания действия API-ключа (с 03.09.2026 ключи Ozon действуют 3 месяца) — /v1/roles, поле
    expires_at. Сохраняется в кэш, чтобы предупреждение было видно и при --from-cache. None — неизвестно."""
    cache_file = cache_dir / "key_info.json"
    if not from_cache:
        try:
            expires = _find_key(api_post(acc, URL_ROLES, {}, retries=3, timeout=20), "expires_at")
        except RuntimeError as e:
            print(f"  срок действия ключа неизвестен ({e})")
            expires = None
        if expires:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({"expires_at": expires}), encoding="utf-8")
    if cache_file.exists():
        expires = json.loads(cache_file.read_text(encoding="utf-8")).get("expires_at")
        try:
            return date.fromisoformat(str(expires)[:10])
        except ValueError:
            return None
    return None


def fetch_products(account):
    """{sku Ozon: артикул продавца} — через /v3/product/list и /v3/product/info/list."""
    products, last_id = [], ""
    for _ in range(200):
        res = api_post(account, "/v3/product/list",
                       {"filter": {"visibility": "ALL"}, "limit": 1000, "last_id": last_id})
        res = res.get("result", res) if isinstance(res, dict) else {}
        items = res.get("items") or []
        products.extend(items)
        last_id = res.get("last_id") or ""
        if not items or not last_id:
            break
    mapping = {}
    ids = [pr.get("product_id") or pr.get("id") for pr in products if pr.get("product_id") or pr.get("id")]
    for i in range(0, len(ids), 1000):
        res = api_post(account, "/v3/product/info/list", {"product_id": ids[i:i + 1000]})
        res = res.get("result", res) if isinstance(res, dict) else {}
        for item in (res.get("items") if isinstance(res, dict) else None) or []:
            offer = item.get("offer_id")
            if not offer:
                continue
            for sku in [item.get("sku")] + [src.get("sku") for src in item.get("sources") or []]:
                if sku:
                    mapping[int(sku)] = offer
    return mapping


def load_products(acc, cache_dir, from_cache, refresh):
    """Сопоставление SKU с артикулом продавца; кэшируется — нужно для себестоимости в сводном отчёте."""
    # Справочник обновляется при каждом запуске с API: у новых товаров появляются новые SKU.
    cache_file = cache_dir / "products.json"
    cached = ({int(k): v for k, v in json.loads(cache_file.read_text(encoding="utf-8")).items()}
              if cache_file.exists() else {})
    if from_cache:
        return cached
    try:
        mapping = fetch_products(acc)
    except RuntimeError as e:
        if not cached:
            raise
        print(f"  справочник товаров не обновлён, беру из кэша: {e}")
        return cached
    if cached and not refresh:
        mapping = {**cached, **mapping}   # SKU снятых с продажи товаров остаются в справочнике
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    return mapping


# ---------------------------------------------------------------------------
# Заказы и остатки (юнит-экономика, в ОПиУ не входят)
# ---------------------------------------------------------------------------
def posting_rows(postings):
    """Строки заказов из postings[] ответа /v3/posting/fbo/list или /v4/posting/fbs/list.
    Цена товара приходит как {"amount": "746", "currency": "RUB"} (в старых версиях — строкой)."""
    out = []
    for p in postings or []:
        day = str(p.get("in_process_at") or p.get("created_at") or "")[:10]
        cancelled = p.get("status") == "cancelled"
        for pr in p.get("products") or []:
            qty = as_int(pr.get("quantity")) or 1
            out.append([day, p.get("posting_number") or "", pr.get("offer_id") or f"SKU {pr.get('sku')}",
                        qty, money(pr.get("price")), cancelled])
    return out


def fetch_postings(account, first, last):
    """Отправления FBO и FBS, созданные с first по last: [[дата, отправление, артикул, шт, цена за шт, отменён]]."""
    flt = {"since": f"{first}T00:00:00.000Z", "to": f"{last}T23:59:59.999Z"}
    out = []
    for path in (URL_FBO_LIST, URL_FBS_LIST):
        cursor = ""
        for _ in range(5000):
            body = {"cursor": cursor, "filter": flt, "limit": POSTINGS_LIMIT, "sort_dir": "ASC",
                    "with": {"analytics_data": False, "financial_data": False}}
            res = api_post(account, path, body)
            res = res.get("result", res) if isinstance(res, dict) else {}
            out += posting_rows(res.get("postings"))
            cursor = res.get("cursor") or ""
            if not res.get("has_next") or not cursor:
                break
    return out


def fetch_stocks(account):
    """Остатки FBO / FBS / rFBS по артикулам: [[артикул, склад, вид, шт]] (/v4/product/info/stocks)."""
    rows, cursor = [], ""
    for _ in range(500):
        res = api_post(account, URL_STOCKS, {"filter": {"visibility": "ALL"}, "limit": 1000, "cursor": cursor})
        res = res.get("result", res) if isinstance(res, dict) else {}
        items = res.get("items") or []
        for item in items:
            for s in item.get("stocks") or []:
                qty = as_int(s.get("present")) or 0
                kind = str(s.get("type") or "").lower()
                if qty:
                    rows.append([item.get("offer_id") or f"SKU {s.get('sku')}", STOCK_PLACES.get(kind, kind.upper()),
                                 "own" if kind in ("fbs", "rfbs") else "mp", qty])
        new_cursor = res.get("cursor") or ""
        if len(items) < 1000 or not new_cursor or new_cursor == cursor:
            break
        cursor = new_cursor
    return rows


def fetch_prices(account):
    """Текущие цены по артикулам: [[артикул, цена до скидки, цена продавца, цена с акциями]] (/v5/product/info/prices).
    Нет значения (например, цена до скидки не задана) — None."""
    rows, cursor = [], ""
    for _ in range(500):
        res = api_post(account, URL_PRICES, {"filter": {"visibility": "ALL"}, "limit": 1000, "cursor": cursor})
        res = res.get("result", res) if isinstance(res, dict) else {}
        items = res.get("items") or []
        for item in items:
            pr = item.get("price") or {}
            price = money(pr.get("price"))
            if not price:
                continue
            promo = money(pr.get("marketing_price")) or money(pr.get("marketing_seller_price"))
            rows.append([item.get("offer_id") or f"SKU {item.get('product_id')}", money(pr.get("old_price")) or None,
                         price, promo or None])
        new_cursor = res.get("cursor") or ""
        if len(items) < 1000 or not new_cursor or new_cursor == cursor:
            break
        cursor = new_cursor
    return rows


def fetch_multiples(account):
    """Кратность продажи по артикулам: [[артикул, шт, характеристика]] (/v4/product/info/attributes)."""
    rows, last_id = [], ""
    for _ in range(500):
        res = api_post(account, URL_ATTRIBUTES, {"filter": {"visibility": "ALL"}, "limit": 1000, "last_id": last_id})
        items = res.get("result") or [] if isinstance(res, dict) else []
        for item in items:
            values = {a.get("id"): (a.get("values") or [{}])[0].get("value") for a in item.get("attributes") or []}
            for attr_id, title in MULTIPLE_ATTRIBUTES:
                qty = money(values.get(attr_id))
                if qty > 0:
                    rows.append([item.get("offer_id") or f"SKU {item.get('sku')}", qty, title])
                    break
        new_last = res.get("last_id") or "" if isinstance(res, dict) else ""
        if len(items) < 1000 or not new_last or new_last == last_id:
            break
        last_id = new_last
    return rows


def fetch_funnel(account, first, last, products):
    """Воронка по артикулам и месяцам: ({месяц: [[артикул, показы, переходы, корзина, заказано шт, заказано ₽,
    доставлено, отменено, возвращено]]}, полученные метрики). Недоступные метрики — None."""
    for metrics in [FUNNEL_METRICS] + FUNNEL_FALLBACK:
        try:
            data, offset = [], 0
            for _ in range(200):
                res = api_post(account, URL_ANALYTICS, {
                    "date_from": str(first), "date_to": str(last), "metrics": metrics,
                    "dimension": ["sku", "month"], "filters": [], "limit": 1000, "offset": offset,
                    "sort": [{"key": metrics[0], "order": "DESC"}]})
                res = res.get("result", res) if isinstance(res, dict) else {}
                batch = res.get("data") or []
                data += batch
                offset += len(batch)
                if len(batch) < 1000:
                    break
                time.sleep(20)
            # Без Premium Ozon может не вернуть ошибку, а молча отдать только разрешённые метрики — тогда в строке
            # их меньше, чем запрошено, и порядок уже не совпадает с запросом. Такой ответ не используем.
            if metrics is not FUNNEL_FALLBACK[-1] and any(len(r.get("metrics") or []) != len(metrics) for r in data):
                print(f"  воронка: Ozon вернул не все метрики из {len(metrics)} (часть — только с Premium), "
                      "пробую меньший набор")
                time.sleep(20)
                continue
            break
        except RuntimeError as e:
            if _OFFLINE or metrics is FUNNEL_FALLBACK[-1]:
                raise
            print(f"  воронка: Ozon не отдал метрики {', '.join(sorted(set(metrics) - set(FUNNEL_FALLBACK[0])) or metrics)}"
                  f" (часть метрик — только с Premium), пробую меньший набор: {str(e)[:200]}")
            time.sleep(20)
    out = defaultdict(lambda: defaultdict(lambda: [0.0] * len(FUNNEL_METRICS)))
    for row in data:
        if len(row.get("metrics") or []) != len(metrics):
            continue
        dims = row.get("dimensions") or []
        sku = as_int(dims[0].get("id")) if dims else None
        month = next((str(d.get("id"))[:7] for d in dims[1:] if re.match(r"\d{4}-\d{2}", str(d.get("id") or ""))),
                     None)
        if sku is None or not month:
            continue
        values = out[month][products.get(sku) or f"SKU {sku}"]
        for name, value in zip(metrics, row.get("metrics") or []):
            values[FUNNEL_METRICS.index(name)] += money(value)
    return ({mon: [[art] + [v if FUNNEL_METRICS[i] in metrics else None for i, v in enumerate(vals)]
                   for art, vals in arts.items()] for mon, arts in out.items()}, metrics)


def load_funnel(acc, cache_dir, from_cache, first_day, products):
    """Воронка по месяцам с кэшем funnel.json: закрытые месяцы не перекачиваются."""
    today = date.today()
    f = cache_dir / "funnel.json"
    cached = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"months": {}}
    if cached.get("version") != FUNNEL_CACHE_VERSION and not from_cache and not _OFFLINE:
        cached = {"months": {}}   # кэш прежней версии мог содержать сдвинутые метрики — загружаем заново
    if from_cache or _OFFLINE:
        return cached
    first = max(first_day, today - timedelta(days=FUNNEL_DAYS - 1))
    start = date(first.year, first.month, 1)
    while start <= today:   # первый месяц, который нужно (пере)загрузить
        nxt = date(start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1, 1)
        if start.strftime("%Y-%m") not in cached["months"] or nxt - timedelta(days=1) > today - timedelta(
                days=FUNNEL_CLOSED_DAYS):
            break
        start = nxt
    if start <= today:
        try:
            months, metrics = fetch_funnel(acc, max(start, first), today, products)
            for mon in list(cached["months"]):
                if mon >= start.strftime("%Y-%m"):
                    del cached["months"][mon]
            cached["months"].update(months)
            cached["metrics"] = metrics
            cached["version"] = FUNNEL_CACHE_VERSION
            cached["funnel_from"] = min(filter(None, [cached.get("funnel_from"), first.isoformat()]))
            cache_dir.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(cached, ensure_ascii=False), encoding="utf-8")
            print(f"  воронка с {start:%Y-%m}: {sum(len(v) for v in months.values())} строк"
                  + ("" if metrics == FUNNEL_METRICS else " (без показов/корзины — нет доступа к метрикам)"))
        except RuntimeError as e:
            print(f"  ОШИБКА: воронка продаж не получена (берётся из прошлой загрузки, если есть): {e}")
    return cached


def load_goods(acc, cache_dir, from_cache, first_day, products=None):
    """Заказы (отправления) по месяцам и остатки. Закрытые месяцы берутся из кэша, текущие — заново,
    т.к. отправления ещё меняют статус. Ошибки не прерывают отчёт: остаются данные прошлой загрузки."""
    today = date.today()
    goods = {"orders": []}
    month = date(first_day.year, first_day.month, 1)
    loaded_from = None
    while month <= today:
        nxt = date(month.year + (month.month == 12), 1 if month.month == 12 else month.month + 1, 1)
        f = cache_dir / f"orders_{month:%Y-%m}.json"
        closed = nxt - timedelta(days=1) <= today - timedelta(days=ORDERS_CLOSED_DAYS)
        rows = None
        if not from_cache and not _OFFLINE and not (closed and f.exists()):
            try:
                rows = fetch_postings(acc, month, min(nxt - timedelta(days=1), today))
                cache_dir.mkdir(parents=True, exist_ok=True)
                f.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            except RuntimeError as e:
                print(f"  ОШИБКА: заказы за {month:%Y-%m} не получены"
                      + (" (берутся из кэша)" if f.exists() else "") + f": {e}")
        if rows is None and f.exists():
            rows = json.loads(f.read_text(encoding="utf-8"))
        if rows is not None:
            goods["orders"] += rows
            loaded_from = loaded_from or month.isoformat()
        month = nxt
    goods["orders_from"] = loaded_from
    stock_file = cache_dir / "stocks.json"
    if not from_cache:
        try:
            stocks = fetch_stocks(acc)
            cache_dir.mkdir(parents=True, exist_ok=True)
            stock_file.write_text(json.dumps({"stocks": stocks, "stocks_at": time.strftime("%Y-%m-%d %H:%M")},
                                             ensure_ascii=False), encoding="utf-8")
        except RuntimeError as e:
            print(f"  ОШИБКА: остатки не получены (берутся из прошлой загрузки, если есть): {e}")
    if stock_file.exists():
        goods.update(json.loads(stock_file.read_text(encoding="utf-8")))
    price_file = cache_dir / "prices.json"
    if not from_cache and not _OFFLINE:
        try:
            prices = fetch_prices(acc)
            cache_dir.mkdir(parents=True, exist_ok=True)
            price_file.write_text(json.dumps({"prices": prices, "prices_at": time.strftime("%Y-%m-%d %H:%M")},
                                             ensure_ascii=False), encoding="utf-8")
        except RuntimeError as e:
            print(f"  ОШИБКА: цены не получены (берутся из прошлой загрузки, если есть): {e}")
    if price_file.exists():
        goods.update(json.loads(price_file.read_text(encoding="utf-8")))
    mult_file = cache_dir / "multiples.json"
    if not from_cache and not _OFFLINE:
        try:
            multiples = fetch_multiples(acc)
            cache_dir.mkdir(parents=True, exist_ok=True)
            mult_file.write_text(json.dumps({"multiples": multiples,
                                             "multiples_at": time.strftime("%Y-%m-%d %H:%M")},
                                            ensure_ascii=False), encoding="utf-8")
        except RuntimeError as e:
            print(f"  ОШИБКА: кратность товаров не получена (берётся из прошлой загрузки, если есть): {e}")
    if mult_file.exists():
        goods.update(json.loads(mult_file.read_text(encoding="utf-8")))
    funnel = load_funnel(acc, cache_dir, from_cache, first_day, products or {})
    if funnel.get("months"):
        goods["funnel"], goods["funnel_from"] = funnel["months"], funnel.get("funnel_from")
    print(f"  заказов (строк отправлений): {len(goods['orders'])}"
          + (f", остатков на {goods['stocks_at']}: " + f"{sum(s[3] for s in goods['stocks']):,.0f}".replace(",", " ")
             + " шт" if goods.get("stocks") is not None else "")
          + (f", цен на {goods['prices_at']}: {len(goods['prices'])}" if goods.get("prices") is not None else "")
          + (f", кратность: {len(goods['multiples'])} товаров" if goods.get("multiples") is not None else ""))
    return goods


def goods_dump(goods):
    """Поля выгрузки для оркестратора: заказы по периодам и артикулам, остатки на дату."""
    ordered = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for day, _posting, article, qty, price, cancelled in goods.get("orders") or []:
        if not day:
            continue
        row = ordered[(period_key(date.fromisoformat(day)), article)]
        row[0] += qty
        row[1] += qty * price
        if cancelled:
            row[2] += qty
            row[3] += qty * price
    return {"ordered": [[mon, art, q, round(s, 2), cq, round(cs, 2)] for (mon, art), (q, s, cq, cs) in ordered.items()],
            "ordered_from": goods.get("orders_from"), "stocks": goods.get("stocks"),
            "stocks_at": goods.get("stocks_at"),
            # Текущие цены [[артикул, цена до скидки, цена продавца, цена с акциями]]
            "prices": goods.get("prices"), "prices_at": goods.get("prices_at"),
            # Кратность продажи из карточек МП [[артикул, шт, характеристика]]
            "multiples": goods.get("multiples"), "multiples_at": goods.get("multiples_at"),
            # Воронка [[месяц, артикул, показы, переходы, корзина, заказано шт, ₽, выкуплено, отменено, возвращено]]
            "funnel": ([[mon] + row for mon, rows in goods["funnel"].items() for row in rows]
                       if goods.get("funnel") is not None else None),
            "funnel_from": goods.get("funnel_from")}


def fetch_day(account, day, cache_dir, refresh):
    """Все начисления за день (с пагинацией по last_id), с кэшем для закрытых дней."""
    cache_file = cache_dir / f"{day.isoformat()}.json"
    closed = day <= date.today() - timedelta(days=CACHE_AFTER_DAYS)
    if closed and cache_file.exists() and not refresh:
        return json.loads(cache_file.read_text(encoding="utf-8"))

    accruals, last_id = [], ""
    for _ in range(500):  # предохранитель от бесконечной пагинации
        body = {"date": day.isoformat()}
        if last_id:
            body["last_id"] = last_id
        res = api_post(account, URL_BY_DAY, body)
        res = res.get("result", res) if isinstance(res, dict) else {}
        batch = res.get("accruals") or []
        accruals.extend(batch)
        new_last = res.get("last_id") or ""
        if not batch or not new_last or new_last == last_id:
            break
        last_id = new_last

    # Сохраняем и свежие дни: при следующем запуске они перезапишутся, зато --from-cache видит весь период
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(accruals, ensure_ascii=False), encoding="utf-8")
    return accruals


# ---------------------------------------------------------------------------
# Разбор начислений в строки ОПиУ
# ---------------------------------------------------------------------------
def type_label(tid, types):
    if tid is None:
        return None
    info = types.get(tid)
    if info and (info["name"] or info["description"]):
        return info["description"] or info["name"]
    return f"Вид начисления {tid}"


def type_group(tid, types, amount, default=None):
    info = types.get(tid) or {}
    text = f"{info.get('description', '')} {info.get('name', '')}"
    return classify(text, default or (G_COMPENSATION if amount > 0 else G_OTHER))


def _fees(node):
    """Рекурсивно собирает {type_id, accrued} из произвольной вложенности (container_fees и т.п.)."""
    if isinstance(node, dict):
        if "type_id" in node and ("accrued" in node or "amount" in node):
            yield as_int(node.get("type_id")), money(node.get("accrued", node.get("amount")))
            return
        for v in node.values():
            yield from _fees(v)
    elif isinstance(node, list):
        for v in node:
            yield from _fees(v)


def components(accrual):
    """(type_id, сумма, sku | None) из услуг отправления, товарных, нетоварных и контейнерных сборов."""
    found = []
    posting = accrual.get("posting") or {}
    for product in posting.get("products") or []:
        sku = as_int(product.get("sku"))
        delivery = product.get("delivery") or {}
        for service in delivery.get("services") or []:
            found.append((as_int(service.get("type_id")), money(service.get("accrued")), sku))
        for service in product.get("services") or []:
            found.append((as_int(service.get("type_id")), money(service.get("accrued", service.get("amount"))), sku))
    item_fees = accrual.get("item_fees") or {}
    for entry in item_fees.get("fees") or []:
        sku = as_int(entry.get("sku"))
        for fee in entry.get("fees") or []:
            found.append((as_int(fee.get("type_id")), money(fee.get("accrued", fee.get("amount"))), sku))
    found.extend((t, a, None) for t, a in _fees(accrual.get("non_item_fee")))
    found.extend((t, a, None) for t, a in _fees(accrual.get("container_fees")))
    return [(t, a, sku) for t, a, sku in found if t is not None and a]


def product_commission(accrual):
    """Блок commission товаров отправления: цена продавца, комиссия, оплата покупателя, доплата Ozon."""
    out = defaultdict(float)
    for product in (accrual.get("posting") or {}).get("products") or []:
        c = product.get("commission") or {}
        for key in ("sale_amount", "commission", "sale_price", "bonus", "coinvestment"):
            out[key] += money(c.get(key))
    return out


def accrual_skus(accrual):
    """Все SKU начисления: товары отправления и товарные сборы."""
    skus = {as_int(p.get("sku")) for p in (accrual.get("posting") or {}).get("products") or []}
    skus |= {as_int(e.get("sku")) for e in (accrual.get("item_fees") or {}).get("fees") or []}
    return {s for s in skus if s}


def posting_number(accrual):
    posting = accrual.get("posting") or {}
    return (posting.get("posting_number") or accrual.get("posting_number") or accrual.get("unit_number") or "")


def split_accrual(accrual, types):
    """Список (группа, подпись, сумма, type_id, sku | None); сумма частей == total_amount.
    sku — товар, к которому относится часть (для юнит-экономики); None — нетоварные начисления."""
    total = money(accrual.get("total_amount", accrual.get("amount")))
    category = accrual.get("accrued_category") or ""
    parts = []
    # Выручка по цене продавца (sale_amount) и комиссия — по каждому товару отправления.
    # sale_amount = оплата покупателя + доплата Ozon за скидки. Подпись — по итогу отправления.
    c = product_commission(accrual)
    sale_label = "Продажи (цена продавца)" if c["sale_amount"] > 0 else "Возвраты и отмены"
    comm_label = "Комиссия за продажу" if c["commission"] < 0 else "Возврат комиссии"
    for product in (accrual.get("posting") or {}).get("products") or []:
        pc = product.get("commission") or {}
        sku = as_int(product.get("sku"))
        if money(pc.get("sale_amount")):
            parts.append((S_REVENUE, sale_label, money(pc.get("sale_amount")), None, sku))
        if money(pc.get("commission")):
            parts.append((S_COMMISSION, comm_label, money(pc.get("commission")), None, sku))
    for tid, amount, sku in components(accrual):
        parts.append((type_group(tid, types, amount), type_label(tid, types), amount, tid, sku))

    residual = total - sum(p[2] for p in parts)
    if abs(residual) >= 0.005:
        skus = accrual_skus(accrual)
        sku = next(iter(skus)) if len(skus) == 1 else None
        tid = as_int(accrual.get("type_id", accrual.get("accrual_type_id")))
        label = type_label(tid, types)
        if label:
            group = type_group(tid, types, residual)
        elif category in ("ITEM", "POSTING") and residual > 0:
            label, group = "Начисления за товары (без расшифровки)", S_REVENUE
        else:
            label = f"Без расшифровки ({category or 'категория не указана'})"
            group = G_COMPENSATION if residual > 0 else G_OTHER
        parts.append((group, label, residual, tid, sku))
    # Справочно (не входит в сумму начисления): как сложилась цена продавца
    if c["sale_price"]:
        parts.append((S_INFO, "Оплачено покупателями", c["sale_price"], None, None))
    if c["bonus"]:
        parts.append((S_INFO, "Доплата Ozon за скидки покупателям (бонусы)", c["bonus"], None, None))
    if c["coinvestment"]:
        parts.append((S_INFO, "Софинансирование скидок", c["coinvestment"], None, None))
    return total, parts


def product_sales(accrual, products):
    """Продажи по товарам отправления: количество = цена продажи / цена продавца за единицу."""
    out = []
    for product in (accrual.get("posting") or {}).get("products") or []:
        c = product.get("commission") or {}
        sale_amount, unit_price = money(c.get("sale_amount")), money(c.get("seller_price"))
        if not sale_amount:
            continue
        qty = sale_amount / unit_price if unit_price else 0.0
        qty = round(qty) if abs(qty - round(qty)) < 0.01 else qty
        sku = as_int(product.get("sku"))
        out.append((products.get(sku) or f"SKU {sku}", qty, sale_amount))
    return out


# Разрез выгрузки: month — ключ «ГГГГ-ММ», day — «ГГГГ-ММ-ДД» (нужен своду для отчётов по неделям и дням)
GRAIN = "month"


def period_key(day):
    """Ключ периода по дате начисления: месяц или день — в зависимости от --grain."""
    return day.isoformat() if GRAIN == "day" else day.strftime("%Y-%m")


def aggregate(day_accruals, types, products=None):
    """{(группа, подпись): {период: сумма}}, строки листа «Начисления», продажи по артикулам."""
    table = defaultdict(lambda: defaultdict(float))
    rows = []
    sales = defaultdict(lambda: [0.0, 0.0])   # (месяц, артикул, дата, отправление) -> штук, выручка
    by_article = defaultdict(float)           # (месяц, артикул, группа, подпись) -> сумма (юнит-экономика)
    tax_base = defaultdict(float)             # период -> фактически оплачено покупателями
    tax_base_by_article = defaultdict(float)  # (период, артикул) -> фактически оплачено покупателями
    products = products or {}
    for day, accruals in day_accruals:
        mon = period_key(day)
        for acc in accruals:
            for product in (acc.get("posting") or {}).get("products") or []:
                c = product.get("commission") or {}
                # Только движение товара (как в product_sales): sale_amount ≠ 0. Знак — по sale_amount,
                # чтобы возврат уменьшал базу, даже если Ozon отдаёт sale_price без минуса.
                sale_amount = money(c.get("sale_amount"))
                paid = abs(money(c.get("sale_price"))) * (-1 if sale_amount < 0 else 1) if sale_amount else 0.0
                if paid:
                    sku = as_int(product.get("sku"))
                    article = products.get(sku) or f"SKU {sku}"
                    tax_base[mon] += paid
                    tax_base_by_article[(mon, article)] += paid
            for article, qty, revenue in product_sales(acc, products):
                sold = sales[(mon, article, day.isoformat(), posting_number(acc))]
                sold[0] += qty
                sold[1] += revenue
            total, parts = split_accrual(acc, types)
            for group, label, value, tid, sku in parts:
                table[(group, label)][mon] += value
                if group != S_INFO:
                    article = (products.get(sku) or f"SKU {sku}") if sku else ""
                    by_article[(mon, article, group, label)] += value
                rows.append((day, mon, acc.get("accrual_id") or acc.get("id") or "",
                             acc.get("accrued_category") or "", posting_number(acc),
                             tid, label, group, value, total))
    return table, rows, sales, by_article, tax_base, tax_base_by_article


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------
NUM = '#,##0.00;[Red]-#,##0.00;"–"'
PCT = '0.0%;[Red]-0.0%;"–"'
F_HEADER = PatternFill("solid", fgColor="1F4E78")
F_SECTION = PatternFill("solid", fgColor="DDEBF7")
F_TOTAL = PatternFill("solid", fgColor="FFF2CC")
F_RESULT = PatternFill("solid", fgColor="E2EFDA")
WHITE_BOLD = Font(bold=True, color="FFFFFF")
BOLD = Font(bold=True)


def month_title(key):
    names = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]
    y, m = key.split("-")
    return f"{names[int(m) - 1]} {y}"


class SheetWriter:
    def __init__(self, ws, months, title):
        self.ws, self.months = ws, months
        ws.cell(1, 1, title).font = Font(bold=True, size=14)
        self.row = 3
        hdr = ["Статья"] + [month_title(m) for m in months] + ["Итого"]
        for c, v in enumerate(hdr, 1):
            cell = ws.cell(self.row, c, v)
            cell.font, cell.fill = WHITE_BOLD, F_HEADER
            cell.alignment = Alignment(horizontal="center")
        ws.freeze_panes = ws.cell(self.row + 1, 2)
        ws.column_dimensions["A"].width = 50
        for c in range(2, len(months) + 3):
            ws.column_dimensions[get_column_letter(c)].width = 15
        self.row += 1

    def line(self, label, values, fill=None, font=None, indent=0, fmt=NUM, total=None):
        ws = self.ws
        ws.cell(self.row, 1, label).alignment = Alignment(indent=indent)
        if total is None:
            total = sum(values.get(m, 0.0) for m in self.months)
        for c, v in enumerate([values.get(m, 0.0) for m in self.months] + [total], 2):
            ws.cell(self.row, c, round(v, 6)).number_format = fmt
        for c in range(1, len(self.months) + 3):
            if fill:
                ws.cell(self.row, c).fill = fill
            if font:
                ws.cell(self.row, c).font = font
        self.row += 1

    def section(self, title):
        for c in range(1, len(self.months) + 3):
            self.ws.cell(self.row, c).fill = F_SECTION
            self.ws.cell(self.row, c).font = BOLD
        self.ws.cell(self.row, 1, title)
        self.row += 1

    def blank(self):
        self.row += 1


def sum_rows(rows, months):
    out = defaultdict(float)
    for vals in rows:
        for m in months:
            out[m] += vals.get(m, 0.0)
    return out


def ratio(num, den, months):
    return {m: (num.get(m, 0.0) / den[m] if den.get(m) else 0.0) for m in months}


def has_group(table, group):
    return any(s == group for s, _ in table)


def pnl_totals(table, months):
    def section_rows(section):
        return [v for (s, _), v in table.items() if s == section]

    revenue = sum_rows(section_rows(S_REVENUE), months)
    commission = sum_rows(section_rows(S_COMMISSION), months)
    groups = {g: sum_rows(section_rows(g), months) for g in EXPENSE_GROUPS}
    compensation = sum_rows(section_rows(G_COMPENSATION), months)
    expenses = sum_rows([commission] + list(groups.values()), months)
    net = sum_rows([revenue, expenses, compensation], months)
    return revenue, commission, groups, compensation, expenses, net


def write_pnl(ws, table, months, title):
    w = SheetWriter(ws, months, title)
    revenue, commission, groups, compensation, expenses, net = pnl_totals(table, months)

    def detail(section):
        items = sorted(((lbl, v) for (s, lbl), v in table.items() if s == section),
                       key=lambda x: -abs(sum(x[1].values())))
        for lbl, vals in items:
            w.line(lbl, vals, indent=2)

    w.section(S_REVENUE.upper())
    detail(S_REVENUE)
    w.line("Выручка за вычетом возвратов", revenue, fill=F_TOTAL, font=BOLD)
    w.blank()

    w.section("РАСХОДЫ OZON")
    if has_group(table, S_COMMISSION):
        w.line(S_COMMISSION, commission, font=BOLD, indent=1)
        detail(S_COMMISSION)
    for g in EXPENSE_GROUPS:
        if has_group(table, g):
            w.line(g, groups[g], font=BOLD, indent=1)
            detail(g)
    w.line("Итого расходы Ozon", expenses, fill=F_TOTAL, font=BOLD)
    w.blank()

    if has_group(table, G_COMPENSATION):
        w.section(G_COMPENSATION.upper())
        detail(G_COMPENSATION)
        w.line("Итого компенсации", compensation, fill=F_TOTAL, font=BOLD)
        w.blank()

    w.line("ИТОГО НАЧИСЛЕНО ПРОДАВЦУ", net, fill=F_RESULT, font=Font(bold=True, size=12))
    w.blank()

    if has_group(table, S_INFO):
        w.section("СПРАВОЧНО (в итоги не входит): из чего сложилась цена продавца")
        detail(S_INFO)
        w.blank()

    w.section("ДОЛЯ ОТ ВЫРУЧКИ")
    rev_total = sum(revenue.values())
    share = lambda vals: (-sum(vals.values()) / rev_total) if rev_total else 0.0
    neg = lambda vals: {m: -v for m, v in vals.items()}
    w.line("Все расходы Ozon", ratio(neg(expenses), revenue, months), fmt=PCT, indent=1, total=share(expenses))
    if has_group(table, S_COMMISSION):
        w.line("Комиссия", ratio(neg(commission), revenue, months), fmt=PCT, indent=2, total=share(commission))
    for g in EXPENSE_GROUPS:
        if has_group(table, g):
            w.line(g, ratio(neg(groups[g]), revenue, months), fmt=PCT, indent=2, total=share(groups[g]))
    w.line("Начислено продавцу", ratio(net, revenue, months), fmt=PCT, indent=1,
           total=(sum(net.values()) / rev_total) if rev_total else 0.0)


def write_by_store(ws, tables, months):
    w = SheetWriter(ws, months, "Ключевые показатели по магазинам")
    for store, table in tables.items():
        revenue, commission, groups, compensation, expenses, net = pnl_totals(table, months)
        w.section(store)
        w.line("Выручка за вычетом возвратов", revenue, indent=1)
        w.line("Комиссия Ozon", commission, indent=1)
        for g in EXPENSE_GROUPS:
            if has_group(table, g):
                w.line(g, groups[g], indent=1)
        w.line("Итого расходы Ozon", expenses, indent=1, font=BOLD)
        w.line("Компенсации", compensation, indent=1)
        w.line("Начислено продавцу", net, indent=1, fill=F_RESULT, font=BOLD)
        rev_total = sum(revenue.values())
        w.line("Расходы, % от выручки", ratio({m: -v for m, v in expenses.items()}, revenue, months),
               fmt=PCT, indent=1, total=(-sum(expenses.values()) / rev_total) if rev_total else 0.0)
        w.blank()


def style_table(ws, widths, num_cols):
    for c in range(1, len(widths) + 1):
        ws.cell(1, c).font, ws.cell(1, c).fill = WHITE_BOLD, F_HEADER
        ws.column_dimensions[get_column_letter(c)].width = widths[c - 1]
    for c in num_cols:
        for (cell,) in ws.iter_rows(min_row=2, min_col=c, max_col=c):
            cell.number_format = NUM
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def write_raw(ws, store_rows):
    ws.append(["Магазин", "Дата", "Месяц", "ID начисления", "Категория", "Отправление",
               "type_id", "Вид начисления", "Группа ОПиУ", "Сумма", "Итого по начислению"])
    for store, rows in store_rows.items():
        for day, mon, acc_id, cat, posting, tid, label, group, value, total in rows:
            ws.append([store, day, mon, str(acc_id), cat, posting, tid, label, group,
                       round(value, 2), round(total, 2)])
    for (cell,) in ws.iter_rows(min_row=2, min_col=2, max_col=2):
        cell.number_format = "DD.MM.YYYY"
    style_table(ws, [18, 12, 9, 22, 12, 22, 9, 45, 30, 14, 16], [10, 11])


def write_types(ws, types_by_store):
    ws.append(["Магазин", "type_id", "Название", "Описание", "Группа ОПиУ (для расходов)"])
    for store, types in types_by_store.items():
        for tid in sorted(types):
            info = types[tid]
            ws.append([store, tid, info["name"], info["description"], type_group(tid, types, -1.0)])
    style_table(ws, [18, 9, 40, 60, 32], [])


def safe_sheet_name(name, used):
    base = "".join(ch for ch in name if ch not in '[]:*?/\\')[:31] or "Магазин"
    title, n = base, 2
    while title.lower() in used:
        suffix = f" ({n})"
        title, n = base[:31 - len(suffix)] + suffix, n + 1
    used.add(title.lower())
    return title


# ---------------------------------------------------------------------------
def parse_month(s):
    y, m = s.split("-")
    return int(y), int(m)


def month_days(start, end):
    """Все дни от первого дня start до последнего дня end, но не позже сегодняшнего."""
    d = date(start[0], start[1], 1)
    last = date(end[0] + (end[1] == 12), 1 if end[1] == 12 else end[1] + 1, 1) - timedelta(days=1)
    last = min(last, date.today())
    while d <= last:
        yield d
        d += timedelta(days=1)


def cache_folder(here, acc):
    return here / "cache" / re.sub(r"[^\w-]", "_", str(acc["client_id"]))


def load_types(acc, cache_dir, here, from_cache):
    """Справочник видов начислений: из API (и в кэш) или, в режиме --from-cache, из кэша / probe-файла."""
    cache_file = cache_dir / "accrual_types.json"
    if not from_cache:
        types = fetch_types(acc)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(types, ensure_ascii=False), encoding="utf-8")
        return types
    if cache_file.exists():
        return {int(k): v for k, v in json.loads(cache_file.read_text(encoding="utf-8")).items()}
    for probe_file in sorted(here.glob("ozon_probe_*.json"), reverse=True):
        data = json.loads(probe_file.read_text(encoding="utf-8")).get("types") or {}
        entries = data.get("accrual_types") or (data.get("result") or {}).get("accrual_types") or []
        if entries:
            return {int(e["id"]): {"name": e.get("name") or "", "description": e.get("description") or ""}
                    for e in entries}
    raise RuntimeError("нет справочника видов начислений в кэше — запустите без --from-cache")


def sales_by_article(sales, name):
    """Продажи по заказам (месяц, артикул, дата, заказ) -> по артикулам (месяц, артикул)."""
    out = defaultdict(lambda: [0.0, 0.0])
    for (mon, art, _day, _order), (qty, rev) in (sales or {}).get(name, {}).items():
        out[(mon, art)][0] += qty
        out[(mon, art)][1] += rev
    return out


def article_rows(table, by_article, info_group):
    """[[месяц, артикул, группа, подпись, сумма]] для юнит-экономики. Всё, что не разнесено по артикулам,
    идёт строкой с пустым артикулом — сумма по артикулам всегда равна таблице ОПиУ."""
    rest = defaultdict(float)
    for (group, label), vals in table.items():
        if group != info_group:
            for mon, value in vals.items():
                rest[(group, label, mon)] += value
    out = []
    for (mon, article, group, label), value in (by_article or {}).items():
        rest[(group, label, mon)] -= value
        if abs(value) >= 0.005:
            out.append([mon, article, group, label, round(value, 2)])
    out += [[mon, "", group, label, round(value, 2)] for (group, label, mon), value in rest.items()
            if abs(value) >= 0.005]
    return out


def write_dump(path, marketplace, months, tables, info_group, net_fn, checks, sales=None, by_article=None,
               goods=None, tax_base=None, tax_base_by_article=None):
    """Выгрузка таблиц ОПиУ для сводного отчёта (opiu_all.py)."""
    data = {"marketplace": marketplace, "months": months, "info_group": info_group, "cabinets": {}}
    for name, table in tables.items():
        data["cabinets"][name] = {
            "rows": [[g, lbl, {m: v for m, v in vals.items() if abs(v) >= 0.005}]
                     for (g, lbl), vals in table.items()],
            "net": net_fn(table),
            "checks": checks.get(name, []),
            "sales": [[mon, art, round(qty, 3), round(rev, 2)]
                      for (mon, art), (qty, rev) in sales_by_article(sales, name).items() if qty or rev],
            # Те же продажи по заказам: дата, номер заказа, артикул, штук, выручка (детализация себестоимости)
            "orders": [[mon, day, order, art, round(qty, 3), round(rev, 2)]
                       for (mon, art, day, order), (qty, rev) in (sales or {}).get(name, {}).items() if qty or rev],
            # Строки ОПиУ по артикулам: месяц, артикул ("" — без артикула), группа, подпись, сумма
            "by_article": article_rows(table, (by_article or {}).get(name), info_group),
            # Налоговая база: sale_price — фактически оплачено покупателями без доплаты Ozon (возвраты с минусом).
            "tax_base": {m: round(v, 2) for m, v in (tax_base or {}).get(name, {}).items()
                         if abs(v) >= 0.005},
            "tax_base_by_article": [[mon, art, round(value, 2)]
                                    for (mon, art), value in (tax_base_by_article or {}).get(name, {}).items()
                                    if abs(value) >= 0.005],
            # Заказы [[месяц, артикул, шт, ₽, отменено шт, отменено ₽]] и остатки [[артикул, склад, вид, шт]]
            **goods_dump((goods or {}).get(name) or {}),
        }
    Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def probe(accounts, day_str, here):
    day = date.fromisoformat(day_str)
    for acc in accounts:
        name = acc.get("name") or str(acc["client_id"])
        out = here / f"ozon_probe_{re.sub(r'[^\w-]', '_', name)}_{day_str}.json"
        data = {"types": api_post(acc, URL_TYPES, {}), "by_day": api_post(acc, URL_BY_DAY, {"date": day_str})}
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[{name}] сырой ответ за {day}: {out}")


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="Финансы Ozon по месяцам для нескольких кабинетов")
    ap.add_argument("--config", default=str(here / "ozon_accounts.json"))
    ap.add_argument("--from", dest="date_from", help="Первый месяц, ГГГГ-ММ")
    ap.add_argument("--to", dest="date_to", help="Последний месяц, ГГГГ-ММ")
    ap.add_argument("--out", help="Путь к Excel-файлу")
    ap.add_argument("--no-raw", action="store_true", help="Не добавлять лист со всеми начислениями")
    ap.add_argument("--refresh", action="store_true", help="Игнорировать кэш и скачать всё заново")
    ap.add_argument("--probe", metavar="ГГГГ-ММ-ДД", help="Сохранить сырой ответ API за день и выйти")
    ap.add_argument("--from-cache", action="store_true", help="Не обращаться к API: только дни из папки cache")
    ap.add_argument("--dump", help="Сохранить таблицы ОПиУ в JSON для сводного отчёта (opiu_all.py)")
    ap.add_argument("--no-excel", action="store_true", help="Не сохранять Excel (удобно вместе с --dump)")
    ap.add_argument("--grain", choices=("month", "day"), default="month",
                    help="Разрез выгрузки: month — по месяцам (по умолчанию), day — по дням")
    args = ap.parse_args()
    global GRAIN
    GRAIN = args.grain

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    accounts = [a for a in cfg["accounts"] if a.get("enabled", True)]
    if not accounts:
        sys.exit("В конфиге нет активных кабинетов")

    if args.probe:
        probe(accounts, args.probe, here)
        return

    today = date.today()
    period = cfg.get("period") or {}
    end = parse_month(args.date_to or period.get("to") or f"{today.year}-{today.month:02d}")
    default_start = (end[0] - 1, end[1] + 1) if end[1] < 12 else (end[0], 1)
    start = parse_month(args.date_from or period.get("from") or f"{default_start[0]}-{default_start[1]:02d}")
    days = list(month_days(start, end))
    if not days:
        sys.exit("Период целиком в будущем — нечего загружать.")
    month_keys = sorted({period_key(d) for d in days})
    print(f"Период: {days[0]} — {days[-1]} ({len(days)} дн.), кабинетов: {len(accounts)}")

    tables, store_rows, types_by_store, checks, failed = {}, {}, {}, {}, []
    store_sales, store_by_article, checks_extra, store_goods = {}, {}, {}, {}
    store_tax_base, store_tax_base_by_article = {}, {}
    for acc in accounts:
        name = acc.get("name") or str(acc["client_id"])
        print(f"\n[{name}] справочник видов начислений...")
        try:
            day_accruals, cache_dir = [], cache_folder(here, acc)
            types = load_types(acc, cache_dir, here, args.from_cache)
            print(f"  видов начислений: {len(types)}")
            products = load_products(acc, cache_dir, args.from_cache, args.refresh)
            print(f"  сопоставлено SKU с артикулами: {len(products)}" if products else
                  "  справочник SKU → артикул не загружен (нужен запуск без --from-cache)")
            key_expiry = load_key_expiry(acc, cache_dir, args.from_cache)
            if key_expiry:
                left = (key_expiry - date.today()).days
                print(f"  API-ключ действует до {key_expiry:%d.%m.%Y} (осталось {left} дн.)"
                      + (" — ПЕРЕВЫПУСТИТЕ КЛЮЧ в ЛК Ozon и замените api_key в ozon_accounts.json"
                         if left <= KEY_WARN_DAYS else ""))
            current, count, missing = None, 0, []
            for day in days:
                mon = day.strftime("%Y-%m")
                if mon != current:
                    if current:
                        print(f"  {current}: {count} начислений")
                    current, count = mon, 0
                if args.from_cache:
                    f = cache_dir / f"{day.isoformat()}.json"
                    if not f.exists():
                        missing.append(day)
                        continue
                    accruals = json.loads(f.read_text(encoding="utf-8"))
                else:
                    accruals = fetch_day(acc, day, cache_dir, args.refresh)
                count += len(accruals)
                day_accruals.append((day, accruals))
            print(f"  {current}: {count} начислений")
            if missing:
                print(f"  нет в кэше {len(missing)} дн.: {missing[0]} — {missing[-1]} (не учтены)")
        except RuntimeError as e:
            print(f"  ОШИБКА: {e}")
            failed.append(name)
            continue
        types_by_store[name] = types
        tables[name], store_rows[name], sales, by_article, tax_base, tax_by_article = \
            aggregate(day_accruals, types, products)
        store_sales[name] = sales
        store_by_article[name] = by_article
        store_tax_base[name] = tax_base
        store_tax_base_by_article[name] = tax_by_article
        if args.dump:
            store_goods[name] = load_goods(acc, cache_dir, args.from_cache, days[0], products)
        no_article = sum(rev for (mon, art, *_), (qty, rev) in sales.items() if art.startswith("SKU "))
        if no_article:
            checks_extra[name] = [["Выручка по SKU без артикула продавца, ₽", round(no_article, 2)]]
        undecoded = sum(v for (g, lbl), vals in tables[name].items() if lbl.startswith("Без расшифровки")
                        for v in vals.values())
        checks[name] = [["Начисления без расшифровки, ₽", round(undecoded, 2)]] + checks_extra.get(name, [])
        if missing:
            checks[name].append(["Дней нет в кэше (не учтены)", f"{len(missing)}: {missing[0]} — {missing[-1]}"])
        if key_expiry:
            checks[name].append([f"API-ключ действует до {key_expiry:%d.%m.%Y}, осталось дней",
                                 (key_expiry - date.today()).days])

    if not tables:
        sys.exit("\nНе удалось получить данные ни по одному кабинету.")

    # Не показываем пустые месяцы в начале периода
    active = [m for m in month_keys if any(abs(v.get(m, 0.0)) >= 0.005 for t in tables.values() for v in t.values())]
    if active:
        month_keys = month_keys[month_keys.index(active[0]):]

    if args.dump:
        write_dump(args.dump, "Ozon", month_keys, tables, S_INFO,
                   lambda t: dict(pnl_totals(t, month_keys)[-1]), checks, store_sales, store_by_article, store_goods,
                   store_tax_base, store_tax_base_by_article)
        print(f"Выгрузка для сводного отчёта: {args.dump}")
    if args.no_excel:
        return

    combined = defaultdict(lambda: defaultdict(float))
    for table in tables.values():
        for key, vals in table.items():
            for m, v in vals.items():
                combined[key][m] += v

    wb = Workbook()
    used = set()
    ws = wb.active
    ws.title = safe_sheet_name("Итого", used)
    write_pnl(ws, combined, month_keys, f"ОПиУ Ozon — все магазины ({len(tables)})")
    if len(tables) > 1:
        write_by_store(wb.create_sheet(safe_sheet_name("По магазинам", used)), tables, month_keys)
    for name, table in tables.items():
        write_pnl(wb.create_sheet(safe_sheet_name(name, used)), table, month_keys, f"ОПиУ Ozon — {name}")
    if not args.no_raw:
        write_raw(wb.create_sheet(safe_sheet_name("Начисления", used)), store_rows)
    write_types(wb.create_sheet(safe_sheet_name("Виды начислений", used)), types_by_store)

    out = Path(args.out) if args.out else here / f"ozon_finance_{month_keys[0]}_{month_keys[-1]}.xlsx"
    wb.save(out)
    print(f"\nГотово: {out}")
    if failed:
        print("Не загружены кабинеты: " + ", ".join(failed))


if __name__ == "__main__":
    main()
