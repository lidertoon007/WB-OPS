"""Аналитика отзывов, вопросов и чатов: дашборд, детальный разбор, теги, тренды, отчёты по товарам.

Источник — локальная база WB OPS (входящие, черновики, журнал публикаций). Теги отзывов считаются
тем же разбором, что и индивидуальные ответы (review_composer.analyze), поэтому «что хвалят» и
«на что жалуются» в аналитике совпадают с тем, на что реагируют ответы.
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

from app.models import InboxItem
from app.services.review_composer import PHRASE_TITLES, analyze

MARKETPLACES = {"wildberries": "Wildberries", "ozon": "Ozon", "yandex": "Яндекс Маркет"}
METHODS = {"ai": "ИИ", "individual": "Сборщик", "templates": "Шаблоны", "knowledge": "Прошлый ответ", "manual": "Вручную"}
# Тег: (название, аспекты «хвалят», аспекты «жалуются»)
TAGS = {
    "quality": ("Качество", {"quality_good"}, {"defect"}),
    "size": ("Размер и посадка", {"fit_good", "size_tip"}, {"size_small", "size_big", "size_wrong"}),
    "length": ("Длина", {"length_ok"}, {"length_short", "length_long_bad"}),
    "material": ("Ткань", {"stretch", "warm", "light", "wash_ok"}, {"thin"}),
    "color": ("Цвет", {"color_good"}, {"color_diff"}),
    "look": ("Внешний вид", {"looks_good"}, set()),
    "match": ("Соответствие описанию", set(), {"not_as_described"}),
    "price": ("Цена", {"price_good"}, {"price_bad"}),
    "delivery": ("Доставка", {"delivery_fast"}, {"delivery_issue"}),
    "smell": ("Запах", set(), {"smell"}),
}
ADVICE = {
    "size_small": "Многие пишут, что модель маломерит: добавьте в карточку совет брать на размер больше и проверьте таблицу замеров.",
    "size_big": "Покупатели отмечают, что модель большемерит: уточните таблицу замеров и добавьте подсказку по выбору размера.",
    "size_wrong": "Частые отказы из-за размера: проверьте таблицу замеров в карточке и добавьте фото с замерами.",
    "length_short": "Жалобы на длину: укажите длину по внутреннему шву и рост, на который рассчитана модель.",
    "length_long_bad": "Модель кажется длинной части покупателей: укажите длину по внутреннему шву в карточке.",
    "defect": "Есть жалобы на брак: усилите контроль качества партии перед отгрузкой.",
    "not_as_described": "Жалобы на несоответствие описанию: сверьте фото, цвет и состав в карточке с реальным товаром и проверьте отгрузку.",
    "thin": "Ткань кажется тонкой: укажите плотность и сезон модели в карточке.",
    "color_diff": "Цвет на фото отличается от реального: обновите фото при дневном свете и уточните оттенок в описании.",
    "price_bad": "Покупатели считают цену завышенной: проверьте цену относительно похожих моделей и участие в акциях.",
    "delivery_issue": "Есть вопросы по доставке и выдаче — их решает Wildberries, но стоит следить за упаковкой и сроками отгрузки.",
    "smell": "Есть жалобы на запах: проверьте хранение и упаковку партии.",
}
RATING_KEYS = ["5", "4", "3", "2", "1"]


def parse_day(value) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None


def period_key(day: date, grain: str) -> str:
    if grain == "month":
        return day.strftime("%Y-%m")
    if grain == "week":
        return (day - timedelta(days=day.weekday())).isoformat()
    return day.isoformat()


def period_keys(start: date, end: date, grain: str) -> list[str]:
    keys, day = [], start
    while day <= end:
        key = period_key(day, grain)
        if not keys or keys[-1] != key:
            keys.append(key)
        day += timedelta(days=1)
    return keys


def sentiment_of(row) -> str:
    rating = row.get("rating")
    if rating is not None:
        return "positive" if rating >= 4 else ("negative" if rating <= 2 else "neutral")
    return row.get("review_sentiment") or "neutral"


class Analytics:
    def __init__(self, store, chats=None):
        self.store = store
        self.chats = chats
        with self.store._connect() as c:
            c.execute("CREATE TABLE IF NOT EXISTS analytics_reports (id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER, "
                      "title TEXT NOT NULL, date_from TEXT, date_to TEXT, created_at TEXT NOT NULL, data TEXT NOT NULL)")

    # ------------------------------------------------------------------ данные
    def rows(self, item_type: str = "review") -> list[dict]:
        with self.store._connect() as c:
            c.row_factory = sqlite3.Row
            items = c.execute(
                "SELECT i.*, d.text AS draft_text, COALESCE(d.published,0) AS published, COALESCE(d.confirmed,0) AS confirmed, "
                "COALESCE(d.user_edited,0) AS user_edited, COALESCE(d.source,'') AS source, d.blocked_reason "
                "FROM inbox_items i LEFT JOIN reply_drafts d ON d.item_id=i.id WHERE i.item_type=?", (item_type,)).fetchall()
            history = c.execute(
                "SELECT h.*, NULL AS draft_text, h.answered_in_wb AS published, 0 AS confirmed, "
                "0 AS user_edited, '' AS source, NULL AS blocked_reason "
                "FROM analytics_history h WHERE h.item_type=? "
                "AND NOT EXISTS (SELECT 1 FROM inbox_items i WHERE i.id=h.id)", (item_type,)).fetchall()
            items = list(items) + list(history)
            published = dict(c.execute("SELECT item_id, MIN(created_at) FROM audit_log WHERE event='published' GROUP BY item_id").fetchall())
            edited = {r[0] for r in c.execute("SELECT DISTINCT item_id FROM draft_history WHERE action='edited'")}
            generated = dict(c.execute("SELECT item_id, text FROM draft_history WHERE id IN "
                                       "(SELECT MAX(id) FROM draft_history WHERE action='generated' GROUP BY item_id)").fetchall())
        out = []
        for r in items:
            row = dict(r)
            row["day"] = parse_day(row["created_at"])
            row["published_at"] = published.get(row["id"])
            row["published"] = bool(row["published"])
            row["marketplace"] = row.get("marketplace") or "wildberries"
            row["has_text"] = bool((row.get("text") or "").strip())
            if row["published"] and "answered_in_wb" in row:
                row["method"] = None
            elif row["published"]:
                last = generated.get(row["id"])
                if last is not None:      # отправлен ли текст в том виде, как его подготовила система
                    changed = " ".join(last.split()) != " ".join(str(row.get("draft_text") or "").split())
                else:
                    changed = bool(row["user_edited"]) or row["id"] in edited
                if changed:
                    row["method"] = "manual"
                else:
                    row["method"] = row["source"] or "templates"
            else:
                row["method"] = None
            out.append(row)
        return out

    def chat_rows(self) -> list[dict]:
        out = []
        for chat in (self.chats.all() if self.chats else []):
            day = parse_day(chat.get("created_at") or (chat.get("messages") or [{}])[0].get("created_at"))
            out.append({"id": chat.get("id"), "day": day, "marketplace": chat.get("marketplace") or "wildberries",
                        "cabinet_id": chat.get("cabinet_id") or "", "published": chat.get("status") in ("answered", "closed"),
                        "product_id": chat.get("product_id"), "product_name": chat.get("product_name"), "rating": None,
                        "text": " ".join(str(m.get("text", "")) for m in chat.get("messages", [])), "has_text": True,
                        "method": "manual" if chat.get("status") in ("answered", "closed") else None})
        return out

    def filtered(self, kind: str, date_from: date, date_to: date, cabinet: str | None = None, marketplace: str | None = None,
                 product_id: int | None = None) -> list[dict]:
        rows = self.chat_rows() if kind == "chat" else self.rows("question" if kind == "question" else "review")
        return [r for r in rows if r["day"] and date_from <= r["day"] <= date_to
                and (not cabinet or r.get("cabinet_id") == cabinet)
                and (not marketplace or r.get("marketplace") == marketplace)
                and (product_id is None or r.get("product_id") == product_id)]

    def aspects(self, row) -> set[str]:
        if not row.get("_aspects_done"):
            item = InboxItem(id=row["id"], cabinet_id=row.get("cabinet_id") or "", item_type="review", text=row.get("text") or "",
                             rating=row.get("rating"), order_status=row.get("order_status") or "notSpecified",
                             author_name=row.get("author_name"), product_name=row.get("product_name"))
            row["_aspects"] = analyze(item).aspects
            row["_aspects_done"] = True
        return row["_aspects"]

    def tags_of(self, row) -> dict[str, str]:
        """{тег: positive|negative} для отзыва."""
        found, a = {}, self.aspects(row)
        for key, (_, pos, neg) in TAGS.items():
            if a & neg:
                found[key] = "negative"
            elif a & pos:
                found[key] = "positive"
        return found

    # ------------------------------------------------------------------ дашборд
    def dashboard(self, kind, date_from, date_to, cabinet=None, marketplace=None, grain="day") -> dict:
        rows = self.filtered(kind, date_from, date_to, cabinet, marketplace)
        span = (date_to - date_from).days + 1
        prev = self.filtered(kind, date_from - timedelta(days=span), date_from - timedelta(days=1), cabinet, marketplace)
        all_rows = self.chat_rows() if kind == "chat" else self.rows("question" if kind == "question" else "review")
        # WB feedback/question status in local history can become stale after a reply in WB.
        # The API layer supplies the live count; chats still use their local status.
        waiting = [r for r in all_rows if not r["published"] and (not cabinet or r.get("cabinet_id") == cabinet)
                   and (not marketplace or r.get("marketplace") == marketplace)] if kind == "chat" else None
        answered = [r for r in rows if r["published"]]
        minutes = []
        for r in answered:
            if r.get("published_at"):
                try:
                    created = datetime.fromisoformat(str(r["created_at"]).replace("Z", "+00:00"))
                    done = datetime.fromisoformat(str(r["published_at"]).replace("Z", "+00:00"))
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    minutes.append(max(0.0, (done - created).total_seconds() / 60))
                except ValueError:
                    pass
        minutes.sort()
        keys = period_keys(date_from, date_to, grain)
        series = {mp: {k: 0 for k in keys} for mp in MARKETPLACES}
        ratings = {k: {r: 0 for r in RATING_KEYS} for k in keys}
        for r in rows:
            k = period_key(r["day"], grain)
            series.setdefault(r["marketplace"], {kk: 0 for kk in keys})[k] += 1
            if r.get("rating"):
                ratings[k][str(r["rating"])] += 1
        cabinets = defaultdict(lambda: {"total": 0, "answered": 0, "marketplace": ""})
        for r in rows:
            cab = cabinets[r.get("cabinet_id") or "—"]
            cab["total"] += 1
            cab["answered"] += int(r["published"])
            cab["marketplace"] = MARKETPLACES.get(r["marketplace"], r["marketplace"])
        total = len(rows) or 1
        cabinet_table = [{"cabinet": k, "marketplace": v["marketplace"], "total": v["total"], "share": v["total"] / total,
                          "answered": v["answered"], "unanswered": v["total"] - v["answered"],
                          "answered_share": v["answered"] / v["total"] if v["total"] else 0} for k, v in sorted(cabinets.items(), key=lambda kv: -kv[1]["total"])]
        rating_counts = Counter(str(r["rating"]) for r in rows if r.get("rating"))
        with_text = sum(1 for r in rows if r["has_text"])
        return {
            "kind": kind, "keys": keys, "grain": grain,
            "kpi": {"new": len(rows), "prev": len(prev), "change": (len(rows) - len(prev)) / len(prev) if prev else None,
                    "waiting": len(waiting) if waiting is not None else None, "answered": len(answered),
                    "response_minutes": minutes[len(minutes) // 2] if minutes else None},
            "series": {MARKETPLACES.get(k, k): v for k, v in series.items() if sum(v.values()) or k == "wildberries"},
            "ratings_series": ratings,
            "ratings": {k: rating_counts.get(k, 0) for k in RATING_KEYS},
            "avg_rating": (sum(int(k) * v for k, v in rating_counts.items()) / sum(rating_counts.values())) if rating_counts else None,
            "text": {"with_text": with_text, "without_text": len(rows) - with_text},
            "cabinets": cabinet_table,
            "marketplaces": {MARKETPLACES.get(k, k): sum(v.values()) for k, v in series.items() if sum(v.values())},
        }

    # ------------------------------------------------------------------ детально
    def detailed(self, kind, date_from, date_to, cabinet=None, marketplace=None, grain="week") -> dict:
        rows = self.filtered(kind, date_from, date_to, cabinet, marketplace)
        keys = period_keys(date_from, date_to, grain)
        methods = {k: {m: 0 for m in METHODS} for k in keys}
        sentiment = {k: {"positive": 0, "neutral": 0, "negative": 0} for k in keys}
        tags_series = {k: Counter() for k in keys}
        tag_total, method_total = Counter(), Counter()
        answers_text, answers_no_text = Counter(), Counter()
        photo = video = 0
        for r in rows:
            k = period_key(r["day"], grain)
            if r.get("method"):
                methods[k][r["method"]] += 1
                method_total[r["method"]] += 1
                (answers_text if r["has_text"] else answers_no_text)[r["method"]] += 1
            if kind == "review":
                sentiment[k][sentiment_of(r)] += 1
                for tag in self.tags_of(r):
                    tags_series[k][tag] += 1
                    tag_total[tag] += 1
                photo += int(bool(r.get("has_photo")))
                video += int(bool(r.get("has_video")))
        sent_total = Counter(sentiment_of(r) for r in rows) if kind == "review" else Counter()
        top_tags = [t for t, _ in tag_total.most_common(6)]
        return {
            "keys": keys, "grain": grain, "total": len(rows),
            "methods": {"titles": METHODS, "series": methods, "total": dict(method_total),
                        "with_text": dict(answers_text), "without_text": dict(answers_no_text)},
            "sentiment": {"series": sentiment, "total": dict(sent_total)},
            "media": {"photo": photo, "video": video, "total": len(rows)},
            "tags": {"titles": {k: v[0] for k, v in TAGS.items()}, "top": top_tags,
                     "series": {k: {t: v.get(t, 0) for t in top_tags} for k, v in tags_series.items()},
                     "popularity": [{"tag": t, "title": TAGS[t][0], "count": c} for t, c in tag_total.most_common()]},
        }

    # ------------------------------------------------------------------ теги
    def tags(self, date_from, date_to, cabinet=None, marketplace=None, product_id=None) -> dict:
        rows = self.filtered("review", date_from, date_to, cabinet, marketplace, product_id)
        table = {k: {"positive": Counter(), "negative": Counter()} for k in TAGS}
        for r in rows:
            for tag, polarity in self.tags_of(r).items():
                table[tag][polarity][MARKETPLACES.get(r["marketplace"], r["marketplace"])] += 1
        mps = sorted({MARKETPLACES.get(r["marketplace"], r["marketplace"]) for r in rows}) or ["Wildberries"]
        out = []
        for key, (title, _, _) in TAGS.items():
            pos, neg = table[key]["positive"], table[key]["negative"]
            p, n = sum(pos.values()), sum(neg.values())
            out.append({"tag": key, "title": title, "positive": dict(pos), "negative": dict(neg), "pos_total": p, "neg_total": n,
                        "pos_share": p / (p + n) if p + n else None})
        return {"marketplaces": mps, "tags": out, "total": len(rows)}

    # ------------------------------------------------------------------ тренды
    def trends(self, date_from, date_to, cabinet=None, marketplace=None, limit=6) -> dict:
        span = (date_to - date_from).days + 1
        now = self.filtered("review", date_from, date_to, cabinet, marketplace)
        before = self.filtered("review", date_from - timedelta(days=span), date_from - timedelta(days=1), cabinet, marketplace)

        def count(rows, polarity):
            c, names = Counter(), {}
            for r in rows:
                if r.get("product_id") and sentiment_of(r) == polarity:
                    c[r["product_id"]] += 1
                    names[r["product_id"]] = r.get("product_name") or ""
            return c, names

        result, all_names = {}, self.product_names()
        for polarity in ("positive", "negative"):
            cur, names = count(now, polarity)
            old, old_names = count(before, polarity)
            items = []
            for pid, n in cur.items():
                was = old.get(pid, 0)
                change = (n - was) / was if was else None
                if n > was:
                    items.append({"product_id": pid, "name": names.get(pid) or old_names.get(pid) or all_names.get(pid, ""), "count": n, "previous": was,
                                  "change": change})
            items.sort(key=lambda x: (-(x["count"] - x["previous"]), -x["count"]))
            result[polarity] = items[:limit]
        return {"growth_positive": result["positive"], "growth_negative": result["negative"]}

    # ------------------------------------------------------------------ товары
    def product_names(self) -> dict[int, str]:
        with self.store._connect() as c:
            rows = c.execute("SELECT product_id, product_name FROM analytics_history WHERE product_id IS NOT NULL AND COALESCE(product_name,'')<>'' "
                             "UNION ALL SELECT product_id, product_name FROM inbox_items WHERE product_id IS NOT NULL AND COALESCE(product_name,'')<>''").fetchall()
        return {pid: name for pid, name in rows}

    def products(self, date_from, date_to) -> list[dict]:
        stats = defaultdict(lambda: {"reviews": 0, "questions": 0, "name": "", "negative": 0, "rating_sum": 0, "rated": 0})
        for kind in ("review", "question"):
            for r in self.filtered(kind, date_from, date_to):
                if not r.get("product_id"):
                    continue
                s = stats[r["product_id"]]
                s["name"] = s["name"] or r.get("product_name") or ""
                s["reviews" if kind == "review" else "questions"] += 1
                if kind == "review" and r.get("rating"):
                    s["rating_sum"] += r["rating"]
                    s["rated"] += 1
                    s["negative"] += int(r["rating"] <= 2)
        names = self.product_names()
        out = [{"product_id": pid, "name": s["name"] or names.get(pid, ""), "reviews": s["reviews"], "questions": s["questions"], "negative": s["negative"],
                "avg_rating": s["rating_sum"] / s["rated"] if s["rated"] else None} for pid, s in stats.items()]
        return sorted(out, key=lambda x: -(x["reviews"] + x["questions"]))

    def product(self, product_id: int, date_from, date_to) -> dict:
        reviews = self.filtered("review", date_from, date_to, product_id=product_id)
        questions = self.filtered("question", date_from, date_to, product_id=product_id)
        name = next((r.get("product_name") for r in reviews + questions if r.get("product_name")), "") or self.product_names().get(product_id, "")
        praise, complain = Counter(), Counter()
        quotes = defaultdict(list)
        neg_aspects = Counter()
        for r in reviews:
            a = self.aspects(r)
            for tag, polarity in self.tags_of(r).items():
                (praise if polarity == "positive" else complain)[tag] += 1
                if r["has_text"] and len(quotes[(tag, polarity)]) < 3:
                    quotes[(tag, polarity)].append((r.get("text") or "")[:180])
            for key in a:
                if key in ADVICE:
                    neg_aspects[key] += 1
        months = period_keys(date_from, date_to, "month")
        monthly = {m: {k: 0 for k in RATING_KEYS} for m in months}
        sentiment = {m: {"positive": 0, "neutral": 0, "negative": 0} for m in months}
        methods = {m: {k: 0 for k in METHODS} for m in months}
        for r in reviews:
            m = period_key(r["day"], "month")
            if r.get("rating"):
                monthly[m][str(r["rating"])] += 1
            sentiment[m][sentiment_of(r)] += 1
            if r.get("method"):
                methods[m][r["method"]] += 1
        # Совет — только по повторяющимся жалобам (≥2 упоминаний или ≥15% отзывов), иначе один главный
        frequent = [k for k, n in neg_aspects.most_common() if n >= 2 or n >= 0.15 * max(1, len(reviews))]
        if not frequent and neg_aspects:
            frequent = [neg_aspects.most_common(1)[0][0]]
        if "size_small" in frequent and "size_big" in frequent:      # противоречат друг другу — оставляем более частый
            frequent.remove("size_big" if neg_aspects["size_small"] >= neg_aspects["size_big"] else "size_small")
        advice = [ADVICE[k] for k in frequent[:4]]
        if not advice and reviews:
            advice = ["Заметных проблем в отзывах нет — поддерживайте качество и отвечайте на отзывы, особенно с фото."]
        with_text = sum(1 for r in reviews if r["has_text"])
        with_tags = sum(1 for r in reviews if self.tags_of(r))
        return {
            "product_id": product_id, "name": name, "date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
            "stats": {"reviews": len(reviews), "with_text": with_text, "with_tags": with_tags, "questions": len(questions),
                      "photo": sum(1 for r in reviews if r.get("has_photo")),
                      "avg_rating": (sum(r["rating"] for r in reviews if r.get("rating")) / max(1, sum(1 for r in reviews if r.get("rating")))) if reviews else None},
            "pros": [{"tag": t, "title": TAGS[t][0], "count": c, "quotes": quotes[(t, "positive")]} for t, c in praise.most_common()],
            "cons": [{"tag": t, "title": TAGS[t][0], "count": c, "quotes": quotes[(t, "negative")]} for t, c in complain.most_common()],
            "advice": advice,
            "charts": {"months": months, "ratings": monthly, "sentiment": sentiment, "methods": methods, "method_titles": METHODS},
            "ratings": {k: sum(1 for r in reviews if str(r.get("rating")) == k) for k in RATING_KEYS},
            "ai": None,
        }

    def review_texts(self, product_id, date_from, date_to, limit=150) -> list[str]:
        rows = [r for r in self.filtered("review", date_from, date_to, product_id=product_id) if r["has_text"]]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return [f"{r.get('rating') or '—'}★: {(r.get('text') or '').strip()[:400]}" for r in rows[:limit]]

    # ------------------------------------------------------------------ отчёты
    def save_report(self, data: dict) -> dict:
        title = f"{data.get('name') or 'Товар'} · {data['product_id']}"
        with self.store._connect() as c:
            cur = c.execute("INSERT INTO analytics_reports(product_id,title,date_from,date_to,created_at,data) VALUES(?,?,?,?,?,?)",
                            (data["product_id"], title, data["date_from"], data["date_to"], datetime.now(timezone.utc).isoformat(),
                             json.dumps(data, ensure_ascii=False)))
            report_id = cur.lastrowid
        return {"id": report_id, "title": title}

    def list_reports(self, search: str = "") -> list[dict]:
        with self.store._connect() as c:
            rows = c.execute("SELECT id,product_id,title,date_from,date_to,created_at FROM analytics_reports ORDER BY id DESC").fetchall()
        term = search.strip().lower()
        out = [{"id": r[0], "product_id": r[1], "title": r[2], "date_from": r[3], "date_to": r[4], "created_at": r[5]} for r in rows]
        return [r for r in out if not term or term in r["title"].lower() or term in str(r["product_id"])]

    def get_report(self, report_id: int) -> dict | None:
        with self.store._connect() as c:
            row = c.execute("SELECT data FROM analytics_reports WHERE id=?", (report_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def delete_report(self, report_id: int) -> bool:
        with self.store._connect() as c:
            return c.execute("DELETE FROM analytics_reports WHERE id=?", (report_id,)).rowcount > 0


# ---------------------------------------------------------------------- экспорт
def report_tables(data: dict) -> dict[str, list[list]]:
    """Таблицы для выгрузки отчёта по товару (CSV/Excel)."""
    s = data["stats"]
    summary = [["Показатель", "Значение"], ["Товар", data.get("name") or ""], ["WB ID", data["product_id"]],
               ["Период", f"{data['date_from']} — {data['date_to']}"], ["Отзывов", s["reviews"]], ["С текстом", s["with_text"]],
               ["С разметкой", s["with_tags"]], ["С фото", s["photo"]], ["Вопросов", s["questions"]],
               ["Средняя оценка", round(s["avg_rating"], 2) if s.get("avg_rating") else ""]]
    summary += [[f"Оценка {k}★", v] for k, v in data["ratings"].items()]
    pros = [["Чаще хвалят", "Упоминаний", "Примеры"]] + [[p["title"], p["count"], " | ".join(p["quotes"])] for p in data["pros"]]
    cons = [["Чаще недовольны", "Упоминаний", "Примеры"]] + [[p["title"], p["count"], " | ".join(p["quotes"])] for p in data["cons"]]
    advice = [["Совет"]] + [[a] for a in data["advice"]]
    if data.get("ai"):
        ai = data["ai"]
        advice += [["ИИ: преимущества"]] + [[x] for x in ai.get("pros", [])] + [["ИИ: недостатки"]] + [[x] for x in ai.get("cons", [])] \
            + [["ИИ: рекомендации"]] + [[x] for x in ai.get("advice", [])]
    return {"Сводка": summary, "Хвалят": pros, "Недовольны": cons, "Советы": advice}


def to_csv(tables: dict[str, list[list]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    for name, rows in tables.items():
        writer.writerow([name])
        writer.writerows(rows)
        writer.writerow([])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def to_xlsx(tables: dict[str, list[list]]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in tables.items():
        ws = wb.create_sheet(name[:31])
        for row in rows:
            ws.append(row)
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for col in ws.columns:
            width = max(len(str(c.value or "")) for c in col)
            ws.column_dimensions[col[0].column_letter].width = min(70, max(10, width + 2))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def dashboard_tables(d: dict) -> dict[str, list[list]]:
    k = d["kpi"]
    summary = [["Показатель", "Значение"], ["Новых за период", k["new"]], ["За прошлый период", k["prev"]],
               ["Ожидают ответа", k["waiting"] if k["waiting"] is not None else "Нет актуальных данных"], ["Отвечено", k["answered"]],
               ["Медианное время ответа, мин", round(k["response_minutes"]) if k["response_minutes"] is not None else ""],
               ["Средняя оценка", round(d["avg_rating"], 2) if d.get("avg_rating") else ""],
               ["С текстом", d["text"]["with_text"]], ["Без текста", d["text"]["without_text"]]]
    dynamics = [["Период", *d["series"].keys()]] + [[key, *[s[key] for s in d["series"].values()]] for key in d["keys"]]
    cabinets = [["Кабинет", "Маркетплейс", "Всего", "Доля", "Отвечено", "Без ответа", "Отвечено, %"]] + [
        [c["cabinet"], c["marketplace"], c["total"], round(c["share"] * 100, 1), c["answered"], c["unanswered"], round(c["answered_share"] * 100, 1)]
        for c in d["cabinets"]]
    ratings = [["Оценка", "Количество"]] + [[f"{k}★", v] for k, v in d["ratings"].items()]
    return {"Сводка": summary, "Динамика": dynamics, "Кабинеты": cabinets, "Оценки": ratings}
