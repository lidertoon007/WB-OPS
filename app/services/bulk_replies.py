"""Operator-confirmed batches of reviews; no scheduler and no automatic retries."""
import asyncio
import json
import time
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app.models import OrderStatus


class ReviewFilters(BaseModel):
    rating: int | None = Field(default=None, ge=1, le=5)
    order_status: OrderStatus | None = None
    product_id: int | None = Field(default=None, ge=1)


class BulkPreviewRequest(BaseModel):
    filters: ReviewFilters = Field(default_factory=ReviewFilters)
    texts: dict[str, str] = Field(default_factory=dict, max_length=500)


class BulkReplySender:
    def __init__(self, store, publisher, delay=1.1):
        self.store = store
        self.publisher = publisher
        self.delay = delay
        self.tasks = set()

    def _save(self, token, data):
        self.store.set_setting("review_batch:" + token, json.dumps(data, ensure_ascii=False))

    def get(self, token):
        raw = self.store.get_setting("review_batch:" + token, "")
        if not raw:
            raise HTTPException(404, "Пакет не найден")
        return json.loads(raw)

    def preview(self, request):
        if not self.publisher.manual_send_enabled and self.publisher.settings.dry_run:
            raise HTTPException(409, "Ручная отправка выключена")
        items = self.store.list_inbox(item_type="review", **request.filters.model_dump())
        if len(items) > 500:
            raise HTTPException(400, "В списке больше 500 отзывов. Уточните фильтры.")
        selected, skipped = [], []
        for item in items:
            draft = self.store.get(item.id)
            text = request.texts.get(item.id, draft.text if draft else "").strip()
            state = self.store.get_setting("reply_delivery:" + item.id, "")
            reason = None
            if draft is None or draft.published:
                reason = "Черновик отсутствует или ответ уже отправлен"
            elif len(text) < 2 or len(text) > 5000:
                reason = "Недопустимая длина ответа"
            elif state in ("sending", "uncertain", "sent"):
                reason = "Ответ отправлен или результат прошлой отправки требует проверки"
            if reason:
                skipped.append({"item_id": item.id, "reason": reason})
                continue
            selected.append({
                "item_id": item.id, "cabinet_id": item.cabinet_id,
                "product_id": item.product_id, "rating": item.rating,
                "order_status": item.order_status.value,
                "review_text": item.text, "text": text, "draft_text_at_preview": draft.text,
            })
        token = uuid4().hex
        data = {"token": token, "status": "preview", "filters": request.filters.model_dump(mode="json"),
                "created_at": time.time(), "items": selected, "skipped": skipped,
                "total": len(selected), "sent": 0, "failed": 0, "results": []}
        self._save(token, data)
        return data

    def start(self, token):
        data = self.get(token)
        if data["status"] != "preview":
            raise HTTPException(409, "Этот пакет уже был запущен")
        if time.time() - data["created_at"] > 600:
            raise HTTPException(409, "Предпросмотр устарел. Сформируйте список заново.")
        if not data["items"]:
            raise HTTPException(400, "Нет отзывов для отправки")
        key = "review_batch:" + token
        data["status"] = "running"
        encoded = json.dumps(data, ensure_ascii=False)
        # Atomic claim: two tabs cannot start the same package twice.
        with self.store._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            active = c.execute("SELECT value FROM app_settings WHERE key='review_batch_active'").fetchone()
            if active:
                previous = c.execute("SELECT value FROM app_settings WHERE key=?", ("review_batch:" + active[0],)).fetchone()
                if previous and json.loads(previous[0])["status"] == "running":
                    raise HTTPException(409, "Другой пакет уже отправляется. Дождитесь его завершения.")
            old = c.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
            if old is None or json.loads(old[0])["status"] != "preview":
                raise HTTPException(409, "Этот пакет уже был запущен")
            updated = c.execute("UPDATE app_settings SET value=? WHERE key=? AND value=?", (encoded, key, old[0]))
            if updated.rowcount != 1:
                raise HTTPException(409, "Пакет уже запущен в другой вкладке")
            c.execute("INSERT OR REPLACE INTO app_settings(key,value) VALUES('review_batch_active',?)", (token,))
        task = asyncio.create_task(self._run(token))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        self.store.audit("review_batch_started", details=f"token={token};count={len(data['items'])}")
        return data

    async def _run(self, token):
        data = self.get(token)
        try:
            for index, selected in enumerate(data["items"]):
                if index and self.delay:
                    await asyncio.sleep(self.delay)
                item = self.store.get_inbox(selected["item_id"])
                current_draft = self.store.get(selected["item_id"])
                # Do not send if sync changed the selection after its approval.
                expected = data["filters"]
                if current_draft is None or current_draft.text not in (selected["draft_text_at_preview"], selected["text"]):
                    result = {"item_id": selected["item_id"], "status": "skipped", "reason": "Черновик изменился после подтверждения списка"}
                elif item is None or item.item_type.value != "review" or item.cabinet_id != selected["cabinet_id"] or any(
                    expected[name] is not None and str(getattr(item, name)) != str(expected[name])
                    for name in ("rating", "order_status", "product_id")
                ):
                    result = {"item_id": selected["item_id"], "status": "skipped", "reason": "Отзыв больше не соответствует подтверждённым фильтрам"}
                else:
                    try:
                        await self.publisher.send(item.id, selected["text"])
                        result = {"item_id": item.id, "status": "sent"}
                        data["sent"] += 1
                    except HTTPException as exc:
                        result = {"item_id": item.id, "status": "failed", "reason": str(exc.detail)}
                        data["failed"] += 1
                data["results"].append(result)
                data["processed"] = index + 1
                self._save(token, data)
                # Stop on authorization/rate-limit errors. No blind retries.
                if result["status"] == "failed" and any(code in result["reason"] for code in ("WB API 401", "WB API 403", "WB API 402", "WB API 429")):
                    data["status"] = "stopped"
                    data["reason"] = result["reason"]
                    self._save(token, data)
                    return
            data["status"] = "completed"
            self._save(token, data)
            self.store.audit("review_batch_completed", details=f"token={token};sent={data['sent']};failed={data['failed']}")
        except asyncio.CancelledError:
            data["status"] = "interrupted"
            data["reason"] = "Сервис остановлен. Пакет не возобновляется автоматически."
            self._save(token, data)
            raise
        except Exception:
            data["status"] = "interrupted"
            data["reason"] = "Отправка прервана. Проверьте журнал и ответы в WB."
            self._save(token, data)

    def recover_interrupted(self):
        with self.store._connect() as c:
            rows = c.execute("SELECT key,value FROM app_settings WHERE key LIKE 'review_batch:%'").fetchall()
        for key, raw in rows:
            data = json.loads(raw)
            if data["status"] == "running":
                data["status"] = "interrupted"
                data["reason"] = "Сервис перезапущен. Автоматическое продолжение выключено."
                self._save(key.removeprefix("review_batch:"), data)
