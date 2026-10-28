/* WB OPS: «РНП» — рука на пульсе. Заказы, реклама и остатки по дням и артикулам, прогноз прибыли по заказам,
   план/факт, тепловая карта, калькулятор гипотез и заметки к дням. */
window.WBOPSRnp = (() => {
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const iso = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  const parse = s => { const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d); };
  const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
  const ru = s => s ? s.split('-').reverse().join('.') : '';
  const WD = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];
  const MONTHS = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь', 'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь'];
  const num = (v, d = 0) => v == null || isNaN(v) ? '—' : Number(v).toLocaleString('ru-RU', {minimumFractionDigits: d, maximumFractionDigits: d});
  const F = {int: v => num(v), rub: v => num(v), rub2: v => num(v, 2), pct: v => v == null ? '—' : num(v, 1) + '%', days: v => v == null ? '—' : num(v)};

  // Строки вкладки «По дням»: [ключ, название, формат, подсказка]
  const DAY_ROWS = [
    ['Заказы'],
    ['orders', 'Заказы, шт', 'int', 'Все заказы покупателей за день, включая будущие отмены.'],
    ['orders_sum', 'Заказы, ₽', 'rub', 'Сумма заказов по цене продавца (с его скидкой, до СПП).'],
    ['price', 'Ср. цена до СПП', 'rub', 'Средняя цена продавца в заказе.'],
    ['spp', 'СПП', 'pct', 'Средняя скидка постоянного покупателя WB (только для заказов из API WB).'],
    ['price_after_spp', 'Цена после СПП', 'rub', 'Сколько в среднем платит покупатель.'],
    ['cancel_rate', 'Отмены', 'pct', 'Доля заказов, отменённых на текущий момент.'],
    ['Реклама'],
    ['ads', 'Расход, ₽', 'rub', 'Расход на рекламу WB Продвижение.'],
    ['drr', 'ДРР от заказов', 'pct', 'Расход на рекламу к сумме заказов.'],
    ['views', 'Показы', 'int'], ['clicks', 'Клики', 'int'],
    ['ctr', 'CTR', 'pct', 'Клики к показам.'], ['cpc', 'CPC, ₽', 'rub2', 'Цена клика.'], ['cpo', 'CPO, ₽', 'rub', 'Расход на один заказ из рекламы.'],
    ['Прогноз по заказам'],
    ['buyout', 'Выкуп', 'pct', 'Процент выкупа артикула за прошлые 60 дней (без последних 10).'],
    ['sales_qty', 'Выкупят, шт', 'int'], ['revenue', 'Выручка, ₽', 'rub', 'Заказы × процент выкупа.'],
    ['commission', 'Комиссия WB', 'rub'], ['acquiring', 'Эквайринг', 'rub'], ['logistics', 'Логистика', 'rub', 'Логистика на один заказ (с отменами и возвратами) × заказы.'],
    ['storage', 'Хранение', 'rub'], ['cogs', 'Себестоимость', 'rub'], ['prep', 'Подготовка', 'rub'], ['tax', 'Налог', 'rub'],
    ['profit', 'Прибыль, ₽', 'rub', 'Прогноз прибыли по заказам дня.', 'strong'],
    ['margin', 'Маржинальность', 'pct', 'Прибыль к прогнозной выручке.', 'strong'],
    ['roi', 'ROI', 'pct', 'Прибыль к себестоимости и подготовке.'],
  ];
  // Колонки «По артикулам»
  const ART_COLS = [
    ['orders_per_day', 'Заказов в день', 'rub2'], ['orders_sum', 'Заказы, ₽', 'rub'], ['price', 'Цена до СПП', 'rub'], ['spp', 'СПП', 'pct'],
    ['buyout', 'Выкуп', 'pct'], ['ads', 'Реклама, ₽', 'rub'], ['drr', 'ДРР', 'pct'], ['ctr', 'CTR', 'pct'], ['cpo', 'CPO', 'rub'],
    ['profit', 'Прибыль, ₽', 'rub'], ['margin', 'Маржа', 'pct'], ['roi', 'ROI', 'pct'], ['stock', 'Остаток', 'int'], ['stock_days', 'Хватит, дн.', 'days'],
  ];
  const HEAT = [['orders', 'Заказы, шт', 'int'], ['orders_sum', 'Заказы, ₽', 'rub'], ['profit', 'Прибыль, ₽', 'rub'], ['margin', 'Маржинальность', 'pct'],
    ['ads', 'Реклама, ₽', 'rub'], ['drr', 'ДРР', 'pct'], ['price', 'Цена до СПП', 'rub'], ['spp', 'СПП', 'pct'], ['ctr', 'CTR', 'pct'],
    ['views', 'Показы', 'int'], ['clicks', 'Клики', 'int'], ['cancel_rate', 'Отмены', 'pct']];

  // Ozon: воронка карточки из аналитики Seller API (показы и корзина — только с подпиской Premium)
  const FUNNEL_ROWS = [['Воронка Ozon'],
    ['card_views', 'Показы карточки', 'int', 'Показы товара на Ozon: поиск, каталог и карточка (аналитика Seller API).'],
    ['tocart', 'В корзину', 'int', 'Добавления в корзину.'], ['cart_rate', 'Конверсия в корзину', 'pct', 'Добавления в корзину к показам.'],
    ['order_rate', 'Корзина → заказ', 'pct', 'Заказы к добавлениям в корзину.']];
  const HEAT_OZON = [['card_views', 'Показы карточки', 'int'], ['tocart', 'В корзину', 'int'], ['cart_rate', 'Конверсия в корзину', 'pct']];
  const MPS = [['wb', 'Wildberries'], ['ozon', 'Ozon'], ['all', 'Все маркетплейсы']];
  const MP_SHORT = {wb: 'WB', ozon: 'Ozon', all: 'МП'};
  // Показатели, которых у Ozon нет: СПП — только у WB, показы и клики рекламы — только в Ozon Performance API
  const WB_ONLY = new Set(['spp', 'price_after_spp', 'views', 'clicks', 'ctr', 'cpc', 'cpo']);

  const st = {preset: 'days:14', from: '', to: '', mp: 'wb', cabinet: '', brand: '', category: '', article: '', view: 'days', heat: 'orders', sort: 'orders_sum', desc: true,
    search: '', calc: '', data: null, poll: null};
  let root = null;
  try { Object.assign(st, JSON.parse(localStorage.getItem('wbops.rnp') || '{}'), {data: null, poll: null}); } catch (e) { /* без хранилища */ }
  if (!MP_SHORT[st.mp]) st.mp = 'wb';
  const remember = () => { try { localStorage.setItem('wbops.rnp', JSON.stringify({preset: st.preset, from: st.from, to: st.to, mp: st.mp, cabinet: st.cabinet, brand: st.brand, category: st.category, article: st.article, view: st.view, heat: st.heat, sort: st.sort, desc: st.desc})); } catch (e) { /* ignore */ } };
  const mpWord = () => MP_SHORT[st.mp] || 'WB';
  const hidden = key => st.mp === 'ozon' && WB_ONLY.has(key);
  const label = text => st.mp === 'wb' ? text : text.replace('Комиссия WB', 'Комиссия МП');

  function presets() {
    const t = new Date(), y = addDays(t, -1);
    const m0 = new Date(t.getFullYear(), t.getMonth(), 1), pm = new Date(t.getFullYear(), t.getMonth() - 1, 1);
    return [['today', 'Сегодня', t, t], ['yesterday', 'Вчера', y, y], ['days:7', 'Последние 7 дней', addDays(t, -6), t], ['days:14', 'Последние 14 дней', addDays(t, -13), t],
      ['days:30', 'Последние 30 дней', addDays(t, -29), t], ['month:0', `${MONTHS[t.getMonth()]} ${t.getFullYear()} · текущий`, m0, t],
      ['month:1', `${MONTHS[pm.getMonth()]} ${pm.getFullYear()}`, pm, new Date(t.getFullYear(), t.getMonth(), 0)], ['custom', 'Свой период…', null, null]];
  }
  function period() {
    if (st.preset === 'custom' && st.from && st.to) return [st.from, st.to];
    const p = presets().find(x => x[0] === st.preset) || presets()[3];
    return [iso(p[2]), iso(p[3])];
  }
  const query = () => { const [from, to] = period(); const q = new URLSearchParams({from, to, mp: st.mp}); ['cabinet', 'brand', 'category', 'article'].forEach(k => st[k] && q.set(k, st[k])); return q; };

  // ---------------------------------------------------------------- каркас
  async function render(section) {
    root = section;
    root.className = 'panel rv-page rnp-page';
    root.innerHTML = `<div class="db-head"><h2>Рука на пульсе</h2><span class="muted db-fresh" data-fresh></span></div>
      <div class="db-filters" data-filters></div>
      <div data-notes></div>
      <div class="rnp-kpis" data-kpis></div>
      <div class="rnp-bar"><div class="rnp-tabs" role="tablist">
        ${[['days', 'По дням'], ['articles', 'По артикулам'], ['heat', 'Тепловая карта'], ['calc', 'Калькулятор гипотез']].map(([k, t]) => `<button type="button" role="tab" data-view="${k}">${t}</button>`).join('')}
      </div><div class="db-bar-actions"><button type="button" class="db-ghost" data-sync></button></div></div>
      <div data-body><p class="muted">Загрузка…</p></div>
      <section class="rnp-notes" data-notebook></section>`;
    root.querySelectorAll('[data-view]').forEach(b => b.onclick = () => { st.view = b.dataset.view; remember(); drawBody(); });
    root.querySelector('[data-sync]').onclick = sync;
    await load();
  }

  async function load() {
    let d;
    try { const r = await fetch('/api/rnp?' + query()); d = await r.json(); if (!r.ok) throw new Error(d.detail || 'Ошибка'); }
    catch (e) { root.querySelector('[data-body]').innerHTML = `<p class="rv-error">${esc(e.message || 'Нет связи с панелью.')}</p>`; return; }
    st.data = d; remember();
    const b = root.querySelector('[data-sync]');
    if (b && !b.disabled) { b.textContent = `↻ Обновить из ${mpWord()}`; b.title = `Загрузить свежие заказы и рекламу из API ${st.mp === 'all' ? 'WB и Ozon' : mpWord()}`; }
    drawFilters(); drawNotes(); drawKpis(); drawBody(); drawNotebook();
    if (d.state?.status === 'running') waitSync();
  }

  function drawFilters() {
    const o = st.data.options || {}, [from, to] = period();
    const opt = (v, t, cur) => `<option value="${esc(v)}" ${String(v) === String(cur) ? 'selected' : ''}>${esc(t)}</option>`;
    const arts = (o.articles || []).filter(a => (!st.brand || a.brand === st.brand) && (!st.category || a.category === st.category));
    // Кабинеты выбранного маркетплейса — группами по юрлицу; список показываем, если кабинетов больше одного
    const all = st.data.cabinets || [], cabs = st.mp === 'all' ? all : all.filter(c => c.mp === st.mp);
    const cabName = c => st.mp === 'all' ? `${c.name} · ${cabs.filter(x => x.name === c.name).map(x => MP_SHORT[x.mp]).join(' + ')}` : c.name;
    const uniq = [...new Map(cabs.map(c => [c.name, c])).values()];       // один кабинет WB и Ozon с тем же названием — один пункт
    const legal = [...new Set(uniq.map(c => c.legal_name || ''))];
    const cabOpts = legal.length > 1
      ? legal.map(l => `<optgroup label="${esc(l || 'Без юрлица')}">${uniq.filter(c => (c.legal_name || '') === l).map(c => opt(c.name, cabName(c), st.cabinet)).join('')}</optgroup>`).join('')
      : uniq.map(c => opt(c.name, cabName(c) + (c.legal_name ? ' · ' + c.legal_name : ''), st.cabinet)).join('');
    const has = st.data.has_tokens_by_mp || {};
    root.querySelector('[data-filters]').innerHTML = `
      <select data-f="preset" aria-label="Период" class="db-period">${presets().map(([k, t]) => opt(k, t, st.preset)).join('')}</select>
      ${st.preset === 'custom' ? `<label class="db-date">с <input type="date" data-f="from" value="${from}"></label><label class="db-date">по <input type="date" data-f="to" value="${to}"></label>` : ''}
      <select data-f="mp" aria-label="Маркетплейс">${MPS.map(([k, t]) => opt(k, t + (k !== 'all' && has[k] === false ? ' · нет ключей' : ''), st.mp)).join('')}</select>
      ${uniq.length > 1 || st.cabinet ? `<select data-f="cabinet" aria-label="Кабинет">${opt('', 'Все кабинеты', st.cabinet)}${cabOpts}</select>` : ''}
      <select data-f="brand" aria-label="Бренд">${opt('', 'Все бренды', st.brand)}${(o.brands || []).map(b => opt(b, b, st.brand)).join('')}</select>
      <select data-f="category" aria-label="Категория">${opt('', 'Все категории', st.category)}${(o.categories || []).map(b => opt(b, b, st.category)).join('')}</select>
      <select data-f="article" aria-label="Артикул">${opt('', 'Все артикулы', st.article)}${arts.map(a => opt(a.key, a.article, st.article)).join('')}</select>`;
    root.querySelectorAll('[data-f]').forEach(el => el.onchange = () => {
      const k = el.dataset.f;
      if (k === 'preset' && el.value === 'custom') { st.from = from; st.to = to; }
      st[k] = el.value;
      if (k === 'brand' || k === 'category') st.article = '';
      if (k === 'mp') {           // кабинет другого маркетплейса сбрасываем
        if (st.cabinet && st.mp !== 'all' && !all.some(c => c.mp === st.mp && c.name === st.cabinet)) st.cabinet = '';
        st.pick = new Set();
      }
      load();
    });
    const d = st.data, src = d.sources || {};
    const when = s => s ? new Date(s).toLocaleString('ru-RU', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'}) : null;
    const synced = Object.entries(d.synced_by_mp || {wb: d.synced_at}).map(([k, s]) => when(s) ? `API ${MP_SHORT[k]}: ${when(s)}` : `API ${MP_SHORT[k]} ещё не загружали`);
    root.querySelector('[data-fresh]').textContent = [...synced,
      d.stocks_at ? `остатки на ${d.stocks_at.slice(0, 16).replace(/(\d{4})-(\d\d)-(\d\d)/, '$3.$2')}` : '',
      src.orders === 'cache' ? 'заказы из кэша ОПиУ' : ''].filter(Boolean).join(' · ');
  }

  function drawNotes() {
    const d = st.data, notes = [], has = d.has_tokens_by_mp || {wb: d.has_tokens};
    const bySrc = d.sources_by_mp || {[st.mp]: d.sources || {}};
    Object.entries(bySrc).forEach(([mp, s]) => {
      const src = [], w = MP_SHORT[mp];
      if (s.orders === 'cache' || s.orders === 'mixed') src.push(`заказы ${s.orders === 'mixed' ? 'за часть дней ' : ''}— из кэша ОПиУ (обновляется с «Финансами»${mp === 'wb' ? ', без СПП' : ''})`);
      if (s.ads === 'report') src.push(mp === 'wb' ? 'реклама — из отчёта реализации WB (с задержкой до недели, без показов и кликов)'
        : 'расходы на продвижение — из начислений Ozon в кэше ОПиУ');
      if (src.length) notes.push(`${w}: сейчас ${src.join('; ')}. ${has[mp] ? `Нажмите «Обновить из ${mpWord()}» — данные подтянутся из API ${w} и дальше будут обновляться каждые 2 часа.`
        : mp === 'wb' ? 'Чтобы данные шли из API WB каждые 2 часа, добавьте токен кабинета в «Кабинеты маркетплейсов» (категории «Статистика» и «Продвижение»).'
          : 'Чтобы данные шли из Seller API Ozon каждые 2 часа, добавьте в «Кабинеты маркетплейсов» кабинет Ozon с Client ID и API-ключом и включённым сбором статистики.'}`);
      if (mp === 'ozon') {
        notes.push('Реклама Ozon — только расходы из начислений (группа «Реклама и продвижение»). Показы и клики рекламы Ozon отдаёт лишь в Performance API ' +
          '(отдельные ключи на performance.ozon.ru) — он не подключён, поэтому показов, кликов, CTR, CPC и CPO по Ozon нет.');
        if ((s.funnel_off || []).length) notes.push(`Воронка Ozon (показы карточки, корзина) недоступна для: ${s.funnel_off.join(', ')} — эти метрики Ozon отдаёт только с подпиской Premium.`);
      }
    });
    if (d.total?.no_cost > 0) notes.push(`${num(d.total.no_cost)} заказов по товарам без себестоимости — прибыль завышена. Задайте цены в «Товары → Номенклатура и себестоимость».`);
    Object.entries(d.states || {[st.mp]: d.state || {}}).forEach(([mp, s]) => {
      (s.errors || []).forEach(x => notes.push(`Загрузка из ${MP_SHORT[mp]}: ${x}`));
      if (s.status === 'error') notes.push(`Загрузка из ${MP_SHORT[mp]} не удалась: ${s.message}`);
    });
    const econ = Object.entries(d.economics_by_mp || {[st.mp]: d.economics || {}});
    root.querySelector('[data-notes]').innerHTML = notes.map(n => `<div class="db-note">${esc(n)}</div>`).join('') +
      `<p class="rnp-how muted">Прогноз прибыли по заказам: заказы × выкуп артикула − комиссия, эквайринг, логистика, реклама, хранение, себестоимость, подготовка и налог. ` +
      econ.map(([mp, e]) => `${MP_SHORT[mp]}: налог ${num(e.tax_rate, 1)}%, доли — по отчёту ${MP_SHORT[mp]} за ${ru(e.window?.[0])} – ${ru(e.window?.[1])}.`).join(' ') + '</p>';
  }

  function drawKpis() {
    const t = st.data.total || {};
    const k = (title, value, sub, cls = '') => `<div class="rnp-kpi ${cls}"><span>${esc(title)}</span><b>${value}</b><small>${sub}</small></div>`;
    root.querySelector('[data-kpis]').innerHTML = [
      k('Заказы', `${num(t.orders)} шт`, `${num(t.orders_sum)} ₽ · ${num(t.orders_per_day, 1)} в день`),
      k('Прогноз выручки', `${num(t.revenue)} ₽`, `выкуп ${F.pct(t.buyout)}`),
      k('Прогноз прибыли', `${num(t.profit)} ₽`, `маржа ${F.pct(t.margin)} · ROI ${F.pct(t.roi)}`, t.profit < 0 ? 'bad' : 'good'),
      k('Реклама', `${num(t.ads)} ₽`, `ДРР ${F.pct(t.drr)}${t.cpo != null && st.mp !== 'ozon' ? ` · CPO ${num(t.cpo)} ₽` : ''}`),
      k(`Остаток ${mpWord()}`, `${num(t.stock)} шт`, t.orders_per_day ? `хватит на ${num(t.stock / t.orders_per_day)} дн.` : 'нет заказов'),
      k('План заказов', t.plan_orders_per_day ? F.pct(t.plan_done) : '—', t.plan_orders_per_day ? `${num(t.plan_fact_per_day, 1)} из ${num(t.plan_orders_per_day, 1)} в день` : 'задайте во вкладке «По артикулам»',
        t.plan_done == null ? '' : t.plan_done >= 100 ? 'good' : 'bad'),
    ].join('');
  }

  function drawBody() {
    root.querySelectorAll('[data-view]').forEach(b => { b.classList.toggle('active', b.dataset.view === st.view); b.setAttribute('aria-selected', String(b.dataset.view === st.view)); });
    ({days: drawDays, articles: drawArticles, heat: drawHeat, calc: drawCalc}[st.view] || drawDays)();
  }

  // ---------------------------------------------------------------- по дням
  function drawDays() {
    const d = st.data, days = [...d.days].reverse(), notes = {};
    (d.notes || []).forEach(n => (notes[n.day] = notes[n.day] || []).push(n));
    const today = iso(new Date());
    const head = `<tr><th class="rnp-sticky">Показатель</th><th class="rnp-total">Итого</th>${days.map(x => {
      const w = parse(x).getDay();
      return `<th class="${w === 0 || w === 6 ? 'we' : ''}">${ru(x).slice(0, 5)}<small>${WD[w]}${x === today ? ' · сегодня' : ''}</small></th>`; }).join('')}</tr>`;
    const cell = (key, fmt, v) => {
      const cls = (key === 'profit' || key === 'margin') && v != null && v !== 0 ? (v < 0 ? 'neg' : 'pos') : '';
      return `<td class="${cls}">${F[fmt](v)}</td>`;
    };
    const funnel = st.mp !== 'wb' && d.sources_by_mp?.ozon?.funnel;
    const list = DAY_ROWS.filter(r => r.length === 1 || !hidden(r[0]));
    if (funnel) list.splice(list.findIndex(r => r[0] === 'Прогноз по заказам'), 0, ...FUNNEL_ROWS);
    const tip = r => r[0] === 'ads' && st.mp !== 'wb' ? (st.mp === 'ozon' ? 'Расходы на продвижение Ozon из начислений.' : 'WB Продвижение и расходы на продвижение Ozon.')
      : r[0] === 'spp' && st.mp === 'all' ? 'Средняя СПП по заказам WB (у Ozon её нет).' : r[3];
    const rows = list.map(r => r.length === 1 ? `<tr class="rnp-group"><th class="rnp-sticky" colspan="${days.length + 2}">${esc(r[0])}</th></tr>`
      : `<tr class="${r[4] || ''}"><th class="rnp-sticky" ${tip(r) ? `title="${esc(tip(r))}"` : ''}>${esc(label(r[1]))}</th>${cell(r[0], r[2], d.total[r[0]])}${days.map(x => cell(r[0], r[2], d.by_day[x]?.[r[0]])).join('')}</tr>`).join('');
    const noteRow = `<tr class="rnp-noterow"><th class="rnp-sticky">Заметки</th><td></td>${days.map(x => `<td>${(notes[x] || []).length
      ? `<button type="button" class="rnp-pin" data-note-day="${x}" title="${esc(notes[x].map(n => (n.article ? n.article + ': ' : '') + n.text).join('\n'))}">✎ ${notes[x].length}</button>`
      : `<button type="button" class="rnp-pin empty" data-note-day="${x}" aria-label="Добавить заметку за ${ru(x)}">+</button>`}</td>`).join('')}</tr>`;
    root.querySelector('[data-body]').innerHTML = d.total.orders || d.total.ads
      ? `<div class="rnp-scroll"><table class="rnp-table rnp-days"><thead>${head}</thead><tbody>${rows}${noteRow}</tbody></table></div>`
      : empty();
    root.querySelectorAll('[data-note-day]').forEach(b => b.onclick = () => focusNote(b.dataset.noteDay));
  }
  const empty = () => `<div class="rv-empty"><b>Нет заказов за период</b><p class="muted">${st.data.has_tokens ? `Нажмите «Обновить из ${mpWord()}», чтобы загрузить заказы и рекламу.`
    : st.mp === 'ozon' ? 'Подключите кабинет Ozon с Client ID и API-ключом или обновите «Финансы».' : 'Подключите кабинет WB с токеном или обновите «Финансы».'}</p></div>`;

  // ---------------------------------------------------------------- по артикулам
  function drawArticles() {
    const d = st.data, month = period()[1].slice(0, 7);
    const q = st.search.trim().toLowerCase();
    let rows = d.articles.filter(r => !q || (r.article + ' ' + r.brand + ' ' + r.category).toLowerCase().includes(q));
    const key = st.sort, sign = st.desc ? -1 : 1;
    rows = rows.slice().sort((a, b) => {
      const x = a[key], y = b[key];
      if (key === 'article') return sign * String(x).localeCompare(String(y), 'ru');
      if (x == null && y == null) return 0; if (x == null) return 1; if (y == null) return -1;
      return sign * (x - y);
    });
    const th = (k, t) => `<th><button type="button" class="rnp-sort ${st.sort === k ? 'on' : ''}" data-sort="${k}">${esc(t)}${st.sort === k ? (st.desc ? ' ↓' : ' ↑') : ''}</button></th>`;
    const cols = ART_COLS.filter(([k]) => !hidden(k)), planOff = st.mp === 'all';      // план задаётся отдельно для WB и Ozon
    const planAttr = planOff ? 'disabled title="План задаётся отдельно для WB и для Ozon — выберите маркетплейс"' : '';
    const mLabel = MONTHS[Number(month.slice(5)) - 1].toLowerCase();
    st.pick = st.pick || new Set();
    root.querySelector('[data-body]').innerHTML = `<div class="rnp-tools"><input type="search" data-search placeholder="Поиск по артикулу, бренду, категории" value="${esc(st.search)}">
        <span class="muted">${rows.length} артикулов · ${planOff ? `план на ${mLabel} — сумма планов WB и Ozon; задаётся при выборе одного маркетплейса.` : `план ${mpWord()} на ${mLabel}: заказов в день и маржа, %. Сохраняется сразу.`}</span>
        <span class="rnp-pick"><button type="button" class="rv-link" data-pick-neg>Отметить с маржой ниже 0</button>
        <button type="button" class="db-ghost" data-reprice ${st.pick.size ? '' : 'disabled'}>₽ Изменить цену${st.pick.size ? ` (${st.pick.size})` : ''}</button></span></div>
      <div class="rnp-scroll"><table class="rnp-table rnp-arts"><thead><tr>${th('article', 'Артикул').replace('<th>', '<th class="rnp-sticky">')}
        ${cols.map(([k, t]) => th(k, t)).join('')}<th>План, шт/день</th><th>План маржи, %</th>${th('plan_done', '% плана')}</tr></thead>
      <tbody>${rows.map(r => `<tr><th class="rnp-sticky"><input type="checkbox" class="rnp-cb" data-pick="${esc(r.key)}" ${st.pick.has(r.key) ? 'checked' : ''} aria-label="Отметить ${esc(r.article)}"> <b>${esc(r.article)}</b><small>${esc([r.brand, r.category].filter(Boolean).join(' · '))}${r.economics?.source === 'кабинет' ? ' · доли по кабинету' : ''}</small></th>
        ${cols.map(([k, , f]) => `<td class="${(k === 'profit' || k === 'margin') && r[k] ? (r[k] < 0 ? 'neg' : 'pos') : ''}${k === 'stock_days' && r[k] != null && r[k] < 14 ? ' warn' : ''}">${F[f](r[k])}</td>`).join('')}
        <td><input type="number" min="0" step="0.5" inputmode="decimal" data-plan="${esc(r.key)}" data-k="orders_per_day" value="${r.plan_orders_per_day ?? ''}" ${planAttr} aria-label="План заказов в день ${esc(r.article)}"></td>
        <td><input type="number" min="-100" max="100" step="1" inputmode="decimal" data-plan="${esc(r.key)}" data-k="margin" value="${r.plan_margin == null ? '' : Math.round(r.plan_margin * 10) / 10}" ${planAttr} aria-label="План маржи ${esc(r.article)}"></td>
        <td class="${r.plan_done == null ? '' : r.plan_done >= 100 ? 'pos' : 'neg'}">${F.pct(r.plan_done)}${r.plan_margin != null && r.margin != null ? `<small>маржа ${r.margin >= r.plan_margin ? '✓' : '✗'}</small>` : ''}</td></tr>`).join('')
        || `<tr><td colspan="${cols.length + 4}" class="muted">Ничего не найдено.</td></tr>`}</tbody></table></div>`;
    const s = root.querySelector('[data-search]');
    s.oninput = () => { st.search = s.value; const pos = s.selectionStart; drawArticles(); const n = root.querySelector('[data-search]'); n.focus(); n.setSelectionRange(pos, pos); };
    root.querySelectorAll('[data-sort]').forEach(b => b.onclick = () => { if (st.sort === b.dataset.sort) st.desc = !st.desc; else { st.sort = b.dataset.sort; st.desc = b.dataset.sort !== 'article'; } remember(); drawArticles(); });
    root.querySelectorAll('[data-plan]').forEach(inp => inp.onchange = () => savePlan(inp.dataset.plan, month));
    root.querySelectorAll('[data-pick]').forEach(cb => cb.onchange = () => { cb.checked ? st.pick.add(cb.dataset.pick) : st.pick.delete(cb.dataset.pick); drawArticles(); });
    root.querySelector('[data-pick-neg]').onclick = () => { rows.filter(r => r.margin != null && r.margin < 0).forEach(r => st.pick.add(r.key)); drawArticles(); };
    root.querySelector('[data-reprice]').onclick = () => {
      const keys = [...st.pick].join(',');
      location.href = '/repricer?sel=' + encodeURIComponent(keys);
    };
  }
  async function savePlan(key, month) {
    const get = k => { const el = root.querySelector(`[data-plan="${CSS.escape(key)}"][data-k="${k}"]`); return el && el.value !== '' ? Number(el.value) : null; };
    const els = root.querySelectorAll(`[data-plan="${CSS.escape(key)}"]`);
    try {
      const r = await fetch('/api/rnp/plan', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({article: key, month, orders_per_day: get('orders_per_day'), margin: get('margin'), mp: st.mp})});
      if (!r.ok) throw new Error((await r.json()).detail || 'Не сохранилось');
      els.forEach(e => { e.classList.add('saved'); setTimeout(() => e.classList.remove('saved'), 1200); });
      st.data = await (await fetch('/api/rnp?' + query())).json();
      drawKpis();                                  // таблицу не перерисовываем, чтобы не сбить ввод
    } catch (e) { els.forEach(x => x.classList.add('err')); alert(e.message); }
  }

  // ---------------------------------------------------------------- тепловая карта
  async function drawHeat() {
    const body = root.querySelector('[data-body]');
    const heat = [...HEAT.filter(([k]) => !hidden(k)), ...(st.mp !== 'wb' && st.data.sources_by_mp?.ozon?.funnel ? HEAT_OZON : [])];
    const def = heat.find(h => h[0] === st.heat) || heat[0];
    body.innerHTML = `<div class="rnp-tools"><label>Показатель <select data-heat>${heat.map(([k, t]) => `<option value="${k}" ${k === def[0] ? 'selected' : ''}>${t}</option>`).join('')}</select></label>
      <span class="muted" data-legend></span></div><div data-heatbody><p class="muted">Загрузка…</p></div>`;
    body.querySelector('[data-heat]').onchange = e => { st.heat = e.target.value; remember(); drawHeat(); };
    const q = query(); q.set('metric', def[0]);
    let h;
    try { h = await (await fetch('/api/rnp/heatmap?' + q)).json(); } catch (e) { body.querySelector('[data-heatbody]').innerHTML = '<p class="rv-error">Нет связи с панелью.</p>'; return; }
    if (st.view !== 'heat') return;
    const vals = Object.values(h.cells || {}).filter(v => v != null && !isNaN(v));
    if (!h.rows?.length || !vals.length) { body.querySelector('[data-heatbody]').innerHTML = empty(); return; }
    const diverging = def[0] === 'profit' || def[0] === 'margin';
    const sorted = vals.slice().sort((a, b) => a - b), q95 = sorted[Math.floor(sorted.length * 0.95)] || sorted[sorted.length - 1];
    const lo = sorted[Math.floor(sorted.length * 0.05)];
    const maxAbs = Math.max(Math.abs(lo), Math.abs(q95)) || 1;
    const SEQ = ['#f4f1fd', '#e4dcfb', '#cbbcf6', '#a993ee', '#8668e0', '#6847c8', '#4c2fa3'];
    const NEG = ['#fbe3e3', '#f3b9b9', '#e48585', '#c94f4f'], POS = ['#e2f1e2', '#b8dcb8', '#7fbd80', '#4c9550'];
    const color = v => {
      if (v == null || isNaN(v)) return ['', ''];
      if (diverging) {
        const r = Math.min(1, Math.abs(v) / maxAbs); if (r < 0.04) return ['#f1f2f4', ''];
        const i = Math.min(3, Math.floor(r * 4)); return [(v < 0 ? NEG : POS)[i], i === 3 ? 'light' : ''];
      }
      const top = q95 || 1, r = Math.max(0, Math.min(1, v / top)), i = Math.min(SEQ.length - 1, Math.floor(r * SEQ.length));
      return [SEQ[i], i >= 4 ? 'light' : ''];
    };
    const days = h.days, fmt = F[def[2]];
    body.querySelector('[data-legend]').innerHTML = diverging
      ? `<span class="rnp-leg"><i style="background:${NEG[3]}"></i>убыток · <i style="background:#f1f2f4"></i>около нуля · <i style="background:${POS[3]}"></i>прибыль</span>`
      : `<span class="rnp-leg">меньше ${SEQ.map(c => `<i style="background:${c}"></i>`).join('')} больше</span>`;
    body.querySelector('[data-heatbody]').innerHTML = `<div class="rnp-scroll"><table class="rnp-table rnp-heat"><thead><tr><th class="rnp-sticky">Артикул</th><th class="rnp-total">За период</th>${days.map(x => {
      const w = parse(x).getDay(); return `<th class="${w === 0 || w === 6 ? 'we' : ''}">${ru(x).slice(0, 5)}<small>${WD[w]}</small></th>`; }).join('')}</tr></thead>
      <tbody>${h.rows.map(r => `<tr><th class="rnp-sticky">${esc(r.article)}</th><td class="rnp-total">${fmt(r.total)}</td>${days.map(x => {
        const v = h.cells[`${x}|${r.key}`], [bg, cls] = color(v);
        return `<td class="${cls}" style="${bg ? `background:${bg}` : ''}" title="${esc(r.article)} · ${ru(x)} · ${esc(def[1])}: ${fmt(v)}">${v == null ? '' : fmt(v)}</td>`; }).join('')}</tr>`).join('')}</tbody></table></div>`;
  }

  // ---------------------------------------------------------------- калькулятор гипотез
  function drawCalc() {
    const d = st.data, arts = d.articles.filter(r => r.orders);
    if (!arts.length) { root.querySelector('[data-body]').innerHTML = empty(); return; }
    const row = arts.find(r => r.key === st.calc) || arts[0];
    st.calc = row.key;
    const e = row.economics || {}, tax = d.economics?.tax_rate || 0;
    const raw = {price: row.price || row.current_price || 0, dprice: 0, drr: row.drr || 0, buyout: (e.buyout || 0) * 100, commission: (e.commission || 0) * 100,
      acquiring: (e.acquiring || 0) * 100, logistics: e.logistics_per_order || 0, cost: row.cost || 0, prep: row.prep_cost || 0, opd: row.orders_per_day || 0, dorders: 0};
    const base = Object.fromEntries(Object.entries(raw).map(([k, v]) => [k, Math.round(v * 100) / 100]));
    const fields = [['price', 'Цена продавца, ₽', 1], ['dprice', 'Изменение цены, %', 1], ['dorders', 'Изменение заказов, %', 1], ['drr', 'ДРР от заказов, %', 0.1],
      ['buyout', 'Выкуп, %', 0.5], ['commission', `${label('Комиссия WB')}, % от выручки`, 0.1], ['acquiring', 'Эквайринг, %', 0.1], ['logistics', 'Логистика на заказ, ₽', 1],
      ['cost', 'Себестоимость, ₽', 1], ['prep', 'Подготовка, ₽', 1], ['opd', 'Заказов в день сейчас', 0.5]];
    root.querySelector('[data-body]').innerHTML = `<div class="rnp-calc"><form class="rnp-calc-form" data-calc>
        <label class="wide">Артикул <select name="art">${arts.map(r => `<option value="${esc(r.key)}" ${r.key === row.key ? 'selected' : ''}>${esc(r.article)}</option>`).join('')}</select></label>
        ${fields.map(([k, t, s]) => `<label>${t}<input type="number" step="${s}" name="${k}" value="${base[k]}"></label>`).join('')}
        <p class="muted wide">Значения подставлены по артикулу за выбранный период. Налог ${num(tax, 1)}% с ${num((e.paid_share || 1) * 100)}% выручки (как в ОПиУ).</p></form>
      <div class="rnp-calc-out" data-out></div></div>`;
    const form = root.querySelector('[data-calc]');
    const calc = v => {
      const P = v.price * (1 + v.dprice / 100), b = v.buyout / 100, rev = P * b;
      const comm = rev * v.commission / 100, acq = rev * v.acquiring / 100, ads = P * v.drr / 100, cogs = b * (v.cost + v.prep);
      const taxv = rev * (e.paid_share || 1) * tax / 100;
      const profit = rev - comm - acq - v.logistics - ads - cogs - taxv;
      const opd = v.opd * (1 + v.dorders / 100);
      const beDrr = P ? (rev - comm - acq - v.logistics - cogs - taxv) / P * 100 : null;
      return {P, rev, comm, acq, log: v.logistics, ads, cogs, tax: taxv, profit, margin: rev ? profit / rev * 100 : null, roi: cogs ? profit / cogs * 100 : null,
        opd, day: profit * opd, month: profit * opd * 30, beDrr};
    };
    const read = () => { const v = {}; fields.forEach(([k]) => { v[k] = Number(form.elements[k].value) || 0; }); return v; };
    const draw = () => {
      const v = read(), now = calc({...base, opd: v.opd}), hyp = calc(v);
      const line = (t, k, f = 'rub', strong) => { const a = now[k], b = hyp[k], diff = a != null && b != null && Math.abs(b - a) >= (f === 'pct' ? 0.05 : 0.5) ? b - a : null;
        return `<tr class="${strong ? 'strong' : ''}"><th>${t}</th><td>${F[f](a)}</td><td>${F[f](b)}</td><td class="${diff == null || k === 'P' ? '' : (['profit', 'margin', 'roi', 'day', 'month', 'rev', 'beDrr', 'opd'].includes(k) ? diff > 0 : diff < 0) ? 'pos' : 'neg'}">${diff == null || Math.abs(diff) < 0.005 ? '' : (diff > 0 ? '+' : '') + F[f](diff)}</td></tr>`; };
      root.querySelector('[data-out]').innerHTML = `<table class="rnp-table rnp-calc-table"><thead><tr><th>На один заказ</th><th>Сейчас</th><th>Гипотеза</th><th>Разница</th></tr></thead><tbody>
        ${line('Цена продавца', 'P')}${line('Выручка с учётом выкупа', 'rev')}${line(label('Комиссия WB'), 'comm')}${line('Эквайринг', 'acq')}${line('Логистика', 'log')}
        ${line('Реклама', 'ads')}${line('Себестоимость и подготовка', 'cogs')}${line('Налог', 'tax')}${line('Прибыль с заказа', 'profit', 'rub', true)}
        ${line('Маржинальность', 'margin', 'pct', true)}${line('ROI', 'roi', 'pct')}${line('ДРР безубыточности', 'beDrr', 'pct')}
        ${line('Заказов в день', 'opd', 'rub2')}${line('Прибыль в день', 'day', 'rub', true)}${line('Прибыль за 30 дней', 'month', 'rub', true)}</tbody></table>
        <button type="button" class="db-ghost" data-save-hyp>✎ Сохранить гипотезу в заметки</button>`;
      root.querySelector('[data-save-hyp]').onclick = () => {
        const parts = [];
        if (v.dprice) parts.push(`цена ${v.dprice > 0 ? '+' : ''}${num(v.dprice, 1)}%`);
        if (v.dorders) parts.push(`заказы ${v.dorders > 0 ? '+' : ''}${num(v.dorders, 1)}%`);
        if (Math.abs(v.drr - base.drr) > 0.05) parts.push(`ДРР ${num(v.drr, 1)}%`);
        if (Math.abs(v.buyout - base.buyout) > 0.05) parts.push(`выкуп ${num(v.buyout, 1)}%`);
        if (Math.abs(v.cost - base.cost) > 0.5) parts.push(`себестоимость ${num(v.cost)} ₽`);
        const text = `Гипотеза: ${parts.join(', ') || 'без изменений'} → прибыль с заказа ${num(hyp.profit)} ₽ (сейчас ${num(now.profit)} ₽), маржа ${F.pct(hyp.margin)}, в день ${num(hyp.day)} ₽`;
        addNote(iso(new Date()), row.key, text);
      };
    };
    form.oninput = e => { if (e.target.name === 'art') return; draw(); };
    form.elements.art.onchange = () => { st.calc = form.elements.art.value; drawCalc(); };
    form.onsubmit = e => e.preventDefault();
    draw();
  }

  // ---------------------------------------------------------------- заметки
  function drawNotebook() {
    const d = st.data, box = root.querySelector('[data-notebook]'), [from, to] = period();
    const today = iso(new Date()), def = today >= from && today <= to ? today : to;
    box.innerHTML = `<h3>Заметки и гипотезы</h3>
      <form class="rnp-note-form" data-note-form><input type="date" name="day" value="${def}" aria-label="Дата">
        <select name="article" aria-label="Артикул"><option value="">Без артикула</option>${d.articles.map(a => `<option value="${esc(a.key)}">${esc(a.article)}</option>`).join('')}</select>
        <input name="text" maxlength="500" placeholder="Что сделали или хотите проверить: подняли цену, запустили рекламу…" aria-label="Текст заметки" required>
        <button class="db-ghost" type="submit">Добавить</button></form>
      <ul class="rnp-note-list">${(d.notes || []).slice().reverse().map(n => `<li><b>${ru(n.day)}</b>${n.article ? `<span class="rnp-chip">${esc((d.articles.find(a => a.key === n.article) || {}).article || n.article)}</span>` : ''}
        <span>${esc(n.text)}</span><small class="muted">${esc(n.author || '')}</small><button type="button" class="rnp-del" data-del="${n.id}" aria-label="Удалить заметку">×</button></li>`).join('')
        || '<li class="muted">Заметок за период нет. Записывайте изменения цены, рекламы, поставки — потом легко сравнить с цифрами.</li>'}</ul>`;
    const f = box.querySelector('[data-note-form]');
    f.onsubmit = e => { e.preventDefault(); addNote(f.elements.day.value, f.elements.article.value, f.elements.text.value); };
    box.querySelectorAll('[data-del]').forEach(b => b.onclick = async () => {
      b.disabled = true; await fetch('/api/rnp/notes/' + b.dataset.del, {method: 'DELETE'}); load();
    });
  }
  function focusNote(day) {
    const f = root.querySelector('[data-note-form]'); if (!f) return;
    f.elements.day.value = day; f.scrollIntoView({behavior: 'smooth', block: 'center'}); f.elements.text.focus();
  }
  async function addNote(day, article, text) {
    if (!String(text || '').trim()) return;
    const r = await fetch('/api/rnp/notes', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({day, article, text})});
    if (!r.ok) { alert((await r.json().catch(() => ({}))).detail || 'Не сохранилось'); return; }
    try { st.data = await (await fetch('/api/rnp?' + query())).json(); } catch (e) { return; }
    drawNotebook();
    if (st.view === 'days') drawDays();
    const saved = root.querySelector('[data-save-hyp]'); if (saved) { saved.textContent = '✓ Сохранено в заметки'; setTimeout(() => { saved.textContent = '✎ Сохранить гипотезу в заметки'; }, 2000); }
  }

  // ---------------------------------------------------------------- загрузка из WB
  async function sync(e) {
    const b = e.currentTarget; b.disabled = true;
    try {
      const r = await fetch('/api/rnp/sync?mp=' + st.mp, {method: 'POST'}); const d = await r.json();
      if (!r.ok) throw new Error(d.detail || 'Не удалось');
      b.textContent = '↻ Загружаю…'; waitSync();
    } catch (err) { alert(err.message); b.disabled = false; }
  }
  function waitSync() {
    clearTimeout(st.poll);
    const b = root.querySelector('[data-sync]'); if (b) { b.disabled = true; b.textContent = '↻ Загружаю…'; }
    st.poll = setTimeout(async () => {
      if (!root?.isConnected) return;
      const s = await (await fetch('/api/rnp/status?mp=' + st.mp)).json().catch(() => ({}));
      if (s.status === 'running') { if (b) b.title = s.message || ''; return waitSync(); }
      if (b) { b.disabled = false; b.textContent = `↻ Обновить из ${mpWord()}`; }

      load();
    }, 4000);
  }

  const style = document.createElement('style');
  style.textContent = `
.rnp-how{font-size:12px;margin:0 0 12px}
.rnp-kpis{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;margin-bottom:16px}
.rnp-kpi{background:#f4f5f7;border-radius:12px;padding:11px 13px;min-width:0;display:flex;flex-direction:column;gap:3px}
.rnp-kpi span{font-size:12px;color:#4b5563}.rnp-kpi b{font-size:20px;font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.rnp-kpi small{font-size:12px;color:#6b7280}
.rnp-kpi.good{background:#e3efe3}.rnp-kpi.bad{background:#f6e0e0}
.rnp-bar{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.rnp-tabs{display:flex;gap:4px;background:#eef0f4;border-radius:10px;padding:3px;flex-wrap:wrap}
.rnp-tabs button{border:0;background:none;border-radius:8px;padding:7px 13px;font:inherit;font-size:13px;font-weight:600;color:#4b5563;cursor:pointer}
.rnp-tabs button.active{background:#fff;color:#111827;box-shadow:0 1px 2px #0000001a}
.rnp-scroll{overflow:auto;max-height:72vh;border:1px solid #eceef2;border-radius:12px}
.rnp-table{border-collapse:separate;border-spacing:0;font-size:13px;font-variant-numeric:tabular-nums;width:max-content;min-width:100%}
.rnp-table th,.rnp-table td{text-transform:none;letter-spacing:normal;padding:7px 10px;border-bottom:1px solid #f0f1f4;text-align:right;white-space:nowrap;background:#fff}
.rnp-table thead th{position:sticky;top:0;z-index:2;background:#f8f9fb;font-weight:600;color:#374151;font-size:12px}
.rnp-table thead th small{display:block;font-weight:400;color:#6b7280}.rnp-table thead th.we{color:#9a3412}
.rnp-table .rnp-sticky{position:sticky;left:0;z-index:1;text-align:left;font-weight:500;color:#1f2328;min-width:190px;max-width:260px;white-space:normal;box-shadow:1px 0 0 #eceef2}
.rnp-table thead .rnp-sticky{z-index:3}.rnp-table .rnp-sticky small{display:block;color:#6b7280;font-weight:400;font-size:11px}
.rnp-table .rnp-total{background:#f7f5ff;font-weight:600}
.rnp-group th{background:#f4f5f7!important;font-size:11px;letter-spacing:.04em!important;text-transform:uppercase!important;color:#6b7280;font-weight:700;text-align:left}
.rnp-table tr.strong td,.rnp-table tr.strong th{font-weight:700}
.rnp-table td.pos{color:#2f6b2f}.rnp-table td.neg{color:#b42323}.rnp-table td.warn{color:#9a3412;font-weight:600}
.rnp-table tbody tr:hover td,.rnp-table tbody tr:hover th{background:#faf8ff}
.rnp-pin{border:1px solid #ddd6fe;background:#f5f3ff;color:#5b21b6;border-radius:7px;padding:2px 8px;font:inherit;font-size:12px;cursor:pointer}
.rnp-pin.empty{background:#fff;border-color:#e5e7eb;color:#9ca3af}.rnp-pin:hover{border-color:#8b5cf6;color:#5b21b6}
.rnp-tools{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:10px;font-size:13px}
.rnp-tools input[type=search],.rnp-tools select{height:38px;border:1px solid #e3e5ea;border-radius:10px;padding:0 12px;font:inherit;min-width:260px}
.rnp-tools label{display:flex;gap:8px;align-items:center}
.rnp-sort{border:0;background:none;font:inherit;font-weight:600;color:inherit;cursor:pointer;padding:0;white-space:nowrap}.rnp-sort.on{color:#5b21b6}
.rnp-arts input{width:74px;height:30px;border:1px solid #e3e5ea;border-radius:7px;padding:0 6px;font:inherit;text-align:right}
.rnp-arts input.saved{border-color:#7fbd80;background:#ecfdf0}.rnp-arts input.err{border-color:#e48585}
.rnp-pick{display:flex;gap:10px;align-items:center;margin-left:auto}.rnp-cb{margin:0 4px 0 0;vertical-align:middle}
.rnp-arts td small{display:block;font-size:11px;color:#6b7280}
.rnp-heat td{min-width:58px;font-size:12px;border-right:1px solid #fff}.rnp-heat td.light{color:#fff}
.rnp-leg{display:inline-flex;align-items:center;gap:4px}.rnp-leg i{display:inline-block;width:16px;height:12px;border-radius:3px}
.rnp-calc{display:grid;grid-template-columns:minmax(260px,380px) 1fr;gap:18px;align-items:start}
.rnp-calc-form{display:grid;grid-template-columns:1fr 1fr;gap:10px;background:#f8f9fb;border-radius:12px;padding:14px}
.rnp-calc-form label{min-width:0;display:flex;flex-direction:column;gap:4px;font-size:12px;color:#4b5563}.rnp-calc-form .wide{grid-column:1/-1}
.rnp-calc-form input,.rnp-calc-form select{width:100%;min-width:0;box-sizing:border-box;height:36px;border:1px solid #e3e5ea;border-radius:8px;padding:0 9px;font:inherit;font-size:14px;background:#fff;color:#111827}
.rnp-calc-form p{font-size:12px;margin:0}
.rnp-calc-out{display:flex;flex-direction:column;gap:10px;align-items:flex-start}.rnp-calc-table{border:1px solid #eceef2;border-radius:12px;overflow:hidden}
.rnp-calc-table th:first-child{text-align:left;font-weight:500}
.rnp-notes{margin-top:22px}.rnp-notes h3{font-size:17px;margin:0 0 10px}
.rnp-note-form{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.rnp-note-form input,.rnp-note-form select{height:38px;border:1px solid #e3e5ea;border-radius:9px;padding:0 10px;font:inherit;font-size:13px;background:#fff}
.rnp-note-form input[name=text]{flex:1;min-width:240px}
.rnp-note-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:6px}
.rnp-note-list li{display:flex;gap:10px;align-items:center;background:#f8f9fb;border-radius:9px;padding:8px 12px;font-size:13px;flex-wrap:wrap}
.rnp-note-list li span:not(.rnp-chip){flex:1;min-width:200px}.rnp-chip{background:#ede9fe;color:#5b21b6;border-radius:6px;padding:1px 7px;font-size:12px;font-weight:600}
.rnp-del{border:0;background:none;font-size:18px;color:#9ca3af;cursor:pointer;line-height:1}.rnp-del:hover{color:#b42323}
.wb-member [data-nav=rnp]{display:none!important}
@media(max-width:1300px){.rnp-kpis{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:900px){.rnp-calc{grid-template-columns:1fr}}
@media(max-width:600px){.rnp-kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.rnp-kpi b{font-size:16px;white-space:normal}.rnp-table .rnp-sticky{min-width:130px}.rnp-tools input[type=search]{min-width:0;width:100%}}`;
  document.head.appendChild(style);

  return {render};
})();
