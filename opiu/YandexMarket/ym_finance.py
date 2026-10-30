"""
Финансовый отчёт Яндекс Маркета по месяцам для нескольких кабинетов.

Источники данных (Partner API, отчёты генерируются асинхронно):
  POST /v2/reports/goods-realization/generate          — отчёт по реализации (выручка), магазин × месяц
  POST /v2/reports/united-marketplace-services/generate — отчёт по стоимости услуг (расходы), до 3 месяцев
  GET  /v2/reports/info/{reportId}                      — статус и ссылка на ZIP с JSON по листам
  GET  /v2/campaigns                                    — магазины кабинета
Авторизация: заголовок Api-Key (доступ «Финансы и отчётность» / finance-and-accounting).
Лимит генерации: 1 отчёт в 2 минуты на метод (на тарифе «Медиум» — в минуту).

Результат: один Excel-файл
  - «Итого»        — сводный ОПиУ по всем кабинетам, месяцы в колонках
  - «По кабинетам» — ключевые показатели каждого кабинета
  - лист на каждый кабинет — такой же ОПиУ
  - «Услуги»       — суммы по листам отчёта об услугах и названиям услуг за месяц
  - «Реализация»   — суммы и штуки по листам отчёта о реализации за месяц

Запуск:
  python ym_finance.py                          # период из config / последние 12 мес.
  python ym_finance.py --from 2026-01 --to 2026-08
  python ym_finance.py --from-cache             # собрать Excel из уже скачанных отчётов
  python ym_finance.py --probe                  # скачать 2 небольших отчёта и сохранить образцы строк

Нужен пакет openpyxl:  pip install openpyxl
"""

import argparse
import io
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict
from datetime import date, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

API = "https://api.partner.market.yandex.ru"
GENERATE_INTERVAL = 121          # сек между генерациями одного типа отчёта (без подписки — 1 в 2 мин)
POLL_TIMEOUT = 45 * 60           # сколько максимум ждать готовности отчёта
SERVICES_MAX_MONTHS = 3          # максимальный период отчёта по услугам
ORDERS_MAX_DAYS = 90             # отчёт по заказам без подписки — не старше 90 дней
FUNNEL_MAX_DAYS = 90             # отчёт «Аналитика продаж» (воронка) без подписки — тоже 90 дней
CLOSED_AFTER_DAYS = 35           # период считается закрытым (кэшируется), если закончился раньше

# ---------------------------------------------------------------------------
# Группы статей ОПиУ
# ---------------------------------------------------------------------------
S_REVENUE = "Выручка"
S_COMMISSION = "Вознаграждение Маркета"
G_LOGISTICS = "Логистика и обработка заказов"
G_PAYMENTS = "Приём и перевод платежей"
G_ADS = "Продвижение и лояльность"
G_STORAGE = "Хранение и склад"
G_OTHER = "Подписки и прочие услуги"
G_COMPENSATION = "Компенсации"
S_INFO = "Справочно"  # в итоги не входит

EXPENSE_GROUPS = [G_LOGISTICS, G_PAYMENTS, G_ADS, G_STORAGE, G_OTHER]

# Лист отчёта по стоимости услуг (имя файла в архиве) -> (название, группа)
SERVICE_SHEETS = {
    "placement": ("Размещение товаров", S_COMMISSION),
    "sale_commission": ("Поручение на продажу", S_COMMISSION),
    "expropriation": ("Вознаграждение за продажу", S_COMMISSION),
    "item_booking": ("Бронирование товара", S_COMMISSION),
    "delivery": ("Доставка покупателю", G_LOGISTICS),
    "crossregional_delivery": ("Доставка (средняя миля)", G_LOGISTICS),
    "express_delivery": ("Экспресс-доставка покупателю", G_LOGISTICS),
    "delivery_from_abroad": ("Доставка из-за рубежа", G_LOGISTICS),
    "intake_logistics": ("Организация забора заказов", G_LOGISTICS),
    "export_from_warehouse": ("Вывоз со склада, СЦ, ПВЗ", G_LOGISTICS),
    "delivery_via_transit_warehouse": ("Поставка через транзитный склад", G_LOGISTICS),
    "order_processing": ("Обработка заказов в СЦ или ПВЗ", G_LOGISTICS),
    "order_processing_on_warehouse": ("Обработка заказов на складе", G_LOGISTICS),
    "warehouse_processing": ("Складская обработка", G_LOGISTICS),
    "payment_accepting": ("Приём платежа", G_PAYMENTS),
    "payment_transfer": ("Перевод платежа", G_PAYMENTS),
    "money_withdraw": ("Поручение на перевод платежа", G_PAYMENTS),
    "installment_plan": ("Рассрочка", G_PAYMENTS),
    "paid_storage_before_31-05-22": ("Платное хранение", G_STORAGE),
    "paid_storage_after_01-06-22": ("Платное хранение", G_STORAGE),
    "paid_storage_after_01-09-26": ("Платное хранение", G_STORAGE),
    "goods_acceptance": ("Приёмка поставки", G_STORAGE),
    "reception_of_surplus": ("Приём излишков на складе", G_STORAGE),
    "storage_of_returns": ("Хранение невыкупов и возвратов", G_STORAGE),
    "product_marking": ("Нанесение знака маркировки", G_STORAGE),
    "utilization": ("Организация утилизации", G_STORAGE),
    "boost": ("Буст продаж, оплата за продажи", G_ADS),
    "cpm-boost": ("Буст продаж, оплата за показы", G_ADS),
    "shelf": ("Полки", G_ADS),
    "product-banners": ("Товарные баннеры", G_ADS),
    "banners": ("Баннеры", G_ADS),
    "pushes": ("Пуш-уведомления", G_ADS),
    "popups": ("Поп-ап уведомления", G_ADS),
    "mailing": ("Рассылки", G_ADS),
    "web": ("Реклама на внешних площадках", G_ADS),
    "tv": ("ТВ реклама", G_ADS),
    "yandex-market": ("Реклама на Маркете", G_ADS),
    "pads": ("Видеобаннеры в такси", G_ADS),
    "loyalty_and_reviews": ("Программа лояльности и отзывы", G_ADS),
    "business_subscription": ("Подписки", G_OTHER),
    "personal_manager": ("Персональный менеджер", G_OTHER),
    "extended_service_access": ("Расширенный доступ к сервисам", G_OTHER),
}
SERVICE_AMOUNT_COLS = ("totalAmount", "servicePrice", "paidStorage", "fullPrice")
SERVICE_DATE_COLS = ("serviceDateTime", "serviceDate", "dateTime", "actDate")

REALIZATION_SHEETS = ("transferred_to_delivery", "delivered", "unredeemed", "returned", "lost_items")


