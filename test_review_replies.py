import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.models import InboxItem
from app.services import ai_writer
from app.services.replies import ReplyService
from app.services.review_composer import PhraseStore, ReviewComposer, analyze, first_name


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

    def test_service_modes(self):
        service = ReplyService(NoKnowledge(), composer=self.composer)
        item = review("Сели как влитые")
        self.assertEqual(service.draft(item).text, self.composer.compose(item)[0])
        service.review_mode = "templates"
        self.assertNotEqual(service.draft(item).text, self.composer.compose(item)[0])


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
