import csv
import json
import tempfile
import unittest
from pathlib import Path

from app.services.nomenclature import Nomenclature, NomenclatureError, norm_date

HEADER = "артикул;кратность;сс_за_ед;подготовка_за_ед;действует_с;название (справочно);бренд;категория;маркетплейсы (справочно);продано шт. с 01.2026 (справочно)\n"


class NomenclatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name) / "opiu"
        (self.dir / "Wildberries" / "cache").mkdir(parents=True)
        (self.dir / "cost.csv").write_text(HEADER + "de-90;1;1091,00;85,00;;;Deemivis;Джинсы;;\nb012;1;1041,00;85,00;;;Deemivis;Джинсы;;\n", encoding="utf-8-sig")
        (self.dir / "cost_missing.csv").write_text("артикул;кратность;сс_за_ед\nNEW-1;1;\n", encoding="utf-8-sig")
        (self.dir / "Wildberries" / "cache" / "shop_goods.json").write_text(json.dumps({
            "stocks": [["DE-90", "Склад", "mp", 10], ["DE-90", "В пути", "to_client", 5], ["NEW-1", "Склад", "mp", 3]],
            "prices": [["NEW-1", 5000, 3500, None]], "stocks_at": "2026-10-22 21:48"}), encoding="utf-8")
        cards = Path(self.temp.name) / "cards.json"
        cards.write_text(json.dumps({"cards": [{"nm": 111, "vendor": "de-90", "title": "Джинсы прямые", "brand": "Deemivis", "subject": "Джинсы", "photo": "p.webp"}],
                                     "updated_at": "2026-10-24 12:00"}), encoding="utf-8")
        self.n = Nomenclature(lambda: self.dir, cards_path=str(cards), vendor_path=str(Path(self.temp.name) / "none.json"))

    def by_key(self):
        return {i["key"]: i for i in self.n.items()["items"]}

    def test_merge_sources(self):
        items = self.by_key()
        self.assertEqual(set(items), {"DE-90", "B012", "NEW-1"})
        de = items["DE-90"]
        self.assertEqual((de["cost"], de["prep"], de["stock"], de["name"], de["nm_ids"]), (1091.0, 85.0, 10.0, "Джинсы прямые", [111]))
        self.assertFalse(items["NEW-1"]["has_cost"])
        self.assertEqual(items["NEW-1"]["price"], 3500)

    def test_edit_current_and_new_article(self):
        self.n.update_price("DE-90", prep=90)                        # регистр не важен
        self.n.update_price("NEW-1", cost=1050, prep=85)
        items = self.by_key()
        self.assertEqual(items["DE-90"]["prep"], 90.0)
        self.assertEqual(items["NEW-1"]["cost"], 1050.0)
        text = (self.dir / "cost.csv").read_text(encoding="utf-8-sig")
        self.assertIn("de-90;1;1091,00;90,00;", text)
        self.assertIn("NEW-1;1;1050,00;85,00;", text)
        self.assertNotIn("NEW-1", (self.dir / "cost_missing.csv").read_text(encoding="utf-8-sig"))   # больше не «без цены»
        self.assertTrue((self.dir / "cost.csv.bak").exists())

    def test_dated_price_and_order(self):
        self.n.update_price("de-90", cost=1200, effective_from="2099-01")
        rows = list(csv.reader((self.dir / "cost.csv").read_text(encoding="utf-8-sig").splitlines(), delimiter=";"))
        self.assertEqual([r[0] for r in rows[1:]], ["de-90", "de-90", "b012"])     # новая строка рядом со своим артикулом
        self.assertEqual(rows[2][4], "2099-01-01")
        de = self.by_key()["DE-90"]
        self.assertEqual(de["cost"], 1091.0)                          # будущая цена пока не действует
        self.assertEqual(de["future"], ["2099-01-01"])
        self.n.update_price("de-90", prep=70)                          # правится действующая (базовая) строка
        self.assertEqual(self.by_key()["DE-90"]["prep"], 70.0)
        self.n.delete_price("de-90", "2099-01")
        self.assertEqual(self.by_key()["DE-90"]["future"], [])
        with self.assertRaises(NomenclatureError):
            self.n.delete_price("de-90", "")

    def test_validation(self):
        with self.assertRaises(NomenclatureError):
            self.n.update_price("de-90", cost=-5)
        with self.assertRaises(NomenclatureError):
            norm_date("31.31.2026")
        self.assertEqual(norm_date("01.11.2026"), "2026-11-01")
        self.assertEqual(norm_date("2026-11"), "2026-11-01")

    def test_retro_bonus_by_brand(self):
        self.assertEqual(self.n.retro_rows(), [])
        self.n.set_retro("Deemivis", 5)
        self.n.set_retro(" deemivis ", 7.5, "2099-01")                    # новый процент с даты; бренд — без учёта регистра
        rows = self.n.retro_rows()
        self.assertEqual([(r["pct"], r["since"]) for r in rows], [(5.0, ""), (7.5, "2099-01-01")])
        self.assertEqual(self.n.retro_at(rows), {"DEEMIVIS": 5.0})
        self.assertEqual(self.n.retro_at(rows, "2099-02-01"), {"DEEMIVIS": 7.5})
        self.n.set_retro("Deemivis", 6)                                    # без даты — правится действующий
        self.assertEqual([(r["pct"], r["since"]) for r in self.n.retro_rows()], [(6.0, ""), (7.5, "2099-01-01")])
        self.n.set_retro("Deemivis", None, "2099-01")                      # убрать будущий
        self.assertEqual(len(self.n.retro_rows()), 1)
        self.assertEqual(self.n.items()["retro"][0]["pct"], 6.0)
        with self.assertRaises(NomenclatureError):
            self.n.set_retro("Deemivis", 150)
        text = (self.dir / "retro_bonus.csv").read_text(encoding="utf-8-sig")
        self.assertTrue(text.startswith("бренд;ретро_бонус_%;действует_с"))

    def test_filled_missing_prices_go_to_cost_csv(self):
        """Цены, вписанные в cost_missing.csv, видны панели без пересборки ОПиУ."""
        (self.dir / "cost_missing.csv").write_text(
            "артикул;кратность;сс_за_ед;подготовка_за_ед;действует_с;бренд;категория;маркетплейс (справочно);продано шт. (справочно)\n"
            "NEW-1;1;163;10;;;;Wildberries;3465\n"
            "de-90;1;999;;;;;Wildberries;1\n"          # цену из cost.csv не перетираем
            "empty;1;;;;;;Wildberries;5\n", encoding="utf-8-sig")
        rows = {r["артикул"]: r for r in self.n.read_cost_rows()[1]}
        self.assertEqual((rows["NEW-1"]["сс_за_ед"], rows["NEW-1"]["подготовка_за_ед"]), ("163", "10"))
        self.assertEqual(rows["NEW-1"]["маркетплейсы (справочно)"], "Wildberries")
        self.assertEqual(rows["de-90"]["сс_за_ед"], "1091,00")
        self.assertNotIn("empty", rows)
        self.assertEqual(self.n.missing_absorbed, 1)
        self.assertTrue(self.by_key()["NEW-1"]["has_cost"])
        mtime = (self.dir / "cost.csv").stat().st_mtime_ns
        self.n.read_cost_rows()                                  # файл не менялся — повторно не переносим
        self.assertEqual((self.dir / "cost.csv").stat().st_mtime_ns, mtime)

    def test_import_shared_cost_book(self):
        # Как файл «Себес.xlsx»: «Кратность» там — штук в упаковке, себестоимость — за всю упаковку
        table = [["Артикулы ВБ", "Наименование товара", "Себестоимость товара", "Категория WB", "Бренд", "Кратность", "ФФ"],
                 ["DE-90", "Джинсы", 1200, "Брюки", "Другой", 12, None],      # цена меняется, подготовка остаётся
                 ["b012", "", 1041, None, "Deemivis", None, 85],             # без изменений
                 ["new-1", "Новый товар", 500.5, "Корм", "Zillii", 3, 60],    # нового в cost.csv не было
                 ["X-NA", "Без цены", "#N/A", None, "Z", None, None]]
        r = self.n.import_table(table)
        self.assertEqual((r["added"], r["updated"], r["unchanged"], r["skipped"]), (1, 1, 1, ["X-NA"]))
        self.assertEqual(r["changes"], [{"article": "DE-90", "cost": [1091.0, 1200.0], "prep": [85.0, 85.0]}])
        rows = {x["артикул"]: x for x in csv.DictReader(open(self.dir / "cost.csv", encoding="utf-8-sig"), delimiter=";")}
        self.assertEqual((rows["de-90"]["сс_за_ед"], rows["de-90"]["подготовка_за_ед"], rows["de-90"]["кратность"]), ("1200,00", "85,00", "1"))
        self.assertEqual(rows["de-90"]["бренд"], "Deemivis")                    # заполненный бренд не перетираем
        self.assertEqual((rows["new-1"]["сс_за_ед"], rows["new-1"]["подготовка_за_ед"], rows["new-1"]["кратность"]), ("500,50", "60,00", "1"))
        self.assertEqual((rows["new-1"]["бренд"], rows["new-1"]["категория"], rows["new-1"]["название (справочно)"]), ("Zillii", "Корм", "Новый товар"))
        self.assertNotIn("X-NA", rows)
        self.assertNotIn("NEW-1", (self.dir / "cost_missing.csv").read_text(encoding="utf-8-sig"))   # цена есть — не «без себестоимости»
        self.assertTrue(self.by_key()["NEW-1"]["has_cost"])

    def test_import_file_xlsx_and_errors(self):
        import io
        from openpyxl import Workbook
        wb = Workbook()
        wb.active.append(["Артикул", "Себестоимость"])
        wb.active.append(["B012", 999])
        buf = io.BytesIO(); wb.save(buf)
        self.assertEqual(self.n.import_file("Себес.xlsx", buf.getvalue())["updated"], 1)
        self.assertEqual(self.n.import_file("c.csv", "Артикул;сс_за_ед\nb012;1000\n".encode())["updated"], 1)
        with self.assertRaises(NomenclatureError):
            self.n.import_file("c.csv", "Товар;Цена\nb012;1\n".encode())


if __name__ == "__main__":
    unittest.main()
