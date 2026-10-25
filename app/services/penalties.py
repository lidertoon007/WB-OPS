"""Аналитика штрафов Wildberries.

Источник — детализация отчётов реализации WB, которую уже скачивает расчёт ОПиУ
(opiu/Wildberries/cache/<кабинет>_<с>_<по>.json, метод finance-api sales-reports/detailed).
Из неё берём:
  • штрафы — строки с полем penalty (операция «Штраф»), причина в bonusTypeName;
  • прочие удержания — строки с полем deduction (реклама, подписки, баллы за отзывы, транзит…);
  • продажи по дням и артикулам — чтобы считать долю штрафов в выручке.
Кэш ОПиУ полностью пересобирается при каждом обновлении, поэтому кабинет переиндексируется целиком,
когда меняется файл. Штрафы по FBS связываются со сборочными заданиями по srid = rid.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.services.nomenclature import art_key

MSK = timezone(timedelta(hours=3))
CACHE_RE = re.compile(r"^(.*)_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.json$")

# Группы штрафов: ключ, название, цвет (порядок и цвет закреплены за группой)
GROUPS = [
    ("fbs", "Сроки и отмены FBS", "#e03131"),
    ("wrong", "Подмена и несоответствие", "#7950f2"),
    ("size", "Габариты упаковки", "#f08c00"),
    ("pvz", "Хранение возвратов на ПВЗ", "#1c7ed6"),
    ("label", "Маркировка", "#0ca678"),
    ("other", "Прочие штрафы", "#868e96"),
]
GROUP_TITLES = {k: t for k, t, _ in GROUPS}

# (регулярка по причине, группа, подсказка «что делать»)
RULES = [
    (r"срока передачи|просрочк.*передач", "fbs",
     "Сборочное задание передано WB позже срока. Собирайте и сдавайте поставку в течение суток — скорость отгрузки по складам видна в «Аналитике FBS»."),
    (r"отмена продавцом", "fbs",
     "Продавец отменил принятый заказ. Обычно это заказ без товара на складе — держите остатки FBS актуальными («Остатки FBS») и обнуляйте закончившиеся размеры."),
    (r"недовоз", "fbs",
     "В поставке пришло меньше товара, чем заявлено. Сверяйте состав поставки перед отгрузкой."),
    (r"отличного от заявленного|подмен", "wrong",
     "Покупатель получил не тот товар, размер или цвет. Сверяйте артикул, размер и стикер при сборке, особенно у похожих моделей."),
    (r"расхождени.*карточк|несоответстви", "wrong",
     "Склад нашёл расхождения товара с карточкой (характеристики, комплектация, цвет). Сверьте карточку с реальным товаром."),
    (r"габарит", "size",
     "Фактические габариты упаковки больше, чем указано в карточке. Перемерьте упаковку и исправьте габариты в карточке — штраф повторяется до исправления."),
    (r"хранение возвратов|возврат.*пвз", "pvz",
     "Возвраты лежат на ПВЗ дольше 3 дней. Забирайте возвраты быстрее или настройте их автоматическое получение."),
    (r"маркировк|честн", "label",
     "Нарушения с маркировкой «Честный знак». Проверяйте коды маркировки до отгрузки."),
    (r"невыполненный заказ", "fbs",
     "Заказ не выполнен по вине продавца. Проверьте остатки и сроки сборки по этому заданию."),
]
_RULES = [(re.compile(p, re.I), g, tip) for p, g, tip in RULES]

# Прочие удержания (не штрафы) — для контекста
DEDUCTIONS = [
    ("ads", "Реклама (WB Продвижение)", r"продвижени"),
    ("jam", "Подписка «Джем»", r"джем"),
    ("reviews", "Баллы за отзывы", r"отзыв"),
    ("transit", "Транзитные поставки", r"транзит"),
    ("utilization", "Утилизация", r"утилиз"),
    ("other", "Прочие удержания", r""),
]
DED_TITLES = {k: t for k, t, _ in DEDUCTIONS}
_DED = [(k, re.compile(p, re.I)) for k, _, p in DEDUCTIONS if p]


def clean_reason(text: str) -> str:
    t = re.sub(r",?\s*документ\s*№\s*\d+", "", str(text or ""), flags=re.I)
    t = re.sub(r"[,.]?\s*поставк[аи]\s*№?\s*\d+", "", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip(" .,") or "Без описания"


def classify(reason: str) -> tuple[str, str]:
    for rx, g, tip in _RULES:
        if rx.search(reason):
            return g, tip
    return "other", "Откройте детализацию отчёта реализации в кабинете WB, чтобы увидеть основание штрафа, и при несогласии оспорьте его через поддержку."


def classify_deduction(reason: str) -> str:
    for k, rx in _DED:
        if rx.search(reason):
            return k
    return "other"


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


class Penalties:
    def __init__(self, store, cache_dir, cabinets=lambda: []):
        self.store = store
        self.cache_dir = cache_dir          # callable -> Path папки opiu/Wildberries/cache (или None)
        self.cabinets = cabinets            # callable -> кабинеты (для связи с FBS-заданиями)
        self.state = {"status": "idle", "message": "", "finished_at": None}
        self._lock = threading.Lock()
        with store._connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS pen_rows (account TEXT, rrd_id INTEGER, rr_date TEXT, kind TEXT, reason TEXT, grp TEXT,
                amount REAL, nm_id INTEGER, article TEXT, brand TEXT, subject TEXT, srid TEXT, report_id INTEGER,
                PRIMARY KEY (account, rrd_id));
            CREATE INDEX IF NOT EXISTS pen_rows_date ON pen_rows(rr_date);
            CREATE TABLE IF NOT EXISTS pen_sales (account TEXT, day TEXT, nm_id INTEGER, article TEXT, brand TEXT, subject TEXT,
                revenue REAL, qty REAL, PRIMARY KEY (account, day, nm_id));
            CREATE TABLE IF NOT EXISTS pen_reports (account TEXT, report_id INTEGER, date_from TEXT, date_to TEXT, penalty_sum REAL,
                deduction_sum REAL, retail_sum REAL, PRIMARY KEY (account, report_id));
            """)

    # ------------------------------------------------------------------ индексация кэша ОПиУ
    def _sources(self) -> dict:
        """Кабинет -> файлы кэша (самый свежий + более ранние периоды)."""
        d = self.cache_dir()
        out = defaultdict(list)
        if not d or not Path(d).exists():
            return {}
        for p in Path(d).glob("*.json"):
            m = CACHE_RE.match(p.name)
            if m and not p.name.startswith("_"):
                out[m.group(1)].append((m.group(2), m.group(3), p))
        return out

    def signature(self) -> dict:
        return {acc: sorted([p.name, int(p.stat().st_mtime)] for _a, _b, p in files) for acc, files in self._sources().items()}

    def stale(self) -> bool:
        try:
            old = json.loads(self.store.get_setting("penalties_sources", "") or "{}")
        except ValueError:
            old = {}
        return self.signature() != old

    def sync(self, force: bool = False) -> dict:
        with self._lock:
            if self.state["status"] == "running":
                return dict(self.state)
            self.state = {"status": "running", "message": "Читаю отчёты реализации WB…", "finished_at": None}
        try:
            sig = self.signature()
            try:
                old = json.loads(self.store.get_setting("penalties_sources", "") or "{}")
            except ValueError:
                old = {}
            sources = self._sources()
            if not sources:
                raise RuntimeError("Нет скачанных отчётов реализации WB. Нажмите «Обновить из WB» — данные скачаются через расчёт ОПиУ.")
            for acc, files in sources.items():
                if force or sig.get(acc) != old.get(acc):
                    self.state["message"] = f"Читаю отчёты кабинета «{acc}»…"
                    self._index_account(acc, files)
            with self.store._connect() as c:
                for acc in set(old) - set(sig):
                    for t in ("pen_rows", "pen_sales", "pen_reports"):
                        c.execute(f"DELETE FROM {t} WHERE account=?", (acc,))
            self.store.set_setting("penalties_sources", json.dumps(sig, ensure_ascii=False))
            self.store.set_setting("penalties_synced_at", datetime.now(MSK).isoformat(timespec="seconds"))
            self.state = {"status": "done", "message": "Готово", "finished_at": time.time()}
        except Exception as exc:
            self.state = {"status": "error", "message": str(exc)[:400], "finished_at": time.time()}
        return dict(self.state)

    def _index_account(self, acc: str, files: list) -> None:
        # Основной файл — с самой поздней датой «по»; более ранние берём только за дни до его начала
        files = sorted(files, key=lambda x: (x[1], -date.fromisoformat(x[0]).toordinal()), reverse=True)
        main_from = files[0][0]
        rows, reports, seen = [], {}, set()
        for i, (d_from, _d_to, path) in enumerate(files):
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            for r in data.get("rows") or []:
                day = str(r.get("rrDate") or "")[:10]
                if i and day >= main_from:
                    continue
                rid = r.get("rrdId")
                if rid in seen:
                    continue
                seen.add(rid)
                rows.append(r)
            for rep in data.get("reports") or []:
                if i and str(rep.get("dateTo") or "")[:10] >= main_from:
                    continue
                reports[rep.get("reportId")] = rep
            del data
        pen, sales = [], defaultdict(lambda: [None, None, None, 0.0, 0.0])
        for r in rows:
            day = str(r.get("rrDate") or "")[:10]
            op = r.get("sellerOperName") or ""
            p, ded = _f(r.get("penalty")), _f(r.get("deduction"))
            base = (r.get("nmId") or 0, r.get("vendorCode") or "", r.get("brandName") or "", r.get("subjectName") or "",
                    r.get("srid") or "", r.get("reportId") or 0)
            if p:
                reason = clean_reason(r.get("bonusTypeName"))
                pen.append((acc, r.get("rrdId"), day, "penalty", reason, classify(reason)[0], p, *base))
            if ded:
                reason = clean_reason(r.get("bonusTypeName"))
                pen.append((acc, -int(r.get("rrdId") or 0), day, "deduction", reason, classify_deduction(reason), ded, *base))
            if op in ("Продажа", "Возврат") and r.get("nmId"):
                sign = 1 if op == "Продажа" else -1
                s = sales[(day, r.get("nmId"))]
                s[0], s[1], s[2] = r.get("vendorCode") or s[0], r.get("brandName") or s[1], r.get("subjectName") or s[2]
                s[3] += sign * _f(r.get("retailAmount"))
                s[4] += sign * (abs(_f(r.get("quantity"))) or 1)
        with self.store._connect() as c:
            for t in ("pen_rows", "pen_sales", "pen_reports"):
                c.execute(f"DELETE FROM {t} WHERE account=?", (acc,))
            c.executemany("INSERT OR REPLACE INTO pen_rows VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", pen)
            c.executemany("INSERT OR REPLACE INTO pen_sales VALUES (?,?,?,?,?,?,?,?)",
                          [(acc, d, nm, a or "", b or "", s or "", round(rev, 2), q) for (d, nm), (a, b, s, rev, q) in sales.items()])
            c.executemany("INSERT OR REPLACE INTO pen_reports VALUES (?,?,?,?,?,?,?)",
                          [(acc, k, str(v.get("dateFrom") or "")[:10], str(v.get("dateTo") or "")[:10], _f(v.get("penaltySum")),
                            _f(v.get("deductionSum")), _f(v.get("retailAmountSum"))) for k, v in reports.items() if k])

    # ------------------------------------------------------------------ выборка
    def _fbs_links(self, srids: set) -> dict:
        if not srids:
            return {}
        out = {}
        with self.store._connect() as c:
            try:
                whs = {(cid, i): n for cid, i, n in c.execute("SELECT cabinet_id, id, name FROM fbs_warehouses")}
                q = list(srids)
                for i in range(0, len(q), 500):
                    part = q[i:i + 500]
                    for cid, oid, rid, created, wh, scan, ss, ws, price in c.execute(
                            f"SELECT cabinet_id, id, rid, created_at, warehouse_id, scan_dt, supplier_status, wb_status, price FROM fbs_orders "
                            f"WHERE rid IN ({','.join('?' * len(part))})", part):
                        hours = None
                        try:
                            a = datetime.fromisoformat(created.replace("Z", "+00:00"))
                            if scan:
                                hours = round((datetime.fromisoformat(scan.replace("Z", "+00:00")) - a).total_seconds() / 3600, 1)
                        except (ValueError, AttributeError):
                            pass
                        out[rid] = {"order_id": oid, "warehouse": whs.get((cid, wh), str(wh or "")), "created_at": created, "scan_dt": scan,
                                    "hours_to_sc": hours, "wb_status": ws or "", "supplier_status": ss or "", "price": price}
            except Exception:
                return {}
        return out

    def _load(self, start: str, end: str, f: dict) -> tuple[list, list]:
        q = (f.get("q") or "").strip().lower()
        with self.store._connect() as c:
            rows = c.execute("SELECT account, rrd_id, rr_date, kind, reason, grp, amount, nm_id, article, brand, subject, srid, report_id "
                             "FROM pen_rows WHERE rr_date>=? AND rr_date<=?", (start, end)).fetchall()
            sales = c.execute("SELECT account, day, nm_id, article, brand, subject, revenue, qty FROM pen_sales WHERE day>=? AND day<=?",
                              (start, end)).fetchall()
        out = []
        for acc, rid, day, kind, reason, grp, amount, nm, art, brand, subject, srid, rep in rows:
            if f.get("cabinet") and acc != f["cabinet"]:
                continue
            if f.get("brand") and brand != f["brand"]:
                continue
            if f.get("category") and subject != f["category"]:
                continue
            if f.get("group") and kind == "penalty" and grp != f["group"]:
                continue
            if f.get("reason") and kind == "penalty" and reason != f["reason"]:
                continue
            if q and q not in f"{art} {nm} {srid} {reason}".lower():
                continue
            out.append({"account": acc, "id": rid, "day": day, "kind": kind, "reason": reason, "group": grp, "amount": round(amount, 2),
                        "nm_id": nm, "article": art, "key": art_key(art) if art else "", "brand": brand, "subject": subject, "srid": srid, "report_id": rep})
        s_out = []
        for acc, day, nm, art, brand, subject, rev, qty in sales:
            if f.get("cabinet") and acc != f["cabinet"]:
                continue
            if f.get("brand") and brand != f["brand"]:
                continue
            if f.get("category") and subject != f["category"]:
                continue
            if q and q not in f"{art} {nm}".lower():
                continue
            s_out.append({"account": acc, "day": day, "nm_id": nm, "article": art, "brand": brand, "subject": subject, "revenue": rev, "qty": qty})
        return out, s_out

    @staticmethod
    def _summary(rows: list, sales: list) -> dict:
        pen = [r for r in rows if r["kind"] == "penalty"]
        ded = [r for r in rows if r["kind"] == "deduction"]
        total = sum(r["amount"] for r in pen)
        revenue = sum(s["revenue"] for s in sales)
        qty = sum(s["qty"] for s in sales)
        groups = defaultdict(lambda: {"sum": 0.0, "count": 0})
        for r in pen:
            groups[r["group"]]["sum"] += r["amount"]
            groups[r["group"]]["count"] += 1
        top = max(groups.items(), key=lambda x: x[1]["sum"])[0] if groups else None
        return {"total": round(total, 2), "count": len(pen), "avg": round(total / len(pen), 2) if pen else None,
                "revenue": round(revenue, 2), "sales_qty": round(qty), "pct_revenue": round(total / revenue * 100, 2) if revenue > 0 else None,
                "per_1000": round(len(pen) / qty * 1000, 2) if qty > 0 else None,
                "articles": len({r["nm_id"] for r in pen if r["nm_id"]}),
                "groups": {k: {"sum": round(v["sum"], 2), "count": v["count"]} for k, v in groups.items()}, "top_group": top,
                "deductions": round(sum(r["amount"] for r in ded), 2), "deductions_count": len(ded)}

    def report(self, start: str, end: str, f: dict, prev: tuple | None = None) -> dict:
        rows, sales = self._load(start, end, f)
        pen = [r for r in rows if r["kind"] == "penalty"]
        prev_rows, prev_sales = self._load(prev[0], prev[1], f) if prev else ([], [])
        prev_pen = [r for r in prev_rows if r["kind"] == "penalty"]
        a, b = date.fromisoformat(start), date.fromisoformat(end)
        days = (b - a).days + 1
        # Динамика: по дням до 45 дней, иначе по неделям (с понедельника)
        weekly = days > 45
        buckets, d = [], a
        while d <= b:
            key = (d - timedelta(days=d.weekday())).isoformat() if weekly else d.isoformat()
            if not buckets or buckets[-1]["key"] != key:
                buckets.append({"key": key, "from": d.isoformat(), "to": d.isoformat(), "groups": defaultdict(float), "count": 0, "revenue": 0.0})
            buckets[-1]["to"] = d.isoformat()
            d += timedelta(days=1)
        idx = {}
        for i, bk in enumerate(buckets):
            x = date.fromisoformat(bk["from"])
            while x <= date.fromisoformat(bk["to"]):
                idx[x.isoformat()] = i
                x += timedelta(days=1)
        for r in pen:
            if r["day"] in idx:
                bk = buckets[idx[r["day"]]]
                bk["groups"][r["group"]] += r["amount"]
                bk["count"] += 1
        for s in sales:
            if s["day"] in idx:
                buckets[idx[s["day"]]]["revenue"] += s["revenue"]
        timeline = [{"key": bk["key"], "from": bk["from"], "to": bk["to"], "groups": {k: round(v, 2) for k, v in bk["groups"].items()},
                     "total": round(sum(bk["groups"].values()), 2), "count": bk["count"],
                     "pct_revenue": round(sum(bk["groups"].values()) / bk["revenue"] * 100, 2) if bk["revenue"] > 0 else None} for bk in buckets]

        # Причины штрафов
        prev_by_reason = defaultdict(float)
        for r in prev_pen:
            prev_by_reason[r["reason"]] += r["amount"]
        links = self._fbs_links({r["srid"] for r in pen if r["srid"]})
        reasons = defaultdict(list)
        for r in pen:
            reasons[r["reason"]].append(r)
        total = sum(r["amount"] for r in pen) or 1
        reason_rows = []
        for reason, rs in reasons.items():
            s = sum(r["amount"] for r in rs)
            arts = defaultdict(lambda: [0.0, 0, ""])
            for r in rs:
                x = arts[r["nm_id"]]
                x[0] += r["amount"]
                x[1] += 1
                x[2] = r["article"]
            top = sorted(arts.items(), key=lambda kv: -kv[1][0])[:5]
            grp, tip = classify(reason)
            reason_rows.append({"reason": reason, "group": rs[0]["group"], "sum": round(s, 2), "count": len(rs), "avg": round(s / len(rs), 2),
                                "share": round(s / total * 100, 1), "prev_sum": round(prev_by_reason.get(reason, 0), 2) if prev else None,
                                "articles": len(arts), "fbs_linked": sum(1 for r in rs if r["srid"] in links),
                                "first": min(r["day"] for r in rs), "last": max(r["day"] for r in rs), "tip": tip,
                                "top_articles": [{"nm_id": nm, "article": v[2], "sum": round(v[0], 2), "count": v[1]} for nm, v in top]})
        reason_rows.sort(key=lambda x: -x["sum"])

        # Артикулы: штрафы против выручки
        art = defaultdict(lambda: {"sum": 0.0, "count": 0, "groups": defaultdict(float), "reasons": defaultdict(float), "revenue": 0.0, "qty": 0.0,
                                   "article": "", "brand": "", "subject": "", "last": ""})
        for r in pen:
            x = art[(r["account"], r["nm_id"])]
            x["sum"] += r["amount"]
            x["count"] += 1
            x["groups"][r["group"]] += r["amount"]
            x["reasons"][r["reason"]] += r["amount"]
            x["article"], x["brand"], x["subject"] = r["article"] or x["article"], r["brand"] or x["brand"], r["subject"] or x["subject"]
            x["last"] = max(x["last"], r["day"])
        for s in sales:
            k = (s["account"], s["nm_id"])
            if k in art:
                art[k]["revenue"] += s["revenue"]
                art[k]["qty"] += s["qty"]
        prev_art = defaultdict(float)
        for r in prev_pen:
            prev_art[(r["account"], r["nm_id"])] += r["amount"]
        articles = []
        for (acc, nm), x in art.items():
            top_reason = max(x["reasons"].items(), key=lambda kv: kv[1])[0] if x["reasons"] else ""
            articles.append({"account": acc, "nm_id": nm, "article": x["article"], "brand": x["brand"], "subject": x["subject"],
                             "sum": round(x["sum"], 2), "count": x["count"], "revenue": round(x["revenue"], 2), "qty": round(x["qty"]),
                             "pct_revenue": round(x["sum"] / x["revenue"] * 100, 2) if x["revenue"] > 0 else None,
                             "groups": {k: round(v, 2) for k, v in x["groups"].items()}, "top_reason": top_reason, "last": x["last"],
                             "prev_sum": round(prev_art.get((acc, nm), 0), 2) if prev else None,
                             "repeat": max(x["reasons"].values()) if x["reasons"] else 0})
        articles.sort(key=lambda x: -x["sum"])

        # Прочие удержания
        ded = [r for r in rows if r["kind"] == "deduction"]
        dcat = defaultdict(lambda: {"sum": 0.0, "count": 0})
        for r in ded:
            dcat[r["group"]]["sum"] += r["amount"]
            dcat[r["group"]]["count"] += 1
        deductions = {"categories": [{"key": k, "title": DED_TITLES.get(k, k), "sum": round(v["sum"], 2), "count": v["count"]}
                                     for k, v in sorted(dcat.items(), key=lambda kv: -kv[1]["sum"])],
                      "items": sorted([{"day": r["day"], "reason": r["reason"], "category": DED_TITLES.get(r["group"], r["group"]), "amount": r["amount"],
                                        "account": r["account"], "article": r["article"], "nm_id": r["nm_id"]} for r in ded],
                                      key=lambda x: (x["day"], x["amount"]), reverse=True)[:300]}

        # Сверка с итогами отчётов WB (отчёты, целиком попавшие в период; только без фильтров по товару)
        with self.store._connect() as c:
            reps = c.execute("SELECT account, report_id, date_from, date_to, penalty_sum FROM pen_reports WHERE date_from>=? AND date_to<=?",
                             (start, end)).fetchall()
            last_day = c.execute("SELECT MAX(rr_date) FROM pen_rows").fetchone()[0]
            last_sale = c.execute("SELECT MAX(day) FROM pen_sales").fetchone()[0]
            accounts = [x[0] for x in c.execute("SELECT DISTINCT account FROM pen_sales UNION SELECT DISTINCT account FROM pen_rows ORDER BY 1")]
            brands = [x[0] for x in c.execute("SELECT DISTINCT brand FROM pen_sales WHERE brand<>'' UNION SELECT DISTINCT brand FROM pen_rows WHERE brand<>'' ORDER BY 1")]
            subjects = [x[0] for x in c.execute("SELECT DISTINCT subject FROM pen_sales WHERE subject<>'' UNION SELECT DISTINCT subject FROM pen_rows WHERE subject<>'' ORDER BY 1")]
            all_reasons = [x[0] for x in c.execute("SELECT DISTINCT reason FROM pen_rows WHERE kind='penalty' ORDER BY 1")]
        reconcile = None
        if not any(f.get(k) for k in ("brand", "category", "q", "group", "reason")):
            reps = [r for r in reps if not f.get("cabinet") or r[0] == f["cabinet"]]
            ids = {(r[0], r[1]) for r in reps}
            ours = sum(r["amount"] for r in pen if (r["account"], r["report_id"]) in ids)
            reconcile = {"reports": len(reps), "wb": round(sum(r[4] for r in reps), 2), "ours": round(ours, 2)}
        return {"days": days, "weekly": weekly, "summary": self._summary(rows, sales),
                "previous": self._summary(prev_rows, prev_sales) if prev else None, "prev_period": list(prev) if prev else None,
                "timeline": timeline, "reasons": reason_rows, "articles": articles, "deductions": deductions, "reconcile": reconcile,
                "groups": [{"key": k, "title": t, "color": c} for k, t, c in GROUPS],
                "options": {"cabinets": accounts, "brands": brands, "categories": subjects, "reasons": all_reasons},
                "last_day": last_day, "last_sale": last_sale, "state": dict(self.state), "stale": self.stale(),
                "synced_at": self.store.get_setting("penalties_synced_at", "")}

    def feed(self, start: str, end: str, f: dict, sort: str = "day", desc: bool = True, limit: int = 100, offset: int = 0) -> dict:
        rows, _ = self._load(start, end, f)
        pen = [r for r in rows if r["kind"] == "penalty"]
        keys = {"day": lambda r: (r["day"], r["amount"]), "amount": lambda r: r["amount"], "reason": lambda r: r["reason"],
                "article": lambda r: r["article"]}
        pen.sort(key=keys.get(sort, keys["day"]), reverse=desc)
        page = pen[offset:offset + limit]
        links = self._fbs_links({r["srid"] for r in page if r["srid"]})
        for r in page:
            r["fbs"] = links.get(r["srid"])
            r["group_title"] = GROUP_TITLES.get(r["group"], r["group"])
        return {"total": len(pen), "sum": round(sum(r["amount"] for r in pen), 2), "items": page}
