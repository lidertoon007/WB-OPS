import fs from "node:fs/promises";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const [inputPath, outputPath] = process.argv.slice(2);
if (!inputPath || !outputPath) throw new Error("Usage: export_honest_sign.mjs input.json output.xlsx");
const payload = JSON.parse(await fs.readFile(inputPath, "utf8"));
const workbook = Workbook.create();
const summary = workbook.worksheets.add("Сводка");
const rowsSheet = workbook.worksheets.add("Товары");
summary.showGridLines = false;
rowsSheet.showGridLines = false;
summary.getRange("A1:D1").values = [["Отчёт Честного знака", "", "", ""]];
summary.mergeCells("A1:D1");
summary.getRange("A3:B6").values = [
  ["PDF-отчётов", payload.reports.length],
  ["Строк товаров", payload.rows.length],
  ["Количество единиц", payload.rows.reduce((sum, row) => sum + row.quantity, 0)],
  ["Сформировано", new Date().toISOString()],
];
summary.getRange("A1:D1").format = { fill: "#5B21B6", font: { name: "Arial", size: 14, bold: true, color: "#FFFFFF" } };
summary.getRange("A3:A6").format = { fill: "#F3E8FF", font: { name: "Arial", bold: true, color: "#4C1D95" } };
summary.getRange("A1:D6").format.font = { name: "Arial", size: 11 };
summary.getRange("A1:D1").format.font = { name: "Arial", size: 14, bold: true, color: "#FFFFFF" };
summary.getRange("A1:D6").format.borders = { preset: "outside", style: "thin", color: "#DDD6FE" };
summary.getRange("A:A").format.columnWidth = 24;
summary.getRange("B:B").format.columnWidth = 20;

rowsSheet.getRange("A1:F1").values = [["Склад", "Номер отчёта", "WB ID товара", "Артикул", "Количество", "PDF"]];
rowsSheet.getRange("A2:F" + (payload.rows.length + 1)).values = payload.rows.map((row) => [row.warehouse, row.report_id, row.wb_product_id, row.article, row.quantity, row.pdf]);
rowsSheet.getRange("A1:F1").format = { fill: "#5B21B6", font: { name: "Arial", size: 11, bold: true, color: "#FFFFFF" } };
rowsSheet.getRange("A1:F" + (payload.rows.length + 1)).format.font = { name: "Arial", size: 10 };
rowsSheet.getRange("E2:E" + (payload.rows.length + 1)).format.numberFormat = "#,##0";
rowsSheet.getRange("A1:F" + (payload.rows.length + 1)).format.borders = { preset: "inside", style: "thin", color: "#E5E7EB" };
rowsSheet.getRange("A:F").format.autofitColumns();
rowsSheet.freezePanes.freezeRows(1);
rowsSheet.tables.add("A1:F" + (payload.rows.length + 1), true, "HonestSignRows");
workbook.recalculate();
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
