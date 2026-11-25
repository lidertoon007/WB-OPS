import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.models import InboxItem
from app.services import ai_writer
from app.services.replies import ReplyService
from app.services.review_composer import DEFAULT_PHRASES, PhraseStore, ReviewComposer, analyze, first_name


class NoKnowledge:
    def exact_answer(self, text, product_id):
        return None


def review(text, rating=5, status="buyout", name="Майя", id="r1", product="Джинсы прямые классические Турция"):
    return InboxItem(id=id, cabinet_id="main", item_type="review", text=text, rating=rating,
                     order_status=status, author_name=name, product_name=product)


class ComposerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.phrases = PhraseStore(str(Path(self.temp.name) / "phrases.json"))
        self.composer = ReviewComposer(self.phrases)

    def text(self, *args, **kwargs):
        return self.composer.compose(review(*args, **kwargs))[0]

    def test_first_name(self):
        self.assertEqual(first_name("Духонина  Алёна Викторовна"), "Алёна")
        self.assertEqual(first_name("Примощенкова Валентина"), "Валентина")
        self.assertEqual(first_name("Елена Анатольевна Литвина"), "Елена")
        self.assertEqual(first_name("Шевченко"), "")
        self.assertEqual(first_name("Пользователь Wildberries"), "")
        self.assertEqual(first_name("Кунсулу"), "Кунсулу")
        self.assertEqual(first_name(None), "")

    def test_reply_reacts_to_details(self):
        facts = analyze(review("Замечательные джинсы. Сели,как влитые. Качество супер. Заказала вторую пару."))
        self.assertTrue({"fit_good", "quality_good", "repeat"} <= facts.aspects)
        text = self.text("Замечательные джинсы. Сели,как влитые. Качество супер. Заказала вторую пару.")
        self.assertTrue(text.startswith(("Здравствуйте, Майя!", "Майя, здравствуйте!")))
        self.assertRegex(text, "повторно|снова|возвращаетесь")

    def test_replies_are_not_identical(self):
        texts = {self.text("Отличные джинсы, рекомендую", id=f"r{i}", name="") for i in range(30)}
        self.assertGreater(len(texts), 5)
        # тот же отзыв — тот же текст (повторная генерация не «скачет»)
        self.assertEqual(self.text("Хорошие", id="x"), self.text("Хорошие", id="x"))

    def test_safety_for_refusal_return_unknown(self):
        samples = ["", "Всё отлично, рекомендую", "Брак, порвана ткань", "Не подошёл размер", "Сели идеально, мужу понравились",
                   "Пришло не то, подмена", "Отказ на почте", "Тонкие как лосины"]
        for status in ("rejected", "returned", "notSpecified"):
            for rating in (None, 1, 3, 5):
                for sample in samples:
                    with self.subTest(status=status, rating=rating, text=sample):
                        text = self.text(sample, rating=rating, status=status)
                        self.assertNotIn("носк", text.lower())
                        self.assertNotIn("с удовольствием", text.lower())
                        self.assertNotRegex(text.lower(), r"(оформите|оформить|можете)\s+(возврат|отказ)")
                        self.assertFalse(ReplyService(NoKnowledge()).validate(text, review(sample, rating, status)))

    def test_size_tip_is_not_a_complaint(self):
        text = self.text("Джинсы хорошего качества. Тянутся. Лучше взять на размер больше, маломерят.")
        self.assertIn("размер", text)
        self.assertNotIn("Жаль", text)
        self.assertNotIn("Жаль", self.text("Сели отлично"))
        # отказ по размеру — сочувствие и совет по размеру
        refusal = self.text("Хорошие, но маловаты, возврат", status="rejected")
        self.assertIn("замер", refusal)

    def test_delivery_goes_to_wb_support(self):
        text = self.text("Товар не увидели, пришел на почту, отказ", rating=1, status="rejected")
        self.assertIn("поддержку Wildberries", text)

    def test_payment_complaints_even_with_other_issues(self):
        for sample, status in (("С карты списали больше, хотя цена была ниже", "buyout"),
                               ("Списали за товар больше, не используя вау баллы. Обман!", "buyout"),
                               ("Вау-баллы так и не начислили", "notSpecified"),
                               ("Отказ: пришёл другой цвет. Не применили вау баллы", "rejected")):
            with self.subTest(sample=sample):
                item = review(sample, status=status)
                text, facts = self.composer.compose(item)
                self.assertTrue(facts.negative)
                self.assertEqual(text.count("поддержку Wildberries"), 1)
                self.assertFalse(ReplyService(NoKnowledge()).validate(text, item))
        for sample in ("Начислили вау-баллы, спасибо", "Цена завышена", "Списали баллы как и ожидала",
                       "Больше денег не списали"):
            self.assertNotIn("payment_issue", analyze(review(sample)).aspects)

    def test_size_mismatch_suggests_measurements_and_chat(self):
        for sample in ("Размер не подошёл", "Не соответствует размеру", "Не соответствует размерной сетке",
                       "Размерная сетка не совпадает"):
            for status in ("buyout", "rejected"):
                with self.subTest(sample=sample, status=status):
                    text = self.text(sample, status=status)
                    self.assertIn("таблиц", text)
                    self.assertIn("чат", text)
        self.assertNotIn("таблиц", self.text("Размер соответствует таблице"))

    def test_received_item_mismatch_suggests_check_before_buyout(self):
        for sample in ("Пришёл неправильный цвет", "Цвет не соответствует фото", "Товар не совпадает с описанием",
                       "Вместо брюк пришли джинсы", "Прислан темно-синий цвет, заказывал другой"):
            with self.subTest(sample=sample):
                item = review(sample, status="rejected")
                text, _ = self.composer.compose(item)
                for phrase in ("видеонаблюдением", "в пункте выдачи", "до выкупа", "зафиксировать причину"):
                    self.assertIn(phrase, text)
                self.assertFalse(ReplyService(NoKnowledge()).validate(text, item))
        for sample in ("Цвет чуть темнее", "Цвет как на фото", "Ткань выглядит дешево"):
            self.assertNotIn("видеонаблюдением", self.text(sample))

    def test_phrase_store_edit_and_reset(self):
        self.phrases.update("wish_buyout", ["Носите на здоровье!", " "])
        self.assertEqual(self.phrases.all()["wish_buyout"], ["Носите на здоровье!"])
        self.assertIn("Носите на здоровье!", self.text("Хорошие"))
        with self.assertRaises(ValueError):
            self.phrases.update("wish_other", ["Можете оформить возврат в приложении"])
        with self.assertRaises(ValueError):
            self.phrases.update("wish_other", [""])
        with self.assertRaises(KeyError):
            self.phrases.update("unknown", ["x"])
        self.phrases.reset("wish_buyout")
        self.assertGreater(len(self.phrases.all()["wish_buyout"]), 1)

    def test_new_phrases_work_with_existing_customizations(self):
        self.phrases.update("size_advice", ["Сверьте таблицу замеров и напишите нам в чат продавца."])
        self.assertIn("нам в чат продавца", self.text("Размер не подошёл"))
        self.assertIn("поддержку Wildberries", self.text("Вау-баллы не применили"))
        self.assertIn("видеонаблюдением", self.text("Прислали не тот цвет"))
        self.phrases.update("payment_issue", ["Проверьте начисление в поддержке площадки."])
        self.assertIn("начисление в поддержке площадки", self.text("Вау-баллы не применили"))
        self.assertIn("нам в чат продавца", self.text("Размер не подошёл"))

    def test_merged_color_patterns_keep_a_single_color_aspect(self):
        samples = ("Вместо темно-синих прислали черные", "Пришли не того цвета",
                   "Прислан не тот цвет", "Прислали неправильный цвет",
                   "Цвет оказался другой", "Цвет не соответствует фото",
                   "Прислан темно-синий цвет, а заказал не такой",
                   "Пришли джинсы синего цвета, заказывала иной")
        for sample in samples:
            with self.subTest(sample=sample):
                reply, facts = self.composer.compose(review(sample, rating=2, status="rejected"))
                self.assertIn("wrong_color_received", facts.aspects)
                self.assertNotIn("wrong_color_sent", facts.aspects)
                self.assertNotIn("color_diff", facts.aspects)
                self.assertEqual(reply.count("видеонаблюдением"), 1)
                self.assertEqual(reply.count("зафиксировать причину"), 1)
                self.assertIn("пересорт", reply)

    def test_other_product_is_not_a_wrong_color(self):
        for sample in ("Вместо брюк пришли джинсы", "Вместо джинсов прислали футболку",
                       "Вместо штанов выдали другую модель"):
            with self.subTest(sample=sample):
                reply, facts = self.composer.compose(review(sample, rating=2, status="rejected"))
                self.assertIn("order_mismatch", facts.aspects)
                self.assertNotIn("wrong_color_received", facts.aspects)
                self.assertEqual(reply.count("зафиксировать причину"), 1)
                self.assertNotIn("цвет не совпал", reply)

    def test_special_scenarios_override_general_mismatch(self):
        for sample, aspect in (
            ("Не соответствует описанию, состав ткани другой", "collection_variation"),
            ("Не соответствует описанию, складка спереди и дешевая ткань", "fabric_fold"),
        ):
            for status in ("buyout", "rejected"):
                with self.subTest(sample=sample, status=status):
                    reply, facts = self.composer.compose(review(sample, rating=2, status=status))
                    self.assertIn(aspect, facts.aspects)
                    self.assertNotIn("not_as_described", facts.aspects)
                    self.assertNotIn("order_mismatch", facts.aspects)
                    self.assertNotIn("видеонаблюдением", reply)
                    self.assertNotIn("фото", reply)
                    self.assertIn("коллекци" if aspect == "collection_variation" else "деним", reply)

    def test_mixed_color_and_payment_complaint_emits_each_instruction_once(self):
        for status in ("buyout", "rejected"):
            with self.subTest(status=status):
                reply, facts = self.composer.compose(review(
                    "Пришёл неправильный цвет, не соответствует фото. Вау-баллы не применили",
                    rating=2, status=status))
                self.assertTrue({"wrong_color_received", "order_mismatch", "payment_issue"} <= facts.aspects)
                self.assertEqual(reply.count("поддержку Wildberries"), 1)
                self.assertEqual(reply.count("зафиксировать причину"), 1)

    def test_service_modes(self):
        service = ReplyService(NoKnowledge(), composer=self.composer)
        item = review("Сели как влитые")
        self.assertEqual(service.draft(item).text, self.composer.compose(item)[0])
        service.review_mode = "templates"
        self.assertNotEqual(service.draft(item).text, self.composer.compose(item)[0])


    def test_wear_out_complaint_even_with_five_stars(self):
        t = ("Приобрели джинсы 5.10, успели надеть 2 раза и материал превратился в решето!!! Это что за качество ткани "
             "за такие немалые деньги??? Верните деньги и заберите свое \"изделие\"!!!")
        for rating in (5, 1):
            reply = self.text(t, rating=rating, name="Галина")
            self.assertIn("Галина", reply)
            self.assertRegex(reply, "контрол[ья] качества|проходит контроль")
            self.assertIn("30 °C", reply)
            self.assertIn("фото", reply)
            self.assertNotIn("возврат", reply.lower())
            for bad in ("Если расскажете подробнее", "Ждём вас снова", "Спасибо за честную оценку", "пять звёзд", "высокую оценку"):
                self.assertNotIn(bad, reply)

    def test_wrong_color_and_short_for_tall_rejected(self):
        reply = self.text("Джинсы хорошие, но на 188 рост коротки, и вместо синих прислали черные. Отказ.", rating=5, status="rejected", name="Лидия")
        self.assertRegex(reply, "понравил|по душе")
        self.assertRegex(reply.lower(), "пвз|пункте выдачи")
        self.assertRegex(reply.lower(), "пересорт|подмен")
        self.assertNotIn("исключена", reply.lower())
        self.assertNotIn("освещени", reply)                 # а не «оттенок на экране»
        self.assertIn("188 см", reply)
        self.assertIn("по внутреннему шву", reply)
        self.assertNotIn("обхваты талии", reply)            # про длину, а не про обхваты
        self.assertNotIn("комфортной носки", reply)

    def test_maya_collection_variation_reply(self):
        item = review("Джинсы неплохие, сели хорошо, поэтому и взяли. А так товар пришел не тот, заказывала с оранжевой отсрочкой, а пришли с черной. И состав ткани не тот. Поэтому снимаю одну звезду.", rating=4, name="Майя")
        facts = analyze(item)
        reply = self.composer.compose(item)[0]
        self.assertIn("collection_variation", facts.aspects)
        self.assertNotIn("not_as_described", facts.aspects)
        self.assertRegex(reply.lower(), "старой и новой коллекц|старой и новой коллекц")
        self.assertRegex(reply.lower(), "отделк|состав")

    def test_sergey_wrong_color_reply(self):
        item = review("Прислан темно-синий цвет, заказывал другой", rating=2, name="Сергей", id="sergey")
        facts = analyze(item)
        reply = self.composer.compose(item)[0]
        self.assertIn("wrong_color_received", facts.aspects)
        self.assertIn("ПВЗ", reply)
        self.assertRegex(reply.lower(), "отказаться|отказ")
        self.assertRegex(reply.lower(), "несоответств|брак")
        self.assertRegex(reply.lower(), "пересорт|подмен")
        self.assertNotIn("исключена", reply.lower())

    def test_natalya_knees_stretching_reply(self):
        item = review("Пока не понятно. Не облегающие. Кажется коленки начинают растягиваться потихоньку. "
                      "Или просто модель такая, не в облип. Позже будет видно.",
                      rating=3, name="Наталья", id="natalya")
        reply, facts = self.composer.compose(item)
        self.assertIn("knees_stretching", facts.aspects)
        self.assertIn("Наталья", reply)
        self.assertRegex(reply.lower(), "плотн.*(?:деним|джинсовой)|(?:деним|джинсовой).*плотн")
        self.assertRegex(reply.lower(), "не вытягивается.*колен|держит форму.*колен")
        self.assertNotIn("размер", reply.lower())

    def test_sergey_repeated_wrong_color_and_design_reply(self):
        item = review("Второй раз прислан товар другого цвета и дизайна.",
                      rating=1, status="rejected", name="Сергей", id="sergey-repeat")
        reply, facts = self.composer.compose(item)
        self.assertIn("repeated_order_mismatch", facts.aspects)
        self.assertNotIn("repeat", facts.aspects)
        self.assertIn("Сергей", reply)
        self.assertRegex(reply.lower(), "повторно|второй раз")
        for phrase in ("пункте выдачи Wildberries", "под камерами", "отказаться", "цвет", "дизайн"):
            self.assertIn(phrase, reply)
        self.assertNotIn("возвращаетесь", reply)
        self.assertFalse(ReplyService(NoKnowledge()).validate(reply, item))

    def test_anonymous_missing_fleece_reply_only_for_warm_model(self):
        sample = "никакого флиса там нет. обычные джинсы"
        item = review(sample, rating=1, status="rejected", name="Аноним", id="fleece",
                      product="Джинсы утеплённые на флисе")
        reply, facts = self.composer.compose(item)
        self.assertIn("missing_fleece", facts.aspects)
        self.assertNotIn("Аноним", reply)
        self.assertRegex(reply.lower(), "флисов(?:ая|ую) подкладк")
        for phrase in ("подмена", "под камерами", "отказаться"):
            self.assertIn(phrase, reply)
        self.assertFalse(ReplyService(NoKnowledge()).validate(reply, item))
        plain_reply, plain_facts = self.composer.compose(review(sample, rating=1, product="Джинсы обычные"))
        self.assertNotIn("missing_fleece", plain_facts.aspects)
        self.assertNotIn("флисовая подкладка", plain_reply)

    def test_danil_smell_after_washing_reply(self):
        item = review("Качество неплохое, но запах просто отвратительный. Они постирались с кондиционером "
                      "и парфюмерными гранулами но запах не выстирался",
                      rating=2, status="buyout", name="Данил", id="danil")
        reply, facts = self.composer.compose(item)
        self.assertIn("smell_after_wash", facts.aspects)
        self.assertIn("Данил", reply)
        self.assertRegex(reply.lower(), "спасибо.*качеств|благодарим.*качеств")
        self.assertRegex(reply.lower(), "окрашиван.*обработ|обработ.*окрашиван")
        self.assertRegex(reply.lower(), "после стирки.*(?:менее выраженным|ослабевает)")
        self.assertNotIn("проверим партию", reply)
        self.assertFalse(ReplyService(NoKnowledge()).validate(reply, item))
        self.assertNotIn("smell_after_wash", analyze(review("После стирки запах исчез, спасибо.")).aspects)

    def test_olga_fold_and_fabric_reply(self):
        item = review("Плохо сидят. Спереди огромная складка. Ткань дешевая", rating=2, name="Ольга", id="olga")
        facts = analyze(item)
        reply = self.composer.compose(item)[0]
        self.assertIn("fabric_fold", facts.aspects)
        self.assertIn("качественн", reply.lower())
        self.assertIn("деним", reply.lower())
        self.assertRegex(reply.lower(), "может (?:расправиться|стать менее заметным)")
        self.assertRegex(reply.lower(), "стирк|глажк")
        self.assertNotRegex(reply.lower(), r"(?:точно|обязательно) (?:уйд|исчез|расправ)")

    def test_alexander_same_size_differs_reply(self):
        item = review("Одни 52 размер — велики, сегодня другие джинсы 52 размера померил — малы. Как шьёте...", rating=2, name="Александр", id="alexander")
        facts = analyze(item)
        reply = self.composer.compose(item)[0]
        self.assertIn("size_inconsistent", facts.aspects)
        self.assertNotIn("size_small", facts.aspects)
        self.assertNotIn("size_big", facts.aspects)
        self.assertRegex(reply.lower(), "модел|парти")
        self.assertIn("1–2 см", reply)

    def test_marina_wrong_color_and_big_size_reply(self):
        item = review("Пришли не того цвета и очень большие", rating=2, name="Марина", id="marina")
        facts = analyze(item)
        reply = self.composer.compose(item)[0]
        self.assertTrue({"wrong_color_received", "size_big"} <= facts.aspects)
        self.assertIn("ПВЗ", reply)
        self.assertRegex(reply.lower(), "таблиц.*замер|замер.*таблиц")
        self.assertRegex(reply.lower(), "отказаться|отказ")

    def test_short_for_tall_buyout_is_not_a_tip(self):
        reply = self.text("Джинсы хорошие, но на 188 рост коротки.", rating=5, status="buyout")
        self.assertIn("188 см", reply)
        self.assertNotIn("совет по размеру", reply)

    def test_lada_height_limit_replies(self):
        samples = ("Маленький рост максимум 175(((", "На маленький рост максимум 175",
                   "Я не знаю на какой рост на карликов", "Маленький рост (((")
        for sample in samples:
            for status in ("buyout", "rejected"):
                with self.subTest(sample=sample, status=status):
                    item = review(sample, rating=2, status=status, name="Лада", id=sample + status)
                    reply, facts = self.composer.compose(item)
                    self.assertIn("height_limit", facts.aspects)
                    self.assertIn("Лада", reply)
                    self.assertRegex(reply.lower(), "длин.*(?:таблиц|сетк)|(?:таблиц|сетк).*длин")
                    self.assertNotRegex(reply.lower(), "обхват|маломер|карлик|175 см|видеонаблюдени")
                    self.assertFalse(ReplyService(NoKnowledge()).validate(reply, item))
        self.assertEqual(len(DEFAULT_PHRASES["height_limit"]), 2)
        self.assertNotIn("height_limit", analyze(review("Длина подошла на рост 175")).aspects)

    def test_alevtina_and_asker_declared_color_replies(self):
        for sample, name in (("Цвет совершенно не соответствует заявленному", "Алевтина"),
                             ("Цвет не соответствует по факту!", "Аскер")):
            for status in ("buyout", "rejected"):
                with self.subTest(sample=sample, status=status):
                    item = review(sample, rating=2, status=status, name=name, id=name + status,
                                  product="Джинсы синие")
                    reply, facts = self.composer.compose(item)
                    self.assertIn("declared_color_mismatch", facts.aspects)
                    self.assertNotIn("wrong_color_received", facts.aspects)
                    self.assertIn(name, reply)
                    self.assertRegex(reply.lower(), "цвет|оттенок")
                    self.assertIn("освещени", reply.lower())
                    self.assertRegex(reply.lower(), "проверим|сверим")
                    self.assertNotRegex(reply.lower(), "пересорт|подмен|пвз|видеонаблюдени|исключен")
                    self.assertFalse(ReplyService(NoKnowledge()).validate(reply, item))
        self.assertEqual(len(DEFAULT_PHRASES["declared_color_mismatch"]), 2)
        self.assertNotIn("declared_color_mismatch", analyze(review("Цвет соответствует заявленному")).aspects)

    def test_vera_waist_measurement_reply(self):
        sample = "Размерная сетка не точная. Джинсы в талии 100 см."
        for status in ("buyout", "rejected"):
            with self.subTest(status=status):
                item = review(sample, rating=2, status=status, name="Вера", id=status)
                reply, facts = self.composer.compose(item)
                self.assertIn("waist_grid_mismatch", facts.aspects)
                self.assertIn("Вера", reply)
                self.assertRegex(reply.lower(), "талии|талия")
                self.assertIn("пару сантиметров", reply)
                self.assertNotRegex(reply.lower(), "именно ваш|вашего заказа|обхват бедер|пвз|пересорт")
                self.assertFalse(ReplyService(NoKnowledge()).validate(reply, item))
        self.assertEqual(len(DEFAULT_PHRASES["waist_grid_mismatch"]), 2)
        self.assertNotIn("waist_grid_mismatch", analyze(review("Джинсы в талии 100 см, всё подошло")).aspects)

    def test_lusine_unclear_disadvantages_reply(self):
        sample = "Недостатки: 11111222222344"
        for status in ("buyout", "rejected"):
            with self.subTest(status=status):
                item = review(sample, rating=2, status=status, name="Lusine", id=status)
                reply, facts = self.composer.compose(item)
                self.assertIn("unclear_disadvantages", facts.aspects)
                self.assertIn("Lusine", reply)
                self.assertRegex(reply.lower(), "уточните|подскажите")
                self.assertNotRegex(reply.lower(), "размер|цвет|брак|возврат|видеонаблюдени")
                self.assertFalse(ReplyService(NoKnowledge()).validate(reply, item))
        self.assertEqual(len(DEFAULT_PHRASES["unclear_disadvantages"]), 2)
        self.assertNotIn("unclear_disadvantages", analyze(review("Недостатки: 123")).aspects)

    def test_new_scenario_phrase_store_override(self):
        self.phrases.update("height_limit", ["Сверьте длину в таблице замеров."])
        self.assertIn("Сверьте длину в таблице замеров.", self.text("Маленький рост (((", rating=2))

    def test_positive_reviews_are_not_complaints(self):
        for t in ("Отличные джинсы!!! Рекомендую!!!", "После 3 стирок как новые, катышков нет", "Ужасно понравились, сидят идеально"):
            facts = analyze(review(t))
            self.assertFalse(facts.aspects & {"wear_out", "angry", "quality_bad", "refund"}, t)
            self.assertFalse(facts.negative, t)

class AIWriterTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"provider": "anthropic", "api_key": "k-123", "model": "claude-test", "tone": "дружелюбный", "length": "", "use_emojis": False}
        self.writer = ai_writer.AIWriter(lambda: self.cfg)

    def fake_post(self, text, status=200):
        response = mock.Mock(status_code=status, text=json.dumps({"x": 1}))
        response.json.return_value = {"content": [{"type": "text", "text": text}]}
        return mock.patch.object(ai_writer.httpx, "post", return_value=response)

    def test_anthropic_request_and_success(self):
        with self.fake_post("Здравствуйте, Майя! Рады, что джинсы сели как влитые. Носите с удовольствием!") as post:
            text = self.writer.write(review("Сели как влитые"))
        self.assertIn("Майя", text)
        url, kwargs = post.call_args[0][0], post.call_args[1]
        self.assertTrue(url.endswith("/messages"))
        self.assertEqual(kwargs["headers"]["x-api-key"], "k-123")
        self.assertEqual(kwargs["json"]["model"], "claude-test")
        self.assertIn("Сели как влитые", kwargs["json"]["messages"][0]["content"])
        self.assertIn("Тон: дружелюбный", kwargs["json"]["messages"][0]["content"])

    def test_prompt_contains_new_complaint_rules(self):
        prompt = ai_writer.SYSTEM_PROMPT.lower()
        self.assertIn("старой и новой коллекций", prompt)
        self.assertIn("несоответствие/брак", prompt)
        self.assertIn("качественного денима", prompt)
        self.assertIn("может расправиться", prompt)
        self.assertIn("1–2 см", prompt)

    def test_rejects_unsafe_answer_and_falls_back(self):
        service = ReplyService(NoKnowledge(), review_mode="ai")
        service.ai = self.writer
        item = review("Не подошёл размер, отказ", status="rejected")
        with self.fake_post("Здравствуйте! Жаль. Желаем комфортной носки!"):
            text = service.draft(item).text
        self.assertNotIn("носк", text)
        self.assertIn("отклонён", self.writer.last_error)

    def test_network_error_falls_back(self):
        service = ReplyService(NoKnowledge(), review_mode="ai")
        service.ai = self.writer
        with mock.patch.object(ai_writer.httpx, "post", side_effect=OSError("нет сети")):
            text = service.draft(review("Хорошие")).text
        self.assertTrue(text.startswith(("Здравствуйте", "Майя")))
        self.assertIn("нет сети", self.writer.last_error)

    def test_no_key_no_request(self):
        self.cfg["api_key"] = ""
        with mock.patch.object(ai_writer.httpx, "post") as post:
            self.assertIsNone(self.writer.write(review("Хорошие")))
        post.assert_not_called()

    def test_openai_picks_text_model_and_retries_new_params(self):
        self.cfg.update(provider="openai", model="", base_url="")
        models = mock.Mock(status_code=200)
        models.json.return_value = {"data": [{"id": "whisper-1"}, {"id": "text-embedding-3-small"}, {"id": "gpt-4o-mini"}, {"id": "gpt-4o-realtime"}]}
        old_params = mock.Mock(status_code=400, text='{"error":"Unsupported parameter: max_tokens"}')
        ok = mock.Mock(status_code=200, text="")
        ok.json.return_value = {"choices": [{"message": {"content": "Здравствуйте, Майя! Рады, что джинсы сели как влитые. Носите с удовольствием!"}}]}
        with mock.patch.object(ai_writer.httpx, "get", return_value=models), \
             mock.patch.object(ai_writer.httpx, "post", side_effect=[old_params, ok]) as post:
            text = self.writer.write(review("Сели как влитые"))
        self.assertIn("Майя", text)
        first, second = post.call_args_list[0][1]["json"], post.call_args_list[1][1]["json"]
        self.assertEqual(first["model"], "gpt-4o-mini")
        self.assertTrue(post.call_args_list[0][0][0].startswith("https://api.openai.com/v1/chat/completions"))
        self.assertIn("max_completion_tokens", second)
        self.assertNotIn("temperature", second)

    def test_describe_hides_key(self):
        self.assertEqual(ai_writer.describe(self.cfg)["key_hint"], "…-123")
        self.assertNotIn("api_key", ai_writer.describe(self.cfg))


if __name__ == "__main__":
    unittest.main()
