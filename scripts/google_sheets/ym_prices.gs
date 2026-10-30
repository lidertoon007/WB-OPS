// Google Apps Script: подтягивает из кабинета Яндекс Маркета текущие цены, остатки
// и средние продажи в день за 30 дней на лист «Юнит 2%».
// Столбцы ищутся по заголовкам во 2-й строке, поэтому вставка/перенос
// столбцов скрипт не ломает.
// Ключ хранится в свойствах скрипта (Настройки проекта → Свойства скрипта): YM_API_KEY.
// Ключ создаётся в кабинете продавца: Настройки → API и модули. Нужны доступы к ценам,
// остаткам и заказам/статистике.

const SHEET_NAME = 'Юнит 2%';
const HEADER_ROW = 2;
const FIRST_ROW = 3;
const ARTICLE_HEADER = 'Артикул продавца';
const PRICE_HEADER = 'Цена сейчас';
const STOCK_HEADERS = ['Остаток ЯМ', 'Остаток Ozon']; // берётся первый найденный на листе
const SALES_HEADER = 'Продажи';
const SALES_DAYS = 30;
// Пусто — все магазины кабинета (цены, продажи). Иначе id магазинов (кампаний), например [12345678].
const CAMPAIGN_IDS = [];
// Остаток берётся только с этого магазина (по названию, как в кабинете). Пусто — со всех магазинов.
const STOCK_SHOP_NAME = 'FBS Москва';
// Пусто — остаток суммируется по всем складам магазина. Иначе id складов.
const WAREHOUSE_IDS = [];
const API = 'https://api.partner.market.yandex.ru';

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('ЯМ')
    .addItem('Обновить всё', 'updateYmAll')
    .addSeparator()
    .addItem('Только цены', 'updateYmPrices')
    .addItem('Только остатки', 'updateYmStocks')
    .addItem('Только продажи', 'updateYmSales')
    .addSeparator()
    .addItem('Показать магазины и склады', 'showYmShops')
    .addToUi();
}

function updateYmAll() {
  const key = ymKey_();
  const shops = ymShops_(key);
  const msgs = [];
  msgs.push(fillColumn_([PRICE_HEADER], 'Цены', fetchYmPrices_(key, shops)));
  msgs.push(fillColumn_(STOCK_HEADERS, 'Остатки', fetchYmStocks_(key, shops)));
  msgs.push(fillSales_(key, shops));
  SpreadsheetApp.getActive().toast(msgs.join('\n'), 'ЯМ', 15);
}

function updateYmPrices() {
  const key = ymKey_();
  const msg = fillColumn_([PRICE_HEADER], 'Цены', fetchYmPrices_(key, ymShops_(key)));
  SpreadsheetApp.getActive().toast(msg, 'ЯМ', 15);
}

function updateYmStocks() {
  const key = ymKey_();
  const msg = fillColumn_(STOCK_HEADERS, 'Остатки', fetchYmStocks_(key, ymShops_(key)));
  SpreadsheetApp.getActive().toast(msg, 'ЯМ', 15);
}

function updateYmSales() {
  SpreadsheetApp.getActive().toast('Загружаю заказы ЯМ за ' + SALES_DAYS + ' дней…', 'ЯМ', 60);
  const key = ymKey_();
  const msg = fillSales_(key, ymShops_(key));
  SpreadsheetApp.getActive().toast(msg, 'ЯМ', 15);
}

// Список магазинов и их складов — чтобы выбрать CAMPAIGN_IDS / WAREHOUSE_IDS.
function showYmShops() {
  const key = ymKey_();
  const lines = [];
  ymShops_(key, true).forEach((shop) => {
    lines.push('Магазин ' + shop.id + ' «' + (shop.domain || '') + '», модель ' + (shop.placementType || '?') +
      ', кабинет ' + ((shop.business || {}).id || '?'));
    const res = ymPost_(key, '/v2/campaigns/' + shop.id + '/offers/stocks', {}, { limit: 1 }).result || {};
    (res.warehouses || []).forEach((w) => lines.push('    склад ' + w.warehouseId));
  });
  SpreadsheetApp.getUi().alert('Магазины Яндекс Маркета', lines.join('\n') || 'Магазинов не найдено', SpreadsheetApp.getUi().ButtonSet.OK);
}

