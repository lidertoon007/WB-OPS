// Google Apps Script: подтягивает из кабинета Ozon текущие цены, остатки
// FBS-склада и средние продажи в день за 30 дней на лист «FBS - 2% (ИУ)».
// Столбцы ищутся по заголовкам во 2-й строке, поэтому вставка/перенос
// столбцов скрипт не ломает.
// Ключи хранятся в свойствах скрипта (Настройки проекта → Свойства скрипта):
// OZON_CLIENT_ID и OZON_API_KEY.

const SHEET_NAME = 'FBS - 2% (ИУ)';
const HEADER_ROW = 2;
const FIRST_ROW = 3;
const WAREHOUSE_ID = 1020005030330030;
const ARTICLE_HEADER = 'Артикул продавца';
const PRICE_HEADER = 'Цена сейчас';
const STOCK_HEADER = 'Остаток Ozon';
const SALES_HEADER = 'Продажи';
const SALES_DAYS = 30;

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Ozon')
    .addItem('Обновить всё', 'updateOzonAll')
    .addSeparator()
    .addItem('Только цены', 'updateOzonPrices')
    .addItem('Только остатки', 'updateOzonStocks')
    .addItem('Только продажи', 'updateOzonSales')
    .addToUi();
}

function updateOzonAll() {
  const auth = ozonAuth_();
  const msgs = [];
  msgs.push(fillColumn_(PRICE_HEADER, 'Цены', fetchOzonPrices_(auth)));
  msgs.push(fillColumn_(STOCK_HEADER, 'Остатки', fetchOzonStocks_(auth)));
  msgs.push(fillSales_(auth));
  SpreadsheetApp.getActive().toast(msgs.join('\n'), 'Ozon', 15);
}

// Старое имя функции — на случай, если кнопка на листе назначена на него.
function updateOzonPricesAndStocks() {
  updateOzonAll();
}

function updateOzonPrices() {
  const msg = fillColumn_(PRICE_HEADER, 'Цены', fetchOzonPrices_(ozonAuth_()));
  SpreadsheetApp.getActive().toast(msg, 'Ozon', 15);
}

function updateOzonStocks() {
  const msg = fillColumn_(STOCK_HEADER, 'Остатки', fetchOzonStocks_(ozonAuth_()));
  SpreadsheetApp.getActive().toast(msg, 'Ozon', 15);
}

function updateOzonSales() {
  SpreadsheetApp.getActive().toast('Загружаю заказы Ozon за ' + SALES_DAYS + ' дней…', 'Ozon', 60);
  const msg = fillSales_(ozonAuth_());
  SpreadsheetApp.getActive().toast(msg, 'Ozon', 15);
}

// В ячейку пишется формула «=штук/30»: на листе видно среднее в день,
// в строке формул — сколько продано за период. Товары без продаж — 0.
function fillSales_(auth) {
  const units = fetchOzonSales_(auth);
  const values = {};
  Object.keys(units).forEach((article) => {
    values[article] = '=' + units[article] + '/' + SALES_DAYS;
  });
  return fillColumn_(SALES_HEADER, 'Продажи', values, 0);
}

function ozonAuth_() {
  const props = PropertiesService.getScriptProperties();
  const clientId = props.getProperty('OZON_CLIENT_ID');
  const apiKey = props.getProperty('OZON_API_KEY');
  if (!clientId || !apiKey) {
    throw new Error('Заполните OZON_CLIENT_ID и OZON_API_KEY в свойствах скрипта');
  }
  return { 'Client-Id': clientId, 'Api-Key': apiKey };
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
  else if (missing.length) msg += ' Нет в Ozon: ' + missing.length + ' (' + missing.slice(0, 5).join(', ') + (missing.length > 5 ? '…' : '') + ').';
  return msg;
}

function findColumn_(sheet, header) {
  const norm = (s) => String(s).replace(/\s+/g, ' ').trim().toLowerCase();
  const headers = sheet.getRange(HEADER_ROW, 1, 1, sheet.getLastColumn()).getValues()[0];
  const idx = headers.findIndex((h) => norm(h) === norm(header));
  if (idx < 0) throw new Error('Во 2-й строке нет столбца «' + header + '»');
  return idx + 1;
}

function ozonPost_(auth, path, body) {
  const resp = UrlFetchApp.fetch('https://api-seller.ozon.ru' + path, {
    method: 'post',
    contentType: 'application/json',
    headers: auth,
    payload: JSON.stringify(body),
    muteHttpExceptions: true,
  });
  const code = resp.getResponseCode();
  if (code !== 200) {
    throw new Error('Ozon ' + path + ' ответил ' + code + ': ' + resp.getContentText().slice(0, 300));
  }
  return JSON.parse(resp.getContentText());
}

function fetchOzonPrices_(auth) {
  const prices = {};
  let cursor = '';
  do {
    const data = ozonPost_(auth, '/v5/product/info/prices', { filter: { visibility: 'ALL' }, cursor: cursor, limit: 1000 });
    const items = data.items || [];
    items.forEach((item) => {
      prices[String(item.offer_id).trim()] = Number(item.price.price);
    });
    cursor = items.length ? (data.cursor || '') : '';
  } while (cursor);
  return prices;
}

// Остаток на складе WAREHOUSE_ID: present — сколько штук лежит на складе (вместе с резервом).
function fetchOzonStocks_(auth) {
  const stocks = {};
  let cursor = '';
  let hasNext = true;
  while (hasNext) {
    const data = ozonPost_(auth, '/v1/product/info/warehouse/stocks', { warehouse_id: WAREHOUSE_ID, limit: 1000, cursor: cursor });
    (data.stocks || []).forEach((s) => {
      stocks[String(s.offer_id).trim()] = Number(s.present);
    });
    cursor = data.cursor || '';
    hasNext = Boolean(data.has_next) && cursor !== '';
  }
  return stocks;
}

// Заказано штук по артикулам за последние SALES_DAYS полных дней (без сегодняшнего),
// FBS + FBO, отменённые отправления не считаются.
function fetchOzonSales_(auth) {
  const tz = 'Europe/Moscow';
  const today = new Date();
  const last = new Date(today.getTime() - 24 * 3600 * 1000);
  const first = new Date(today.getTime() - SALES_DAYS * 24 * 3600 * 1000);
  const filter = {
    since: Utilities.formatDate(first, tz, 'yyyy-MM-dd') + 'T00:00:00.000Z',
    to: Utilities.formatDate(last, tz, 'yyyy-MM-dd') + 'T23:59:59.999Z',
  };
  const units = {};
  ['/v4/posting/fbs/list', '/v3/posting/fbo/list'].forEach((path) => {
    let cursor = '';
    let hasNext = true;
    while (hasNext) {
      const data = ozonPost_(auth, path, {
        cursor: cursor,
        filter: filter,
        limit: 100,
        sort_dir: 'ASC',
        with: { analytics_data: false, financial_data: false },
      });
      const res = data.result || data;
      (res.postings || []).forEach((posting) => {
        if (posting.status === 'cancelled') return;
        (posting.products || []).forEach((p) => {
          const article = String(p.offer_id).trim();
          units[article] = (units[article] || 0) + (Number(p.quantity) || 1);
        });
      });
      cursor = res.cursor || '';
      hasNext = Boolean(res.has_next) && cursor !== '';
    }
  });
  return units;
}
