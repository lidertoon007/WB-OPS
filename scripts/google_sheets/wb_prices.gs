// Google Apps Script: подтягивает из кабинета Wildberries текущие цены, остатки
// FBS-склада и средние продажи в день за 30 дней на лист «FBS - 2% ЕвроЗоо».
// Столбцы ищутся по заголовкам в 1-й строке, поэтому вставка/перенос
// столбцов скрипт не ломает.
// Токен WB хранится в свойствах скрипта (Настройки проекта → Свойства скрипта):
// WB_TOKEN. Нужны категории «Контент», «Цены и скидки», «Маркетплейс», «Статистика».

const SHEET_NAME = 'FBS - 2% ЕвроЗоо';
const HEADER_ROW = 1;
const FIRST_ROW = 2;
const WAREHOUSE_ID = 1854666; // FBS Москва
const ARTICLE_HEADER = 'Артикул продавца';
const PRICE_HEADER = 'Цена сейчас';
const STOCK_HEADER = 'Остаток WB';
const SALES_HEADER = 'Продажи';
const SALES_DAYS = 30;

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('WB')
    .addItem('Обновить всё', 'updateWbAll')
    .addSeparator()
    .addItem('Только цены', 'updateWbPrices')
    .addItem('Только остатки', 'updateWbStocks')
    .addItem('Только продажи', 'updateWbSales')
    .addToUi();
}

function updateWbAll() {
  const token = wbToken_();
  const msgs = [];
  msgs.push(fillColumn_(PRICE_HEADER, 'Цены', fetchWbPrices_(token)));
  msgs.push(fillColumn_(STOCK_HEADER, 'Остатки', fetchWbStocks_(token)));
  msgs.push(fillSales_(token));
  SpreadsheetApp.getActive().toast(msgs.join('\n'), 'WB', 15);
}

function updateWbPrices() {
  const msg = fillColumn_(PRICE_HEADER, 'Цены', fetchWbPrices_(wbToken_()));
  SpreadsheetApp.getActive().toast(msg, 'WB', 15);
}

function updateWbStocks() {
  SpreadsheetApp.getActive().toast('Загружаю остатки WB…', 'WB', 60);
  const msg = fillColumn_(STOCK_HEADER, 'Остатки', fetchWbStocks_(wbToken_()));
  SpreadsheetApp.getActive().toast(msg, 'WB', 15);
}

function updateWbSales() {
  SpreadsheetApp.getActive().toast('Загружаю заказы WB за ' + SALES_DAYS + ' дней…', 'WB', 60);
  const msg = fillSales_(wbToken_());
  SpreadsheetApp.getActive().toast(msg, 'WB', 15);
}

// В ячейку пишется формула «=штук/30»: на листе видно среднее в день,
// в строке формул — сколько заказано за период. Товары без заказов — 0.
function fillSales_(token) {
  const units = fetchWbSales_(token);
  const values = {};
  Object.keys(units).forEach((article) => {
    values[article] = '=' + units[article] + '/' + SALES_DAYS;
  });
  return fillColumn_(SALES_HEADER, 'Продажи', values, 0);
}

function wbToken_() {
  const token = PropertiesService.getScriptProperties().getProperty('WB_TOKEN');
  if (!token) throw new Error('Заполните WB_TOKEN в свойствах скрипта');
  return token;
}

// Записывает значения из map {артикул: значение} в столбец с заголовком header.
// Артикулам, которых нет в map, ставится missingValue; если оно не задано —
// ячейку не трогаем, в ней остаётся прежнее значение.
function fillColumn_(header, label, values, missingValue) {
  const sheet = SpreadsheetApp.getActive().getSheetByName(SHEET_NAME);
  if (!sheet) throw new Error('Не найден лист «' + SHEET_NAME + '»');
  const articleCol = findColumn_(sheet, ARTICLE_HEADER);
  const targetCol = findColumn_(sheet, header);
  const lastRow = sheet.getLastRow();
  if (lastRow < FIRST_ROW) return label + ': нет строк';
  const n = lastRow - FIRST_ROW + 1;

  const articles = sheet.getRange(FIRST_ROW, articleCol, n, 1).getValues();
  const range = sheet.getRange(FIRST_ROW, targetCol, n, 1);
  // Нетронутые ячейки записываем обратно как были: формулу — формулой, значение — значением.
  const values0 = range.getValues();
  const formulas0 = range.getFormulas();
  const current = values0.map((row, i) => (formulas0[i][0] ? [formulas0[i][0]] : row));
  let found = 0;
  const missing = [];
  const out = articles.map((row, i) => {
    const article = String(row[0]).trim();
    if (!article) return current[i];
    if (Object.prototype.hasOwnProperty.call(values, article)) {
      found++;
      return [values[article]];
    }
    missing.push(article);
    return missingValue === undefined ? current[i] : [missingValue];
  });
  range.setValues(out);

  let msg = label + ': обновлено ' + found + '.';
  if (missing.length && missingValue !== undefined) msg += ' Без данных (поставлено ' + missingValue + '): ' + missing.length + '.';
  else if (missing.length) msg += ' Нет в WB: ' + missing.length + ' (' + missing.slice(0, 5).join(', ') + (missing.length > 5 ? '…' : '') + ').';
  return msg;
}

