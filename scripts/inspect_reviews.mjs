import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";
const path = process.argv[2];
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(path));
console.log((await workbook.inspect({kind:"workbook,sheet,table",maxChars:12000,tableMaxRows:8,tableMaxCols:12})).ndjson);
const sheet = workbook.worksheets.getItem("feedbacks");
const values = sheet.getRange("A1:S2416").values;
const headers = values[0];
const rows = values.slice(1);
const idx = Object.fromEntries(headers.map((h, i) => [h, i]));
const norm = (v) => String(v ?? "").trim().toLowerCase();
const countBy = (column) => rows.reduce((acc, r) => { const key = String(r[idx[column]] ?? ""); acc[key] = (acc[key] || 0) + 1; return acc; }, {});
const responseLengths = rows.map(r => String(r[idx["Ответ"]] ?? "").trim().length);
const top = (column, n=15) => Object.entries(countBy(column)).sort((a,b)=>b[1]-a[1]).slice(0,n);
const topResponses = Object.entries(rows.reduce((acc, r) => { const key = String(r[idx["Ответ"]] ?? "").trim(); acc[key] = (acc[key] || 0) + 1; return acc; }, {})).sort((a,b)=>b[1]-a[1]).slice(0,25);
const ratingResponseStats = Object.fromEntries([1,2,3,4,5].map(rating => { const subset=rows.filter(r=>Number(r[idx["Количество звезд"]])===rating); return [rating,{total:subset.length,placeholder22:subset.filter(r=>norm(r[idx["Ответ"]])==='22').length,avgLength:subset.reduce((s,r)=>s+String(r[idx["Ответ"]]??'').trim().length,0)/(subset.length||1)}]; }));
const negativeExamples = rows.filter(r=>Number(r[idx["Количество звезд"]])<=3).slice(0,35).map(r=>({rating:r[idx["Количество звезд"]],review:r[idx["Текст отзыва"]],pros:r[idx["Достоинства"]],cons:r[idx["Недостатки"]],response:r[idx["Ответ"]]}));
const topicRules = {
  "Размер/посадка": /размер|маломер|большемер|широк|узк|посадк|длин|коротк/,
  "Брак/качество": /брак|крив|порван|дыр|шов|карман|протер|качест|нитк|молни|пугов/,
  "Ткань/плотность": /тонк|плотн|ткан|материал|деним|лосин/,
  "Пересорт": /не тот|другой товар|пересорт|другая модель|не те/,
  "Цвет": /цвет|оттен|выцвет|линя/,
  "Запах": /запах|воня/,
  "Доставка/упаковка": /достав|упаков|пвз|короб|пакет/,
};
const negativeRows = rows.filter(r=>Number(r[idx["Количество звезд"]])<=3);
const topicCounts = Object.fromEntries(Object.entries(topicRules).map(([name,re])=>[name,negativeRows.filter(r=>re.test([r[idx["Текст отзыва"]],r[idx["Достоинства"]],r[idx["Недостатки"]]].map(norm).join(' '))).length]));
const phrases = ["спасибо", "рады", "благодар", "приятно", "жаль", "сожале", "рекоменду", "советуем", "качество", "размер", "достав", "цвет", "фото", "упаков", "запах"];
const responseText = rows.map(r=>norm(r[idx["Ответ"]])).join("\n");
console.log(JSON.stringify({
  total: rows.length,
  headers,
  ratings: top("Количество звезд", 10),
  brands: top("Бренд", 15),
  responses_nonempty: responseLengths.filter(x=>x>0).length,
  response_placeholder_22: rows.filter(r=>norm(r[idx["Ответ"]]) === "22").length,
  response_length: {min: Math.min(...responseLengths), max: Math.max(...responseLengths), avg: responseLengths.reduce((a,b)=>a+b,0)/responseLengths.length},
  phrase_hits: Object.fromEntries(phrases.map(p=>[p,(responseText.match(new RegExp(p,"g"))||[]).length])),
  rating_response_stats: ratingResponseStats,
  top_responses: topResponses,
  negative_examples: negativeExamples,
  negative_topic_counts: topicCounts,
  examples: rows.filter(r=>norm(r[idx["Ответ"]]) !== "22" && norm(r[idx["Ответ"]]) !== "").slice(0,12).map(r=>({rating:r[idx["Количество звезд"]], review:r[idx["Текст отзыва"]], pros:r[idx["Достоинства"]], cons:r[idx["Недостатки"]], response:r[idx["Ответ"]]}))
}, null, 2));
