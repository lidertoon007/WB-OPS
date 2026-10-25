"""Налог с фактической цены покупки (после скидок МП), а не с цены продавца.

Запуск из папки проекта: python tests/test_tax_purchase_base.py
"""
from datetime import date
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(name, path):
    spec = spec_from_file_location(name, path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- Wildberries: база = retailAmount («Вайлдберриз реализовал»), возврат уменьшает базу
wb = load("wb_finance_test", ROOT / "Wildberries" / "wb_finance.py")
wb_rows = [
    {"rrDate": "2026-09-01", "vendorCode": "A", "sellerOperName": "Продажа",
     "docTypeName": "Продажа", "quantity": 1, "retailPriceWithDisc": 1000,
     "retailAmount": 800, "forPay": 700, "acquiringFee": 20, "srid": "S1"},
    {"rrDate": "2026-09-02", "vendorCode": "A", "sellerOperName": "Возврат",
     "docTypeName": "Возврат", "quantity": 1, "retailPriceWithDisc": 500,
     "retailAmount": 400, "forPay": 350, "acquiringFee": 10, "srid": "R1"},
]
*_, wb_base, wb_articles = wb.aggregate(wb_rows, {"2026-09"})
assert wb_base["2026-09"] == 400, wb_base
assert wb_articles[("2026-09", "A")] == 400

# --- Ozon: база = sale_price; возврат (sale_amount < 0) уменьшает базу; начисления без движения товара не считаются
ozon = load("ozon_finance_test", ROOT / "Ozon" / "ozon_finance.py")
sale = {"posting": {"posting_number": "P1", "products": [{"sku": 1, "commission": {
    "sale_amount": 1000, "sale_price": 800, "seller_price": 1000, "commission": -200}}]}, "total_amount": 800}
ret = {"posting": {"posting_number": "P1", "products": [{"sku": 1, "commission": {
    "sale_amount": -1000, "sale_price": 800, "seller_price": 1000, "commission": 200}}]}, "total_amount": -800}
service = {"posting": {"posting_number": "P1", "products": [{"sku": 1, "commission": {
    "sale_amount": 0, "sale_price": 800}}]}, "total_amount": -50}
*_, ozon_base, ozon_articles = ozon.aggregate([(date(2026, 9, 1), [sale, sale, ret, service])], {}, {1: "A"})
assert ozon_base["2026-09"] == 800, ozon_base
assert ozon_articles[("2026-09", "A")] == 800

# --- Яндекс Маркет: база = цена с учётом скидок
ym = load("ym_finance_test", ROOT / "YandexMarket" / "ym_finance.py")
realization = [({}, "2026-09", {
    "delivered": [{"orderId": "S1", "yourSku": "A", "deliveryDate": "2026-09-01",
                   "deliveredPriceSumWithVatAndNoDiscounts": 1000,
                   "deliveredPriceSumWithVatAndDiscounts": 800, "deliveredCount": 1}],
    "returned": [{"orderId": "R1", "yourSku": "A", "returnWarehouseOrScAcceptDate": "2026-09-02",
                  "returnPriceSumWithVatAndNoDiscounts": 500,
                  "returnPriceSumWithVatAndDiscounts": 400, "returnedCount": 1}],
})]
*_, ym_base, ym_articles = ym.aggregate([], realization, [], {"2026-09"})
assert ym_base["2026-09"] == 400, ym_base
assert ym_articles[("2026-09", "A")] == 400

# --- Свод: налог считается от tax_base, а не от выручки по цене продавца; прогноз сохраняет базу
opiu = load("opiu_all_test", ROOT / "opiu_all.py")
entity = {"mp": "Wildberries", "cabinet": "К1", "tax_base": {"2026-09": 400.0},
          "table": {opiu.REV_SALES: {"x": {"2026-09": 1500.0}}, opiu.REV_RETURNS: {"x": {"2026-09": -500.0}}}}
tax, used = opiu.apply_tax([entity], {("Wildberries", None): 6.0}, {"2026-09"})
assert round(tax[("Wildberries", "К1")]["2026-09"], 2) == -24.0, tax
old = dict(entity, tax_base=None)   # старый кэш без базы — запасной вариант: выручка по цене продавца
tax, _ = opiu.apply_tax([old], {("Wildberries", None): 6.0}, {"2026-09"})
assert round(tax[("Wildberries", "К1")]["2026-09"], 2) == -60.0, tax
dump = {"marketplace": "Wildberries", "months": ["2026-09-01", "2026-09-20"],
        "cabinets": {"К1": {"rows": [], "sales": [], "tax_base": {"2026-09-01": 1.0, "2026-09-20": 2.0}}}}
assert opiu.day_slice(dump, "2026-09-10")["cabinets"]["К1"]["tax_base"] == {"2026-09-20": 2.0}

print("Налоговая база: WB=400, Ozon=800, Яндекс Маркет=400, свод и прогноз — OK")
