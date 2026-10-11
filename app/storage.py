from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path

from app.models import ComplaintCase, ComplaintStatus, InboxItem, ReplyDraft


class DraftStore:
    """SQLite store for the first web MVP; schema is migration-friendly."""

    def __init__(self, database_url: str):
        if not database_url.startswith("sqlite:///"):
            raise ValueError("Первая версия поддерживает SQLite; PostgreSQL подключается тем же интерфейсом позже")
        self.path = Path(database_url.removeprefix("sqlite:///"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS reply_drafts (item_id TEXT PRIMARY KEY, text TEXT NOT NULL, mode TEXT NOT NULL, blocked_reason TEXT)")
            try:
                connection.execute("ALTER TABLE reply_drafts ADD COLUMN confirmed INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            try:
                connection.execute("ALTER TABLE reply_drafts ADD COLUMN published INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            for statement in (
                "ALTER TABLE reply_drafts ADD COLUMN moderation_status TEXT NOT NULL DEFAULT 'pending'",
                "ALTER TABLE reply_drafts ADD COLUMN moderation_comment TEXT NOT NULL DEFAULT ''",
            ):
                try:
                    connection.execute(statement)
                except sqlite3.OperationalError:
                    pass
            connection.execute("CREATE TABLE IF NOT EXISTS inbox_items (id TEXT PRIMARY KEY, cabinet_id TEXT NOT NULL, item_type TEXT NOT NULL, text TEXT NOT NULL, rating INTEGER, product_id INTEGER, created_at TEXT NOT NULL)")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(inbox_items)")}
            if "order_status" not in columns:
                connection.execute("ALTER TABLE inbox_items ADD COLUMN order_status TEXT NOT NULL DEFAULT 'notSpecified'")
            if "author_name" not in columns:
                connection.execute("ALTER TABLE inbox_items ADD COLUMN author_name TEXT")
            inbox_additions = {
                "marketplace": "TEXT NOT NULL DEFAULT 'wildberries'",
                "product_name": "TEXT",
                "brand": "TEXT",
                "has_photo": "INTEGER NOT NULL DEFAULT 0",
                "has_video": "INTEGER NOT NULL DEFAULT 0",
                "review_topics": "TEXT NOT NULL DEFAULT '[]'",
                "review_sentiment": "TEXT NOT NULL DEFAULT 'neutral'",
            }
            columns = {row[1] for row in connection.execute("PRAGMA table_info(inbox_items)")}
            for name, definition in inbox_additions.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE inbox_items ADD COLUMN {name} {definition}")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(reply_drafts)")}
            if "user_edited" not in columns:
                # Existing drafts are protected until the safe upgrade classifies them.
                connection.execute("ALTER TABLE reply_drafts ADD COLUMN user_edited INTEGER NOT NULL DEFAULT 1")
            connection.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL, item_id TEXT, details TEXT, created_at TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS draft_history (id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL, text TEXT NOT NULL, action TEXT NOT NULL, created_at TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS complaints (item_id TEXT PRIMARY KEY, cabinet_id TEXT NOT NULL, rating INTEGER, status TEXT NOT NULL, reason_code TEXT, reason_title TEXT, evidence TEXT, checklist TEXT NOT NULL, created_at TEXT NOT NULL)")

    def save(self, draft: ReplyDraft) -> ReplyDraft:
        with self._connect() as connection:
                connection.execute("INSERT OR REPLACE INTO reply_drafts(item_id,text,mode,blocked_reason,confirmed,published) VALUES(?,?,?,?,?,?)", (draft.item_id, draft.text, draft.mode.value, draft.blocked_reason, int(draft.confirmed), int(draft.published)))
        return draft

    def save_generated(self, draft: ReplyDraft) -> ReplyDraft:
        with self._connect() as connection:
            row = connection.execute("SELECT confirmed,published,user_edited FROM reply_drafts WHERE item_id=?", (draft.item_id,)).fetchone()
            if row and any(row):
                return self.get(draft.item_id)
            connection.execute(
                "INSERT INTO reply_drafts(item_id,text,mode,blocked_reason,confirmed,published,user_edited) VALUES(?,?,?,?,0,0,0) "
                "ON CONFLICT(item_id) DO UPDATE SET text=excluded.text,mode=excluded.mode,blocked_reason=excluded.blocked_reason",
                (draft.item_id, draft.text, draft.mode.value, draft.blocked_reason),
            )
            from datetime import datetime, timezone
            connection.execute("INSERT INTO draft_history(item_id,text,action,created_at) VALUES(?,?,?,?)", (draft.item_id, draft.text, "generated", datetime.now(timezone.utc).isoformat()))
        return draft

    def list(self) -> list[ReplyDraft]:
        with self._connect() as connection:
            rows = connection.execute("SELECT item_id,text,mode,blocked_reason,confirmed,published,moderation_status,moderation_comment FROM reply_drafts ORDER BY rowid DESC").fetchall()
        return [ReplyDraft(item_id=row[0], text=row[1], mode=row[2], blocked_reason=row[3], confirmed=bool(row[4]), published=bool(row[5]), moderation_status=row[6], moderation_comment=row[7]) for row in rows]

    def get(self, item_id: str) -> ReplyDraft | None:
        with self._connect() as connection:
            row = connection.execute("SELECT item_id,text,mode,blocked_reason,confirmed,published,moderation_status,moderation_comment FROM reply_drafts WHERE item_id=?", (item_id,)).fetchone()
        return ReplyDraft(item_id=row[0], text=row[1], mode=row[2], blocked_reason=row[3], confirmed=bool(row[4]), published=bool(row[5]), moderation_status=row[6], moderation_comment=row[7]) if row else None

    def update_text(self, item_id: str, text: str) -> ReplyDraft | None:
        with self._connect() as connection:
            connection.execute("UPDATE reply_drafts SET text=?,user_edited=1,confirmed=0,moderation_status='pending',moderation_comment='' WHERE item_id=?", (text, item_id))
            from datetime import datetime, timezone
            connection.execute("INSERT INTO draft_history(item_id,text,action,created_at) VALUES(?,?,?,?)", (item_id, text, "edited", datetime.now(timezone.utc).isoformat()))
        return self.get(item_id)

    def confirm(self, item_id: str, confirmed: bool = True) -> ReplyDraft | None:
        with self._connect() as connection:
            connection.execute("UPDATE reply_drafts SET confirmed=?,moderation_status=? WHERE item_id=?", (int(confirmed), "approved" if confirmed else "pending", item_id))
        return self.get(item_id)

    def moderate(self, item_id: str, status: str, comment: str = "") -> ReplyDraft | None:
        with self._connect() as connection:
            confirmed = 1 if status == "approved" else 0
            connection.execute("UPDATE reply_drafts SET moderation_status=?,moderation_comment=?,confirmed=? WHERE item_id=?", (status, comment, confirmed, item_id))
            from datetime import datetime, timezone
            current = connection.execute("SELECT text FROM reply_drafts WHERE item_id=?", (item_id,)).fetchone()
            if current:
                connection.execute("INSERT INTO draft_history(item_id,text,action,created_at) VALUES(?,?,?,?)", (item_id, current[0], f"moderation_{status}", datetime.now(timezone.utc).isoformat()))
        return self.get(item_id)

    def draft_history(self, item_id: str, limit: int = 50) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT id,item_id,text,action,created_at FROM draft_history WHERE item_id=? ORDER BY id DESC LIMIT ?", (item_id, limit)).fetchall()
        return [{"id": r[0], "item_id": r[1], "text": r[2], "action": r[3], "created_at": r[4]} for r in rows]

    def mark_published(self, item_id: str) -> ReplyDraft | None:
        with self._connect() as connection:
            connection.execute("UPDATE reply_drafts SET published=1 WHERE item_id=?", (item_id,))
        return self.get(item_id)

    def get_inbox(self, item_id: str) -> InboxItem | None:
        with self._connect() as connection:
            row = connection.execute("SELECT id,cabinet_id,item_type,text,rating,product_id,created_at,order_status,author_name,marketplace,product_name,brand,has_photo,has_video FROM inbox_items WHERE id=?", (item_id,)).fetchone()
        return InboxItem(id=row[0], cabinet_id=row[1], item_type=row[2], text=row[3], rating=row[4], product_id=row[5], created_at=row[6], order_status=row[7], author_name=row[8], marketplace=row[9], product_name=row[10], brand=row[11], has_photo=bool(row[12]), has_video=bool(row[13])) if row else None

    def get_setting(self, key: str, default: str) -> str:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute("INSERT OR REPLACE INTO app_settings(key,value) VALUES(?,?)", (key, value))

    def audit(self, event: str, item_id: str | None = None, details: str = "") -> None:
        from datetime import datetime, timezone
        with self._connect() as connection:
            connection.execute("INSERT INTO audit_log(event,item_id,details,created_at) VALUES(?,?,?,?)", (event, item_id, details, datetime.now(timezone.utc).isoformat()))

    def list_audit(self, limit: int = 100) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT id,event,item_id,details,created_at FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": r[0], "event": r[1], "item_id": r[2], "details": r[3], "created_at": r[4]} for r in rows]

    def save_complaint(self, case: ComplaintCase) -> ComplaintCase:
        import json
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO complaints(item_id,cabinet_id,rating,status,reason_code,reason_title,evidence,checklist,created_at) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(item_id) DO UPDATE SET cabinet_id=excluded.cabinet_id,rating=excluded.rating,"
                "status=CASE WHEN complaints.status IN ('submitted_manual','submitted_browser','dismissed') THEN complaints.status ELSE excluded.status END,"
                "reason_code=excluded.reason_code,reason_title=excluded.reason_title,evidence=excluded.evidence,checklist=excluded.checklist",
                (case.item_id, case.cabinet_id, case.rating, case.status.value, case.reason_code, case.reason_title, case.evidence, json.dumps(case.checklist, ensure_ascii=False), case.created_at.isoformat()))
        return next(c for c in self.list_complaints() if c.item_id == case.item_id)

    def list_complaints(self, status: str | None = None) -> list[ComplaintCase]:
        import json
        query = "SELECT item_id,cabinet_id,rating,status,reason_code,reason_title,evidence,checklist,created_at FROM complaints"
        params: tuple[object, ...] = ()
        if status:
            query += " WHERE status=?"
            params = (status,)
        query += " ORDER BY created_at DESC"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [ComplaintCase(item_id=r[0], cabinet_id=r[1], rating=r[2], status=ComplaintStatus(r[3]), reason_code=r[4], reason_title=r[5], evidence=r[6], checklist=json.loads(r[7]), created_at=r[8]) for r in rows]

    def update_complaint_status(self, item_id: str, status: ComplaintStatus) -> ComplaintCase | None:
        with self._connect() as connection:
            connection.execute("UPDATE complaints SET status=? WHERE item_id=?", (status.value, item_id))
        return next((case for case in self.list_complaints() if case.item_id == item_id), None)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def save_inbox(self, items: list[InboxItem]) -> int:
        import json
        from app.services.review_analysis import analyze_review
        with self._connect() as connection:
            for item in items:
                markup = analyze_review(item.text, item.rating) if item.item_type.value == "review" else None
                connection.execute("INSERT OR REPLACE INTO inbox_items(id,cabinet_id,item_type,text,rating,product_id,created_at,order_status,author_name,marketplace,product_name,brand,has_photo,has_video,review_topics,review_sentiment) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (item.id, item.cabinet_id, item.item_type.value, item.text, item.rating, item.product_id, item.created_at.isoformat(), item.order_status.value, item.author_name, item.marketplace, item.product_name, item.brand, int(item.has_photo), int(item.has_video), json.dumps(markup.topics if markup else [], ensure_ascii=False), markup.sentiment if markup else "neutral"))
        return len(items)

    def list_inbox(self, item_type: str | None = None, rating: int | None = None, product_id: int | None = None, order_status: str | None = None, search: str | None = None, status: str | None = None, limit: int = 200, offset: int = 0) -> list[InboxItem]:
        with self._connect() as connection:
            publication_clause = "COALESCE(d.published,0)=1" if status == "published" else "COALESCE(d.published,0)=0"
            rows = connection.execute(f"SELECT i.id,i.cabinet_id,i.item_type,i.text,i.rating,i.product_id,i.created_at,i.order_status,i.author_name,i.marketplace,i.product_name,i.brand,i.has_photo,i.has_video FROM inbox_items i LEFT JOIN reply_drafts d ON d.item_id=i.id WHERE {publication_clause} ORDER BY i.created_at DESC").fetchall()
        items = [InboxItem(id=r[0], cabinet_id=r[1], item_type=r[2], text=r[3], rating=r[4], product_id=r[5], created_at=r[6], order_status=r[7], author_name=r[8], marketplace=r[9], product_name=r[10], brand=r[11], has_photo=bool(r[12]), has_video=bool(r[13])) for r in rows]
        drafts = {draft.item_id: draft for draft in self.list()}
        query = (search or "").strip().lower()
        filtered = [item for item in items if (not item_type or item.item_type.value == item_type) and (rating is None or item.rating == rating) and (product_id is None or item.product_id == product_id) and (not order_status or (item.item_type.value == "review" and item.order_status.value == order_status)) and (not query or query in item.text.lower() or query in item.id.lower() or query in str(item.product_id or "").lower()) and (not status or ("published" if drafts.get(item.id, None) and drafts[item.id].published else "draft") == status)]
        return filtered[max(offset, 0):max(offset, 0) + min(max(limit, 1), 500)]

    def list_inbox_overview(self, item_type: str | None = None, rating: int | None = None, product_id: int | None = None, order_status: str | None = None, search: str | None = None, status: str | None = None, topic: str | None = None, sentiment: str | None = None, date_from: str | None = None, date_to: str | None = None, sort: str = "desc", limit: int = 25, offset: int = 0) -> list[dict[str, object]]:
        import json
        with self._connect() as connection:
            order = "ASC" if sort == "asc" else "DESC"
            rows = connection.execute(f"SELECT i.id,i.cabinet_id,i.item_type,i.text,i.rating,i.product_id,i.created_at,i.order_status,d.text,d.confirmed,d.published,i.review_topics,i.review_sentiment,d.moderation_status,d.moderation_comment,i.author_name,i.marketplace,i.product_name,i.brand,i.has_photo,i.has_video FROM inbox_items i LEFT JOIN reply_drafts d ON d.item_id=i.id ORDER BY i.created_at {order}").fetchall()
        query = (search or "").strip().lower()
        result = []
        for r in rows:
            item_type_value, order_value = r[2], r[7]
            if item_type and item_type_value != item_type or rating is not None and r[4] != rating or product_id is not None and r[5] != product_id or order_status and not (item_type_value == "review" and order_value == order_status):
                continue
            if query and query not in (r[0] + " " + r[3] + " " + str(r[5] or "")).lower():
                continue
            topics = json.loads(r[11] or "[]")
            if topic and topic not in topics:
                continue
            if sentiment and r[12] != sentiment:
                continue
            if date_from and r[6][:10] < date_from:
                continue
            if date_to and r[6][:10] > date_to:
                continue
            delivery_state = self.get_setting(f"reply_delivery:{r[0]}", "")
            current = "published" if r[10] else "error" if delivery_state == "uncertain" else "sending" if delivery_state == "sending" else "ready" if r[9] else "draft" if r[8] is not None else "new"
            if status and current != status:
                continue
            result.append({"id": r[0], "cabinet_id": r[1], "item_type": item_type_value, "text": r[3], "rating": r[4], "product_id": r[5], "created_at": r[6], "order_status": order_value, "draft_text": r[8] or "", "confirmed": bool(r[9]), "published": bool(r[10]), "status": current, "error": delivery_state if current == "error" else "", "topics": topics, "sentiment": r[12], "moderation_status": r[13] or "pending", "moderation_comment": r[14] or "", "author_name": r[15], "marketplace": r[16], "product_name": r[17], "brand": r[18], "has_photo": bool(r[19]), "has_video": bool(r[20])})
        return result[max(offset, 0):max(offset, 0) + min(max(limit, 1), 500)]
