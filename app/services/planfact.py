"""План-факт: планы на месяц по бизнесу и по артикулам, факт, отклонения и прогноз на конец месяца.

Факт берётся из РНП (оперативные данные WB по дням): заказы шт и ₽, выручка по выкупам (заказы × процент
выкупа артикула), реклама, прибыль (юнит-экономика артикула минус реклама, себестоимость и налог).
Прогноз на конец месяца = факт по вчерашний день + средний темп × оставшиеся дни (включая сегодня).
Темп — средний за полные дни месяца; если их меньше трёх — за последние 7 дней.
План по заказам и марже артикула синхронизирован с планом РНП (заказов в день и маржа).
"""
from __future__ import annotations

import calendar
import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from app.services.nomenclature import art_key

MSK = timezone(timedelta(hours=3))
TOTAL = "__total__"
# ключ, название, единица, направление (up — больше лучше), тип (sum — суммируется, ratio — доля)
METRICS = [
    ("orders", "Заказы", "шт", "up", "sum"),
    ("orders_sum", "Заказы", "₽", "up", "sum"),
    ("revenue", "Выручка (выкупы)", "₽", "up", "sum"),
    ("ads", "Реклама", "₽", "down", "sum"),
    ("profit", "Чистая прибыль", "₽", "up", "sum"),
    ("margin", "Маржинальность", "%", "up", "ratio"),
    ("drr", "ДРР", "%", "down", "ratio"),
]
MKEYS = {m[0]: m for m in METRICS}
SUMS = [m[0] for m in METRICS if m[4] == "sum"]


class PlanFactError(Exception):
    pass


def _ratio(key, v: dict):
    if key == "margin":
        return round(v["profit"] / v["revenue"] * 100, 2) if v.get("revenue") else None
    if key == "drr":
        return round(v["ads"] / v["orders_sum"] * 100, 2) if v.get("orders_sum") else None
    return None


def status(key, plan, forecast) -> str | None:
    if plan is None or forecast is None:
        return None
    _k, _t, _u, direction, kind = MKEYS[key]
    if kind == "ratio":
        diff = forecast - plan if direction == "up" else plan - forecast
        return "good" if diff >= 0 else "warn" if diff >= -2 else "bad"
    if direction == "up":
        if plan <= 0:
            return "good" if forecast >= plan else "bad"
        p = forecast / plan * 100
        return "good" if p >= 100 else "warn" if p >= 90 else "bad"
    if plan <= 0:
        return "good" if forecast <= 0 else "bad"
    p = forecast / plan * 100
    return "good" if p <= 100 else "warn" if p <= 110 else "bad"