// В ячейку пишется формула «=штук/30»: на листе видно среднее в день,
// в строке формул — сколько заказано за период. Товары без заказов — 0.
function fillSales_(key, shops) {
  const units = fetchYmSales_(key, shops);
  const values = {};
  Object.keys(units).forEach((article) => {
    values[article] = '=' + units[article] + '/' + SALES_DAYS;
  });
  return fillColumn_([SALES_HEADER], 'Продажи', values, 0);
}

function ymKey_() {
  const key = PropertiesService.getScriptProperties().getProperty('YM_API_KEY');
  if (!key) throw new Error('Заполните YM_API_KEY в свойствах скрипта');
  return key;
}

// Записывает значения из map {артикул: значение} в столбец с одним из заголовков headers.
// Артикулам, которых нет в map, ставится missingValue; если оно не задано —
// ячейку не трогаем, в ней остаётся прежнее значение.
function fillColumn_(headers, label, values, missingValue) {
  const sheet = SpreadsheetApp.getActive().getSheetByName(SHEET_NAME);
  if (!sheet) throw new Error('Не найден лист «' + SHEET_NAME + '»');
  const articleCol = findColumn_(sheet, [ARTICLE_HEADER]);
  const targetCol = findColumn_(sheet, headers);
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
  else if (missing.length) msg += ' Нет в ЯМ: ' + missing.length + ' (' + missing.slice(0, 5).join(', ') + (missing.length > 5 ? '…' : '') + ').';
  return msg;
}

function findColumn_(sheet, headers) {
  const norm = (s) => String(s).replace(/\s+/g, ' ').trim().toLowerCase();
  const row = sheet.getRange(HEADER_ROW, 1, 1, sheet.getLastColumn()).getValues()[0].map(norm);
  for (const header of headers) {
    const idx = row.indexOf(norm(header));
    if (idx >= 0) return idx + 1;
  }
  throw new Error('Во ' + HEADER_ROW + '-й строке нет столбца «' + headers.join('» / «') + '»');
}

// Запрос к API Маркета с повтором при превышении лимита (420/429).
function ymRequest_(key, method, path, body, params) {
  let url = API + path;
  if (params) {
    const q = Object.keys(params).filter((k) => params[k] !== undefined && params[k] !== '')
      .map((k) => k + '=' + encodeURIComponent(params[k])).join('&');
    if (q) url += '?' + q;
  }
  const options = { method: method, headers: { 'Api-Key': key }, muteHttpExceptions: true };
  if (body !== undefined) {
    options.contentType = 'application/json';
    options.payload = JSON.stringify(body);
  }
  for (let attempt = 0; attempt < 6; attempt++) {
    const resp = UrlFetchApp.fetch(url, options);
    const code = resp.getResponseCode();
    if (code === 420 || code === 429) {
      Utilities.sleep(10000 * (attempt + 1));
      continue;
    }
    if (code !== 200) {
      const hint = { 401: ' — неверный ключ', 403: ' — у ключа нет нужного доступа' }[code] || '';
      throw new Error('Маркет ответил ' + code + hint + ' на ' + path + ': ' + resp.getContentText().slice(0, 300));
    }
    const text = resp.getContentText();
    return text ? JSON.parse(text) : {};
  }
  throw new Error('Маркет ограничил частоту запросов — попробуйте через минуту');
}

function ymPost_(key, path, body, params) {
  return ymRequest_(key, 'post', path, body, params);
}

// Магазины (кампании) кабинета; если задан CAMPAIGN_IDS — только они.
function ymShops_(key, all) {
  const shops = [];
  let token = '';
  for (let i = 0; i < 100; i++) {
    const res = ymRequest_(key, 'get', '/v2/campaigns', undefined, { limit: 50, pageToken: token });
    (res.campaigns || []).forEach((c) => shops.push(c));
    token = (res.paging || {}).nextPageToken || '';
    if (!token) break;
  }
  if (all || !CAMPAIGN_IDS.length) return shops;
  return shops.filter((s) => CAMPAIGN_IDS.indexOf(Number(s.id)) >= 0);
}

