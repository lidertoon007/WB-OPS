import re

from app.models import ComplaintCase, ComplaintStatus, InboxItem, ItemType, ComplaintDetails, ReplyDraft


class ComplaintService:
    """Conservative pre-check for manual WB complaint submission.

    A low rating is never treated as sufficient evidence by itself.
    """

    _rules = [
        ("spam", "Спам или реклама", r"(?:https?://|www\.|telegram|телеграм|whatsapp|вацап|купите|звоните|\+7\s*\(?\d{3})"),
        ("obscene", "Нецензурная лексика", r"(?:бля|еба|ебл|хуй|пизд|сука|мудак|долбо|fuck|shit)"),
        ("threat", "Угрозы или оскорбления", r"(?:убью|убить|сдохни|найду тебя|угрож|оскорб)"),
        ("politics", "Политический контекст", r"(?:президент|войн[ауеы]|парт(?:ия|ии)|политик|санкци)"),
    ]

    def details(self, case: ComplaintCase, item: InboxItem | None, draft: ReplyDraft | None = None) -> ComplaintDetails:
        text = ""
        if case.reason_code and case.evidence and item:
            text = (
                f"Просим проверить отзыв {case.item_id} на товар с артикулом WB {item.product_id or 'не указан'}. "
                f"Возможное основание: {case.reason_title}. "
                f"Фрагмент отзыва: «{case.evidence}». "
                "Просим оценить, нарушает ли данный фрагмент правила площадки, и принять решение по результатам проверки."
            )[:1000]
        return ComplaintDetails(
            **case.model_dump(), review_text=item.text if item else "",
            review_exists=item is not None, product_id=item.product_id if item else None,
            order_status=item.order_status if item else "notSpecified",
            reply_published=draft.published if draft else False, complaint_draft=text,
        )

    def evaluate(self, item: InboxItem) -> ComplaintCase | None:
        if item.item_type != ItemType.REVIEW or item.rating not in (1, 2):
            return None
        text = item.text.strip()
        checklist = [
            "Отзыв имеет оценку 1–2 звезды",
            "Нарушение подтверждается текстом отзыва",
            "Основание относится к правилам отзыва, а не к качеству товара",
            "Перед отправкой нужно проверить карточку в ЛК WB",
        ]
        for code, title, pattern in self._rules:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                evidence = text[max(0, match.start() - 60): min(len(text), match.end() + 100)]
                return ComplaintCase(item_id=item.id, cabinet_id=item.cabinet_id, rating=item.rating, status=ComplaintStatus.READY_MANUAL, reason_code=code, reason_title=title, evidence=evidence, checklist=checklist)
        return ComplaintCase(item_id=item.id, cabinet_id=item.cabinet_id, rating=item.rating, status=ComplaintStatus.NOT_ELIGIBLE, reason_title="Недостаточно явных признаков нарушения", evidence="", checklist=checklist)
