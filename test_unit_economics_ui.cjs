const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

function fixture(page = 1) {
  return {
    file: 'unit_economics_2026-10.xlsx', period: '2026-10', updated_at: '2026-11-15T12:00:00+03:00',
    period_filter: true, period_filtered: false, available_periods: ['2026-09', '2026-10'],
    active_period: {date_from: null, date_to: null},
    columns: [
      ['Артикул', 'identity', 'text'], ['Название', 'identity', 'text'],
      ['Выручка нетто, ₽', 'finance', 'number'], ['Маржа, % выручки', 'finance', 'percent'],
      ['Прибыль на 1 шт', 'per_unit', 'number'], ['Текущая цена, ₽', 'prices', 'number', true]
    ].map(([key, group, format, snapshot]) => ({key, group, format, snapshot})),
    filters: {marketplace: ['Ozon'], cabinet: [{value: 'Ozon|К1', label: 'К1 · Ozon'}],
      brand: ['Бренд <Плюс>'], category: ['Корм']},
    rows: [[page === 1 ? 'A-001' : 'A-002', '<Тест & товар>', 50, 0.25, 12.5, 250]],
    totals: [null, null, 100, 0.25, 12.5, 250], total_rows: 2, page, page_size: 1
  };
}

function setup(respond) {
  const nodes = {}, timers = new Map(), requests = [];
  let timerId = 0;
  const filters = ['marketplace', 'cabinet', 'brand', 'category'].map(key => ({
    dataset: {ueFilter: key}, value: ''
  }));
  const tabs = ['finance', 'per_unit', 'prices', 'funnel', 'inventory', 'all'].map(key => ({
    dataset: {ueGroup: key}, setAttribute(name, value) { this[name] = value; }
  }));
  const root = {
    querySelector: key => nodes[key] ??= {},
    querySelectorAll: key => key === '[data-ue-filter]' ? filters : tabs
  };
  const context = {
    window: {}, document: {createElement: () => ({}), head: {appendChild() {}}},
    URLSearchParams,
    setTimeout(fn, delay) { const id = ++timerId; timers.set(id, {fn, delay}); return id; },
    clearTimeout(id) { timers.delete(id); },
    fetch: async (url, init) => {
      assert.match(url, /^\/api\/unit-economics\//);
      const {pathname, searchParams} = new URL(url, 'http://localhost');
      const request = {pathname, searchParams, method: init?.method || 'GET'};
      requests.push(request);
      return {ok: true, json: async () => respond(request)};
    }
  };
  vm.runInNewContext(fs.readFileSync('app/static/unit_economics.js', 'utf8'), context);
  context.window.WBOPSUnitEconomics.render(root);
  const node = name => root.querySelector(`[data-ue-${name}]`);
  return {root, filters, tabs, requests, node, runTimer(delay) {
    const [id, {fn}] = [...timers].find(([, timer]) => timer.delay === delay);
    timers.delete(id);
    fn();
  }};
}

const settled = () => new Promise(resolve => setImmediate(resolve));

test('actual unit economics changes column groups, paginates and sends filter parameters', async () => {
  const {node, filters, tabs, root, requests, runTimer} = setup(({pathname, searchParams}) =>
    pathname.endsWith('/update') ? {status: 'idle'} : fixture(Number(searchParams.get('page'))));
  await settled();
  assert.match(root.innerHTML, /\/api\/unit-economics\/report\/download/);
  assert.match(node('meta').textContent, /Период 2026-10.*unit_economics_2026-10.xlsx/);
  assert.match(node('table').innerHTML, /Итого.*100/);
  assert.match(node('table').innerHTML, /&lt;Тест &amp; товар&gt;/);
  assert.match(node('table').innerHTML, /Маржа, % выручки/);
  assert.doesNotMatch(node('table').innerHTML, /Прибыль на 1 шт/);
  assert.match(filters[2].innerHTML, /Бренд &lt;Плюс&gt;/);
  assert.equal(node('prev').disabled, true);
  assert.equal(node('next').disabled, false);

  tabs[1].onclick();
  assert.equal(tabs[1]['aria-pressed'], 'true');
  assert.match(node('table').innerHTML, /Прибыль на 1 шт/);
  assert.doesNotMatch(node('table').innerHTML, /Маржа, % выручки/);
  tabs[5].onclick();
  assert.match(node('table').innerHTML, /Маржа, % выручки/);
  assert.match(node('table').innerHTML, /Прибыль на 1 шт/);
  node('next').onclick();
  await settled();
  assert.equal(requests.at(-1).searchParams.get('page'), '2');
  assert.match(node('table').innerHTML, /A-002/);
  assert.match(node('table').innerHTML, /Итого.*100/);
  assert.equal(node('next').disabled, true);

  for (const [field, value] of filters.map((filter, i) => [filter, ['Ozon', 'Ozon|К1', 'Бренд <Плюс>', 'Корм'][i]])) {
    field.value = value;
    field.onchange();
    await settled();
  }
  node('search').oninput({target: {value: 'тест'}});
  runTimer(220);
  await settled();
  assert.deepEqual(Object.fromEntries(requests.at(-1).searchParams), {
    marketplace: 'Ozon', cabinet: 'Ozon|К1', brand: 'Бренд <Плюс>', category: 'Корм', q: 'тест', page: '1'
  });
});

test('empty state offers cache build and refreshes report after completion', async () => {
  let state = {status: 'idle'};
  const {node, requests, runTimer} = setup(({pathname, method}) => {
    if (method === 'POST') state = {status: 'running', mode: 'unit', log: ['Кэш загружен.']};
    if (pathname.endsWith('/update')) return state;
    return state.status === 'done' ? fixture() : {file: null};
  });
  await settled();
  assert.match(node('notice').textContent, /Отчёт ещё не собран/);
  assert.equal(node('excel').hidden, true);
  assert.equal(node('build').disabled, false);
  await node('build').onclick();
  assert.equal(requests.at(-1).method, 'POST');
  assert.equal(node('build').disabled, true);
  assert.match(node('run').innerHTML, /Собираю фактическую юнит-экономику из кэша/);
  state = {status: 'done', mode: 'unit'};
  runTimer(2000);
  await settled();
  assert.equal(node('build').disabled, false);
  assert.equal(node('excel').hidden, false);
  assert.match(node('run').innerHTML, /Юнит-экономика собрана/);
  assert.match(node('table').innerHTML, /A-001/);
});

test('rebuild clears filters missing from the new report and reloads page one once', async () => {
  let status = {status: 'idle'};
  const {node, filters, requests, runTimer} = setup(({pathname, method, searchParams}) => {
    if (method === 'POST') status = {status: 'running', mode: 'unit'};
    if (pathname.endsWith('/update')) return status;
    const data = fixture(Number(searchParams.get('page')));
    if (status.status === 'done') {
      data.filters.cabinet = [{value: 'Ozon|К2', label: 'К2'}];
      data.filters.brand = ['Другой бренд'];
      data.filters.category = ['Игрушки'];
      if (['cabinet', 'brand', 'category'].some(key => searchParams.get(key))) {
        data.rows = [];
        data.total_rows = 0;
      }
    }
    return data;
  });
  await settled();
  for (const [i, value] of ['Ozon', 'Ozon|К1', 'Бренд <Плюс>', 'Корм'].entries()) {
    filters[i].value = value;
    filters[i].onchange();
    await settled();
  }
  node('search').oninput({target: {value: 'товар'}});
  runTimer(220);
  await settled();
  node('next').onclick();
  await settled();
  assert.equal(requests.at(-1).searchParams.get('page'), '2');

  await node('build').onclick();
  status = {status: 'done', mode: 'unit'};
  const beforePoll = requests.length;
  runTimer(2000);
  await settled();
  assert.deepEqual(requests.slice(beforePoll).map(request => request.pathname), [
    '/api/unit-economics/update', '/api/unit-economics/report', '/api/unit-economics/report'
  ]);
  assert.equal(requests.at(-2).searchParams.get('cabinet'), 'Ozon|К1');
  assert.deepEqual(Object.fromEntries(requests.at(-1).searchParams), {
    marketplace: 'Ozon', cabinet: '', brand: '', category: '', q: 'товар', page: '1'
  });
  assert.deepEqual(filters.map(field => field.value), ['Ozon', '', '', '']);
  assert.match(node('table').innerHTML, /A-001/);
});

test('month controls request inclusive bounds, reset pagination, and show an empty selection', async () => {
  const {node, filters, tabs, requests} = setup(({pathname, searchParams}) => {
    if (pathname.endsWith('/update')) return {status: 'idle'};
    const data = fixture(Number(searchParams.get('page')));
    const date_from = searchParams.get('date_from'), date_to = searchParams.get('date_to');
    data.active_period = {date_from, date_to};
    data.period_filtered = date_to === '2026-09' || date_from === '2026-10';
    if (date_from === '2026-10') {
      data.rows = [];
      data.total_rows = 0;
      data.filters.brand = [];
    }
    return data;
  });
  await settled();
  assert.equal(node('period').hidden, false);
  assert.match(node('period-from').innerHTML, /value="2026-09"/);
  tabs[2].onclick();
  assert.match(node('table').innerHTML, /Текущая цена, ₽\s+<small>Снимок<\/small>/);
  assert.equal(node('snapshot').hidden, false);
  filters[2].value = 'Бренд <Плюс>';
  filters[2].onchange();
  await settled();
  node('next').onclick();
  await settled();
  assert.equal(requests.at(-1).searchParams.get('page'), '2');

  node('period-to').value = '2026-09';
  node('period-to').onchange();
  await settled();
  assert.equal(requests.at(-1).searchParams.get('date_to'), '2026-09');
  assert.equal(requests.at(-1).searchParams.get('page'), '1');
  tabs[5].onclick();
  assert.match(node('snapshot').textContent, /по календарным дням выбранного периода/);
  node('period-from').value = '2026-10';
  node('period-from').onchange();
  await settled();
  const selected = requests.at(-1).searchParams;
  assert.equal(selected.get('date_from'), '2026-10');
  assert.equal(selected.get('date_to'), '2026-10');
  assert.equal(selected.get('brand'), '');
  assert.equal(node('period-to').value, '2026-10');
  assert.match(node('table').innerHTML, /По выбранным фильтрам нет строк/);

  node('period-all').onclick();
  await settled();
  assert.equal(requests.at(-1).searchParams.has('date_from'), false);
  assert.equal(requests.at(-1).searchParams.has('date_to'), false);
  assert.equal(node('period-all').disabled, true);
  assert.match(node('table').innerHTML, /A-001/);

  node('period-from').value = '2026-09';
  node('period-from').onchange();
  await settled();
  node('period-to').value = '2026-10';
  node('period-to').onchange();
  await settled();
  assert.equal(node('period-from').value, '2026-09');
  assert.equal(node('period-to').value, '2026-10');
  assert.doesNotMatch(node('snapshot').textContent, /по календарным дням выбранного периода/);
});

test('weekly workbook shows why month controls are unavailable', async () => {
  const {node} = setup(({pathname}) => pathname.endsWith('/update') ? {status: 'idle'} : {
    ...fixture(), period_filter: false, available_periods: [],
    period_filter_notice: 'Для недельного отчёта выбор периода недоступен.'
  });
  await settled();
  assert.equal(node('period').hidden, true);
  assert.match(node('period-note').textContent, /недельного отчёта/);
  assert.match(node('table').innerHTML, /A-001/);
});
