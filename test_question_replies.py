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


NEW_MERCHANT_CASES = [
    ("здравствуйте. на какой рост идут брюки?", ["сравнить длину", "Напишите ваш рост и размер"]),
    ("Добрый день покупал у вас джинсы На этикетке указанно: &#x20;", ["Не совсем поняли", "Уточните"]),
    ("Здравствуйте, помогите мне правильно подобрать размер, я измерила мужа сантиметром, обхват талии под животиком 116,обхват бедер 119,какой размер подойдет, если джинсы на резинке, они должны растягиваться и сужаться?", ["таблицей размеров", "конкретный размер назвать нельзя"]),
    ("Здравствуйте. На рост 198 какой размер подойдет?", ["обхват талии и бёдер", "длину по внутреннему шву"]),
    ("Добрый день подскажите мне нужны джинсы у вас вроде покупал. На этикетке DEEMIVIS D3248A-D1 размер W:36. L:34", ["историю покупок", "ранее заказанного товара"]),
    ("Здравствуйте, серые джинсы. У мужа по бёдрам 106 мм. рост 176. Какой надо размер.", ["обхват талии", "таблицей размеров"]),
    ("нужны вмеру светлые джинсы с карманами из плотной ткани (не синтетика), такие есть?", ["наш магазин", "98% хлопок", "2% эластан"]),
    ("Здравствуйте. Будет ли еще поступление 38 размер?", ["карточке товара", "каждую неделю"]),
    ("Размер 36 на какой рост?", ["длину по внутреннему шву", "таблице"]),
    ("какая плотность джинс?", ["классических", "400–475 г/м²"]),
    ("в размере 33 какая длина?", ["замер конкретного размера", "таблице"]),
    ("Здравствуйте! Подскажите пожалуйста, 32 размер какая длина по шву?", ["замер конкретного размера", "таблице"]),
    ("на какой сезон расчитаны данные джинсы летом в них не будет жарко при +24", ["+24 °C", "жарковато", "индивидуальных ощущений"]),
    ("Здравствуйте подскажите пожалуйста какой размер мне точно подойдёт артикул джинс 922327358. Да и джинса тонкая сразу беру только с размером помогите пожалуйста. Обхват талии на уровне пупка 84 см Обхват бёдер по выступам ягодиц 97 см", ["таблицей размеров", "конкретный размер назвать нельзя"]),
    ("Добрый день! 56р подойдут на рост 190?", ["Пришлите", "обхват талии и бёдер"]),
    ("Джоггеры - это увеличенная длина сидения?", ["свободного кроя", "не является обязательной"]),
    ("Скажите какой ПРОЦЕНТНЫЙ состав материала брюк", ["98% хлопок, 2% эластан"]),
    ("ЗАТРУДНЯЮСЬ, НА РОСТ 168 СМ, ОБХВАТ ТАЛИИ 83,5 СМ, ОБХВАТ БЁДЕР 93 СМ КАКОЙ РАЗМЕР РЕКОМЕНДУЕТЕ С УЧЁТОМ УСАДКИ ПОСЛЕ СТИРКИ", ["таблицей размеров", "конкретный размер назвать нельзя", "стир"]),
    ("Здравствуйте. будут ещё такие джинсы?", ["карточке товара", "каждую неделю"]),
    ("здравствуй, в описании указано что джинсы без застежки - на фото пуговица и молния, фактически есть застёжка?", ["пуговица и молния"]),
    ("Здравствуйте, подскажите на рост 192 какой размер Подойдет", ["По одному росту 192", "обхват талии и бёдер"]),
    ("Заказали джинсы сегодня должны прийти цена 2505 а на сайте уже 3026", ["стоимость фиксируется", "момент покупки"]),
    ("Джогеры на рост 195 подойдут ? &#x20;", ["По одному росту 195", "обхват талии и бёдер"]),
]


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
        self.assertIn("98% хлопок, 2% эластан", self.answer("Какой состав?", product=999))

    def test_facts_and_measures(self):
        self.assertIn("Состав: 98% хлопок, 2% эластан", self.answer("какой состав ткани?"))
        self.assertIn("22 см", self.answer("Какая ширина низа у 34 размера?"))
        self.assertIn("на рост 180–190 см", self.answer("32 размер на какой рост?"))
        self.assertIn("коротковат", self.answer("на большой рост подойдут? нужен р.52 рост 195"))
        self.assertIn("артикул Wildberries 111", self.answer("На этикетке D3238A-D1 W:36 L:34, дайте ссылку"))

    def test_service_topics(self):
        self.assertIn("Wildberries", self.answer("При заказе цена 2732, в корзине 3404. Почему?"))
        self.assertIn("поддержку Wildberries", self.answer("джинсы вернула, деньги не вернули"))
        self.assertIn("каждую неделю", self.answer("будут ли ещё в наличии 48-50?"))
        self.assertIn("86 см", self.answer("Мне нужен размер W-36, L-34. Какой заказывать?", product=999))

    def test_fresh_price_availability_and_size_guidance(self):
        price = self.answer("Почему цена в карточке 3085, а в корзине 3985?", product=999)
        self.assertIn("корзине", price)
        self.assertIn("разных размеров", price)
        self.assertIn("фиксируется", price)
        available = self.answer("Когда появится 48 размер?", product=999)
        self.assertIn("карточке товара", available)
        self.assertIn("каждую неделю", available)
        self.assertIn("точную дату", available)
        unknown = self.answer("ОТ 104 ОБ 113 рост 166, какой размер?", product=999)
        self.assertIn("таблицей размеров именно этой модели", unknown)
        self.assertNotRegex(unknown, r"рекомендуем W?\d{2}|подойд[её]т W?\d{2}")
        too_large = self.answer("ОТ 120 ОБ 130, какой размер?", product=111)
        self.assertIn("нельзя уверенно рекомендовать размер", too_large)
        self.assertNotIn("самый подходящий", too_large)

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

    def test_merchant_question_examples(self):
        cases = [
            ("почему так долго идёт товар?", ["Логистикой", "поддержку"]),
            ("здравствуйте, размер 48-50 на какой рост максимум?", ["длину по внутреннему шву", "таблицей"]),
            ("Ширинка есть или зашита?", ["стандартная ширинка на молнии"]),
            ("скажите пожалуйста когда такая модель появится в продаже?", ["карточке товара", "каждую неделю"]),
            ("добрый день. так они на резинке и с молнией? без застежки и пуговицы, только на резинке с верёвкой нет? &#x20;", ["другая модель", "пока её нет", "Подпишитесь"]),
            ("Здравствуйте!Какая модель подойдет на 1,70?", ["рост 170 см", "обхват талии и бёдер"]),
            ("На рост 170 какая модель, нужны прямые или чуть зауженные к низу и пожалуйста состав в % можно узнать", ["98% хлопок, 2% эластан"]),
            ("Здравствуйте, джинсы артикул 955536606, на рост 185-187см. подойдут? Они на резинке?", ["подойдёт на рост 185–187", "Наличие резинки"]),
            ("Здравствуйте.А почему обхват талии 33 размера меньше,чем 34???", ["допуски замеров 1–2 см"]),
            ("Здравствуйте. Джинсы действительно широкие или нет. На фото обычные?", ["актуальный фасон"]),
            ("добрый вечер.Подскажите пожалуйста,какой нужен размер брюк,если талия 80, рост170. Нужны летние джинсы.", ["таблицей размеров", "обхват бёдер"]),
            ("Подскажите пожалуйста какой размер заказать? Мои данные: талия- 99 см, длина - 106 см, бедра - 100 см.", ["таблицей размеров", "конкретный размер назвать нельзя"]),
            ("Здравствуйте. Джинсы арт. 922327365 какая плотность, самые самые тонкие нужны", ["тонкие летние", "хорошо подойдут"]),
            ("Здравствуйте! арт. 922327387 джинсы летние темно- синий цвет или Меланж? Напишите состав в процентах сколько хлопка и сколько синтетических. За ранее благодарю за ответ.", ["Цвет — синий", "98% хлопок, 2% эластан"]),
            ("Здравствуйте! Меня интересуют летние джинсы цвет синий или темно синий 34,36 размер. Но цвет должен быть ярким. Заказывала у Вас летние джинсы синего цвета, пришли цвет как будто старые стираные", ["Понимаем", "освещения", "отзывы покупателей"]),
            ("Вы мне ответите или нет насчет 38р", ["каждую неделю"]),
            ("Здравствуйте. Артикул 922327355 38р будет в продаже", ["каждую неделю"]),
            ("Подскажите какие id (или как ещё найти) прямых тонких летних джинс (цвет любой) имеют рост L32?", ["названиях товаров", "Длина зависит", "L32"]),
            ("есть такие,голубые?", ["нашем магазине", "актуальные варианты"]),
            ("Здравствуйте ,Рост 170, ОТ-96, ОБ-103 какой размер выбрать ?", ["таблицей размеров", "конкретный размер назвать нельзя"]),
        ]
        for question, fragments in cases:
            with self.subTest(question=question):
                text = self.answer(question, product=999, name="Аноним")
                for fragment in fragments:
                    self.assertIn(fragment, text)
                self.assertNotRegex(text.lower(), r"wildberries|вайлд|\bвб\b|\bwb\b")
                self.assertNotIn("Уточните, пожалуйста, что именно", text)
                self.assertNotIn("обхват талии 33 см", text)

    def test_new_merchant_question_examples(self):
        cases = NEW_MERCHANT_CASES
        for question, fragments in cases:
            with self.subTest(question=question):
                text = self.answer(question, product=999, name="Аноним")
                for fragment in fragments:
                    self.assertIn(fragment, text)
                self.assertNotRegex(text.lower(), r"wildberries|вайлд|\bвб\b")

    def test_new_guidance_preserves_model_data_and_other_topics(self):
        self.models.save("9006", {**self.models.get("9006"), "density": "220 г/м²", "season": "Летние", "fastener": "Без застёжки"})
        self.assertIn("220 г/м²", self.answer("Какая плотность джинс?"))
        self.assertIn("Без застёжки", self.answer("В описании указано что джинсы без застежки, на фото пуговица и молния, фактически есть застёжка?"))
        self.assertIn("84 см", self.answer("32 размер какая длина по шву?"))
        self.assertNotIn("W44–W46", self.answer("Талия 116 бедра 119 какой размер?"))
        self.models.save("summer", {"nm_ids": [777], "season": "Летние"})
        self.assertNotIn("400–475", self.answer("Какая плотность?", product=777))
        for question in ("Будет ли поступление 38 размера? Какой состав?", "На какой рост идут брюки? Какой состав?", "На этикетке указано: какой состав?"):
            self.assertIn("98% хлопок, 2% эластан", self.answer(question, product=999))

    def test_merchant_guidance_respects_model_data_and_product_scope(self):
        text = self.answer("Рост 170, ОТ-96, ОБ-103 какой размер выбрать?")
        self.assertIn("36", text)
        self.assertNotIn("W38 L30", text)
        self.assertNotIn("тонкие летние", self.answer("Какая плотность? Самые тонкие нужны", product=999))
        self.models.save("other", {"nm_ids": [922327387], "composition": "100% хлопок"})
        self.assertIn("100% хлопок", self.answer("Какой состав?", product=922327387))
        self.assertNotIn("2% эластан", self.answer("Какой состав?", product=922327387))

    def test_merchant_guidance_keeps_other_questions_and_requires_specific_variant(self):
        self.models.save("9006", {**self.models.get("9006"), "fastener": "Пояс на резинке, без пуговицы"})
        for question in ("Они только на резинке?", "Они без пуговицы?", "На резинке со шнурком?"):
            with self.subTest(question=question):
                self.assertNotIn("другая модель", self.answer(question))
                self.assertNotIn("пока её нет", self.answer(question))
        for question in ("Почему так долго идёт товар? Какой состав?", "Когда модель появится в продаже? Какой состав?",
                         "Ширинка есть или зашита? Какой состав?"):
            with self.subTest(question=question):
                text = self.answer(question)
                self.assertIn("98% хлопок, 2% эластан", text)


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
