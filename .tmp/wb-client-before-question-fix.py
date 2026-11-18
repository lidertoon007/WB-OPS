from datetime import date, datetime, time, timezone
import math
import re
from typing import Any

import httpx

from app.models import InboxItem, ItemType, OrderStatus


class WBApiError(RuntimeError):
    pass

class WBDeliveryUncertain(WBApiError):
    pass


class WBFeedbackClient:
    """Minimal client for the current WB feedbacks/questions API."""

    def __init__(self, token: str, base_url: str = "https://feedbacks-api.wildberries.ru"):
        self.token = token
        self.base_url = base_url.rstrip("/")

    async def count_unanswered(self, kind: str) -> int:
        """Current all-time count from WB, without touching inbox or drafts."""
        if not self.token:
            raise WBApiError("Для кабинета не задан API-токен")
        if kind not in {"review", "question"}:
            raise ValueError("Unsupported count kind")
        path = "/api/v1/feedbacks/count-unanswered" if kind == "review" else "/api/v1/questions/count-unanswered"
        try:
            async with httpx.AsyncClient(base_url=self.base_url, headers={"Authorization": self.token}, timeout=10) as client:
                response = await client.get(path)
        except httpx.RequestError as exc:
            raise WBApiError("Не удалось получить актуальный счётчик WB") from exc
        if response.status_code == 429:
            raise WBApiError("WB API 429: превышен лимит запросов")
        if response.status_code >= 400:
            raise WBApiError(f"WB API {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise WBApiError("WB API вернул некорректный счётчик") from exc
        if not isinstance(payload, dict) or payload.get("error") is not False:
            raise WBApiError("WB API не подтвердил актуальный счётчик")
        data = payload.get("data")
        count = data.get("countUnanswered") if isinstance(data, dict) else None
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise WBApiError("WB API вернул некорректный счётчик")
        return count

    async def fetch_unanswered(self) -> list[InboxItem]:
        if not self.token:
            raise WBApiError("Для кабинета не задан API-токен")
        headers = {"Authorization": self.token, "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=30) as client:
                questions = await self._get_all(client, "/api/v1/questions", "questions", is_answered=False)
                feedbacks = await self._get_all(client, "/api/v1/feedbacks", "feedbacks", is_answered=False)
        except httpx.RequestError as exc:
            raise WBApiError(f"Не удалось подключиться к WB API: {exc}") from exc
        return [self._question(item) for item in questions] + [self._feedback(item) for item in feedbacks]

    async def fetch_history(self, date_from: str | None = None, date_to: str | None = None, with_answered: bool = True) -> list[InboxItem]:
        if not self.token:
            raise WBApiError("Для кабинета не задан API-токен")
        headers = {"Authorization": self.token, "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=30) as client:
                if with_answered:
                    question_batches = await self._history_batches(client, "/api/v1/questions", "questions", date_from, date_to)
                    feedback_batches = await self._history_batches(client, "/api/v1/feedbacks", "feedbacks", date_from, date_to)
                    questions = self._deduplicate(question_batches)
                    feedbacks = self._deduplicate(feedback_batches)
                else:
                    questions = [{**item, "_wb_answered": False} for item in await self._get_all(client, "/api/v1/questions", "questions", is_answered=False, date_from=date_from, date_to=date_to)]
                    feedbacks = [{**item, "_wb_answered": False} for item in await self._get_all(client, "/api/v1/feedbacks", "feedbacks", is_answered=False, date_from=date_from, date_to=date_to)]
        except httpx.RequestError as exc:
            raise WBApiError(f"Не удалось подключиться к WB API: {exc}") from exc
        return [self._question(item) for item in questions] + [self._feedback(item) for item in feedbacks]

    async def _history_batches(self, client: httpx.AsyncClient, path: str, key: str,
                               date_from: str | None, date_to: str | None) -> list[dict[str, Any]]:
        """Load both WB history partitions; isAnswered is mandatory for these endpoints."""
        values: list[dict[str, Any]] = []
        for is_answered in (True, False):
            batch = await self._get_all(client, path, key, is_answered=is_answered,
                                        date_from=date_from, date_to=date_to)
            values.extend({**item, "_wb_answered": is_answered} for item in batch)
        return values

    @staticmethod
    def _deduplicate(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for item in items:
            item_id = item.get("id") or item.get("questionId") or item.get("feedbackId")
            key = str(item_id)
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    @staticmethod
    def _date_timestamp(value: str, *, end_of_day: bool = False) -> int:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise WBApiError("Дата должна быть в формате ГГГГ-ММ-ДД")
        try:
            day = date.fromisoformat(value)
        except ValueError as exc:
            raise WBApiError("Некорректная дата") from exc
        boundary = time(23, 59, 59) if end_of_day else time.min
        return int(datetime.combine(day, boundary, tzinfo=timezone.utc).timestamp())

    async def publish(self, item: InboxItem, text: str) -> None:
        headers = {"Authorization": self.token, "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=30) as client:
                if item.item_type == ItemType.QUESTION:
                    response = await client.patch("/api/v1/questions", json={"id": item.id, "text": text, "state": "wbRu"})
                else:
                    response = await client.post("/api/v1/feedbacks/answer", json={"id": item.id, "text": text})
        except httpx.RequestError as exc:
            raise WBDeliveryUncertain("Нет подтверждения от WB. Проверьте наличие ответа в личном кабинете; повторная отправка заблокирована.") from exc
        if response.status_code >= 500:
            raise WBDeliveryUncertain("WB вернул ошибку сервера. Проверьте наличие ответа в личном кабинете перед повторной отправкой.")
        if response.status_code >= 400:
            raise WBApiError(f"WB API {response.status_code}: {response.text[:300]}")
        if response.content:
            try:
                payload = response.json()
            except ValueError as exc:
                raise WBDeliveryUncertain("WB вернул неожиданный ответ. Проверьте публикацию в личном кабинете.") from exc
            if isinstance(payload, dict) and payload.get("error"):
                raise WBApiError(f"WB API: {str(payload.get('errorText') or 'Ответ не принят')[:300]}")

    async def _get_all(self, client: httpx.AsyncClient, path: str, key: str, is_answered: bool | None = False, date_from: str | None = None, date_to: str | None = None) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        skip = 0
        take = 100
        from_timestamp = self._date_timestamp(date_from) if date_from is not None else None
        to_timestamp = self._date_timestamp(date_to, end_of_day=True) if date_to is not None else None
        if from_timestamp is not None and to_timestamp is not None and from_timestamp > to_timestamp:
            raise WBApiError("Начало периода позже конца")
        while True:
            params: dict[str, object] = {"take": take, "skip": skip, "order": "dateDesc"}
            if is_answered is not None:
                params["isAnswered"] = "true" if is_answered else "false"
            if from_timestamp is not None:
                params["dateFrom"] = from_timestamp
            if to_timestamp is not None:
                params["dateTo"] = to_timestamp
            response = await client.get(path, params=params)
            if response.status_code == 429:
                retry = response.headers.get("X-RateLimit-Retry", "").strip()
                try:
                    seconds = float(retry)
                except ValueError:
                    seconds = 0
                wait = f" Повторите импорт через {math.ceil(seconds)} сек." if math.isfinite(seconds) and seconds > 0 else " Повторите импорт позже."
                raise WBApiError(f"WB API 429: превышен лимит запросов.{wait}")
            if response.status_code >= 400:
                raise WBApiError(f"WB API {response.status_code}: {response.text[:300]}")
            payload = response.json()
            if isinstance(payload, list):
                batch = payload
            elif isinstance(payload, dict):
                if payload.get("error"):
                    raise WBApiError(f"WB API: {str(payload.get('errorText') or 'Ошибка загрузки истории')[:300]}")
                data = payload.get("data", payload)
                if not isinstance(data, dict) or not isinstance(data.get(key), list):
                    raise WBApiError("WB API вернул неожиданный формат списка")
                batch = data[key]
            else:
                raise WBApiError("WB API вернул неожиданный формат списка")
            if any(not isinstance(item, dict) or not (item.get("id") or item.get("questionId") or item.get("feedbackId")) for item in batch):
                raise WBApiError("WB API вернул некорректную запись списка; синхронизация отменена")
            values.extend(batch)
            if len(batch) < take:
                return values
            skip += take

    def _question(self, item: dict[str, Any]) -> InboxItem:
        product = item.get("productDetails") or {}
        return InboxItem(id=str(item.get("id") or item.get("questionId")), cabinet_id="", item_type=ItemType.QUESTION, text=str(item.get("text") or item.get("question") or ""), product_id=item.get("nmId") or product.get("nmId"), author_name=item.get("userName") or item.get("user_name") or item.get("authorName"), product_name=product.get("productName"), brand=product.get("brandName"), created_at=self._created(item), answered_in_wb=item.get("_wb_answered"))

    def _feedback(self, item: dict[str, Any]) -> InboxItem:
        status = item.get("orderStatus")
        if status not in {value.value for value in OrderStatus}:
            status = OrderStatus.NOT_SPECIFIED
        parts = [str(item.get("text") or "").strip()]
        for key, label in (("pros", "Достоинства"), ("cons", "Недостатки")):
            if item.get(key):
                parts.append(f"{label}: {item[key]}")
        product = item.get("productDetails") or {}
        photos = item.get("photoLinks") or item.get("photos") or []
        video = item.get("video") or item.get("videoLink")
        return InboxItem(id=str(item.get("id") or item.get("feedbackId")), cabinet_id="", item_type=ItemType.REVIEW, text="\n".join(filter(None, parts)), rating=item.get("productValuation") or item.get("rating"), product_id=item.get("nmId") or product.get("nmId"), order_status=status, author_name=item.get("userName") or item.get("user_name") or item.get("authorName"), product_name=product.get("productName"), brand=product.get("brandName"), has_photo=bool(photos), has_video=bool(video), created_at=self._created(item), answered_in_wb=item.get("_wb_answered"))

    @staticmethod
    def _created(item: dict[str, Any]) -> datetime:
        raw = item.get("createdDate") or item.get("createdAt") or item.get("date")
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                pass
        if "_wb_answered" in item:
            raise WBApiError("WB API вернул отзыв или вопрос без корректной даты создания")
        return datetime.now(timezone.utc)
