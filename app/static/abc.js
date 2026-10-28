/* WB OPS: «ABC-анализ» — сегментация ассортимента по выручке, оборачиваемости и прибыли и панель неликвида
   (как лист «ABC» таблицы продавца), по данным WB и Ozon. */
window.WBOPSAbc = (() => {
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const iso = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
  const ru = s => s ? s.split('-').reverse().join('.') : '';
  const num = (v, digits = 0) => v == null || isNaN(v) ? '—' : Number(v).toLocaleString('ru-RU', {minimumFractionDigits: digits, maximumFractionDigits: digits});
  const rub = v => v == null ? '—' : `${num(v)} ₽`;
  const pct = v => v == null ? '—' : `${num(v, 1)}%`;
  const KEY = 'wbops.abc', PAGE = 200;
  const STATUS_CLS = {'Дефицит': 'def', 'В норме': 'ok', 'Избыток запаса': 'exc', 'Критический неликвид': 'dead', 'Нет остатка': 'none'};
  const RECS = {
    A: '🟢 Группа A: бесперебойное наличие (запас 30–45 дней), контроль OOS, приоритетная поставка.',
    B: '🟡 Группа B: оптимизация партий поставок (запас 45–60 дней), удержание маржинальности и спроса.',
    C: '🔴 Группа C: запрет дозакупок, глубокий демпинг, акции, вывод неликвидных SKU из оборота.',
  };
  const COLS = [
    ['brand', 'Бренд', r => esc(r.brand), 'l'], ['category', 'Категория', r => esc(r.category), 'l'],
    ['article', 'Артикул', r => `<b>${esc(r.article)}</b>`, 'l'], ['name', 'Наименование', r => `<span class="ab-name" title="${esc(r.name)}">${esc(r.name)}</span>`, 'l'],
    ['sold', 'Продажи, шт', r => num(r.sold)], ['revenue', 'Выручка, ₽', r => rub(r.revenue)], ['share', 'Доля выручки', r => pct(r.share)],
    ['cum_share', 'Накоп. доля', r => pct(r.cum_share)],
    ['abc_revenue', 'ABC выручка', r => badge(r.abc_revenue), 'c'], ['abc_turnover', 'ABC оборач.', r => badge(r.abc_turnover), 'c'],
    ['abc_profit', 'ABC прибыль', r => badge(r.abc_profit), 'c'], ['abc', 'ABC', r => `<b>${esc(r.abc)}</b>`, 'c'], ['group', 'Группа', r => badge(r.group), 'c'],
    ['profit', 'Прибыль, ₽', r => `<span class="${r.profit < 0 ? 'neg' : ''}">${rub(r.profit)}</span>`],
    ['stock', 'Остаток, шт', r => num(r.stock)], ['unit_cost', 'Себест. ед., ₽', r => rub(r.unit_cost)],
    ['stock_cost', 'Себест. остатка, ₽', r => rub(r.stock_cost)], ['per_day', 'Продажи в день', r => num(r.per_day, 1)],
    ['days_left', 'Запас, дн', r => r.days_left >= 999 ? '∞' : num(r.days_left)],
    ['status', 'Статус', r => `<span class="ab-st ${STATUS_CLS[r.status] || ''}">${esc(r.status)}</span>`, 'l'],
    ['restock', 'Подсорт 14 дн, шт', r => r.restock ? `<b>${num(r.restock)}</b>` : '0'],
  ];
  const HINTS = {
    abc_revenue: 'Артикулы по убыванию выручки: накопительная доля до 80% — A, до 95% — B, остальное — C.',
    abc_turnover: 'То же по проданным штукам.', group: 'Итоговая группа по большинству трёх букв ABC: AAC → A, ABB → B, CCA → C; все три разные — B.', abc_profit: 'То же по прибыли артикула; прибыль 0 или в минусе — C.',
    profit: 'Как на дашборде с фильтром по товару: выручка минус удержания и реклама артикула, себестоимость, подготовка и налог, плюс ретро-бонусы.',
    stock: 'Сейчас: склады маркетплейсов + свой склад (один раз, хотя его видят все маркетплейсы).',
    days_left: 'Остаток / продажи в день. ∞ — остаток без продаж.',
    status: 'Критический неликвид — итоговая группа C и запас больше 120 дн или остаток без продаж (у A и B такой запас — избыток); избыток — 60–120 дн; дефицит — меньше 15 дн при продажах.',
    restock: '14 × продажи в день − остаток. Для итоговой группы C — 0: дозакупать не нужно.',
  };
  const badge = g => `<span class="ab-g ab-${g}">${g}</span>`;

  let root = null;
  const st = {preset: 'days:30', from: '', to: '', mp: 'all', brand: '', category: '', group: '', status: '', q: '', cf: {}, sort: 'revenue', desc: true, shown: PAGE, data: null};
  try { Object.assign(st, JSON.parse(localStorage.getItem(KEY) || '{}'), {data: null, shown: PAGE}); } catch (e) { /* ignore */ }

  const PRESETS = [['days:30', 'Последние 30 дней', 30], ['days:14', 'Последние 14 дней', 14], ['days:60', 'Последние 60 дней', 60],
                   ['days:90', 'Последние 90 дней', 90], ['custom', 'Свой период…', 0]];
  function period() {
    if (st.preset === 'custom' && st.from && st.to) return [st.from, st.to];
    const end = addDays(new Date(), -1), n = (PRESETS.find(p => p[0] === st.preset) || PRESETS[0])[2];
    return [iso(addDays(end, -(n - 1))), iso(end)];
  }
  // Ширина столбцов: тянуть правый край заголовка; хранится в браузере, двойной клик — по содержимому
  const WKEY = 'wbops.abc.widths', MIN_W = 48, MAX_W = 700;
  let widths = {};
  try { widths = JSON.parse(localStorage.getItem(WKEY) || '{}') || {}; } catch (e) { widths = {}; }
  const saveWidths = () => { try { localStorage.setItem(WKEY, JSON.stringify(widths)); } catch (e) { /* ignore */ } };
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify({preset: st.preset, from: st.from, to: st.to, mp: st.mp, sort: st.sort, desc: st.desc})); } catch (e) { /* ignore */ } };

  async function render(section) {
    root = section;
    root.className = 'panel rv-page ab-page';
    root.innerHTML = `<div class="ab-filters" data-period></div><div data-body><p class="muted">Загрузка…</p></div>`;
    await load();
  }

  async function load() {
    const [from, to] = period();
    drawPeriod(from, to);
    const q = new URLSearchParams({from, to, mp: st.mp});
    st.exportUrl = '/api/abc/export?' + q;
    let r, d;
    try { r = await fetch('/api/abc?' + q); d = await r.json(); }
    catch (e) { root.querySelector('[data-body]').innerHTML = '<p class="rv-error">Нет связи с панелью.</p>'; return; }
    if (!r.ok) { root.querySelector('[data-body]').innerHTML = `<p class="rv-error">${esc(d.detail || 'Ошибка')}</p>`; return; }
    st.data = d; st.shown = PAGE; save();
    drawBody();
  }

  function drawPeriod(from, to) {
    const opt = (v, t, cur) => `<option value="${esc(v)}" ${v === cur ? 'selected' : ''}>${esc(t)}</option>`;
    root.querySelector('[data-period]').innerHTML = `
      <select data-p="preset" aria-label="Период">${PRESETS.map(([k, t]) => opt(k, t, st.preset)).join('')}</select>
      ${st.preset === 'custom' ? `<label class="ab-date">с <input type="date" data-p="from" value="${from}"></label><label class="ab-date">по <input type="date" data-p="to" value="${to}"></label>` : ''}
      <select data-p="mp" aria-label="Маркетплейс">${[['all', 'Все маркетплейсы'], ['wb', 'Wildberries'], ['ozon', 'Ozon']].map(([k, t]) => opt(k, t, st.mp)).join('')}</select>
      <span class="muted ab-range">${ru(from)} – ${ru(to)}</span>`;
    root.querySelectorAll('[data-p]').forEach(el => el.onchange = () => {
      if (el.dataset.p === 'preset' && el.value === 'custom') { st.from = from; st.to = to; }
      st[el.dataset.p] = el.value; load();
    });
  }

  // Фильтры по столбцам: текст — «содержит», ABC и статус — список, числа — условие: >100, <5, >=10, 10-50, 0
  const TEXT_COLS = ['brand', 'category', 'article', 'name', 'abc'], PICK_COLS = {abc_revenue: 'ABC', abc_turnover: 'ABC', abc_profit: 'ABC', group: 'ABC', status: null};
  const norm = v => String(v ?? '').toLowerCase().replace(/ё/g, 'е');
  const toNum = s => { const t = String(s).replace(/\s/g, '').replace(',', '.').replace('%', ''); return t === '' || isNaN(t) ? null : Number(t); };
  function numTest(expr) {
    const e = String(expr).replace(/\s/g, '').replace(/,/g, '.');
    if (!e) return null;
    let m = e.match(/^(>=|<=|>|<|=)?(-?[\d.]+)%?$/);
    if (m) {
      const x = Number(m[2]);
      return {'>=': v => v >= x, '<=': v => v <= x, '>': v => v > x, '<': v => v < x}[m[1]] || (v => Math.abs(v - x) < 0.5);
    }
    m = e.match(/^(-?[\d.]+)(?:-|\.\.|–)(-?[\d.]+)$/);
    if (m) { const a = Number(m[1]), b = Number(m[2]); return v => v >= Math.min(a, b) && v <= Math.max(a, b); }
    const none = () => false;                 // непонятное условие — ничего не показываем, поле подсвечивается
    none.bad = true;
    return none;
  }
  function colTests() {
    return Object.entries(st.cf || {}).filter(([, v]) => v !== '' && v != null).map(([k, v]) => {
      if (TEXT_COLS.includes(k)) { const q = norm(v); return r => norm(r[k]).includes(q); }
      if (k in PICK_COLS) return r => r[k] === v;
      const t = numTest(v);
      return t ? r => r[k] != null && t(k === 'days_left' && r[k] >= 999 ? Infinity : r[k]) : null;
    }).filter(Boolean);
  }

  function visible() {
    const q = st.q.toLowerCase().replace(/ё/g, 'е'), tests = colTests();
    const rows = st.data.rows.filter(r => (!st.brand || r.brand === st.brand) && (!st.category || r.category === st.category)
      && (!st.group || r.group === st.group) && (!st.status || r.status === st.status)
      && (!q || `${r.article} ${r.name} ${r.brand}`.toLowerCase().replace(/ё/g, 'е').includes(q)) && tests.every(t => t(r)));
    const k = st.sort, dir = st.desc ? -1 : 1;
    return rows.sort((a, b) => { const x = a[k] ?? -Infinity, y = b[k] ?? -Infinity;
      return (typeof x === 'string' ? x.localeCompare(y, 'ru') : x - y) * dir; });
  }

  function drawBody() {
    const d = st.data, k = d.kpi, box = root.querySelector('[data-body]');
    if (!d.marketplaces.length) { box.innerHTML = '<div class="rv-empty"><b>Нет данных маркетплейсов</b><p class="muted">В «Финансах» нажмите «Обновить данные МП» — ABC считается по тем же данным, что и дашборды.</p></div>'; return; }
    const total = d.count || 1;
    const uniq = key => [...new Set(d.rows.map(r => r[key]).filter(Boolean))].sort((a, b) => a.localeCompare(b, 'ru'));
    const opt = (v, t, cur) => `<option value="${esc(v)}" ${v === cur ? 'selected' : ''}>${esc(t)}</option>`;
    box.innerHTML = `
      <div class="ab-kpis">
        <div class="ab-kpi"><span>💰 Выручка за ${d.days} дн.</span><b>${rub(k.revenue)}</b><small>Реализовано за период · ${esc(d.marketplaces.join(' + '))}</small></div>
        <div class="ab-kpi"><span>📦 Стоимость остатков</span><b>${rub(k.stock_cost)}</b><small>Себестоимость на складах сейчас</small></div>
        <div class="ab-kpi warn"><span>⚠️ В неликвиде</span><b>${rub(k.dead_cost)}</b><small>Замороженные средства</small></div>
        <div class="ab-kpi warn"><span>📉 Доля неликвида</span><b>${pct(k.dead_share)}</b><small>От стоимости склада</small></div>
        <div class="ab-kpi bad"><span>🚨 Проблемных SKU</span><b>${num(k.dead_count)} SKU</b><small>Критический неликвид</small></div>
      </div>
      <div class="db-note">Учтены продажи кабинетов панели: ${esc(d.marketplaces.join(' и '))}. Свой склад общий для всех кабинетов, поэтому если товар продаётся ещё в других кабинетах или на Яндекс Маркете, без них он попадёт в «Неликвид» или «Избыток» ошибочно — подключите все кабинеты в «Кабинетах маркетплейсов».</div>
      ${k.no_cost ? `<div class="db-note">У ${num(k.no_cost)} артикулов с остатком нет себестоимости — стоимость их остатка не посчитана. Задайте цены в «Товары → Номенклатура и себестоимость».</div>` : ''}
      <div class="ab-summary">
        <table class="ab-mini"><thead><tr><th>Группа</th><th>Кол-во SKU</th><th>Доля SKU</th><th>Выручка, ₽</th><th>Доля выручки</th><th>Остаток, ₽</th><th title="Себестоимость остатка в статусе «Критический неликвид» — он бывает только у группы C">Неликвид, ₽</th></tr></thead>
          <tbody>${'ABC'.split('').map(g => { const x = d.groups[g]; return `<tr data-group="${g}" class="${st.group === g ? 'sel' : ''}" title="Показать группу ${g}"><td>${badge(g)} Группа ${g}</td><td>${num(x.count)}</td><td>${pct(x.count / total * 100)}</td><td>${rub(x.revenue)}</td><td>${pct(k.revenue ? x.revenue / k.revenue * 100 : 0)}</td><td>${rub(x.stock_cost)}</td><td class="${x.dead_cost ? 'ab-dead' : ''}">${rub(x.dead_cost)}</td></tr>`; }).join('')}</tbody>
          <tfoot><tr><td>Итого</td><td>${num(d.count)}</td><td>100%</td><td>${rub(k.revenue)}</td><td>100%</td><td>${rub(k.stock_cost)}</td><td>${rub(k.dead_cost)}</td></tr></tfoot></table>
        <div class="ab-recs"><b>📋 Рекомендации по ассортименту и запасам</b>${'ABC'.split('').map(g => `<p>${esc(RECS[g])}</p>`).join('')}
          <p class="muted">⚡ Группа — по большинству трёх букв ABC (выручка · оборачиваемость · прибыль): AAC → A, ABB → B, все разные → B. «Критический неликвид» — группа C и запас больше 120 дн или остаток без продаж, «Избыток» — 60–120 дн, «Норма» — 15–60 дн, «Дефицит» — меньше 15 дн.</p></div>
        <table class="ab-mini"><thead><tr><th>Статус остатка</th><th>Кол-во SKU</th><th>Доля SKU</th><th>Остаток, ₽</th></tr></thead>
          <tbody>${Object.entries(d.statuses).map(([s, x]) => `<tr data-status="${esc(s)}" class="${st.status === s ? 'sel' : ''}" title="Показать «${esc(s)}»"><td><span class="ab-st ${STATUS_CLS[s]}">${esc(s)}</span></td><td>${num(x.count)}</td><td>${pct(x.count / total * 100)}</td><td>${rub(x.stock_cost)}</td></tr>`).join('')}</tbody></table>
      </div>
      <div class="ab-filters ab-tools">
        <label class="rv-search"><span class="sr">Поиск</span><input type="search" data-f="q" value="${esc(st.q)}" placeholder="Артикул, название или бренд…"></label>
        <select data-f="brand" aria-label="Бренд">${opt('', 'Все бренды', st.brand)}${uniq('brand').map(b => opt(b, b, st.brand)).join('')}</select>
        <select data-f="category" aria-label="Категория">${opt('', 'Все категории', st.category)}${uniq('category').map(b => opt(b, b, st.category)).join('')}</select>
        <select data-f="group" aria-label="Группа ABC">${opt('', 'Все группы', st.group)}${'ABC'.split('').map(g => opt(g, 'Группа ' + g, st.group)).join('')}</select>
        <select data-f="status" aria-label="Статус">${opt('', 'Все статусы', st.status)}${Object.keys(d.statuses).map(s => opt(s, s, st.status)).join('')}</select>
        <span class="muted" data-count></span>
        <button type="button" class="db-ghost" data-widths-reset hidden title="Вернуть автоматическую ширину всем столбцам">↔ Ширина по умолчанию</button>
        <a class="db-ghost ab-export" href="${esc(st.exportUrl || '#')}" title="Все артикулы за период, без фильтров">⇩ Excel — все артикулы</a>
        <button type="button" class="db-ghost ab-dl" data-download title="То, что в таблице сейчас: с фильтрами и сортировкой">⇩ Скачать в Excel</button></div>
      <div class="ab-wrap" data-table></div>`;
    box.querySelectorAll('[data-f]').forEach(el => el[el.tagName === 'INPUT' ? 'oninput' : 'onchange'] = () => { st[el.dataset.f] = el.value; st.shown = PAGE; drawTable(); syncMini(); });
    box.querySelector('[data-download]').onclick = download;
    box.querySelector('[data-widths-reset]').onclick = () => { widths = {}; saveWidths(); drawTable(); };
    box.querySelectorAll('[data-group]').forEach(tr => tr.onclick = () => { st.group = st.group === tr.dataset.group ? '' : tr.dataset.group; drawBody(); });
    box.querySelectorAll('[data-status]').forEach(tr => tr.onclick = () => { st.status = st.status === tr.dataset.status ? '' : tr.dataset.status; drawBody(); });
    drawTable();
  }
  function syncMini() {
    root.querySelectorAll('[data-group]').forEach(tr => tr.classList.toggle('sel', st.group === tr.dataset.group));
    root.querySelectorAll('[data-status]').forEach(tr => tr.classList.toggle('sel', st.status === tr.dataset.status));
  }

  function filterCell(k) {
    const v = (st.cf || {})[k] ?? '';
    if (k in PICK_COLS) {
      const opts = PICK_COLS[k] ? PICK_COLS[k].split('') : Object.keys(st.data.statuses);
      return `<select data-cf="${k}" aria-label="Фильтр: ${esc(k)}"><option value="">все</option>${opts.map(o => `<option ${o === v ? 'selected' : ''}>${esc(o)}</option>`).join('')}</select>`;
    }
    const text = TEXT_COLS.includes(k);
    const bad = !text && v && numTest(v)?.bad;
    return `<input data-cf="${k}" value="${esc(v)}" class="${bad ? 'bad' : ''}" placeholder="${text ? 'содержит…' : '>0, 10-50'}"
      title="${text ? 'Часть текста' : 'Условие: >100, <5, >=10, <=0, 10-50 или число'}" aria-label="Фильтр столбца">`;
  }

  function drawTable() {
    const rows = visible(), box = root.querySelector('[data-table]');
    st.view = rows.map(r => r.key);
    const dl = root.querySelector('[data-download]');
    if (dl) { dl.disabled = !rows.length; dl.textContent = `⇩ Скачать в Excel (${num(rows.length)})`; }
    const focus = document.activeElement?.dataset?.cf, caret = focus ? document.activeElement.selectionStart : null;
    const active = Object.values(st.cf || {}).filter(v => v !== '' && v != null).length;
    root.querySelector('[data-count]').innerHTML = `Показано ${num(Math.min(rows.length, st.shown))} из ${num(rows.length)}`
      + (active ? ` · фильтров по столбцам: ${active} <button type="button" class="rv-link" data-cf-reset>сбросить</button>` : '');
    const fixed = COLS.some(([k]) => widths[k]), w = k => widths[k] || 90;   // новый столбец после сохранённых ширин — 90 px
    box.innerHTML = `<table class="ab-table ${fixed ? 'fixed' : ''}" ${fixed ? `style="width:${COLS.reduce((a, [k]) => a + w(k), 0)}px"` : ''}>
      <colgroup>${COLS.map(([k]) => `<col data-col="${k}" ${fixed ? `style="width:${w(k)}px"` : ''}>`).join('')}</colgroup>
      <thead><tr>${COLS.map(([k, t, , a]) => `<th class="${a || ''} ${st.sort === k ? 'sorted' : ''}" data-sort="${k}" ${HINTS[k] ? `title="${esc(HINTS[k])}"` : ''} aria-sort="${st.sort === k ? (st.desc ? 'descending' : 'ascending') : 'none'}">${esc(t)}${st.sort === k ? (st.desc ? ' ↓' : ' ↑') : ''}<span class="ab-rz" data-rz="${k}" title="Потяните, чтобы изменить ширину. Двойной клик — по содержимому" aria-hidden="true"></span></th>`).join('')}</tr>
        <tr class="ab-cf">${COLS.map(([k]) => `<th>${filterCell(k)}</th>`).join('')}</tr></thead>
      <tbody>${rows.length ? rows.slice(0, st.shown).map(r => `<tr>${COLS.map(([, , v, a]) => `<td class="${a || ''}">${v(r)}</td>`).join('')}</tr>`).join('')
        : `<tr><td colspan="${COLS.length}" class="l muted ab-empty">Нет артикулов по этим фильтрам.</td></tr>`}</tbody></table>
      ${rows.length > st.shown ? `<button type="button" class="db-ghost ab-more" data-more>Показать ещё ${num(Math.min(PAGE, rows.length - st.shown))}</button>` : ''}`;
    box.querySelectorAll('[data-sort]').forEach(th => th.onclick = () => {
      if (resizedAt && Date.now() - resizedAt < 300) return;   // только что тянули ширину — не сортируем
      st.desc = st.sort === th.dataset.sort ? !st.desc : !['brand', 'category', 'article', 'name', 'status'].includes(th.dataset.sort);
      st.sort = th.dataset.sort; save(); drawTable();
    });
    let timer = null;
    box.querySelectorAll('[data-cf]').forEach(el => {
      const apply = () => { st.cf = {...(st.cf || {}), [el.dataset.cf]: el.value}; st.shown = PAGE; drawTable(); };
      if (el.tagName === 'SELECT') el.onchange = apply;
      else el.oninput = () => { clearTimeout(timer); timer = setTimeout(apply, 250); };
    });
    const reset = root.querySelector('[data-cf-reset]');
    if (reset) reset.onclick = () => { st.cf = {}; st.shown = PAGE; drawTable(); };
    if (focus) {                               // таблица перерисована — возвращаем курсор в поле, где печатали
      const el = box.querySelector(`[data-cf="${focus}"]`);
      if (el) { el.focus(); if (caret != null && el.setSelectionRange) el.setSelectionRange(caret, caret); }
    }
    const more = box.querySelector('[data-more]');
    if (more) more.onclick = () => { st.shown += PAGE; drawTable(); };
    box.querySelectorAll('[data-rz]').forEach(h => { h.onmousedown = startResize; h.ondblclick = fitColumn; h.onclick = e => e.stopPropagation(); });
    const wr = root.querySelector('[data-widths-reset]');
    if (wr) wr.hidden = !Object.keys(widths).length;
  }

  let resizedAt = 0;
  function measure(table) {                    // первая правка — запоминаем текущую ширину всех столбцов
    if (COLS.every(([k]) => widths[k])) return;
    table.querySelectorAll('thead tr:first-child th').forEach((th, i) => { widths[COLS[i][0]] = widths[COLS[i][0]] || Math.round(th.getBoundingClientRect().width); });
  }
  function applyWidths(table) {
    table.classList.add('fixed');
    table.querySelectorAll('col').forEach(c => { c.style.width = widths[c.dataset.col] + 'px'; });
    table.style.width = COLS.reduce((a, [k]) => a + widths[k], 0) + 'px';
  }
  function startResize(e) {
    e.preventDefault(); e.stopPropagation();
    const handle = e.currentTarget, key = handle.dataset.rz, table = handle.closest('table');
    measure(table); applyWidths(table);
    const x0 = e.clientX, w0 = widths[key];
    handle.classList.add('on'); document.body.classList.add('ab-resizing');
    const move = ev => { widths[key] = Math.max(MIN_W, Math.min(MAX_W, Math.round(w0 + ev.clientX - x0))); applyWidths(table); };
    const up = () => { document.removeEventListener('mousemove', move); document.removeEventListener('mouseup', up);
      handle.classList.remove('on'); document.body.classList.remove('ab-resizing'); resizedAt = Date.now(); saveWidths();
      const wr = root.querySelector('[data-widths-reset]'); if (wr) wr.hidden = false; };
    document.addEventListener('mousemove', move); document.addEventListener('mouseup', up);
  }
  function fitColumn(e) {                     // двойной клик по краю — ширина по самому длинному значению
    e.preventDefault(); e.stopPropagation();
    const key = e.currentTarget.dataset.rz, table = e.currentTarget.closest('table'), i = COLS.findIndex(([k]) => k === key);
    measure(table); applyWidths(table);
    let w = MIN_W;
    table.querySelectorAll(`tbody tr td:nth-child(${i + 1})`).forEach(td => { const el = td.firstElementChild || td;
      w = Math.max(w, (el === td ? td.scrollWidth : el.scrollWidth + 18)); });
    const th = table.querySelector(`thead tr:first-child th:nth-child(${i + 1})`);
    w = Math.max(w, Math.min(th.scrollWidth, 160));
    widths[key] = Math.min(MAX_W, Math.ceil(w + 4)); applyWidths(table); resizedAt = Date.now(); saveWidths();
  }

  async function download(e) {
    const b = e.currentTarget, [from, to] = period(), label = b.textContent;
    b.disabled = true; b.textContent = 'Готовлю файл…';
    try {
      const r = await fetch('/api/abc/export', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({from, to, mp: st.mp, keys: st.view, format: 'xlsx'})});
      if (!r.ok) { let m = 'Не удалось скачать'; try { m = (await r.json()).detail || m; } catch (err) { /* не JSON */ } throw new Error(m); }
      const cd = r.headers.get('Content-Disposition') || '';
      const name = (/filename\*=UTF-8''([^;]+)/i.exec(cd) || /filename="?([^";]+)/i.exec(cd) || [])[1];
      const url = URL.createObjectURL(await r.blob()), a = document.createElement('a');
      a.href = url; a.download = name ? decodeURIComponent(name) : `abc_${from}_${to}_filtered.xlsx`;
      document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (err) { alert(err.message); }
    b.disabled = false; b.textContent = label;
  }

  const style = document.createElement('style');
  style.textContent = `
.ab-export{display:inline-flex;align-items:center;text-decoration:none}.ab-dl{background:#2f4b8a;color:#fff}
.ab-tools{flex-wrap:nowrap;justify-content:center}.ab-tools>*{flex:0 0 auto;white-space:nowrap}.ab-tools .rv-search{flex:1 1 220px;max-width:320px;min-width:160px}.ab-tools .rv-search input{width:100%;height:40px}.ab-tools select{flex:0 1 190px;min-width:110px;width:190px}
@media(max-width:1200px){.ab-tools{flex-wrap:wrap}}.ab-dl:hover{background:#24407a}.ab-dl:disabled{opacity:.5}
.ab-filters{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin:14px 0}.ab-filters select,.ab-filters input[type=date]{height:40px;border:1px solid #e3e5ea;border-radius:10px;padding:0 12px;font:inherit;font-size:14px;background:#fff;max-width:100%}
.ab-date{display:flex;align-items:center;gap:6px;font-size:13px;color:#6b7280}.ab-range{font-size:13px}
.ab-kpis{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin-bottom:12px}
.ab-kpi{background:#fff;border:1px solid #e7e9ee;border-radius:12px;padding:12px 14px;display:flex;flex-direction:column;gap:4px;min-width:0}
.ab-kpi span{font-size:12px;font-weight:600;color:#374151}.ab-kpi b{font-size:22px;font-variant-numeric:tabular-nums}.ab-kpi small{font-size:11px;color:#6b7280}
.ab-kpi.warn{background:#fffbeb;border-color:#fde68a}.ab-kpi.bad{background:#fef2f2;border-color:#fecaca}
.ab-mini td.ab-dead{color:#9f1239}
.ab-summary{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(0,1fr) minmax(0,1fr);gap:12px;align-items:start}
.ab-mini{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e7e9ee;border-radius:12px;overflow:hidden;font-size:13px;font-variant-numeric:tabular-nums}
.ab-mini th{background:#2f4b8a;color:#fff;font-weight:600;font-size:12px;padding:7px 8px;text-align:right}.ab-mini th:first-child,.ab-mini td:first-child{text-align:left}
.ab-mini td{padding:7px 8px;border-bottom:1px solid #eef0f5;text-align:right;white-space:nowrap}.ab-mini tbody tr{cursor:pointer}.ab-mini tbody tr:hover td{background:#f5f7fb}
.ab-mini tr.sel td{background:#ede9fe}.ab-mini tfoot td{font-weight:700;background:#f3f5f9}
.ab-recs{background:#fff;border:1px solid #e7e9ee;border-radius:12px;padding:10px 14px;font-size:13px}.ab-recs p{margin:6px 0}
.ab-g{display:inline-grid;place-items:center;width:20px;height:20px;border-radius:6px;font-size:11px;font-weight:700}.ab-A{background:#dcfce7;color:#166534}.ab-B{background:#fef3c7;color:#92400e}.ab-C{background:#fee2e2;color:#991b1b}
.ab-st{display:inline-block;border-radius:999px;padding:2px 9px;font-size:12px;font-weight:600;white-space:nowrap}
.ab-st.def{background:#fee2e2;color:#991b1b}.ab-st.ok{background:#dcfce7;color:#166534}.ab-st.exc{background:#fef3c7;color:#92400e}.ab-st.dead{background:#7f1d1d;color:#fff}.ab-st.none{background:#eef0f4;color:#6b7280}
.ab-wrap{overflow-x:auto;border:1px solid #e3e6ee;border-radius:12px;background:#fff}
.ab-table{border-collapse:collapse;font-size:13px;white-space:nowrap;font-variant-numeric:tabular-nums;min-width:100%}
.ab-rz{position:absolute;top:0;right:0;width:7px;height:100%;cursor:col-resize;z-index:3}
.ab-rz:hover,.ab-rz.on{background:#a5b4fc80}.ab-resizing,.ab-resizing *{cursor:col-resize!important;user-select:none!important}
.ab-table.fixed{table-layout:fixed}.ab-table.fixed th,.ab-table.fixed td{min-width:0;overflow:hidden;text-overflow:ellipsis}
.ab-table.fixed .ab-name{max-width:100%}
.ab-table th{background:#2f4b8a;color:#fff;font-weight:600;font-size:12px;padding:8px;text-align:right;cursor:pointer;white-space:normal;min-width:70px;line-height:1.25;position:sticky;top:0}
.ab-table th.sorted{background:#24407a}.ab-table .ab-cf th{position:static;background:#e9edf6;padding:4px;cursor:default}
.ab-cf input,.ab-cf select{width:100%;min-width:60px;height:28px;border:1px solid #d5dbe8;border-radius:6px;padding:0 6px;font:inherit;font-size:12px;background:#fff;color:#1f2328}
.ab-cf input.bad{border-color:#dc2626;background:#fef2f2}.ab-cf input:focus,.ab-cf select:focus{outline:2px solid #c4b5fd;border-color:#7c3aed}.ab-table td{padding:6px 8px;border-bottom:1px solid #eef0f5;text-align:right}.ab-table .l{text-align:left}.ab-table .c{text-align:center}
.ab-table tbody tr:hover td{background:#f8f9fc}.ab-name{display:inline-block;max-width:320px;overflow:hidden;text-overflow:ellipsis;vertical-align:bottom}
.ab-table .neg{color:#b42323}.ab-more{margin:10px}.ab-empty{padding:14px;margin:0}
.wb-member [data-nav=abc]{display:none!important}
@media(max-width:1300px){.ab-kpis{grid-template-columns:repeat(3,minmax(0,1fr))}.ab-summary{grid-template-columns:1fr}}
@media(max-width:700px){.ab-kpis{grid-template-columns:1fr 1fr}}`;
  document.head.appendChild(style);

  return {render};
})();