def num(value):
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(" ", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return 0.0


def month_of(value):
    """'2026-08-15T10:00:00+03:00' / '2026-08-15' / '15.08.2026' -> '2026-08'."""
    s = str(value or "")
    m = re.match(r"(\d{4})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", s)
    if m:
        return f"{m.group(3)}-{m.group(2)}"
    return ""


def iso_day(value):
    """'2026-08-15 10:00:00' / '15.08.2026' -> '2026-08-15'."""
    s = str(value or "")
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", s)
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else ""


# Разрез выгрузки: month — ключ «ГГГГ-ММ», day — «ГГГГ-ММ-ДД» (нужен своду для отчётов по неделям и дням)
GRAIN = "month"


def period_key(value):
    """Ключ периода из даты строки отчёта: месяц или день — в зависимости от --grain."""
    return iso_day(value) if GRAIN == "day" else month_of(value)


def clean_label(text):
    text = re.sub(r"№\s*\d+", "", str(text or ""))
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip(" ,.-")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class YMClient:
    def __init__(self, api_key, interval=GENERATE_INTERVAL):
        self.api_key = api_key.strip()
        self.interval = interval
        self.last_generate = {}

    def _request(self, method, path, body=None, params=None, retries=6):
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        for attempt in range(1, retries + 1):
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Api-Key": self.api_key, "Content-Type": "application/json", "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    return json.loads(resp.read() or b"{}")
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")
                if e.code in (420, 429) and attempt < retries:
                    wait = 60
                    until = e.headers.get("X-RateLimit-Resource-Until")
                    if until:
                        try:
                            wait = max(parsedate_to_datetime(until).timestamp() - time.time() + 3, 5)
                        except (TypeError, ValueError):
                            pass
                    print(f"    лимит запросов Маркета ({e.code}), ждём {int(wait)} с...")
                    time.sleep(wait)
                    continue
                if e.code >= 500 and attempt < retries:
                    time.sleep(10)
                    continue
                hint = {401: " — неверный Api-Key", 403: " — у ключа нет доступа «Финансы и отчётность»"}.get(e.code, "")
                raise RuntimeError(f"{method} {path}: HTTP {e.code}{hint}: {text[:500]}") from None
            except (urllib.error.URLError, OSError) as e:
                if attempt < retries:
                    time.sleep(10)
                    continue
                raise RuntimeError(f"Нет соединения с API Маркета: {e}") from None

    def campaigns(self):
        # Токенная пагинация: page/pageSize помечены Маркетом устаревшими (04.03.2026)
        out, token = [], None
        for _ in range(100):
            params = {"limit": 50, **({"pageToken": token} if token else {})}
            res = self._request("GET", "/v2/campaigns", params=params)
            out.extend(res.get("campaigns") or [])
            token = (res.get("paging") or {}).get("nextPageToken")
            if not token:
                break
        return out

    def report(self, path, body, label):
        """Генерирует отчёт, ждёт готовности и возвращает {лист: [строки]} из ZIP с JSON."""
        wait = self.interval - (time.monotonic() - self.last_generate.get(path, -1e9))
        if wait > 0:
            print(f"    пауза {int(wait)} с (лимит Маркета на генерацию отчётов)...")
            time.sleep(wait)
        self.last_generate[path] = time.monotonic()
        res = self._request("POST", path, body=body, params={"format": "JSON", "language": "RU"})
        report_id = (res.get("result") or {}).get("reportId")
        if not report_id:
            raise RuntimeError(f"{label}: Маркет не вернул reportId: {res}")
        started = time.monotonic()
        while True:
            info = self._request("GET", f"/v2/reports/info/{report_id}").get("result") or {}
            status, sub = info.get("status"), info.get("subStatus")
            if status == "DONE":
                if sub == "NO_DATA" or not info.get("file"):
                    return {}
                for attempt in range(1, 6):
                    try:
                        return download_zip_json(info["file"])
                    except (urllib.error.URLError, OSError, zipfile.BadZipFile, json.JSONDecodeError) as e:
                        if attempt == 5:
                            raise RuntimeError(f"{label}: не удалось скачать файл отчёта: {e}") from None
                        print(f"    обрыв при скачивании отчёта ({e}), повтор...")
                        time.sleep(10 * attempt)
                        # ссылка одноразовая по времени — берём свежую
                        info = self._request("GET", f"/v2/reports/info/{report_id}").get("result") or info
            if status == "FAILED":
                if sub == "NO_DATA":
                    return {}
                raise RuntimeError(f"{label}: генерация не удалась (subStatus={sub})")
            if time.monotonic() - started > POLL_TIMEOUT:
                raise RuntimeError(f"{label}: отчёт не сформирован за {POLL_TIMEOUT // 60} мин")
            est = num(info.get("estimatedGenerationTime")) / 1000
            time.sleep(min(max(est / 4, 5), 30))


def download_zip_json(url):
    with urllib.request.urlopen(url, timeout=600) as resp:
        payload = resp.read()
    sheets = {}
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile:
        return {"report": rows_from_json(json.loads(payload))}
    for name in archive.namelist():
        if not name.lower().endswith(".json"):
            continue
        stem = Path(name).stem
        sheets[stem] = rows_from_json(json.loads(archive.read(name).decode("utf-8-sig")))
    return sheets


def rows_from_json(data):
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list) and (not v or isinstance(v[0], dict)):
                return v
        return [data]
    return []


def sheet_key(stem, known):
    """Имя файла в архиве может содержать префиксы — ищем самое длинное известное имя листа."""
    s = stem.lower()
    matches = [k for k in known if s == k or s.endswith(k) or k in s]
    return max(matches, key=len) if matches else stem


# ---------------------------------------------------------------------------
# Загрузка с кэшем
# ---------------------------------------------------------------------------
def month_add(d, n):
    y, m = d.year + (d.month - 1 + n) // 12, (d.month - 1 + n) % 12 + 1
    return date(y, m, 1)


def month_end(d):
    return month_add(d, 1) - timedelta(days=1)


