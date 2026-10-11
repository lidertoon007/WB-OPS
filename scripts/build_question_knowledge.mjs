import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const input = process.argv[2];
const output = process.argv[3] || "app/knowledge/questions.json";
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(input));
const values = workbook.worksheets.getItemAt(0).getUsedRange().values;
const headers = values[0];
const idx = Object.fromEntries(headers.map((h, i) => [h, i]));
const topic = (text) => {
  const value = String(text ?? "").toLowerCase();
  if (/размер|тал|бед|замер|обхват|ширин|длин|рост|посадк/.test(value)) return "measurements";
  if (/состав|хлоп|эластан|ткан|материал|деним|тян|стрейч/.test(value)) return "composition";
  if (/налич|будет|появит|цвет|расцвет/.test(value)) return "availability";
  if (/стир|стирать|сад|усад|глад|уход/.test(value)) return "care";
  if (/зим|лет|сезон|тепл|холод|утеп|флис/.test(value)) return "season";
  if (/цен|скид|акци|дешев|дорог/.test(value)) return "price";
  if (/карман|молни|пугов|застеж|низ|крой|фасон/.test(value)) return "details";
  if (/достав|заказ|пвз|вернут|получ/.test(value)) return "delivery";
  return "general";
};
const records = values.slice(1).map((row) => ({
  vendor_code: String(row[idx["Артикул продавца"]] ?? ""),
  nm_id: row[idx["Артикул Wildberries"]],
  topic: topic(row[idx["Текст вопроса"]]),
  question: String(row[idx["Текст вопроса"]] ?? "").trim(),
  answer: String(row[idx["Ответ"]] ?? "").trim(),
})).filter((row) => row.question && row.answer && row.answer !== "Вопрос отклонён");
await fs.mkdir("app/knowledge", { recursive: true });
await fs.writeFile(output, JSON.stringify({ source: "Вопросы за год.xlsx", records }, null, 2), "utf8");
console.log(`knowledge records: ${records.length}`);
