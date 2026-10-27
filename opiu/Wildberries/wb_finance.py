"""
Финансовый отчёт Wildberries по месяцам для нескольких кабинетов.

Источник данных (WB API, категория «Финансы»):
  POST https://finance-api.wildberries.ru/api/finance/v1/sales-reports/detailed
  — детализация отчётов реализации за период (замена отключённого
    GET /api/v5/supplier/reportDetailByPeriod). Данные доступны с 29.01.2024.
  Лимит: 1 запрос в минуту на аккаунт продавца, до 100 000 строк за запрос,
  продолжение по rrdId последней строки, конец выгрузки — ответ 204.

Результат: один Excel-файл
  - «Итого»        — сводный ОПиУ по всем магазинам, месяцы в колонках
  - «По магазинам» — ключевые показатели каждого магазина по месяцам
  - лист на каждый магазин — такой же ОПиУ, как «Итого»
  - «Операции»     — суммы по видам операций WB (обоснование, тип документа, вид) за месяц
  и CSV со всеми строками отчёта (если не указан --no-raw).

Запуск:
  python wb_finance.py                          # период из config / последние 12 мес.
  python wb_finance.py --from 2026-01 --to 2026-08
  python wb_finance.py --probe                  # сохранить 20 сырых строк за прошлую неделю

Нужен пакет openpyxl:  pip install openpyxl
"""

import argparse
import csv
import gzip
import http.client
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

API_URL = "https://finance-api.wildberries.ru/api/finance/v1/sales-reports/detailed"
LIST_URL = "https://finance-api.wildberries.ru/api/finance/v1/sales-reports/list"
PAGE_LIMIT = 100000
REQUEST_INTERVAL = 61  # лимит WB: 1 запрос в минуту
DATA_AVAILABLE_FROM = date(2024, 1, 29)

# Заказы и остатки для юнит-экономики (в ОПиУ не входят)
ORDERS_URL = "https://statistics-api.wildberries.ru/api/v1/supplier/orders"   # 1 запрос в минуту
ORDERS_DAYS = 90   # WB отдаёт заказы примерно за 90 дней — более ранние копятся в кэше от запуска к запуску
REMAINS_URL = "https://seller-analytics-api.wildberries.ru/api/v1/warehouse_remains"   # остатки на складах WB
CARDS_URL = "https://content-api.wildberries.ru/content/v2/get/cards/list"
FBS_WAREHOUSES_URL = "https://marketplace-api.wildberries.ru/api/v3/warehouses"
FBS_STOCKS_URL = "https://marketplace-api.wildberries.ru/api/v3/stocks/{}"
PRICES_URL = "https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter"   # 10 запросов за 6 с
# Вид остатка (одинаковые коды во всех скриптах МП): mp — на складе маркетплейса, own — на своём складе (FBS),
# to_client — в пути к покупателям, returning — возвраты в пути на склад
REMAINS_KIND = {"В пути до получателей": "to_client", "В пути возвраты на склад WB": "returning"}
REMAINS_SKIP = {"Всего находится на складах"}   # итог по складам WB — склады и так перечислены
# Воронка продаж по товарам (аналитика WB): переходы в карточку, корзина, заказы, выкупы, отмены.
# Показов в API нет — поисковая аналитика WB доступна только с подпиской «Джем».
FUNNEL_URL = "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products"  # 3 в минуту
FUNNEL_DAYS = 365          # глубже WB не отдаёт
FUNNEL_CLOSED_DAYS = 35    # месяц, закончившийся раньше, уже не меняется (выкупы) — берётся из кэша

# Поля, которые запрашиваем (остальные не нужны для ОПиУ и только утяжеляют ответ)
FIELDS = [
    "rrdId", "reportId", "rrDate", "docTypeName", "sellerOperName", "bonusTypeName",
    "nmId", "vendorCode", "subjectName", "brandName", "quantity", "deliveryMethod", "srid",
    "retailPriceWithDisc", "retailAmount", "forPay", "acquiringFee", "vw", "vwNds", "ppvzReward",
    "deliveryService", "rebillLogisticCost", "penalty", "paidStorage", "deduction",
    "paidAcceptance", "additionalPayment", "cashbackAmount", "cashbackDiscount",
    "cashbackCommissionChange", "installmentCofinancingAmount",
]
MONEY_FIELDS = {f for f in FIELDS if f not in (
    "rrdId", "reportId", "rrDate", "docTypeName", "sellerOperName", "bonusTypeName", "nmId", "vendorCode",
    "subjectName", "brandName", "quantity", "deliveryMethod", "srid")}

# ---------------------------------------------------------------------------
# Группы статей ОПиУ
# ---------------------------------------------------------------------------
S_REVENUE = "Выручка"
S_COMMISSION = "Вознаграждение Wildberries"
G_LOGISTICS = "Логистика"
G_ACQUIRING = "Эквайринг"
G_ADS = "Реклама и продвижение"
G_STORAGE = "Хранение и приёмка"
G_PENALTY = "Штрафы"
G_OTHER = "Прочие удержания"
G_COMPENSATION = "Компенсации и доплаты"
G_ADVANCE = "Авансы за услуги WB и их возврат"
S_INFO = "Справочно"  # не входит в итоги

EXPENSE_GROUPS = [G_LOGISTICS, G_ACQUIRING, G_ADS, G_STORAGE, G_PENALTY, G_OTHER]

ADS_WORDS = ["продвиж", "реклам", "джем", "подписк", "аналитик", "медиа", "баннер", "отзыв", "лояльн", "балл"]

MONTHS_RU = "январь|февраль|март|апрель|май|июнь|июль|август|сентябрь|октябрь|ноябрь|декабрь"
_LABEL_NOISE = [
    re.compile(r",?\s*документ\s*№\s*\d+", re.I),
    re.compile(r",?\s*поставка\s*№?\s*\d+", re.I),
    re.compile(rf"\s+за\s+({MONTHS_RU})\s+\d{{4}}", re.I),
]


def clean_label(text):
    """Убирает номера документов, поставок и месяцы, чтобы однотипные удержания сложились в одну строку."""
    for rx in _LABEL_NOISE:
        text = rx.sub("", text)
    return text.strip(" ,") or text


