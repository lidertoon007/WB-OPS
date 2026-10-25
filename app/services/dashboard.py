"""Дашборд «Оцифровка»: ключевые показатели за любой период с фильтрами по бренду, категории и артикулу.

Источник — дневная выгрузка расчёта ОПиУ по Wildberries (wb_finance.py --grain day --dump), собранная
из уже скачанного кэша: те же строки отчёта реализации и те же правила разнесения по статьям, что и
в разделе «Финансы», поэтому цифры сходятся. Заказы, остатки и цены — из кэша товаров WB,
себестоимость и подготовка — из opiu/cost.csv, налог — ставка tax_percent из конфига кабинетов WB.
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

from app.services.nomenclature import COST_COL, PREP_COL, art_key, num

DUMP_NAME = "_wbops_dashboard_day.json"
REVENUE, COMMISSION, ACQUIRING = "Выручка", "Вознаграждение Wildberries", "Эквайринг"
LOGISTICS, ADS, STORAGE_GROUP = "Логистика", "Реклама и продвижение", "Хранение и приёмка"
PENALTY, OTHER, COMPENSATION, ADVANCE, INFO = "Штрафы", "Прочие удержания", "Компенсации и доплаты", "Авансы за услуги WB и их возврат", "Справочно"


class DashboardError(Exception):
    pass


class Dashboard:
    def __init__(self, project_dir, python_path, cost_rows):
        self.project_dir = project_dir      # callable -> папка opiu
        self.python_path = python_path      # callable -> python для скриптов ОПиУ
        self.cost_rows = cost_rows          # callable -> строки cost.csv
        self.state = {"status": "idle", "message": "", "started_at": None, "finished_at": None}
        self._lock = threading.Lock()
        self._data = None
        self._data_key = None

    # ------------------------------------------------------------------ выгрузка
    def wb_dir(self) -> Path:
        return Path(self.project_dir()) / "Wildberries"

    def dump_path(self) -> Path:
        return self.wb_dir() / "cache" / DUMP_NAME

    def sources_mtime(self) -> float:
        cache = self.wb_dir() / "cache"
        files = [p for p in cache.glob("*.json") if p.name != DUMP_NAME] if cache.exists() else []
        return max((p.stat().st_mtime for p in files), default=0)

    def stale(self) -> bool:
        path = self.dump_path()
        return not path.exists() or path.stat().st_mtime < self.sources_mtime()

    def refresh(self, wait: bool = False) -> dict:
        """Собрать дневную выгрузку из кэша WB (без запросов к API) — в фоне."""
        with self._lock:
            if self.state["status"] == "running":
                return dict(self.state)
            script = self.wb_dir() / "wb_finance.py"
            config = self.wb_dir() / "wb_accounts.json"
            if not script.exists() or not config.exists():
                raise DashboardError("Не найден расчёт Wildberries (opiu/Wildberries/wb_finance.py и wb_accounts.json)")
            if not any(p.name != DUMP_NAME and not p.name.endswith("_goods.json") for p in (self.wb_dir() / "cache").glob("*.json")):
                raise DashboardError("Нет скачанных данных WB: в разделе «Финансы» нажмите «Обновить данные»")
            self.state = {"status": "running", "message": "Собираю показатели из кэша WB…", "started_at": time.time(), "finished_at": None}
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
        accounts = self.wb_dir() / "wb_accounts.json"
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
            stocks = defaultdict(float)
            for art, _wh, kind, qty in c.get("stocks") or []:
                if kind not in ("to_client", "returning"):
                    stocks[art_key(art)] += float(qty or 0)
                d["catalog"].setdefault(art_key(art), {"article": art, "brand": "", "category": ""})
            prices = {}
            for row in c.get("prices") or []:
                if row and len(row) >= 3:
                    prices[art_key(row[0])] = row[2] or row[1]
            d["cabinets"][cab] = {"days": {day: {a: dict(m) for a, m in arts.items()} for day, arts in per_article.items()},
                                  "stocks": dict(stocks), "prices": prices, "stocks_at": c.get("stocks_at"),
                                  "ordered_from": c.get("ordered_from"), "tax_rate": rates.get(cab, rates.get(None, 0.0))}
        last = [day for cab in d["cabinets"].values() for day, arts in cab["days"].items()
                if any(m.get("revenue_sales") or m.get("revenue_returns") or m.get("commission") or m.get("logistics") for m in arts.values())]
        d["last_day"] = max(last) if last else None
        d["first_day"] = min(last) if last else None
        d["costs"] = self._costs()
        d["built_at"] = path.stat().st_mtime
        self._data, self._data_key = d, key
        return d

    @staticmethod
    def _metric(group, label):
        if group == INFO:
            return None
        if group == REVENUE:
            return "revenue_returns" if label.startswith("Возврат") else "revenue_sales"
        if group == STORAGE_GROUP:
            return "acceptance" if "приёмк" in label.lower() else "storage"
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
        for r in self.cost_rows():
            key = art_key(r.get("артикул"))
            if key:
                out[key].append((r.get("действует_с", ""), num(r.get("кратность")) or 1.0, num(r.get(COST_COL)), num(r.get(PREP_COL))))
        for v in out.values():
            v.sort(key=lambda x: x[0])
        return dict(out)

    @staticmethod
    def _cost_at(rows, day):
        chosen = None
        for since, mult, cost, prep in rows or []:
            if not since or since[:7] <= day[:7]:
                chosen = (mult, cost, prep)
        return chosen

    def _tax_rates(self) -> dict:
        try:
            cfg = json.loads((self.wb_dir() / "wb_accounts.json").read_text(encoding="utf-8"))
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

    def totals(self, start: date, end: date, cabinet=None, brand=None, category=None, articles=None) -> dict:
        d = self.data()
        t = defaultdict(float)
        has_cost_for_all = True
        filtered = bool(brand or category or articles)
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
        return dict(t)

    @staticmethod
    def derive(t: dict) -> dict:
        revenue = t.get("revenue_sales", 0) + t.get("revenue_returns", 0)
        expenses = sum(t.get(k, 0) for k in ("commission", "acquiring", "logistics", "storage", "acceptance", "ads", "penalty", "other"))
        net = revenue + expenses + t.get("compensation", 0) + t.get("advance", 0)
        cogs, prep, tax = -t.get("cogs", 0), -t.get("prep", 0), -t.get("tax", 0)
        profit = net + cogs + prep + tax
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
            "ads": t.get("ads", 0), "drr": pct(-t.get("ads", 0), revenue), "drr_orders": pct(-t.get("ads", 0), t.get("orders_sum", 0)),
            "cost_per_order": (-t.get("ads", 0) / t["orders_qty"]) if t.get("orders_qty") else None,
            "logistics": t.get("logistics", 0), "logistics_pct": pct(-t.get("logistics", 0), revenue),
            "logistics_unit": (-t.get("logistics", 0) / sold) if sold else None,
            "buyout": pct(t.get("net_qty", 0), t.get("orders_qty", 0)),
            "storage": t.get("storage", 0), "storage_pct": pct(-t.get("storage", 0), revenue),
            "acceptance": t.get("acceptance", 0), "acceptance_pct": pct(-t.get("acceptance", 0), revenue),
            "commission": t.get("commission", 0), "commission_pct": pct(-t.get("commission", 0), revenue),
            "acquiring": t.get("acquiring", 0), "acquiring_pct": pct(-t.get("acquiring", 0), revenue),
            "penalty": t.get("penalty", 0), "penalty_pct": pct(-t.get("penalty", 0), revenue),
            "other": t.get("other", 0) + t.get("advance", 0), "other_pct": pct(-(t.get("other", 0) + t.get("advance", 0)), revenue),
            "compensation": t.get("compensation", 0), "compensation_pct": pct(t.get("compensation", 0), revenue),
            "cogs": cogs, "cogs_pct": pct(-cogs, revenue), "prep": prep, "prep_pct": pct(-prep, revenue),
            "tax": tax, "tax_pct": pct(-tax, revenue), "mp_expenses": expenses, "mp_expenses_pct": pct(-expenses, revenue),
            "roi": pct(profit, -(cogs + prep)) if (cogs + prep) else None,
            "avg_price_seller": (t.get("revenue_sales", 0) / sold) if sold else None,
            "avg_price_paid": (t.get("paid", 0) / t.get("net_qty", 0)) if t.get("net_qty", 0) > 0 else None,
            "stock_qty": t.get("stock_qty", 0), "stock_cost": t.get("stock_cost", 0), "stock_retail": t.get("stock_retail", 0),
            "turnover_sales": (t.get("stock_qty", 0) / per_day_sales) if per_day_sales > 0 else None,
            "turnover_orders": (t.get("stock_qty", 0) / per_day_orders) if per_day_orders > 0 else None,
            "no_cost_qty": t.get("no_cost_qty", 0), "net_to_pay": net,
        }

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
        return {"current": cur, "previous": prev, "period": [start.isoformat(), end.isoformat()],
                "previous_period": [prev_start.isoformat(), prev_end.isoformat()], "options": self.options()}
