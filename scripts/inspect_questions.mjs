import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";
const path = process.argv[2];
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(path));
console.log((await workbook.inspect({kind:"workbook,sheet,table",maxChars:12000,tableMaxRows:8,tableMaxCols:16})).ndjson);
for (const sheet of workbook.worksheets.items) {
  const used = sheet.getUsedRange();
  const values = used.values;
  console.log(JSON.stringify({sheet:sheet.name, headers:values[0], sample:values.slice(1,6)}));
  const headers=values[0]; const rows=values.slice(1); const idx=Object.fromEntries(headers.map((h,i)=>[h,i]));
  const norm=v=>String(v??'').trim().toLowerCase();
  const rules={
    "Размер/замеры":/размер|тал|бед|замер|обхват|ширин|длин|рост|посадк/,
    "Состав/ткань":/состав|хлоп|эластан|ткан|материал|деним|тян|стрейч/,
    "Наличие":/налич|будет|появит|законч|другой цвет|расцвет/,
    "Уход/стирка":/стир|стирать|сад|усад|глад|уход/,
    "Сезон/тепло":/зим|лет|сезон|тепл|холод|утеп|флис/,
    "Цена/скидка":/цен|скид|акци|дешев|дорог/,
    "Детали модели":/карман|молни|пугов|застеж|низ|крой|фасон/,
    "Доставка/заказ":/достав|заказ|пвз|вернут|получ/,
  };
  const topics=Object.fromEntries(Object.entries(rules).map(([k,re])=>[k,rows.filter(r=>re.test(norm(r[idx['Текст вопроса']]))).length]));
  const rejected=rows.filter(r=>norm(r[idx['Ответ']]).includes('отклон')).length;
  const avg=rows.reduce((s,r)=>s+String(r[idx['Ответ']]??'').trim().length,0)/(rows.length||1);
  const short=rows.filter(r=>String(r[idx['Ответ']]??'').trim().length<25).length;
  console.log(JSON.stringify({total:rows.length,topics,rejected,avg_response_length:avg,short_responses:short},null,2));
}