def cached(cache_file, closed, refresh, from_cache, loader, fallback_pattern=None):
    """Кэш отчёта. В режиме --from-cache подходит и файл за близкий период: конец периода
    сдвигается каждый день (сегодняшняя дата), а данные в нём те же."""
    if cache_file.exists() and (from_cache or (closed and not refresh)):
        return json.loads(cache_file.read_text(encoding="utf-8"))
    if from_cache:
        if fallback_pattern:
            candidates = sorted(cache_file.parent.glob(fallback_pattern))
            if candidates:
                print(f"    беру из кэша {candidates[-1].name}")
                return json.loads(candidates[-1].read_text(encoding="utf-8"))
        return None
    data = loader()
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def load_account(acc, client, d_from, d_to, cache_dir, args):
    today = date.today()
    business_id = acc.get("business_id")
    campaign_ids = [int(c) for c in acc.get("campaign_ids") or []]
    campaigns_file = cache_dir / "campaigns.json"
    if args.from_cache and campaigns_file.exists():
        campaigns = json.loads(campaigns_file.read_text(encoding="utf-8"))
    else:
        campaigns = client.campaigns()
        cache_dir.mkdir(parents=True, exist_ok=True)
        campaigns_file.write_text(json.dumps(campaigns, ensure_ascii=False), encoding="utf-8")
    ids = sorted({(c.get("business") or {}).get("id") for c in campaigns if (c.get("business") or {}).get("id")})
    if business_id and int(business_id) not in ids:
        # В кабинетах WB OPS «ID кабинета» может быть ID магазина (кампании), а не бизнеса
        shop = next((c for c in campaigns if c.get("id") == int(business_id)), None)
        if shop and (shop.get("business") or {}).get("id"):
            campaign_ids = campaign_ids or [shop["id"]]
            business_id = shop["business"]["id"]
    if not business_id:
        if len(ids) != 1:
            raise RuntimeError(f"укажите business_id в конфиге — ключ видит кабинеты: {ids}")
        business_id = ids[0]
    shops = [c for c in campaigns if (c.get("business") or {}).get("id") == int(business_id)
             and (not campaign_ids or c.get("id") in campaign_ids)]
    print(f"  кабинет {business_id}, магазинов: {len(shops)} — " +
          ", ".join(f"{c.get('domain')} ({c.get('id')}, {c.get('placementType')})" for c in shops))

    # Задания на отчёты. У каждого типа отчёта свой лимит на кабинет, поэтому чередуем типы — меньше пауз.
    svc_jobs, real_jobs, pay_jobs = [], [], []
    start = date(d_from.year, d_from.month, 1)
    while start <= d_to:
        end = min(month_end(month_add(start, SERVICES_MAX_MONTHS - 1)), d_to)
        svc_jobs.append(("services", None, start, end))
        pay_jobs.append(("payments", None, start, end))
        start = month_add(start, SERVICES_MAX_MONTHS)
    m = date(d_from.year, d_from.month, 1)
    while m <= d_to:
        for shop in shops:
            real_jobs.append(("realization", shop, m, month_end(m)))
        m = month_add(m, 1)
    jobs = []
    for i in range(max(len(svc_jobs), len(real_jobs))):
        jobs += svc_jobs[i:i + 1] + pay_jobs[i:i + 1] + real_jobs[i:i + 1]

    services, realization, payments = [], [], []  # [sheets], [(shop, month, sheets)], [(start, end, rows)]
    for n, (kind, shop, start, end) in enumerate(jobs, 1):
        closed = end <= today - timedelta(days=CLOSED_AFTER_DAYS)
        try:
            if kind == "services":
                f = cache_dir / f"services_{start}_{end}.json"
                print(f"  [{n}/{len(jobs)}] услуги {start} — {end}...")
                data = cached(f, closed, args.refresh, args.from_cache, lambda: client.report(
                    "/v2/reports/united-marketplace-services/generate",
                    {"businessId": int(business_id), "dateFrom": str(start), "dateTo": str(end)},
                    f"услуги {start}—{end}"), f"services_{start}_*.json")
            elif kind == "payments":
                f = cache_dir / f"payments_{start}_{end}.json"
                print(f"  [{n}/{len(jobs)}] платежи {start} — {end}...")
                data = cached(f, closed, args.refresh, args.from_cache, lambda: client.report(
                    "/v2/reports/united-netting/generate",
                    {"businessId": int(business_id), "dateFrom": str(start), "dateTo": str(end)},
                    f"платежи {start}—{end}"), f"payments_{start}_*.json")
            else:
                key = start.strftime("%Y-%m")
                f = cache_dir / f"realization_{shop['id']}_{key}.json"
                print(f"  [{n}/{len(jobs)}] реализация {shop.get('domain')} {key}...")
                data = cached(f, closed, args.refresh, args.from_cache, lambda: client.report(
                    "/v2/reports/goods-realization/generate",
                    {"campaignId": shop["id"], "year": start.year, "month": start.month}, f"реализация {shop['id']} {key}"))
        except RuntimeError as e:
            if "HTTP 401" in str(e) or "HTTP 403" in str(e):
                raise
            # Report generation is asynchronous and Market can occasionally
            # return FAILED with an empty subStatus.  If this month was
            # downloaded earlier, keep the last good report instead of
            # dropping the whole month from the P&L.
            fallback = f if 'f' in locals() and f.exists() else None
            if fallback and fallback.stat().st_size > 2:
                try:
                    cached_data = json.loads(fallback.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    cached_data = None
                if cached_data:
                    print(f"    ошибка API: {e}; беру сохранённый отчёт {fallback.name}")
                    data = cached_data
                    if kind == "services":
                        services.append(data)
                    elif kind == "payments":
                        payments.append((start, end, [r for rows in data.values() for r in rows]))
                    else:
                        realization.append((shop, key, data))
                    continue
            print(f"    ОШИБКА, отчёт пропущен (запустите скрипт ещё раз, скачается только недостающее): {e}")
            continue
        if data is None:
            print("    нет в кэше — пропущено")
        elif kind == "services":
            services.append(data)
        elif kind == "payments":
            payments.append((start, end, [r for rows in data.values() for r in rows]))
        else:
            realization.append((shop, key, data))

    # Отчёт по заказам (без подписки — только последние 90 дней): выручка за месяцы,
    # по которым Маркет ещё не сформировал отчёт по реализации (обычно текущий месяц).
    orders = []
    o_from, o_to = max(d_from, today - timedelta(days=ORDERS_MAX_DAYS - 1)), min(d_to, today)
    if o_from <= o_to:
        f = cache_dir / "orders_last90.json"
        print(f"  заказы {o_from} — {o_to} (для месяцев без отчёта по реализации)...")
        try:
            data = cached(f, False, args.refresh, args.from_cache, lambda: client.report(
                "/v2/reports/united-orders/generate",
                {"businessId": int(business_id), "dateFrom": str(o_from), "dateTo": str(o_to)}, "заказы"))
            for stem, rows in (data or {}).items():
                if sheet_key(stem, ("orders_and_offers_transactions",)) == "orders_and_offers_transactions":
                    orders.extend(rows)
        except RuntimeError as e:
            print(f"    отчёт по заказам не получен, текущий месяц останется без выручки: {e}")
    funnel = []
    if args.dump:
        f_from = max(d_from, today - timedelta(days=FUNNEL_MAX_DAYS - 1))
        f = cache_dir / "funnel_last90.json"
        print(f"  воронка продаж {f_from} — {o_to}...")
        try:
            data = cached(f, False, args.refresh, args.from_cache, lambda: client.report(
                "/v2/reports/shows-sales/generate",
                {"businessId": int(business_id), "dateFrom": str(f_from), "dateTo": str(o_to), "grouping": "OFFERS"},
                "воронка продаж"))
            funnel = [r for rows in (data or {}).values() for r in rows]
        except RuntimeError as e:
            print(f"    ОШИБКА: воронка продаж не получена (в юнит-экономике — данные прошлой загрузки): {e}")
        else:
            if data is not None:   # отметка периода — только если отчёт действительно есть
                funnel = [dict(r, _from=str(f_from)) for r in funnel] or [{"_from": str(f_from)}]
    goods = load_goods(client, shops, orders, o_from, cache_dir, args.from_cache, funnel) if args.dump else {}
    return services, realization, orders, payments, goods


# ---------------------------------------------------------------------------
# Заказы и остатки (юнит-экономика, в ОПиУ не входят)
# ---------------------------------------------------------------------------
def fetch_stocks(client, shops):
    """Годные остатки (FIT) по магазинам: [[артикул, склад, вид, шт]]. Вид: mp — склад Маркета (FBY),
    own — свой склад (FBS/DBS/Express)."""
    rows = []
    for shop in shops:
        fby = shop.get("placementType") == "FBY"
        token = None
        for _ in range(2000):
            params = {"limit": 100, **({"page_token": token} if token else {})}
            res = client._request("POST", f"/v2/campaigns/{shop['id']}/offers/stocks", body={}, params=params)
            result = res.get("result") or {}
            for w in result.get("warehouses") or []:
                place = f"Склад Маркета {w.get('warehouseId')}" if fby else (shop.get("domain") or str(shop["id"]))
                for offer in w.get("offers") or []:
                    qty = sum(num(st.get("count")) for st in offer.get("stocks") or [] if st.get("type") == "FIT")
                    if qty:
                        rows.append([str(offer.get("offerId") or "").strip(), place, "mp" if fby else "own", qty])
            token = (result.get("paging") or {}).get("nextPageToken")
            if not token:
                break
    return rows


def fetch_prices(client, shops):
    """Текущие цены: [[артикул, цена до скидки, цена, None]]. Базовые цены кабинета (offer-prices бизнеса);
    если их нет — цены в магазинах (у магазина могут быть свои цены)."""
    def offers(path):
        rows, token = [], None
        for _ in range(2000):
            params = {"limit": 200, **({"page_token": token} if token else {})}
            result = client._request("POST", path, body={}, params=params).get("result") or {}
            for offer in result.get("offers") or []:
                price = offer.get("price") or {}
                if num(price.get("value")):
                    rows.append([str(offer.get("offerId") or "").strip(), num(price.get("discountBase")) or None,
                                 num(price["value"]), None])
            token = (result.get("paging") or {}).get("nextPageToken")
            if not token:
                break
        return rows
    business = next(((s.get("business") or {}).get("id") for s in shops if (s.get("business") or {}).get("id")), None)
    rows = offers(f"/v2/businesses/{business}/offer-prices") if business else []
    if not rows:
        for shop in shops:
            rows += offers(f"/v2/campaigns/{shop['id']}/offer-prices")
    return rows


# Кратность продажи из характеристик карточки (id характеристик у Маркета свои в каждой категории — ищем по названию)
MULTIPLE_PARAMS = ["Количество упаковок в товаре", "Количество в упаковке", "Количество штук в упаковке",
                   "Количество предметов в упаковке"]


def fetch_multiples(client, shops, known=None):
    """Кратность продажи по артикулам: ([[артикул, шт, характеристика]], {категория: {id: название}}).
    known — названия характеристик категорий из прошлой загрузки (чтобы не запрашивать их заново)."""
    business = next(((s.get("business") or {}).get("id") for s in shops if (s.get("business") or {}).get("id")), None)
    if not business:
        return [], known or {}
    cards, token = [], None
    for _ in range(2000):
        params = {"limit": 100, **({"page_token": token} if token else {})}
        result = client._request("POST", f"/v2/businesses/{business}/offer-cards", body={},
                                 params=params).get("result") or {}
        cards += result.get("offerCards") or []
        token = (result.get("paging") or {}).get("nextPageToken")
        if not token:
            break
    params_by_cat = {str(k): v for k, v in (known or {}).items()}
    for cat in {str((c.get("mapping") or {}).get("marketCategoryId") or "") for c in cards} - {""} - set(params_by_cat):
        res = client._request("POST", f"/v2/category/{cat}/parameters", body={}).get("result") or {}
        params_by_cat[cat] = {str(p["id"]): p.get("name") for p in res.get("parameters") or []
                              if p.get("name") in MULTIPLE_PARAMS}
    rows = []
    for card in cards:
        wanted = params_by_cat.get(str((card.get("mapping") or {}).get("marketCategoryId") or ""), {})
        values = {wanted[str(p.get("parameterId"))]: p.get("value") for p in card.get("parameterValues") or []
                  if str(p.get("parameterId")) in wanted}
        for title in MULTIPLE_PARAMS:
            qty = num(values.get(title))
            if qty > 0:
                rows.append([str(card.get("offerId") or "").strip(), qty, title])
                break
    return rows, params_by_cat


def load_goods(client, shops, orders, o_from, cache_dir, from_cache, funnel=None):
    """Заказы копятся в orders_history.json (отчёт Маркета без подписки — только за 90 дней);
    остатки — на момент загрузки (stocks.json). Ошибки не прерывают отчёт."""
    f = cache_dir / "orders_history.json"
    hist = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"orders": {}}
    seen = defaultdict(int)
    for r in orders:
        if not r.get("orderId"):
            continue
        sku = str(r.get("shopSku") or "").strip()
        seen[(r["orderId"], sku)] += 1
        qty = num(r.get("deliveredOrReturned")) or num(r.get("transferredForDelivery")) or 1
        cancelled = str(r.get("offerStatus") or "").startswith(("Отмен", "Удал"))
        hist["orders"][f"{r['orderId']}|{sku}|{seen[(r['orderId'], sku)]}"] = [
            iso_day(r.get("creationDate")), sku, qty, round(num(r.get("billingPrice")) * qty, 2), cancelled]
    if orders:
        hist["orders_from"] = min(filter(None, [hist.get("orders_from"), str(o_from)]))
        cache_dir.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(hist, ensure_ascii=False), encoding="utf-8")
    # Воронка по дням копится в funnel_history.json: {"день|артикул": [показы, клики, корзина, заказано шт,
    # заказано ₽, доставлено, отменено, возвращено]}
    ff = cache_dir / "funnel_history.json"
    fhist = json.loads(ff.read_text(encoding="utf-8")) if ff.exists() else {"days": {}}
    rows = [r for r in funnel or [] if r.get("offerId")]
    for r in rows:
        day = re.sub(r"^(\d{2})-(\d{2})-(\d{4})$", r"\3-\2-\1", str(r.get("day") or ""))   # 01-09-2026
        fhist["days"][f"{iso_day(day)}|{str(r['offerId']).strip()}"] = [num(r.get(c)) for c in (
            "shows", "clicks", "toCart", "orderItems", "orderItemsTotalAmount", "orderItemsDeliveredCount",
            "orderItemsCanceledCount", "orderItemsReturnedCount")]
    if funnel:
        fhist["funnel_from"] = min(filter(None, [fhist.get("funnel_from"), funnel[0].get("_from")]))
        cache_dir.mkdir(parents=True, exist_ok=True)
        ff.write_text(json.dumps(fhist, ensure_ascii=False), encoding="utf-8")
    if fhist["days"]:
        hist["funnel"], hist["funnel_from"] = fhist["days"], fhist.get("funnel_from")
    stock_file = cache_dir / "stocks.json"
    if not from_cache:
        print("  остатки...")
        try:
            stocks = fetch_stocks(client, shops)
            cache_dir.mkdir(parents=True, exist_ok=True)
            stock_file.write_text(json.dumps({"stocks": stocks, "stocks_at": time.strftime("%Y-%m-%d %H:%M")},
                                             ensure_ascii=False), encoding="utf-8")
            print("    всего " + f"{sum(r[3] for r in stocks):,.0f}".replace(",", " ") + " шт")
        except RuntimeError as e:
            print(f"    ОШИБКА: остатки не получены (нужен доступ ключа к товарам/остаткам; "
                  f"берутся из прошлой загрузки, если есть): {e}")
    if stock_file.exists():
        hist.update(json.loads(stock_file.read_text(encoding="utf-8")))
    price_file = cache_dir / "prices.json"
    if not from_cache:
        print("  цены...")
        try:
            prices = fetch_prices(client, shops)
            cache_dir.mkdir(parents=True, exist_ok=True)
            price_file.write_text(json.dumps({"prices": prices, "prices_at": time.strftime("%Y-%m-%d %H:%M")},
                                             ensure_ascii=False), encoding="utf-8")
            print(f"    товаров с ценой: {len(prices)}")
        except RuntimeError as e:
            print(f"    ОШИБКА: цены не получены (нужен доступ ключа к ценам/товарам; "
                  f"берутся из прошлой загрузки, если есть): {e}")
    if price_file.exists():
        hist.update(json.loads(price_file.read_text(encoding="utf-8")))
    mult_file = cache_dir / "multiples.json"
    mult = json.loads(mult_file.read_text(encoding="utf-8")) if mult_file.exists() else {}
    if not from_cache:
        print("  кратность продажи (карточки товаров)...")
        try:
            rows, params = fetch_multiples(client, shops, mult.get("params"))
            mult = {"multiples": rows, "multiples_at": time.strftime("%Y-%m-%d %H:%M"), "params": params}
            cache_dir.mkdir(parents=True, exist_ok=True)
            mult_file.write_text(json.dumps(mult, ensure_ascii=False), encoding="utf-8")
            print(f"    товаров с кратностью: {len(rows)}")
        except RuntimeError as e:
            print(f"    ОШИБКА: кратность не получена (берётся из прошлой загрузки, если есть): {e}")
    hist.update({k: mult.get(k) for k in ("multiples", "multiples_at") if k in mult})
    return hist


