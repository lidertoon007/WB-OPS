"""Explicit single-item publication. Synchronization never calls this service."""
import asyncio
import json
from collections import defaultdict

from fastapi import HTTPException

from app.models import ItemType
from app.services.wb_client import WBApiError, WBDeliveryUncertain, WBFeedbackClient
from app.services.telegram import send_telegram_message


class ReplyPublisher:
    def __init__(self, store, settings, cabinet_loader, client_factory=WBFeedbackClient, manual_send_enabled=False):
        self.store = store
        self.settings = settings
        self.cabinet_loader = cabinet_loader
        self.client_factory = client_factory
        self.manual_send_enabled = manual_send_enabled
        self.locks = defaultdict(asyncio.Lock)

    async def send(self, item_id: str, text: str | None = None):
        async with self.locks[item_id]:
            if self.settings.dry_run and not (text is not None and self.manual_send_enabled):
                raise HTTPException(409, "Публикация отключена")
            item = self.store.get_inbox(item_id)
            draft = self.store.get(item_id)
            if item is None or draft is None:
                raise HTTPException(404, "Входящий объект или черновик не найден")
            if draft.published:
                raise HTTPException(409, "Ответ уже отправлен")
            state_key = f"reply_delivery:{item_id}"
            if self.store.get_setting(state_key, "") in ("sending", "uncertain"):
                raise HTTPException(409, "Результат предыдущей отправки не подтверждён. Проверьте ответ в личном кабинете WB; повторная отправка заблокирована.")
            body = draft.text if text is None else text.strip()
            if len(body.strip()) < 2:
                raise HTTPException(400, "Ответ должен содержать не менее двух символов")
            if item.item_type == ItemType.REVIEW and len(body) > 5000:
                raise HTTPException(400, "Ответ на отзыв не должен превышать 5000 символов")
            if text is None and not draft.confirmed:
                raise HTTPException(409, "Черновик не подтверждён вручную")
            cabinets = {c.id: c for c in self.cabinet_loader(self.settings.cabinets_json)}
            cabinet = cabinets.get(item.cabinet_id)
            if cabinet is None or not cabinet.token:
                raise HTTPException(404, "Кабинет или токен не найден")
            # Atomic SQLite claim also prevents sends by another process.
            with self.store._connect() as c:
                c.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES(?, '')", (state_key,))
                result = c.execute("UPDATE app_settings SET value='sending' WHERE key=? AND value NOT IN ('sending','uncertain','sent')", (state_key,))
                if result.rowcount != 1:
                    raise HTTPException(409, "Отправка уже выполняется или ответ отправлен")
            try:
                # One operator click confirms exactly this text, not other drafts.
                if text is not None:
                    self.store.update_text(item_id, body)
                    self.store.confirm(item_id, True)
                    self.store.audit("draft_confirmed_by_send", item_id)
                await self.client_factory(cabinet.token, self.settings.feedbacks_api_base_url).publish(item, body)
            except WBDeliveryUncertain as exc:
                self.store.set_setting(state_key, "uncertain")
                self.store.audit("publish_uncertain", item_id)
                raise HTTPException(502, str(exc)) from exc
            except WBApiError as exc:
                self.store.set_setting(state_key, "")
                self.store.audit("publish_failed", item_id)
                raise HTTPException(502, str(exc)) from exc
            except Exception as exc:
                self.store.set_setting(state_key, "uncertain")
                self.store.audit("publish_uncertain", item_id)
                raise HTTPException(502, "Не удалось подтвердить отправку. Проверьте ответ в личном кабинете WB перед повторной попыткой.") from exc
            self.store.mark_published(item_id)
            self.store.set_setting(state_key, "sent")
            self.store.audit("published", item_id, f"type={item.item_type.value}")
            telegram_raw = self.store.get_setting("module:telegram", "")
            try:
                telegram = json.loads(telegram_raw) if telegram_raw else {}
            except json.JSONDecodeError:
                telegram = {}
            values = telegram.get("values", {}) if isinstance(telegram, dict) else {}
            if isinstance(values, dict) and values.get("enabled") and (
                values.get("notify_published") or (item.item_type == ItemType.QUESTION and values.get("notify_question_answers"))
            ):
                try:
                    await send_telegram_message(values, f"WB OPS: опубликован ответ на {'вопрос' if item.item_type == ItemType.QUESTION else 'отзыв'} {item_id}.")
                except Exception as exc:
                    self.store.audit("telegram_error", item_id, str(exc)[:300])
            return {"item_id": item_id, "published": True}
