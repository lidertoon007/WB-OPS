"""Список «Все отзывы»: фильтры, теги, статус ответа, способ ответа, выгрузка.

Теги считаются тем же разбором, что и в аналитике (review_composer.analyze → analytics.TAGS),
поэтому фильтр «Тег» и отчёты в аналитике совпадают.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.models import InboxItem
from app.services.analytics import METHODS, TAGS
from app.services.review_composer import analyze

TAG_ICONS = {"quality": "🏅", "size": "📏", "length": "📐", "material": "🧵", "color": "🎨", "look": "✨",
             "match": "🔍", "price": "💰", "delivery": "🚚", "smell": "👃"}
ANSWER_STATUSES = {
    "unanswered": "Без ответа",       # актуальная очередь WB после синхронизации
    "nodraft": "Нет черновика",
    "draft": "Черновик",
    "approved": "Одобрен",
    "rework": "На доработке",
    "error": "Ошибка отправки",
    "published": "Отвечен",
}
ORDER_STATUSES = {"buyout": "Выкуп", "rejected": "Отказ", "returned": "Возврат", "notSpecified": "Не указан"}
METHOD_LABELS = {"ai": "Ответ ИИ", "individual": "Сборщик", "templates": "Шаблон", "knowledge": "Прошлый ответ", "manual": "Вручную"}


def answer_status(row) -> str:
    if row["published"]:
        return "published"
    if row["delivery"] in ("uncertain", "sending"):
        return "error"
    if row["draft_text"] is None:
        return "nodraft"
    if row["moderation_status"] in ("rework", "rejected"):
        return "rework"
    if row["moderation_status"] == "approved" or row["confirmed"]:
        return "approved"
    return "draft"


class ReviewList:
    def __init__(self, store, cabinet_names=lambda: {}, product_names=lambda: {}, vendor_path="app/knowledge/nm_vendor.json"):
        self.store = store
        self.cabinet_names = cabinet_names
        self.product_names = product_names
        self.vendor_path = Path(vendor_path)
        self._tags_cache: dict[tuple, dict] = {}
        self._vendor: dict[str, str] | None = None

    def vendor_codes(self) -> dict[str, str]:
        if self._vendor is None:
            try:
                data = json.loads(self.vendor_path.read_text(encoding="utf-8"))
                self._vendor = {str(k): str(v).strip() for k, v in (data.get("links", data) if isinstance(data, dict) else {}).items()}
            except (OSError, ValueError):
                self._vendor = {}
        return self._vendor

    def tags_of(self, row) -> dict[str, str]:
        key = (row["id"], row["text"], row["rating"], row["order_status"])
        if key not in self._tags_cache:
            item = InboxItem(id=row["id"], cabinet_id=row["cabinet_id"] or "", item_type="review", text=row["text"] or "",
                             rating=row["rating"], order_status=row["order_status"] or "notSpecified", author_name=row["author_name"])
            aspects = analyze(item).aspects
            found = {}
            for tag, (_, pos, neg) in TAGS.items():
                if aspects & neg:
                    found[tag] = "negative"
                elif aspects & pos:
                    found[tag] = "positive"
            self._tags_cache[key] = found
            if len(self._tags_cache) > 20000:
                self._tags_cache.clear()
        return self._tags_cache[key]

    def _rows(self, item_type: str) -> list[dict]:
        with self.store._connect() as c:
            c.row_factory = sqlite3.Row
            rows = [dict(r) for r in c.execute(
                "SELECT i.id, i.cabinet_id, i.text, i.rating, i.product_id, i.created_at, i.order_status, i.author_name, i.marketplace, "
                "i.product_name, i.has_photo, i.has_video, i.wb_unanswered, d.text AS draft_text, COALESCE(d.confirmed,0) AS confirmed, "
                "COALESCE(d.published,0) AS published, COALESCE(d.moderation_status,'pending') AS moderation_status, "
                "COALESCE(d.moderation_comment,'') AS moderation_comment, COALESCE(d.user_edited,0) AS user_edited, COALESCE(d.source,'') AS source, "
                "s.value AS delivery FROM inbox_items i LEFT JOIN reply_drafts d ON d.item_id=i.id "
                "LEFT JOIN app_settings s ON s.key='reply_delivery:' || i.id WHERE i.item_type=?", (item_type,))]
            published = {r[0]: (r[1], r[2]) for r in c.execute(
                "SELECT item_id, created_at, actor FROM audit_log WHERE id IN (SELECT MIN(id) FROM audit_log WHERE event='published' GROUP BY item_id)")}
            generated = dict(c.execute("SELECT item_id, text FROM draft_history WHERE id IN "
                                       "(SELECT MAX(id) FROM draft_history WHERE action='generated' GROUP BY item_id)").fetchall())
        for r in rows:
            r["published"] = bool(r["published"])
            r["status"] = answer_status(r)
            r["published_at"], r["published_by"] = published.get(r["id"], (None, None))
            last = generated.get(r["id"])
            edited = (" ".join(last.split()) != " ".join((r["draft_text"] or "").split())) if last is not None else bool(r["user_edited"] and r["published"])
            r["method"] = "manual" if (edited and r["draft_text"] is not None) else (r["source"] or ("templates" if r["draft_text"] is not None else None))
        return rows

    def query(self, *, item_type="review", search="", date_from=None, date_to=None, marketplace=None, cabinet_id=None,
              order_status=None, answer=None, ratings=None, tags=None, product_id=None, sort="desc", limit=20, offset=0,
              with_warnings=None) -> dict:
        rows = self._rows(item_type)
        names = self.product_names()
        vendor = self.vendor_codes()
        term = (search or "").strip().lower().replace("ё", "е")
        wanted_tags = [t for t in (tags or []) if t]
        base = []
        for r in rows:
            day = (r["created_at"] or "")[:10]
            if date_from and day < date_from or date_to and day > date_to:
                continue
            if marketplace and (r["marketplace"] or "wildberries") != marketplace:
                continue
            if cabinet_id and r["cabinet_id"] != cabinet_id:
                continue
            if order_status and r["order_status"] != order_status:
                continue
            if ratings and r["rating"] not in ratings:
                continue
            if product_id and r["product_id"] != product_id:
                continue
            r["product_name"] = r["product_name"] or names.get(r["product_id"], "")
            r["vendor_code"] = vendor.get(str(r["product_id"]), "")
            if term:
                hay = " ".join(str(x or "") for x in (r["text"], r["author_name"], r["product_name"], r["product_id"], r["vendor_code"], r["draft_text"], r["id"])).lower().replace("ё", "е")
                if term not in hay:
                    continue
            if wanted_tags and item_type == "review":
                found = self.tags_of(r)
                ok = True
                for want in wanted_tags:
                    tag, _, polarity = want.partition(":")
                    if tag not in found or (polarity in ("positive", "negative") and found[tag] != polarity):
                        ok = False
                        break
                if not ok:
                    continue
            base.append(r)
        counts = {k: 0 for k in ANSWER_STATUSES}
        for r in base:
            counts[r["status"]] += 1
            if r["status"] != "published" and r["wb_unanswered"] != 0:
                counts["unanswered"] += 1
        if answer:
            base = [r for r in base if (r["status"] != "published" and r["wb_unanswered"] != 0 if answer == "unanswered" else r["status"] == answer)]
        base.sort(key=lambda r: r["created_at"] or "", reverse=sort != "asc")
        page = base[max(0, offset):max(0, offset) + max(1, min(limit, 200))]
        cabinets = self.cabinet_names()
        items = []
        for r in page:
            tag_map = self.tags_of(r) if item_type == "review" else {}
            warnings = []
            if with_warnings and r["draft_text"] and not r["published"]:
                item = self.store.get_inbox(r["id"])
                warnings = with_warnings(r["draft_text"], item) if item else []
            items.append({
                "id": r["id"], "created_at": r["created_at"], "cabinet_id": r["cabinet_id"], "cabinet_name": cabinets.get(r["cabinet_id"], r["cabinet_id"]),
                "marketplace": r["marketplace"] or "wildberries", "product_id": r["product_id"], "product_name": r["product_name"],
                "vendor_code": r["vendor_code"], "rating": r["rating"], "author_name": r["author_name"] or "", "text": r["text"] or "",
                "order_status": r["order_status"], "order_status_title": ORDER_STATUSES.get(r["order_status"], r["order_status"]),
                "has_photo": bool(r["has_photo"]), "has_video": bool(r["has_video"]),
                "tags": [{"tag": t, "title": TAGS[t][0], "icon": TAG_ICONS.get(t, "•"), "polarity": p} for t, p in tag_map.items()],
                "answer": r["draft_text"] or "", "status": r["status"], "status_title": ANSWER_STATUSES[r["status"]],
                "method": r["method"], "method_title": METHOD_LABELS.get(r["method"], "") if r["method"] else "",
                "moderation_comment": r["moderation_comment"], "published_at": r["published_at"], "published_by": r["published_by"] or "",
                "warnings": warnings,
            })
        return {"items": items, "total": len(base), "counts": counts, "offset": offset, "limit": limit}

    def export_rows(self, **filters) -> list[list]:
        data = self.query(**{**filters, "limit": 200000, "offset": 0})
        out = [["Дата", "Маркетплейс", "Кабинет", "WB ID", "Артикул", "Товар", "Оценка", "Покупатель", "Отзыв", "Теги",
                "Статус заказа", "Статус ответа", "Способ ответа", "Ответ", "Отправлен", "Кто отправил"]]
        for i in data["items"]:
            out.append([(i["created_at"] or "")[:16].replace("T", " "), i["marketplace"], i["cabinet_name"], i["product_id"], i["vendor_code"],
                        i["product_name"], i["rating"], i["author_name"], i["text"],
                        ", ".join(("+" if t["polarity"] == "positive" else "−") + t["title"] for t in i["tags"]),
                        i["order_status_title"], i["status_title"], i["method_title"], i["answer"],
                        (i["published_at"] or "")[:16].replace("T", " "), i["published_by"] or ("Владелец" if i["published_at"] else "")])
        return out


METHOD_TITLES = METHODS