// Обход страниц ответа Маркета (page_token).
function ymPaged_(key, path, body, limit, take) {
  let token = '';
  for (let i = 0; i < 2000; i++) {
    const res = ymPost_(key, path, body, { limit: limit, page_token: token }).result || {};
    take(res);
    token = (res.paging || {}).nextPageToken || '';
    if (!token) break;
  }
}

// Текущая цена (со скидкой) по артикулам: базовые цены кабинета; если их нет — цены магазинов.
function fetchYmPrices_(key, shops) {
  const prices = {};
  const take = (res) => (res.offers || []).forEach((o) => {
    const value = Number((o.price || {}).value);
    if (value) prices[String(o.offerId).trim()] = value;
  });
  const businesses = {};
  shops.forEach((s) => { if ((s.business || {}).id) businesses[s.business.id] = true; });
  Object.keys(businesses).forEach((id) => ymPaged_(key, '/v2/businesses/' + id + '/offer-prices', {}, 200, take));
  if (!Object.keys(prices).length) {
    shops.forEach((s) => ymPaged_(key, '/v2/campaigns/' + s.id + '/offer-prices', {}, 200, take));
  }
  return prices;
}

// Годный остаток (FIT) по артикулам: склады магазина STOCK_SHOP_NAME (или WAREHOUSE_IDS).
function fetchYmStocks_(key, shops) {
  let stockShops = shops;
  if (STOCK_SHOP_NAME) {
    const want = STOCK_SHOP_NAME.toLowerCase().trim();
    stockShops = shops.filter((s) => String(s.domain || s.name || '').toLowerCase().trim() === want);
    if (!stockShops.length) {
      stockShops = shops.filter((s) => String(s.domain || s.name || '').toLowerCase().indexOf(want) >= 0);
    }
    if (!stockShops.length) {
      throw new Error('Не найден магазин «' + STOCK_SHOP_NAME + '». Магазины кабинета: ' +
        shops.map((s) => (s.domain || s.name || '?') + ' (' + s.id + ')').join(', '));
    }
  }
  const stocks = {};
  stockShops.forEach((s) => ymPaged_(key, '/v2/campaigns/' + s.id + '/offers/stocks', {}, 100, (res) => {
    (res.warehouses || []).forEach((w) => {
      if (WAREHOUSE_IDS.length && WAREHOUSE_IDS.indexOf(Number(w.warehouseId)) < 0) return;
      (w.offers || []).forEach((o) => {
        const article = String(o.offerId).trim();
        const fit = (o.stocks || []).filter((st) => st.type === 'FIT')
          .reduce((sum, st) => sum + (Number(st.count) || 0), 0);
        stocks[article] = (stocks[article] || 0) + fit;
      });
    });
  }));
  return stocks;
}

// Заказано штук по артикулам за последние SALES_DAYS полных дней (без сегодняшнего),
// по всем выбранным магазинам; отменённые заказы не считаются.
function fetchYmSales_(key, shops) {
  const tz = 'Europe/Moscow';
  const today = new Date();
  const body = {
    dateFrom: Utilities.formatDate(new Date(today.getTime() - SALES_DAYS * 24 * 3600 * 1000), tz, 'yyyy-MM-dd'),
    dateTo: Utilities.formatDate(new Date(today.getTime() - 24 * 3600 * 1000), tz, 'yyyy-MM-dd'),
  };
  const units = {};
  shops.forEach((s) => ymPaged_(key, '/v2/campaigns/' + s.id + '/stats/orders', body, 200, (res) => {
    (res.orders || []).forEach((order) => {
      if (/^CANCELLED/.test(String(order.status || ''))) return;
      (order.items || []).forEach((item) => {
        const article = String(item.shopSku || item.offerId || '').trim();
        if (article) units[article] = (units[article] || 0) + (Number(item.count) || 0);
      });
    });
  }));
  return units;
}
