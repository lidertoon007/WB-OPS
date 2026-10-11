import re

from app.models import InboxItem, PublicationMode, ReplyDraft, OrderStatus
from app.services.question_knowledge import QuestionKnowledge
from app.services.product_knowledge import ProductKnowledge
from app.services.templates import TemplateStore


class ReplyService:
    """Safe reply workflow. Publishing is intentionally not implemented yet."""

    def _topic(self, text: str) -> str:
        value = text.lower()
        if any(word in value for word in ("почт", "достав", "не привез", "не приех", "не дали пример", "примерять не дают", "пвз")):
            return "delivery"
        if any(word in value for word in ("не тот", "пересорт", "другая модель", "другой товар")):
            return "wrong_item"
        if any(word in value for word in ("брак", "крив", "порван", "дыр", "протер", "шов", "карман", "молни", "пугов")):
            return "defect"
        if any(word in value for word in ("маломер", "большемер", "размер", "посадк", "широк", "узк", "коротк", "длин")):
            return "size"
        if any(word in value for word in ("тонк", "плотн", "ткан", "материал", "лосин", "деним")):
            return "fabric"
        if any(word in value for word in ("цвет", "оттен", "выцвет", "линя")):
            return "color"
        if any(word in value for word in ("запах", "воня")):
            return "smell"
        return "general"

    def _personalize(self, text: str, item: InboxItem) -> str:
        name = (item.author_name or "").strip()
        if not name:
            return text
        name = re.sub(r"[^\w\- .]", "", name, flags=re.UNICODE).strip()[:60]
        if not name:
            return text
        return re.sub(r"^Здравствуйте!", f"Здравствуйте, {name}!", text, count=1)

    def _question_topic(self, text: str) -> str:
        value = text.lower()
        if any(word in value for word in ("вернул", "возврат", "деньги не", "деньги ещё", "деньги еще", "не вернули", "отменить заказ")):
            return "delivery"
        if any(word in value for word in ("страна производ", "где производ", "произведен", "турци", "росси")):
            return "details"
        if any(word in value for word in ("размер", "тал", "бед", "замер", "обхват", "ширин", "длин", "рост", "посадк")):
            return "measurements"
        if any(word in value for word in ("состав", "хлоп", "эластан", "ткан", "материал", "деним", "тян", "стрейч")):
            return "composition"
        if any(word in value for word in ("налич", "будет", "появит", "цвет", "расцвет")):
            return "availability"
        if any(word in value for word in ("стир", "стирать", "сад", "усад", "глад", "уход")):
            return "care"
        if any(word in value for word in ("зим", "лет", "сезон", "тепл", "холод", "утеп", "флис")):
            return "season"
        if any(word in value for word in ("цен", "скид", "акци", "дешев", "дорог")):
            return "price"
        if any(word in value for word in ("карман", "молни", "пугов", "застеж", "низ", "крой", "фасон")):
            return "details"
        if any(word in value for word in ("достав", "заказ", "пвз", "вернут", "получ")):
            return "delivery"
        return "general"

    def draft(self, item: InboxItem) -> ReplyDraft:
        if item.item_type.value == "review":
            return ReplyDraft(item_id=item.id, text=self._personalize(self._review_reply(item), item), mode=PublicationMode.DRAFT)
        if not item.text.strip():
            return ReplyDraft(
                item_id=item.id,
                text="",
                blocked_reason="Пустой текст обращения",
            )

        topic = self._topic(item.text)
        if item.item_type.value == "question":
            question_topic = self._question_topic(item.text)
            known_answer = self.knowledge.exact_answer(item.text, item.product_id)
            if known_answer:
                return ReplyDraft(item_id=item.id, text=self._personalize(known_answer, item), mode=PublicationMode.DRAFT)
            facts = self.products.get(item.product_id) or {}
            fact_text = ""
            if question_topic == "composition" and facts.get("composition"):
                fact_text = f" Состав: {facts['composition']}."
            elif question_topic == "measurements" and facts.get("measurements"):
                fact_text = f" Замеры: {facts['measurements']}."
            elif question_topic == "details" and facts.get("details"):
                fact_text = f" Характеристики: {facts['details']}."
            elif question_topic == "care" and facts.get("care"):
                fact_text = f" Уход: {facts['care']}."
            elif question_topic == "season" and facts.get("season"):
                fact_text = f" Сезонность: {facts['season']}."
            editable = self.templates.all()
            question_templates = {
                "measurements": "Здравствуйте! Для точного подбора сравните параметры с таблицей замеров в карточке товара. Если останутся вопросы, напишите нам в чат продавца — поможем сориентироваться.",
                "composition": "Здравствуйте! Спасибо за вопрос. Актуальный состав и характеристики указаны в карточке товара и на ярлыке изделия. Если нужного параметра не видно, напишите — уточним информацию по модели.",
                "availability": "Здравствуйте! Наличие размеров и цветов меняется в течение дня. Выберите нужный вариант в карточке и включите уведомление о поступлении, если он временно недоступен.",
                "care": "Здравствуйте! Рекомендуем соблюдать инструкцию на ярлыке изделия: подходящий режим стирки и температура помогут сохранить форму и цвет.",
                "season": "Здравствуйте! Ориентируйтесь на плотность ткани и описание модели в карточке. Если напишете, для какого сезона выбираете вещь, поможем сориентироваться.",
                "price": "Здравствуйте! Цена и скидка формируются Wildberries и могут меняться. Актуальную стоимость можно увидеть в карточке товара перед оформлением заказа.",
                "details": "Здравствуйте! Спасибо за вопрос. Проверьте характеристики и фотографии в карточке товара. Если уточните нужную деталь, мы проверим информацию по модели.",
                "delivery": "Здравствуйте! Срок доставки, получение и возврат зависят от заказа и отображаются в приложении Wildberries. Если вопрос относится к конкретному заказу, рекомендуем обратиться в поддержку площадки.",
                "general": "Здравствуйте! Спасибо за вопрос. Уточните, пожалуйста, модель и параметр, который вас интересует, — проверим информацию и подскажем.",
            }
            question_templates["measurements"] = editable.get("question_measurements", question_templates["measurements"])
            question_templates["details"] = editable.get("question_details", question_templates["details"])
            text = self._personalize(question_templates[question_topic] + fact_text, item)

        return ReplyDraft(item_id=item.id, text=text, mode=PublicationMode.DRAFT)

    def validate(self, text: str, item: InboxItem | None = None) -> list[str]:
        value = (text or "").strip()
        warnings: list[str] = []
        if len(value) < 2:
            warnings.append("Ответ слишком короткий")
        if item and item.item_type.value == "review" and len(value) > 5000:
            warnings.append("Ответ на отзыв длиннее 5000 символов")
        if item and item.order_status in (OrderStatus.REJECTED, OrderStatus.RETURNED) and re.search(r"комфортн\w* носк", value.lower()):
            warnings.append("Для отказа или возврата нельзя использовать пожелание комфортной носки")
        if item and item.item_type.value == "review" and re.search(r"(оформите|оформить|можно|можете)\s+(?:возврат|отказ)|верните\s+товар", value.lower()):
            warnings.append("По политике компании в ответе на отзыв нельзя предлагать возврат товара")
        if re.search(r"(гарантируем|точно получите|обязательно будет|мы виноваты,? вы)", value.lower()):
            warnings.append("Фраза содержит рискованное обещание или обвинение покупателя")
        if item and item.item_type.value == "question" and not any(ch.isalpha() for ch in value):
            warnings.append("Ответ не содержит понятного текста")
        return warnings

    def _review_reply(self, item: InboxItem) -> str:
        value = item.text.lower().replace("ё", "е")
        topic = self._topic(value)
        status = item.order_status
        # Explicit first-person refusal/return also blocks wear wishes when WB
        # status is missing or stale. Do not infer purchase from a high rating.
        returned = bool(re.search(r"\b(вернул[аи]?|вернули|возврат оформ|оформил[аи]? возврат|сдал[аи]? обратно)\b", value))
        refused = bool(re.search(r"\b(отказ(?:алась|ался|ались|ываюсь)?|не (?:забрал[аи]?|выкупил[аи]?|купил[аи]?|стал[аи]? брать))\b", value))
        if status == OrderStatus.RETURNED or returned:
            context = "returned"
        elif status == OrderStatus.REJECTED or refused:
            context = "rejected"
        elif status == OrderStatus.BUYOUT:
            context = "buyout"
        else:
            context = "unknown"
        low = item.rating is not None and item.rating <= 3
        cons = re.search(r"недостатки:\s*(.+)", value, flags=re.DOTALL)
        has_cons = bool(cons and cons.group(1).strip(" .!,-") not in {"нет", "нет недостатков", "не обнаружено", "не нашла", "не нашел", "отсутствуют"})
        issue = low or has_cons or bool(re.search(r"\b(брак|порван\w*|дыр\w*|не понрав\w*|не устро\w*|не соответств\w*|не совп\w*|не подош\w*|не рекоменд\w*|плох\w*|ужас\w*|маломер\w*|большемер\w*|слишком\w*|разочар\w*|воня\w*|крив\w*|не тот|пересорт|другой товар)\b", value))
        editable = self.templates.all()
        editable_key = {"size": "review_size", "defect": "review_defect", "delivery": "review_delivery"}.get(topic)
        if issue and editable_key and editable.get(editable_key):
            response = editable[editable_key]
            if context == "rejected":
                response = response.replace("Здравствуйте!", "Здравствуйте! Покупка не состоялась.", 1)
            elif context == "returned":
                response = response.replace("Здравствуйте!", "Здравствуйте! Покупка завершилась возвратом.", 1)
            return response
        thanks = "Здравствуйте! Спасибо за обратную связь."
        if context in ("rejected", "returned"):
            intro = ("Покупка не состоялась." if status == OrderStatus.REJECTED
                     else "Покупка завершилась возвратом." if status == OrderStatus.RETURNED or returned
                     else "Товар не подошёл.")
            explanations = {
                "size": "Для выбора модели рекомендуем сравнить параметры с таблицей замеров в карточке товара. При необходимости напишите нам в чат продавца — поможем сориентироваться.",
                "defect": "Передадим информацию в отдел контроля качества и проверим модель.",
                "wrong_item": "Передадим информацию в отдел контроля качества и проверим комплектацию и маркировку модели.",
                "delivery": "По условиям выдачи, примерке и доставке конкретного заказа рекомендуем обратиться в поддержку Wildberries.",
                "fabric": "При выборе другой модели рекомендуем сверить состав и характеристики ткани в карточке товара.",
                "color": "При выборе оттенка стоит учитывать освещение на фотографиях и настройки экрана.",
                "smell": "Комментарий о запахе важен для оценки качества товара.",
                "general": "Спасибо, что поделились впечатлениями о товаре.",
            }
            if not issue and context == "rejected" and item.rating and item.rating >= 4 and topic == "general":
                return "Здравствуйте! Благодарим за отзыв и высокую оценку товара! Надеемся, вы найдёте подходящий вариант."
            return f"{thanks} {intro} {explanations[topic]}"
        if issue or topic == "delivery":
            explanations = {
                "size": "Для выбора размера рекомендуем сравнить параметры с таблицей замеров в карточке товара. При необходимости напишите нам в чат продавца — поможем сориентироваться.",
                "defect": "Передадим информацию в отдел контроля качества и проверим модель.",
                "wrong_item": "Передадим информацию в отдел контроля качества и проверим комплектацию и маркировку модели.",
                "delivery": "По вопросам доставки, выдачи и примерки конкретного заказа рекомендуем обратиться в поддержку Wildberries.",
                "fabric": "Спасибо за обратную связь. Проверим, насколько понятно в карточке описаны характеристики ткани.",
                "color": "Спасибо за обратную связь. Проверим описание и фотографии модели, чтобы информация об оттенке была понятнее.",
                "smell": "Передадим информацию в отдел контроля качества и проверим модель.",
                "general": "Спасибо за подробный отзыв. Передадим замечания команде и проверим модель, если это связано с характеристиками товара.",
            }
            return f"{thanks} {explanations[topic]}"
        if item.rating is None:
            return f"{thanks} Благодарим, что поделились впечатлениями."
        return editable.get("review_positive", "Здравствуйте! Спасибо за высокую оценку и обратную связь!")

    def __init__(self, knowledge: QuestionKnowledge | None = None, products: ProductKnowledge | None = None, templates: TemplateStore | None = None):
        self.knowledge = knowledge or QuestionKnowledge()
        self.products = products or ProductKnowledge()
        self.templates = templates or TemplateStore()
