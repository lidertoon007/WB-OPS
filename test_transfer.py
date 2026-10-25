import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from app.services.cabinets import Cabinet
from app.services.transfer import common
from app.services.transfer.common import TransferError, find_values, norm
from app.services.transfer.service import TransferService
from app.storage import DraftStore

WB_CARD = {
    "nmID": 466345357, "subjectID": 180, "subjectName": "Джинсы", "vendorCode": "3140-D1", "brand": "Deemivis",
    "title": "Джинсы мужские прямые Турция", "description": "Джинсы мужские прямые deemivis.",
    "photos": [{"big": "https://basket/1.webp"}, {"big": "https://basket/2.webp"}],
    "dimensions": {"width": 30, "height": 3, "length": 40, "weightBrutto": 0.53},
    "characteristics": [{"id": 14177451, "name": "Страна производства", "value": ["Турция"]}, {"id": 14177449, "name": "Цвет", "value": ["синий"]},
                        {"id": 14177450, "name": "Состав", "value": ["хлопок", "эластан"]}, {"id": 16, "name": "Пол", "value": ["Мужской"]}],
    "sizes": [{"chrtID": 1, "techSize": "32 акц", "wbSize": "48", "skus": ["2000000000001"]},
              {"chrtID": 2, "techSize": "32", "wbSize": "48", "skus": ["2000000000002"]},
              {"chrtID": 3, "techSize": "34", "wbSize": "50", "skus": ["2000000000003"]}],
}
OZ_ATTRS = [
    {"id": 85, "name": "Бренд", "dictionary_id": 28732849, "is_required": True},
    {"id": 4180, "name": "Название", "type": "String", "is_required": False},
    {"id": 4191, "name": "Аннотация", "type": "String"},
    {"id": 9048, "name": "Название модели (для объединения в одну карточку)", "type": "String", "is_required": True},
    {"id": 10096, "name": "Цвет товара", "dictionary_id": 1, "is_collection": True, "is_required": True},
    {"id": 4389, "name": "Страна-изготовитель", "dictionary_id": 2, "is_collection": True},
    {"id": 4295, "name": "Российский размер", "dictionary_id": 3, "is_required": True},
    {"id": 9533, "name": "Размер производителя", "type": "String"},
    {"id": 22232, "name": "ТН ВЭД коды ЕАЭС", "dictionary_id": 4, "is_required": True},
    {"id": 8229, "name": "Тип", "dictionary_id": 5, "is_required": True},
]
OZ_DICT = {85: ["Deemivis", "Нет бренда"], 10096: ["синий", "черный"], 4389: ["Турция", "Китай"], 4295: ["48", "50", "52"],
           22232: ["6203423100 - Брюки и бриджи мужские или для мальчиков из денима, или джинсовой ткани"]}
WB_CHARCS = [{"charcID": 14177449, "name": "Цвет", "required": True, "charcType": 1, "maxCount": 3},
             {"charcID": 14177451, "name": "Страна производства", "required": False, "charcType": 1, "maxCount": 1},
             {"charcID": 54337, "name": "Размер", "required": False, "charcType": 4},
             {"charcID": 90, "name": "Длина изделия", "required": False, "charcType": 4, "unitName": "см"},
             {"charcID": 16, "name": "Пол", "required": True, "charcType": 1, "maxCount": 1}]
YM_PARAMS = [{"id": 1, "name": "Цвет товара", "type": "ENUM", "required": True, "values": [{"id": 11, "value": "синий"}]},
             {"id": 2, "name": "Размер", "type": "TEXT", "required": True},
             {"id": 3, "name": "Пол", "type": "ENUM", "required": True, "values": [{"id": 31, "value": "мужской"}]},
             {"id": 4, "name": "Вес", "type": "NUMERIC", "required": False, "unit": {"defaultUnitId": 9}}]


