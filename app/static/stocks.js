/* WB OPS: «Остатки FBS» — быстрое пополнение остатков по размерам, фильтры и сортировка, оповещения о заканчивающихся размерах. */
window.WBOPSStocks = (() => {
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const num = (v, d = 0) => v == null || v === '' || isNaN(v) ? '—' : Number(v).toLocaleString('ru-RU', {minimumFractionDigits: d, maximumFractionDigits: d});
  const dt = s => s ? new Date(s).toLocaleString('ru-RU', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'}) : '—';
  const STATUS = {zero: ['Закончился', 'bad'], low: ['Мало', 'warn'], ok: ['В наличии', 'ok']};
  const SIZES = ['XXS', 'XS', 'S', 'M', 'L', 'XL', 'XXL', '2XL', '3XL', '4XL', '5XL'];
  const sizeKey = s => { const m = String(s || '').match(/^(\d+(?:[.,]\d+)?)/); const u = String(s || '').toUpperCase();
    return m ? [0, parseFloat(m[1].replace(',', '.'))] : SIZES.includes(u) ? [1, SIZES.indexOf(u)] : [2, 0]; };
  const st = {data: null, tab: 'stocks', f: {cabinet: '', warehouse: '', q: '', brand: '', category: '', status: 'all', sort: 'risk', min: '', max: ''},
    sel: new Set(), pending: new Map(), collapsed: new Set(), showSettings: false, hist: null, histDays: 7, busy: false, lastRefresh: 0, tick: null};
  let root = null;
  try { Object.assign(st.f, JSON.parse(localStorage.getItem('wbops.stocks.f') || '{}')); } catch (e) { /* без хранилища */ }
  const remember = () => { try { localStorage.setItem('wbops.stocks.f', JSON.stringify(st.f)); } catch (e) { /* ignore */ } };
  const api = async (url, opt = {}) => {
    const r = await fetch(url, {method: opt.method || 'GET', headers: opt.body ? {'Content-Type': 'application/json'} : {}, body: opt.body ? JSON.stringify(opt.body) : undefined});
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || 'Ошибка запроса');
    return d;
  };
  const amountOf = r => st.pending.has(r.id) ? st.pending.get(r.id) : r.amount;
  const statusOf = (r, a = amountOf(r)) => a <= 0 ? 'zero' : a <= r.limit ? 'low' : 'ok';

  // ---------------------------------------------------------------- каркас
  async function render(section) {
    root = section;
    root.className = 'panel rv-page sk-page';
    root.innerHTML = `<div class="db-head"><h2>Остатки FBS</h2><span class="muted db-fresh">Остаток на ваших складах (FBS) по каждому размеру</span></div>
      <div class="rnp-tabs sk-tabs" role="tablist">${[['stocks', 'Остатки'], ['history', 'История изменений']].map(([k, t]) => `<button type="button" role="tab" data-tab="${k}">${t}</button>`).join('')}</div>
      <div data-body><p class="muted">Загружаю остатки с WB…</p></div>`;
    root.querySelectorAll('[data-tab]').forEach(b => b.onclick = () => { st.tab = b.dataset.tab; draw(); });
    window.addEventListener('beforeunload', e => { if (st.pending.size && root?.isConnected) { e.preventDefault(); e.returnValue = ''; } });
    await load();
    clearInterval(st.tick);
    st.tick = setInterval(() => { if (!root?.isConnected) return clearInterval(st.tick); const c = root.querySelector('[data-clock]'); if (c) drawClock(c); }, 15000);
  }
  async function load() {
    try { st.data = await api('/api/stocks'); }
    catch (e) { root.querySelector('[data-body]').innerHTML = `<p class="rv-error">${esc(e.message)}</p>`; return; }
    const ids = new Set(st.data.rows.map(r => r.id));
    [...st.pending.keys()].forEach(k => { if (!ids.has(k)) st.pending.delete(k); });
    draw();
    clearTimeout(st.ordersPoll);                     // заказы ещё грузятся — обновить таблицу, когда загрузятся
    if (st.data.orders_sync?.status === 'running') st.ordersPoll = setTimeout(() => { if (root?.isConnected) load(); }, 20000);
  }
  function draw() {
    root.querySelectorAll('[data-tab]').forEach(b => { b.classList.toggle('active', b.dataset.tab === st.tab); b.setAttribute('aria-selected', String(b.dataset.tab === st.tab)); });
    if (!st.data) return;
    if (!st.data.cabinets.length) {
      root.querySelector('[data-body]').innerHTML = '<div class="rv-empty"><b>Нет кабинета Wildberries с ключом API</b><p class="muted">Добавьте ключ в «Кабинеты маркетплейсов». Нужны категории «Маркетплейс» (чтение и запись) и «Контент».</p></div>';
      return;
    }
    st.tab === 'history' ? drawHistory() : drawStocks();
  }
  function drawClock(el) {
    const ages = Object.values(st.data.state || {}).map(s => s.at ? Date.now() / 1000 - s.at : 1e9);
    const min = (ages.length ? Math.max(...ages) : 1e9) / 60;
    el.className = 'rp-clock ' + (min < 10 ? 'g' : min < 30 ? 'y' : 'r');
    el.title = min > 1e6 ? 'остатки ещё не загружены' : `остатки обновлены ${Math.round(min)} мин назад`;
    el.setAttribute('aria-label', el.title);
  }

  // ---------------------------------------------------------------- данные для таблицы
  function visible() {
    const f = st.f, q = f.q.trim().toLowerCase();
    const whs = whList();
    // Пустой выбор означает все склады. Раньше он автоматически подменялся
    // первым складом, поэтому остальные склады нельзя было показать вместе.
    const wh = whs.some(w => String(w.id) === String(f.warehouse)) ? String(f.warehouse) : '';
    const rows = st.data.rows.filter(r => (!f.cabinet || r.cabinet_id === f.cabinet) && (!wh || String(r.warehouse_id) === wh)
      && (!f.brand || r.brand === f.brand) && (!f.category || r.category === f.category)
      && (!q || `${r.vendor} ${r.nm_id} ${r.title} ${r.barcode} ${r.size} ${r.ru_size}`.toLowerCase().includes(q))
      && (f.min === '' || amountOf(r) >= Number(f.min)) && (f.max === '' || amountOf(r) <= Number(f.max)));
    const counts = {all: 0, low: 0, zero: 0, ok: 0, muted: 0, changed: 0, archive: 0};
    const archNm = new Set();
    rows.forEach(r => {
      if (r.archived) { archNm.add(`${r.cabinet_id}:${r.nm_id}`); return; }
      counts.all++; if (!r.watched) counts.muted++; counts[statusOf(r)]++; if (st.pending.has(r.id)) counts.changed++;
    });
    counts.archive = archNm.size;
    const shown = rows.filter(r => f.status === 'archive' ? r.archived : !r.archived && (f.status === 'all'
      || (f.status === 'muted' ? !r.watched : f.status === 'changed' ? st.pending.has(r.id) : statusOf(r) === f.status)));
    const groups = new Map();
    shown.forEach(r => { const k = `${r.cabinet_id}:${r.warehouse_id}:${r.nm_id}`; if (!groups.has(k)) groups.set(k, {key: k, rows: [], r}); groups.get(k).rows.push(r); });
    const list = [...groups.values()];
    list.forEach(g => {
      g.rows.sort((a, b) => { const x = sizeKey(a.size), y = sizeKey(b.size); return x[0] - y[0] || x[1] - y[1] || String(a.size).localeCompare(String(b.size)); });
      g.total = g.rows.reduce((s, r) => s + amountOf(r), 0);
      g.minLeft = Math.min(...g.rows.map(r => r.watched ? amountOf(r) - r.limit : 1e9));
      g.worst = g.rows.some(r => r.watched && statusOf(r) === 'zero') ? 'zero' : g.rows.some(r => r.watched && statusOf(r) === 'low') ? 'low' : 'ok';
    });
    const by = {risk: (a, b) => a.minLeft - b.minLeft || b.r.orders_per_day - a.r.orders_per_day, article: (a, b) => a.r.vendor.localeCompare(b.r.vendor, 'ru', {numeric: true}),
      stock: (a, b) => a.total - b.total, stock_desc: (a, b) => b.total - a.total, sales: (a, b) => (b.r.orders_per_day || 0) - (a.r.orders_per_day || 0)};
    list.sort(by[f.sort] || by.risk);
    return {groups: list, counts, rows: shown, wh};
  }
  function whList() {
    const st_ = st.data.state || {}, f = st.f;
    return Object.entries(st_).filter(([cid]) => !f.cabinet || cid === f.cabinet).flatMap(([cid, s]) => (s.warehouses || []).map(w => ({...w, cabinet_id: cid})));
  }

  // ---------------------------------------------------------------- вкладка «Остатки»
  function drawStocks() {
    const d = st.data, f = st.f, {groups, counts, rows, wh} = visible();
    const opt = (v, t, cur) => `<option value="${esc(v)}" ${String(v) === String(cur) ? 'selected' : ''}>${esc(t)}</option>`;
    const brands = [...new Set(d.rows.map(r => r.brand).filter(Boolean))].sort(), cats = [...new Set(d.rows.map(r => r.category).filter(Boolean))].sort();
    const whs = whList(), cfg = d.settings, n = d.notify;
    const errs = Object.entries(d.state || {}).filter(([, s]) => s.error).map(([cid, s]) => `${(d.cabinets.find(c => c.id === cid) || {}).name}: ${s.error}`);
    const delta = [...st.pending].reduce((s, [id, a]) => s + a - (d.rows.find(r => r.id === id)?.amount || 0), 0);
    const chip = (k, t) => `<button type="button" class="sk-chip ${f.status === k ? 'on' : ''} ${k}" data-status="${k}">${t} <b>${counts[k]}</b></button>`;
    root.querySelector('[data-body]').innerHTML = `
      ${notifyBanner(n, cfg)}
      <div class="db-filters sk-filters">
        ${d.cabinets.length > 1 ? `<select data-f="cabinet" aria-label="Магазин">${opt('', 'Все магазины', f.cabinet)}${d.cabinets.map(c => opt(c.id, c.name, f.cabinet)).join('')}</select>` : ''}
        ${whs.length > 1 ? `<select data-f="warehouse" aria-label="Склад">${opt('', 'Все склады', wh)}${whs.map(w => opt(w.id, w.name, wh)).join('')}</select>` : ''}
        <select data-f="brand" aria-label="Бренд">${opt('', 'Все бренды', f.brand)}${brands.map(b => opt(b, b, f.brand)).join('')}</select>
        <select data-f="category" aria-label="Категория">${opt('', 'Все категории', f.category)}${cats.map(b => opt(b, b, f.category)).join('')}</select>
        <input type="search" data-f="q" placeholder="Артикул, баркод, размер" value="${esc(f.q)}" aria-label="Поиск">
        <span class="sk-range">Остаток <input data-f="min" type="number" min="0" placeholder="от" value="${esc(f.min)}" aria-label="Остаток от"> – <input data-f="max" type="number" min="0" placeholder="до" value="${esc(f.max)}" aria-label="Остаток до"></span>
        <select data-f="sort" aria-label="Сортировка">${[['risk', 'Сначала заканчивающиеся'], ['sales', 'Сначала продаваемые'], ['stock', 'Остаток ↑'], ['stock_desc', 'Остаток ↓'], ['article', 'По артикулу']].map(([k, t]) => opt(k, t, f.sort)).join('')}</select>
        <span class="rp-tools"><span data-clock></span><button type="button" class="db-ghost" data-refresh>↻ Обновить</button>
          <button type="button" class="db-ghost" data-toggle-set aria-expanded="${st.showSettings}">🔔 Оповещения</button></span>
      </div>
      ${st.showSettings ? settingsHtml(cfg, n) : ''}
      ${errs.map(e => `<div class="db-note">${esc(e)}</div>`).join('')}
      <div class="sk-chips">${chip('all', 'Все')}${chip('low', `Мало (≤ ${cfg.threshold})`)}${chip('zero', 'Закончились')}${chip('ok', 'В наличии')}${chip('muted', 'Не следим')}${counts.changed ? chip('changed', 'Изменены') : ''}${counts.archive || f.status === 'archive' ? chip('archive', '🗄 Архив') : ''}
        <span class="muted sk-count">${groups.length} артикулов · ${rows.length} размеров</span>
        ${groups.length ? `<button type="button" class="rv-link" data-collapse-all>${groups.every(g => st.collapsed.has(g.key)) ? 'Развернуть все' : 'Свернуть все'}</button>` : ''}</div>
      <div class="sk-list">${groups.slice(0, 300).map(groupHtml).join('') || '<div class="rv-empty"><b>Ничего не найдено</b><p class="muted">Измените фильтры.</p></div>'}</div>
      ${groups.length > 300 ? `<p class="muted">Показаны первые 300 артикулов из ${groups.length} — уточните фильтр.</p>` : ''}
      ${st.sel.size ? `<div class="rp-bar sk-bar"><b>Выбрано размеров: ${st.sel.size}</b>
        <form class="sk-bulk" data-bulk><input name="n" type="number" min="0" step="1" required placeholder="шт." aria-label="Количество">
          <button class="btn secondary" type="submit" data-mode="add">Добавить</button><button class="btn secondary" type="submit" data-mode="set">Установить</button>
          <button class="btn secondary" type="submit" data-mode="thr" title="Порог оповещения для выбранных">Порог</button></form>
        <button type="button" class="rv-link" data-mute="1">Не следить</button><button type="button" class="rv-link" data-mute="0">Следить</button>
        <button type="button" class="rv-link" data-sel-all>Выбрать все по фильтрам (${rows.length})</button><button type="button" class="rv-link" data-sel-clear>Снять выбор</button></div>` : ''}
      ${st.pending.size ? `<div class="rp-bar sk-save"><b>Изменений: ${st.pending.size}</b><span>${delta >= 0 ? '+' : ''}${num(delta)} шт.</span>
        <button type="button" class="rv-link" data-show-changed>Показать</button><button type="button" class="rv-link" data-discard>Отменить</button>
        <button type="button" class="btn" data-save ${st.busy ? 'disabled' : ''}>${st.busy ? 'Отправляю…' : 'Отправить на WB'}</button></div>` : ''}`;
    bindStocks(groups, rows);
    drawClock(root.querySelector('[data-clock]'));
  }

  function notifyBanner(n, cfg) {
    if (!cfg.monitor) return '<div class="db-note">Оповещения об остатках выключены. Включите их в «🔔 Оповещения».</div>';
    if (!n.telegram && !n.email) return '<div class="db-note">Чтобы получать оповещения, подключите Telegram в «Личный кабинет → Настройки уведомлений». <a href="/notifications">Открыть</a></div>';
    if (!n.event) return '<div class="db-note">В «Настройках уведомлений» выключено событие «Оповещать о заканчивающихся остатках». <a href="/notifications">Включить</a></div>';
    return '';
  }
  function settingsHtml(cfg, n) {
    return `<form class="tr-box sk-set" data-set>
      <label class="rp-check"><input type="checkbox" name="monitor" ${cfg.monitor ? 'checked' : ''}> Следить за остатками и присылать оповещения</label>
      <label>Порог по умолчанию, шт.<input name="threshold" type="number" min="0" max="10000" value="${cfg.threshold}"></label>
      <label>Проверять каждые, мин<input name="interval_min" type="number" min="5" max="1440" value="${cfg.interval_min}"></label>
      <label class="rp-check"><input type="checkbox" name="only_seen" ${cfg.only_seen ? 'checked' : ''}> Только размеры, по которым был остаток (не напоминать о тех, что не продаёте с FBS)</label>
      <p class="muted">Оповещение приходит один раз, когда остаток размера дошёл до порога, и ещё раз — когда закончился. Для отдельного размера порог можно задать в его строке.
        Канал: ${n.telegram ? 'Telegram ✓' : 'Telegram не подключён'}${n.email ? ', почта ✓' : ''}.</p>
      <div class="tr-actions"><button class="btn" type="submit">Сохранить</button><button class="btn secondary" type="button" data-check>Проверить и прислать сейчас</button>
        <span class="muted" data-set-state role="status"></span></div></form>`;
  }

  function groupHtml(g) {
    const r = g.r, collapsed = st.collapsed.has(g.key), allSel = g.rows.every(x => st.sel.has(x.id));
    const days = r.orders_per_day ? g.rows.reduce((s, x) => s + amountOf(x), 0) / r.orders_per_day : null;
    const akey = `${r.cabinet_id}:${r.nm_id}`;
    return `<article class="sk-group ${r.archived ? 'arch' : g.worst}">
      <header><input type="checkbox" data-gsel="${esc(g.key)}" ${allSel ? 'checked' : ''} aria-label="Выбрать все размеры ${esc(r.vendor)}">
        <button type="button" class="sk-fold" data-fold="${esc(g.key)}" aria-expanded="${!collapsed}" aria-label="${collapsed ? 'Развернуть' : 'Свернуть'}">${collapsed ? '▸' : '▾'}</button>
        ${r.photo ? `<img src="${esc(r.photo)}" alt="" loading="lazy">` : '<span class="sk-noimg"></span>'}
        <div class="sk-g-main"><b>${esc(r.vendor)}</b> <span class="muted">${esc(r.title)}</span>
          <small>${esc([r.brand, r.category, r.nm_id, st.data.cabinets.length > 1 ? r.cabinet : ''].filter(Boolean).join(' · '))}</small></div>
        <div class="sk-g-stats"><span title="Сумма остатков FBS по показанным размерам"><b>${num(g.total)}</b> шт. FBS</span>
          ${r.wb_stock != null ? `<span title="Остаток на складах WB (FBO) по артикулу"><b>${num(r.wb_stock)}</b> на складах WB</span>` : ''}
          ${r.orders_per_day ? `<span title="Среднее заказов в день по артикулу за последние 14 полных дней (без сегодняшнего), все склады, без отмен"><b>${num(r.orders_per_day, 1)}</b> зак./день</span>` : ''}
          ${days != null ? `<span title="На сколько дней хватит FBS-остатка при текущих заказах" class="${days < 7 ? 'neg' : ''}">≈ <b>${num(days)}</b> дн.</span>` : ''}
          ${r.archived ? '<span class="tr-chip mute">В архиве</span>' : `<span class="tr-chip ${STATUS[g.worst][1]}">${STATUS[g.worst][0]}</span>`}
          ${r.archived ? '' : `<button type="button" class="sk-arch sk-zero" data-zero="${esc(g.key)}" ${g.rows.every(x => amountOf(x) === 0) ? 'disabled' : ''}
            title="Поставить 0 по всем размерам артикула на этом складе и сразу отправить на WB">⦸ Обнулить</button>`}
          <button type="button" class="sk-arch" data-arch="${esc(akey)}" data-to="${r.archived ? 0 : 1}" title="${r.archived ? 'Вернуть в работу: снова показывать и присылать оповещения' : 'Больше не пополняем: скрыть из списка и не присылать оповещения'}">${r.archived ? '↩ Вернуть' : '🗄 В архив'}</button></div></header>
      ${collapsed ? '' : `<table class="sk-table"><thead><tr><th class="c"></th><th>Размер</th><th>Баркод</th><th class="r">Остаток</th><th class="r" title="Среднее заказов в день по размеру за последние 14 полных дней (без сегодняшнего), все склады, без отмен; ниже — на сколько дней хватит FBS-остатка">Заказов в день</th><th>Пополнить</th><th>Порог</th><th>Следить</th><th>Обновлён</th></tr></thead><tbody>
        ${g.rows.map(sizeRow).join('')}</tbody></table>`}</article>`;
  }

  function sizeRow(r) {
    const a = amountOf(r), changed = st.pending.has(r.id), s = statusOf(r);
    return `<tr class="${changed ? 'sk-changed' : ''}">
      <td class="c"><input type="checkbox" data-sel="${esc(r.id)}" ${st.sel.has(r.id) ? 'checked' : ''} aria-label="Выбрать размер ${esc(r.size)}"></td>
      <td><b>${esc(r.size || 'без размера')}</b>${r.ru_size && r.ru_size !== r.size ? `<small>рос. ${esc(r.ru_size)}</small>` : ''}</td>
      <td class="sk-bc">${esc(r.barcode)}</td>
      <td class="r"><span class="sk-amount ${s}" title="${STATUS[s][0]}">${changed ? `<s>${num(r.amount)}</s> → ` : ''}<b>${num(a)}</b></span></td>
      <td class="r sk-vel">${velCell(r, a)}</td>
      <td><div class="sk-quick">
        <button type="button" data-add="5" data-id="${esc(r.id)}" aria-label="Плюс 5">+5</button>
        <button type="button" data-add="10" data-id="${esc(r.id)}" aria-label="Плюс 10">+10</button>
        <button type="button" data-add="50" data-id="${esc(r.id)}" aria-label="Плюс 50">+50</button>
        <input data-qty="${esc(r.id)}" inputmode="numeric" placeholder="+N / =N" title="Число — добавить, =N — установить точно. Enter — применить" aria-label="Добавить или установить количество">
        <button type="button" class="sk-zero" data-zero-size="${esc(r.id)}" ${a === 0 ? 'disabled' : ''} aria-label="Обнулить размер ${esc(r.size || 'без размера')}" title="Поставить 0 для этого размера и сразу отправить на WB">⦸ Обнулить</button>
        ${changed ? `<button type="button" class="rv-link" data-undo="${esc(r.id)}" title="Вернуть как было">↺</button>` : ''}</div></td>
      <td><input class="sk-thr" type="number" min="0" data-thr="${esc(r.id)}" value="${r.threshold ?? ''}" placeholder="${st.data.settings.threshold}" aria-label="Порог оповещения"></td>
      <td><button type="button" class="st-switch ${r.muted ? '' : 'on'}" role="switch" aria-checked="${!r.muted}" data-watch="${esc(r.id)}" aria-label="Следить за размером"><i></i></button></td>
      <td class="muted sk-upd">${dt(r.updated_at)}</td></tr>`;
  }

  function velCell(r, a) {
    const v = r.size_orders_per_day;
    if (v == null) {
      const s = st.data.orders_sync || {};
      return s.status === 'running' ? '<span class="muted" title="Загружаю заказы из WB — таблица обновится сама">загрузка…</span>'
        : '<span class="muted" title="Нет заказов с размерами: нужен токен WB с категорией «Статистика» (РНП → «Обновить из WB»)">—</span>';
    }
    if (!v) return '<span class="muted">0</span>';
    const days = a / v;
    return `<b>${num(v, v < 1 ? 2 : 1)}</b><small class="${days < 7 ? 'neg' : days < 14 ? 'warn' : ''}">≈ ${num(days)} дн.</small>`;
  }

  function setPending(id, value) {
    const r = st.data.rows.find(x => x.id === id);
    if (!r) return;
    const v = Math.max(0, Math.min(100000, Math.round(value)));
    if (v === r.amount) st.pending.delete(id); else st.pending.set(id, v);
  }
  function keepFocus(sel, fn) {
    const el = document.activeElement, key = el?.matches?.(sel) ? [...el.attributes].find(a => a.name.startsWith('data-'))?.name : null;
    const val = key ? el.getAttribute(key) : null, pos = el?.selectionStart;
    fn();
    if (key) { const n = root.querySelector(`[${key}="${CSS.escape(val)}"]`); if (n) { n.focus(); try { n.setSelectionRange(pos, pos); } catch (e) { /* number */ } } }
  }

  function bindStocks(groups, rows) {
    const body = root.querySelector('[data-body]');
    body.querySelectorAll('[data-f]').forEach(el => {
      const h = () => { st.f[el.dataset.f] = el.value; remember(); keepFocus('[data-f]', drawStocks); };
      (el.type === 'search' || el.type === 'number') ? (el.oninput = h) : (el.onchange = h);
    });
    body.querySelectorAll('[data-status]').forEach(b => b.onclick = () => { st.f.status = b.dataset.status; remember(); drawStocks(); });
    body.querySelector('[data-refresh]').onclick = async e => {
      e.currentTarget.disabled = true;
      try { await api('/api/stocks/refresh', {method: 'POST', body: {reload_cards: Date.now() - st.lastRefresh < 120000}}); st.lastRefresh = Date.now(); await load(); }
      catch (err) { alert(err.message); e.currentTarget.disabled = false; }
    };
    body.querySelector('[data-toggle-set]').onclick = () => { st.showSettings = !st.showSettings; drawStocks(); };
    const sf = body.querySelector('[data-set]');
    if (sf) {
      sf.onsubmit = async e => {
        e.preventDefault();
        const b = {monitor: sf.elements.monitor.checked, only_seen: sf.elements.only_seen.checked, threshold: Number(sf.elements.threshold.value), interval_min: Number(sf.elements.interval_min.value)};
        try { st.data.settings = await api('/api/stocks/settings', {method: 'PUT', body: b}); await load(); } catch (err) { sf.querySelector('[data-set-state]').textContent = err.message; }
      };
      sf.querySelector('[data-check]').onclick = async e => {
        const s = sf.querySelector('[data-set-state]'); e.currentTarget.disabled = true; s.textContent = 'Проверяю остатки…';
        try { const r = await api('/api/stocks/check', {method: 'POST'}); s.textContent = r.message + (r.sent ? ` · мало: ${r.low}, закончились: ${r.zero}` : ''); await load(); }
        catch (err) { s.textContent = err.message; }
        e.currentTarget.disabled = false;
      };
    }
    body.querySelector('[data-collapse-all]')?.addEventListener('click', () => {
      const all = groups.every(g => st.collapsed.has(g.key));
      groups.forEach(g => all ? st.collapsed.delete(g.key) : st.collapsed.add(g.key)); drawStocks();
    });
    body.querySelectorAll('[data-fold]').forEach(b => b.onclick = () => { st.collapsed.has(b.dataset.fold) ? st.collapsed.delete(b.dataset.fold) : st.collapsed.add(b.dataset.fold); drawStocks(); });
    body.querySelectorAll('[data-gsel]').forEach(cb => cb.onchange = () => {
      const g = groups.find(x => x.key === cb.dataset.gsel); g.rows.forEach(r => cb.checked ? st.sel.add(r.id) : st.sel.delete(r.id)); drawStocks();
    });
    body.querySelectorAll('[data-sel]').forEach(cb => cb.onchange = () => { cb.checked ? st.sel.add(cb.dataset.sel) : st.sel.delete(cb.dataset.sel); drawStocks(); });
    body.querySelectorAll('[data-add]').forEach(b => b.onclick = () => {
      const r = st.data.rows.find(x => x.id === b.dataset.id); setPending(r.id, amountOf(r) + Number(b.dataset.add)); drawStocks();
    });
    body.querySelectorAll('[data-zero-size]').forEach(b => b.onclick = async () => {
      const r = st.data.rows.find(x => x.id === b.dataset.zeroSize);
      if (!r || amountOf(r) === 0) return;
      if (!confirm(`Обнулить размер «${r.size || 'без размера'}» для ${r.vendor}?\n\nИзменение сразу уйдёт на WB.`)) return;
      b.disabled = true; b.textContent = '…';
      try {
        const res = await api('/api/stocks', {method: 'PUT', body: {changes: [{id: r.id, amount: 0}]}});
        if (res.errors?.[r.id]) throw new Error(res.errors[r.id]);
        st.pending.delete(r.id);
        await load();
      } catch (err) { alert(err.message); b.disabled = false; b.textContent = '⦸ Обнулить'; }
    });
    body.querySelectorAll('[data-qty]').forEach(inp => inp.onkeydown = e => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      const raw = inp.value.replace(/\s+/g, ''), r = st.data.rows.find(x => x.id === inp.dataset.qty);
      const m = raw.match(/^([=+-]?)(\d+)$/);
      if (!m) { inp.classList.add('err'); return; }
      setPending(r.id, m[1] === '=' ? Number(m[2]) : amountOf(r) + (m[1] === '-' ? -1 : 1) * Number(m[2]));
      drawStocks();
      const ids = rows.map(x => x.id), next = ids[ids.indexOf(r.id) + 1];          // курсор — на следующий размер
      root.querySelector(next ? `[data-qty="${CSS.escape(next)}"]` : `[data-qty="${CSS.escape(r.id)}"]`)?.focus();
    });
    body.querySelectorAll('[data-zero]').forEach(b => b.onclick = async () => {
      const [cid, wh, nm] = b.dataset.zero.split(':');
      const sizes = st.data.rows.filter(r => r.cabinet_id === cid && String(r.warehouse_id) === wh && String(r.nm_id) === nm && r.amount > 0);
      const extra = st.data.rows.filter(r => r.cabinet_id === cid && String(r.warehouse_id) === wh && String(r.nm_id) === nm && r.amount === 0 && st.pending.has(r.id));
      if (!sizes.length) { extra.forEach(r => st.pending.delete(r.id)); return drawStocks(); }
      const total = sizes.reduce((s, r) => s + r.amount, 0), r0 = sizes[0];
      if (!confirm(`Обнулить остатки ${r0.vendor} на складе «${r0.warehouse}»?\n\nРазмеров: ${sizes.length}, всего ${total} шт. Изменение сразу уйдёт на WB.`)) return;
      b.disabled = true; b.textContent = 'Обнуляю…';
      try {
        const res = await api('/api/stocks', {method: 'PUT', body: {changes: sizes.map(r => ({id: r.id, amount: 0}))}});
        sizes.concat(extra).forEach(r => { if (!res.errors?.[r.id]) st.pending.delete(r.id); });
        const failed = Object.entries(res.errors || {});
        if (failed.length) alert(`Обнулено: ${res.ok}. Не удалось: ${failed.length}\n` + failed.slice(0, 10).map(([k, v]) => `${k.split(':')[2]}: ${v}`).join('\n'));
        await load();
      } catch (err) { alert(err.message); b.disabled = false; b.textContent = '⦸ Обнулить'; }
    });
    body.querySelectorAll('[data-arch]').forEach(b => b.onclick = async () => {
      const to = b.dataset.to === '1';
      b.disabled = true;
      try {
        await api('/api/stocks/archive', {method: 'PUT', body: {keys: [b.dataset.arch], archived: to}});
        st.data.rows.forEach(r => { if (`${r.cabinet_id}:${r.nm_id}` === b.dataset.arch) { r.archived = to; r.watched = !to && !r.muted; if (to) { st.pending.delete(r.id); st.sel.delete(r.id); } } });
        if (!to && st.f.status === 'archive' && !st.data.rows.some(r => r.archived)) st.f.status = 'all';
        drawStocks();
      } catch (err) { alert(err.message); b.disabled = false; }
    });
    body.querySelectorAll('[data-undo]').forEach(b => b.onclick = () => { st.pending.delete(b.dataset.undo); drawStocks(); });
    body.querySelectorAll('[data-thr]').forEach(inp => inp.onchange = async () => {
      try { await api('/api/stocks/watch', {method: 'PUT', body: {ids: [inp.dataset.thr], threshold: inp.value === '' ? null : Number(inp.value)}});
        const r = st.data.rows.find(x => x.id === inp.dataset.thr); r.threshold = inp.value === '' ? null : Number(inp.value); r.limit = r.threshold ?? st.data.settings.threshold;
        keepFocus('[data-thr]', drawStocks); }
      catch (err) { alert(err.message); }
    });
    body.querySelectorAll('[data-watch]').forEach(b => b.onclick = async () => {
      const on = b.getAttribute('aria-checked') !== 'true';
      try { await api('/api/stocks/watch', {method: 'PUT', body: {ids: [b.dataset.watch], muted: !on}}); const r = st.data.rows.find(x => x.id === b.dataset.watch); r.muted = !on; r.watched = on; drawStocks(); }
      catch (err) { alert(err.message); }
    });
    const bulk = body.querySelector('[data-bulk]');
    if (bulk) {
      let mode = 'add';
      bulk.querySelectorAll('[data-mode]').forEach(b => b.onclick = () => { mode = b.dataset.mode; });
      bulk.onsubmit = async e => {
        e.preventDefault();
        const n = Number(bulk.elements.n.value);
        if (mode === 'thr') {
          try { await api('/api/stocks/watch', {method: 'PUT', body: {ids: [...st.sel], threshold: n}}); await load(); } catch (err) { alert(err.message); }
          return;
        }
        st.sel.forEach(id => { const r = st.data.rows.find(x => x.id === id); if (r) setPending(id, mode === 'set' ? n : amountOf(r) + n); });
        drawStocks();
      };
    }
    body.querySelectorAll('[data-mute]').forEach(b => b.onclick = async () => {
      try { await api('/api/stocks/watch', {method: 'PUT', body: {ids: [...st.sel], muted: b.dataset.mute === '1'}}); await load(); } catch (err) { alert(err.message); }
    });
    body.querySelector('[data-sel-all]')?.addEventListener('click', () => { rows.forEach(r => st.sel.add(r.id)); drawStocks(); });
    body.querySelector('[data-sel-clear]')?.addEventListener('click', () => { st.sel.clear(); drawStocks(); });
    body.querySelector('[data-show-changed]')?.addEventListener('click', () => { st.f.status = 'changed'; drawStocks(); });
    body.querySelector('[data-discard]')?.addEventListener('click', () => { if (confirm('Отменить все несохранённые изменения?')) { st.pending.clear(); if (st.f.status === 'changed') st.f.status = 'all'; drawStocks(); } });
    body.querySelector('[data-save]')?.addEventListener('click', save);
  }

  async function save() {
    st.busy = true; drawStocks();
    const changes = [...st.pending].map(([id, amount]) => ({id, amount}));
    try {
      const res = await api('/api/stocks', {method: 'PUT', body: {changes}});
      const failed = Object.entries(res.errors || {});
      changes.forEach(c => { if (!res.errors?.[c.id]) st.pending.delete(c.id); });
      if (failed.length) alert(`Сохранено: ${res.ok}. Не удалось: ${failed.length}\n` + failed.slice(0, 10).map(([k, v]) => `${k.split(':')[2]}: ${v}`).join('\n'));
      if (st.f.status === 'changed' && !st.pending.size) st.f.status = 'all';
      await load();
    } catch (e) { alert(e.message); }
    st.busy = false; drawStocks();
  }

  // ---------------------------------------------------------------- история
  async function drawHistory() {
    const body = root.querySelector('[data-body]');
    try { st.hist = await api('/api/stocks/history?days=' + st.histDays); } catch (e) { body.innerHTML = `<p class="rv-error">${esc(e.message)}</p>`; return; }
    if (st.tab !== 'history') return;
    body.innerHTML = `<div class="db-filters"><select data-hdays aria-label="Период">${[[1, 'Сегодня'], [7, '7 дней'], [30, '30 дней'], [90, '90 дней']].map(([d, t]) => `<option value="${d}" ${st.histDays === d ? 'selected' : ''}>${t}</option>`).join('')}</select></div>
      <div class="tr-table-wrap"><table class="tr-table"><thead><tr><th>Когда</th><th>Кто</th><th>Артикул</th><th>Размер</th><th class="r">Было</th><th class="r">Стало</th><th class="r">Изменение</th><th>Статус</th></tr></thead><tbody>
      ${st.hist.map(h => `<tr><td>${dt(h.at)}</td><td>${esc(h.author)}</td><td><b>${esc(h.vendor)}</b></td><td>${esc(h.size || '—')}</td><td class="r">${num(h.old)}</td><td class="r">${num(h.new)}</td>
        <td class="r ${h.new - h.old > 0 ? 'pos' : 'neg'}">${h.new - h.old > 0 ? '+' : ''}${num(h.new - h.old)}</td>
        <td>${h.status === 'ok' ? '<span class="tr-chip ok">Сохранено</span>' : `<span class="tr-chip bad">Ошибка</span> <small>${esc(h.error)}</small>`}</td></tr>`).join('')
        || '<tr><td colspan="8" class="muted">За период изменений не было.</td></tr>'}</tbody></table></div>`;
    body.querySelector('[data-hdays]').onchange = e => { st.histDays = Number(e.target.value); drawHistory(); };
  }

  const style = document.createElement('style');
  style.textContent = `
.sk-tabs{margin:12px 0 14px;width:max-content;max-width:100%}
.sk-filters{align-items:center}.sk-filters input[type=search]{min-width:220px;background-image:none;padding:0 12px}
.sk-range{display:inline-flex;align-items:center;gap:6px;font-size:13px;color:#4b5563}.db-filters .sk-range input{width:76px;padding:0 8px;background-image:none}
.sk-set{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px 16px;margin-top:0}.sk-set label{display:grid;gap:4px;font-size:13px;font-weight:600;color:#374151}
.sk-set label.rp-check{display:flex;grid-column:1/-1;white-space:normal}.sk-set input[type=number]{height:38px;border:1px solid #e3e5ea;border-radius:9px;padding:0 10px;font:inherit;max-width:200px}
.sk-set p,.sk-set .tr-actions{grid-column:1/-1;margin:0}.sk-set p{font-size:12px}
.sk-chips{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:4px 0 12px}
.sk-chip{border:1px solid #e5e7eb;background:#fff;border-radius:999px;padding:6px 12px;font:inherit;font-size:13px;font-weight:600;color:#374151;cursor:pointer}
.sk-chip b{font-weight:700;color:#6b7280;margin-left:3px}.sk-chip.on{background:#1f2328;border-color:#1f2328;color:#fff}.sk-chip.on b{color:#d1d5db}
.sk-chip.low:not(.on) b{color:#b45309}.sk-chip.zero:not(.on) b{color:#b42323}.sk-count{margin-left:auto;font-size:12px}
.sk-group{border:1px solid #eceef2;border-left:4px solid #d1d5db;border-radius:12px;background:#fff;margin-bottom:10px;overflow:hidden}
.sk-group.ok{border-left-color:#7fbd80}.sk-group.arch{border-left-color:#c4c4cc;opacity:.85}.sk-group.arch header{background:#f6f6f8}
.sk-arch{height:30px;border:1px solid #e3e5ea;background:#fff;border-radius:8px;padding:0 10px;font:inherit;font-size:12px;font-weight:600;color:#4b5563;cursor:pointer;white-space:nowrap}
.sk-arch:hover{border-color:#8b5cf6;color:#5b21b6}.sk-zero{color:#b42323}.sk-zero:hover{border-color:#e03131!important;color:#b42323!important;background:#fdf2f2}
.sk-arch:disabled{opacity:.45;cursor:not-allowed}.sk-chip.archive:not(.on){color:#4b5563}
.sk-vel b{font-variant-numeric:tabular-nums}.sk-vel small{display:block}.sk-vel small.neg{color:#b42323;font-weight:600}.sk-vel small.warn{color:#b45309}.sk-group.low{border-left-color:#f0b429}.sk-group.zero{border-left-color:#e03131}
.sk-group header{display:flex;align-items:center;gap:10px;padding:10px 14px;background:#fafbfc;flex-wrap:wrap}
.sk-group header img,.sk-noimg{width:36px;height:48px;border-radius:6px;object-fit:cover;background:#f1f2f4;flex:none}
.sk-fold{border:0;background:none;font-size:14px;cursor:pointer;color:#6b7280;width:20px}
.sk-g-main{flex:1;min-width:220px}.sk-g-main b{font-size:15px}.sk-g-main small{display:block;font-size:12px;color:#6b7280}
.sk-g-stats{display:flex;gap:14px;align-items:center;font-size:12px;color:#4b5563;flex-wrap:wrap}.sk-g-stats b{font-size:14px;color:#111827}.sk-g-stats .neg b{color:#b42323}
.sk-table{width:100%;border-collapse:collapse;font-size:13px}.sk-table th,.sk-table td{padding:7px 10px;border-top:1px solid #f0f1f4;text-align:left;text-transform:none;letter-spacing:normal;vertical-align:middle}
.sk-table th{font-size:12px;font-weight:600;color:#6b7280;background:#fff}.sk-table .r{text-align:right}.sk-table .c{width:30px;text-align:center}
.sk-table td small{display:block;font-size:11px;color:#6b7280}.sk-bc{font-variant-numeric:tabular-nums;color:#4b5563;font-size:12px}
.sk-table tr.sk-changed td{background:#f5f3ff}
.sk-amount{display:inline-flex;align-items:baseline;gap:4px;border-radius:8px;padding:3px 9px;font-variant-numeric:tabular-nums;font-size:14px}
.sk-amount.ok{background:#e9f5ea;color:#2f6b2f}.sk-amount.low{background:#fff4d6;color:#92400e}.sk-amount.zero{background:#fbe3e3;color:#b42323}.sk-amount s{color:#9ca3af;font-size:12px}
.sk-quick{display:flex;gap:4px;align-items:center;flex-wrap:nowrap}
.sk-quick button:not(.rv-link){height:30px;min-width:38px;border:1px solid #e3e5ea;background:#fff;border-radius:8px;font:inherit;font-size:12px;font-weight:700;color:#374151;cursor:pointer}
.sk-quick button:not(.rv-link):hover{border-color:#8b5cf6;color:#5b21b6}.sk-quick button:disabled{opacity:.4;cursor:not-allowed}
.sk-quick button.sk-zero{color:#b42323}.sk-quick button.sk-zero:hover{border-color:#e03131;color:#b42323;background:#fdf2f2}
.sk-quick input{width:84px;height:30px;border:1px solid #e3e5ea;border-radius:8px;padding:0 8px;font:inherit;font-size:13px}.sk-quick input:focus{outline:2px solid #ddd6fe;border-color:#8b5cf6}
.sk-quick input.err{border-color:#e03131}
.sk-thr{width:70px;height:30px;border:1px solid #e3e5ea;border-radius:8px;padding:0 8px;font:inherit}.sk-upd{font-size:12px;white-space:nowrap}
.sk-bar{bottom:64px}.sk-save{background:#5b21b6}.sk-save span{font-weight:700}
.sk-bulk{display:flex;gap:6px;align-items:center}.sk-bulk input{width:80px;height:34px;border:0;border-radius:8px;padding:0 8px;font:inherit}
.sk-bulk .btn{height:34px;padding:0 10px;font-size:12px;background:#fff;color:#1f2328;border:0}
.sk-save .btn{background:#fff;color:#5b21b6;border:0}
.sk-bar:not(:last-child){margin-bottom:6px}
@media(max-width:900px){.sk-table thead{display:none}.sk-table tr{display:grid;grid-template-columns:30px 1fr auto;gap:4px 8px;padding:8px 6px;border-top:1px solid #f0f1f4}
  .sk-table td{border:0;padding:2px 4px}.sk-table td:nth-child(3),.sk-table td:nth-child(9){display:none}.sk-table td:nth-child(6){grid-column:2/-1}
  .sk-set{grid-template-columns:1fr}.sk-filters input[type=search]{min-width:0;width:100%}}`;
  document.head.appendChild(style);

  return {render};
})();