class PlanFact:
    def __init__(self, store, rnp):
        self.store = store
        self.rnp = rnp
        with store._connect() as c:
            c.execute("CREATE TABLE IF NOT EXISTS pf_plans (month TEXT, scope TEXT, metric TEXT, value REAL, PRIMARY KEY(month, scope, metric))")

    # ------------------------------------------------------------------ планы
    def plans(self, month: str) -> dict:
        out = defaultdict(dict)
        with self.store._connect() as c:
            for scope, metric, value in c.execute("SELECT scope, metric, value FROM pf_plans WHERE month=?", (month,)):
                out[scope][metric] = value
        # планы РНП (заказов в день и маржа) — если в план-факте артикулу их не задавали
        days = calendar.monthrange(*map(int, month.split("-")))[1]
        for (art, m), p in (self.rnp.plans() or {}).items():
            if m != month:
                continue
            if p.get("orders_per_day") is not None and "orders" not in out[art]:
                out[art]["orders"] = round(p["orders_per_day"] * days, 1)
            if p.get("margin") is not None and "margin" not in out[art]:
                out[art]["margin"] = p["margin"]
        return out

    def set_plan(self, month: str, scope: str, metric: str, value) -> None:
        if metric not in MKEYS:
            raise PlanFactError("Неизвестный показатель")
        try:
            datetime.strptime(month, "%Y-%m")
        except ValueError:
            raise PlanFactError("Месяц в формате ГГГГ-ММ")
        scope = TOTAL if scope in ("", TOTAL) else art_key(scope)
        if not scope:
            raise PlanFactError("Укажите артикул")
        with self.store._connect() as c:
            if value is None or value == "":
                c.execute("DELETE FROM pf_plans WHERE month=? AND scope=? AND metric=?", (month, scope, metric))
            else:
                c.execute("INSERT OR REPLACE INTO pf_plans(month, scope, metric, value) VALUES(?,?,?,?)", (month, scope, metric, float(value)))
        if scope != TOTAL and metric in ("orders", "margin"):         # тот же план — в РНП
            days = calendar.monthrange(*map(int, month.split("-")))[1]
            cur = (self.rnp.plans() or {}).get((scope, month)) or {}
            opd = cur.get("orders_per_day")
            mg = cur.get("margin")
            if metric == "orders":
                opd = None if value in (None, "") else round(float(value) / days, 2)
            else:
                mg = None if value in (None, "") else float(value)
            try:
                self.rnp.set_plan(scope, month, opd, mg)
            except Exception:
                pass

    def autofill(self, month: str, growth: float = 0.0, target: str = "all", overwrite: bool = False) -> dict:
        """Планы = факт прошлого месяца (по полным месяцам, пересчитанный на число дней) × (1 + рост %)."""
        y, m = map(int, month.split("-"))
        prev_end = date(y, m, 1) - timedelta(days=1)
        prev_start = prev_end.replace(day=1)
        days = calendar.monthrange(y, m)[1]
        scale = days / prev_end.day
        t = self.rnp.table(prev_start.isoformat(), prev_end.isoformat())
        k = 1 + (growth or 0) / 100
        current = self.plans(month)
        written = 0

        def put(scope, metric, value):
            nonlocal written
            if value is None or abs(value) < 1e-9 or (not overwrite and metric in current.get(scope, {})):   # нулевой факт — не план
                return
            self.set_plan(month, scope, metric, round(value, 2 if metric in ("margin", "drr") else 0))
            written += 1
        rows = [(TOTAL, t["total"])] if target in ("all", "total") else []
        if target in ("all", "articles"):
            rows += [(r["key"], r) for r in t["articles"] if (r.get("orders") or 0) > 0]
        for scope, r in rows:
            for metric in SUMS:
                v = r.get(metric)
                if v is None:
                    continue
                put(scope, metric, v * scale * (k if metric != "ads" else 1))
            if r.get("margin") is not None:
                put(scope, "margin", r["margin"])
            if r.get("drr") is not None:
                put(scope, "drr", r["drr"])
        return {"written": written, "source": [prev_start.isoformat(), prev_end.isoformat()]}

    # ------------------------------------------------------------------ отчёт
    @staticmethod
    def _vals(r: dict) -> dict:
        return {k: float(r.get(k) or 0) for k in SUMS}

    def report(self, month: str, brand: str | None = None, category: str | None = None) -> dict:
        try:
            y, m = map(int, month.split("-"))
            first = date(y, m, 1)
        except ValueError:
            raise PlanFactError("Месяц в формате ГГГГ-ММ")
        days_total = calendar.monthrange(y, m)[1]
        last = date(y, m, days_total)
        today = datetime.now(MSK).date()
        current = first <= today <= last
        future = first > today
        upto = min(today, last)
        yesterday = upto - timedelta(days=1) if current else upto
        elapsed = (yesterday - first).days + 1 if not future else 0        # полных дней прошло
        elapsed = max(0, elapsed)
        remaining = days_total - elapsed                                   # включая сегодня
        flt = {"brand": brand, "category": category}

        fact_t = self.rnp.table(first.isoformat(), upto.isoformat(), **flt) if not future else None
        full_t = (self.rnp.table(first.isoformat(), yesterday.isoformat(), **flt) if current and elapsed > 0 else fact_t) if not future else None
        if elapsed >= 3:
            pace_t, pace_days = full_t, elapsed
        else:
            ref = (today if current or future else last + timedelta(days=1))
            pace_t, pace_days = self.rnp.table((ref - timedelta(days=7)).isoformat(), (ref - timedelta(days=1)).isoformat(), **flt), 7

        plans = self.plans(month)
        prev_end = first - timedelta(days=1)
        prev_t = self.rnp.table(prev_end.replace(day=1).isoformat(), prev_end.isoformat(), **flt)

        def by_key(t):
            return {r["key"]: r for r in (t or {}).get("articles", [])}
        fact_a, full_a, pace_a, prev_a = by_key(fact_t), by_key(full_t), by_key(pace_t), by_key(prev_t)

        def calc(plan: dict, fact_r, full_r, pace_r, prev_r, plan_src: dict | None = None) -> dict:
            fact = self._vals(fact_r or {})
            full = self._vals(full_r or {})
            pace = self._vals(pace_r or {})
            if future:
                forecast = {k: pace[k] / pace_days * days_total for k in SUMS}
            elif current:
                forecast = {k: full[k] + pace[k] / pace_days * remaining for k in SUMS}
            else:
                forecast = dict(fact)
            prev = self._vals(prev_r or {})
            out = {}
            for key, _t, _u, direction, kind in METRICS:
                p = plan.get(key)
                if kind == "sum":
                    f, fc = fact[key], forecast[key]
                    expected = p * elapsed / days_total if p is not None else None
                    out[key] = {"plan": p, "fact": round(f, 2), "forecast": round(fc, 2), "prev": round(prev[key], 2),
                                "done_pct": round(f / p * 100, 1) if p else None,
                                "forecast_pct": round(fc / p * 100, 1) if p else None,
                                "expected": round(expected, 2) if expected is not None else None,
                                "gap": round(full[key] - expected, 2) if expected is not None and current else (round(f - p, 2) if p is not None and not current else None),
                                "need_per_day": round(max(0.0, p - f) / remaining, 2) if p is not None and current and remaining and direction == "up" else None,
                                "left_per_day": round((p - f) / remaining, 2) if p is not None and current and remaining and direction == "down" else None,
                                "pace_per_day": round(pace[key] / pace_days, 2) if pace_days else None,
                                "status": status(key, p, fc if not future else None) if not future else None}
                else:
                    f, fc = _ratio(key, fact), _ratio(key, forecast)
                    out[key] = {"plan": p, "fact": f, "forecast": fc, "prev": _ratio(key, prev), "status": status(key, p, fc) if not future else None,
                                "diff": round(fc - p, 2) if p is not None and fc is not None else None}
                if plan_src and key in plan_src:
                    out[key]["plan_source"] = plan_src[key]
            return out

        # Итог: свой план или сумма планов артикулов
        tplan, tsrc = dict(plans.get(TOTAL, {})), {}
        for k in tplan:
            tsrc[k] = "set"
        for k in SUMS:
            if k not in tplan:
                vals = [p[k] for s, p in plans.items() if s != TOTAL and k in p]
                if vals:
                    tplan[k] = sum(vals)
                    tsrc[k] = "articles"
        total = calc(tplan, (fact_t or {}).get("total"), (full_t or {}).get("total"), (pace_t or {}).get("total"), prev_t.get("total"), tsrc)

        keys = set(fact_a) | {s for s in plans if s != TOTAL}
        meta = {}
        for src in (prev_a, pace_a, full_a, fact_a):
            for k, r in src.items():
                meta[k] = {"article": r.get("article") or k, "brand": r.get("brand") or "", "category": r.get("category") or "", "nm_id": r.get("nm_id"),
                           "stock": r.get("stock"), "no_cost": r.get("no_cost") or 0}
        articles = []
        for k in keys:
            mt = meta.get(k) or {"article": k, "brand": "", "category": "", "nm_id": None}
            if (brand and mt.get("brand") and mt["brand"] != brand) or (category and mt.get("category") and mt["category"] != category):
                continue
            if k not in meta and (brand or category):
                continue
            row = calc(plans.get(k, {}), fact_a.get(k), full_a.get(k), pace_a.get(k), prev_a.get(k))
            articles.append({"key": k, **mt, "has_plan": bool(plans.get(k)), "metrics": row})
        articles.sort(key=lambda a: -(a["metrics"]["orders_sum"]["fact"] or 0))

        # Дни месяца: факт по дням (для накопительного графика)
        by_day = (fact_t or {}).get("by_day", {})
        daily = []
        d = first
        while d <= last:
            v = by_day.get(d.isoformat())
            daily.append({"day": d.isoformat(), **({k: round(float(v.get(k) or 0), 2) for k in SUMS} if v else {})})
            d += timedelta(days=1)

        counts = defaultdict(lambda: defaultdict(int))
        for a in articles:
            for key, v in a["metrics"].items():
                if v.get("status"):
                    counts[key][v["status"]] += 1
        alerts = self._alerts(total, articles)
        rnp_meta = fact_t or prev_t or {}
        return {"month": month, "days_total": days_total, "elapsed": elapsed, "remaining": remaining, "current": current, "future": future,
                "today": today.isoformat(), "pace_window": "month" if elapsed >= 3 else "7days",
                "metrics": [{"key": k, "title": t, "unit": u, "dir": d_, "kind": kd} for k, t, u, d_, kd in METRICS],
                "total": total, "articles": articles, "daily": daily, "status_counts": {k: dict(v) for k, v in counts.items()}, "alerts": alerts,
                "sources": rnp_meta.get("sources"), "synced_at": rnp_meta.get("synced_at"), "rnp_state": rnp_meta.get("state"),
                "no_cost": (fact_t or {}).get("total", {}).get("no_cost") if fact_t else None,
                "settings": self.settings()}

    @staticmethod
    def _alerts(total: dict, articles: list) -> list:
        out = []
        names = {k: f"{t}, {u}" if u != "%" else t for k, t, u, _d, _k in METRICS}
        for key, v in total.items():
            if v.get("status") in ("bad", "warn") and v.get("plan") is not None:
                if MKEYS[key][4] == "sum":
                    out.append({"level": v["status"], "scope": "total", "metric": key,
                                "text": f"{names[key]}: прогноз {v['forecast']:,.0f} при плане {v['plan']:,.0f} ({v['forecast_pct']}%)".replace(",", " ")})
                else:
                    out.append({"level": v["status"], "scope": "total", "metric": key,
                                "text": f"{names[key]}: прогноз {v['forecast']:.1f}% при плане {v['plan']:.1f}%".replace(".", ",")})
        bad = [a for a in articles if a["metrics"]["orders"].get("status") == "bad" or a["metrics"]["profit"].get("status") == "bad"]
        for a in sorted(bad, key=lambda a: (a["metrics"]["orders"].get("forecast_pct") or 999))[:8]:
            mo, mp = a["metrics"]["orders"], a["metrics"]["profit"]
            parts = []
            if mo.get("status") == "bad":
                parts.append(f"заказы {mo['forecast_pct']}% плана")
            if mp.get("status") == "bad":
                parts.append(f"прибыль {mp['forecast_pct']}% плана" if mp.get("forecast_pct") is not None else "прибыль ниже плана")
            out.append({"level": "bad", "scope": a["key"], "metric": "orders", "text": f"{a['article']}: " + ", ".join(parts)})
        return out

    # ------------------------------------------------------------------ уведомления
    def settings(self) -> dict:
        try:
            s = json.loads(self.store.get_setting("planfact_settings", "") or "{}")
        except ValueError:
            s = {}
        return {"notify": bool(s.get("notify")), "hour": int(s.get("hour") or 10)}

    def save_settings(self, notify: bool, hour: int) -> dict:
        self.store.set_setting("planfact_settings", json.dumps({"notify": bool(notify), "hour": max(0, min(23, int(hour)))}))
        return self.settings()

    def digest(self) -> tuple[str, str] | None:
        """Текст утренней сводки для Telegram или None, если планов нет."""
        month = datetime.now(MSK).strftime("%Y-%m")
        r = self.report(month)
        t = r["total"]
        if not any(v.get("plan") is not None for v in t.values()):
            return None
        icon = {"good": "🟢", "warn": "🟡", "bad": "🔴"}
        lines = [f"План-факт за {month}: прошло {r['elapsed']} из {r['days_total']} дней"]
        for key, title, unit, _d, kind in METRICS:
            v = t[key]
            if v.get("plan") is None:
                continue
            if kind == "sum":
                lines.append(f"{icon.get(v['status'], '•')} {title}, {unit}: факт {v['fact']:,.0f}, прогноз {v['forecast']:,.0f} из {v['plan']:,.0f} ({v['forecast_pct']}%)".replace(",", " "))
            else:
                lines.append(f"{icon.get(v['status'], '•')} {title}: прогноз {v['forecast'] or 0:.1f}%, план {v['plan']:.1f}%".replace(".", ","))
        bad = [a for a in r["alerts"] if a["scope"] != "total"]
        if bad:
            lines.append("")
            lines.append("Отстают артикулы:")
            lines += [f"• {a['text']}" for a in bad[:8]]
        return "WB OPS — план-факт", "\n".join(lines)
