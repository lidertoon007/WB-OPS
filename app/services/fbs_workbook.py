"""Build the FBS supply export without temporary files or a Node process."""

from datetime import UTC, datetime
from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.table import Table, TableStyleInfo

HEADERS = ("Склад", "Номер отчёта", "WB ID товара", "Артикул", "Количество", "Файл")
TEXT_FIELDS = ("warehouse", "report_id", "wb_product_id", "article", "pdf")


def _text(cell: Any, value: object) -> None:
    """Keep identifiers intact and prevent spreadsheet formula interpretation."""
    cell.value = "" if value is None else str(value)
    cell.data_type = "s"


def build_fbs_workbook(reports: list[object], rows: list[dict[str, object]]) -> bytes:
    book = Workbook()
    summary = book.active
    summary.title = "Сводка"
    sheet = book.create_sheet("Поставки")
    summary.sheet_view.showGridLines = False
    sheet.sheet_view.showGridLines = False

    teal = PatternFill("solid", fgColor="0F766E")
    pale = PatternFill("solid", fgColor="CCFBF1")
    white = "FFFFFF"
    border = Border(bottom=Side(style="thin", color="D1D5DB"))

    summary.merge_cells("A1:D1")
    summary["A1"] = "Сводка поставок FBS"
    summary["A1"].fill = teal
    summary["A1"].font = Font(name="Arial", size=14, bold=True, color=white)
    summary["A1"].alignment = Alignment(vertical="center")
    for column in summary[1][:4]:
        column.fill = teal
    summary.row_dimensions[1].height = 26
    metrics = (
        ("Файлов отчётов", len(reports)),
        ("Строк поставок", len(rows)),
        ("Количество единиц", sum(int(row["quantity"]) for row in rows)),
        ("Сформировано", datetime.now(UTC).isoformat().replace("+00:00", "Z")),
    )
    for number, (label, value) in enumerate(metrics, start=3):
        summary.cell(number, 1, label)
        summary.cell(number, 2, value)
        summary.cell(number, 1).fill = pale
        summary.cell(number, 1).font = Font(name="Arial", size=11, bold=True, color="115E59")
        summary.cell(number, 2).font = Font(name="Arial", size=11)
    summary.column_dimensions["A"].width = 24
    summary.column_dimensions["B"].width = 27

    for col, label in enumerate(HEADERS, start=1):
        cell = sheet.cell(1, col, label)
        cell.fill = teal
        cell.font = Font(name="Arial", size=11, bold=True, color=white)
    sheet.row_dimensions[1].height = 22
    sheet.freeze_panes = "A2"

    widths = [len(label) for label in HEADERS]
    for number, row in enumerate(rows, start=2):
        for col, field in enumerate(TEXT_FIELDS[:4], start=1):
            value = row.get(field)
            _text(sheet.cell(number, col), value)
            widths[col - 1] = max(widths[col - 1], len(str(value or "")))
        quantity = sheet.cell(number, 5, int(row["quantity"]))
        widths[4] = max(widths[4], len(str(quantity.value)))
        value = row.get("pdf")
        _text(sheet.cell(number, 6), value)
        widths[5] = max(widths[5], len(str(value or "")))
        for cell in sheet[number][:6]:
            cell.font = Font(name="Arial", size=10)
            cell.border = border

    for col, width in zip("ABCDEF", widths):
        sheet.column_dimensions[col].width = min(max(width + 2, 12), 60)
    if rows:
        table = Table(displayName="FbsSupplyRows", ref=f"A1:F{len(rows) + 1}")
        table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
        sheet.add_table(table)

    output = BytesIO()
    book.save(output)
    return output.getvalue()