class Fake:
    """Маршрутизатор запросов к WB, Ozon и Маркету."""

    def __init__(self):
        self.calls, self.imported, self.wb_created = [], [], []

    def __call__(self, request: httpx.Request):
        url, path = str(request.url), request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        j = lambda data, code=200: httpx.Response(code, json=data)           # noqa: E731
        # ---------------- WB
        if path == "/content/v2/get/cards/list":
            q = body["settings"]["filter"]["textSearch"]
            cards = [WB_CARD] if q in ("3140-D1", "466345357") else []
            cards += [{"nmID": 900, "vendorCode": v["vendorCode"]} for c in self.wb_created for v in c["variants"] if v["vendorCode"] == q]
            return j({"cards": cards})
        if path == "/api/v2/list/goods/filter":
            return j({"data": {"listGoods": [{"nmID": 466345357, "sizes": [{"techSizeName": "32", "price": 9000, "discountedPrice": 4500},
                                                                           {"techSizeName": "34", "price": 9000, "discountedPrice": 4500}]}]}})
        if path.startswith("/content/v2/object/charcs/"):
            return j({"data": WB_CHARCS})
        if path == "/content/v2/object/all":
            return j({"data": [{"subjectID": 180, "subjectName": "Джинсы", "parentName": "Одежда"}, {"subjectID": 181, "subjectName": "Джоггеры", "parentName": "Одежда"}]})
        if path == "/content/v2/barcodes":
            return j({"data": [f"20999{i:08d}" for i in range(body["count"])]})
        if path == "/content/v2/cards/upload":
            self.wb_created += body
            return j({"error": False, "data": None})
        if path in ("/content/v3/media/save", "/api/v2/upload/task", "/content/v2/cards/error/list"):
            return j({"data": {}, "error": False})
        # ---------------- Ozon
        if path == "/v1/description-category/tree":
            return j({"result": [{"category_name": "Одежда", "description_category_id": 200000933, "children": [
                {"type_id": 93080, "type_name": "Джинсы"}, {"type_id": 93144, "type_name": "Легинсы"}]}]})
        if path == "/v1/description-category/attribute":
            return j({"result": OZ_ATTRS})
        if path == "/v1/description-category/attribute/values/search":
            vals = OZ_DICT.get(body["attribute_id"], [])
            return j({"result": [{"id": 1000 + i, "value": v} for i, v in enumerate(vals) if norm(body["value"])[:4] in norm(v)]})
        if path == "/v3/product/import":
            self.imported += body["items"]
            return j({"result": {"task_id": 77}})
        if path == "/v1/product/import/info":
            return j({"result": {"items": [{"offer_id": i["offer_id"], "status": "imported", "product_id": 5000 + n, "errors": []} for n, i in enumerate(self.imported)]}})
        if path == "/v1/product/import/prices":
            return j({"result": [{"offer_id": p["offer_id"], "updated": True} for p in body["prices"]]})
        if path == "/v3/product/list":
            offers = ["OZ-1_48", "OZ-1_50", "OTHER"]
            if body["filter"].get("offer_id"):
                return j({"result": {"items": [{"offer_id": o} for o in offers if o in body["filter"]["offer_id"]]}})
            return j({"result": {"items": [{"offer_id": o} for o in offers], "last_id": ""}})
        if path == "/v3/product/info/list":
            return j({"items": [{"offer_id": o, "name": "Джинсы Ozon", "price": "5000", "old_price": "8000", "barcodes": [f"46{o[-2:]}"],
                                 "description_category_id": 200000933, "type_id": 93080, "id": 1} for o in body["offer_id"]]})
        if path == "/v4/product/info/attributes":
            return j({"result": [{"offer_id": o, "name": "Джинсы Ozon", "description_category_id": 200000933, "type_id": 93080,
                                  "depth": 400, "width": 300, "height": 30, "dimension_unit": "mm", "weight": 530, "weight_unit": "g",
                                  "primary_image": "https://oz/1.jpg", "images": ["https://oz/1.jpg", "https://oz/2.jpg"], "barcodes": ["4600"],
                                  "attributes": [{"id": 85, "values": [{"value": "Deemivis", "dictionary_value_id": 1000}]},
                                                 {"id": 4191, "values": [{"value": "Описание"}]}, {"id": 9048, "values": [{"value": "OZ-1"}]},
                                                 {"id": 10096, "values": [{"value": "синий", "dictionary_value_id": 1000}]},
                                                 {"id": 4295, "values": [{"value": o[-2:], "dictionary_value_id": 1001}]},
                                                 {"id": 9533, "values": [{"value": "W" + o[-2:]}]}]} for o in body["filter"]["offer_id"]], "last_id": ""})
        # ---------------- Маркет
        if "partner.market" in url:
            if path == "/v2/campaigns/555":
                return j({"campaign": {"id": 555, "business": {"id": 777}}})
            if path == "/v2/categories/tree":
                return j({"result": {"id": 1, "name": "Все товары", "children": [{"id": 10, "name": "Одежда", "children": [{"id": 100, "name": "Джинсы мужские"}]}]}})
            if path == "/v2/category/100/parameters":
                return j({"result": {"parameters": YM_PARAMS}})
            if path == "/v2/businesses/777/offer-mappings/update":
                return j({"status": "OK", "results": [{"offerId": m["offer"]["offerId"], "errors": [], "warnings": []} for m in body["offerMappings"]]})
        return j({"error": f"не замокано: {path}"}, 404)


