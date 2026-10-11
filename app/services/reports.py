from hashlib import sha256
from pathlib import Path
import re
import csv

import pdfplumber

from app.models import ReportFile, ReportRow


class ReportImporter:
    """Indexes FBS supply reports without mixing in Honest Sign documents."""

    def __init__(self, root: str):
        self.root = Path(root)

    def scan(self) -> list[ReportFile]:
        result: list[ReportFile] = []
        if not self.root.exists():
            return result
        # FBS files are intentionally read from a separate folder. Supported
        # exports are indexed even when their table format is not recognised;
        # this lets the UI show the file and report an extraction issue.
        allowed = {".pdf", ".csv", ".xlsx", ".xls"}
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in allowed:
                continue
            digest = sha256(path.read_bytes()).hexdigest()
            report_id = path.stem
            warehouse = path.parent.name or None
            result.append(
                ReportFile(
                    path=str(path),
                    filename=path.name,
                    report_id=report_id,
                    warehouse=warehouse,
                    sha256=digest,
                    size_bytes=path.stat().st_size,
                )
            )
        return result

    def rows(self) -> list[ReportRow]:
        """Extract the stable numeric table columns from signed PDFs.

        Product names are intentionally not used for identity: the WB product
        identifier and article are the stable fields for reconciliation.
        """
        result: list[ReportRow] = []
        for report in self.scan():
            suffix = Path(report.path).suffix.lower()
            if suffix == ".csv":
                result.extend(self._csv_rows(report))
                continue
            if suffix in {".xlsx", ".xls"}:
                result.extend(self._spreadsheet_rows(report))
                continue
            try:
                with pdfplumber.open(report.path) as document:
                    text = "\n".join(page.extract_text() or "" for page in document.pages)
            except Exception:
                continue
            for line in text.splitlines():
                match = re.match(r"^\s*(\d{6,})\s+(.+?)\s+(\d+)\s*$", line)
                if not match or not report.report_id or not report.warehouse:
                    continue
                result.append(
                    ReportRow(
                        report_id=report.report_id,
                        warehouse=report.warehouse,
                        wb_product_id=match.group(1),
                        article=match.group(2).strip(),
                        quantity=int(match.group(3)),
                    )
                )
        return result

    @staticmethod
    def _column(row: dict[str, object], *names: str) -> object | None:
        normalized = {re.sub(r"[^a-zа-я0-9]", "", str(k).lower()): v for k, v in row.items()}
        for name in names:
            key = re.sub(r"[^a-zа-я0-9]", "", name.lower())
            for actual, value in normalized.items():
                if key in actual or actual in key:
                    return value
        return None

    def _dict_row(self, report: ReportFile, row: dict[str, object]) -> ReportRow | None:
        product = self._column(row, "wb_id", "артикул wb", "артикулwb", "нмц", "nm_id", "номер номенклатуры", "товар")
        quantity = self._column(row, "количество", "кол-во", "qty", "шт")
        article = self._column(row, "артикул", "vendor_code", "код") or ""
        try:
            product_text = str(product or "").strip()
            quantity_int = int(float(str(quantity or "0").replace(",", ".")))
        except (TypeError, ValueError):
            return None
        if not product_text.isdigit() or quantity_int < 0:
            return None
        return ReportRow(report_id=report.report_id or report.filename, warehouse=report.warehouse or "", wb_product_id=product_text, article=str(article).strip(), quantity=quantity_int)

    def _csv_rows(self, report: ReportFile) -> list[ReportRow]:
        rows: list[ReportRow] = []
        try:
            with open(report.path, "r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle, delimiter=";")
                for row in reader:
                    item = self._dict_row(report, row)
                    if item:
                        rows.append(item)
        except (OSError, UnicodeDecodeError):
            return []
        return rows

    def _spreadsheet_rows(self, report: ReportFile) -> list[ReportRow]:
        try:
            from openpyxl import load_workbook
            book = load_workbook(report.path, read_only=True, data_only=True)
            sheet = book.active
            values = list(sheet.values)
            if not values:
                return []
            headers = [str(value or "") for value in values[0]]
            return [item for raw in values[1:] if (item := self._dict_row(report, dict(zip(headers, raw))))]
        except Exception:
            return []
