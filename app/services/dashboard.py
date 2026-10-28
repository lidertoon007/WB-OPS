"""Дашборд «Оцифровка»: ключевые показатели за любой период с фильтрами по бренду, категории и артикулу.

Источник — дневная выгрузка расчёта ОПиУ маркетплейса (wb_finance.py / ozon_finance.py --grain day --dump),
собранная из уже скачанного кэша: те же строки отчётов и те же правила разнесения по статьям, что и
в разделе «Финансы», поэтому цифры сходятся. Заказы, остатки и цены — из кэша товаров маркетплейса,
себестоимость и подготовка — из opiu/cost.csv (одна на все кабинеты), налог — ставка tax_percent из конфига кабинетов.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from app.services.nomenclature import COST_COL, PREP_COL, art_key, brand_key, num

DUMP_NAME = "_wbops_dashboard_day.json"
REVENUE, COMMISSION, ACQUIRING = "Выручка", "Вознаграждение Wildberries", "Эквайринг"
LOGISTICS, ADS, STORAGE_GROUP = "Логистика", "Реклама и продвижение", "Хранение и приёмка"
PENALTY, OTHER, COMPENSATION, ADVANCE, INFO = "Штрафы", "Прочие удержания", "Компенсации и доплаты", "Авансы за услуги WB и их возврат", "Справочно"


PROMOTION_LABEL = "WB Продвижение"      # «Оказание услуг «WB Продвижение»» в отчёте реализации
# Маркетплейсы дашборда: папка и скрипт расчёта ОПиУ, конфиг кабинетов
MARKETPLACES = {"wb": {"dir": "Wildberries", "script": "wb_finance.py", "config": "wb_accounts.json", "title": "Wildberries"},
                "ozon": {"dir": "Ozon", "script": "ozon_finance.py", "config": "ozon_accounts.json", "title": "Ozon"}}
# Статьи ozon_finance.py -> показатели дашборда. Реклама Ozon (продвижение, оплата за клик, звёздные товары и т. п.)
# списывается в начислениях — она вся идёт в «Рекламу», отдельной истории затрат, как у WB, нет.
OZON_METRICS = {"Комиссия Ozon": "commission", "Логистика и доставка": "logistics", "Обработка возвратов и отмен": "logistics",
                "Эквайринг и платёжные услуги": "acquiring", "Реклама и продвижение": "ads_promo",
                "Хранение и складские операции": "storage", "Штрафы и нарушения": "penalty",
                "Подписки и прочие услуги Ozon": "other", "Компенсации и прочие начисления": "compensation"}
# «Маркетинг» Ozon на Главной (не реклама за заказы): продвижение бренда, баллы за отзывы, бейдж «Оригинал»
# («Бренд проверен»), Premium Pro и механики лояльности. Сравнение — по началу подписи, без регистра.
OZON_MARKETING = ("продвижение бренда", "ускоренный сбор отзывов", "сбор первых отзывов", "баллы за отзыв", "отзывы за баллы",
                  "бейдж оригинал", "бейдж «бренд проверен»", "подписка premium pro", "баллы за покупк", "бонусы продавца",
                  "программа лояльности", "механики лояльности")
FUNNEL_URL ="https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products"
FUNNEL_TIMEOUT = 30
FUNNEL_TTL, FUNNEL_TTL_CLOSED = 3600, 24 * 3600


class DashboardError(Exception):
    pass


class Dashboard:
    def __init__(self, project_dir, python_path, cost_rows, ad_spend=lambda start, end, cabinet: None, retro_rows=lambda: [],
                 mp: str = "wb"):
        self.mp = mp                        # wb | ozon — см. MARKETPLACES
        self.setup = MARKETPLACES[mp]
        self.project_dir = project_dir      # callable -> папка opiu
        self.python_path = python_path      # callable -> python для скриптов ОПиУ
        self.cost_rows = cost_rows          # callable -> строки cost.csv
        self.retro_rows = retro_rows        # callable -> ретро-бонусы по брендам [{brand, pct, since}] (Nomenclature.retro_rows)
        self.ad_spend = ad_spend            # callable -> затраты рекламного кабинета (AdsAnalytics.spend) или None
        self.state = {"status": "idle", "message": "", "started_at": None, "finished_at": None}
        self._lock = threading.Lock()
        self._data = None
        self._data_key = None
        self._manual_brands: dict = {}      # артикул -> бренд из cost.csv (как в ОПиУ — приоритетнее данных WB)
        self._book: dict = {}               # артикул -> {article, brand, category} из cost.csv
        self._funnel_cache: dict = {}       # (кабинет, начало, конец) -> (время загрузки, воронка)

    # ------------------------------------------------------------------ выгрузка
    def wb_dir(self) -> Path:
        """Папка расчёта маркетплейса (историческое имя: сначала дашборд был только по WB)."""
        return Path(self.project_dir()) / self.setup["dir"]

    def dump_path(self) -> Path:
        return self.wb_dir() / "cache" / DUMP_NAME

    def _sources(self) -> list[Path]:
        """Файлы кэша, из которых собирается выгрузка (у Ozon кэш разложен по папкам кабинетов)."""
        cache = self.wb_dir() / "cache"
        return [p for p in cache.rglob("*.json") if p.name != DUMP_NAME] if cache.exists() else []

    def sources_mtime(self) -> float:
        return max((p.stat().st_mtime for p in self._sources()), default=0)

    def stale(self) -> bool:
        path = self.dump_path()
        return not path.exists() or path.stat().st_mtime < self.sources_mtime()

    def refresh(self, wait: bool = False) -> dict:
        """Собрать дневную выгрузку из кэша WB (без запросов к API) — в фоне."""
        with self._lock:
            if self.state["status"] == "running":
                return dict(self.state)
            s = self.setup
            script = self.wb_dir() / s["script"]
            config = self.wb_dir() / s["config"]
            if not script.exists() or not config.exists():
                raise DashboardError(f"Не найден расчёт {s['title']} (opiu/{s['dir']}/{s['script']} и {s['config']}) — "
                                     f"добавьте кабинет {s['title']} в «Кабинеты маркетплейсов»")
            if not any(not p.name.endswith("_goods.json") for p in self._sources()):
                raise DashboardError(f"Нет скачанных данных {s['title']}: в разделе «Финансы» нажмите «Обновить данные МП»")
            self.state = {"status": "running", "message": f"Собираю показатели из кэша {s['title']}…", "started_at": time.time(), "finished_at": None}
        cmd = [self.python_path(), "-X", "utf8", str(script), "--config", str(config), "--dump", str(self.dump_path()), "--no-excel",
               "--grain", "day", "--from-cache", "--to", f"{date.today():%Y-%m}"]

        def run():
            try:
                kwargs = {"creationflags": 0x08000000} if sys.platform == "win32" else {}   # без окна консоли
                # В Windows вывод скрипта по умолчанию в cp1251 — символы вроде «≥» роняют его; просим UTF-8
                env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
                proc = subprocess.run(cmd, cwd=str(self.wb_dir()), capture_output=True, text=True, encoding="utf-8",
                                      errors="replace", timeout=900, env=env, **kwargs)
                if proc.returncode or not self.dump_path().exists():
                    tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
                    raise DashboardError(" ".join(tail)[:400] or "скрипт завершился с ошибкой")
                self.state = {**self.state, "status": "done", "message": "Готово", "finished_at": time.time()}
            except Exception as exc:
                self.state = {**self.state, "status": "error", "message": str(exc)[:400], "finished_at": time.time()}
        if wait:
            run()
        else:
            threading.Thread(target=run, daemon=True).start()
        return dict(self.state)

    # ------------------------------------------------------------------ данные
    def data(self) -> dict:
        path = self.dump_path()
        if not path.exists():
            raise DashboardError("Показатели ещё не собраны")
        accounts = self.wb_dir() / self.setup["config"]
        key = (path.stat().st_mtime, self._cost_key(), accounts.stat().st_mtime if accounts.exists() else 0)
        if self._data is not None and self._data_key == key:
            return self._data
        raw = json.loads(path.read_text(encoding="utf-8"))
        rates = self._tax_rates()
        d = {"days": raw.get("months") or [], "cabinets": {}, "catalog": {}}
        for cab, c in (raw.get("cabinets") or {}).items():
            flows = defaultdict(lambda: defaultdict(float))      # day -> метрика -> сумма (по артикулу ниже)
            per_article = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))   # day -> art -> метрика
            for day, art, group, label, value in c.get("by_article") or []:
                metric = self._metric(group, label)
                if metric:
                    per_article[day][art_key(art)][metric] += value
            for _mon, day, _order, art, qty, rev in c.get("orders") or []:
                m = per_article[day][art_key(art)]
                if qty > 0:
                    m["sold_qty"] += qty
                else:
                    m["return_qty"] += -qty
                m["net_qty"] += qty
            for day, art, value in c.get("tax_base_by_article") or []:
                per_article[day][art_key(art)]["paid"] += value
            for day, art, q, s, cq, cs in c.get("ordered") or []:
                m = per_article[day][art_key(art)]
                m["orders_qty"] += q
                m["orders_sum"] += s
                m["cancel_qty"] += cq
            for art, brand, subject in c.get("catalog") or []:
                d["catalog"].setdefault(art_key(art), {"article": art, "brand": brand, "category": subject})
            stocks, own = defaultdict(float), defaultdict(float)     # own — свой склад продавца (FBS), его видят все МП
            for art, _wh, kind, qty in c.get("stocks") or []:
                if kind not in ("to_client", "returning"):
                    stocks[art_key(art)] += float(qty or 0)
                    if kind == "own":
                        own[art_key(art)] += float(qty or 0)
                d["catalog"].setdefault(art_key(art), {"article": art, "brand": "", "category": ""})
            prices = {}
            for row in c.get("prices") or []:
                if row and len(row) >= 3:
                    prices[art_key(row[0])] = row[2] or row[1]
            d["cabinets"][cab] = {"days": {day: {a: dict(m) for a, m in arts.items()} for day, arts in per_article.items()},
                                  "stocks": dict(stocks), "stocks_own": dict(own), "prices": prices, "stocks_at": c.get("stocks_at"),
                                  "ordered_from": c.get("ordered_from"), "tax_rate": rates.get(cab, rates.get(None, 0.0))}
        last = [day for cab in d["cabinets"].values() for day, arts in cab["days"].items()
                if any(m.get("revenue_sales") or m.get("revenue_returns") or m.get("commission") or m.get("logistics") for m in arts.values())]
        d["last_day"] = max(last) if last else None
        d["first_day"] = min(last) if last else None
        d["costs"] = self._costs()
        d["brands"] = dict(self._manual_brands)
        # Бренд и категория из справочника cost.csv: Ozon в отчётах их не даёт, у WB заполняем пустые
        seen = {a for cab in d["cabinets"].values() for arts in cab["days"].values() for a in arts if a}
        for art in seen:
            entry = d["catalog"].setdefault(art, {"article": self._book.get(art, {}).get("article") or art, "brand": "", "category": ""})
            book = self._book.get(art) or {}
            entry["brand"] = book.get("brand") or entry["brand"]
            entry["category"] = entry["category"] or book.get("category") or ""
        d["built_at"] = path.stat().st_mtime
        self._data, self._data_key = d, key
        return d

    def _metric(self, group, label):
        if group == INFO:
            return None
        if group == REVENUE:
            return "revenue_returns" if label.startswith("Возврат") else "revenue_sales"
        if self.mp == "ozon":
            if label.lower().startswith(OZON_MARKETING):
                return "marketing"
            return OZON_METRICS.get(group, "other")
        if group == STORAGE_GROUP:
            return "acceptance" if "приёмк" in label.lower() else "storage"
        if group == ADS and PROMOTION_LABEL in label:
            return "ads_promo"          # удержание за рекламные кампании — на дашборде заменяется затратами кабинета
        return {COMMISSION: "commission", ACQUIRING: "acquiring", LOGISTICS: "logistics", ADS: "ads", PENALTY: "penalty",
                OTHER: "other", COMPENSATION: "compensation", ADVANCE: "advance"}.get(group, "other")

    def _cost_key(self):
        try:
            p = Path(self.project_dir()) / "cost.csv"
            return p.stat().st_mtime if p.exists() else 0
        except Exception:
            return 0

    def _costs(self) -> dict:
        """{артикул: [(действует_с, кратность, себестоимость, подготовка)]} по возрастанию даты."""
        out = defaultdict(list)
        self._manual_brands, self._book = {}, {}
        for r in self.cost_rows():
            key = art_key(r.get("артикул"))
            if key:
                out[key].append((r.get("действует_с", ""), num(r.get("кратность")) or 1.0, num(r.get(COST_COL)), num(r.get(PREP_COL))))
                if r.get("бренд"):
                    self._manual_brands[key] = r["бренд"]
                book = self._book.setdefault(key, {"article": r.get("артикул"), "brand": "", "category": ""})
                book["brand"] = book["brand"] or r.get("бренд") or ""
                book["category"] = book["category"] or r.get("категория") or ""
        for v in out.values():
            v.sort(key=lambda x: x[0])
        return dict(out)

    def _retro(self) -> dict:
        """{бренд: [(действует_с, %)]} — ретро-бонусы производителей по возрастанию даты."""
        out = defaultdict(list)
        for r in self.retro_rows() or []:
            out[brand_key(r["brand"])].append((r.get("since") or "", r["pct"]))
        for v in out.values():
            v.sort(key=lambda x: x[0])
        return dict(out)

    @staticmethod
    def _retro_at(rows, day) -> float | None:
        """% ретро-бонуса, действующий в месяце дня: строка с наибольшей датой «действует_с» не позже."""
        chosen = None
        for since, pct in rows or []:
            if not since or since[:7] <= day[:7]:
                chosen = pct
        return chosen

    def _brand(self, d, art) -> str:
        """Бренд артикула как в ОПиУ: заполненный вручную в cost.csv, иначе из данных WB."""
        return d.get("brands", {}).get(art) or (d["catalog"].get(art) or {}).get("brand") or ""

    @staticmethod
    def _cost_at(rows, day):
        chosen = None
        for since, mult, cost, prep in rows or []:
            if not since or since[:7] <= day[:7]:
                chosen = (mult, cost, prep)
        return chosen

    def _tax_rates(self) -> dict:
        try:
            cfg = json.loads((self.wb_dir() / self.setup["config"]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        default = cfg.get("tax_percent")
        rates = {None: float(default or 0)}
        for acc in cfg.get("accounts") or []:
            rate = acc.get("tax_percent", default)
            if rate is not None:
                rates[acc.get("name") or "Магазин"] = float(rate)
        return rates

    # ------------------------------------------------------------------ показатели
    def options(self) -> dict:
        d = self.data()
        cat = d["catalog"]
        return {"cabinets": sorted(d["cabinets"]), "brands": sorted({c["brand"] for c in cat.values() if c["brand"]}),
                "categories": sorted({c["category"] for c in cat.values() if c["category"]}),
                "articles": sorted(({"key": k, "article": c["article"], "brand": c["brand"], "category": c["category"]} for k, c in cat.items()),
                                   key=lambda x: x["article"].lower()),
                "first_day": d["first_day"], "last_day": d["last_day"],
                "ordered_from": min(filter(None, (c["ordered_from"] for c in d["cabinets"].values())), default=None),
                "stocks_at": max(filter(None, (c["stocks_at"] for c in d["cabinets"].values())), default=None),
                "built_at": d["built_at"]}

    def _match(self, key, brand, category, articles):
        if articles and key not in articles:
            return False
        if brand or category:
            c = self._data["catalog"].get(key)
            if not c or (brand and c["brand"] != brand) or (category and c["category"] != category):
                return False
        return True

    def totals(self, start: date, end: date, cabinet=None, brand=None, category=None, articles=None, with_ads=True) -> dict:
        d = self.data()
        t = defaultdict(float)
        has_cost_for_all = True
        filtered = bool(brand or category or articles)
        retro = self._retro()
        for cab, c in d["cabinets"].items():
            if cabinet and cab != cabinet:
                continue
            rate = c["tax_rate"] / 100
            day = start
            while day <= end:
                key_day = day.isoformat()
                for art, m in c["days"].get(key_day, {}).items():
                    if filtered and (not art or not self._match(art, brand, category, articles)):
                        continue
                    for k, v in m.items():
                        t[k] += v
                    t["tax"] += m.get("paid", 0) * rate
                    if m.get("net_qty"):
                        price = self._cost_at(d["costs"].get(art), key_day)
                        if price and price[1] is not None:
                            t["cogs"] += m["net_qty"] * price[0] * price[1]
                            t["prep"] += m["net_qty"] * price[0] * (price[2] or 0)
                            pct = self._retro_at(retro.get(brand_key(self._brand(d, art))), key_day)
                            if pct:
                                t["retro"] += m["net_qty"] * price[0] * price[1] * pct / 100
                        elif m["net_qty"] > 0:
                            has_cost_for_all = False
                            t["no_cost_qty"] += m["net_qty"]
                day += timedelta(days=1)
            for art, qty in c["stocks"].items():          # остатки — на сегодня, не за период
                if filtered and not self._match(art, brand, category, articles):
                    continue
                t["stock_qty"] += qty
                price = self._cost_at(d["costs"].get(art), date.today().isoformat())
                if price and price[1] is not None:
                    t["stock_cost"] += qty * price[1]
                t["stock_retail"] += qty * (c["prices"].get(art) or 0)
        t["days"] = (end - start).days + 1
        t["has_cost_for_all"] = has_cost_for_all
        # Реклама — по истории затрат рекламного кабинета (как «Затраты за период» в ЛК WB), а не по удержаниям
        # в отчёте реализации: WB удерживает за продвижение пачками со сдвигом, на неделе суммы не совпадают.
        # С фильтром по товарам — затраты кампаний по артикулам. Удержания (t["ads"]) остаются в «К перечислению».
        spend = self.ad_spend(start.isoformat(), end.isoformat(), cabinet) if with_ads and self.mp == "wb" else None
        if spend is not None:
            if filtered:
                t["ads_spend"] = -sum(v for (_, art), v in spend["by_article"].items() if self._match(art, brand, category, articles))
            else:
                t["ads_spend"] = -sum(spend["by_day"].values())
        return dict(t)

    def by_article(self, start: date, end: date) -> dict[str, dict]:
        """Суммы totals() по каждому артикулу за период, за один проход по дням (для ABC-анализа).
        Расходы без артикула (часть рекламы, хранения, подписки) сюда не попадают — как при фильтре по товару."""
        d = self.data()
        out = defaultdict(lambda: defaultdict(float))
        retro = self._retro()
        for c in d["cabinets"].values():
            rate = c["tax_rate"] / 100
            day = start
            while day <= end:
                key_day = day.isoformat()
                for art, m in c["days"].get(key_day, {}).items():
                    if not art:
                        continue
                    t = out[art]
                    for k, v in m.items():
                        t[k] += v
                    t["tax"] += m.get("paid", 0) * rate
                    if m.get("net_qty"):
                        price = self._cost_at(d["costs"].get(art), key_day)
                        if price and price[1] is not None:
                            t["cogs"] += m["net_qty"] * price[0] * price[1]
                            t["prep"] += m["net_qty"] * price[0] * (price[2] or 0)
                            pct = self._retro_at(retro.get(brand_key(self._brand(d, art))), key_day)
                            if pct:
                                t["retro"] += m["net_qty"] * price[0] * price[1] * pct / 100
                        elif m["net_qty"] > 0:
                            t["no_cost_qty"] += m["net_qty"]
                day += timedelta(days=1)
        spend = self.ad_spend(start.isoformat(), end.isoformat(), None) if self.mp == "wb" else None
        if spend is not None:                      # реклама WB — затраты кампаний по артикулам, как на дашборде с фильтром
            for (_, art), v in spend["by_article"].items():
                out[art_key(art)]["ads_spend"] -= v
            for t in out.values():
                t.setdefault("ads_spend", 0.0)
        return {art: dict(t) for art, t in out.items()}

    def stocks(self) -> tuple[dict[str, float], dict[str, float]]:
        """Остатки сейчас по артикулам: (склады маркетплейса, свой склад продавца)."""
        mp_stock, own = defaultdict(float), defaultdict(float)
        for c in self.data()["cabinets"].values():
            for art, qty in c["stocks"].items():
                o = (c.get("stocks_own") or {}).get(art, 0)
                mp_stock[art] += qty - o
                own[art] += o
        return dict(mp_stock), dict(own)

    @staticmethod
    def derive(t: dict) -> dict:
        revenue = t.get("revenue_sales", 0) + t.get("revenue_returns", 0)
        withheld = sum(t.get(k, 0) for k in ("commission", "acquiring", "logistics", "storage", "acceptance", "ads", "ads_promo",
                                             "marketing", "penalty", "other"))
        net = revenue + withheld + t.get("compensation", 0) + t.get("advance", 0)      # к перечислению — по удержаниям WB
        # Реклама — затраты рекламного кабинета (если загружены; иначе удержание «WB Продвижение»). Прочие строки
        # группы «Реклама и продвижение» (баллы и участие в программе лояльности, подписка «Джем») — в «Прочие удержания».
        # Маркетинг Ozon (OZON_MARKETING) на дашборде входит в рекламу, на Главной показывается отдельно.
        marketing = t.get("marketing", 0)
        promo = t.get("ads_spend", t.get("ads_promo", 0)) + marketing
        ads = promo
        other = t.get("other", 0) + t.get("advance", 0) + t.get("ads", 0)
        expenses = withheld - t.get("ads_promo", 0) - marketing + promo
        cogs, prep, tax = -t.get("cogs", 0), -t.get("prep", 0), -t.get("tax", 0)
        retro = t.get("retro", 0)                       # ретро-бонусы производителей — доход, «+»
        profit = net - t.get("ads_promo", 0) - marketing + promo + cogs + retro + prep + tax
        sold = t.get("sold_qty", 0)
        days = max(1, t.get("days", 1))
        pct = lambda v, base: (v / base * 100) if base else None
        per_day_sales = t.get("net_qty", 0) / days
        per_day_orders = t.get("orders_qty", 0) / days
        return {
            "profit": profit, "margin": pct(profit, revenue),
            "orders_sum": t.get("orders_sum", 0), "orders_qty": t.get("orders_qty", 0), "cancel_qty": t.get("cancel_qty", 0),
            "revenue": revenue, "sold_qty": sold, "return_qty": t.get("return_qty", 0), "net_qty": t.get("net_qty", 0),
            "paid": t.get("paid", 0),
            "ads": ads, "ads_source": "spend" if "ads_spend" in t else "withheld", "ads_withheld": t.get("ads_promo", 0),
            "ads_only": ads - marketing, "marketing": marketing,
            "drr": pct(-ads, revenue), "drr_orders": pct(-ads, t.get("orders_sum", 0)),
            "cost_per_order": (-ads / t["orders_qty"]) if t.get("orders_qty") else None,
            "logistics": t.get("logistics", 0), "logistics_pct": pct(-t.get("logistics", 0), revenue),
            "logistics_unit": (-t.get("logistics", 0) / sold) if sold else None,
            "buyout": pct(t.get("net_qty", 0), t.get("orders_qty", 0)),
            "storage": t.get("storage", 0), "storage_pct": pct(-t.get("storage", 0), revenue),
            "acceptance": t.get("acceptance", 0), "acceptance_pct": pct(-t.get("acceptance", 0), revenue),
            "commission": t.get("commission", 0), "commission_pct": pct(-t.get("commission", 0), revenue),
            "acquiring": t.get("acquiring", 0), "acquiring_pct": pct(-t.get("acquiring", 0), revenue),
            "penalty": t.get("penalty", 0), "penalty_pct": pct(-t.get("penalty", 0), revenue),
            "other": other, "other_pct": pct(-other, revenue),
            "compensation": t.get("compensation", 0), "compensation_pct": pct(t.get("compensation", 0), revenue),
            "cogs": cogs, "cogs_pct": pct(-cogs, revenue), "prep": prep, "prep_pct": pct(-prep, revenue),
            "retro": retro, "retro_pct": pct(retro, revenue),
            "tax": tax, "tax_pct": pct(-tax, revenue), "mp_expenses": expenses, "mp_expenses_pct": pct(-expenses, revenue),
            "roi": pct(profit, -(cogs + prep)) if (cogs + prep) else None,
            "avg_price_seller": (t.get("revenue_sales", 0) / sold) if sold else None,
            "avg_price_paid": (t.get("paid", 0) / t.get("net_qty", 0)) if t.get("net_qty", 0) > 0 else None,
            "stock_qty": t.get("stock_qty", 0), "stock_cost": t.get("stock_cost", 0), "stock_retail": t.get("stock_retail", 0),
            "turnover_sales": (t.get("stock_qty", 0) / per_day_sales) if per_day_sales > 0 else None,
            "turnover_orders": (t.get("stock_qty", 0) / per_day_orders) if per_day_orders > 0 else None,
            "no_cost_qty": t.get("no_cost_qty", 0), "net_to_pay": net,
        }

    # ------------------------------------------------------------------ процент выкупа — из воронки продаж WB
    def _funnel_tokens(self) -> dict[str, str]:
        """{кабинет: ключ API} включённых кабинетов из конфига расчёта (туда их пишет панель)."""
        try:
            cfg = json.loads((self.wb_dir() / "wb_accounts.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        return {a.get("name") or "Магазин": a["token"] for a in cfg.get("accounts") or []
                if a.get("enabled", True) and a.get("token")}

    @staticmethod
    def fetch_funnel(token: str, start: date, end: date) -> dict[str, tuple[float, float]]:
        """Воронка WB за период: {артикул: (выкуплено шт, отменено шт)}. Лимит WB — 3 запроса в минуту."""
        import httpx
        out, offset = defaultdict(lambda: [0.0, 0.0]), 0
        with httpx.Client(timeout=FUNNEL_TIMEOUT) as client:
            for _ in range(50):
                response = client.post(FUNNEL_URL, headers={"Authorization": token},
                                       json={"selectedPeriod": {"start": start.isoformat(), "end": end.isoformat()},
                                             "limit": 1000, "offset": offset})
                response.raise_for_status()
                products = (response.json().get("data") or {}).get("products") or []
                for pr in products:
                    info, st = pr.get("product") or {}, (pr.get("statistic") or {}).get("selected") or {}
                    row = out[art_key(info.get("vendorCode") or f"nmId {info.get('nmId')}")]
                    row[0] += num(st.get("buyoutCount")) or 0
                    row[1] += num(st.get("cancelCount")) or 0
                offset += len(products)
                if len(products) < 1000:
                    break
        return {k: (v[0], v[1]) for k, v in out.items()}

    def _funnel(self, cabinet: str, token: str, start: date, end: date):
        """Воронка кабинета за период с кэшем: закрытые периоды — на сутки, текущий — на час. None — WB не ответил."""
        key = (cabinet, start, end)
        cached = self._funnel_cache.get(key)
        ttl = FUNNEL_TTL_CLOSED if end < date.today() - timedelta(days=7) else FUNNEL_TTL
        if cached and time.time() - cached[0] < ttl:
            return cached[1]
        try:
            rows = self.fetch_funnel(token, start, end)
        except Exception:
            return cached[1] if cached else None     # лимит запросов, сеть, нет доступа к «Аналитике»
        self._funnel_cache[key] = (time.time(), rows)
        return rows

    def funnel_buyout(self, start: date, end: date, cabinet=None, brand=None, category=None, articles=None) -> float | None:
        """Процент выкупа как в воронке продаж WB: выкуплено / (выкуплено + отменено) по заказам периода.
        None — воронку получить не удалось хотя бы по одному кабинету."""
        if self.mp == "ozon":
            # Воронка Ozon есть только по месяцам: выкуп — доля заказанного без отмен за период
            t = self.totals(start, end, cabinet, brand, category, articles, with_ads=False)
            ordered = t.get("orders_qty", 0)
            return (ordered - t.get("cancel_qty", 0)) / ordered * 100 if ordered else None
        tokens, bought, cancelled = self._funnel_tokens(), 0.0, 0.0
        filtered = bool(brand or category or articles)
        cabinets = [cab for cab in self.data()["cabinets"] if not cabinet or cab == cabinet]
        if not cabinets or any(cab not in tokens for cab in cabinets):
            return None
        for cab in cabinets:
            rows = self._funnel(cab, tokens[cab], start, end)
            if rows is None:
                return None
            for art, (b, c) in rows.items():
                if filtered and not self._match(art, brand, category, articles):
                    continue
                bought += b
                cancelled += c
        return bought / (bought + cancelled) * 100 if bought + cancelled else None

    def summary(self, start: date, end: date, previous: tuple[date, date] | None = None, **filters) -> dict:
        if start > end:
            raise DashboardError("Начало периода позже конца")
        if previous and previous[0] <= previous[1]:
            prev_start, prev_end = previous             # например, прошлый календарный месяц
        else:
            span = (end - start).days + 1
            prev_end = start - timedelta(days=1)
            prev_start = prev_end - timedelta(days=span - 1)
        cur = self.derive(self.totals(start, end, **filters))
        prev = self.derive(self.totals(prev_start, prev_end, **filters))
        # Выкуп по отчёту реализации (продано за период / заказано за период) на коротком периоде уходит за 100%:
        # выкупают заказы прошлых недель. Берём воронку WB — как в личном кабинете.
        for values, (a, b) in ((cur, (start, end)), (prev, (prev_start, prev_end))):
            values["buyout"] = self.funnel_buyout(a, b, **filters)
        return {"mp": self.mp, "current": cur, "previous": prev, "period": [start.isoformat(), end.isoformat()],
                "previous_period": [prev_start.isoformat(), prev_end.isoformat()], "options": self.options()}