function findColumn_(sheet, header) {
  const norm = (s) => String(s).replace(/\s+/g, ' ').trim().toLowerCase();
  const headers = sheet.getRange(HEADER_ROW, 1, 1, sheet.getLastColumn()).getValues()[0];
  const idx = headers.findIndex((h) => norm(h) === norm(header));
  if (idx < 0) throw new Error('В ' + HEADER_ROW + '-й строке нет столбца «' + header + '»');
  return idx + 1;
}

// Запрос к API WB с повтором при 429 (WB ограничивает частоту запросов).
function wbFetch_(token, method, url, body) {
  const options = { method: method, headers: { Authorization: token }, muteHttpExceptions: true };
  if (body !== undefined) {
    options.contentType = 'application/json';
    options.payload = JSON.stringify(body);
  }
  for (let attempt = 0; attempt < 8; attempt++) {
    const resp = UrlFetchApp.fetch(url, options);
    const code = resp.getResponseCode();
    if (code === 429) {
      const retry = Number(resp.getHeaders()['X-Ratelimit-Retry'] || resp.getHeaders()['x-ratelimit-retry'] || 10);
      Utilities.sleep(Math.min(Math.max(retry, 1), 65) * 1000);
      continue;
    }
    if (code !== 200) {
      throw new Error('WB ответил ' + code + ' на ' + url.split('?')[0] + ': ' + resp.getContentText().slice(0, 300));
    }
    const text = resp.getContentText();
    return text ? JSON.parse(text) : {};
  }
  throw new Error('WB ограничил частоту запросов — попробуйте через минуту');
}

// Цена со скидкой продавца (до СПП). Если у размеров разные цены — берём минимальную.
function fetchWbPrices_(token) {
  const prices = {};
  let offset = 0;
  while (true) {
    const data = wbFetch_(token, 'get',
      'https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter?limit=1000&offset=' + offset);
    const goods = (data.data && data.data.listGoods) || [];
    goods.forEach((g) => {
      const list = (g.sizes || []).map((s) => Number(s.discountedPrice)).filter((p) => !isNaN(p));
      if (list.length) prices[String(g.vendorCode).trim()] = Math.min.apply(null, list);
    });
    if (goods.length < 1000) break;
    offset += 1000;
    Utilities.sleep(700);
  }
  return prices;
}

// Остаток на складе WAREHOUSE_ID по артикулам: размеры карточек (chrtID) → остатки → сумма по артикулу.
function fetchWbStocks_(token) {
  const chrtToArticle = {};
  let cursor = { limit: 100 };
  while (true) {
    const data = wbFetch_(token, 'post', 'https://content-api.wildberries.ru/content/v2/get/cards/list?locale=ru',
      { settings: { cursor: cursor, filter: { withPhoto: -1 } } });
    const cards = data.cards || [];
    cards.forEach((card) => {
      (card.sizes || []).forEach((s) => {
        if (s.chrtID) chrtToArticle[s.chrtID] = String(card.vendorCode).trim();
      });
    });
    const next = data.cursor || {};
    if (cards.length < 100 || !next.nmID) break;
    cursor = { limit: 100, updatedAt: next.updatedAt, nmID: next.nmID };
    Utilities.sleep(700);
  }

  const stocks = {};
  Object.keys(chrtToArticle).forEach((chrt) => { stocks[chrtToArticle[chrt]] = 0; });
  const chrts = Object.keys(chrtToArticle).map(Number);
  for (let i = 0; i < chrts.length; i += 1000) {
    const data = wbFetch_(token, 'post', 'https://marketplace-api.wildberries.ru/api/v3/stocks/' + WAREHOUSE_ID,
      { chrtIds: chrts.slice(i, i + 1000) });
    (data.stocks || []).forEach((s) => {
      const article = chrtToArticle[s.chrtId];
      if (article !== undefined) stocks[article] += Number(s.amount) || 0;
    });
    Utilities.sleep(300);
  }
  return stocks;
}

// Заказано штук по артикулам за последние SALES_DAYS полных дней (без сегодняшнего),
// все склады, отменённые заказы не считаются. Одна строка отчёта WB = одна штука.
function fetchWbSales_(token) {
  const tz = 'Europe/Moscow';
  const today = new Date();
  const first = Utilities.formatDate(new Date(today.getTime() - SALES_DAYS * 24 * 3600 * 1000), tz, 'yyyy-MM-dd');
  const last = Utilities.formatDate(new Date(today.getTime() - 24 * 3600 * 1000), tz, 'yyyy-MM-dd');
  const units = {};
  let since = first;
  // Отчёт отдаёт до 80 000 строк за раз; если строк больше — продолжаем с даты последней.
  for (let page = 0; page < 20; page++) {
    const rows = wbFetch_(token, 'get',
      'https://statistics-api.wildberries.ru/api/v1/supplier/orders?flag=0&dateFrom=' + encodeURIComponent(since)) || [];
    rows.forEach((r) => {
      const day = String(r.date || '').slice(0, 10);
      if (day < first || day > last || r.isCancel) return;
      const article = String(r.supplierArticle || '').trim();
      units[article] = (units[article] || 0) + 1;
    });
    const lastChange = rows.length ? rows[rows.length - 1].lastChangeDate : null;
    if (rows.length < 80000 || !lastChange || lastChange === since) break;
    since = lastChange;
    Utilities.sleep(61000); // отчёт о заказах — 1 запрос в минуту
  }
  return units;
}