def goods_dump(goods):
    """Поля выгрузки для оркестратора: заказы по периодам и артикулам, остатки на дату."""
    ordered = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for day, article, qty, amount, cancelled in (goods.get("orders") or {}).values():
        if not day:
            continue
        row = ordered[(period_key(day), article)]
        row[0] += qty
        row[1] += amount
        if cancelled:
            row[2] += qty
            row[3] += amount
    return {"ordered": [[mon, art, q, round(s, 2), cq, round(cs, 2)] for (mon, art), (q, s, cq, cs) in ordered.items()],
            "ordered_from": goods.get("orders_from"), "stocks": goods.get("stocks"),
            "stocks_at": goods.get("stocks_at"),
            # Текущие цены [[артикул, цена до скидки, цена продавца, цена с акциями]]
            "prices": goods.get("prices"), "prices_at": goods.get("prices_at"),
            # Кратность продажи из карточек МП [[артикул, шт, характеристика]]
            "multiples": goods.get("multiples"), "multiples_at": goods.get("multiples_at"),
            # Воронка [[месяц, артикул, показы, переходы, корзина, заказано шт, ₽, выкуплено, отменено, возвращено]]
            "funnel": funnel_rows(goods.get("funnel")), "funnel_from": goods.get("funnel_from")}


def funnel_rows(days):
    """Воронка по дням -> по месяцам и артикулам."""
    if days is None:
        return None
    out = defaultdict(lambda: [0.0] * 8)
    for key, values in days.items():
        day, article = key.split("|", 1)
        if day:
            row = out[(month_of(day), article)]
            for i, v in enumerate(values):
                row[i] += v
    return [[mon, art] + [round(v, 2) for v in vals] for (mon, art), vals in out.items()]


# ---------------------------------------------------------------------------
# Разбор в статьи ОПиУ
# ---------------------------------------------------------------------------
def first_value(row, cols):
    for c in cols:
        if row.get(c) not in (None, ""):
            return row.get(c)
    return None