def cab(cid, mp, token="t", client_id=""):
    return Cabinet(id=cid, name=cid, token=token, marketplace=mp, client_id=client_id)


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DraftStore(f"sqlite:///{Path(self.temp.name) / 'db.sqlite3'}")
        self.fake = Fake()
        real = httpx.Client
        for p in (mock.patch.object(common.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(self.fake), timeout=5)),
                  mock.patch("time.sleep", lambda *_: None)):
            p.start()
            self.addCleanup(p.stop)
        self.cabs = [cab("wb1", "wildberries"), cab("wb2", "wildberries"), cab("oz", "ozon", client_id="123"), cab("ym", "yandex", client_id="555"),
                     cab("oz-nokey", "ozon", token="")]
        self.t = TransferService(self.store, lambda: self.cabs, Path(self.temp.name) / "transfer")

    def wait(self, run_id):
        import time as _t
        for _ in range(3000):
            j = self.t.get(run_id)
            if j["status"] != "running":
                return j
            _t.perf_counter()
            threading_yield()
        self.fail("задание не завершилось")

    def test_cabinets_and_seed(self):
        cl = {c["id"]: c for c in self.t.cabinet_list()}
        self.assertTrue(cl["wb1"]["ready"])
        self.assertEqual(cl["oz-nokey"]["reason"], "нет ключа API")
        s = self.t.settings()
        self.assertEqual(s["targets"]["ozon"]["price_multiplier"], 1.5)
        self.assertIn("Джинсы", s["targets"]["ozon"]["extras"])

    def test_wb_to_ozon(self):
        job = self.wait(self.t.start("wb1", "oz", ["3140-D1", "нет-такого"])["id"])
        items = {i["offer_id"] or i["status"]: i for i in job["items"]}
        self.assertEqual(items["error"]["message"], "артикул не найден в кабинете-источнике")
        self.assertEqual(sum(1 for i in job["items"] if i["status"] == "skipped"), 1)            # «32 акц»
        it = items["3140-D1_32"]
        self.assertEqual(it["status"], "ready", it["missing"])
        p = it["payload"]
        self.assertEqual((p["price"], p["old_price"], p["type_id"], p["depth"], p["weight"]), ("6750", "15000", 93080, 400, 530))
        attrs = {a["id"]: a["values"] for a in p["attributes"]}
        self.assertEqual(attrs[4295][0]["value"], "48")
        self.assertEqual(attrs[9048][0]["value"], "3140-D1")
        self.assertEqual(attrs[22232][0]["value"], "6203423100 - Брюки и бриджи мужские или для мальчиков из денима, или джинсовой ткани")           # из значений категории
        self.assertEqual(attrs[85][0]["value"], "Deemivis")
        self.t.upload(job["id"], None)
        job = self.wait(job["id"])
        self.assertEqual(sorted(i["offer_id"] for i in job["items"] if i["status"] == "uploaded"), ["3140-D1_32", "3140-D1_34"])
        self.assertEqual(len(self.fake.imported), 2)
        self.assertTrue(any(c[1] == "/v1/product/import/prices" for c in self.fake.calls))
        self.assertEqual(self.t.history()[0]["counts"]["uploaded"], 2)

    def test_missing_extra_auto_and_category(self):
        cfg = self.t.settings()
        cfg["targets"]["ozon"]["extras"] = {}
        self.t.save_settings(cfg)
        job = self.wait(self.t.start("wb1", "oz", ["3140-D1"], auto=True)["id"])
        it = next(i for i in job["items"] if i["offer_id"] == "3140-D1_32")
        self.assertEqual(it["status"], "missing")                       # авто-выгрузки нет, пока не хватает данных
        self.assertIn("ТН ВЭД коды ЕАЭС", it["missing"])
        self.assertEqual(self.fake.imported, [])
        self.t.set_extra("ozon", "Джинсы", "ТН ВЭД коды ЕАЭС", "6203423100")
        job = self.wait(self.t.start("wb1", "oz", ["3140-D1"], auto=True)["id"])
        self.assertTrue(all(i["status"] in ("uploaded", "skipped") for i in job["items"]), [i["status"] for i in job["items"]])
        self.t.set_category("ozon", "wildberries:Джинсы", {"id": "200000933:93144", "name": "Легинсы", "description_category_id": 200000933, "type_id": 93144})
        self.assertEqual(self.t.settings()["targets"]["ozon"]["categories"]["wildberries:Джинсы"]["name"], "Легинсы")

    def test_wb_to_wb_new_barcodes(self):
        job = self.wait(self.t.start("wb1", "wb2", ["3140-D1"])["id"])
        it = job["items"][0]
        self.assertEqual(it["status"], "ready", it["missing"])
        v = it["payload"]["variants"][0]
        self.assertEqual(it["payload"]["subjectID"], 180)
        self.assertEqual({c["id"] for c in v["characteristics"]}, {14177451, 14177449, 14177450, 16})
        self.assertEqual([s["techSize"] for s in v["sizes"]], ["32", "34"])
        self.assertEqual(v["sizes"][0]["price"], 9000)
        self.t.upload(job["id"], None)
        job = self.wait(job["id"])
        sent = self.fake.wb_created[0]["variants"][0]
        self.assertTrue(all(s["skus"][0].startswith("20999") for s in sent["sizes"]))
        self.assertEqual(job["items"][0]["status"], "uploaded", job["items"][0]["message"])
        self.assertTrue(any(c[1] == "/content/v3/media/save" for c in self.fake.calls))
        task = next(c[2] for c in self.fake.calls if c[1] == "/api/v2/upload/task")
        self.assertEqual(task["data"][0]["discount"], 50)

    def test_ozon_to_wb_and_wb_to_ym(self):
        job = self.wait(self.t.start("oz", "wb2", ["OZ-1"])["id"])
        it = job["items"][0]
        self.assertEqual(it["offer_id"], "OZ-1")
        v = it["payload"]["variants"][0]
        self.assertEqual([s["techSize"] for s in v["sizes"]], ["W48", "W50"])
        self.assertEqual([s["wbSize"] for s in v["sizes"]], ["48", "50"])
        chars = {c["id"]: c["value"] for c in v["characteristics"]}
        self.assertEqual(chars[14177449], ["синий"])
        self.assertIn("Пол", it["missing"])                                              # в Ozon-карточке пола нет
        self.assertEqual(v["dimensions"], {"length": 40, "width": 30, "height": 3, "weightBrutto": 0.53})

        job = self.wait(self.t.start("wb1", "ym", ["3140-D1"])["id"])
        it = next(i for i in job["items"] if i["offer_id"] == "3140-D1_34")
        self.assertEqual(it["status"], "ready", it["missing"])
        o = it["payload"]
        self.assertEqual(o["marketCategoryId"], 100)
        pv = {p["parameterId"]: p for p in o["parameterValues"]}
        self.assertEqual((pv[1]["valueId"], pv[2]["value"], pv[3]["valueId"]), (11, "34", 31))
        self.assertEqual(o["basicPrice"], {"value": 4500, "currencyId": "RUR", "discountBase": 9000})
        self.assertEqual(o["manufacturerCountries"], ["Турция"])
        self.t.upload(job["id"], None)
        job = self.wait(job["id"])
        self.assertTrue(all(i["status"] in ("uploaded", "skipped") for i in job["items"]))

    def test_guards(self):
        with self.assertRaises(TransferError):
            self.t.start("wb1", "wb1", ["x"])
        with self.assertRaises(TransferError):
            self.t.start("wb1", "oz-nokey", ["x"])
        with self.assertRaises(TransferError):
            self.t.start("wb1", "oz", ["  "])

    def test_find_values(self):
        chars = [{"name": "Страна производства", "values": ["Турция"]}, {"name": "Цвет", "values": ["синий"]}]
        self.assertEqual(find_values("Страна-изготовитель", chars), ["Турция"])
        self.assertEqual(find_values("Цвет товара", chars), ["синий"])
        self.assertIsNone(find_values("Сезон", chars))


def threading_yield():
    import threading
    threading.Event().wait(0.005)


if __name__ == "__main__":
    unittest.main()
