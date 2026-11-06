"""ABC-анализ продаж и панель неликвида — как лист «ABC» таблицы продавца, по данным WB и Ozon.

По каждому артикулу за период (по умолчанию 30 дней), по всем выбранным маркетплейсам сразу:
  • ABC по выручке, по оборачиваемости (продажам в штуках) и по прибыли: артикулы по убыванию, накопительная
    доля до 80% — A, до 95% — B, остальное — C; с нулём или минусом — C. Код ABC — три буквы подряд (AAC);
  • остаток сейчас (склады маркетплейсов + свой склад один раз), себестоимость остатка, продажи в день,
    запас в днях и статус: «Нет остатка», «Критический неликвид» (итоговая группа C и запас >120 дн или остаток
    без продаж; у A и B такой запас — «Избыток запаса»), «Избыток запаса» (60–120 дн), «Дефицит» (<15 дн при продажах),
    «В норме»;
  • итоговая группа — по большинству трёх букв (AAC → A, ABB → B, все три разные → B);
  • подсорт на 14 дней: 14 × продаж в день − остаток (для итоговой группы C — 0, дозакупать не нужно).
Цифры по артикулу — как на дашборде с фильтром по товару: выручка, удержания и реклама этого артикула,
себестоимость и подготовка из cost.csv, налог, ретро-бонусы; расходы без артикула в прибыль артикула не входят.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date

from app.services.dashboard import Dashboard, DashboardError

STATUSES = ("Дефицит", "В норме", "Избыток запаса", "Критический неликвид", "Нет остатка")
DEFICIT_DAYS, EXCESS_DAYS, DEAD_DAYS, RESTOCK_DAYS = 15, 60, 120, 14


def groups(values: dict[str, float]) -> dict[str, str]:
    """ABC по накопительной доле: до 80% — A, до 95% — B, остальное и всё ≤ 0 — C (как в таблице)."""
    total = sum(v for v in values.values() if v > 0)
    out, acc = {}, 0.0
    for key, v in sorted(values.items(), key=lambda kv: (-kv[1], kv[0])):
        acc += v
        share = acc / total if total else 0
        out[key] = "C" if v <= 0 else "A" if share <= 0.8 + 1e-9 else "B" if share <= 0.95 + 1e-9 else "C"
    return out


def final_group(code: str) -> str:
    """Итоговая группа по трём буквам (выручка, оборачиваемость, прибыль): та, что встречается 2–3 раза; все разные — B."""
    for g in "ABC":
        if code.count(g) >= 2:
            return g
    return "B"


def status(stock: float, sold: float, days_left: float, group: str = "C") -> str:
    """Неликвид — только у итоговой группы C: у A и B долгий запас — «Избыток запаса»."""
    if stock <= 0 and sold <= 0:
        return "Нет остатка"
    if stock > 0 and (sold <= 0 or days_left > DEAD_DAYS):
        return "Критический неликвид" if group == "C" else "Избыток запаса"
    if days_left > EXCESS_DAYS:
        return "Избыток запаса"
    if days_left < DEFICIT_DAYS and sold > 0:
        return "Дефицит"
    return "В норме"


def analysis(boards: dict[str, Dashboard], start: date, end: date, book: dict[str, dict]) -> dict:
    """boards — дашборды выбранных маркетплейсов; book — справочник cost.csv {артикул: {article, name, brand, category, cost}}."""
    if start > end:
        raise DashboardError("Начало периода позже конца")
    days = (end - start).days + 1
    sums = defaultdict(lambda: defaultdict(float))
    mp_stock, own, mps, shown = defaultdict(float), defaultdict(float), defaultdict(set), {}
    used = []
    for mp, board in boards.items():
        try:
            d = board.data()
        except DashboardError:
            continue
        used.append(board.setup["title"])
        for art, t in board.by_article(start, end).items():
            v = board.derive({**t, "days": days})
            s = sums[art]
            for k in ("net_qty", "revenue", "profit", "orders_qty"):
                s[k] += v.get(k) or 0
            if t.get("revenue_sales") or t.get("net_qty"):
                mps[art].add(board.setup["title"])
        stock, o = board.stocks()
        for art, q in stock.items():
            mp_stock[art] += q
        for art, q in o.items():
            own[art] = max(own[art], q)           # свой склад виден всем маркетплейсам — считаем один раз
        for art, c in d["catalog"].items():
            shown.setdefault(art, c)
    keys = set(sums) | {a for a, q in mp_stock.items() if q} | {a for a, q in own.items() if q} | set(book)
    keys.discard("")
    revenue = groups({k: sums[k]["revenue"] for k in keys})
    turnover = groups({k: sums[k]["net_qty"] for k in keys})
    profit = groups({k: sums[k]["profit"] for k in keys})
    total_revenue = sum(max(0.0, sums[k]["revenue"]) for k in keys)
    rows = []
    for k in keys:
        s, b, c = sums[k], book.get(k) or {}, shown.get(k) or {}
        sold, stock = s["net_qty"], mp_stock[k] + own[k]
        per_day = max(0.0, sold) / days
        left = stock / per_day if per_day > 0 else (999 if stock > 0 else 0)
        cost = b.get("cost")
        code = revenue[k] + turnover[k] + profit[k]
        group = final_group(code)
        st = status(stock, sold, left, group)
        restock = 0 if group == "C" or per_day <= 0 else max(0, math.ceil(RESTOCK_DAYS * per_day - stock))
        rows.append({"key": k, "article": b.get("article") or c.get("article") or k, "name": b.get("name") or "",
                     "brand": b.get("brand") or c.get("brand") or "", "category": b.get("category") or c.get("category") or "",
                     "marketplaces": sorted(mps[k]), "sold": sold, "revenue": s["revenue"], "profit": s["profit"],
                     "share": s["revenue"] / total_revenue * 100 if total_revenue and s["revenue"] > 0 else 0.0,
                     "buyout": s["net_qty"] / s["orders_qty"] * 100 if s["orders_qty"] else None,
                     "abc_revenue": revenue[k], "abc_turnover": turnover[k], "abc_profit": profit[k],
                     "abc": code, "group": group, "stock": stock, "unit_cost": cost,
                     "stock_cost": stock * cost if cost is not None else None, "per_day": per_day,
                     "days_left": round(left), "status": st, "restock": restock})
    rows.sort(key=lambda r: (-r["revenue"], r["article"].lower()))
    acc = 0.0
    for r in rows:
        acc += r["share"]
        r["cum_share"] = acc
    stock_total = sum(r["stock_cost"] or 0 for r in rows)
    dead = [r for r in rows if r["status"] == "Критический неликвид"]
    by_group = {}
    for g in "ABC":
        part = [r for r in rows if r["group"] == g]
        by_group[g] = {"count": len(part), "revenue": sum(r["revenue"] for r in part),
                       "stock_cost": sum(r["stock_cost"] or 0 for r in part),
                       "dead_cost": sum(r["stock_cost"] or 0 for r in part if r["status"] == "Критический неликвид")}
    by_status = {s: {"count": sum(1 for r in rows if r["status"] == s),
                     "stock_cost": sum(r["stock_cost"] or 0 for r in rows if r["status"] == s)} for s in STATUSES}
    dead_cost = sum(r["stock_cost"] or 0 for r in dead)
    return {"period": [start.isoformat(), end.isoformat()], "days": days, "marketplaces": used, "rows": rows,
            "kpi": {"revenue": sum(r["revenue"] for r in rows), "stock_cost": stock_total, "dead_cost": dead_cost,
                    "dead_share": dead_cost / stock_total * 100 if stock_total else 0.0, "dead_count": len(dead),
                    "no_cost": sum(1 for r in rows if r["unit_cost"] is None and r["stock"] > 0)},
            "groups": by_group, "statuses": by_status, "count": len(rows)}
