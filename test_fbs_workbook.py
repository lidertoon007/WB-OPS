from datetime import datetime
from io import BytesIO

from openpyxl import load_workbook

from app.services.fbs_workbook import HEADERS, build_fbs_workbook


def test_fbs_workbook_preserves_identifiers_and_blocks_formulas():
    rows = [{
        "warehouse": "=2+2", "report_id": "00012345678901234567890",
        "wb_product_id": "1234567890123456789012345", "article": "+SUM(1,2)",
        "quantity": 7, "pdf": "=HYPERLINK(\"https://example.org\")",
    }]
    book = load_workbook(BytesIO(build_fbs_workbook([object()], rows)))
    assert book.sheetnames == ["Сводка", "Поставки"]
    summary, sheet = book.worksheets
    assert summary["A1"].value == "Сводка поставок FBS"
    assert [summary[f"B{n}"].value for n in (3, 4, 5)] == [1, 1, 7]
    assert datetime.fromisoformat(summary["B6"].value)
    assert tuple(cell.value for cell in sheet[1]) == HEADERS
    assert [sheet.cell(2, n).value for n in (1, 2, 3, 4, 6)] == [
        "=2+2", "00012345678901234567890", "1234567890123456789012345",
        "+SUM(1,2)", '=HYPERLINK("https://example.org")',
    ]
    assert all(sheet.cell(2, n).data_type == "s" for n in (1, 2, 3, 4, 6))
    assert sheet["E2"].value == 7 and sheet["E2"].data_type == "n"
    assert sheet.freeze_panes == "A2"
    assert sheet.tables["FbsSupplyRows"].ref == "A1:F2"


def test_empty_fbs_workbook_keeps_headers_without_table():
    book = load_workbook(BytesIO(build_fbs_workbook([], [])))
    summary, sheet = book.worksheets
    assert [summary[f"B{n}"].value for n in (3, 4, 5)] == [0, 0, 0]
    assert sheet.max_row == 1
    assert tuple(cell.value for cell in sheet[1]) == HEADERS
    assert sheet.freeze_panes == "A2"
    assert not sheet.tables
