from datetime import datetime, timezone
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
                questions = await self._get_all(client, "/api/v1/questions", "questions", is_answered=None if with_answered else False, date_from=date_from, date_to=date_to)
                feedbacks = await self._get_all(client, "/api/v1/feedbacks", "feedbacks", is_answered=None if with_answered else False, date_from=date_from, date_to=date_to)
        except httpx.RequestError as exc:
            raise WBApiError(f"Не удалось подключиться к WB API: {exc}") from exc
        return [self._question(item) for item in questions] + [self._feedback(item) for item in feedbacks]

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
        while True:
            params: dict[str, object] = {"take": take, "skip": skip, "order": "dateDesc"}
            if is_answered is not None:
                params["isAnswered"] = "true" if is_answered else "false"
            if date_from:
                params["dateFrom"] = date_from
            if date_to:
                params["dateTo"] = date_to
            response = await client.get(path, params=params)
            if response.status_code >= 400:
                raise WBApiError(f"WB API {response.status_code}: {response.text[:300]}")
            payload = response.json()
            if isinstance(payload, list):
                batch = payload
            elif isinstance(payload, dict):
                data = payload.get("data", payload)
                batch = data.get(key, []) if isinstance(data, dict) else []
            else:
                batch = []
            batch = [item for item in batch if isinstance(item, dict) and (item.get("id") or item.get("questionId") or item.get("feedbackId"))]
            values.extend(batch)
            if len(batch) < take:
                return values
            skip += take

    def _question(self, item: dict[str, Any]) -> InboxItem:
        product = item.get("productDetails") or {}
        return InboxItem(id=str(item.get("id") or item.get("questionId")), cabinet_id="", item_type=ItemType.QUESTION, text=str(item.get("text") or item.get("question") or ""), product_id=item.get("nmId") or product.get("nmId"), author_name=item.get("userName") or item.get("user_name") or item.get("authorName"), product_name=product.get("productName"), brand=product.get("brandName"), created_at=self._created(item))

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
        return InboxItem(id=str(item.get("id") or item.get("feedbackId")), cabinet_id="", item_type=ItemType.REVIEW, text="\n".join(filter(None, parts)), rating=item.get("productValuation") or item.get("rating"), product_id=item.get("nmId") or product.get("nmId"), order_status=status, author_name=item.get("userName") or item.get("user_name") or item.get("authorName"), product_name=product.get("productName"), brand=product.get("brandName"), has_photo=bool(photos), has_video=bool(video), created_at=self._created(item))

    @staticmethod
    def _created(item: dict[str, Any]) -> datetime:
        raw = item.get("createdDate") or item.get("createdAt") or item.get("date")
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                pass
        return datetime.now(timezone.utc)
