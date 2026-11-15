const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');

function summary(revenue, period = 'Ноя 2026') {
  const kpi = Object.fromEntries(
    ['revenue', 'mp_expenses', 'mp_result', 'cogs', 'tax', 'profit'].map(key =>
      [key, {total: revenue, last: revenue}])
  );
  kpi.margin = {total: 1};
  return {subtitle: 'Отчёт', periods: [period], period_keys: ['2026-11-01'],
    forecast_title: null, kpi, rows: [{label: 'Выручка за вычетом возвратов', kind: 'total',
      total: revenue, pct: 1, values: [revenue]}]};
}

function fixture(key) {
  const amounts = {'': 600, 'Wildberries|К1': 100, 'Wildberries|К2': 200, 'Wildberries|К3': 300};
  const amount = amounts[key];
  return {selected: key, summary: summary(amount), detail: {
    day: summary(amount, '01.11'), week: summary(amount, '26.10–01.11')},
    cabinets: [1, 2, 3].map(n => ({key: `Wildberries|К${n}`, marketplace: 'Wildberries',
      cabinet: `К${n}`, legal_name: `ИП ${n}`, panel_id: `panel-${n}`})),
    control: [], goods: {header: [], rows: []}, quality: {reliable: true},
    finance_status: {}, file: 'test.xlsx', updated_at: '2026-11-15T12:00'};
}

test('choosing a legal entity reloads its KPI and all table grains', async () => {
  const requested = [];
  const nodes = Object.fromEntries(['[data-f-cabinet]', '[data-f-details]', '[data-f-grain]',
    '[data-f-period]', '[data-f-goods-search]', '[data-f-goods]'].map(key => [key, {}]));
  const box = {innerHTML: '', querySelector: key => nodes[key]};
  const run = {hidden: true};
  const form = {dir: {}, python: {}, querySelector: () => ({textContent: ''})};
  const root = {dataset: {}, querySelector: key => ({'[data-f-report]': box,
    '[data-f-settings]': form, '[data-f-run]': run})[key], querySelectorAll: () => []};
  const context = {window: {}, document: {createElement: () => ({}), head: {appendChild() {}}},
    localStorage: {getItem: () => null, setItem() {}}, clearTimeout() {}, setTimeout() {},
    fetch: async url => {
      if (url.startsWith('/api/finance/report?')) {
        const key = decodeURIComponent(url.split('cabinet=')[1]);
        requested.push(key);
        return {ok: true, json: async () => fixture(key)};
      }
      if (url === '/api/finance/settings') return {ok: true, json: async () => ({dir: '', python: '', resolved_dir: 'opiu'})};
      return {ok: true, json: async () => ({status: 'idle'})};
    },
    Intl, Date, Object, console};
  vm.runInNewContext(fs.readFileSync('app/static/finance.js', 'utf8'), context);
  context.window.WBOPSFinance.render(root);
  await new Promise(resolve => setImmediate(resolve));

  assert.match(box.innerHTML, /600/);
  assert.doesNotMatch(box.innerHTML, /data-f-run-cabinet/);
  for (const n of [1, 2, 3]) {
    nodes['[data-f-cabinet]'].onchange({target: {value: `Wildberries|К${n}`}});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(requested.at(-1), `Wildberries|К${n}`);
    assert.equal(root.dataset.financeCabinet, `panel-${n}`);
    assert.match(box.innerHTML, new RegExp(`>${n}00<`));
    for (const grain of ['day', 'week', 'month']) {
      nodes['[data-f-grain]'].onchange({target: {value: grain}});
      assert.match(box.innerHTML, new RegExp(`ОПиУ по ${grain === 'day' ? 'дням' : grain === 'week' ? 'неделям' : 'месяцам'}`));
      assert.match(box.innerHTML, new RegExp(`>${n}00<`));
    }
  }
  nodes['[data-f-cabinet]'].onchange({target: {value: ''}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(root.dataset.financeCabinet, '');
  assert.match(box.innerHTML, />600</);
});
