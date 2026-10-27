/* WB OPS: «Дашборд» — оцифровка ключевых показателей Wildberries за период (в духе TrueStats). */
window.WBOPSDashboard = (() => {
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const MONTHS = ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь'];
  const iso = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  const parse = s => { const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d); };
  const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
  const ru = s => s ? s.split('-').reverse().join('.') : '';
  const fmt = (v, digits = 0) => v == null || isNaN(v) ? '—' : Number(v).toLocaleString('ru-RU', {minimumFractionDigits: digits, maximumFractionDigits: digits});
  const isoWeek = d => { const t = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate())); const day = t.getUTCDay() || 7; t.setUTCDate(t.getUTCDate() + 4 - day);
    const y0 = new Date(Date.UTC(t.getUTCFullYear(), 0, 1)); return Math.ceil(((t - y0) / 864e5 + 1) / 7); };
  const monday = d => addDays(d, -((d.getDay() + 6) % 7));

  // Плитки: значение (+ доля), направление «хорошо», подсказка. money — показывать по модулю (расходы).
  const TILES = [
    {id: 'profit', title: 'Прибыль / Маржинальность', v: 'profit', pct: 'margin', good: 'up', hint: 'Выручка за вычетом возвратов минус все удержания WB, себестоимость, подготовка и налог, плюс ретро-бонусы производителей — как строка «Прибыль» в ОПиУ. Маржинальность — прибыль к выручке.'},
    {id: 'orders', title: 'Заказы', v: 'orders_sum', qty: 'orders_qty', good: 'up', hint: 'Все заказы покупателей за период (включая будущие отмены), по ценам заказа.'},
    {id: 'sales', title: 'Продажи', v: 'revenue', qty: 'net_qty', good: 'up', hint: 'Выручка по цене продавца за вычетом возвратов и количество выкупленных штук минус возвраты.'},
    {id: 'ads', title: 'Реклама / ДРР', v: 'ads', pct: 'drr', money: true, good: 'down', by: 'pct', hint: 'Затраты рекламного кабинета за период — как «История затрат» в ЛК WB (раздел «Аналитика рекламы» загружает их сам). С фильтром по товарам — затраты кампаний по артикулам. «К перечислению» считается по фактическим удержаниям WB. ДРР — доля от продаж.'},
    {id: 'cost_per_order', title: 'Стоимость за заказ', v: 'cost_per_order', digits: 2, good: 'down', hint: 'Расходы на рекламу за период, делённые на количество заказов за тот же период (все заказы, включая будущие отмены). Сравнение — с прошлым периодом: зелёный — заказ стал дешевле, красный — дороже.'},
    {id: 'net_to_pay', title: 'К перечислению от WB', v: 'net_to_pay', good: 'up', hint: 'Выручка минус все удержания WB плюс компенсации — сколько WB перечислит за период.'},
    {id: 'logistics', title: 'Логистика', v: 'logistics', pct: 'logistics_pct', money: true, good: 'down', by: 'pct', hint: 'Доставка к покупателю, обратная логистика при отменах и возвратах.'},
    {id: 'buyout', title: 'Процент выкупа', pctOnly: 'buyout', good: 'up', hint: 'Как в воронке продаж WB: выкуплено / (выкуплено + отменено) по заказам периода. Нужен доступ ключа к разделу «Аналитика»; если WB не ответил — прочерк.'},
    {id: 'paid', title: 'Реализация', v: 'paid', good: 'up', hint: 'Сколько фактически заплатили покупатели (цена после скидок WB), за вычетом возвратов. С этой суммы считается налог.'},
    {id: 'storage', title: 'Хранение', v: 'storage', pct: 'storage_pct', money: true, good: 'down', by: 'pct', hint: 'Платное хранение на складах WB.'},
    {id: 'acceptance', title: 'Плат. приёмка', v: 'acceptance', pct: 'acceptance_pct', money: true, good: 'down', by: 'pct', hint: 'Платные операции на приёмке.'},
    {id: 'other', title: 'Прочие удержания', v: 'other', pct: 'other_pct', money: true, good: 'down', hint: 'Подписки (в т. ч. «Джем»), программа лояльности, утилизация, транзит и другие удержания WB.'},
    {id: 'roi', title: 'ROI', pctOnly: 'roi', good: 'up', hint: 'Прибыль к вложениям в товар (себестоимость + подготовка проданного).'},
    {id: 'cogs', title: 'Себестоимость продаж', v: 'cogs', pct: 'cogs_pct', money: true, good: 'down', by: 'pct', hint: 'Проданные штуки минус возвраты × себестоимость из «Номенклатуры» (действующая в месяце продажи).'},
    {id: 'retro', title: 'Ретро-бонусы', v: 'retro', pct: 'retro_pct', good: 'up', hint: 'Ретро-бонусы производителей: себестоимость проданного × процент бренда из «Товары → Ретро-бонусы производителей». Увеличивают прибыль.'},
    {id: 'prep', title: 'Подготовка', v: 'prep', pct: 'prep_pct', money: true, good: 'down', by: 'pct', hint: 'Проданные штуки минус возвраты × стоимость подготовки из «Номенклатуры».'},
    {id: 'tax', title: 'Налоги', v: 'tax', pct: 'tax_pct', money: true, good: 'down', by: 'pct', hint: 'Ставка налога из настроек кабинета × сумма, оплаченная покупателями.'},
    {id: 'commission', title: 'Комиссия', v: 'commission', pct: 'commission_pct', money: true, good: 'down', by: 'pct', hint: 'Вознаграждение WB (кВВ с НДС, выдача на ПВЗ) с учётом СПП.'},
    {id: 'avg_seller', title: 'Сред. цена до скидок МП', v: 'avg_price_seller', good: 'up', hint: 'Средняя цена продавца (с его скидкой) за проданную штуку.'},
    {id: 'cap_cost', title: 'Капитализация по себес.', v: 'stock_cost', fixed: true, hint: 'Остатки на складах WB сейчас × себестоимость.'},
    {id: 'cap_retail', title: 'Капитализация по розн.', v: 'stock_retail', fixed: true, hint: 'Остатки на складах WB сейчас × текущая цена продавца.'},
    {id: 'penalty', title: 'Штрафы', v: 'penalty', pct: 'penalty_pct', money: true, good: 'down', hint: 'Штрафы WB, включая хранение возвратов на ПВЗ.'},
    {id: 'compensation', title: 'Компенсации', v: 'compensation', pct: 'compensation_pct', good: 'up', hint: 'Компенсации ущерба и доплаты от WB.'},
    {id: 'avg_paid', title: 'Сред. цена продажи', v: 'avg_price_paid', good: 'up', hint: 'Сколько в среднем заплатил покупатель за штуку (после скидок WB).'},
    {id: 'drr_orders', title: 'Реклама / ДРРз', v: 'ads', pct: 'drr_orders', money: true, good: 'down', by: 'pct', hint: 'Реклама к сумме заказов.'},
    {id: 'log_unit', title: 'Ср. стоимость логистики на 1 шт.', v: 'logistics_unit', good: 'down', hint: 'Вся логистика периода на одну проданную штуку.'},
    {id: 'turn_sales', title: 'Оборачиваемость по прод.', days: 'turnover_sales', good: 'down', hint: 'На сколько дней хватит текущего остатка при темпе продаж (выкупов) периода.'},
    {id: 'turn_orders', title: 'Оборачиваемость по зак.', days: 'turnover_orders', good: 'down', hint: 'На сколько дней хватит текущего остатка при темпе заказов периода.'},
    {id: 'stock', title: 'Остаток на складах WB', qtyOnly: 'stock_qty', fixed: true, hint: 'Сейчас на складах WB, без товаров в пути к покупателю и возвратов.'},
    {id: 'returns', title: 'Возвраты', qtyOnly: 'return_qty', good: 'down', hint: 'Возвращено штук за период.'},
    {id: 'acquiring', title: 'Эквайринг', v: 'acquiring', pct: 'acquiring_pct', money: true, good: 'down', by: 'pct', hint: 'Плата за приём платежей.'},
  ];
  const st = {preset: 'week:1', from: '', to: '', cabinet: '', brand: '', category: '', article: '', data: null, poll: null};
  let root = null, hidden = new Set();
  try { hidden = new Set(JSON.parse(localStorage.getItem('wbops.dashboard.hidden') || '[]')); } catch (e) { /* нет хранилища */ }
  try { Object.assign(st, JSON.parse(localStorage.getItem('wbops.dashboard.filters') || '{}'), {data: null, poll: null}); } catch (e) { /* ignore */ }

  // ---------------------------------------------------------------- периоды
  function presets(last) {
    const today = last ? parse(last) : addDays(new Date(), -1);
    const list = [['yesterday', 'Вчера', today, today], ['days:7', 'Последние 7 дней', addDays(today, -6), today], ['days:30', 'Последние 30 дней', addDays(today, -29), today]];
    const m0 = monday(today);
    for (let i = 0; i < 12; i++) {
      const s = addDays(m0, -7 * i), e = addDays(s, 6);
      list.push([`week:${i}`, `${isoWeek(s)} неделя (${ru(iso(s))} - ${ru(iso(e))})${i === 0 ? ' · текущая' : ''}`, s, e > today && i === 0 ? today : e]);
    }
    for (let i = 0; i < 12; i++) {
      const s = new Date(today.getFullYear(), today.getMonth() - i, 1), e = new Date(s.getFullYear(), s.getMonth() + 1, 0);
      list.push([`month:${i}`, `${MONTHS[s.getMonth()][0].toUpperCase() + MONTHS[s.getMonth()].slice(1)} ${s.getFullYear()}${i === 0 ? ' · текущий' : ''}`, s, e > today ? today : e]);
    }
    list.push(['custom', 'Свой период…', null, null]);
    return list;
  }
  function resolve() {
    const last = st.data?.options?.last_day;
    if (st.preset === 'custom' && st.from && st.to) return [st.from, st.to];
    const p = presets(last).find(x => x[0] === st.preset) || presets(last).find(x => x[0] === 'week:1');
    return [iso(p[2]), iso(p[3])];
  }

  // ---------------------------------------------------------------- каркас
  async function render(section) {
    root = section;
    root.className = 'panel rv-page db-page';
    root.innerHTML = `<div class="db-head"><h2>Оцифровка</h2><span class="muted db-fresh" data-fresh></span></div>
      <div class="db-filters" data-filters></div>
      <div class="db-bar"><h3>Общие показатели</h3><div class="db-bar-actions">
        <button type="button" class="db-ghost" data-refresh title="Пересобрать показатели из уже скачанных данных WB">↻ Обновить</button>
        <button type="button" class="db-ghost" data-widgets aria-expanded="false">⚙ Настройка виджетов</button></div></div>
      <div class="db-widgets" data-widget-panel hidden></div>
      <div data-notes></div><div class="db-grid" data-grid><p class="muted">Загрузка…</p></div>`;
    root.querySelector('[data-refresh]').onclick = refresh;
    root.querySelector('[data-widgets]').onclick = e => { const p = root.querySelector('[data-widget-panel]'); p.hidden = !p.hidden; e.currentTarget.setAttribute('aria-expanded', String(!p.hidden)); drawWidgets(); };
    await load();
  }

  async function load() {
    const [from, to] = resolve();
    const q = new URLSearchParams({from, to});
    if (st.preset.startsWith('month:')) {                       // месяц сравниваем с прошлым календарным месяцем
      const s = parse(from), ps = new Date(s.getFullYear(), s.getMonth() - 1, 1);
      const pe = new Date(Math.min(new Date(s.getFullYear(), s.getMonth(), 0), new Date(ps.getFullYear(), ps.getMonth(), parse(to).getDate())));
      q.set('prev_from', iso(ps)); q.set('prev_to', iso(st.preset === 'month:0' ? pe : new Date(s.getFullYear(), s.getMonth(), 0)));
    }
    ['cabinet', 'brand', 'category', 'article'].forEach(k => st[k] && q.set(k, st[k]));
    let d;
    try { d = await (await fetch('/api/dashboard?' + q)).json(); }
    catch (e) { root.querySelector('[data-grid]').innerHTML = '<p class="rv-error">Нет связи с панелью.</p>'; return; }
    if (d.status === 'building') {
      root.querySelector('[data-grid]').innerHTML = `<div class="rv-empty"><b>Собираю показатели…</b><p class="muted">Первый раз панель считает все дни из скачанного отчёта WB — это около минуты. Страница обновится сама.</p></div>`;
      waitBuild(); return;
    }
    if (d.status === 'error') { root.querySelector('[data-grid]').innerHTML = `<div class="rv-empty"><b>Нет данных для дашборда</b><p class="muted">${esc(d.message)}</p></div>`; return; }
    st.data = d;
    if (st.preset !== 'custom') { const [f2, t2] = resolve(); if (f2 !== from || t2 !== to) return load(); }   // «вчера» = последний день с данными
    try { localStorage.setItem('wbops.dashboard.filters', JSON.stringify({preset: st.preset, from: st.from, to: st.to, cabinet: st.cabinet, brand: st.brand, category: st.category, article: st.article})); } catch (e) { /* ignore */ }
    drawFilters(); drawNotes(); drawGrid();
    if (d.state && d.state.status === 'running') waitBuild(true);
  }
  function waitBuild(silent) {
    clearTimeout(st.poll);
    st.poll = setTimeout(async () => {
      if (!root?.isConnected) return;
      const s = await (await fetch('/api/dashboard/status')).json().catch(() => ({}));
      if (s.status === 'running') return waitBuild(silent);
      if (s.status === 'error') { root.querySelector('[data-grid]').innerHTML = `<div class="rv-empty"><b>Не удалось собрать показатели</b><p class="muted">${esc(s.message)}</p></div>`; return; }
      load();
    }, 2500);
  }
  async function refresh(e) {
    const b = e.currentTarget; b.disabled = true;
    try { const r = await fetch('/api/dashboard/refresh', {method: 'POST'}); const d = await r.json(); if (!r.ok) throw new Error(d.detail); waitBuild(); }
    catch (err) { alert(err.message); }
    setTimeout(() => { b.disabled = false; }, 3000);
  }

  function drawFilters() {
    const o = st.data.options, [from, to] = resolve();
    const opt = (v, t, cur) => `<option value="${esc(v)}" ${String(v) === String(cur) ? 'selected' : ''}>${esc(t)}</option>`;
    const articles = o.articles.filter(a => (!st.brand || a.brand === st.brand) && (!st.category || a.category === st.category));
    root.querySelector('[data-filters]').innerHTML = `
      <select data-f="preset" aria-label="Период" class="db-period">${presets(o.last_day).map(([k, t]) => opt(k, t, st.preset)).join('')}</select>
      ${st.preset === 'custom' ? `<label class="db-date">с <input type="date" data-f="from" value="${from}" min="${o.first_day || ''}" max="${o.last_day || ''}"></label><label class="db-date">по <input type="date" data-f="to" value="${to}" min="${o.first_day || ''}" max="${o.last_day || ''}"></label>` : ''}
      ${o.cabinets.length > 1 ? `<select data-f="cabinet" aria-label="Кабинет">${opt('', 'Все кабинеты', st.cabinet)}${o.cabinets.map(c => opt(c, c, st.cabinet)).join('')}</select>` : ''}
      <select data-f="brand" aria-label="Бренд">${opt('', 'Все бренды', st.brand)}${o.brands.map(b => opt(b, b, st.brand)).join('')}</select>
      <select data-f="category" aria-label="Категория">${opt('', 'Все категории', st.category)}${o.categories.map(b => opt(b, b, st.category)).join('')}</select>
      <select data-f="article" aria-label="Артикул">${opt('', 'Все артикулы', st.article)}${articles.map(a => opt(a.key, a.article, st.article)).join('')}</select>`;
    root.querySelectorAll('[data-f]').forEach(el => el.onchange = () => {
      const k = el.dataset.f;
      if (k === 'preset' && el.value === 'custom') { st.from = from; st.to = to; }
      st[k] = el.value;
      if (k === 'brand' || k === 'category') st.article = '';
      load();
    });
    root.querySelector('[data-fresh]').textContent = `Отчёт WB по ${ru(o.last_day)}${o.stocks_at ? ` · остатки на ${o.stocks_at.slice(0, 16).replace(/(\d{4})-(\d\d)-(\d\d)/, '$3.$2')}` : ''} · сравнение с ${ru(st.data.previous_period[0])} – ${ru(st.data.previous_period[1])}`;
  }

  function drawNotes() {
    const c = st.data.current, o = st.data.options, [from] = resolve(), notes = [];
    if (c.ads_source === 'withheld' && c.ads_withheld) notes.push('Реклама — по удержаниям WB из отчёта реализации: история затрат рекламного кабинета за этот период не загружена (раздел «Аналитика рекламы»).');
    if (c.no_cost_qty > 0) notes.push(`Продано ${fmt(c.no_cost_qty)} шт. товаров без себестоимости — прибыль завышена. Задайте цены в «Товары → Номенклатура и себестоимость».`);
    if (o.ordered_from && from < o.ordered_from) notes.push(`Заказы WB сохранены с ${ru(o.ordered_from)} — за более ранние дни заказы, выкуп и ДРРз не посчитаны.`);
    if (st.brand || st.category || st.article) notes.push('С фильтром не учитываются расходы WB без артикула: часть рекламы, хранения и штрафов.');
    if (st.data.state?.status === 'running') notes.push('Подтягиваю свежие данные из кэша WB — цифры обновятся сами.');
    root.querySelector('[data-notes]').innerHTML = notes.map(n => `<div class="db-note">${esc(n)}</div>`).join('');
  }

  // ---------------------------------------------------------------- плитки
  function tileHtml(t) {
    const c = st.data.current, p = st.data.previous;
    const val = key => (key in c ? c[key] : null), prev = key => (key in p ? p[key] : null);
    const money = v => v == null ? null : (t.money ? Math.abs(v) : v);
    let main = '', base, before, unit = '', isPct = false;
    if (t.pctOnly) { base = val(t.pctOnly); before = prev(t.pctOnly); main = `${fmt(base, 2)}<sup>%</sup>`; isPct = true; }
    else if (t.days) { base = val(t.days); before = prev(t.days); main = base == null ? '—' : `${fmt(base, 2)}<sup>дн.</sup>`; }
    else if (t.qtyOnly) { base = val(t.qtyOnly); before = prev(t.qtyOnly); main = `${fmt(base)}<sup>шт</sup>`; }
    else {
      base = money(val(t.v)); before = money(prev(t.v));
      main = `${fmt(base, t.digits || 0)}<sup>₽</sup>`;
      if (t.qty) main += `<span class="db-sep">/</span>${fmt(val(t.qty))}<sup>шт</sup>`;
      if (t.pct) main += `<span class="db-sep">/</span>${fmt(val(t.pct), 2)}<sup>%</sup>`;
    }
    // направление сравнения: по доле (для расходов) или по сумме
    let cmpNow = base, cmpBefore = before;
    if (t.by === 'pct' && t.pct) { cmpNow = val(t.pct); cmpBefore = prev(t.pct); }
    let cls = 'neutral', delta = '';
    if (!t.fixed && base != null && before != null && !(base === 0 && before === 0)) {
      const diff = base - before;
      const rel = before ? diff / Math.abs(before) * 100 : null;
      const dir = (cmpNow ?? 0) - (cmpBefore ?? 0);
      const good = t.good === 'up' ? dir > 0 : dir < 0;
      cls = Math.abs(dir) < 1e-9 ? 'neutral' : good ? 'good' : 'bad';
      const arrow = diff > 0 ? '↗' : diff < 0 ? '↘' : '→';
      delta = `<span class="db-delta">${diff > 0 ? '+' : ''}${fmt(diff, isPct || t.days || t.digits ? 2 : 0)}${!isPct && !t.days && !t.qtyOnly && !t.pctOnly ? ' ₽' : ''}${isPct ? ' п.п.' : ''}${rel != null && !isPct ? ` <span>(${fmt(rel, 2)}%)</span>` : ''}</span><span class="db-arrow" aria-hidden="true">${arrow}</span>`;
    } else if (!t.fixed && before == null && base != null) {
      delta = '<span class="db-delta muted">нет данных за прошлый период</span>';
    }
    return `<article class="db-tile ${cls}" data-tile="${t.id}"><div class="db-title">${esc(t.title)} <span class="db-i" tabindex="0" role="note" title="${esc(t.hint)}" aria-label="${esc(t.hint)}">i</span></div>
      <div class="db-value ${main.replace(/<[^>]+>/g, '').length > 17 ? 'long' : main.replace(/<[^>]+>/g, '').length > 14 ? 'mid' : ''}">${main}</div>${t.fixed ? '<div class="db-fixed">на сегодня</div>' : `<div class="db-change">${delta}</div>`}</article>`;
  }
  function drawGrid() {
    root.querySelector('[data-grid]').innerHTML = TILES.filter(t => !hidden.has(t.id)).map(tileHtml).join('') || '<p class="muted">Все виджеты скрыты — включите их в «Настройке виджетов».</p>';
  }
  function drawWidgets() {
    const box = root.querySelector('[data-widget-panel]');
    box.innerHTML = `<div class="db-wlist">${TILES.map(t => `<label><input type="checkbox" data-w="${t.id}" ${hidden.has(t.id) ? '' : 'checked'}> ${esc(t.title)}</label>`).join('')}</div>
      <button type="button" class="rv-link" data-wall>Показать все</button>`;
    box.querySelectorAll('[data-w]').forEach(cb => cb.onchange = () => { cb.checked ? hidden.delete(cb.dataset.w) : hidden.add(cb.dataset.w); save(); drawGrid(); });
    box.querySelector('[data-wall]').onclick = () => { hidden.clear(); save(); drawWidgets(); drawGrid(); };
  }
  function save() { try { localStorage.setItem('wbops.dashboard.hidden', JSON.stringify([...hidden])); } catch (e) { /* ignore */ } }

  const style = document.createElement('style');
  style.textContent = `
.db-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:12px;justify-content:space-between}.db-head h2{font-size:24px;font-weight:700;margin:0}.db-fresh{font-size:12px}
.db-filters{display:flex;flex-wrap:wrap;gap:10px;margin:14px 0 18px}.db-filters select,.db-filters input{height:42px;border:1px solid #e3e5ea;border-radius:10px;padding:0 34px 0 14px;font:inherit;font-size:14px;font-weight:500;color:#1f2328;background:#fff;
  appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%23374151' stroke-width='1.6' fill='none'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 12px center;min-width:0;max-width:100%}
.db-filters input{padding:0 10px;background-image:none}.db-period{min-width:280px}.db-date{display:flex;align-items:center;gap:6px;font-size:13px;color:#6b7280}
.db-bar{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:12px}.db-bar h3{font-size:17px;margin:0}.db-bar-actions{display:flex;gap:8px}
.db-ghost{height:36px;border:0;border-radius:9px;background:#eef0f4;padding:0 12px;font:inherit;font-size:13px;font-weight:600;color:#374151;cursor:pointer}.db-ghost:hover{background:#e4e7ec}.db-ghost:disabled{opacity:.6}
.db-widgets{background:#fff;border:1px solid #e7e9ee;border-radius:12px;padding:12px 14px;margin-bottom:12px}.db-wlist{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:6px 14px;font-size:13px;margin-bottom:8px}
.db-wlist label{display:flex;gap:7px;align-items:center}.db-note{background:#fffbeb;border:1px solid #fde68a;color:#92400e;border-radius:10px;padding:9px 13px;font-size:13px;margin-bottom:10px}
.db-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px}
.db-tile{border-radius:12px;padding:12px 13px 13px;background:#eceef1;box-shadow:0 1px 2px #0000000d;min-width:0}
.db-tile.good{background:#dcebdc}.db-tile.bad{background:#f3d9da}
.db-title{font-size:13px;font-weight:500;color:#1f2328;display:flex;align-items:center;gap:5px}
.db-i{display:inline-grid;place-items:center;width:15px;height:15px;border-radius:50%;background:#1f2328;color:#fff;font-size:10px;font-weight:700;font-style:italic;cursor:help;flex:none}
.db-value{font-size:24px;font-weight:700;color:#111827;margin:6px 0 8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.db-value.mid{font-size:20px}.db-value.long{font-size:18px;white-space:normal;line-height:1.3}.db-value sup{font-size:12px;font-weight:600;margin-left:2px}.db-sep{margin:0 6px;font-weight:600}
.db-change{display:flex;align-items:center;gap:8px;min-height:24px}.db-delta{display:inline-block;background:#fff;border-radius:7px;padding:2px 8px;font-size:12px;font-weight:700;color:#1f2328;white-space:nowrap}
.db-delta span{font-weight:500}.good .db-delta{color:#2f6b2f}.bad .db-delta{color:#b42323}.db-arrow{font-size:16px;font-weight:700}.good .db-arrow{color:#2f6b2f}.bad .db-arrow{color:#c03030}.neutral .db-arrow{color:#6b7280}
.db-fixed{font-size:11px;color:#6b7280;min-height:24px;display:flex;align-items:center}
.wb-member [data-nav=dashboard]{display:none!important}
@media(max-width:1500px){.db-grid{grid-template-columns:repeat(4,minmax(0,1fr))}}
@media(max-width:1150px){.db-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:800px){.db-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.db-period{min-width:0;width:100%}.db-value{font-size:20px}}
@media(max-width:480px){.db-grid{grid-template-columns:1fr}}`;
  document.head.appendChild(style);

  return {render};
})();