def parse_ru_date(value):
    """'05.01.2026 18:00' / '2026-01-05' -> date или None."""
    s = str(value or "")
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", s)
    if m:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


P_BUYER = "Платежи покупателей"
P_REFUND = "Возвраты платежей покупателям"
P_SERVICES = "Удержано за услуги Маркета"
P_OTHER = "Прочие начисления и удержания"
P_ACCRUED = "Начислено к выплате деньгами"
P_PAID = "Перечислено на счёт (платёжные поручения)"
P_DIFF = "Разница за месяц (начислено − перечислено)"
P_DEBT = "Не перечислено на конец месяца (накопительно)"
P_PNL = "Для сравнения: результат деньгами из ОПиУ (по дате доставки и оказания услуг)"
P_POINTS_IN = "Баллы: пополнение баланса"
P_POINTS_OUT = "Баллы: списание с баланса"


def aggregate_payments(payments, month_set):
    """Сверка с выплатами по «Отчёту по платежам» (united-netting).
    Начисления — по дате транзакции внутри периода отчёта (периоды отчётов пересекаются по выплатам).
    Перечисления — по уникальным платёжным поручениям, по дате поручения."""
    table = defaultdict(lambda: defaultdict(float))
    other = defaultdict(lambda: defaultdict(float))  # премии, компенсации и т.п. — для ОПиУ
    bank_orders = {}
    pending = defaultdict(float)
    for start, end, rows in payments:
        for r in rows:
            if r.get("bankOrderId"):
                bank_orders[r["bankOrderId"]] = (parse_ru_date(r.get("bankOrderDate")), num(r.get("bankSum")))
            d = parse_ru_date(r.get("transactionDate"))
            if not d or not (start <= d <= end):
                continue
            mon = d.strftime("%Y-%m")
            if mon not in month_set:
                continue
            src = str(r.get("transactionSource") or "")
            status = str(r.get("paymentStatus") or "")
            value = num(r.get("transactionSum"))
            if status.startswith("Справочно"):
                if "не будет" in status.lower():
                    continue
                table[P_POINTS_IN if value > 0 else P_POINTS_OUT][mon] += value
                continue
            if status.startswith("Не будет"):
                continue
            if src == "Платёж покупателя":
                key = P_BUYER
            elif src.startswith("Возврат платежа"):
                key = P_REFUND
            elif src == "Оплата услуг Маркета":
                key = P_SERVICES
            else:
                key = P_OTHER
                other[src or "Прочие начисления"][mon] += value
            table[key][mon] += value
            table[P_ACCRUED][mon] += value
            if status.startswith("Будет"):
                pending[status] += value
    for bid, (d, s) in bank_orders.items():
        if d and d.strftime("%Y-%m") in month_set:
            table[P_PAID][d.strftime("%Y-%m")] += s
    return table, bank_orders, pending, other


I_BUYER = "Оплачено покупателями деньгами"
I_POINTS_IN = "Баллы: начислено Маркетом в компенсацию его скидок"
I_POINTS_OUT = "Баллы: списано на оплату услуг Маркета"
I_POINTS_BAL = "Баллы: изменение остатка (начислено − списано)"
I_BONUS = "Бонусы Маркета на продвижение (подарочные, в стоимость не включены)"
I_MONEY = "Результат деньгами: оплаты покупателей − услуги деньгами + компенсации + премии"
I_PREMIUM = "Премия: выплата деньгами неизрасходованных баллов (баллы уже в выручке)"
I_ORDERS = "Выручка месяца взята из отчёта по заказам (реализация ещё не сформирована)"


