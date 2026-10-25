import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.models import InboxItem
from app.services import ai_writer
from app.services.product_models import ProductModels, pick_size
from app.services.question_composer import QuestionComposer, analyze_question
from app.services.replies import ReplyService

TABLE = [
    {"size": "32", "ru": "48", "waist": "80-84", "hips": "96-100", "inseam": "84", "bottom": "21", "height": "180-190"},
    {"size": "34", "ru": "50-52", "waist": "86-90", "hips": "100-104", "inseam": "84", "bottom": "22", "height": "180-190"},
    {"size": "36", "ru": "52-54", "waist": "92-96", "hips": "104-108", "inseam": "86", "bottom": "22", "height": "182-192"},
]


def q(text, product=111, name=None, id="q1"):
    return InboxItem(id=id, cabinet_id="main", item_type="question", text=text, product_id=product, author_name=name)


class NoKnowledge:
    records = []

    def exact_answer(self, text, product_id):
        return None


class QuestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.models = ProductModels(str(Path(self.temp.name) / "models.json"))
        self.models.save("9006", {"nm_ids": ["111", "112"], "composition": "98% хлопок, 2% эластан", "marks": "D3238A-D1", "sizes": TABLE})
        self.composer = QuestionComposer(self.models)

    def answer(self, *args, **kwargs):
        return self.composer.compose(q(*args, **kwargs))[0]

    def test_parsing(self):
        f = analyze_question(q("Здравствуйте!на рост 187 размер 32 будут не короткие?"))
        self.assertEqual((f.height, f.size), (187, "32"))
        f = analyze_question(q("ОТ-94 , ОБ-98 , рост-180 ?"))
        self.assertEqual((f.waist, f.hips, f.height), (94, 98, 180))
        f = analyze_question(q("Мне нужен размер W-36, L-34"))
        self.assertEqual((f.size, f.length_l), ("36", "L34"))
        f = analyze_question(q("нужен р.52 рост 195"))
        self.assertEqual((f.ru_size, f.height), ("52", 195))
        f = analyze_question(q("как заказать размер 33, рост 36?"))
        self.assertEqual((f.size, f.length_l), ("33", "L36"))

    def test_size_pick_by_table(self):
        text = self.answer("Добрый день. Обхват талии 84, обхват бедер 100 — какой размер?")
        self.assertTrue(text.startswith("Добрый день!"))
        self.assertIn("32 (48) размер", text)
        mixed = self.answer("талия 89 см, бёдра 106 см, рост 186")
        self.assertIn("36", mixed)
        self.assertIn("по бёдрам", mixed.lower())
        self.assertIn("По длине на рост 186 см он тоже подходит", mixed)
        self.assertEqual(pick_size({"sizes": TABLE}, 120, 130)["largest"], True)

    def test_no_invented_numbers_without_table(self):
        text = self.answer("ОТ 94, ОБ 102 какой размер?", product=999)
        for n in ("32", "34", "36", "38", "40"):
            self.assertNotIn(f" {n} размер", text)
        self.assertIn("ОТ" if False else "обхват талии 94", text)
        self.assertIn("Состав указан в карточке", self.answer("Какой состав?", product=999))

    def test_facts_and_measures(self):
        self.assertIn("Состав: 98% хлопок, 2% эластан", self.answer("какой состав ткани?"))
        self.assertIn("22 см", self.answer("Какая ширина низа у 34 размера?"))
        self.assertIn("на рост 180–190 см", self.answer("32 размер на какой рост?"))
        self.assertIn("коротковат", self.answer("на большой рост подойдут? нужен р.52 рост 195"))
        self.assertIn("артикул Wildberries 111", self.answer("На этикетке D3238A-D1 W:36 L:34, дайте ссылку"))

    def test_service_topics(self):
        self.assertIn("Wildberries", self.answer("При заказе цена 2732, в корзине 3404. Почему?"))
        self.assertIn("поддержку Wildberries", self.answer("джинсы вернула, деньги не вернули"))
        self.assertIn("Сообщить о поступлении", self.answer("будут ли ещё в наличии 48-50?"))
        self.assertIn("86 см", self.answer("Мне нужен размер W-36, L-34. Какой заказывать?", product=999))

    def test_name_and_variety(self):
        self.assertTrue(self.answer("Какой состав?", name="Духонина Алёна Викторовна").startswith("Здравствуйте, Алёна!"))
        texts = {self.answer("Здравствуйте, на рост 190 подойдут?", product=999, id=f"x{i}") for i in range(20)}
        self.assertGreater(len(texts), 1)

    def test_models_store(self):
        self.assertEqual(self.models.for_product(112)["key"], "9006")
        with self.assertRaises(ValueError):
            self.models.save("other", {"nm_ids": ["111"]})
        added = self.models.seed({"9006": {113}, "new": {114}})
        self.assertEqual(added, 2)
        self.assertIn(113, self.models.get("9006")["nm_ids"])

    def test_service_modes(self):
        service = ReplyService(NoKnowledge(), question_composer=self.composer)
        self.assertIn("32 (48)", service.draft(q("ОТ 82 ОБ 98 какой размер?")).text)
        service.question_mode = "templates"
        self.assertIn("таблицей замеров", service.draft(q("ОТ 82 ОБ 98 какой размер?")).text)


class QuestionAITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        models = ProductModels(str(Path(self.temp.name) / "models.json"))
        models.save("9006", {"nm_ids": ["111"], "sizes": TABLE})
        self.composer = QuestionComposer(models)
        self.writer = ai_writer.AIWriter(lambda: {"provider": "anthropic", "api_key": "k", "model": "m"})

    def post(self, text):
        response = mock.Mock(status_code=200, text="")
        response.json.return_value = {"content": [{"type": "text", "text": text}]}
        return mock.patch.object(ai_writer.httpx, "post", return_value=response)

    def test_ai_answer_with_table_numbers_accepted(self):
        with self.post("Здравствуйте! С обхватом талии 84 и бёдер 100 см вам подойдёт 32 размер — он рассчитан на талию 80–84 см.") as post:
            text = self.writer.write_answer(q("ОТ 84 ОБ 100 какой размер?"), self.composer, history=[("старый вопрос", "старый ответ")])
        self.assertIn("32 размер", text)
        prompt = post.call_args[1]["json"]["messages"][0]["content"]
        self.assertIn("Таблица замеров", prompt)
        self.assertIn("старый ответ", prompt)

    def test_ai_invented_numbers_rejected(self):
        service = ReplyService(NoKnowledge(), question_composer=self.composer, question_mode="ai")
        service.ai = self.writer
        with self.post("Здравствуйте! Вам подойдёт 40 размер, длина 95 см."):
            text = service.draft(q("ОТ 84 ОБ 100 какой размер?")).text
        self.assertIn("32 (48)", text)                  # откат на сборщик
        self.assertIn("числа не из данных", self.writer.last_error)


if __name__ == "__main__":
    unittest.main()
