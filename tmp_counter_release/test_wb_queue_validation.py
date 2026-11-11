import importlib.util
import unittest
from pathlib import Path


module_path = Path(__file__).parent / "release/app/services/wb_client.py"
spec = importlib.util.spec_from_file_location("staged_wb_client", module_path)
wb_client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wb_client)


class Response:
    status_code = 200
    headers = {}
    text = ""

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class Client:
    def __init__(self, payload):
        self.payload = payload

    async def get(self, path, params):
        return Response(self.payload)


class QueueValidationTests(unittest.TestCase):
    @staticmethod
    def run_coroutine(coroutine):
        try:
            coroutine.send(None)
        except StopIteration as result:
            return result.value
        finally:
            coroutine.close()

    def test_bad_entry_aborts_snapshot(self):
        payload = {"data": {"feedbacks": [{"id": "valid"}, {"text": "missing id"}]}}
        with self.assertRaisesRegex(wb_client.WBApiError, "некорректную запись"):
            self.run_coroutine(wb_client.WBFeedbackClient("token")._get_all(
                Client(payload), "/api/v1/feedbacks", "feedbacks"))

    def test_bad_response_shape_aborts_snapshot(self):
        payload = {"data": {"feedbacks": {"id": "wrong type"}}}
        with self.assertRaisesRegex(wb_client.WBApiError, "неожиданный формат"):
            self.run_coroutine(wb_client.WBFeedbackClient("token")._get_all(
                Client(payload), "/api/v1/feedbacks", "feedbacks"))


if __name__ == "__main__":
    unittest.main()