def aggregate(services, realization, orders, month_set):
    """Выручка — по цене продавца (до скидок Маркета). Скидки Маркета он компенсирует баллами,
    а баллами оплачивается часть услуг (колонка netting). Поэтому услуги — полная стоимость: деньги + баллы."""
    table = defaultdict(lambda: defaultdict(float))
    svc_detail = defaultdict(lambda: [0, 0.0, 0.0])    # (месяц, лист, услуга) -> строк, деньгами, баллами
    real_detail = defaultdict(lambda: [0, 0.0, 0.0])   # (месяц, магазин, лист) -> штук, цена продавца, оплачено покупателями
    sales = defaultdict(lambda: [0.0, 0.0])            # (месяц, Ваш SKU, дата, заказ) -> штук, выручка (для себестоимости)
    by_article = defaultdict(float)                    # (месяц, Ваш SKU, группа, подпись) -> сумма (юнит-экономика)
    skipped = 0

    # SKU заказа — для услуг без shopSku (обработка заказа и т.п.): если в заказе один товар, услуга его
    order_skus = defaultdict(set)
    for _shop, _mon, data in realization:
        for rows in data.values():
            for r in rows:
                if r.get("orderId") and r.get("yourSku"):
                    order_skus[str(r["orderId"])].add(str(r["yourSku"]).strip())
    for r in orders:
        if r.get("orderId") and r.get("shopSku"):
            order_skus[str(r["orderId"])].add(str(r["shopSku"]).strip())

    def sku_of(r, *cols):
        for col in cols:
            if r.get(col):
                return str(r[col]).strip()
        skus = order_skus.get(str(r.get("orderId") or ""), ())
        return next(iter(skus)) if len(skus) == 1 else ""
    buyer = defaultdict(float)
    buyer_by_article = defaultdict(float)  # (период, артикул) -> фактически оплачено покупателями
    svc_money = defaultdict(float)
    comp = defaultdict(float)

    for data in services:
        for stem, rows in data.items():
            key = sheet_key(stem, SERVICE_SHEETS)
            title, group = SERVICE_SHEETS.get(key, (stem, G_OTHER))
            for r in rows:
                mon = period_key(first_value(r, SERVICE_DATE_COLS))
                if mon not in month_set:
                    skipped += 1
                    continue
                money = num(first_value(r, SERVICE_AMOUNT_COLS))
                points = num(r.get("netting"))
                bonus = num(r.get("bonusPaid"))
                if bonus:
                    table[(S_INFO, I_BONUS)][mon] += bonus
                if not money and not points:
                    continue
                svc = clean_label(r.get("serviceName") or r.get("service") or "")
                label = f"{title}: {svc}" if svc and svc.lower() != title.lower() else title
                table[(group, label)][mon] -= money + points
                by_article[(mon, sku_of(r, "shopSku", "offerId"), group, label)] -= money + points
                svc_money[mon] += money
                if points:
                    table[(S_INFO, I_POINTS_OUT)][mon] -= points
                d = svc_detail[(mon, title, svc)]
                d[0] += 1
                d[1] += money
                d[2] += points

    real_months, real_returned = set(), set()

    def real_key(report_month, r, *date_cols):
        """Ключ периода строки отчёта о реализации. По дням — дата самой строки, но не выходя за месяц
        отчёта: сумма по дням всегда равна месяцу отчёта, как в ОПиУ по месяцам."""
        if GRAIN != "day":
            return report_month
        day = next((iso_day(r.get(c)) for c in date_cols if r.get(c)), "")
        first = f"{report_month}-01"
        last = month_end(date.fromisoformat(first)).isoformat()
        return min(max(day, first), last) if day else first

    for shop, report_month, data in realization:
        if report_month not in {m[:7] for m in month_set}:
            continue
        mon = report_month
        shop_name = shop.get("domain") or str(shop.get("id"))
        for stem, rows in data.items():
            key = sheet_key(stem, REALIZATION_SHEETS)
            for r in rows:
                paid = 0.0
                mon = real_key(report_month, r, "deliveryDate", "transferredToDeliveryDate", "orderCreationDate")
                if key == "delivered":
                    v = num(r.get("deliveredPriceSumWithVatAndNoDiscounts"))
                    paid = num(r.get("deliveredPriceSumWithVatAndDiscounts"))
                    qty = num(r.get("deliveredCount"))
                    table[(S_REVENUE, "Доставлено покупателям (цена продавца)")][mon] += v
                    by_article[(mon, sku_of(r, "yourSku", "shopSku"), S_REVENUE,
                                "Доставлено покупателям (цена продавца)")] += v
                    table[(S_INFO, I_POINTS_IN)][mon] += v - paid
                    buyer[mon] += paid
                    buyer_by_article[(mon, sku_of(r, "yourSku", "shopSku"))] += paid
                    real_months.add(mon[:7])
                    if r.get("yourSku"):
                        sold = sales[(mon, str(r["yourSku"]).strip(), iso_day(r.get("deliveryDate")),
                                      str(r.get("orderId") or ""))]
                        sold[0] += qty
                        sold[1] += v
                elif key == "returned":
                    mon = real_key(report_month, r, "returnWarehouseOrScAcceptDate", "deliveryDate")
                    v = -num(r.get("returnPriceSumWithVatAndNoDiscounts"))
                    paid = -num(r.get("returnPriceSumWithVatAndDiscounts"))
                    qty = num(r.get("returnedCount"))
                    real_returned.add(str(r.get("orderId")))
                    if r.get("yourSku"):
                        sold = sales[(mon, str(r["yourSku"]).strip(),
                                      iso_day(r.get("returnWarehouseOrScAcceptDate") or r.get("deliveryDate")),
                                      str(r.get("orderId") or ""))]
                        sold[0] -= qty
                        sold[1] += v
                    table[(S_REVENUE, "Возвраты")][mon] += v
                    by_article[(mon, sku_of(r, "yourSku", "shopSku"), S_REVENUE, "Возвраты")] += v
                    table[(S_INFO, I_POINTS_IN)][mon] += v - paid
                    buyer[mon] += paid
                    buyer_by_article[(mon, sku_of(r, "yourSku", "shopSku"))] += paid
                elif key == "lost_items":
                    v = num(r.get("compensationAmount")) - num(r.get("decompensationAmount"))
                    qty = num(r.get("transferredToDeliveryCount"))
                    if v:
                        table[(G_COMPENSATION, "Компенсация за утраченные товары")][mon] += v
                        by_article[(mon, sku_of(r, "yourSku", "shopSku"), G_COMPENSATION,
                                    "Компенсация за утраченные товары")] += v
                        comp[mon] += v
                elif key == "unredeemed":
                    mon = real_key(report_month, r, "unredeemedWarehouseOrScAcceptDate", "deliveryDate")
                    v = num(r.get("unredeemedPriceSumWithVatAndNoDiscounts"))
                    qty = num(r.get("unredeemedCount"))
                    if v:
                        table[(S_INFO, "Невыкупленные товары, цена продавца (не выручка)")][mon] += v
                else:
                    v = num(r.get("transferredToDeliveryPriceSumWithVatAndNoDiscounts"))
                    qty = num(r.get("transferredToDeliveryCount"))
                d = real_detail[(mon, shop_name, key)]
                d[0] += qty
                d[1] += v
                d[2] += paid

    # Месяцы без отчёта по реализации — по отчёту по заказам (дата доставки / дата смены статуса)
    for r in orders:
        status = str(r.get("offerStatus") or "")
        cnt = num(r.get("deliveredOrReturned")) or num(r.get("transferredForDelivery"))
        price = num(r.get("partnerPriceForDelivery")) * cnt
        shop_name = r.get("shopName") or str(r.get("partnerId"))
        if status == "Доставлен покупателю":
            mon, kind = period_key(r.get("deliveryDate")), "delivered (заказы)"
            sign, paid = 1, num(r.get("buyerPaymentAmount"))
        elif status.startswith("Возврат"):
            mon, kind = period_key(r.get("statusChanged")), "returned (заказы)"
            sign, paid = -1, -num(r.get("refundBuyerPaymentAmount"))
            if month_of(r.get("deliveryDate")) == mon[:7] or str(r.get("orderId")) in real_returned:
                continue  # доставлен и возвращён в одном месяце, или возврат уже есть в отчёте по реализации
        else:
            continue
        if mon not in month_set or mon[:7] in real_months:
            continue
        v = sign * price
        table[(S_REVENUE, "Доставлено покупателям (цена продавца)" if sign > 0 else "Возвраты")][mon] += v
        by_article[(mon, sku_of(r, "shopSku"), S_REVENUE,
                    "Доставлено покупателям (цена продавца)" if sign > 0 else "Возвраты")] += v
        table[(S_INFO, I_POINTS_IN)][mon] += v - paid
        table[(S_INFO, I_ORDERS)][mon] += v
        buyer[mon] += paid
        buyer_by_article[(mon, sku_of(r, "shopSku"))] += paid
        if r.get("shopSku"):
            day = r.get("deliveryDate") if sign > 0 else r.get("statusChanged")
            sold = sales[(mon, str(r["shopSku"]).strip(), iso_day(day), str(r.get("orderId") or ""))]
            sold[0] += sign * cnt
            sold[1] += v
        d = real_detail[(mon, shop_name, kind)]
        d[0] += sign * cnt
        d[1] += v
        d[2] += paid

    for mon in month_set:
        pin = table[(S_INFO, I_POINTS_IN)].get(mon, 0.0) if (S_INFO, I_POINTS_IN) in table else 0.0
        pout = table[(S_INFO, I_POINTS_OUT)].get(mon, 0.0) if (S_INFO, I_POINTS_OUT) in table else 0.0
        if buyer.get(mon):
            table[(S_INFO, I_BUYER)][mon] += buyer[mon]
        if pin or pout:
            table[(S_INFO, I_POINTS_BAL)][mon] += pin + pout
        if buyer.get(mon) or svc_money.get(mon) or comp.get(mon):
            table[(S_INFO, I_MONEY)][mon] += buyer.get(mon, 0.0) - svc_money.get(mon, 0.0) + comp.get(mon, 0.0)
    return table, svc_detail, real_detail, sales, skipped, by_article, buyer, buyer_by_article


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------
NUM = '#,##0.00;[Red]-#,##0.00;"–"'
PCT = '0.0%;[Red]-0.0%;"–"'
F_HEADER = PatternFill("solid", fgColor="C8A600")
F_SECTION = PatternFill("solid", fgColor="FFF4C2")
F_TOTAL = PatternFill("solid", fgColor="FCE4D6")
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
        ws.column_dimensions["A"].width = 55
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

    w.section("ВЫРУЧКА (отчёт по реализации: цена продавца с НДС, до скидок Маркета)")
    detail(S_REVENUE)
    w.line("Выручка за вычетом возвратов", revenue, fill=F_TOTAL, font=BOLD)
    w.blank()

    w.section("УСЛУГИ МАРКЕТА (отчёт по стоимости услуг, по дате оказания; деньги + баллы)")
    for g in [S_COMMISSION] + EXPENSE_GROUPS:
        if has_group(table, g):
            w.line(g, commission if g == S_COMMISSION else groups[g], font=BOLD, indent=1)
            detail(g)
    w.line("Итого услуги Маркета", expenses, fill=F_TOTAL, font=BOLD)
    w.blank()

    if has_group(table, G_COMPENSATION):
        w.section(G_COMPENSATION.upper())
        detail(G_COMPENSATION)
        w.line("Итого компенсации", compensation, fill=F_TOTAL, font=BOLD)
        w.blank()

    w.line("ИТОГО: ВЫРУЧКА − УСЛУГИ МАРКЕТА + КОМПЕНСАЦИИ", net, fill=F_RESULT, font=Font(bold=True, size=12))
    w.blank()

    if has_group(table, S_INFO):
        w.section(S_INFO.upper() + " (в итоги не входит)")
        order = [I_ORDERS, I_BUYER, I_POINTS_IN, I_POINTS_OUT, I_POINTS_BAL, I_PREMIUM, I_MONEY, I_BONUS]
        infos = {lbl: v for (s, lbl), v in table.items() if s == S_INFO}
        for lbl in order + sorted(set(infos) - set(order)):
            if lbl in infos:
                w.line(lbl, infos[lbl], indent=2, font=BOLD if lbl in (I_MONEY, I_POINTS_BAL) else None)
        w.blank()

    w.section("ДОЛЯ ОТ ВЫРУЧКИ")
    rev_total = sum(revenue.values())
    share = lambda vals: (-sum(vals.values()) / rev_total) if rev_total else 0.0
    neg = lambda vals: {m: -v for m, v in vals.items()}
    w.line("Все услуги Маркета", ratio(neg(expenses), revenue, months), fmt=PCT, indent=1, total=share(expenses))
    for g in [S_COMMISSION] + EXPENSE_GROUPS:
        if has_group(table, g):
            vals = commission if g == S_COMMISSION else groups[g]
            w.line(g, ratio(neg(vals), revenue, months), fmt=PCT, indent=2, total=share(vals))
    w.line("Остаётся продавцу", ratio(net, revenue, months), fmt=PCT, indent=1,
           total=(sum(net.values()) / rev_total) if rev_total else 0.0)


