/* WB OPS: «Главная» — магазины (юрлицо × маркетплейс): факт за период, остатки и прогноз на конец месяца. */
window.WBOPSHome = (() => {
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const MONTHS = ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь'];
  const iso = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  const parse = s => { const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d); };
  const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
  const ru = s => s ? s.split('-').reverse().join('.') : '';
  const num = (v, digits = 0) => v == null || isNaN(v) ? '—' : Number(v).toLocaleString('ru-RU', {minimumFractionDigits: digits, maximumFractionDigits: digits});
  const rub = v => v == null ? '—' : `${num(v)} ₽`;
  const pct = v => v == null ? '—' : `${num(v, 2)}%`;
  const neg = v => v == null ? null : -v;            // расходы приходят со знаком «−», в таблице — по модулю
  const monday = d => addDays(d, -((d.getDay() + 6) % 7));
  const KEY = 'wbops.home';
  const DASH = {wb: '/dashboard', ozon: '/dashboard-ozon'};

  // Колонки факта и прогноза: [заголовок, значение строки, класс]
  const FACT = [
    ['Заказы, шт', r => num(r.orders_qty)], ['Заказы, руб', r => rub(r.orders_sum)], ['Выкупы, шт', r => num(r.net_qty)],
    ['Выручка, руб', r => rub(r.revenue)], ['Прибыль вал, руб', r => rub(r.profit), r => r.profit < 0 ? 'neg' : ''],
    ['Маржинальность', r => pct(r.margin), r => r.margin < 0 ? 'neg' : ''], ['Процент выкупа, %', r => pct(r.buyout)],
    ['Рекламные расходы, руб', r => rub(neg(r.ads_only))], ['ДРР, %', r => pct(r.drr)],
    ['Маркетинг, руб', r => rub(neg(r.marketing))], ['Маркетинг, %', r => pct(r.marketing_pct)],
    ['Общий маркетинг', r => rub(neg(r.total_marketing))], ['Общий маркетинг, %', r => pct(r.total_marketing_pct)],
    ['К перечислению', r => rub(r.net_to_pay)],
  ];
  const FORECAST = [
    ['Заказы, шт', f => num(f.orders_qty)], ['Заказы, руб', f => rub(f.orders_sum)], ['Выкупы, шт', f => num(f.net_qty)],
    ['Выручка, руб', f => rub(f.revenue)], ['Рекламные расходы, руб', f => rub(neg(f.ads_only))], ['ДРР, %', f => pct(f.drr)],
    ['Маркетинг, руб', f => rub(neg(f.marketing))], ['Маркетинг, %', f => pct(f.marketing_pct)],
    ['Прибыль вал, руб', f => rub(f.profit), f => f.profit < 0 ? 'neg' : ''],
  ];
  const HINTS = {
    'Прибыль вал, руб': 'Как строка «Прибыль» в ОПиУ: выручка минус все удержания маркетплейса, реклама, себестоимость, подготовка и налог, плюс ретро-бонусы.',
    'Процент выкупа, %': 'WB — по воронке продаж (выкуплено / выкуплено + отменено). Ozon — доля заказанного без отмен.',
    'Рекламные расходы, руб': 'WB — затраты рекламного кабинета. Ozon — продвижение товара, оплата за клик, звёздные товары.',
    'Маркетинг, руб': 'Ozon: продвижение бренда, баллы за отзывы (ускоренный и первый сбор отзывов), бейдж «Оригинал» («Бренд проверен»), Premium Pro, механики лояльности.',
    'Общий маркетинг': 'Реклама + маркетинг.',
    'К перечислению': 'Выручка минус все удержания маркетплейса плюс компенсации.',
    'Остаток, руб': 'Остатки на складах сейчас × себестоимость.',
  };

  let root = null;
  const st = {preset: 'month:0', from: '', to: '', data: null};
  try { Object.assign(st, JSON.parse(localStorage.getItem(KEY) || '{}'), {data: null}); } catch (e) { /* ignore */ }

  function presets() {
    const today = addDays(new Date(), -1), list = [];
    for (let i = 0; i < 12; i++) {
      const s = new Date(today.getFullYear(), today.getMonth() - i, 1), e = new Date(s.getFullYear(), s.getMonth() + 1, 0);
      list.push([`month:${i}`, `${MONTHS[s.getMonth()][0].toUpperCase() + MONTHS[s.getMonth()].slice(1)} ${s.getFullYear()}${i === 0 ? ' · текущий' : ''}`, s, e]);
    }
    const m0 = monday(today);
    for (let i = 0; i < 8; i++) {
      const s = addDays(m0, -7 * i), e = addDays(s, 6);
      list.push([`week:${i}`, `Неделя ${ru(iso(s))} – ${ru(iso(e))}${i === 0 ? ' · текущая' : ''}`, s, e]);
    }
    list.push(['days:7', 'Последние 7 дней', addDays(today, -6), today], ['days:30', 'Последние 30 дней', addDays(today, -29), today], ['custom', 'Свой период…', null, null]);
    return list;
  }
  function period() {
    if (st.preset === 'custom' && st.from && st.to) return [st.from, st.to];
    const p = presets().find(x => x[0] === st.preset) || presets()[0];
    return [iso(p[2]), iso(p[3])];
  }

  async function render(section) {
    root = section;
    root.className = 'panel rv-page hm-page';
    root.innerHTML = `<div class="hm-filters" data-filters></div><div data-notes></div><div class="hm-wrap" data-table><p class="muted">Загрузка…</p></div>`;
    await load();
  }

  async function load() {
    const [from, to] = period();
    drawFilters(from, to);
    let r, d;
    try { r = await fetch('/api/home?' + new URLSearchParams({from, to})); d = await r.json(); }
    catch (e) { root.querySelector('[data-table]').innerHTML = '<p class="rv-error">Нет связи с панелью.</p>'; return; }
    if (r.status === 403) { root.querySelector('[data-table]').innerHTML = '<div class="rv-empty"><b>Сводка по магазинам доступна владельцу</b><p class="muted"><a href="/workspace" data-nav="workspace">Рабочая очередь</a> — отзывы, вопросы и поставки FBS.</p></div>'; return; }
    if (!r.ok) { root.querySelector('[data-table]').innerHTML = `<p class="rv-error">${esc(d.detail || 'Ошибка')}</p>`; return; }
    st.data = d;
    try { localStorage.setItem(KEY, JSON.stringify({preset: st.preset, from: st.from, to: st.to})); } catch (e) { /* ignore */ }
    drawFilters(from, to); drawNotes(); drawTable();
    if (d.building.length) setTimeout(() => root?.isConnected && load(), 4000);
  }

  function drawFilters(from, to) {
    const d = st.data, opt = (v, t) => `<option value="${esc(v)}" ${v === st.preset ? 'selected' : ''}>${esc(t)}</option>`;
    root.querySelector('[data-filters]').innerHTML = `
      <select data-f="preset" aria-label="Период" class="hm-period">${presets().map(([k, t]) => opt(k, t)).join('')}</select>
      ${st.preset === 'custom' ? `<label class="hm-date">с <input type="date" data-f="from" value="${from}"></label><label class="hm-date">по <input type="date" data-f="to" value="${to}"></label>` : ''}
      <span class="muted hm-days">${d && d.rows.length ? `Дней прошло: <b>${d.days_passed}</b> из ${d.days_total}${d.fact_to ? ` · данные по ${ru(d.fact_to)}` : ''}` : ''}</span>`;
    root.querySelectorAll('[data-f]').forEach(el => el.onchange = () => {
      if (el.dataset.f === 'preset' && el.value === 'custom') { st.from = from; st.to = to; }
      st[el.dataset.f] = el.value;
      load();
    });
  }

  function drawNotes() {
    const d = st.data, notes = [];
    if (d.building.length) notes.push(`Собираю показатели ${d.building.join(', ')} из кэша — таблица обновится сама.`);
    if (d.missing.length) notes.push(`Нет данных ${d.missing.join(', ')}: подключите кабинет в «Кабинетах маркетплейсов» и нажмите «Обновить данные МП» в «Финансах».`);
    if (d.not_supported.length) notes.push(`${d.not_supported.join(', ')} в сводку пока не входит: для него нет дневных показателей.`);
    if (d.rows.some(r => r.fact_to < period()[1])) notes.push('Прогноз — на конец периода: факт за прошедшие дни ÷ дней прошло × дней в периоде. У каждого маркетплейса свой последний день с данными.');
    root.querySelector('[data-notes]').innerHTML = notes.map(n => `<div class="db-note">${esc(n)}</div>`).join('');
  }

  function cells(cols, r, extra = '') { return cols.map(([, val, cls]) => `<td class="${cls ? cls(r) : ''} ${extra}">${val(r)}</td>`).join(''); }
  function th(title, extra = '') {
    const h = HINTS[title];
    return `<th class="${extra}" ${h ? `title="${esc(h)}"` : ''}>${esc(title)}${h ? ' <span class="hm-i" aria-hidden="true">i</span>' : ''}</th>`;
  }

  function drawTable() {
    const d = st.data, box = root.querySelector('[data-table]');
    if (!d.rows.length) { box.innerHTML = '<div class="rv-empty"><b>За этот период данных нет</b><p class="muted">Выберите другой период или обновите данные маркетплейсов в «Финансах».</p></div>'; return; }
    box.innerHTML = `<table class="hm-table">
      <thead><tr><th rowspan="2" class="hm-shop">Магазин</th><th rowspan="2">МП</th><th colspan="${FACT.length}" class="hm-group">Факт</th>
        <th class="hm-group hm-stock">Остатки</th><th colspan="${FORECAST.length}" class="hm-group hm-fc">Прогноз на конец периода</th></tr>
        <tr>${FACT.map(([t]) => th(t)).join('')}${th('Остаток, руб', 'hm-stock')}${FORECAST.map(([t], i) => th(t, i === 0 ? 'hm-fc-start' : '')).join('')}</tr></thead>
      <tbody>${d.rows.map(r => `<tr><td class="hm-shop">${esc(r.shop)}${r.shop !== r.cabinet ? `<span class="muted"> · ${esc(r.cabinet)}</span>` : ''}</td>
        <td><a href="${DASH[r.mp]}" data-nav="${r.mp === 'wb' ? 'dashboard' : 'dashboard-ozon'}" title="Открыть дашборд">${esc(r.mp_title === 'Wildberries' ? 'WB' : r.mp_title)}</a></td>
        ${cells(FACT, r)}<td class="hm-stock">${rub(r.stock_cost)}</td>${FORECAST.map(([, val, cls], i) => `<td class="${cls ? cls(r.forecast) : ''} ${i === 0 ? 'hm-fc-start' : ''}">${val(r.forecast)}</td>`).join('')}</tr>`).join('')}</tbody>
      <tfoot><tr><td class="hm-shop">Итого</td><td></td>${cells(FACT, d.total)}<td class="hm-stock">${rub(d.total.stock_cost)}</td>
        ${FORECAST.map(([, val, cls], i) => `<td class="${cls ? cls(d.total.forecast) : ''} ${i === 0 ? 'hm-fc-start' : ''}">${val(d.total.forecast)}</td>`).join('')}</tr></tfoot></table>`;
  }

  const style = document.createElement('style');
  style.textContent = `
.hm-filters{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin:4px 0 14px}.hm-filters select,.hm-filters input{height:40px;border:1px solid #e3e5ea;border-radius:10px;padding:0 12px;font:inherit;font-size:14px;background:#fff}
.hm-period{min-width:240px}.hm-date{display:flex;align-items:center;gap:6px;font-size:13px;color:#6b7280}.hm-days{font-size:13px}
.hm-wrap{overflow-x:auto;border:1px solid #e3e6ee;border-radius:12px;background:#fff}
.hm-table{border-collapse:collapse;font-size:13px;white-space:nowrap;font-variant-numeric:tabular-nums;min-width:100%}
.hm-table th,.hm-table td{padding:7px 10px;border-bottom:1px solid #eef0f5;text-align:right}
.hm-table thead th{background:#2f4b8a;color:#fff;font-weight:600;font-size:12px;white-space:normal;min-width:78px;line-height:1.25;border-right:1px solid #ffffff26;cursor:default}
.hm-table thead tr:first-child th{text-align:center}.hm-table thead .hm-group{font-size:13px;text-align:left;padding-left:14px}
.hm-table .hm-shop{text-align:left;position:sticky;left:0;background:#fff;z-index:1;min-width:170px}.hm-table thead .hm-shop{background:#2f4b8a;z-index:2}
.hm-table td:nth-child(2){text-align:center}.hm-table td a{color:#2f4b8a;font-weight:600;text-decoration:none}.hm-table td a:hover{text-decoration:underline}
.hm-table .hm-stock{background:#f6f8fc}.hm-table thead .hm-stock{background:#3d5a99}.hm-fc-start{border-left:2px solid #c9d2e6!important}
.hm-table thead .hm-fc,.hm-table thead .hm-fc-start{background:#24407a}
.hm-table tbody tr:hover td{background:#f8f9fc}.hm-table tbody tr:hover .hm-stock{background:#eef2fa}
.hm-table tfoot td{font-weight:700;background:#f3f5f9;border-top:2px solid #d9deea}.hm-table .neg{color:#b42323}
.hm-i{display:inline-grid;place-items:center;width:13px;height:13px;border-radius:50%;background:#ffffff33;font-size:9px;font-style:italic;font-weight:700;margin-left:2px}
.wb-member [data-nav=home]{display:none!important}
@media(max-width:800px){.hm-period{min-width:0;width:100%}.hm-table .hm-shop{min-width:120px}}`;
  document.head.appendChild(style);

  return {render};
})();
