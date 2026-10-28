"""Главная: сводка по магазинам (юрлицо × маркетплейс) за период — факт, остатки и прогноз на конец месяца.

Цифры — те же, что на дашбордах WB и Ozon (дневные выгрузки расчёта ОПиУ из кэша): каждая строка — кабинет
маркетплейса, магазин — юрлицо кабинета из «Кабинетов маркетплейсов». Реклама — затраты на продвижение товаров,
маркетинг (у Ozon: продвижение бренда, баллы за отзывы, «Оригинал», Premium Pro, механики лояльности) — отдельно.
Прогноз: факт за прошедшие дни периода × (дней в периоде / дней прошло); для текущего месяца — на конец месяца.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from app.services.dashboard import Dashboard, DashboardError

FLOW = ("orders_qty", "orders_sum", "net_qty", "revenue", "profit", "ads_only", "marketing", "total_marketing", "net_to_pay")


def _pct(v, base):
    return v / base * 100 + 0.0 if base else None        # + 0.0: без «−0,00%»


def _shares(r: dict) -> dict:
    """Доли от выручки и маржинальность — из сумм строки."""
    rev = r.get("revenue") or 0
    r["margin"] = _pct(r.get("profit", 0), rev)
    r["drr"] = _pct(-r.get("ads_only", 0), rev)
    r["marketing_pct"] = _pct(-r.get("marketing", 0), rev)
    r["total_marketing_pct"] = _pct(-r.get("total_marketing", 0), rev)
    return r


def _row(board: Dashboard, cabinet: str, start: date, end: date) -> dict | None:
    d = board.data()
    last = date.fromisoformat(d["last_day"]) if d.get("last_day") else None
    fact_end = min(end, last) if last else end
    if fact_end < start:
        return None
    v = board.derive(board.totals(start, fact_end, cabinet=cabinet))
    row = {k: v.get(k, 0) or 0 for k in ("orders_qty", "orders_sum", "net_qty", "revenue", "profit", "ads_only", "marketing", "net_to_pay")}
    row["total_marketing"] = row["ads_only"] + row["marketing"]
    row["buyout"] = board.funnel_buyout(start, fact_end, cabinet=cabinet)
    row["stock_cost"] = v.get("stock_cost", 0)
    # Свой склад (FBS) виден в кабинетах всех маркетплейсов — для «Итого» его надо считать один раз
    today = date.today().isoformat()
    own, price = {}, {}
    for art, qty in (d["cabinets"][cabinet].get("stocks_own") or {}).items():
        p = board._cost_at(d["costs"].get(art), today)
        if p and p[1] is not None:
            own[art], price[art] = qty, p[1]
    row["_own"], row["_own_cost"] = own, price
    # Прогноз — до конца выбранного периода (для текущего месяца — до конца месяца); прошедший период не досчитываем
    passed, total = (fact_end - start).days + 1, (end - start).days + 1
    factor = total / passed if fact_end < end else 1.0
    forecast = {k: row[k] * factor for k in FLOW}
    forecast["buyout"] = row["buyout"]
    row.update(fact_to=fact_end.isoformat(), days_passed=passed, days_total=total, forecast=_shares(forecast))
    return _shares(row)


def summary(boards: dict[str, Dashboard], start: date, end: date, legal_names: dict[tuple[str, str], str]) -> dict:
    """boards: {"wb": Dashboard, "ozon": Dashboard}; legal_names: {(маркетплейс, кабинет): юрлицо}."""
    if start > end:
        raise DashboardError("Начало периода позже конца")
    rows, missing = [], []
    for mp, board in boards.items():
        try:
            cabinets = sorted(board.data()["cabinets"])
        except DashboardError:
            missing.append(board.setup["title"])
            continue
        for cab in cabinets:
            r = _row(board, cab, start, end)
            if r is None:
                continue
            title = board.setup["title"]
            rows.append({"shop": legal_names.get((title, cab)) or cab, "cabinet": cab, "mp": mp, "mp_title": title, **r})
    order = list(boards)
    rows.sort(key=lambda r: (r["shop"].lower(), order.index(r["mp"])))
    total = {k: sum(r[k] for r in rows) for k in FLOW + ("stock_cost",)}
    # Итого остатков: склады МП — по каждой строке, свой склад одного юрлица — один раз (по артикулу — больший остаток)
    shared = defaultdict(dict)
    for r in rows:
        own, cost = r.pop("_own"), r.pop("_own_cost")
        total["stock_cost"] -= sum(qty * cost[a] for a, qty in own.items())
        for art, qty in own.items():
            if qty > shared[r["shop"]].get(art, (0, 0))[0]:
                shared[r["shop"]][art] = (qty, cost[art])
    total["stock_cost"] += sum(qty * cost for arts in shared.values() for qty, cost in arts.values())
    ordered = sum(r["orders_qty"] for r in rows if r["buyout"] is not None)
    total["buyout"] = (sum(r["buyout"] * r["orders_qty"] for r in rows if r["buyout"] is not None) / ordered) if ordered else None
    forecast = {k: sum(r["forecast"][k] for r in rows) for k in FLOW}
    forecast["buyout"] = total["buyout"]
    total["forecast"] = _shares(forecast)
    days = sorted({(r["days_passed"], r["days_total"]) for r in rows})
    return {"period": [start.isoformat(), end.isoformat()], "rows": rows, "total": _shares(total), "missing": missing,
            "days_passed": max((d[0] for d in days), default=0), "days_total": max((d[1] for d in days), default=0),
            "fact_to": max((r["fact_to"] for r in rows), default=None)}