def write_by_store(ws, tables, months):
    w = SheetWriter(ws, months, "Ключевые показатели по кабинетам")
    for store, table in tables.items():
        revenue, commission, groups, compensation, expenses, net = pnl_totals(table, months)
        w.section(store)
        w.line("Выручка за вычетом возвратов", revenue, indent=1)
        w.line(S_COMMISSION, commission, indent=1)
        for g in EXPENSE_GROUPS:
            if has_group(table, g):
                w.line(g, groups[g], indent=1)
        w.line("Итого услуги Маркета", expenses, indent=1, font=BOLD)
        w.line("Компенсации", compensation, indent=1)
        w.line("Итого", net, indent=1, fill=F_RESULT, font=BOLD)
        rev_total = sum(revenue.values())
        w.line("Услуги, % от выручки", ratio({m: -v for m, v in expenses.items()}, revenue, months),
               fmt=PCT, indent=1, total=(-sum(expenses.values()) / rev_total) if rev_total else 0.0)
        w.blank()


def write_payments(ws, pay_table, pnl_table, bank_orders, pending, months, title):
    w = SheetWriter(ws, months, title)
    w.section("НАЧИСЛЕНО К ВЫПЛАТЕ ДЕНЬГАМИ (отчёт по платежам, по дате транзакции)")
    for key in (P_BUYER, P_REFUND, P_SERVICES, P_OTHER):
        if key in pay_table:
            w.line(key, pay_table[key], indent=2)
    accrued = pay_table.get(P_ACCRUED, {})
    paid = pay_table.get(P_PAID, {})
    w.line(P_ACCRUED, accrued, fill=F_TOTAL, font=BOLD)
    w.blank()
    w.section("ПОСТУПЛЕНИЯ НА СЧЁТ")
    w.line(P_PAID, paid, fill=F_RESULT, font=BOLD)
    diff = {m: accrued.get(m, 0.0) - paid.get(m, 0.0) for m in months}
    w.line(P_DIFF, diff, indent=1)
    running, debt = 0.0, {}
    for m in months:
        running += diff[m]
        debt[m] = running
    w.line(P_DEBT, debt, indent=1, font=BOLD, total=running)
    w.blank()
    w.section("СПРАВОЧНО")
    pnl_money = pnl_table.get((S_INFO, I_MONEY), {})
    w.line(P_PNL, pnl_money, indent=2)
    prev = {m: pnl_money.get(month_add(date(int(m[:4]), int(m[5:]), 1), -1).strftime("%Y-%m"), 0.0) for m in months}
    w.line("Результат деньгами из ОПиУ за предыдущий месяц (Маркет перечисляет его в следующем месяце)",
           prev, indent=2, font=BOLD, total=sum(pnl_money.get(m, 0.0) for m in months[:-1]))
    for key in (P_POINTS_IN, P_POINTS_OUT):
        if key in pay_table:
            w.line(key, pay_table[key], indent=2)
    wait_total = sum(pending.values())
    w.row += 1
    ws.cell(w.row, 1, "Сверка: по статусам Маркета ещё ожидает перевода / удержания").font = BOLD
    ws.cell(w.row, len(months) + 2, round(wait_total, 2)).number_format = NUM
    w.row += 1
    for status, value in sorted(pending.items()):
        ws.cell(w.row, 1, status).alignment = Alignment(indent=2)
        ws.cell(w.row, len(months) + 2, round(value, 2)).number_format = NUM
        w.row += 1
    ws.cell(w.row, 1, "Расхождение: не перечислено накопительно − ожидает по статусам").font = BOLD
    ws.cell(w.row, len(months) + 2, round(running - wait_total, 2)).number_format = NUM
    w.row += 2

    ws.cell(w.row, 1, "Платёжные поручения").font = Font(bold=True, size=12)
    w.row += 1
    for c, v in enumerate(["Номер", "Дата", "Сумма"], 1):
        cell = ws.cell(w.row, c, v)
        cell.font, cell.fill = WHITE_BOLD, F_HEADER
    w.row += 1
    for bid, (d, s) in sorted(bank_orders.items(), key=lambda x: (x[1][0] or date.min, x[0])):
        ws.cell(w.row, 1, str(bid))
        ws.cell(w.row, 2, d).number_format = "DD.MM.YYYY"
        ws.cell(w.row, 3, round(s, 2)).number_format = NUM
        w.row += 1


def write_table(ws, header, rows, widths, num_cols):
    ws.append(header)
    for r in rows:
        ws.append(r)
    for c, wdt in enumerate(widths, 1):
        ws.cell(1, c).font, ws.cell(1, c).fill = WHITE_BOLD, F_HEADER
        ws.column_dimensions[get_column_letter(c)].width = wdt
    for c in num_cols:
        for (cell,) in ws.iter_rows(min_row=2, min_col=c, max_col=c):
            cell.number_format = NUM
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


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
            # Налоговая база: фактическая оплата покупателя с учётом скидок Маркета и возвратов.
            "tax_base": {m: round(v, 2) for m, v in (tax_base or {}).get(name, {}).items()
                         if abs(v) >= 0.005},
            "tax_base_by_article": [[mon, art, round(value, 2)]
                                    for (mon, art), value in (tax_base_by_article or {}).get(name, {}).items()
                                    if abs(value) >= 0.005],
            # Заказы [[месяц, артикул, шт, ₽, отменено шт, отменено ₽]] и остатки [[артикул, склад, вид, шт]]
            **goods_dump((goods or {}).get(name) or {}),
        }
    Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def safe_name(name, used=None, limit=31):
    base = re.sub(r'[\[\]:*?/\\]', "", name)[:limit] or "Кабинет"
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