def num(value):
    """Суммы в новом API приходят строками: "367", "-12.5"."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class WBClient:
    def __init__(self, token, log=print):
        self.token = token.strip()
        self.last_call = {}   # лимит WB свой у каждой группы методов: ключ группы -> время последнего запроса
        self.log = log

    def post(self, body, url=API_URL, retries=6):
        return self.call("POST", url, body, retries=retries)

    def call(self, method, url, body=None, interval=REQUEST_INTERVAL, limit_key="finance", need="Финансы",
             retries=6):
        """Запрос с паузой interval секунд между запросами одной группы limit_key.
        need — категория токена, без которой WB отвечает 401/403 (для подсказки в ошибке)."""
        data = json.dumps(body).encode("utf-8") if body is not None else None
        for attempt in range(1, retries + 1):
            last = self.last_call.get(limit_key, 0.0)
            wait = interval - (time.monotonic() - last)
            if last and wait > 0:
                if wait > 10:
                    self.log(f"  пауза {int(wait)} с (лимит запросов WB)...")
                time.sleep(wait)
            self.last_call[limit_key] = time.monotonic()
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": self.token, "Content-Type": "application/json", "Accept-Encoding": "gzip"})
            try:
                with urllib.request.urlopen(req, timeout=600) as resp:
                    if resp.status == 204:
                        return []
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                        raw = gzip.decompress(raw)
                    return json.loads(raw) if raw.strip() else []
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")
                if e.code == 429 and attempt < retries:
                    retry = num(e.headers.get("X-Ratelimit-Retry")) or max(interval, 5)
                    self.log(f"  429: слишком много запросов, ждём {int(retry)} с...")
                    time.sleep(retry)
                    self.last_call[limit_key] = 0.0
                    continue
                if e.code in (500, 502, 503, 504) and attempt < retries:
                    time.sleep(3)
                    continue
                hint = {401: f" — неверный токен, истёк срок действия или у токена нет категории «{need}»",
                        402: " — нужна платная подписка/доступ",
                        403: f" — у токена нет категории «{need}» или тип токена не подходит"}.get(e.code, "")
                raise RuntimeError(f"HTTP {e.code}{hint}: {text[:500]}") from None
            except (urllib.error.URLError, http.client.HTTPException, EOFError, OSError) as e:
                # обрыв соединения, недокачанный ответ, таймаут
                if attempt < retries:
                    self.log(f"  обрыв связи ({type(e).__name__}), повтор...")
                    continue
                raise RuntimeError(f"Нет соединения с API Wildberries: {e}") from None


def fetch_rows(client, date_from, date_to, period="weekly"):
    rows, rrd_id, page = {}, 0, 0
    while True:
        page += 1
        batch = client.post({
            "dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat(),
            "limit": PAGE_LIMIT, "rrdId": rrd_id, "period": period, "fields": FIELDS,
        })
        if not batch:
            break
        for r in batch:
            rows[r.get("rrdId")] = r
        client.log(f"  часть {page}: {len(batch)} строк (всего {len(rows)})")
        new_id = batch[-1].get("rrdId") or 0
        if len(batch) < PAGE_LIMIT or new_id == rrd_id:
            break
        rrd_id = new_id
    return list(rows.values())


def fetch_reports(client, date_from, date_to, period="weekly"):
    """Список отчётов реализации с итогами WB (для сверки). Данные с 01.01.2025."""
    reports, offset = [], 0
    while True:
        batch = client.post({"dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat(),
                             "limit": 1000, "offset": offset, "period": period}, url=LIST_URL)
        reports.extend(batch or [])
        if not batch or len(batch) < 1000:
            return reports
        offset += len(batch)


def closed_weeks(data, d_to):
    """Из прошлой загрузки — закрытые еженедельные отчёты (WB их больше не меняет) и их строки.
    Ежедневные отчёты не берём: их WB заменяет недельным, они загружаются заново.
    Возвращает (rows, reports) или None, если догружать не от чего."""
    reports = data.get("reports") or []
    daily_ids = set(data.get("daily_ids") or [])
    daily = data.get("daily")
    if not daily_ids and daily:
        # кэш до появления daily_ids: ежедневные отчёты — однодневные, начиная с первого ежедневного дня
        daily_ids = {r.get("reportId") for r in reports
                     if str(r.get("dateFrom"))[:10] == str(r.get("dateTo"))[:10] >= daily[0]}
    weekly = [r for r in reports if r.get("reportId") not in daily_ids and str(r.get("dateTo") or "")[:10]
              and str(r.get("dateTo"))[:10] <= d_to.isoformat()]
    if not weekly:
        return None
    ids = {r.get("reportId") for r in weekly}
    return [r for r in data.get("rows") or [] if r.get("reportId") in ids], weekly


def load_account(acc, d_from, d_to, cache_dir, from_cache, reload=False):
    """Строки и список отчётов кабинета: закрытые недели — из еженедельных отчётов WB,
    дни после последней закрытой недели (по вчерашний) — из ежедневных отчётов.
    Закрытые недели прошлой загрузки берутся из кэша, из API — только новые (reload — загрузить всё заново).
    Возвращает (rows, reports, daily), daily = [первый день, последний день] ежедневных отчётов или None."""
    name = acc.get("name") or "Магазин"

    def log(text):
        print(f"[{name}] {text}", flush=True)

    cache_file = cache_dir / f"{safe_name(name)}_{d_from}_{d_to}.json"
    candidates = sorted(cache_dir.glob(f"{safe_name(name)}_{d_from}_*.json"))
    if from_cache:
        if not cache_file.exists():
            if not candidates:
                raise RuntimeError(f"нет кэша {cache_file.name} — запустите без --from-cache")
            cache_file = candidates[-1]
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        rows, reports = data["rows"], data.get("reports") or []
        log(f"из кэша {cache_file.name}: {len(rows)} строк, {len(reports)} отчётов")
        return rows, reports, data.get("daily")

    client = WBClient(acc["token"], log)
    base = None
    if candidates and not reload:
        prev = cache_file if cache_file.exists() else candidates[-1]
        try:
            base = closed_weeks(json.loads(prev.read_text(encoding="utf-8")), d_to)
        except (OSError, ValueError, KeyError) as e:
            log(f"кэш {prev.name} не прочитан ({e}), загружаю всё заново")
    if base:
        rows, reports = base
        last = max(str(r.get("dateTo"))[:10] for r in reports)
        week_from = date.fromisoformat(last) + timedelta(days=1)
        log(f"из кэша: {len(reports)} закрытых отчётов по {last}, {len(rows)} строк")
        # Недельный отчёт WB формирует после воскресенья: пока неделя не прошла, спрашивать о нём незачем
        # (лимит — 1 запрос в минуту)
        if week_from + timedelta(days=7) <= d_to:
            known = {r.get("reportId") for r in reports}
            log(f"догрузка еженедельных отчётов {week_from} — {d_to}...")
            new_rows = [r for r in fetch_rows(client, week_from, d_to) if r.get("reportId") not in known]
            try:
                new_reports = [r for r in fetch_reports(client, week_from, d_to) if r.get("reportId") not in known]
            except RuntimeError as e:
                log(f"  список отчётов не получен, сверки новых недель не будет: {e}")
                new_reports = []
            log(f"  новых отчётов: {len(new_reports)}, строк: {len(new_rows)}")
            rows, reports = rows + new_rows, reports + new_reports
    else:
        log("загрузка еженедельных отчётов реализации...")
        rows = fetch_rows(client, d_from, d_to)
        log("загрузка списка отчётов для сверки...")
        try:
            reports = fetch_reports(client, d_from, d_to)
        except RuntimeError as e:
            log(f"  список отчётов не получен, сверки не будет: {e}")
            reports = []

    # Еженедельный отчёт WB формирует в понедельник за прошлую неделю. Дни после него есть
    # только в ежедневных отчётах — берём их, чтобы видеть данные по вчерашний день.
    ends = [str(r.get("dateTo") or "")[:10] for r in reports] or [str(r.get("rrDate") or "")[:10] for r in rows]
    ends = [e for e in ends if e]
    weekly_end = date.fromisoformat(max(ends)) if ends else d_from - timedelta(days=1)
    daily, daily_ids = None, []
    if weekly_end < d_to:
        day_from = weekly_end + timedelta(days=1)
        log(f"загрузка ежедневных отчётов {day_from} — {d_to} (неделя ещё не закрыта)...")
        try:
            day_rows = fetch_rows(client, day_from, d_to, period="daily")
            day_reports = fetch_reports(client, day_from, d_to, period="daily")
            ids = {r.get("reportId") for r in day_reports}
            weekly_ids = {r.get("reportId") for r in rows}
            day_rows = [r for r in day_rows if (not ids or r.get("reportId") in ids)
                        and r.get("reportId") not in weekly_ids]
            rows = rows + day_rows
            reports = reports + day_reports
            daily_ids = [r.get("reportId") for r in day_reports]
            days = sorted(str(r.get("rrDate") or "")[:10] for r in day_rows if r.get("rrDate"))
            if days:
                daily = [days[0], days[-1]]
            log(f"  ежедневных отчётов: {len(day_reports)}, строк: {len(day_rows)}"
                + (f", дни {daily[0]} — {daily[1]}" if daily else ""))
        except RuntimeError as e:
            log(f"  ежедневные отчёты не получены, данные только по {weekly_end}: {e}")

    cache_dir.mkdir(exist_ok=True)
    # Через временный файл: оборванная запись не испортит кэш, от которого догружается следующий запуск
    tmp = cache_file.with_suffix(".tmp")
    tmp.write_text(json.dumps({"rows": rows, "reports": reports, "daily": daily, "daily_ids": daily_ids},
                              ensure_ascii=False), encoding="utf-8")
    tmp.replace(cache_file)
    # Старые выгрузки этого кабинета за тот же период не нужны: новая их полностью заменяет
    for old in cache_dir.glob(f"{safe_name(name)}_{d_from}_*.json"):
        if old != cache_file:
            old.unlink()
    return rows, reports, daily


# ---------------------------------------------------------------------------
# Заказы и остатки (юнит-экономика)
# ---------------------------------------------------------------------------
def fetch_orders(client, since):
    """Строки заказов, изменившиеся начиная с since (по lastChangeDate), — с продолжением по дате последней строки."""
    rows = []
    for _ in range(100):
        batch = client.call("GET", f"{ORDERS_URL}?{urllib.parse.urlencode({'dateFrom': since, 'flag': 0})}",
                            limit_key="statistics", need="Статистика") or []
        rows.extend(batch)
        last = batch[-1].get("lastChangeDate") if batch else None
        if len(batch) < 80000 or not last or last == since:
            break
        since = last
    return rows


def fetch_remains(client):
    """Остатки на складах WB и в пути: [[артикул, склад, вид, шт]] (асинхронный отчёт: задание → статус → файл)."""
    task = client.call("GET", f"{REMAINS_URL}?groupBySa=true&groupByNm=true", limit_key="remains_create",
                       need="Аналитика")
    task_id = ((task or {}).get("data") or {}).get("taskId")
    if not task_id:
        raise RuntimeError(f"WB не вернул номер задания на отчёт об остатках: {task}")
    for _ in range(120):
        status = client.call("GET", f"{REMAINS_URL}/tasks/{task_id}/status", interval=5,
                             limit_key="remains_status", need="Аналитика")
        state = ((status or {}).get("data") or {}).get("status")
        if state == "done":
            break
        if state in ("canceled", "purged"):
            raise RuntimeError(f"отчёт об остатках не сформирован (статус {state})")
    else:
        raise RuntimeError("отчёт об остатках не сформирован за 10 минут")
    rows = []
    for item in client.call("GET", f"{REMAINS_URL}/tasks/{task_id}/download", limit_key="remains_download",
                            need="Аналитика") or []:
        article = item.get("vendorCode") or f"nmId {item.get('nmId')}"
        for w in item.get("warehouses") or []:
            name, qty = w.get("warehouseName") or "", num(w.get("quantity"))
            if qty and name not in REMAINS_SKIP:
                rows.append([article, name, REMAINS_KIND.get(name, "mp"), qty])
    return rows


def fetch_cards(client):
    """Все карточки товаров кабинета (content-api cards/list)."""
    cards, cursor = [], {"limit": 100}
    for _ in range(1000):
        res = client.call("POST", CARDS_URL, {"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}},
                          interval=0.7, limit_key="content", need="Контент") or {}
        page = res.get("cards") or []
        cards += page
        nxt = res.get("cursor") or {}
        if len(page) < 100 or not nxt.get("updatedAt"):
            break
        cursor = {"limit": 100, "updatedAt": nxt["updatedAt"], "nmID": nxt.get("nmID")}
    return cards


# Кратность продажи из характеристик карточки: первая заполненная по порядку
MULTIPLE_CHARS = ["Количество предметов в упаковке", "Количество в упаковке (шт.)"]


def card_multiples(cards):
    """Кратность продажи по артикулам: [[артикул, шт, характеристика]]. Значение бывает числом,
    строкой или списком строк вида «26 шт.» — берётся первое число."""
    rows = []
    for card in cards:
        chars = {ch.get("name"): ch.get("value") for ch in card.get("characteristics") or []}
        for title in MULTIPLE_CHARS:
            value = chars.get(title)
            value = value[0] if isinstance(value, list) and value else value
            m = re.search(r"\d+(?:[.,]\d+)?", str(value if value is not None else ""))
            if m and float(m.group().replace(",", ".")) > 0:
                rows.append([card.get("vendorCode") or f"nmId {card.get('nmID')}",
                             float(m.group().replace(",", ".")), title])
                break
    return rows


def fetch_fbs_stocks(client, cards):
    """Остатки на своих складах (FBS/DBS): [[артикул, склад, вид, шт]]. Нужны размеры карточек (chrtID)."""
    warehouses = client.call("GET", FBS_WAREHOUSES_URL, interval=0.3, limit_key="marketplace",
                             need="Маркетплейс") or []
    if not warehouses:
        return []
    sizes = {}
    for card in cards:
        for size in card.get("sizes") or []:
            if size.get("chrtID"):
                sizes[size["chrtID"]] = card.get("vendorCode") or f"nmId {card.get('nmID')}"
    ids, rows = list(sizes), []
    for w in warehouses:
        for i in range(0, len(ids), 1000):
            res = client.call("POST", FBS_STOCKS_URL.format(w["id"]), {"chrtIds": ids[i:i + 1000]},
                              interval=0.3, limit_key="marketplace", need="Маркетплейс") or {}
            for s in res.get("stocks") or []:
                if num(s.get("amount")):
                    rows.append([sizes.get(s.get("chrtId"), s.get("sku") or ""), w.get("name") or str(w["id"]),
                                 "own", num(s["amount"])])
    return rows


def fetch_prices(client):
    """Текущие цены по размерам: [[артикул, цена до скидки, цена со скидкой продавца, None]].
    Цены с СПП (для покупателя) WB в API не отдаёт."""
    rows, offset = [], 0
    for _ in range(1000):
        res = client.call("GET", f"{PRICES_URL}?{urllib.parse.urlencode({'limit': 1000, 'offset': offset})}",
                          interval=0.7, limit_key="prices", need="Цены и скидки") or {}
        goods = (res.get("data") or {}).get("listGoods") or [] if isinstance(res, dict) else []
        for g in goods:
            article = g.get("vendorCode") or f"nmId {g.get('nmID')}"
            for size in g.get("sizes") or []:
                price = num(size.get("discountedPrice")) or num(size.get("price"))
                if price:
                    rows.append([article, num(size.get("price")) or None, price, None])
        offset += len(goods)
        if len(goods) < 1000:
            break
    return rows


def fetch_funnel(client, first, last):
    """Воронка за период по артикулам: [[артикул, показы, переходы, корзина, заказано шт, заказано ₽,
    выкуплено шт, отменено шт, возвращено шт]]; показов и возвратов у WB нет — None."""
    rows, offset = [], 0
    for _ in range(200):
        res = client.call("POST", FUNNEL_URL, {"selectedPeriod": {"start": str(first), "end": str(last)},
                                               "limit": 1000, "offset": offset},
                          interval=21, limit_key="funnel", need="Аналитика") or {}
        products = (res.get("data") or {}).get("products") or []
        for pr in products:
            info, st = pr.get("product") or {}, (pr.get("statistic") or {}).get("selected") or {}
            values = [num(st.get(k)) for k in ("openCount", "cartCount", "orderCount", "orderSum", "buyoutCount",
                                               "cancelCount")]
            if any(values):
                rows.append([info.get("vendorCode") or f"nmId {info.get('nmId')}", None] + values + [None])
        offset += len(products)
        if len(products) < 1000:
            break
    return rows


def load_funnel(client, goods, d_from, log):
    """Воронка по месяцам в goods["funnel"] = {месяц: строки}; закрытые месяцы не перекачиваются."""
    today = date.today()
    funnel = goods.setdefault("funnel", {})
    first = max(d_from, today - timedelta(days=FUNNEL_DAYS - 1))
    month = date(first.year, first.month, 1)
    fetched = 0
    while month <= today:
        nxt = date(month.year + (month.month == 12), 1 if month.month == 12 else month.month + 1, 1)
        end = min(nxt - timedelta(days=1), today)
        key = month.strftime("%Y-%m")
        if not (key in funnel and end <= today - timedelta(days=FUNNEL_CLOSED_DAYS)):
            funnel[key] = fetch_funnel(client, max(month, first), end)
            fetched += 1
        month = nxt
    goods["funnel_from"] = min(filter(None, [goods.get("funnel_from"), first.isoformat()]))
    log(f"  воронка: обновлено месяцев {fetched}, в кэше {len(funnel)}")


def load_goods(acc, d_from, cache_dir, from_cache, log):
    """Заказы и остатки кабинета: {"orders": {srid: [дата, артикул, цена продавца, отменён]}, "orders_from",
    "last_change", "stocks": [[артикул, склад, вид, шт]], "stocks_at"}. Ошибки не прерывают отчёт:
    пишутся в лог, остаются данные прошлой загрузки."""
    cache_file = cache_dir / f"{safe_name(acc.get('name') or 'Магазин')}_goods.json"
    goods = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() else {}
    if from_cache:
        return goods
    client = WBClient(acc["token"], log)
    orders = goods.setdefault("orders", {})
    start = max(d_from, date.today() - timedelta(days=ORDERS_DAYS - 1)).isoformat()
    since = goods.get("last_change") if orders and (goods.get("last_change") or "") >= start else start
    log(f"загрузка заказов с {since[:10]}...")
    try:
        rows = fetch_orders(client, since)
        for r in rows:
            key = r.get("srid") or f"{r.get('gNumber')}/{r.get('nmId')}/{r.get('date')}"
            orders[key] = [str(r.get("date") or "")[:10], r.get("supplierArticle") or f"nmId {r.get('nmId')}",
                           round(num(r.get("priceWithDisc")), 2), bool(r.get("isCancel"))]
        goods["last_change"] = max([goods.get("last_change") or ""] +
                                   [str(r.get("lastChangeDate") or "") for r in rows]) or since
        goods["orders_from"] = min(goods.get("orders_from") or start, start)
        log(f"  заказов: {len(rows)} новых/изменённых, всего в кэше {len(orders)}")
    except RuntimeError as e:
        log(f"  ОШИБКА: заказы не получены (в юнит-экономике останутся данные прошлой загрузки): {e}")
    log("загрузка карточек товаров (кратность продажи)...")
    cards = None
    try:
        cards = fetch_cards(client)
        goods["multiples"], goods["multiples_at"] = card_multiples(cards), time.strftime("%Y-%m-%d %H:%M")
        log(f"  карточек: {len(cards)}, с кратностью: {len(goods['multiples'])}")
    except RuntimeError as e:
        log(f"  ОШИБКА: карточки не получены (кратность — на {goods.get('multiples_at') or '—'}): {e}")
    log("загрузка остатков...")
    try:
        stocks = fetch_remains(client) + fetch_fbs_stocks(client, cards if cards is not None else fetch_cards(client))
        goods["stocks"], goods["stocks_at"] = stocks, time.strftime("%Y-%m-%d %H:%M")
        log(f"  остатков: {sum(s[3] for s in stocks):,.0f} шт".replace(",", " "))
    except RuntimeError as e:
        log(f"  ОШИБКА: остатки не получены (в юнит-экономике — остатки на {goods.get('stocks_at') or '—'}): {e}")
    log("загрузка цен...")
    try:
        prices = fetch_prices(client)
        goods["prices"], goods["prices_at"] = prices, time.strftime("%Y-%m-%d %H:%M")
        log(f"  цен: {len(prices)}")
    except RuntimeError as e:
        log(f"  ОШИБКА: цены не получены (в юнит-экономике — цены на {goods.get('prices_at') or '—'}): {e}")
    log("загрузка воронки продаж...")
    try:
        load_funnel(client, goods, d_from, log)
    except RuntimeError as e:
        log(f"  ОШИБКА: воронка продаж не получена (в юнит-экономике — данные прошлой загрузки): {e}")
    cache_dir.mkdir(exist_ok=True)
    cache_file.write_text(json.dumps(goods, ensure_ascii=False), encoding="utf-8")
    return goods


def goods_dump(goods):
    """Поля выгрузки для оркестратора: заказы по периодам и артикулам, остатки на дату."""
    ordered = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for day, article, price, cancelled in (goods.get("orders") or {}).values():
        if day:
            row = ordered[(period_key(day), article)]
            row[0] += 1
            row[1] += price
            if cancelled:
                row[2] += 1
                row[3] += price
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


# ---------------------------------------------------------------------------
# Разбор строк отчёта в статьи ОПиУ
# ---------------------------------------------------------------------------
def split_row(r):
    """Список (группа, подпись, сумма). Сумма частей (без «Справочно») = вклад строки в «Итого к оплате» WB:
    ± к перечислению − логистика − штрафы − хранение − удержания − приёмка
    + корректировка ВВ − баллы лояльности − стоимость участия в лояльности.
    """
    parts = []
    doc = (r.get("docTypeName") or "").strip()
    oper = (r.get("sellerOperName") or "").strip()
    bonus = clean_label((r.get("bonusTypeName") or "").strip())
    sign = -1 if doc.lower().startswith("возврат") else 1

    retail = num(r.get("retailAmount"))          # цена после скидки WB (СПП) — «Вайлдберриз реализовал»
    for_pay = num(r.get("forPay"))
    acquiring = num(r.get("acquiringFee"))
    qty = int(num(r.get("quantity"))) or 1
    seller_price = num(r.get("retailPriceWithDisc")) * qty or retail  # цена продавца с его скидкой

    if retail == 0 and for_pay != 0:
        # Компенсации ущерба, коррекции продаж и т.п. — деньги без продажи товара
        parts.append((G_COMPENSATION, oper or "Компенсация", sign * for_pay))
    elif retail or for_pay or acquiring:
        parts.append((S_REVENUE, "Возвраты" if sign < 0 else "Продажи (по цене продавца)", sign * seller_price))
        if acquiring:
            parts.append((G_ACQUIRING, "Эквайринг / платёжные услуги", -sign * acquiring))
        commission = seller_price - for_pay - acquiring
        if abs(commission) >= 0.005:
            parts.append((S_COMMISSION, "Комиссия WB (кВВ с НДС, выдача на ПВЗ)", -sign * commission))
        spp = seller_price - retail
        if abs(spp) >= 0.005:
            parts.append((S_INFO, "Скидка WB покупателю (СПП) — оплачивает WB, в итоги не входит", sign * spp))

    def add(field, group, label, direction=-1):
        v = num(r.get(field))
        if v:
            parts.append((group, label, direction * v))

    add("deliveryService", G_LOGISTICS, f"Логистика: {bonus}" if bonus else (oper or "Логистика"))
    # Сверено с bankPaymentSum из /sales-reports/list: эти два поля WB в «Итого к оплате» не учитывает
    add("rebillLogisticCost", S_INFO, "Возмещение издержек по перевозке (WB не вычитает из выплаты)")
    add("cashbackDiscount", S_INFO, "Компенсация скидки по программе лояльности (уже в «к перечислению»)", +1)
    add("penalty", G_PENALTY, bonus or "Штрафы")
    add("paidStorage", G_STORAGE, "Хранение")
    add("paidAcceptance", G_STORAGE, "Операции на приёмке")
    # При возврате WB отдаёт удержанные баллы и плату за участие обратно (сверено с bankPaymentSum)
    add("cashbackAmount", G_ADS, "Баллы программы лояльности", -sign)
    add("cashbackCommissionChange", G_ADS, "Стоимость участия в программе лояльности", -sign)

    deduction = num(r.get("deduction"))
    if deduction:
        label = bonus or oper or "Удержания"
        text = label.lower()
        if "аванс" in text:
            group = G_ADVANCE
        elif any(w in text for w in ADS_WORDS):
            group = G_ADS
        else:
            group = G_OTHER
        parts.append((group, label, -deduction))

    extra = num(r.get("additionalPayment"))
    if extra:
        parts.append((G_COMPENSATION if extra > 0 else S_COMMISSION, "Корректировка вознаграждения WB", extra))
    return parts


# Разрез выгрузки: month — ключ «ГГГГ-ММ», day — «ГГГГ-ММ-ДД» (нужен своду для отчётов по неделям и дням)
GRAIN = "month"


def period_key(value):
    """Ключ периода из даты строки отчёта: месяц или день — в зависимости от --grain."""
    return str(value or "")[:10 if GRAIN == "day" else 7]


def aggregate(rows, month_set):
    table = defaultdict(lambda: defaultdict(float))
    ops = defaultdict(lambda: [0, 0.0, 0.0, 0.0])  # (месяц, обоснование, док, вид) -> строк, реализовано, к перечислению, вклад
    report_net = defaultdict(float)                # reportId -> «к оплате» по строкам (для сверки, без фильтра периода)
    sales = defaultdict(lambda: [0.0, 0.0])        # (месяц, артикул продавца, дата, srid заказа) -> штук, выручка (для себестоимости)
    by_article = defaultdict(float)                # (месяц, артикул, группа, подпись) -> сумма (юнит-экономика)
    tax_base = defaultdict(float)                  # период -> фактически оплачено покупателями
    tax_base_by_article = defaultdict(float)       # (период, артикул) -> фактически оплачено покупателями
    catalog = defaultdict(lambda: (defaultdict(int), defaultdict(int)))  # артикул -> бренды, предметы (счётчики)
    skipped = 0
    for r in rows:
        parts = split_row(r)
        net = sum(p[2] for p in parts if p[0] != S_INFO)
        report_net[r.get("reportId")] += net
        mon = period_key(r.get("rrDate"))
        if mon not in month_set:
            skipped += 1
            continue
        article = str(r.get("vendorCode") or "").strip()
        if article:
            brands, subjects = catalog[article]
            brand, subject = (str(r.get(f) or "").strip() for f in ("brandName", "subjectName"))
            if brand:
                brands[brand] += 1
            if subject:
                subjects[subject] += 1
        for group, label, value in parts:
            table[(group, label)][mon] += value
            if group != S_INFO:
                by_article[(mon, article, group, label)] += value
        oper = (r.get("sellerOperName") or "").strip().lower()
        # Товар продан или возвращён только в строках «Продажа» / «Возврат». «Коррекция продаж», компенсации
        # лояльности и т.п. имеют тот же docType и quantity=1, но товар по ним не движется — штуки не считаем.
        qty = num(r.get("quantity"))
        price = num(r.get("retailPriceWithDisc")) * qty or num(r.get("retailAmount"))
        if article and oper in ("продажа", "возврат") and (qty or price):
            sign = -1 if oper == "возврат" else 1
            sold = sales[(mon, str(r["vendorCode"]).strip(), str(r.get("rrDate") or "")[:10],
                          str(r.get("srid") or ""))]
            sold[0] += sign * qty
            sold[1] += sign * price
            paid = num(r.get("retailAmount"))
            tax_base[mon] += sign * paid
            tax_base_by_article[(mon, article)] += sign * paid
        key = (mon, r.get("sellerOperName") or "", r.get("docTypeName") or "", clean_label(r.get("bonusTypeName") or ""))
        o = ops[key]
        o[0] += 1
        o[1] += num(r.get("retailAmount"))
        o[2] += num(r.get("forPay"))
        o[3] += net
    return table, ops, report_net, sales, skipped, by_article, catalog, tax_base, tax_base_by_article


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------
NUM = '#,##0.00;[Red]-#,##0.00;"–"'
PCT = '0.0%;[Red]-0.0%;"–"'
F_HEADER = PatternFill("solid", fgColor="7B2D8E")
F_SECTION = PatternFill("solid", fgColor="EADCF0")
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
        for c, v in enumerate(["Статья"] + [month_title(m) for m in months] + ["Итого"], 1):
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


def ratio(num_, den, months):
    return {m: (num_.get(m, 0.0) / den[m] if den.get(m) else 0.0) for m in months}


def has_group(table, group):
    return any(s == group for s, _ in table)


def pnl_totals(table, months):
    def section_rows(section):
        return [v for (s, _), v in table.items() if s == section]

    revenue = sum_rows(section_rows(S_REVENUE), months)
    commission = sum_rows(section_rows(S_COMMISSION), months)
    groups = {g: sum_rows(section_rows(g), months) for g in EXPENSE_GROUPS}
    compensation = sum_rows(section_rows(G_COMPENSATION), months)
    advance = sum_rows(section_rows(G_ADVANCE), months)
    expenses = sum_rows([commission] + list(groups.values()), months)
    net = sum_rows([revenue, expenses, compensation, advance], months)
    return revenue, commission, groups, compensation, advance, expenses, net


def write_pnl(ws, table, months, title):
    w = SheetWriter(ws, months, title)
    revenue, commission, groups, compensation, advance, expenses, net = pnl_totals(table, months)

    def detail(section):
        items = sorted(((lbl, v) for (s, lbl), v in table.items() if s == section),
                       key=lambda x: -abs(sum(x[1].values())))
        for lbl, vals in items:
            w.line(lbl, vals, indent=2)

    w.section(S_REVENUE.upper() + " (по цене продавца с его скидкой)")
    detail(S_REVENUE)
    w.line("Выручка за вычетом возвратов", revenue, fill=F_TOTAL, font=BOLD)
    w.blank()

    w.section("РАСХОДЫ WILDBERRIES")
    if has_group(table, S_COMMISSION):
        w.line(S_COMMISSION, commission, font=BOLD, indent=1)
        detail(S_COMMISSION)
    for g in EXPENSE_GROUPS:
        if has_group(table, g):
            w.line(g, groups[g], font=BOLD, indent=1)
            detail(g)
    w.line("Итого расходы WB", expenses, fill=F_TOTAL, font=BOLD)
    w.blank()

    if has_group(table, G_COMPENSATION):
        w.section(G_COMPENSATION.upper())
        detail(G_COMPENSATION)
        w.line("Итого компенсации", compensation, fill=F_TOTAL, font=BOLD)
        w.blank()

    if has_group(table, G_ADVANCE):
        w.section(G_ADVANCE.upper() + " (движение денег, не расход месяца)")
        detail(G_ADVANCE)
        w.line("Итого по авансам (списано − возвращено)", advance, fill=F_TOTAL, font=BOLD)
        w.blank()

    w.line("ИТОГО К ОПЛАТЕ ПРОДАВЦУ", net, fill=F_RESULT, font=Font(bold=True, size=12))
    w.blank()

    if has_group(table, S_INFO):
        w.section(S_INFO.upper())
        detail(S_INFO)
        w.blank()

    w.section("ДОЛЯ ОТ ВЫРУЧКИ")
    rev_total = sum(revenue.values())
    share = lambda vals: (-sum(vals.values()) / rev_total) if rev_total else 0.0
    neg = lambda vals: {m: -v for m, v in vals.items()}
    w.line("Все расходы WB", ratio(neg(expenses), revenue, months), fmt=PCT, indent=1, total=share(expenses))
    if has_group(table, S_COMMISSION):
        w.line(S_COMMISSION, ratio(neg(commission), revenue, months), fmt=PCT, indent=2, total=share(commission))
    for g in EXPENSE_GROUPS:
        if has_group(table, g):
            w.line(g, ratio(neg(groups[g]), revenue, months), fmt=PCT, indent=2, total=share(groups[g]))
    w.line("К оплате продавцу", ratio(net, revenue, months), fmt=PCT, indent=1,
           total=(sum(net.values()) / rev_total) if rev_total else 0.0)


def write_by_store(ws, tables, months):
    w = SheetWriter(ws, months, "Ключевые показатели по магазинам")
    for store, table in tables.items():
        revenue, commission, groups, compensation, advance, expenses, net = pnl_totals(table, months)
        w.section(store)
        w.line("Выручка за вычетом возвратов", revenue, indent=1)
        w.line(S_COMMISSION, commission, indent=1)
        for g in EXPENSE_GROUPS:
            if has_group(table, g):
                w.line(g, groups[g], indent=1)
        w.line("Итого расходы WB", expenses, indent=1, font=BOLD)
        w.line("Компенсации и доплаты", compensation, indent=1)
        if has_group(table, G_ADVANCE):
            w.line("Авансы (списано − возвращено)", advance, indent=1)
        w.line("К оплате продавцу", net, indent=1, fill=F_RESULT, font=BOLD)
        rev_total = sum(revenue.values())
        w.line("Расходы, % от выручки", ratio({m: -v for m, v in expenses.items()}, revenue, months),
               fmt=PCT, indent=1, total=(-sum(expenses.values()) / rev_total) if rev_total else 0.0)
        w.blank()


def write_ops(ws, store_ops):
    cols = ["Магазин", "Месяц", "Обоснование для оплаты", "Тип документа", "Вид (штраф/логистика/удержание)",
            "Строк", "Вайлдберриз реализовал", "К перечислению за товар", "Вклад в «К оплате»"]
    ws.append(cols)
    for store, ops in store_ops.items():
        for (mon, oper, doc, bonus), (cnt, retail, for_pay, net) in sorted(ops.items()):
            ws.append([store, mon, oper, doc, bonus, cnt, round(retail, 2), round(for_pay, 2), round(net, 2)])
    widths = [18, 9, 36, 14, 50, 8, 18, 18, 18]
    for c, wdt in enumerate(widths, 1):
        ws.cell(1, c).font, ws.cell(1, c).fill = WHITE_BOLD, F_HEADER
        ws.column_dimensions[get_column_letter(c)].width = wdt
    for c in (7, 8, 9):
        for (cell,) in ws.iter_rows(min_row=2, min_col=c, max_col=c):
            cell.number_format = NUM
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def write_reconciliation(ws, store_reports, store_report_net):
    ws.append(["Магазин", "ID отчёта", "Период с", "Период по", "Тип отчёта",
               "Итого к оплате (WB)", "Итого к оплате (по строкам)", "Разница"])
    for store, reports in store_reports.items():
        nets = store_report_net.get(store, {})
        for rep in sorted(reports, key=lambda x: (x.get("dateFrom") or "", x.get("reportId") or 0)):
            wb_sum = num(rep.get("bankPaymentSum"))
            ours = nets.get(rep.get("reportId"))
            ws.append([store, str(rep.get("reportId")), rep.get("dateFrom"), rep.get("dateTo"), rep.get("reportType"),
                       round(wb_sum, 2), None if ours is None else round(ours, 2),
                       None if ours is None else round(ours - wb_sum, 2)])
    widths = [18, 14, 12, 12, 10, 20, 24, 14]
    for c, wdt in enumerate(widths, 1):
        ws.cell(1, c).font, ws.cell(1, c).fill = WHITE_BOLD, F_HEADER
        ws.column_dimensions[get_column_letter(c)].width = wdt
    for c in (6, 7, 8):
        for (cell,) in ws.iter_rows(min_row=2, min_col=c, max_col=c):
            cell.number_format = NUM
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def reconciliation_summary(reports, report_net):
    wb_total = sum(num(r.get("bankPaymentSum")) for r in reports)
    ours = sum(report_net.get(r.get("reportId"), 0.0) for r in reports)
    bad = [r for r in reports if abs(report_net.get(r.get("reportId"), 0.0) - num(r.get("bankPaymentSum"))) >= 1]
    return wb_total, ours, bad


def write_csv(path, store_rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        wr = csv.writer(fh, delimiter=";")
        wr.writerow(["store"] + FIELDS)
        for store, rows in store_rows.items():
            for r in rows:
                wr.writerow([store] + [str(r.get(f, "")).replace(".", ",") if f in MONEY_FIELDS
                                       else r.get(f, "") for f in FIELDS])


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


def catalog_rows(catalog):
    """[[артикул, бренд, предмет]] — самые частые значения по каждому артикулу (WB иногда пишет их не во всех
    строках отчёта, поэтому берём преобладающее)."""
    def top(counter):
        return max(counter.items(), key=lambda kv: (kv[1], kv[0]))[0] if counter else ""

    rows = [[article, top(brands), top(subjects)] for article, (brands, subjects) in (catalog or {}).items()]
    return sorted(row for row in rows if row[1] or row[2])


def write_dump(path, marketplace, months, tables, info_group, net_fn, checks, sales=None, by_article=None,
               catalog=None, goods=None, tax_base=None, tax_base_by_article=None):
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
            # Налоговая база: фактическая цена покупки после скидок WB, возвраты со знаком минус.
            "tax_base": {m: round(v, 2) for m, v in (tax_base or {}).get(name, {}).items()
                         if abs(v) >= 0.005},
            "tax_base_by_article": [[mon, art, round(value, 2)]
                                    for (mon, art), value in (tax_base_by_article or {}).get(name, {}).items()
                                    if abs(value) >= 0.005],
            # Справочник товаров: артикул, бренд, предмет (категория WB) — для юнит-экономики
            "catalog": catalog_rows((catalog or {}).get(name)),
            # Заказы [[месяц, артикул, шт, ₽, отменено шт, отменено ₽]] и остатки [[артикул, склад, вид, шт]]
            **goods_dump((goods or {}).get(name) or {}),
        }
    Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def safe_name(name, used=None, limit=31):
    base = re.sub(r'[\[\]:*?/\\]', "", name)[:limit] or "Магазин"
    if used is None:
        return base
    title, n = base, 2
    while title.lower() in used:
        suffix = f" ({n})"
        title, n = base[:limit - len(suffix)] + suffix, n + 1
    used.add(title.lower())
    return title


# ---------------------------------------------------------------------------
def parse_month(s):
    y, m = s.split("-")
    return int(y), int(m)


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="Финансы Wildberries по месяцам для нескольких кабинетов")
    ap.add_argument("--config", default=str(here / "wb_accounts.json"))
    ap.add_argument("--from", dest="date_from", help="Первый месяц, ГГГГ-ММ")
    ap.add_argument("--to", dest="date_to", help="Последний месяц, ГГГГ-ММ")
    ap.add_argument("--out", help="Путь к Excel-файлу")
    ap.add_argument("--no-raw", action="store_true", help="Не сохранять CSV со всеми строками отчёта")
    ap.add_argument("--probe", action="store_true", help="Сохранить 20 сырых строк за прошлую неделю и выйти")
    ap.add_argument("--dump", help="Сохранить таблицы ОПиУ в JSON для сводного отчёта (opiu_all.py)")
    ap.add_argument("--no-excel", action="store_true", help="Не сохранять Excel и CSV (удобно вместе с --dump)")
    ap.add_argument("--grain", choices=("month", "day"), default="month",
                    help="Разрез выгрузки: month — по месяцам (по умолчанию), day — по дням")
    ap.add_argument("--from-cache", action="store_true",
                    help="Не обращаться к API, собрать отчёт из данных прошлой загрузки (папка cache)")
    ap.add_argument("--reload", action="store_true",
                    help="Загрузить все недели заново (по умолчанию закрытые недели берутся из кэша)")
    args = ap.parse_args()
    global GRAIN
    GRAIN = args.grain

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    accounts = [a for a in cfg["accounts"] if a.get("enabled", True)]
    if not accounts:
        sys.exit("В конфиге нет активных кабинетов")

    today = date.today()
    if args.probe:
        for acc in accounts:
            name = acc.get("name") or "Магазин"
            body = {"dateFrom": (today - timedelta(days=14)).isoformat(), "dateTo": today.isoformat(),
                    "limit": 20, "rrdId": 0, "period": "weekly"}
            rows = WBClient(acc["token"]).post(body)
            out = here / f"wb_probe_{safe_name(name)}.json"
            out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[{name}] строк: {len(rows)} -> {out}")
        return

    period = cfg.get("period") or {}
    end = parse_month(args.date_to or period.get("to") or f"{today.year}-{today.month:02d}")
    default_start = (end[0] - 1, end[1] + 1) if end[1] < 12 else (end[0], 1)
    start = parse_month(args.date_from or period.get("from") or f"{default_start[0]}-{default_start[1]:02d}")

    d_from = max(date(start[0], start[1], 1), DATA_AVAILABLE_FROM)
    d_to = min(date(end[0] + (end[1] == 12), 1 if end[1] == 12 else end[1] + 1, 1) - timedelta(days=1), today)
    if d_from > d_to:
        sys.exit("Пустой период.")
    if GRAIN == "day":
        month_keys = [(d_from + timedelta(days=i)).isoformat() for i in range((d_to - d_from).days + 1)]
    else:
        month_keys, d = [], date(d_from.year, d_from.month, 1)
        while d <= d_to:
            month_keys.append(d.strftime("%Y-%m"))
            d = date(d.year + (d.month == 12), 1 if d.month == 12 else d.month + 1, 1)
    print(f"Период: {d_from} — {d_to}, кабинетов: {len(accounts)}")

    tables, store_ops, store_rows, store_reports, store_report_net, failed = {}, {}, {}, {}, {}, []
    checks, store_sales, store_by_article, store_catalog, store_goods = {}, {}, {}, {}, {}
    store_tax_base, store_tax_base_by_article = {}, {}
    cache_dir = here / "cache"

    def load(acc):
        try:
            result = load_account(acc, d_from, d_to, cache_dir, args.from_cache, args.reload)
        except RuntimeError as e:
            return None, e
        name = acc.get("name") or "Магазин"
        goods = load_goods(acc, d_from, cache_dir, args.from_cache,
                           lambda text: print(f"[{name}] {text}", flush=True)) if args.dump else {}
        return (*result, goods), None

    # Лимит WB — 1 запрос в минуту на продавца, поэтому кабинеты разных продавцов грузим одновременно
    with ThreadPoolExecutor(max_workers=max(1, len(accounts))) as pool:
        loaded = list(pool.map(load, accounts))
    for acc, (result, error) in zip(accounts, loaded):
        name = acc.get("name") or "Магазин"
        print(f"\n[{name}]")
        if error:
            print(f"  ОШИБКА: {error}")
            failed.append(name)
            continue
        rows, reports, daily, store_goods[name] = result
        table, ops, report_net, sales, skipped, by_article, catalog, tax_base, tax_by_article = \
            aggregate(rows, set(month_keys))
        if skipped:
            print(f"  строк с датой операции вне периода (не учтены): {skipped}")
        checks[name] = []
        if reports:
            wb_total, ours, bad = reconciliation_summary(reports, report_net)
            print(f"  сверка с WB: «Итого к оплате» по отчётам WB {wb_total:,.2f}, по строкам {ours:,.2f}, "
                  f"расхождение {ours - wb_total:,.2f}; отчётов с разницей ≥ 1 ₽: {len(bad)} из {len(reports)}")
            checks[name] = [["Итого к оплате по отчётам WB, ₽", round(wb_total, 2)],
                            ["Итого к оплате по строкам отчёта, ₽", round(ours, 2)],
                            ["Расхождение со сверкой WB, ₽", round(ours - wb_total, 2)],
                            ["Отчётов с разницей ≥ 1 ₽", f"{len(bad)} из {len(reports)}"]]
        if skipped:
            checks[name].append(["Строк с датой вне периода (не учтены)", skipped])
        if daily:
            print(f"  дни {daily[0]} — {daily[1]} — из ежедневных отчётов WB (неделя ещё не закрыта)")
            checks[name].append(["Из ежедневных отчётов WB (неделя ещё не закрыта)", f"{daily[0]} — {daily[1]}"])
        tables[name], store_ops[name], store_rows[name] = table, ops, rows
        store_sales[name] = sales
        store_by_article[name] = by_article
        store_catalog[name] = catalog
        store_tax_base[name] = tax_base
        store_tax_base_by_article[name] = tax_by_article
        store_reports[name], store_report_net[name] = reports, report_net

    if not tables:
        sys.exit("\nНе удалось получить данные ни по одному кабинету.")

    # Не показываем пустые месяцы в начале периода (магазин ещё не торговал)
    active = [m for m in month_keys if any(abs(v.get(m, 0.0)) >= 0.005 for t in tables.values() for v in t.values())]
    if active:
        month_keys = month_keys[month_keys.index(active[0]):]

    if args.dump:
        write_dump(args.dump, "Wildberries", month_keys, tables, S_INFO,
                   lambda t: dict(pnl_totals(t, month_keys)[-1]), checks, store_sales, store_by_article,
                   store_catalog, store_goods, store_tax_base, store_tax_base_by_article)
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
    ws.title = safe_name("Итого", used)
    write_pnl(ws, combined, month_keys, f"ОПиУ Wildberries — все магазины ({len(tables)})")
    if len(tables) > 1:
        write_by_store(wb.create_sheet(safe_name("По магазинам", used)), tables, month_keys)
    for name, table in tables.items():
        write_pnl(wb.create_sheet(safe_name(name, used)), table, month_keys, f"ОПиУ Wildberries — {name}")
    write_ops(wb.create_sheet(safe_name("Операции", used)), store_ops)
    if any(store_reports.values()):
        write_reconciliation(wb.create_sheet(safe_name("Сверка с WB", used)), store_reports, store_report_net)

    stem = f"wb_finance_{month_keys[0]}_{month_keys[-1]}"
    out = Path(args.out) if args.out else here / f"{stem}.xlsx"
    wb.save(out)
    print(f"\nГотово: {out}")
    if not args.no_raw:
        csv_path = out.with_suffix(".csv")
        write_csv(csv_path, store_rows)
        print(f"Все строки отчёта: {csv_path}")
    if failed:
        print("Не загружены кабинеты: " + ", ".join(failed))


if __name__ == "__main__":
    main()