def probe(accounts, here):
    today = date.today()
    for acc in accounts:
        name = acc.get("name") or "Кабинет"
        client = YMClient(acc["api_key"], acc.get("interval_seconds", GENERATE_INTERVAL))
        campaigns = client.campaigns()
        out = {"campaigns": campaigns}
        biz = acc.get("business_id") or next(((c.get("business") or {}).get("id") for c in campaigns), None)
        print(f"[{name}] кабинет {biz}, магазинов {len(campaigns)}; генерирую отчёт по услугам за 7 дней...")
        sv = client.report("/v2/reports/united-marketplace-services/generate",
                           {"businessId": int(biz), "dateFrom": str(today - timedelta(days=7)), "dateTo": str(today)},
                           "probe услуги")
        out["services"] = {k: v[:5] for k, v in sv.items()}
        if campaigns:
            prev = month_add(date(today.year, today.month, 1), -1)
            print(f"[{name}] генерирую отчёт по реализации за {prev:%Y-%m}...")
            rz = client.report("/v2/reports/goods-realization/generate",
                               {"campaignId": campaigns[0]["id"], "year": prev.year, "month": prev.month}, "probe реализация")
            out["realization"] = {k: v[:5] for k, v in rz.items()}
        f = here / f"ym_probe_{safe_name(name)}.json"
        f.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[{name}] образцы сохранены: {f}")


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="Финансы Яндекс Маркета по месяцам для нескольких кабинетов")
    ap.add_argument("--config", default=str(here / "ym_accounts.json"))
    ap.add_argument("--from", dest="date_from", help="Первый месяц, ГГГГ-ММ")
    ap.add_argument("--to", dest="date_to", help="Последний месяц, ГГГГ-ММ")
    ap.add_argument("--out", help="Путь к Excel-файлу")
    ap.add_argument("--refresh", action="store_true", help="Скачать заново даже закрытые периоды")
    ap.add_argument("--from-cache", action="store_true", help="Не обращаться к API, взять отчёты из папки cache")
    ap.add_argument("--dump", help="Сохранить таблицы ОПиУ в JSON для сводного отчёта (opiu_all.py)")
    ap.add_argument("--no-excel", action="store_true", help="Не сохранять Excel (удобно вместе с --dump)")
    ap.add_argument("--grain", choices=("month", "day"), default="month",
                    help="Разрез выгрузки: month — по месяцам (по умолчанию), day — по дням")
    ap.add_argument("--probe", action="store_true", help="Скачать 2 небольших отчёта, сохранить образцы и выйти")
    args = ap.parse_args()
    global GRAIN
    GRAIN = args.grain

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    accounts = [a for a in cfg["accounts"] if a.get("enabled", True)]
    if not accounts:
        sys.exit("В конфиге нет активных кабинетов")
    if args.probe:
        probe(accounts, here)
        return

    today = date.today()
    period = cfg.get("period") or {}
    end = parse_month(args.date_to or period.get("to") or f"{today.year}-{today.month:02d}")
    default_start = (end[0] - 1, end[1] + 1) if end[1] < 12 else (end[0], 1)
    start = parse_month(args.date_from or period.get("from") or f"{default_start[0]}-{default_start[1]:02d}")
    d_from = date(start[0], start[1], 1)
    d_to = min(month_end(date(end[0], end[1], 1)), today)
    if d_from > d_to:
        sys.exit("Пустой период.")
    if GRAIN == "day":
        month_keys = [(d_from + timedelta(days=i)).isoformat() for i in range((d_to - d_from).days + 1)]
    else:
        month_keys, m = [], d_from
        while m <= d_to:
            month_keys.append(m.strftime("%Y-%m"))
            m = month_add(m, 1)
    print(f"Период: {d_from} — {d_to}, кабинетов: {len(accounts)}")

    tables, svc_details, real_details, pay_data, failed = {}, {}, {}, {}, []
    checks, store_sales, store_by_article, store_goods = {}, {}, {}, {}
    store_tax_base, store_tax_base_by_article = {}, {}
    for acc in accounts:
        name = acc.get("name") or "Кабинет"
        print(f"\n[{name}]")
        try:
            client = YMClient(acc["api_key"], acc.get("interval_seconds", GENERATE_INTERVAL))
            services, realization, orders, payments, goods = load_account(
                acc, client, d_from, d_to, here / "cache" / safe_name(name), args)
        except RuntimeError as e:
            print(f"  ОШИБКА: {e}")
            failed.append(name)
            continue
        table, svc_detail, real_detail, sales, skipped, by_article, tax_base, tax_by_article = \
            aggregate(services, realization, orders, set(month_keys))
        if payments:
            pay_table, bank_orders, pending, other = aggregate_payments(payments, set(month_keys))
            pay_data[name] = (pay_table, bank_orders, pending)
            # Премии и компенсации деньгами есть только в отчёте по платежам — добавляем в ОПиУ.
            # Компенсацию за потерянные заказы берём отсюда, только если её нет в отчёте по реализации.
            # «Премия» — выплата деньгами неизрасходованных баллов: баллы уже в выручке, поэтому в ОПиУ её не добавляем.
            has_lost = any(s == G_COMPENSATION for s, _ in table)
            for label, vals in other.items():
                is_premium = "преми" in label.lower()
                if has_lost and "потерян" in label.lower():
                    continue
                for mon, v in vals.items():
                    if is_premium:
                        table[(S_INFO, I_PREMIUM)][mon] += v
                    else:
                        table[(G_COMPENSATION, label)][mon] += v
                    table[(S_INFO, I_MONEY)][mon] += v
        if skipped:
            print(f"  строк услуг с датой вне периода (не учтены): {skipped}")
        tables[name], svc_details[name], real_details[name] = table, svc_detail, real_detail
        store_sales[name] = sales
        store_by_article[name] = by_article
        store_goods[name] = goods
        store_tax_base[name] = tax_base
        store_tax_base_by_article[name] = tax_by_article
        checks[name] = []
        if name in pay_data:
            pay_table, bank_orders, pending = pay_data[name]
            accrued = sum(pay_table.get(P_ACCRUED, {}).values())
            paid = sum(pay_table.get(P_PAID, {}).values())
            waiting = sum(pending.values())
            checks[name] = [["Перечислено на счёт за период, ₽", round(paid, 2)],
                            ["Не перечислено Маркетом на конец периода, ₽", round(accrued - paid, 2)],
                            ["Ожидает перевода/удержания по статусам Маркета, ₽", round(waiting, 2)],
                            ["Расхождение сверки выплат, ₽", round(accrued - paid - waiting, 2)]]
        if skipped:
            checks[name].append(["Строк услуг с датой вне периода (не учтены)", skipped])

    if not tables:
        sys.exit("\nНе удалось получить данные ни по одному кабинету.")

    active = [m for m in month_keys if any(abs(v.get(m, 0.0)) >= 0.005 for t in tables.values() for v in t.values())]
    if active:
        month_keys = month_keys[month_keys.index(active[0]):]

    if args.dump:
        write_dump(args.dump, "Яндекс Маркет", month_keys, tables, S_INFO,
                   lambda t: dict(pnl_totals(t, month_keys)[-1]), checks, store_sales, store_by_article, store_goods,
                   store_tax_base, store_tax_base_by_article)
        print(f"Выгрузка для сводного отчёта: {args.dump}")
    if args.no_excel:
        return

    combined = defaultdict(lambda: defaultdict(float))
    for table in tables.values():
        for key, vals in table.items():
            for mk, v in vals.items():
                combined[key][mk] += v

    wb = Workbook()
    used = set()
    ws = wb.active
    ws.title = safe_name("Итого", used)
    write_pnl(ws, combined, month_keys, f"ОПиУ Яндекс Маркет — все кабинеты ({len(tables)})")
    if len(tables) > 1:
        write_by_store(wb.create_sheet(safe_name("По кабинетам", used)), tables, month_keys)
    for name, table in tables.items():
        write_pnl(wb.create_sheet(safe_name(name, used)), table, month_keys, f"ОПиУ Яндекс Маркет — {name}")
    for name, (pay_table, bank_orders, pending) in pay_data.items():
        sheet = safe_name("Выплаты" if len(pay_data) == 1 else f"Выплаты {name}", used)
        write_payments(wb.create_sheet(sheet), pay_table, tables[name], bank_orders, pending, month_keys,
                       f"Сверка с выплатами Маркета — {name}")
    write_table(wb.create_sheet(safe_name("Услуги", used)),
                ["Кабинет", "Месяц", "Лист отчёта", "Услуга", "Строк", "Оплачено деньгами", "Оплачено баллами",
                 "Полная стоимость"],
                [[n, mon, sheet, svc, cnt, round(money, 2), round(pts, 2), round(money + pts, 2)]
                 for n, det in svc_details.items() for (mon, sheet, svc), (cnt, money, pts) in sorted(det.items())],
                [18, 9, 36, 50, 8, 18, 18, 18], [6, 7, 8])
    write_table(wb.create_sheet(safe_name("Реализация", used)),
                ["Кабинет", "Месяц", "Магазин", "Лист отчёта", "Штук", "Цена продавца", "Оплачено покупателями"],
                [[n, mon, shop, sheet, qty, round(amt, 2), round(paid, 2)]
                 for n, det in real_details.items() for (mon, shop, sheet), (qty, amt, paid) in sorted(det.items())],
                [18, 9, 30, 26, 10, 18, 22], [6, 7])

    out = Path(args.out) if args.out else here / f"ym_finance_{month_keys[0]}_{month_keys[-1]}.xlsx"
    wb.save(out)
    print(f"\nГотово: {out}")
    if failed:
        print("Не загружены кабинеты: " + ", ".join(failed))


if __name__ == "__main__":
    main()
